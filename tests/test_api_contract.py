"""Tests for the API Contract Verifier (src/api_contract.py)."""

import json
import sys
import textwrap
import types

import pytest

import api_contract as ac
from api_contract import (
    CONTRADICTED,
    UNCERTAIN,
    VERIFIED,
    Spec,
    SpecError,
    extract_claims,
    normalize_path,
    verify,
)
from tests.api_fixtures import SPEC


@pytest.fixture
def spec():
    return Spec(SPEC, source="openapi.json")


def run(spec, md):
    return verify(extract_claims(textwrap.dedent(md), "docs/api.md"), spec)


def verdicts(report, check=None):
    return [(f.verdict, f.check) for f in report.findings if check in (None, f.check)]


def one(report, check, text):
    matches = [f for f in report.findings if f.check == check and text in f.message]
    assert len(matches) == 1, [f.message for f in report.findings]
    return matches[0]


# --------------------------------------------------------------------------- paths


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("/users/{id}", "/users/{}"),
        ("/users/:id", "/users/{}"),
        ("/users/<int:id>", "/users/{}"),
        ("/users/", "/users"),
        ("/users?limit=5", "/users"),
        ("/", "/"),
        ("users", "/users"),
    ],
)
def test_normalize_path(raw, expected):
    assert normalize_path(raw) == expected


# --------------------------------------------------------------------------- spec


def test_spec_requires_paths():
    with pytest.raises(SpecError):
        Spec({"openapi": "3.0.0"})


def test_spec_operations_and_body_fields(spec):
    op = spec.operations[("POST", "/users")]
    body = {p.name: p for p in op.params if p.location == "body"}
    assert set(body) == {"id", "email", "age"}  # allOf merged
    assert body["email"].required and not body["age"].required
    assert body["age"].types == frozenset({"integer"})  # null dropped


def test_path_level_parameters_are_merged(spec):
    op = spec.operations[("GET", "/users/{}")]
    assert [(p.name, p.location, p.required) for p in op.params] == [("user_id", "path", True)]


def test_external_ref_marks_operation_unresolved(spec):
    assert spec.operations[("GET", "/ext")].unresolved


def test_load_spec_json_and_yaml(tmp_path):
    (tmp_path / "openapi.json").write_text(json.dumps(SPEC))
    assert ac.load_spec(tmp_path / "openapi.json").source == "openapi.json"
    yaml = pytest.importorskip("yaml")
    (tmp_path / "o.yaml").write_text(yaml.safe_dump(SPEC))
    assert ("GET", "/health") in ac.load_spec(tmp_path / "o.yaml").operations


def test_load_spec_errors(tmp_path):
    with pytest.raises(SpecError):
        ac.load_spec(tmp_path / "missing.json")
    (tmp_path / "bad.json").write_text("{nope")
    with pytest.raises(SpecError):
        ac.load_spec(tmp_path / "bad.json")


def test_find_spec_file(tmp_path):
    assert ac.find_spec_file(tmp_path) is None
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "openapi.yaml").write_text("x")
    assert ac.find_spec_file(tmp_path).name == "openapi.yaml"


def test_load_spec_from_fastapi_stub(monkeypatch):
    mod = types.ModuleType("fake_app_module")
    mod.app = types.SimpleNamespace(openapi=lambda: SPEC)
    monkeypatch.setitem(sys.modules, "fake_app_module", mod)
    assert ("GET", "/health") in ac.load_spec_from_fastapi("fake_app_module:app").operations
    with pytest.raises(SpecError):
        ac.load_spec_from_fastapi("no-colon")
    with pytest.raises(SpecError):
        ac.load_spec_from_fastapi("fake_app_module:missing")


# --------------------------------------------------------------------------- extraction


def test_extract_heading_block_table_and_responses():
    claims = extract_claims(
        textwrap.dedent(
            """\
            ### GET /users

            Parameters:

            | Name | Type | Required |
            |---|---|---|
            | `limit` | int | no |
            | q | string | yes |

            Responses:

            | Status | Meaning |
            |---|---|
            | 200 | ok |
            | 404 | none |

            ### POST /users
            """
        )
    )
    assert [c.label for c in claims] == ["GET /users", "POST /users"]
    get = claims[0]
    assert [(p.name, p.type, p.required) for p in get.params] == [
        ("limit", "integer", False),
        ("q", "string", True),
    ]
    assert get.statuses == ["200", "404"]
    assert claims[1].params == []  # block ended at the next endpoint heading


