"""Read transcripts for one Teams meeting and exported notes in one OneDrive folder."""

import json
import os
import tempfile
import uuid
import zipfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from meeting_notes import parse_text, parse_transcript, parse_vtt

GRAPH = "https://graph.microsoft.com/v1.0"
SUPPORTED = {".vtt", ".docx", ".txt", ".md"}
MAX_BYTES = 16 * 1024 * 1024


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
        raise ValueError(
            f"Microsoft request failed (HTTP {exc.code}). Check access or retry."
        ) from None
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


def graph_notes(env=None):
    """Yield (stable source key, parsed remarks). No raw note is retained."""
    env = os.environ if env is None else env
    organizer, meeting = env.get("TEAMS_ORGANIZER_ID"), env.get("TEAMS_MEETING_ID")
    drive, folder = env.get("ONEDRIVE_DRIVE_ID"), env.get("ONEDRIVE_FOLDER_ID")
    if bool(organizer) != bool(meeting) or bool(drive) != bool(folder) or not (meeting or folder):
        raise ValueError("Configure an organizer and meeting ID, a drive and folder ID, or both.")
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
                # The pilot only needs the words. This format also works when the tenant
                # disables speaker attribution; avoid asking for participant identities.
                content = _request(
                    key + "/content",
                    token=token,
                    accept="application/vnd.microsoft.graph.transcript+text",
                )
                text = content.decode("utf-8-sig")
                yield (
                    key,
                    parse_vtt(text) if text.lstrip().startswith("WEBVTT") else parse_text(text),
                )
        if folder:
            items = f"{GRAPH}/drives/{_segment(drive)}/items"
            for item in _pages(f"{items}/{_segment(folder)}/children", token):
                suffix = Path(item["name"]).suffix.lower()
                if "file" in item and "remoteItem" not in item and suffix in SUPPORTED:
                    key = f"{items}/{_segment(item['id'])}"
                    yield key, parse(_request(key + "/content", token=token, accept="*/*"), suffix)
