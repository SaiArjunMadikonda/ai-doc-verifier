"""Tests for suggested fixes, the health score, history and AI rewrites."""

import json
import textwrap
from datetime import UTC, datetime
from types import SimpleNamespace

import api_contract as ac
import health as h
import suggestions as sg
from api_contract import Spec, extract_claims, verify
from tests.api_fixtures import SPEC


def run(md, file="docs/api.md"):
    return verify(extract_claims(textwrap.dedent(md), file), Spec(SPEC, source="openapi.json"))


def fix_for(report, text):
    matches = [f for f in report.findings if text in f.message]
    assert matches, [f.message for f in report.findings]
    return matches[0].fix


# ------------------------------------------------------------------------ fix hints


def test_fix_hints_cover_every_contradiction_kind():
    report = run(
        """\
        ### GET /user

        ### DELETE /users

        ### GET /users

        | Name | Type | Required |
        |---|---|---|
        | limit | string | no |
        | q | string | no |
        | sort | string | no |

        Returns 500.
        """
    )
    contradictions = [f for f in report.findings if f.verdict == ac.CONTRADICTED]
    assert contradictions and all(f.fix for f in contradictions)
    assert fix_for(report, "does not define") == "Change `/user` to `/users`."
    assert "Use one of GET, POST" in fix_for(report, "`DELETE`")
    assert "Change the type of `limit` to `integer`" in fix_for(report, "Parameter `limit`")
    assert "Mark `q` as required" in fix_for(report, "Parameter `q`")
    assert "Remove `sort`" in fix_for(report, "Parameter `sort`")
    assert "Remove `500`" in fix_for(report, "Response `500`")


def test_fix_when_no_similar_path():
    report = run("GET /zzz-completely-different\n")
    assert "Remove `GET /zzz-completely-different`" in report.findings[0].fix


def test_verified_findings_have_no_fix():
    report = run("GET /health\n")
    assert all(not f.fix for f in report.findings)


def test_render_shows_fix_and_ai_rewrite():
    report = run("DELETE /users\n")
    report.findings[0].ai_fix = "Use GET or POST."
    md = ac.render_markdown(report)
    assert "💡 Suggested fix: Use one of GET, POST" in md
    assert "🤖 AI-suggested rewrite (review before use): Use GET or POST." in md


# ------------------------------------------------------------------------ health


def test_health_score_formula_and_per_file():
    report = run("GET /health\n", "good.md")
    report.findings += run("GET /nope\n", "bad.md").findings
    report.findings += run("GET /gateway/health\n", "maybe.md").findings
    health = h.compute_health(report.findings)
    assert health.per_file["good.md"].score == 100
    assert health.per_file["bad.md"].score == 0
    assert health.per_file["maybe.md"].score == 50  # one uncertain counts half
    assert health.score == round(100 * (1 + 0.5) / 3, 1)
    assert [name for name, _ in health.worst_files(2)] == ["bad.md", "maybe.md"]


def test_health_without_checks_is_none():
    health = h.compute_health([])
    assert health.score is None
    assert h.render_health(health) == []
    assert h.FileHealth().score is None


def test_health_in_report_dict_and_markdown():
    report = run("GET /health\n\nGET /nope\n")
    data = report.to_dict()
    assert data["health"]["score"] == 50
    assert data["health"]["files"]["docs/api.md"]["contradicted"] == 1
    assert "Documentation health: 50 / 100" in ac.render_markdown(report)


def test_history_roundtrip_and_trend(tmp_path):
    path = tmp_path / "state" / "health.json"
    bad = h.compute_health(run("GET /nope\n").findings)
    good = h.compute_health(run("GET /health\n").findings)
    when = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
    h.append_history(path, bad, when, label="before")
    history = h.append_history(path, good, when)
    assert [e["score"] for e in history] == [0, 100]
    assert history[0]["time"] == "2026-11-19T12:00:00Z" and history[0]["label"] == "before"
    lines = "\n".join(h.render_health(good, history))
    assert "+100 since last run" in lines and "Trend:" in lines


def test_history_tolerates_garbage_and_caps_length(tmp_path):
    path = tmp_path / "h.json"
    path.write_text("not json")
    assert h.load_history(path) == []
    path.write_text(json.dumps({"not": "a list"}))
    assert h.load_history(path) == []
    health = h.compute_health(run("GET /health\n").findings)
    for _ in range(5):
        history = h.append_history(path, health, keep=3)
    assert len(history) == 3


def test_sparkline():
    assert h.sparkline([]) == ""
    assert h.sparkline([50, 50]) == "██"
    line = h.sparkline([0, None, 100])
    assert line[0] == "▁" and line[1] == " " and line[2] == "█"


def test_cli_history_flag(tmp_path, monkeypatch, capsys):
    (tmp_path / "openapi.json").write_text(json.dumps(SPEC))
    (tmp_path / "api.md").write_text("### GET /health\n")
    monkeypatch.chdir(tmp_path)
    hist = tmp_path / "hist.json"
    assert ac.main(["--history", str(hist), "api.md"]) == 0
    assert ac.main(["--history", str(hist), "api.md"]) == 0
    assert len(json.loads(hist.read_text())) == 2
    assert "Trend:" in capsys.readouterr().out


# ------------------------------------------------------------------------ AI rewrites


class FakeClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, model, messages):
        self.prompts.append(messages[-1]["content"])
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=reply))])


def test_snippet_and_prompt_contain_evidence():
    text = "\n".join(f"line {i}" for i in range(1, 30))
    snippet = sg.snippet_around(text, 10, before=1, after=2)
    assert snippet.splitlines() == ["line 9", "line 10", "line 11", "line 12"]
    report = run("DELETE /users\n")
    prompt = sg.build_prompt(report.findings[0], snippet)
    assert "openapi.json#/paths/~1users" in prompt and "Use one of GET, POST" in prompt


def test_clean_ai_text():
    assert sg.clean_ai_text("```md\nUse  GET.\n```") == "Use GET."
    assert sg.clean_ai_text(None) == ""
    assert len(sg.clean_ai_text("x" * 5000)) == sg.MAX_AI_FIX_CHARS


def test_refine_fixes_only_touches_contradictions_and_survives_errors():
    report = run("GET /health\n\nDELETE /users\n\nGET /nope\n")
    client = FakeClient([RuntimeError("boom"), "Use `GET /nope2`."])
    done = sg.refine_fixes(report, client, "m", lambda path: "doc text\n" * 5)
    assert done == 1
    assert len(client.prompts) == 2  # only the 2 contradictions were sent
    by_label = {f.claim.label: f for f in report.findings if f.check == "endpoint"}
    assert by_label["DELETE /users"].ai_fix == ""  # that call raised
    assert by_label["GET /nope"].ai_fix == "Use `GET /nope2`."
    assert by_label["GET /health"].ai_fix == ""


def test_refine_fixes_respects_max_items():
    report = run("GET /a\n\nGET /b\n\nGET /c\n")
    client = FakeClient(["one", "two", "three"])
    assert sg.refine_fixes(report, client, "m", lambda p: "x", max_items=2) == 2
    assert len(client.prompts) == 2


def test_apply_ai_rewrites_needs_configuration(monkeypatch, capsys):
    monkeypatch.delenv("MODEL_API_BASE", raising=False)
    assert ac.apply_ai_rewrites(run("GET /nope\n")) == 0
    assert "MODEL_API_BASE" in capsys.readouterr().out
