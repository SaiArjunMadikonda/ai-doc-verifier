"""Collect scoped Microsoft Teams transcripts and notes from a OneDrive folder."""

import json
import os
import sys
import tempfile
import uuid
import zipfile
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from meeting_notes import parse_text, parse_transcript, parse_vtt

GRAPH = "https://graph.microsoft.com/v1.0"
SUPPORTED = {".vtt", ".docx", ".txt", ".md"}
MAX_BYTES = 16 * 1024 * 1024


class GraphError(ValueError):
    def __init__(self, status):
        self.status = status
        super().__init__(f"Microsoft request failed (HTTP {status}). Check access or retry.")


class _LoopText(HTMLParser):
    """Collect visible text from Graph's rendered Loop HTML; preserve paragraph boundaries."""

    blocks = {"p", "div", "li", "tr", "br", "h1", "h2", "h3", "h4", "h5", "h6", "section"}
    ignored = {"head", "script", "style", "template"}

    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.ignored:
            self.hidden += 1
        elif not self.hidden and tag in self.blocks:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.ignored:
            self.hidden = max(0, self.hidden - 1)
        elif not self.hidden and tag in self.blocks:
            self.parts.append("\n")
        elif not self.hidden and tag in {"td", "th"}:
            self.parts.append(" ")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data.replace("\n", " "))


def loop_notes(body):
    page = _LoopText()
    page.feed(body.decode("utf-8-sig"))
    page.close()
    return parse_text(
        "\n".join(" ".join(line.split()) for line in "".join(page.parts).splitlines())
    )


class _Redirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Drive content redirects to a short-lived, preauthenticated download URL.
        if req.get_method() != "GET" or urlsplit(newurl).scheme != "https":
            raise ValueError("Unexpected redirect from Microsoft.")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None and urlsplit(req.full_url).netloc != urlsplit(newurl).netloc:
            redirected.remove_header("Authorization")
        return redirected


def _request(url, *, token=None, data=None, accept="application/json"):
    if token and not url.startswith(GRAPH + "/"):
        raise ValueError("Unexpected Microsoft Graph URL.")
    req = Request(url, data=data, headers={"Accept": accept})
    if token:
        req.add_unredirected_header("Authorization", "Bearer " + token)
    try:
        with build_opener(_Redirect()).open(req, timeout=45) as response:
            body = response.read(MAX_BYTES + 1)
    except HTTPError as exc:
        raise GraphError(exc.code) from None
    except (URLError, TimeoutError) as exc:
        raise ValueError("Could not reach Microsoft. Retry the sync.") from exc
    if len(body) > MAX_BYTES:
        raise ValueError("Microsoft response exceeds the 16 MiB import limit.")
    return body


def _access_token(env):
    try:
        tenant = uuid.UUID(env["MS_TENANT_ID"])
        form = urlencode(
            {
                "client_id": env["MS_CLIENT_ID"],
                "client_secret": env["MS_CLIENT_SECRET"],
                "grant_type": "client_credentials",
                "scope": "https://graph.microsoft.com/.default",
            }
        ).encode()
        url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
        return json.loads(_request(url, data=form))["access_token"]
    except KeyError as exc:
        raise ValueError(f"Missing Microsoft configuration or response field: {exc}") from None


def _pages(url, token):
    visited = set()
    while url:
        if url in visited:
            raise ValueError("Microsoft Graph returned a repeated page.")
        visited.add(url)
        page = json.loads(_request(url, token=token))
        yield from page["value"]
        url = page.get("@odata.nextLink")


def _segment(value):
    if not value or value in {".", ".."}:
        raise ValueError("Invalid Microsoft resource identifier.")
    return quote(value, safe="")


def read_export(path):
    if path.suffix.lower() not in SUPPORTED:
        raise ValueError("Choose a VTT, DOCX, text or Markdown notes file.")
    if path.stat().st_size > MAX_BYTES:
        raise ValueError("Meeting file exceeds the 16 MiB import limit.")
    if path.suffix.lower() == ".docx":
        try:
            with zipfile.ZipFile(path) as doc:
                if doc.getinfo("word/document.xml").file_size > MAX_BYTES:
                    raise ValueError("Word transcript exceeds the import limit.")
        except (zipfile.BadZipFile, KeyError) as exc:
            raise ValueError("The selected DOCX is not a readable Word transcript.") from exc
    return parse_transcript(path)


