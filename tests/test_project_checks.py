"""Tests for project fact checks (src/project_checks.py)."""

import textwrap

import project_checks as pc
from api_contract import CONTRADICTED, UNCERTAIN, VERIFIED


def make_project(tmp_path, docs: dict[str, str], code: dict[str, str] | None = None):
    base = {
        "pyproject.toml": '[project]\nname = "shop"\nrequires-python = ">=3.11"\n'
        '[project.scripts]\nshopctl = "shop.cli:main"\n',
        "shop/__init__.py": "",
        "shop/config.py": textwrap.dedent(
            """\
            import os

            API_KEY = os.environ["SHOP_API_KEY"]
            DB_URL = os.getenv("SHOP_DB_URL", "sqlite://")
            """
        ),
        "shop/cli.py": textwrap.dedent(
            """\
            import argparse


            def build_order(items):
                return items


            class Cart:
                def checkout(self):
                    pass


            def main():
                p = argparse.ArgumentParser(prog="shopctl")
                p.add_argument("--dry-run", action="store_true")
                p.add_argument("--output-format", default="json")
                return p
            """
        ),
        ".env.example": "SHOP_REGION=us\n",
    }
    base.update(code or {})
    for name, content in {
        **base,
        **{f"docs/{k}" if not k.endswith(("README.md",)) else k: v for k, v in docs.items()},
    }.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return tmp_path


def run(tmp_path, docs, code=None, checks=pc.CHECK_NAMES):
    root = make_project(tmp_path, docs, code)
    return pc.verify_project(
        root,
        [root / "docs", root / "README.md"] if (root / "README.md").exists() else [root / "docs"],
        checks,
    )


def by(report, verdict, check=None):
    return [
        f for f in report.findings if f.verdict == verdict and (check is None or f.check == check)
    ]


# ----------------------------------------------------------------------------- env


def test_env_vars_verified_contradicted_and_suggested(tmp_path):
    report = run(
        tmp_path,
        {
            "setup.md": textwrap.dedent(
                """\
                # Setup

                Set the environment variable `SHOP_API_KEY` before starting.
                Set the env var `SHOP_API_TOKEN` too.
                Also `SHOP_REGION` in your .env file, and `SHOP_UNKNOWN_THING` as an env variable.

                ```bash
                export SHOP_DB_URL=postgres://x
                export SHOP_DEBUG_MODE=1
                export PATH=/usr/bin
                ```

                | Variable | Meaning |
                |---|---|
                | `SHOP_DB_URL` | database |
                | `SHOP_CACHE_TTL` | cache |
                """
            )
        },
        checks=("env",),
    )
    ok = {f.subject for f in by(report, VERIFIED)}
    assert ok == {"`SHOP_API_KEY`", "`SHOP_REGION`", "`SHOP_DB_URL`"}
    bad = {f.subject: f for f in by(report, CONTRADICTED)}
    assert bad["`SHOP_API_TOKEN`"].fix == "Rename `SHOP_API_TOKEN` to `SHOP_API_KEY` in the docs."
    assert "never read by the code" in bad["`SHOP_UNKNOWN_THING`"].message
    assert "`SHOP_DEBUG_MODE`" in bad and "`SHOP_CACHE_TTL`" in bad
    assert "`PATH`" not in {f.subject for f in report.findings}
    assert bad["`SHOP_API_TOKEN`"].file == "docs/setup.md" and bad["`SHOP_API_TOKEN`"].line == 4


def test_env_uncertain_when_code_has_no_reads_or_uses_settings_framework(tmp_path):
    docs = {"a.md": "Set the environment variable `MY_SECRET`.\n"}
    code = {"shop/config.py": "X = 1\n", ".env.example": ""}
    report = run(tmp_path, docs, code, checks=("env",))
    assert [f.verdict for f in report.findings] == [UNCERTAIN]

    code = {
        "shop/config.py": "from pydantic_settings import BaseSettings\nimport os\nos.getenv('A_B')\n"
    }
    report = run(tmp_path / "x", docs, code, checks=("env",))
    assert [f.verdict for f in report.findings] == [UNCERTAIN]


