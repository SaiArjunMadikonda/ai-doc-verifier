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
    assert write_baseline(path, result.findings) == 19
    again = _audit_demo(baseline_path=path)
    assert not again.has_contradictions
    assert len(again.baselined) == 19
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


def test_endpoint_rename_scopes_unchanged_docs_and_curl_example(repo):
    api = repo / "src" / "routes.py"
    api.write_text(
        'from fastapi import FastAPI\napp = FastAPI()\n@app.get("/users")\ndef users(): pass\n@app.get("/health")\ndef health(): pass\n'
    )
    (repo / "docs/users.md").write_text("### GET /users\n")
    (repo / "docs/example.md").write_text("```sh\ncurl http://localhost/users\n```\n")
    (repo / "docs/health.md").write_text("### GET /health\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "document endpoints")
    api.write_text(api.read_text().replace('"/users"', '"/customers"'))
    _git(repo, "commit", "-qam", "rename users endpoint")
    result = docaudit.audit(repo, [repo / "docs"], changed_since="HEAD^")
    assert result.scope == ["docs/example.md", "docs/users.md"]
    assert {f.claim.file for f in result.new_contradictions} == set(result.scope)
    assert docaudit.main(["--root", str(repo), "--changed-since", "HEAD^", str(repo / "docs")]) == 1


def test_deleted_and_renamed_files_select_docs_for_old_paths(repo):
    (repo / "src/config.py").rename(repo / "src/settings.py")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "move config")
    assert docaudit.audit(repo, changed_since="HEAD^").scope == ["docs/setup.md"]


def test_threshold_excludes_baselined_findings_but_reports_absolute_health(tmp_path, capsys):
    base = tmp_path / "baseline.json"
    args = ["--root", str(DEMO), str(DEMO / "docs"), "--baseline", str(base)]
    assert docaudit.main([*args, "--update-baseline"]) == 0
    capsys.readouterr()
    assert docaudit.main([*args, "--json", "--fail-under", "80"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["health"]["score"] < 80 <= result["threshold_health"]["score"]


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
    assert docaudit.main(["--root", root, "--fail-under", "80"]) == 1
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


def test_only_baselined_checks_pass_threshold(repo):
    page = repo / "docs/unrelated.md"
    args = ["--root", str(repo), str(page)]
    assert docaudit.main([*args, "--update-baseline"]) == 0
    assert docaudit.main([*args, "--fail-under", "100"]) == 0
    page.write_text("Plain prose.\n")
    assert docaudit.main([*args, "--fail-under", "100"]) == 1


def test_action_verification_gates_new_contradictions(repo, monkeypatch):
    import sys

    import suggest_docs

    monkeypatch.chdir(repo)
    monkeypatch.setenv("COMMENT_BODY", "[verify-docs]")
    monkeypatch.setenv("DOCS_SUBFOLDER", "docs")
    monkeypatch.setattr(sys, "argv", ["suggest_docs.py", "--dry-run"])
    with pytest.raises(SystemExit) as failed:
        suggest_docs.main()
    assert failed.value.code == 1
    assert docaudit.main(["--update-baseline"]) == 0
    suggest_docs.main()


def test_removed_api_definition_cannot_silently_skip_its_docs(repo):
    spec = repo / "openapi.json"
    spec.write_text(json.dumps({"openapi": "3.0.0", "paths": {"/users": {"get": {}}}}))
    (repo / "docs/users.md").write_text("### GET /users\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "publish API and guide")
    spec.unlink()
    _git(repo, "commit", "-qam", "remove API definition")
    assert docaudit.main(["--root", str(repo), "--changed-since", "HEAD^"]) == 2
