"""Baseline, ignore rules, PR-scoped audits and SARIF output."""

import json
import subprocess
from pathlib import Path

import pytest

import docaudit
from api_contract import CONTRADICTED
from baseline import fingerprint, is_ignored, load_baseline, load_ignore_rules, write_baseline
from changed_files import changed_files, docs_to_check
from sarif import to_sarif

DEMO = Path(__file__).resolve().parent.parent / "examples" / "shop-demo"


def _audit_demo(**kw):
    return docaudit.audit(DEMO, [DEMO / "docs"], code_path=str(DEMO), **kw)


def test_baseline_makes_known_problems_non_failing(tmp_path):
    result = _audit_demo()
    assert result.has_contradictions
    path = tmp_path / "base.json"
    assert write_baseline(path, result.findings) == 20
    again = _audit_demo(baseline_path=path)
    assert not again.has_contradictions
    assert len(again.baselined) == 20
    assert "known problem(s) are in the baseline" in docaudit.render_audit_markdown(again)


def test_fingerprint_ignores_line_numbers():
    f = next(f for f in _audit_demo().findings if f.verdict == CONTRADICTED)
    before = fingerprint(f)
    if hasattr(f, "claim"):
        f.claim.line += 40
    else:
        f.line += 40
    assert fingerprint(f) == before


def test_new_problem_still_fails_with_baseline(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (tmp_path / "app.py").write_text("import os\nos.environ.get('A_TOKEN')\n")
    (docs / "a.md").write_text("Set the environment variable `A_TOKEN`.\n")
    clean = docaudit.audit(tmp_path, [docs])
    assert not clean.has_contradictions
    base = tmp_path / "b.json"
    write_baseline(base, clean.findings)
    (docs / "a.md").write_text(
        "Set the environment variable `A_TOKEN`.\nSet the environment variable `B_SECRET`.\n"
    )
    now = docaudit.audit(tmp_path, [docs], baseline_path=base)
    assert [f.subject for f in now.new_contradictions] == ["`B_SECRET`"]


def test_ignore_rules_by_glob_and_subject(tmp_path):
    rules_file = tmp_path / ".docaudit.json"
    rules_file.write_text(json.dumps({"ignore": [{"file": "docs/setup.md", "check": "env"}]}))
    rules = load_ignore_rules(rules_file)
    result = _audit_demo(ignore_path=rules_file)
    assert result.ignored >= 1
    assert not any(f.check == "env" and f.file == "docs/setup.md" for f in result.project.findings)
    sample = next(f for f in _audit_demo().project.findings if f.check == "env")
    assert is_ignored(sample, [{"check": "env"}]) and not is_ignored(sample, [{"check": "flag"}])
    assert rules == [{"file": "docs/setup.md", "check": "env"}]


def test_bad_ignore_and_baseline_files_raise(tmp_path):
    bad = tmp_path / "x.json"
    bad.write_text("{not json")
    with pytest.raises(ValueError):
        load_baseline(bad)
    bad.write_text(json.dumps({"ignore": [{"nonsense": 1}]}))
    with pytest.raises(ValueError):
        load_ignore_rules(bad)
    assert load_baseline(tmp_path / "missing.json") == set()


def test_sarif_lists_problems_only():
    result = _audit_demo()
    sarif = to_sarif(result.findings)
    results = sarif["runs"][0]["results"]
    assert sarif["version"] == "2.1.0"
    assert len(results) == len([f for f in result.findings if f.verdict != "verified"])
    assert {r["level"] for r in results} <= {"error", "note"}
    first = results[0]["locations"][0]["physicalLocation"]
    assert first["region"]["startLine"] >= 1 and first["artifactLocation"]["uri"]
    assert all("partialFingerprints" in r for r in results)


# ------------------------------------------------------------------ PR scope (real git repo)


def _git(root, *args):
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=root,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "config.py").write_text("import os\nos.environ.get('API_TOKEN')\n")
    (tmp_path / "docs" / "setup.md").write_text(
        "Set the environment variable `API_TOKEN` first. See `src/config.py`.\n"
    )
    (tmp_path / "docs" / "other.md").write_text("Nothing about code here.\n")
    (tmp_path / "docs" / "unrelated.md").write_text("Set the environment variable `GHOST_VAR`.\n")
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    _git(tmp_path, "checkout", "-q", "-b", "feature")
    return tmp_path


