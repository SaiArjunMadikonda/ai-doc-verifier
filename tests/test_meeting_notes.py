"""Meeting notes: transcript parsing, statement finding, verdicts, AI guard rails, CLI."""

import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

import docaudit
import meeting_notes as mn

DEMO = Path(__file__).resolve().parent.parent / "examples" / "shop-demo"
VTT = DEMO / "meetings" / "api-sync.vtt"


# ------------------------------------------------------------------ parsing


def test_vtt_parsing_keeps_speaker_time_and_separates_remarks():
    u = mn.parse_vtt(VTT.read_text(encoding="utf-8"))
    assert u[0].speaker == "Priya Shah" and u[0].time == "00:00:03"
    assert len(u) == 11
    assert u[1].line > u[0].line


def test_vtt_joins_a_sentence_split_over_captions():
    text = (
        "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n<v Ann>We are renaming `--a` to\n\n"
        "00:00:02.500 --> 00:00:03.000\n<v Ann>`--b` today.</v>\n"
    )
    (only,) = mn.parse_vtt(text)
    assert "`--a` to `--b`" in only.text


def test_plain_text_formats():
    text = (
        "[00:01:02] Priya: We are removing /legacy.\n"
        "Sam (00:02:00): Fine by me.\n"
        "just a loose line\n"
    )
    u = mn.parse_text(text)
    assert (u[0].speaker, u[0].time) == ("Priya", "00:01:02")
    assert (u[1].speaker, u[1].time) == ("Sam", "00:02:00")
    assert u[2].speaker == ""


def _docx(path, paragraphs):
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    xml = f'<?xml version="1.0"?><w:document xmlns:w="x"><w:body>{body}</w:body></w:document>'
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", xml)


def test_docx_transcript(tmp_path):
    f = tmp_path / "t.docx"
    _docx(
        f,
        [
            "0:05 Priya Shah",
            "We are removing the /legacy endpoint &amp; its docs.",
            "Sam Lee 0:40",
            "OK.",
        ],
    )
    u = mn.parse_transcript(f)
    assert u[0].speaker == "Priya Shah" and u[0].time == "00:00:05"
    assert "endpoint & its docs" in u[0].text
    assert u[1].speaker == "Sam Lee"


def test_bad_docx_is_a_clear_error(tmp_path):
    f = tmp_path / "t.docx"
    f.write_bytes(b"not a zip")
    with pytest.raises(ValueError):
        mn.run(f, tmp_path)


# ------------------------------------------------------------------ statements


def _find(sentence, speaker="Ann"):
    return mn.extract_statements([mn.Utterance(speaker, "00:00:01", sentence, 1)])


@pytest.mark.parametrize(
    "sentence,kind,old,new",
    [
        (
            "We are renaming `--format` to `--output-format`.",
            "rename",
            "--format",
            "--output-format",
        ),
        (
            "SHOP_DB_URL is getting renamed to SHOP_DATABASE_URL.",
            "rename",
            "SHOP_DB_URL",
            "SHOP_DATABASE_URL",
        ),
        ("We're moving the /orders endpoint to /purchases.", "rename", "/orders", "/purchases"),
        ("API_KEY is now API_TOKEN.", "rename", "API_KEY", "API_TOKEN"),
        ("The /invoices endpoint was retired.", "remove", "/invoices", ""),
        ("We deprecated `old_helper()` last month.", "remove", "old_helper", ""),
        ("Remove src/legacy.py before release.", "remove", "src/legacy.py", ""),
    ],
)
def test_statement_patterns(sentence, kind, old, new):
    (s,) = _find(sentence)
    assert (s.kind, s.old, s.new) == (kind, old, new)


def test_value_change_and_action_items():
    (s,) = _find("We are changing the default limit from 20 to 50.")
    assert (s.kind, s.subject, s.old, s.new) == ("change", "limit", "20", "50")
    (a,) = _find("I'll update the setup docs after the merge.", speaker="Priya")
    assert a.kind == "action" and a.owner == "Priya"
    (b,) = _find("Dana will fix the README.")
    assert b.owner == "Dana"


@pytest.mark.parametrize(
    "sentence",
    [
        "Lunch is on me if the demo goes well.",
        "Let's rename the team channel to something fun.",  # nothing code-like to check
        "We renamed API_KEY to the secret thing, whatever.",  # new side is not an identifier
        "The weather is renamed.",
        "Replace `--flag` with `SOME_ENV`.",  # kinds differ: not a rename of one thing
    ],
)
def test_small_talk_is_ignored(sentence):
    assert [s for s in _find(sentence) if s.kind != "action"] == []


# ------------------------------------------------------------------ verdicts