def test_extract_bullets_without_section_label():
    (claim,) = extract_claims(
        "## POST /users\n\n- `email` (string, required): a\n- `age` (integer, optional)\n"
        "- Some other bullet that is plain prose\n\nReturns `201` on success.\n"
    )
    assert [(p.name, p.type, p.required) for p in claim.params] == [
        ("email", "string", True),
        ("age", "integer", False),
    ]
    assert claim.statuses == ["201"]


def test_extract_endpoint_table_both_orders_and_query_params():
    claims = extract_claims(
        "| Method | Path |\n|---|---|\n| GET | /health |\n| /ping | DELETE |\n\n"
        "Call `GET /users?limit=5&q=x` to list.\n"
    )
    assert {c.label for c in claims} == {"GET /health", "DELETE /ping", "GET /users"}
    users = next(c for c in claims if c.path == "/users")
    assert [(p.name, p.location) for p in users.params] == [("limit", "query"), ("q", "query")]


def test_fenced_code_is_ignored():
    text = "```http\nGET /secret HTTP/1.1\n```\n\n~~~\nPOST /also-ignored\n~~~\n"
    assert extract_claims(text) == []


def test_lowercase_prose_is_not_a_claim():
    assert extract_claims("you can get /users by asking nicely") == []


def test_duplicate_mentions_are_merged():
    claims = extract_claims(
        "### GET /users\n\n- `limit` (integer)\n\nLater: GET /users?q=1 also works.\n"
    )
    assert len(claims) == 1
    assert {p.name for p in claims[0].params} == {"limit", "q"}


def test_prose_numbers_are_not_status_codes():
    (claim,) = extract_claims("### GET /x\n\nReturns 250 items per page, or 404 if empty.\n")
    assert claim.statuses == ["404"]


def test_bold_name_is_not_required_star():
    (claim,) = extract_claims("### GET /x\n\n| Name |\n|---|\n| **a** |\n")
    assert claim.params[0].required is None


def test_required_star_and_question_mark_suffixes():
    (claim,) = extract_claims(
        "### GET /x\n\n| Name | Type |\n|---|---|\n| a* | string |\n| b? | string |\n"
    )
    assert [(p.name, p.required) for p in claim.params] == [("a", True), ("b", False)]


def test_optional_column_is_inverted():
    (claim,) = extract_claims(
        "### GET /x\n\n| Name | Optional |\n|---|---|\n| a | yes |\n| b | no |\n"
    )
    assert [(p.name, p.required) for p in claim.params] == [("a", False), ("b", True)]


def test_location_column():
    (claim,) = extract_claims(
        "### GET /x\n\n| Name | In | Type |\n|---|---|---|\n| X-Key | header | string |\n"
    )
    assert claim.params[0].location == "header"


# --------------------------------------------------------------------------- verification


def test_all_good_is_verified(spec):
    report = run(
        spec,
        """\
        ### GET /users

        | Name | Type | Required |
        |---|---|---|
        | limit | integer | no |
        | q | string | yes |

        Returns 200.
        """,
    )
    assert not report.has_contradictions
    assert {v for v, _ in verdicts(report)} == {VERIFIED}
    assert report.count(VERIFIED) == 4  # endpoint, 2 params, 1 response


def test_unknown_path_is_contradicted_with_suggestion(spec):
    report = run(spec, "### GET /user\n")
    f = one(report, "endpoint", "does not define")
    assert f.verdict == CONTRADICTED and "`/users`" in f.message


def test_wrong_method_is_contradicted(spec):
    f = one(run(spec, "DELETE /users\n"), "endpoint", "`DELETE`")
    assert f.verdict == CONTRADICTED and "GET, POST" in f.message
    assert f.evidence == "openapi.json#/paths/~1users"


def test_example_values_and_placeholder_names(spec):
    r1 = run(spec, "GET /users/42\n")
    assert r1.findings[0].verdict == VERIFIED and "example values" in r1.findings[0].message
    r2 = run(spec, "GET /users/{id}\n")
    assert r2.findings[0].verdict == VERIFIED and "`user_id`" in r2.findings[0].message


def test_server_base_path_is_stripped():
    data = json.loads(json.dumps(SPEC))
    data["servers"] = [{"url": "https://example.com/api/v1"}]
    report = run(Spec(data), "GET /api/v1/health\n")
    assert report.findings[0].verdict == VERIFIED and "base path" in report.findings[0].message


