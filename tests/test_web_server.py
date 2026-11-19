"""Tests for the local dashboard server (web/server.py)."""

import importlib.util
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from tests.api_fixtures import SPEC

_path = Path(__file__).resolve().parent.parent / "web" / "server.py"
_spec = importlib.util.spec_from_file_location("web_server", _path)
server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(server)

DOCS = "### GET /users\n\n| Name | Type |\n|---|---|\n| limit | integer |\n| nope | string |\n\n### GET /gone\n"


def test_analyze_reports_findings_and_health():
    out = server.analyze({"spec": json.dumps(SPEC), "docs": DOCS})
    verdicts = {(f["check"], f["verdict"]) for f in out["findings"]}
    assert ("endpoint", "contradicted") in verdicts and ("parameter", "verified") in verdicts
    assert out["endpoints_in_spec"] >= 5 and out["health"]["score"] is not None
    assert {f["line"] for f in out["findings"]} <= set(range(1, 12))


def test_analyze_accepts_yaml_and_rst():
    yaml = pytest.importorskip("yaml")
    out = server.analyze(
        {"spec": yaml.safe_dump(SPEC), "docs": "Users\n=====\n\n``GET /users``\n", "format": "rst"}
    )
    assert out["claims_checked"] >= 1


@pytest.mark.parametrize(
    "payload,needle",
    [
        ({"spec": "", "docs": "x"}, "Paste an OpenAPI spec"),
        ({"spec": json.dumps(SPEC), "docs": "  "}, "Paste the documentation"),
        ({"spec": "{nope", "docs": "x"}, "neither valid JSON nor valid YAML"),
        ({"spec": '{"openapi": "3.0.0"}', "docs": "x"}, "missing top-level 'paths'"),
        ({"spec": "[1, 2]", "docs": "x"}, "(?i)not an OpenAPI document"),
        ({"spec": json.dumps(SPEC), "docs": "x", "format": "pdf"}, "format must be"),
    ],
)
def test_analyze_rejects_bad_input(payload, needle):
    with pytest.raises(server.BadRequest, match=needle):
        server.analyze(payload)


def test_demo_payload_finds_planted_mistakes():
    demo = server.demo()
    out = server.analyze(demo)
    assert out["summary"]["contradicted"] >= 8


@pytest.fixture
def live():
    httpd = server.make_server("127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def call(url, body=None, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read(), resp.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers


def test_http_endpoints(live):
    status, body, headers = call(live + "/")
    assert status == 200 and b"Does your documentation still match your API?" in body
    assert headers["X-Content-Type-Options"] == "nosniff"
    status, body, _ = call(live + "/api/demo")
    assert status == 200 and "spec" in json.loads(body)
    status, body, _ = call(live + "/api/audit", {"spec": json.dumps(SPEC), "docs": DOCS})
    assert status == 200 and json.loads(body)["summary"]["contradicted"] >= 1
    assert call(live + "/api/audit", {"spec": "", "docs": "x"})[0] == 400
    assert call(live + "/api/audit", raw=b"not json")[0] == 400
    assert call(live + "/api/audit", raw=b"[1]")[0] == 400
    assert call(live + "/nope")[0] == 404
    assert call(live + "/api/other", {"a": 1})[0] == 404


def test_http_rejects_oversized_body(live):
    big = b"{" + b" " * (server.MAX_BODY + 10) + b"}"
    try:
        status = call(live + "/api/audit", raw=big)[0]
    except (BrokenPipeError, ConnectionResetError):
        status = 413  # the server refused before reading the body
    assert status == 413
