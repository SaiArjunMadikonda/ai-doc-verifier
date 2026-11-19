"""Tests for checking code examples and sample responses (src/examples_check.py)."""

import json
import textwrap

import api_contract as ac
import examples_check as ex
from api_contract import CONTRADICTED, UNCERTAIN, VERIFIED, Spec
from tests.api_fixtures import EXAMPLE_SPEC


def spec():
    return Spec(EXAMPLE_SPEC, source="openapi.json")


def check(markdown):
    findings, count = ex.check_examples(textwrap.dedent(markdown), "docs/api.md", spec())
    return findings, count


def messages(findings, verdict=None):
    return [f.message for f in findings if verdict in (None, f.verdict)]


# ----------------------------------------------------------------------------- calls


def test_curl_calls_verified_and_contradicted():
    findings, count = check(
        """\
        ```bash
        curl http://localhost:8000/users?limit=5
        curl -X POST https://api.shopco.io/v1/users -H "Content-Type: application/json" \\
             -d '{"email": "a@b.c", "nickname": "x"}'
        curl -X DELETE https://api.example.com/users
        curl https://api.example.com/usres
        ```
        """
    )
    assert count == 4
    verdicts = [f.verdict for f in findings]
    assert verdicts.count(VERIFIED) == 1  # only the first call is fully right
    bad = messages(findings, CONTRADICTED)
    assert any("body field `nickname`" in m for m in bad)
    assert any("uses a method the spec does not define" in m and "DELETE" in m for m in bad)
    assert any("`GET /usres`" in m and "path the spec does not define" in m for m in bad)
    fixes = {f.fix for f in findings if f.verdict == CONTRADICTED}
    assert any("`/users`" in fix for fix in fixes)  # did-you-mean for the typo


def test_curl_defaults_method_to_post_with_data_and_flags_missing_required():
    findings, _ = check("```sh\n$ curl http://localhost/users -d '{\"age\": 3}'\n```\n")
    assert any("omits the required body field `email`" in m for m in messages(findings))
    assert findings[0].claim.method == "POST"


def test_third_party_urls_are_ignored():
    findings, count = check(
        """\
        ```bash
        curl https://raw.githubusercontent.com/x/y/main/install.sh | sh
        curl https://api.github.com/user
        curl https://other-service.io/users
        ```
        """
    )
    assert (findings, count) == ([], 0)


def test_placeholder_hosts_and_relative_paths_are_ours():
    findings, count = check(
        """\
        ```bash
        curl $BASE_URL/users
        curl "{{host}}/users/42"
        curl ${API}/users?bogus=1
        ```
        """
    )
    assert count == 3
    assert [f.verdict for f in findings] == [VERIFIED, VERIFIED, CONTRADICTED]
    assert "query parameter `bogus`" in findings[2].message


def test_python_examples():
    findings, count = check(
        """\
        ```python
        import requests
        requests.get("http://localhost:8000/users", params={"limit": 1, "page": 2})
        requests.post(f"{BASE}/users", json={"email": "a"})
        requests.get(f"http://localhost/users/{uid}")
        print(d.get("/not-a-call"))
        ```
        """
    )
    assert count == 3
    bad = messages(findings, CONTRADICTED)
    assert len(bad) == 1 and "query parameter `page`" in bad[0]
    assert sum(1 for f in findings if f.verdict == VERIFIED) == 2


def test_javascript_examples():
    findings, count = check(
        """\
        ```js
        fetch("http://localhost:3000/users", { method: "POST", body: JSON.stringify({ email: "a", extra: 1 }) });
        const r = await axios.get(`http://localhost/users/${id}`);
        ```
        """
    )
    assert count == 2
    bad = messages(findings, CONTRADICTED)
    assert len(bad) == 1 and "body field `extra`" in bad[0]


def test_raw_http_examples():
    findings, count = check(
        """\
        ```http
        GET /users?limit=10 HTTP/1.1
        Host: api.shopco.io

        ```

        ```http
        PUT /users/3 HTTP/1.1
        ```
        """
    )
    assert count == 2
    assert findings[0].verdict == VERIFIED
    assert "method the spec does not define" in findings[1].message


def test_example_line_numbers_point_into_the_fence():
    findings, _ = check(
        "intro\n\n```bash\ncurl http://localhost/users\ncurl http://localhost/nope\n```\n"
    )
    assert [f.claim.line for f in findings] == [4, 5]