def test_env_in_javascript_code(tmp_path):
    report = run(
        tmp_path,
        {"a.md": "Set env var `PORT_NUMBER` and `SERVER_URL`.\n"},
        {"server.js": "const u = process.env.SERVER_URL; const p = process.env['PORT_NUMBER'];\n"},
        checks=("env",),
    )
    assert all(f.verdict == VERIFIED for f in report.findings) and len(report.findings) == 2


# ----------------------------------------------------------------------------- flags


def test_cli_flags(tmp_path):
    report = run(
        tmp_path,
        {
            "cli.md": textwrap.dedent(
                """\
                Use `--dry-run` to preview and `--output-format` to choose a format.
                The `--dry-runn` flag is a typo and `--verbose` was never added.

                ```bash
                shopctl --dry-run --output-fmt yaml
                pip install shop --upgrade
                docker run --rm shop --not-ours
                ```
                """
            )
        },
        checks=("flag",),
    )
    ok = {f.subject for f in by(report, VERIFIED)}
    assert ok == {"`--dry-run`", "`--output-format`"}
    bad = {f.subject: f for f in by(report, CONTRADICTED)}
    assert set(bad) == {"`--dry-runn`", "`--verbose`", "`--output-fmt`"}
    assert "Use `--dry-run`" in bad["`--dry-runn`"].fix
    assert "`--upgrade`" not in {
        f.subject for f in report.findings
    }  # third-party commands are ignored
    assert "`--not-ours`" not in {f.subject for f in report.findings}


def test_flags_skipped_without_a_cli_and_builtin_flags_ignored(tmp_path):
    code = {"shop/cli.py": "def main():\n    pass\n"}
    report = run(tmp_path, {"a.md": "Try `--anything`.\n"}, code, checks=("flag",))
    assert report.findings == []
    report = run(tmp_path / "y", {"a.md": "Run `--help` or `--version`.\n"}, checks=("flag",))
    assert report.findings == []


def test_typer_flags_make_misses_uncertain(tmp_path):
    code = {
        "shop/cli.py": "import typer\napp = typer.Typer()\n\n@app.command()\ndef run(max_items: int = 3):\n    pass\n"
    }
    report = run(tmp_path, {"a.md": "Use `--max-items` and `--other`.\n"}, code, checks=("flag",))
    assert [(f.subject, f.verdict) for f in report.findings] == [
        ("`--max-items`", VERIFIED),
        ("`--other`", UNCERTAIN),
    ]


# ----------------------------------------------------------------------------- references


def test_file_and_link_references(tmp_path):
    report = run(
        tmp_path,
        {
            "guide.md": textwrap.dedent(
                """\
                See `shop/config.py` and `cli.py` and `shop/confg.py` and `missing/thing.yml`.
                Read [the setup](setup.md) or [broken](nope.md#top) or [site](https://example.com/x.md).
                Mentions of `Node.js`, `/api/users`, `src/*.py`, and `example.com/a.md` are ignored.
                ```
                `ignored/in/fence.py`
                ```
                """
            ),
            "setup.md": "# Setup\n",
        },
        checks=("reference",),
    )
    ok = {f.subject for f in by(report, VERIFIED)}
    assert {"`shop/config.py`", "`cli.py`", "`setup.md`"} <= ok
    bad = {f.subject: f for f in by(report, CONTRADICTED)}
    assert set(bad) == {"`shop/confg.py`", "`missing/thing.yml`", "`nope.md`"}
    assert "shop/config.py" in bad["`shop/confg.py`"].fix
    assert all(
        "Node.js" not in f.subject and "example.com" not in f.subject for f in report.findings
    )