def corpus(code=None, docs=None, routes=None):
    return mn.Corpus(code or {}, docs or {}, routes or set())


def verdict(sentence, c):
    (s,) = _find(sentence)
    f = mn.verify_statement(s, c, "meeting.vtt")
    return f.verdict, f.pending, f


def test_rename_implemented_and_docs_stale():
    c = corpus({"a.py": "x = environ['NEW_NAME']"}, {"d.md": "Set `OLD_NAME`.\n"})
    v, pending, f = verdict("We renamed OLD_NAME to NEW_NAME.", c)
    assert (v, pending) == ("contradicted", False)
    assert (f.file, f.line) == ("d.md", 1) and "Replace `OLD_NAME`" in f.fix


def test_rename_implemented_and_docs_current():
    c = corpus({"a.py": "environ['NEW_NAME']"}, {"d.md": "Set `NEW_NAME`.\n"})
    assert verdict("We renamed OLD_NAME to NEW_NAME.", c)[0] == "verified"


def test_rename_pending_and_half_done_and_unknown():
    c = corpus({"a.py": "environ['OLD_NAME']"}, {"d.md": "Set `OLD_NAME`.\n"})
    v, pending, f = verdict("We will rename OLD_NAME to NEW_NAME.", c)
    assert (v, pending) == ("uncertain", True) and "d.md:1" in f.message
    both = corpus({"a.py": "OLD_NAME\nNEW_NAME"})
    assert verdict("We renamed OLD_NAME to NEW_NAME.", both)[:2] == ("uncertain", True)
    none = corpus({"a.py": "other"})
    assert verdict("We renamed OLD_NAME to NEW_NAME.", none)[:2] == ("uncertain", False)


def test_removal_verdicts_and_history_words_are_not_stale():
    gone_stale = corpus({"a.py": "x"}, {"d.md": "Call `/legacy` for data.\n"})
    assert verdict("The /legacy endpoint was removed.", gone_stale)[0] == "contradicted"
    gone_ok = corpus({"a.py": "x"}, {"d.md": "The `/legacy` endpoint was removed in v2.\n"})
    assert verdict("The /legacy endpoint was removed.", gone_ok)[0] == "verified"
    still = corpus({"a.py": "@app.get('/legacy')"}, {"d.md": "Call `/legacy`.\n"})
    assert verdict("We will retire the /legacy endpoint.", still)[:2] == ("uncertain", True)


def test_routes_come_from_the_api_definition():
    c = corpus({}, {"d.md": "POST /orders creates one.\n"}, routes={"/orders"})
    v, pending, _ = verdict("We are moving /orders to /purchases.", c)
    assert (v, pending) == ("uncertain", True)


def test_value_change_verdicts():
    docs = {"d.md": "The default limit is 20.\n"}
    shipped = corpus({"a.py": "limit: int = 50"}, docs)
    assert verdict("We changed the default limit from 20 to 50.", shipped)[0] == "contradicted"
    waiting = corpus({"a.py": "limit: int = 20"}, docs)
    assert verdict("We are changing the default limit from 20 to 50.", waiting)[:2] == (
        "uncertain",
        True,
    )
    unrelated = corpus({"a.py": "x = 1"}, docs)
    assert verdict("We changed the default limit from 20 to 50.", unrelated)[:2] == (
        "uncertain",
        False,
    )


def test_number_matching_does_not_hit_longer_numbers():
    c = corpus({"a.py": "limit = 200"}, {})
    assert c.find_value_in_code("limit", "20") is None


# ------------------------------------------------------------------ AI guard rails


class FakeClient:
    def __init__(self, reply):
        self.reply = reply
        self.sent = None
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, model, messages):
        self.sent = messages[-1]["content"]
        msg = SimpleNamespace(content=self.reply)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


def test_ai_statements_are_validated_and_redacted():
    u = [
        mn.Utterance(
            "Priya Shah",
            "00:00:01",
            "Email me at p@corp.com. We should call it PAY_URL instead of PAY_DB.",
            3,
        ),
    ]
    reply = json.dumps(
        [
            {"kind": "rename", "old": "PAY_DB", "new": "PAY_URL", "quote": "", "index": 0},
            {"kind": "rename", "old": "MADE_UP", "new": "PAY_URL", "index": 0},  # not in text
            {"kind": "rename", "old": "PAY_DB", "new": "--flag", "index": 0},  # kinds differ
            {"kind": "remove", "old": "PAY_DB", "index": 9},  # bad index
            {"kind": "explode", "old": "PAY_DB", "index": 0},
        ]
    )
    client = FakeClient("```json\n" + reply + "\n```")
    found, err = mn.extract_ai(u, client, "m")
    assert err == "" and [(s.kind, s.old, s.new, s.source) for s in found] == [
        ("rename", "PAY_DB", "PAY_URL", "ai")
    ]
    assert "Priya" not in client.sent and "p@corp.com" not in client.sent
    assert "Speaker 1" in client.sent and "[email]" in client.sent


