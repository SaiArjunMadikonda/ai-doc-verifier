"""Anchor links and runbook commands."""

import pytest

from link_checks import github_slug, heading_anchors
from project_checks import CONTRADICTED, VERIFIED, verify_project


def run(root, checks, **files):
    docs = root / "docs"
    docs.mkdir(exist_ok=True)
    for name, text in files.items():
        (docs / name).write_text(text)
    report = verify_project(root, [docs], checks=checks)
    return {(f.subject, f.verdict) for f in report.findings}, report


# ------------------------------------------------------------------ anchors


@pytest.mark.parametrize(
    "heading,slug",
    [
        ("Getting Started", "getting-started"),
        ("The `--flag` option!", "the---flag-option"),
        ("Q&A: what's new?", "qa-whats-new"),
        ("[Link text](http://x.y) here", "link-text-here"),
        ("Snake_case and **bold**", "snake_case-and-bold"),
    ],
)
def test_github_slugs(heading, slug):
    assert github_slug(heading) == slug


def test_duplicate_setext_and_html_anchors():
    lines = [
        "# Intro", "", "## Usage", "", "## Usage", "", "Setext", "======", "",
        '<a name="legacy-id"></a>', "```", "# not a heading", "```",
    ]  # fmt: skip
    anchors = heading_anchors(lines)
    assert {"intro", "usage", "usage-1", "setext", "legacy-id"} <= anchors
    assert "not-a-heading" not in anchors


def test_anchor_links(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "setup.md").write_text("# Setup\n\n## Install it\n")
    got, report = run(
        tmp_path,
        ("link",),
        **{
            "a.md": (
                "# Top\n\n## Details\n\n"
                "See [install](setup.md#install-it) and [bad](setup.md#instal-it), "
                "[here](#details), [gone](#missing-part), [line](setup.md#L10), "
                "[web](https://x.y/z#frag).\n"
            )
        },
    )
    assert ("`setup.md#install-it`", VERIFIED) in got
    assert ("`setup.md#instal-it`", CONTRADICTED) in got
    assert ("`#details`", VERIFIED) in got
    assert ("`#missing-part`", CONTRADICTED) in got
    assert len(got) == 4  # line anchors and web links are left alone
    bad = next(f for f in report.findings if f.subject == "`setup.md#instal-it`")
    assert bad.fix == "Did you mean `#install-it`?"


def test_missing_target_is_left_to_the_reference_check(tmp_path):
    got, _ = run(tmp_path, ("link",), **{"a.md": "[x](nope.md#top)\n"})
    assert got == set()


# ------------------------------------------------------------------ runbooks


@pytest.fixture
def project(tmp_path):
    (tmp_path / "Makefile").write_text(
        ".PHONY: test deploy\ntest:\n\tpytest\ndeploy: test\n\t./scripts/deploy.sh\n"
    )
    (tmp_path / "package.json").write_text('{"scripts": {"build": "x", "migrate:up": "y"}}')
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "deploy.sh").write_text("echo hi\n")
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "seed.py").write_text("print(1)\n")
    return tmp_path


def test_make_npm_and_script_commands(project):
    got, report = run(
        project,
        ("runbook",),
        **{
            "ops.md": (
                "```bash\n"
                "make test\n"
                "$ make -j 4 deploy && make tset\n"
                "make -C sub other\n"
                "npm run build\n"
                "npm run migrate:up\n"
                "yarn run biuld\n"
                "./scripts/deploy.sh --now\n"
                "bash scripts/rotate.sh\n"
                "python tools/seed.py\n"
                "python -m http.server\n"
                "make CC=gcc\n"
                "```\n"
            )
        },
    )
    assert ("`make test`", VERIFIED) in got and ("`make deploy`", VERIFIED) in got
    assert ("`make tset`", CONTRADICTED) in got
    assert not any("other" in s for s, _ in got)  # -C points at a Makefile we did not read
    assert ("`npm run build`", VERIFIED) in got and ("`npm run migrate:up`", VERIFIED) in got
    assert ("`yarn run biuld`", CONTRADICTED) in got
    assert ("`scripts/deploy.sh`", VERIFIED) in got
    assert ("`scripts/rotate.sh`", CONTRADICTED) in got
    assert ("`tools/seed.py`", VERIFIED) in got
    assert next(f.fix for f in report.findings if f.subject == "`make tset`") == "Use `make test`."


def test_inline_commands_and_non_shell_blocks_and_tables(project):
    got, _ = run(
        project,
        ("runbook",),
        **{
            "ops.md": (
                "Run `make nothing` first.\n\n```python\nmake nothing2\n```\n\n"
                "| Command |\n|---|\n| `make nothing3` |\n"
            )
        },
    )
    assert got == {("`make nothing`", CONTRADICTED)}


def test_no_makefile_or_package_json_means_no_claims(tmp_path):
    got, _ = run(tmp_path, ("runbook",), **{"a.md": "```\nmake test\nnpm run build\n```\n"})
    assert got == set()