def test_unknown_prefix_is_uncertain(spec):
    f = run(spec, "GET /gateway/health\n").findings[0]
    assert f.verdict == UNCERTAIN and "deployment prefix" in f.message


def test_parameter_checks(spec):
    report = run(
        spec,
        """\
        ### GET /users

        | Name | Type | Required |
        |---|---|---|
        | limit | string | no |
        | q | string | no |
        | sort | string | no |
        """,
    )
    assert one(report, "parameter", "Parameter `limit`").verdict == CONTRADICTED
    assert (
        "type `string`, spec says `integer`"
        in one(report, "parameter", "Parameter `limit`").message
    )
    assert (
        "docs say optional, spec says required" in one(report, "parameter", "Parameter `q`").message
    )
    sort = one(report, "parameter", "Parameter `sort`")
    assert sort.verdict == CONTRADICTED and "`limit`, `q`" in sort.message


def test_parameter_location_mismatch(spec):
    report = run(
        spec,
        "### GET /users\n\n| Name | In |\n|---|---|\n| limit | header |\n",
    )
    assert "documented as `header`" in one(report, "parameter", "Parameter `limit`").message


def test_integer_and_number_are_compatible(spec):
    report = run(spec, "### GET /users\n\n| Name | Type |\n|---|---|\n| limit | number |\n")
    assert one(report, "parameter", "Parameter `limit`").verdict == VERIFIED


def test_body_fields(spec):
    report = run(
        spec,
        "### POST /users\n\n- `email` (string, required)\n- `age` (integer, optional)\n"
        "- `nickname` (string, optional)\n",
    )
    assert one(report, "parameter", "Parameter `email`").verdict == VERIFIED
    assert one(report, "parameter", "Parameter `age`").verdict == VERIFIED
    assert one(report, "parameter", "Parameter `nickname`").verdict == CONTRADICTED


def test_unresolved_ref_gives_uncertain_not_contradicted(spec):
    report = run(spec, "### GET /ext\n\n| Name |\n|---|\n| mystery |\n")
    assert one(report, "parameter", "`mystery`").verdict == UNCERTAIN


def test_response_checks(spec):
    report = run(
        spec,
        "### GET /users/{id}\n\nReturns 404 when missing and 500 on error.\n\n"
        "### GET /search\n\nReturns 418.\n",
    )
    assert one(report, "response", "Response `404`").verdict == VERIFIED
    assert one(report, "response", "Response `500`").verdict == CONTRADICTED
    assert one(report, "response", "Response `418`").verdict == UNCERTAIN  # spec has `default`


def test_status_family_matches(spec):
    data = json.loads(json.dumps(SPEC))
    data["paths"]["/health"]["get"]["responses"] = {"2XX": {}}
    report = run(Spec(data), "GET /health returns 204\n")
    assert one(report, "response", "Response `204`").verdict == VERIFIED


def test_gaps_are_reported(spec):
    report = run(spec, "### GET /users\n\nNothing about parameters here.\n")
    assert any("required query parameter `q`" in m for m in report.missing_required_params)
    assert "`POST /users`" in report.undocumented_operations
    assert "`GET /users`" not in report.undocumented_operations


# --------------------------------------------------------------------------- reporting + CLI


def test_render_markdown_sections(spec):
    report = run(spec, "### GET /users\n\n- `bogus` (string)\n\nGET /gateway/health\n")
    md = ac.render_markdown(report)
    assert "Contradicted by the spec" in md
    assert "Could not be verified" in md
    assert "<details>" in md and "Documentation gaps" in md
    assert "openapi.json#/paths" in md


def test_render_markdown_without_claims(spec):
    assert "No endpoint claims" in ac.render_markdown(run(spec, "just prose\n"))


def test_render_truncates_long_lists(spec):
    md = "\n".join(f"GET /nope{i}" for i in range(10))
    assert "and 5 more" in ac.render_markdown(run(spec, md), max_items=5)


def _project(tmp_path, doc):
    (tmp_path / "openapi.json").write_text(json.dumps(SPEC))
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "api.md").write_text(doc)
    return tmp_path