def test_ai_failure_never_breaks_the_report():
    class Boom:
        chat = SimpleNamespace(completions=SimpleNamespace(create=lambda **k: 1 / 0))

    report = mn.verify_meeting(
        [mn.Utterance("A", "", "We renamed OLD_X to NEW_X.", 1)],
        corpus({"a.py": "NEW_X"}),
        client=Boom(),
        model="m",
    )
    assert len(report.findings) == 1 and report.ai_used is False
    notjson, err = mn.extract_ai([mn.Utterance("A", "", "x", 1)], FakeClient("not json"), "m")
    assert notjson == [] and err


def test_ai_adds_decisions_the_rules_missed_but_code_decides_the_verdict():
    u = [
        mn.Utterance("A", "", "Honestly PAY_DB should just go by the name PAY_URL from now on.", 1)
    ]
    reply = json.dumps([{"kind": "rename", "old": "PAY_DB", "new": "PAY_URL", "index": 0}])
    report = mn.verify_meeting(u, corpus({"a.py": "PAY_DB"}), client=FakeClient(reply), model="m")
    (f,) = report.findings
    assert f.source == "ai" and f.verdict == "uncertain" and f.pending
    assert report.ai_used


# ------------------------------------------------------------------ demo + CLI


def test_demo_meeting_findings():
    report = mn.run(VTT, DEMO, [DEMO / "docs"], code_path=str(DEMO))
    by_subject = {f.subject: f for f in report.findings}
    assert by_subject["`--format` → `--output-format`"].verdict == "contradicted"
    assert by_subject["`/invoices`"].verdict == "contradicted"
    assert by_subject["`shop/config.py`"].verdict == "verified"
    for s in ("`SHOP_DB_URL` → `SHOP_DATABASE_URL`", "`/orders` → `/purchases`", "limit: 20 → 50"):
        assert by_subject[s].verdict == "uncertain" and by_subject[s].pending
    assert [a.owner for a in report.actions] == ["Priya Shah"]


def test_demo_meeting_expected_output_is_current():
    report = mn.run(VTT, DEMO, [DEMO / "docs"], code_path=str(DEMO))
    committed = (DEMO / "meetings" / "EXPECTED_OUTPUT.md").read_text(encoding="utf-8")
    assert committed == mn.render_meeting_markdown(report) + "\n"


def test_quiet_meeting_renders_a_helpful_message(tmp_path):
    f = tmp_path / "n.txt"
    f.write_text("Ann: Nice weather today.\n")
    text = mn.render_meeting_markdown(mn.run(f, tmp_path))
    assert "Nothing in these notes" in text


def test_cli_exit_codes(tmp_path, capsys):
    assert mn.main([str(VTT), "--root", str(DEMO), str(DEMO / "docs")]) == 1
    quiet = tmp_path / "n.txt"
    quiet.write_text("Ann: Nice weather today.\n")
    assert mn.main([str(quiet), "--root", str(tmp_path)]) == 0
    assert mn.main([str(tmp_path / "missing.vtt"), "--root", str(tmp_path)]) == 2
    capsys.readouterr()
    assert mn.main([str(VTT), "--root", str(DEMO), "--json", str(DEMO / "docs")]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["counts"]["contradicted"] == 2


def test_docaudit_includes_meeting_and_gates_on_it(tmp_path):
    result = docaudit.audit(DEMO, [DEMO / "docs"], code_path=str(DEMO), meeting_path=VTT)
    assert result.meeting and result.has_contradictions
    text = docaudit.render_audit_markdown(result)
    assert "Meeting follow-ups" in text
    data = result.to_dict()
    assert data["meeting"]["counts"]["contradicted"] == 2
    # the meeting's stale docs can be baselined like any other finding
    from baseline import write_baseline

    base = tmp_path / "b.json"
    write_baseline(base, result.gate_findings)
    again = docaudit.audit(
        DEMO, [DEMO / "docs"], code_path=str(DEMO), meeting_path=VTT, baseline_path=base
    )
    assert not again.has_contradictions


def test_docaudit_main_with_meeting_flag(capsys):
    code = docaudit.main(
        ["--root", str(DEMO), "--code", str(DEMO), "--meeting", str(VTT), str(DEMO / "docs")]
    )
    assert code == 1
    assert "Meeting follow-ups" in capsys.readouterr().out
