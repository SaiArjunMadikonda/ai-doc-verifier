"""
Local web dashboard for the documentation checker.

    python web/server.py            # then open http://127.0.0.1:8765

The overview reads a local project. The API workbench checks pasted text separately.

* ``GET /`` and ``GET /api/workspace`` show the selected repository audit and meeting decisions.
* ``GET /portfolio.html`` exports the bundled demo as an offline dashboard.
* ``GET /workbench`` opens the input editor.
* ``POST /api/audit``  JSON body with ``spec`` (OpenAPI JSON or YAML), ``docs`` (the doc
  text), ``format`` (``md``, ``rst`` or ``adoc``) and optionally ``meeting`` (meeting notes:
  Teams ``.vtt`` text or plain ``Name: text`` lines)
* ``GET  /api/demo``   the bundled demo project (spec read from its code, plus its API docs)

Pasted inputs are parsed locally. Optional GitHub activity is enabled by an explicit
command-line setting. The server listens on 127.0.0.1 by default.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from web.workspace import dashboard, snapshot  # noqa: E402

from api_contract import (  # noqa: E402
    Spec,
    SpecError,
    load_spec_from_code,
    normalize_path,
    verify_docs,
)
from meeting_notes import (  # noqa: E402
    Corpus,
    parse_text,
    parse_vtt,
    verify_meeting,
)

PAGE = Path(__file__).with_name("index.html")
DEMO_DIR = ROOT / "examples" / "shop-demo"
MAX_BODY = 1_000_000
FORMATS = {"md": ".md", "rst": ".rst", "adoc": ".adoc"}


class BadRequest(Exception):
    pass


def parse_spec(text: str) -> Spec:
    if not text.strip():
        raise BadRequest("Paste an OpenAPI spec (JSON or YAML) first.")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        try:
            import yaml

            data = yaml.safe_load(text)
        except Exception as e:  # noqa: BLE001 - any YAML error means "not a spec"
            raise BadRequest(f"The spec is neither valid JSON nor valid YAML: {e}") from e
    try:
        return Spec(data, source="your spec")
    except (SpecError, AttributeError, TypeError) as e:
        raise BadRequest(
            str(e) if isinstance(e, SpecError) else "That is not an OpenAPI document."
        ) from e


def analyze(payload: dict) -> dict:
    """Run the API checks on pasted text and return JSON-ready results."""
    docs = payload.get("docs")
    if not isinstance(docs, str) or not docs.strip():
        raise BadRequest("Paste the documentation to check.")
    fmt = payload.get("format", "md")
    if fmt not in FORMATS:
        raise BadRequest("format must be md, rst or adoc.")
    spec = parse_spec(str(payload.get("spec", "")))
    with tempfile.TemporaryDirectory() as tmp:
        name = f"docs{FORMATS[fmt]}"
        (Path(tmp) / name).write_text(docs, encoding="utf-8")
        report = verify_docs(spec, [tmp], tmp)
    out = report.to_dict()
    out["endpoints_in_spec"] = len(spec.operations)
    meeting = payload.get("meeting")
    if isinstance(meeting, str) and meeting.strip():
        out["meeting"] = check_meeting(meeting, str(payload.get("spec", "")), docs, name)
    return out


def check_meeting(meeting: str, spec_text: str, docs: str, doc_name: str) -> dict:
    """Check pasted meeting notes against the pasted spec and docs (no repository here)."""
    spec = parse_spec(spec_text)
    utterances = (
        parse_vtt(meeting) if meeting.lstrip().startswith("WEBVTT") else parse_text(meeting)
    )
    corpus = Corpus(
        code={"the spec": spec_text},
        docs={doc_name: docs},
        routes={normalize_path(p) for (_m, p) in spec.operations},
        api_only=True,
    )
    return verify_meeting(utterances, corpus, "your meeting notes").to_dict()


def demo() -> dict:
    spec = load_spec_from_code(DEMO_DIR)
    docs = (DEMO_DIR / "docs" / "api.md").read_text(encoding="utf-8")
    meeting = (DEMO_DIR / "meetings" / "api-sync.vtt").read_text(encoding="utf-8")
    return {
        "spec": json.dumps(spec.data, indent=2),
        "docs": docs,
        "format": "md",
        "meeting": meeting,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "DocCheck/1.0"

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, data: dict) -> None:
        self._send(status, json.dumps(data).encode(), "application/json; charset=utf-8")

    def do_GET(self):  # noqa: N802 - http.server API
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            self._send(200, dashboard().encode(), "text/html; charset=utf-8")
        elif path == "/workbench":
            self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/workspace":
            try:
                self._json(200, snapshot(**self.server.workspace))
            except (SpecError, OSError, ValueError, sqlite3.Error, subprocess.SubprocessError) as e:
                self._json(500, {"error": f"Workspace unavailable: {e}"})
        elif path == "/portfolio.html":
            self._send(200, dashboard(snapshot()).encode(), "text/html; charset=utf-8")
        elif path == "/api/demo":
            try:
                self._json(200, demo())
            except (SpecError, OSError) as e:
                self._json(500, {"error": f"Demo unavailable: {e}"})
        else:
            self._json(404, {"error": "Not found."})

    def do_POST(self):  # noqa: N802
        if self.path.split("?")[0] != "/api/audit":
            return self._json(404, {"error": "Not found."})
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            return self._json(413, {"error": "Input is too large (limit 1 MB)."})
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(payload, dict):
                raise BadRequest("Send a JSON object.")
            self._json(200, analyze(payload))
        except json.JSONDecodeError:
            self._json(400, {"error": "The request body is not valid JSON."})
        except BadRequest as e:
            self._json(400, {"error": str(e)})

    def log_message(self, fmt, *args):  # keep the console quiet
        pass


def make_server(host: str = "127.0.0.1", port: int = 8765, **workspace) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), Handler)
    server.workspace = workspace
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Documentation checker dashboard")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--host", default="127.0.0.1", help="Keep the default unless you trust your network"
    )
    parser.add_argument("--project", type=Path, default=DEMO_DIR)
    parser.add_argument("--docs", default="docs", help="Documentation path relative to the project")
    parser.add_argument("--notes", type=Path, action="append", default=[])
    parser.add_argument("--state", type=Path, help="Read the meeting sync SQLite database")
    parser.add_argument(
        "--release-notes", type=Path, help="Display an existing meeting release draft"
    )
    parser.add_argument(
        "--github-repo", help="Read meeting PRs and conflict issues via authenticated gh"
    )
    parser.add_argument("--base", help="Branch to match when reading GitHub PRs")
    parser.add_argument(
        "--export-demo", type=Path, help="Write the bundled demo as a standalone HTML file"
    )
    args = parser.parse_args(argv)
    if args.export_demo:
        args.export_demo.write_text(dashboard(snapshot()), encoding="utf-8")
        return 0
    if args.github_repo and not args.base:
        parser.error("--github-repo needs --base")
    server = make_server(
        args.host,
        args.port,
        project=args.project,
        docs=args.docs,
        notes=args.notes,
        state=args.state,
        release=args.release_notes,
        repo=args.github_repo,
        base=args.base,
    )
    print(f"Open http://{args.host}:{server.server_address[1]}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
