"""Tests for reading .rst and .adoc documentation (src/doc_formats.py)."""

import json
import textwrap

import api_contract as ac
import doc_formats as df
from tests.api_fixtures import SPEC


def convert(text, kind):
    md, linemap = df.to_markdown(textwrap.dedent(text), kind)
    return md, linemap


def claims_of(text, kind):
    md, linemap = convert(text, kind)
    claims = ac.extract_claims(md, "doc")
    for c in claims:
        c.line = df.remap_line(linemap, c.line)
    return claims


def test_kind_of():
    assert [df.kind_of(p) for p in ("a.md", "b.RST", "c.adoc", "d.asciidoc", "e.mdx")] == [
        "md",
        "rst",
        "adoc",
        "adoc",
        "md",
    ]


def test_markdown_is_unchanged():
    md, linemap = df.to_markdown("# T\n\nGET /x\n", "md")
    assert md == "# T\n\nGET /x\n" and linemap == [1, 2, 3]


# ----------------------------------------------------------------------------- RST


RST_HTTPDOMAIN = """\
    Users
    =====

    .. http:get:: /users/(int:user_id)

       Fetch one user.

       :query verbose: include details
       :query int limit: page size
       :statuscode 200: ok
       :statuscode 404: no such user

    .. http:post:: /users

       :<json string email: address
       :reqheader X-Key: api key
"""


def test_rst_httpdomain_endpoints_params_and_statuses():
    claims = claims_of(RST_HTTPDOMAIN, "rst")
    get, post = claims
    assert (get.method, get.raw_path) == ("GET", "/users/{user_id}")
    assert {(p.name, p.location, p.type) for p in get.params} == {
        ("verbose", "query", None),
        ("limit", "query", None),
    } or {(p.name, p.location) for p in get.params} == {("verbose", "query"), ("limit", "query")}
    assert get.statuses == ["200", "404"]
    assert {(p.name, p.location) for p in post.params} == {("email", "body"), ("X-Key", "header")}
    assert get.line == 4 and post.line == 13  # lines of the original .rst file


def test_rst_headings_levels_and_overlines():
    md, _ = convert(
        """\
        ==========
        Top title
        ==========

        Section
        -------

        Sub
        ~~~

        Section two
        -----------
        """,
        "rst",
    )
    assert [line for line in md.splitlines() if line.startswith("#")] == [
        "# Top title",
        "## Section",
        "### Sub",
        "## Section two",
    ]


def test_rst_code_block_content_is_fenced_and_ignored():
    claims = claims_of(
        """\
        Example
        -------

        .. code-block:: http

           GET /hidden HTTP/1.1
           Host: example.com

        Literal::

            POST /also-hidden

        After the block we mention GET /real.
        """,
        "rst",
    )
    assert [c.label for c in claims] == ["GET /real"]


def test_rst_grid_and_simple_tables_for_parameters():
    grid = """\
        GET /users
        ----------

        +---------+---------+----------+
        | Name    | Type    | Required |
        +=========+=========+==========+
        | limit   | string  | no       |
        +---------+---------+----------+
        | q       | string  | yes      |
        +---------+---------+----------+
        """
    (claim,) = claims_of(grid, "rst")
    assert [(p.name, p.type, p.required) for p in claim.params] == [
        ("limit", "string", False),
        ("q", "string", True),
    ]
    simple = """\
        GET /users
        ----------

        =====  =======  ========
        Name   Type     Required
        =====  =======  ========
        limit  integer  no
        q      string   yes
        =====  =======  ========
        """
    (claim,) = claims_of(simple, "rst")
    assert [(p.name, p.type, p.required) for p in claim.params] == [
        ("limit", "integer", False),
        ("q", "string", True),
    ]


def test_rst_inline_roles_and_literals():
    md, _ = convert("Use ``GET /users`` or :http:get:`/users` or :class:`~pkg.Thing`.\n", "rst")
    assert md == "Use `GET /users` or `/users` or `pkg.Thing`."


# ----------------------------------------------------------------------------- AsciiDoc


ADOC = """\
    = API guide

    == GET /users

    .Parameters
    [cols="1,1,1",options="header"]
    |===
    |Name |Type |Required
    |limit |integer |no
    |q |string |yes
    |===

    [source,http]
    ----
    GET /hidden HTTP/1.1
    ----

    .Responses
    |===
    |Status |Meaning

    |200 |ok
    |404 |missing
    |===
"""


def test_adoc_headings_tables_and_source_blocks():
    (claim,) = claims_of(ADOC, "adoc")
    assert claim.label == "GET /users"
    assert [(p.name, p.type, p.required) for p in claim.params] == [
        ("limit", "integer", False),
        ("q", "string", True),
    ]
    assert claim.statuses == ["200", "404"]
    assert claim.line == 3


def test_adoc_cell_per_line_tables():
    (claim,) = claims_of(
        """\
        == GET /users

        .Parameters
        [cols="2*",options="header"]
        |===
        |Name
        |Type
        |limit
        |integer
        |===
        """,
        "adoc",
    )
    assert [(p.name, p.type) for p in claim.params] == [("limit", "integer")]


# ----------------------------------------------------------------------------- end to end


def test_verify_docs_reads_rst_and_adoc_files(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "users.rst").write_text(
        "Users\n=====\n\n.. http:get:: /users\n\n   :query int limit: page size\n   :statuscode 500: boom\n"
    )
    (tmp_path / "docs" / "health.adoc").write_text(
        "== GET /health\n\nReturns 200.\n\n== GET /nope\n"
    )
    spec = ac.Spec(SPEC, source="openapi.json")
    report = ac.verify_docs(spec, [tmp_path / "docs"], tmp_path)
    by_file = {}
    for f in report.findings:
        by_file.setdefault(f.claim.file, []).append(f)
    assert {f.claim.file for f in report.findings} == {"docs/users.rst", "docs/health.adoc"}
    nope = next(f for f in by_file["docs/health.adoc"] if "/nope" in f.message)
    assert nope.verdict == ac.CONTRADICTED and nope.claim.line == 5  # original line
    boom = next(f for f in by_file["docs/users.rst"] if "`500`" in f.message)
    assert boom.verdict == ac.CONTRADICTED and boom.claim.line == 4


def test_cli_accepts_rst_files(tmp_path, monkeypatch, capsys):
    (tmp_path / "openapi.json").write_text(json.dumps(SPEC))
    (tmp_path / "api.rst").write_text(".. http:delete:: /users\n")
    monkeypatch.chdir(tmp_path)
    assert ac.main(["api.rst"]) == 1
    assert "DELETE /users" in capsys.readouterr().out