def _transcript(key, token):
    content = _request(
        key + "/content", token=token, accept="application/vnd.microsoft.graph.transcript+text"
    )
    text = content.decode("utf-8-sig")
    return parse_vtt(text) if text.lstrip().startswith("WEBVTT") else parse_text(text)


def _organizer_notes(organizer, token, previous, until):
    organizer = str(uuid.UUID(organizer))
    base = f"{GRAPH}/users/{organizer}/onlineMeetings"
    params = [f"meetingOrganizerUserId='{organizer}'", f"endDateTime={until}"]
    if previous:
        # An overlap catches artifacts that became visible late and makes retries harmless.
        start = datetime.fromisoformat(previous) - timedelta(days=1)
        params.append(f"startDateTime={start.isoformat().replace('+00:00', 'Z')}")
    for item in _pages(f"{base}/getAllTranscripts({','.join(params)})", token):
        key = f"{base}/{_segment(item['meetingId'])}/transcripts/{_segment(item['id'])}"
        yield key, _transcript(key, token)


def graph_notes(env=None, *, last_sync=lambda key: None, checkpoint=lambda key, value: None):
    """Yield (stable source key, parsed remarks). No raw note is retained."""
    env = os.environ if env is None else env
    organizer, meeting = env.get("TEAMS_ORGANIZER_ID"), env.get("TEAMS_MEETING_ID")
    drive, folder = env.get("ONEDRIVE_DRIVE_ID"), env.get("ONEDRIVE_FOLDER_ID")
    scope = env.get("TEAMS_SCOPE") or "meeting"
    if scope not in {"meeting", "organizer", "tenant"} or bool(drive) != bool(folder):
        raise ValueError("Configure a Teams scope and supply both IDs for a OneDrive folder.")
    if scope == "meeting" and (bool(organizer) != bool(meeting) or not (meeting or folder)):
        raise ValueError("Configure an organizer and meeting ID, a drive and folder ID, or both.")
    if scope == "organizer" and (not organizer or meeting):
        raise ValueError(
            "Organizer scope requires TEAMS_ORGANIZER_ID and an empty TEAMS_MEETING_ID."
        )
    if scope == "tenant" and (organizer or meeting):
        raise ValueError("Clear the individual Teams IDs before selecting tenant scope.")
    token = _access_token(env)
    with tempfile.TemporaryDirectory() as tmp:

        def parse(body, suffix):
            path = Path(tmp) / ("notes" + suffix)
            path.write_bytes(body)
            return read_export(path)

        if meeting:
            base = f"{GRAPH}/users/{_segment(organizer)}/onlineMeetings/{_segment(meeting)}/transcripts"
            for item in _pages(base, token):
                key = f"{base}/{_segment(item['id'])}"
                yield key, _transcript(key, token)
        if scope in {"organizer", "tenant"}:
            until = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
            organizers = (
                [organizer]
                if scope == "organizer"
                else [
                    u["id"]
                    for u in _pages(f"{GRAPH}/users?$select=id,accountEnabled,userType", token)
                    if u.get("accountEnabled") and u.get("userType") == "Member"
                ]
            )
            # Rotate larger tenants across runs without losing unvisited organizers.
            streams = [(last_sync(f"attempt:{o}") or "", o) for o in organizers]
            excluded = 0
            for _, user in sorted(streams)[:25]:
                previous = last_sync(f"organizer:{user}")
                try:
                    yield from _organizer_notes(user, token, previous, until)
                except GraphError as exc:
                    if scope != "tenant" or exc.status not in {403, 404}:
                        raise
                    excluded += 1
                else:
                    checkpoint(f"organizer:{user}", until)
                if scope == "tenant":
                    checkpoint(f"attempt:{user}", until)
            if excluded:
                print(
                    f"Microsoft denied access or returned 404 for {excluded} organizer(s). Their transcript checkpoints were retained for retry.",
                    file=sys.stderr,
                )
        if folder:
            items = f"{GRAPH}/drives/{_segment(drive)}/items"
            for item in _pages(f"{items}/{_segment(folder)}/children", token):
                suffix = Path(item["name"]).suffix.lower()
                if "file" in item and "remoteItem" not in item and suffix in SUPPORTED | {".loop"}:
                    key = f"{items}/{_segment(item['id'])}"
                    if suffix == ".loop":
                        yield (
                            key,
                            loop_notes(
                                _request(
                                    key + "/content?format=html", token=token, accept="text/html"
                                )
                            ),
                        )
                    else:
                        yield (
                            key,
                            parse(_request(key + "/content", token=token, accept="*/*"), suffix),
                        )
