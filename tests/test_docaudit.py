"""Tests for the combined documentation audit (src/docaudit.py)."""

import json
import textwrap

import docaudit
from api_contract import CONTRADICTED, VERIFIED
from tests.api_fixtures import SPEC


def make_repo(tmp_path, with_spec=True):
    (tmp_path / "pyproject.toml").write_text('[project]\nname="x"\nrequires-python=">=3.11"\n')
    (tmp_path / "app.py").write_text("import os\nKEY = os.environ['APP_KEY']\n")
    if with_spec:
        (tmp_path / "openapi.json").write_text(json.dumps(SPEC))
    (tmp_path / "README.md").write_text(
        textwrap.dedent(
            """\
            # App

            Requires Python 3.9+. Set the environment variable `APP_KEY`.
            See `missing.py`.

            ### GET /nope

            Nothing here.
            """
        )
    )
    return tmp_path


def test_audit_combines_api_and_project_findings(tmp_path):
    root = make_repo(tmp_path)
    result = docaudit.audit(root, [root / "README.md"])
    assert result.api is not None and result.project is not None
    checks = {getattr(f, "check", None) for f in result.findings}
    assert {"env", "version", "reference"} <= checks
    assert any(f.claim.path == "/nope" for f in result.api.findings)
    assert result.count(VERIFIED) >= 1 and result.count(CONTRADICTED) >= 3
    assert result.has_contradictions
    md = docaudit.render_audit_markdown(result)
    assert md.startswith("# 📚 Documentation audit")
    assert "Documentation health" in md and "Project facts check" in md and "API Contract" in md


def test_audit_without_a_spec_still_runs_project_checks(tmp_path):
    root = make_repo(tmp_path, with_spec=False)
    result = docaudit.audit(root, [root / "README.md"])
    assert result.api is None and result.project is not None
    assert any("API checks skipped" in n for n in result.notes)
    assert "ℹ️ API checks skipped" in docaudit.render_audit_markdown(result)
    only = docaudit.audit(root, [root / "README.md"], run_api=False)
    assert only.notes == []


def test_cli_exit_codes_json_and_history(tmp_path, capsys):
    root = make_repo(tmp_path)
    hist = tmp_path / "h" / "history.json"
    args = ["--root", str(root), str(root / "README.md"), "--history", str(hist)]
    assert docaudit.main(args) == 1  # contradictions found
    assert docaudit.main([*args, "--fail-under", "0"]) == 0
    assert docaudit.main([*args, "--fail-under", "99"]) == 1
    assert len(json.loads(hist.read_text())) == 3
    capsys.readouterr()
    docaudit.main([*args, "--json", "--no-api"])
    data = json.loads(capsys.readouterr().out)
    assert data["api"] is None and data["project"]["counts"]["contradicted"] >= 2


def test_run_from_env_uses_docs_subfolder(tmp_path, monkeypatch, capsys):
    root = make_repo(tmp_path)
    monkeypatch.chdir(root)
    monkeypatch.setenv("DOCS_SUBFOLDER", ".")
    monkeypatch.setenv("API_SPEC_PATH", "openapi.json")
    result = docaudit.run_from_env(post=False)
    assert result is not None and result.has_contradictions
    assert "Documentation audit" in capsys.readouterr().out


def test_explicit_but_missing_spec_is_an_error_not_a_pass(tmp_path, capsys):
    root = make_repo(tmp_path, with_spec=False)
    args = ["--root", str(root), str(root / "README.md")]
    assert docaudit.main([*args, "--spec", str(root / "nope.json"), "--fail-under", "50"]) == 2
    assert "Cannot read spec" in capsys.readouterr().err
    assert docaudit.main([*args, "--code", str(root / "empty-dir"), "--fail-under", "50"]) == 2
    # without an explicit request the missing spec is only a note
    assert docaudit.main([*args, "--fail-under", "0"]) == 0


def test_empty_run_fails_the_gate(tmp_path, capsys):
    (tmp_path / "README.md").write_text("Nothing checkable here.\n")
    args = ["--root", str(tmp_path), str(tmp_path / "README.md"), "--no-api"]
    assert docaudit.main([*args, "--fail-under", "80"]) == 1
    assert "Nothing was checked" in capsys.readouterr().err
    assert docaudit.main(args) == 0  # no gate requested: just a warning


def test_run_from_env_reports_unreadable_configured_spec(tmp_path, monkeypatch, capsys):
    root = make_repo(tmp_path, with_spec=False)
    monkeypatch.chdir(root)
    monkeypatch.setenv("DOCS_SUBFOLDER", ".")
    monkeypatch.setenv("API_SPEC_PATH", "missing.json")
    assert docaudit.run_from_env(post=False) is None
    assert "Could not read the API definition" in capsys.readouterr().out


def test_separate_docs_repo_uses_the_code_repo_as_root(tmp_path, monkeypatch, capsys):
    """[verify-docs] with docs in another repo must check against the code, not the docs repo."""
    code = tmp_path / "work"
    code.mkdir()
    make_repo(code)
    (code / "README.md").unlink()
    docs_repo = code / "docs_repo"  # the action clones it inside the workspace
    docs_repo.mkdir()
    (docs_repo / "guide.md").write_text(
        "Set the environment variable `APP_KEY` and `APP_GONE`.\n"
        "See [other](other.md) and `app.py` and `missing.py`.\n\n### GET /nope\n\nx\n"
    )
    (docs_repo / "other.md").write_text("# other\n")
    monkeypatch.chdir(code)
    monkeypatch.delenv("DOCS_SUBFOLDER", raising=False)
    monkeypatch.delenv("API_SPEC_PATH", raising=False)
    import github_ops

    def fake_setup():
        import os

        os.chdir(docs_repo)
        return True

    monkeypatch.setattr(github_ops, "setup_docs_environment", fake_setup)
    result = docaudit.run_from_env(post=False)
    assert result is not None
    assert result.api is not None  # openapi.json was found in the code repo
    by_msg = {f.message: f for f in result.findings}
    assert any("`APP_KEY` is read" in m for m in by_msg)  # env read from the code repo
    assert any("`APP_GONE`" in m and f.verdict == CONTRADICTED for m, f in by_msg.items())
    assert any("linked file `other.md` exists" in m for m in by_msg)  # link inside the docs repo
    assert any("file `app.py` exists" in m for m in by_msg)  # file in the code repo
    assert any("`missing.py` does not exist" in m for m in by_msg)
    assert all(f.claim.file == "guide.md" for f in result.api.findings)  # paths relative to docs
    assert not any("docs_repo" in str(getattr(f, "file", "")) for f in result.findings)
    import os

    assert os.getcwd() == str(code)