def test_changed_files_and_affected_docs(repo):
    (repo / "src" / "config.py").write_text("import os\nos.environ.get('API_KEY')\n")
    (repo / "docs" / "other.md").write_text("Nothing about code here, edited.\n")
    _git(repo, "commit", "-aq", "-m", "change")
    changed = changed_files(repo, "main")
    assert sorted(changed) == ["docs/other.md", "src/config.py"]
    picked = {p.name for p in docs_to_check(repo, changed)}
    assert picked == {"other.md", "setup.md"}  # unrelated.md is left alone


def test_scoped_audit_checks_only_affected_pages(repo):
    (repo / "src" / "config.py").write_text("import os\nos.environ.get('API_KEY')\n")
    _git(repo, "commit", "-aq", "-m", "rename env var")
    result = docaudit.audit(repo, changed_since="main")
    assert result.scope == ["docs/setup.md"]
    assert [f.subject for f in result.findings if f.verdict == CONTRADICTED] == ["`API_TOKEN`"]
    # the full audit would also have flagged the unrelated page
    full = docaudit.audit(repo)
    assert any(f.subject == "`GHOST_VAR`" for f in full.findings)


def test_change_with_no_docs_is_not_a_failure(repo, capsys):
    (repo / "src" / "other.py").write_text("x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "unrelated code")
    assert (
        docaudit.main(["--root", str(repo), "--changed-since", "main", "--fail-under", "80"]) == 0
    )
    assert "nothing to check" in capsys.readouterr().out


def test_cli_update_baseline_then_gate(repo, capsys):
    (repo / "docs" / "setup.md").write_text(
        "Set the environment variable `API_TOKEN`.\nSet the environment variable `OLD_VAR`.\n"
    )
    root = str(repo)
    assert docaudit.main(["--root", root]) == 1
    assert docaudit.main(["--root", root, "--update-baseline"]) == 0
    assert (repo / ".docaudit-baseline.json").is_file()
    assert docaudit.main(["--root", root]) == 0  # known problem no longer fails
    sarif = repo / "out.sarif"
    docaudit.main(["--root", root, "--sarif", str(sarif)])
    assert json.loads(sarif.read_text())["version"] == "2.1.0"
    (repo / "docs" / "setup.md").write_text(
        "Set the environment variable `API_TOKEN`.\nSet the environment variable `OLD_VAR`.\nSet the environment variable `NEW_VAR`.\n"
    )
    assert docaudit.main(["--root", root]) == 1  # a new problem does
    capsys.readouterr()


def test_bad_git_ref_exits_2(repo, capsys):
    assert docaudit.main(["--root", str(repo), "--changed-since", "nope"]) == 2
    assert "git" in capsys.readouterr().err


def test_run_from_env_uses_base_ref_baseline_and_ignore(repo, monkeypatch):
    (repo / "src" / "config.py").write_text("import os\nos.environ.get('API_KEY')\n")
    _git(repo, "commit", "-aq", "-m", "rename env var")
    monkeypatch.chdir(repo)
    monkeypatch.setenv("DOCS_SUBFOLDER", "docs")
    monkeypatch.setenv("DOCAUDIT_BASE_REF", "main")
    result = docaudit.run_from_env(post=False)
    assert result.scope == ["docs/setup.md"] and result.has_contradictions
    write_baseline(repo / ".docaudit-baseline.json", result.gate_findings)
    again = docaudit.run_from_env(post=False)
    assert not again.has_contradictions and again.baselined
    # a base ref that does not exist falls back to a full audit instead of failing
    monkeypatch.setenv("DOCAUDIT_BASE_REF", "no-such-ref")
    fallback = docaudit.run_from_env(post=False)
    assert fallback.scope is None
