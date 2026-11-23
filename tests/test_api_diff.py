"""Breaking-change detection between git refs, and deprecation tracking in docs."""

import json
import subprocess

import pytest

import api_diff
import docaudit
from api_contract import Spec, verify_docs

V1 = """
from fastapi import FastAPI

app = FastAPI()


@app.get("/users")
def list_users(limit: int = 20):
    return []


@app.get("/orders")
def list_orders():
    return []


@app.get("/legacy")
def legacy():
    return {}


@app.post("/items", status_code=201)
def make_item(name: str):
    return {}
"""

V2 = """
from fastapi import FastAPI

app = FastAPI()


@app.get("/users")
def list_users(tenant: str, limit: int = 20):
    return []


@app.get("/purchases")
def list_purchases():
    return []


@app.get("/legacy", deprecated=True)
def legacy():
    return {}


@app.post("/items", status_code=202)
def make_item(name: int):
    return {}
"""

DOC = """# API

### GET /orders

List orders.

### GET /legacy

Old data.

### POST /items

Create an item.
"""


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
    (tmp_path / "docs" / "api.md").write_text(DOC)
    (tmp_path / "app.py").write_text(V1)
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "v1")
    _git(tmp_path, "tag", "v1")
    (tmp_path / "app.py").write_text(V2)
    _git(tmp_path, "commit", "-aq", "-m", "v2")
    return tmp_path


def kinds(report):
    return {(c.severity, c.kind, c.detail) for c in report.changes}


def test_changes_between_two_refs(repo):
    report = api_diff.compare(repo, "v1", "HEAD", [repo / "docs"], code_path=".")
    got = kinds(report)
    assert ("breaking", "removed", "") in got  # /orders is gone
    assert ("breaking", "param-required", "tenant") in got
    assert ("breaking", "param-type", "name") in got
    assert ("breaking", "response-removed", "201") in got
    assert ("warning", "deprecated", "") in got
    assert ("info", "added", "") in got
    removed = next(c for c in report.changes if c.kind == "removed")
    assert "may have been renamed to `GET /purchases`" in removed.message


def test_documented_endpoints_are_called_out(repo):
    report = api_diff.compare(repo, "v1", "HEAD", [repo / "docs"], code_path=".")
    places = {(i.file, i.line, i.change.kind) for i in report.impacts}
    assert ("docs/api.md", 3, "removed") in places  # GET /orders is documented
    assert ("docs/api.md", 11, "response-removed") in places
    text = api_diff.render_diff_markdown(report)
    assert "Documented at `docs/api.md:3`" in text
    assert "Not described in the docs you checked." in text  # the /users change


def test_working_tree_is_compared_when_no_new_ref(repo):
    (repo / "app.py").write_text(V1)  # revert in the working tree only
    report = api_diff.compare(repo, "HEAD", None, code_path=".")
    assert report.new_label == "working tree"
    assert any(c.kind == "added" and c.path == "/orders" for c in report.changes)


def test_no_changes_message(repo):
    report = api_diff.compare(repo, "HEAD", "HEAD", code_path=".")
    assert "No API changes" in api_diff.render_diff_markdown(report)


def test_cli_exit_codes(repo, capsys):
    base = ["v1", "HEAD", "--root", str(repo), "--code", "."]
    assert api_diff.main(base) == 0
    assert api_diff.main([*base, "--fail-on-breaking"]) == 1
    assert api_diff.main(["nope", "HEAD", "--root", str(repo), "--code", "."]) == 2
    assert "git archive" in capsys.readouterr().err
    assert api_diff.main([*base, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["counts"]["breaking"] >= 4


def test_archive_extraction_refuses_path_escape(tmp_path):
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo("../evil.txt")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(api_diff.RefError):
        api_diff._safe_extract(buf.getvalue(), tmp_path / "out")


def test_openapi_file_specs_are_compared_too(tmp_path):
    def spec(extra):
        return json.dumps(
            {
                "openapi": "3.0.0",
                "info": {},
                "paths": {"/a": {"get": {"responses": {"200": {}}}}, **extra},
            }
        )

    (tmp_path / "openapi.json").write_text(spec({}))
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "a")
    (tmp_path / "openapi.json").write_text(spec({"/b": {"get": {"responses": {"200": {}}}}}))
    report = api_diff.compare(tmp_path, "HEAD", None, spec_path="openapi.json")
    assert [(c.kind, c.path) for c in report.changes] == [("added", "/b")]


# ------------------------------------------------------------------ deprecation tracking


def _spec(deprecated):
    return Spec(
        {
            "openapi": "3.0.0",
            "paths": {"/old": {"get": {"deprecated": deprecated, "responses": {"200": {}}}}},
        }
    )


def test_deprecated_endpoint_documented_as_current(tmp_path):
    (tmp_path / "a.md").write_text("### GET /old\n\nReturns data.\n\n### GET /other\n")
    report = verify_docs(_spec(True), [tmp_path], tmp_path)
    note = [f for f in report.findings if f.check == "deprecation"]
    assert len(note) == 1 and note[0].verdict == "uncertain"
    assert "marked deprecated" in note[0].message and note[0].claim.line == 1


def test_deprecation_mentioned_or_not_deprecated_is_fine(tmp_path):
    (tmp_path / "a.md").write_text("### GET /old\n\nDeprecated: use /new.\n")
    assert not [
        f
        for f in verify_docs(_spec(True), [tmp_path], tmp_path).findings
        if f.check == "deprecation"
    ]
    assert not [
        f
        for f in verify_docs(_spec(False), [tmp_path], tmp_path).findings
        if f.check == "deprecation"
    ]


def test_deprecation_mention_in_the_next_section_does_not_count(tmp_path):
    (tmp_path / "a.md").write_text(
        "### GET /old\n\nReturns data.\n\n### GET /x\n\nDeprecated soon.\n"
    )
    report = verify_docs(_spec(True), [tmp_path], tmp_path)
    assert [f for f in report.findings if f.check == "deprecation"]


def test_fastapi_deprecated_flag_is_read_from_code(tmp_path):
    (tmp_path / "app.py").write_text(V2)
    from api_contract import load_spec_from_code

    spec = load_spec_from_code(tmp_path)
    assert spec.operations[("GET", "/legacy")].deprecated is True
    assert spec.operations[("GET", "/users")].deprecated is False


def test_docaudit_compare_to_flag(repo, capsys):
    code = docaudit.main(
        ["--root", str(repo), "--code", ".", "--compare-to", "v1", "--fail-on-breaking", "docs"]
    )
    assert code == 1
    assert "API changes" in capsys.readouterr().out