def test_unresolved_operations_make_example_findings_uncertain():
    data = json.loads(json.dumps(EXAMPLE_SPEC))
    data["paths"]["/users"]["get"]["x-unresolved"] = True
    findings, _ = ex.check_examples(
        "```bash\ncurl http://localhost/users?mystery=1\n```\n", "d.md", Spec(data)
    )
    assert findings[0].verdict == UNCERTAIN


# ----------------------------------------------------------------------------- sample responses


def test_response_sample_matches_schema():
    findings, count = check(
        """\
        ### GET /users/{id}

        Example response:

        ```json
        {"id": 1, "email": "a@b.c", "age": null, "address": {"city": "Dallas"}}
        ```
        """
    )
    assert count == 1
    assert [f.verdict for f in findings] == [VERIFIED]
    assert findings[0].check == "example-response"


def test_response_sample_problems_are_reported_with_paths():
    findings, _ = check(
        """\
        ### GET /users/{id}

        Response (200):

        ```json
        {"id": "7", "extra": true, "address": {"zip": "75001"}}
        ```
        """
    )
    text = " | ".join(messages(findings, CONTRADICTED))
    assert "`$.id` is string in the example but the schema says integer" in text
    assert "`$.extra` is in the example but not in the schema" in text
    assert "`$.email` is required by the schema but missing" in text
    assert "`$.address.city` is required by the schema but missing" in text


def test_response_sample_array_and_status_from_label():
    findings, _ = check(
        """\
        ### GET /users

        Returns a list:

        ```json
        [{"id": 1}]
        ```

        ### POST /users

        Response `201`:

        ```json
        {"id": 1, "email": "x"}
        ```

        Response 404:

        ```json
        {"detail": "nope"}
        ```
        """
    )
    kinds = {(f.claim.method, f.verdict) for f in findings}
    assert ("GET", CONTRADICTED) in kinds  # element 0 lacks required `email`
    assert ("POST", VERIFIED) in kinds
    assert any(f.verdict == UNCERTAIN and "no JSON schema" in f.message for f in findings)


def test_request_examples_are_not_treated_as_responses():
    findings, count = check(
        """\
        ### POST /users

        Request body:

        ```json
        {"email": "a@b.c"}
        ```
        """
    )
    assert (findings, count) == ([], 0)


def test_invalid_or_commented_json_is_tolerated():
    findings, _ = check(
        """\
        ### GET /users/{id}

        Example response:

        ```json
        // trimmed
        {"id": 1, "email": "x",}
        ```

        Example response:

        ```json
        {"id": ...}
        ```
        """
    )
    assert [f.verdict for f in findings] == [VERIFIED]  # the second block is skipped silently


def test_compare_json_nullable_and_number_rules():
    s = spec()
    schema = {
        "type": "object",
        "properties": {"n": {"type": "number"}, "m": {"type": "string", "nullable": True}},
    }
    assert ex.compare_json(s, schema, {"n": 3, "m": None}) == []
    assert ex.compare_json(s, schema, {"n": None}) == [
        "`$.n` is null but the schema does not allow null"
    ]
    assert ex.compare_json(s, {}, {"anything": 1}) == []


# ----------------------------------------------------------------------------- integration


def test_verify_docs_includes_examples_and_health(tmp_path):
    (tmp_path / "api.md").write_text(
        "### GET /users\n\n```bash\ncurl http://localhost/users?limit=1\ncurl http://localhost/nope\n```\n"
    )
    report = ac.verify_docs(spec(), [tmp_path], tmp_path)
    assert report.examples_checked == 2
    assert any(f.check == "example" and f.verdict == CONTRADICTED for f in report.findings)
    md = ac.render_markdown(report)
    assert "and **2** example(s)" in md
    off = ac.verify_docs(spec(), [tmp_path], tmp_path, check_examples=False)
    assert off.examples_checked == 0 and len(off.findings) < len(report.findings)


def test_examples_only_docs_still_render(tmp_path):
    (tmp_path / "a.md").write_text("```bash\ncurl http://localhost/nope\n```\n")
    report = ac.verify_docs(spec(), [tmp_path], tmp_path)
    assert report.claims_checked == 0 and report.examples_checked == 1
    assert "No endpoint claims" not in ac.render_markdown(report)


def test_rst_examples_map_back_to_original_lines(tmp_path):
    (tmp_path / "a.rst").write_text(
        "Intro\n=====\n\n.. code-block:: bash\n\n   curl http://localhost/nope\n"
    )
    report = ac.verify_docs(spec(), [tmp_path], tmp_path)
    assert [f.claim.line for f in report.findings] == [6]