def test_cli_exit_codes_and_json(tmp_path, capsys, monkeypatch):
    root = _project(tmp_path, "### GET /health\n\nReturns 200.\n")
    monkeypatch.chdir(root)
    assert ac.main(["--spec", "openapi.json", "docs"]) == 0
    (root / "docs" / "api.md").write_text("### GET /nope\n")
    capsys.readouterr()
    assert ac.main(["--spec", "openapi.json", "--json", "docs"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["summary"][CONTRADICTED] == 1
    assert data["findings"][0]["file"] == "docs/api.md"
    assert ac.main(["--spec", "missing.json", "docs"]) == 2


def test_cli_autodetects_spec(tmp_path, monkeypatch):
    root = _project(tmp_path, "### GET /health\n")
    monkeypatch.chdir(root)
    assert ac.main(["docs"]) == 0


def test_collect_doc_files_skips_junk_dirs(tmp_path):
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.md").write_text("GET /a")
    (tmp_path / "a.md").write_text("GET /a")
    (tmp_path / "b.txt").write_text("GET /a")
    assert [p.name for p in ac.collect_doc_files([tmp_path])] == ["a.md"]


# --------------------------------------------------------------------------- action entry point


def test_run_from_env_reports_missing_spec(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("API_SPEC_PATH", raising=False)
    assert ac.run_from_env(post=False) is None
    out = capsys.readouterr().out
    assert "No supported web framework" in out and "api-code-path" in out


def test_run_from_env_uses_docs_subfolder(tmp_path, monkeypatch):
    root = _project(tmp_path, "### GET /nope\n")
    monkeypatch.chdir(root)
    monkeypatch.setenv("DOCS_SUBFOLDER", "docs")
    monkeypatch.delenv("API_SPEC_PATH", raising=False)
    report = ac.run_from_env(post=False)
    assert report.has_contradictions


def test_run_from_env_posts_comment(tmp_path, monkeypatch):
    root = _project(tmp_path, "### GET /health\n")
    monkeypatch.chdir(root)
    monkeypatch.setenv("DOCS_SUBFOLDER", "docs")
    posted = []
    monkeypatch.setattr(ac, "post_pr_comment", lambda md: posted.append(md) or True)
    ac.run_from_env(post=True)
    assert posted and "API Contract Verification" in posted[0]


def test_post_pr_comment_needs_pr_and_token(monkeypatch):
    monkeypatch.delenv("PR_NUMBER", raising=False)
    assert ac.post_pr_comment("x") is False


def test_parameter_type_behind_unresolved_ref_is_uncertain_not_verified():
    data = {
        "openapi": "3.0.0",
        "info": {},
        "paths": {
            "/items": {
                "get": {
                    "parameters": [
                        {"name": "q", "in": "query", "schema": {"$ref": "other.yaml#/Q"}},
                        {"name": "n", "in": "query", "schema": {"type": "integer"}},
                    ],
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    md = "### GET /items\n\n| Name | Type |\n|---|---|\n| q | integer |\n| n | integer |\n"
    report = verify(extract_claims(md, "d.md"), Spec(data))
    got = {f.message.split("`")[1]: f.verdict for f in report.findings if f.check == "parameter"}
    assert got == {"q": UNCERTAIN, "n": VERIFIED}


def _run_dispatch(monkeypatch, tmp_path, env):
    import sys

    import suggest_docs

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["suggest_docs.py", "--dry-run"])
    for key in ("API_SPEC_PATH", "API_CODE_PATH", "DOCS_SUBFOLDER"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("COMMENT_BODY", "[verify-api]")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    suggest_docs.main()


def test_verify_api_command_fails_when_configured_spec_is_unreadable(tmp_path, monkeypatch):
    with pytest.raises(SystemExit) as exc:
        _run_dispatch(
            monkeypatch, tmp_path, {"API_SPEC_PATH": "missing.json", "DOCS_SUBFOLDER": "."}
        )
    assert exc.value.code == 1


def test_verify_api_command_without_any_api_definition_is_only_a_notice(tmp_path, monkeypatch):
    _run_dispatch(monkeypatch, tmp_path, {"DOCS_SUBFOLDER": "."})  # nothing configured: no exit


def test_post_pr_comment_reports_gh_failure(monkeypatch):
    import subprocess

    import security_utils

    monkeypatch.setenv("PR_NUMBER", "7")
    monkeypatch.setenv("GH_TOKEN", "t")
    monkeypatch.setattr(
        security_utils,
        "run_command_safe",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 1, "", "boom"),
    )
    assert ac.post_pr_comment("x") is False
    monkeypatch.setattr(
        security_utils,
        "run_command_safe",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "", ""),
    )
    assert ac.post_pr_comment("x") is True
