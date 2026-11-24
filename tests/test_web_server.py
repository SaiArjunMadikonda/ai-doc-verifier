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
    assert status == 200 and b"Workspace overview" in body
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert b"Does your documentation still match your API?" in call(live + "/workbench")[1]
    status, body, _ = call(live + "/api/workspace")
    assert status == 200 and json.loads(body)["demo"] is True
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
    except (BrokenPipeError, ConnectionResetError, urllib.error.URLError):
        status = 413  # the server refused before reading the body (the socket was reset)
    assert status == 413


def test_meeting_notes_are_checked_against_the_pasted_spec_and_docs():
    meeting = (
        "Ana: The /gone endpoint was retired.\nBo: We will rename /users to /people.\nCy: Lunch?\n"
    )
    out = server.analyze({"spec": json.dumps(SPEC), "docs": DOCS, "meeting": meeting})
    by = {f["subject"]: f for f in out["meeting"]["findings"]}
    assert by["`/gone`"]["verdict"] == "contradicted"
    assert by["`/users` → `/people`"]["pending"] is True
    assert out["meeting"]["counts"]["contradicted"] == 1


def test_meeting_is_optional_and_vtt_is_detected():
    assert "meeting" not in server.analyze({"spec": json.dumps(SPEC), "docs": DOCS})
    vtt = "WEBVTT\n\n00:00:01.000 --> 00:00:04.000\n<v Ana>The /gone endpoint was retired.</v>\n"
    out = server.analyze({"spec": json.dumps(SPEC), "docs": DOCS, "meeting": vtt})
    assert out["meeting"]["findings"][0]["time"] == "00:00:01"


def test_demo_includes_a_meeting():
    d = server.demo()
    assert d["meeting"].startswith("WEBVTT")
    out = server.analyze(d)
    assert out["meeting"]["findings"]


def test_meeting_in_the_dashboard_does_not_guess_about_source_files():
    notes = "Ana: shop/config.py was removed.\nBo: We renamed --format to --output-format.\n"
    out = server.analyze({"spec": json.dumps(SPEC), "docs": DOCS, "meeting": notes})
    assert {f["verdict"] for f in out["meeting"]["findings"]} == {"uncertain"}
    assert all("only the API definition" in f["message"] for f in out["meeting"]["findings"])


def test_workspace_shows_real_audit_edits_and_evaluation():
    data = server.snapshot()
    assert data["health"]["score"] == 55.7 and len(data["findings"]) == 44
    assert data["decision_counts"] == {"ready": 2, "pending": 3, "verified": 1}
    edits = [edit for d in data["decisions"] for edit in d["edits"]]
    assert any("--output-format" in e["after"] and e["file"] == "docs/setup.md" for e in edits)
    assert data["evaluation"]["cases"] == 24 and not data["evaluation"]["problems"]
    assert data["release"] is None and data["github"] is None
    rename = next(d for d in data["decisions"] if d["old"] == "--format")
    assert rename["sources"][0]["time"] and rename["sources"][0]["speaker"] == "Dana Ortiz"


def test_dashboard_exports_only_bundled_project(tmp_path):
    import re

    out = tmp_path / "portfolio.html"
    assert server.main(["--project", str(tmp_path), "--export-demo", str(out)]) == 0
    html = out.read_text(encoding="utf-8")
    embedded = re.search(
        r'<script type="application/json" id="snapshot">(.*?)</script>', html, re.S
    )
    assert json.loads(embedded[1])["project"] == "shop-demo"
    assert "/* DASHBOARD_APP */" not in html and "/* DASHBOARD_STYLES */" not in html
    assert "</script><unsafe>" not in server.dashboard({"project": "</script><unsafe>"})


def test_custom_project_from_saved_decisions_is_read_only(tmp_path):
    from meeting_notes import parse_text
    from meeting_sync import remember

    project = tmp_path / "sample"
    (project / "docs").mkdir(parents=True)
    (project / "cli.py").write_text("flag = '--new'")
    (project / "docs/guide.md").write_text("Use --old.")
    state = tmp_path / "decisions.db"
    remember(state, [("meeting", parse_text("We renamed `--old` to `--new`."))])
    before = state.read_bytes()
    release = tmp_path / "draft.md"
    release.write_text("An approved release entry")
    data = server.snapshot(project, state=state, release=release)
    assert data["demo"] is False and data["decisions"][0]["status"] == "ready"
    assert data["release"] == release.read_text() and state.read_bytes() == before


def test_workspace_reads_only_matching_github_activity(tmp_path, monkeypatch):
    from web import workspace

    repo = "org/app"
    branch = "meeting-docs/" + "a" * 24
    pull = {
        "head": {"ref": branch, "repo": {"full_name": repo}},
        "base": {"ref": "main"},
        "number": 7,
        "state": "open",
        "draft": True,
        "title": "Update guide",
        "requested_reviewers": [{"login": "editor"}],
    }

    def pages(run, root, path):
        if "/pulls?" in path:
            return [
                pull,
                {**pull, "base": {"ref": "other"}},
                {**pull, "head": {"ref": branch, "repo": {"full_name": "fork/app"}}},
            ]
        return [
            {
                "number": 8,
                "title": "Two proposed names",
                "state": "open",
                "body": "<!-- meeting-docs-conflict:" + "b" * 24 + " -->",
            },
            {"number": 9, "title": "Unrelated issue", "body": ""},
        ]

    monkeypatch.setattr(workspace, "pages", pages)
    result = workspace.github_activity(tmp_path, repo, "main")
    assert [(p["number"], p["owners"], p["status"]) for p in result["pulls"]] == [
        (7, ["editor"], "draft")
    ]
    assert [i["number"] for i in result["issues"]] == [8]
