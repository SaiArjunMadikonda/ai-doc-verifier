"""
Local web dashboard for the documentation checker.

    python web/server.py            # then open http://127.0.0.1:8765

It serves one page and one endpoint, using only the Python standard library:

* ``POST /api/audit``  JSON body with ``spec`` (OpenAPI JSON or YAML), ``docs`` (the doc
  text) and ``format`` (``md``, ``rst`` or ``adoc``)
* ``GET  /api/demo``   the bundled demo project (spec read from its code, plus its API docs)

Everything runs on your machine. The spec and docs you paste are only parsed: no
code is executed and nothing is sent anywhere. The server listens on 127.0.0.1 only.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from api_contract import Spec, SpecError, load_spec_from_code, verify_docs  # noqa: E402

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
    return out


def demo() -> dict:
    spec = load_spec_from_code(DEMO_DIR)
    docs = (DEMO_DIR / "docs" / "api.md").read_text(encoding="utf-8")
    return {"spec": json.dumps(spec.data, indent=2), "docs": docs, "format": "md"}


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
            self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
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


def make_server(host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), Handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Documentation checker dashboard")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--host", default="127.0.0.1", help="Keep the default unless you trust your network"
    )
    args = parser.parse_args(argv)
    server = make_server(args.host, args.port)
    print(f"Open http://{args.host}:{server.server_address[1]}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