def test_historical_mentions_are_distinct_from_current_file_references(tmp_path):
    report = run(
        tmp_path,
        {
            "guide.md": "Use `shop/config.py`; `old/loader.py` was removed.\n"
            "Open `old/loader.py` to configure the service.\n"
            "We removed `old/tool.py` and now use `shop/cli.py`.\n"
        },
        checks=("reference",),
    )
    assert [(f.line, f.verdict) for f in report.findings if f.subject == "`old/loader.py`"] == [
        (1, UNCERTAIN),
        (2, CONTRADICTED),
    ]
    assert {f.subject for f in by(report, VERIFIED)} == {"`shop/config.py`", "`shop/cli.py`"}
    assert all(not f.fix for f in by(report, UNCERTAIN))
    assert "`old/tool.py`" in {f.subject for f in by(report, UNCERTAIN)}


def test_function_references(tmp_path):
    report = run(
        tmp_path,
        {
            "api.md": "Call `build_order()` then `Cart.checkout()`, not `build_ordr()`. "
            "Also `print()`, `os.path.join()`, `requests.get()`, `helper_that_is_gone()`, `shop.build_order()`.\n"
        },
        checks=("reference",),
    )
    got = {f.subject: f.verdict for f in report.findings}
    assert got["`build_order()`"] == VERIFIED
    assert got["`Cart.checkout()`"] == VERIFIED
    assert got["`shop.build_order()`"] == VERIFIED
    assert got["`build_ordr()`"] == CONTRADICTED
    assert got["`helper_that_is_gone()`"] == UNCERTAIN  # may be external, so not accused
    assert (
        "`print()`" not in got and "`os.path.join()`" not in got and "`requests.get()`" not in got
    )


# ----------------------------------------------------------------------------- versions


def test_python_version_claims(tmp_path):
    report = run(
        tmp_path,
        {
            "a.md": "Requires Python 3.9+ to run.\n",
            "b.md": "Works with Python 3.11 or newer.\n",
            "c.md": "We tested on Python 3.8 in CI.\n",  # not a minimum claim
        },
        checks=("version",),
    )
    got = {(f.file, f.verdict) for f in report.findings}
    assert got == {("docs/a.md", CONTRADICTED), ("docs/b.md", VERIFIED)}
    bad = by(report, CONTRADICTED)[0]
    assert "requires Python 3.11+" in bad.message and "pyproject.toml" in bad.evidence


def test_node_version_claims(tmp_path):
    code = {"package.json": '{"name": "web", "engines": {"node": ">=18"}}'}
    report = run(tmp_path, {"a.md": "Needs Node.js 16+ installed.\n"}, code, checks=("version",))
    assert [(f.verdict, f.subject) for f in report.findings] == [(CONTRADICTED, "Node.js 16+")]


# ----------------------------------------------------------------------------- report


def test_report_health_and_rendering(tmp_path):
    report = run(
        tmp_path,
        {"a.md": "Set the env var `SHOP_API_KEY`. Use `--nope`. See `gone/file.py`.\n"},
    )
    assert report.docs_checked == 1
    assert report.counts()[CONTRADICTED] == 2
    assert report.health.score == round(100 * 1 / 3, 1)
    md = pc.render_project_markdown(report)
    assert "Project facts check" in md and "docs/a.md:1" in md and "Suggested fix" in md
    assert report.to_dict()["counts"]["verified"] == 1


def test_rst_line_numbers_and_empty_report(tmp_path):
    root = make_project(tmp_path, {"x.rst": "Title\n=====\n\nSee ``gone/file.py`` for details.\n"})
    report = pc.verify_project(root, [root / "docs"])
    # the double-backtick RST literal is converted to inline code, line stays 4
    assert [(f.line, f.verdict) for f in report.findings if f.check == "reference"] == [
        (4, CONTRADICTED)
    ]
    empty = pc.verify_project(root, [root / "docs"], checks=("env",))
    assert "No environment variables" in pc.render_project_markdown(empty)
