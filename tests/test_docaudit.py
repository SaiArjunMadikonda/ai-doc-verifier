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
