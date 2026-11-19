"""
One command that runs every deterministic documentation check.

* API checks (needs an OpenAPI spec or readable API code): endpoints, parameters,
  responses, code examples and sample responses.
* Project checks (always): environment variables, CLI flags, file/function
  references and version claims.

The results are combined into one report with one health score.

    python src/docaudit.py docs README.md --code . --history .docaudit/history.json

In a PR, comment ``[verify-docs]``. No model is called.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from api_contract import (
    CONTRADICTED,
    UNCERTAIN,
    VERIFIED,
    Report,
    SpecError,
    post_pr_comment,
    render_markdown,
    resolve_spec,
    verify_docs,
)
from health import Health, append_history, compute_health, render_health
from project_checks import ProjectReport, render_project_markdown, verify_project


@dataclass
class AuditResult:
    api: Report | None = None
    project: ProjectReport | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def findings(self) -> list:
        return (self.api.findings if self.api else []) + (
            self.project.findings if self.project else []
        )

    @property
    def health(self) -> Health:
        return compute_health(self.findings)

    def count(self, verdict: str) -> int:
        return sum(1 for f in self.findings if f.verdict == verdict)

    @property
    def has_contradictions(self) -> bool:
        return self.count(CONTRADICTED) > 0

    def to_dict(self) -> dict:
        return {
            "health": self.health.to_dict(),
            "counts": {v: self.count(v) for v in (VERIFIED, CONTRADICTED, UNCERTAIN)},
            "notes": self.notes,
            "api": self.api.to_dict() if self.api else None,
            "project": self.project.to_dict() if self.project else None,
        }


def _under(root: Path, path: str | os.PathLike | None) -> str | None:
    """Resolve a configured path against the code repository, not the current directory."""
    if not path:
        return None
    p = Path(path)
    return str(p if p.is_absolute() else root / p)


def audit(
    root: str | os.PathLike = ".",
    doc_paths: list[str | os.PathLike] | None = None,
    spec_path: str | None = None,
    code_path: str | None = None,
    app: str | None = None,
    run_api: bool = True,
    run_project: bool = True,
    require_api: bool = False,
    docs_root: str | os.PathLike | None = None,
) -> AuditResult:
    """Run the selected checks and return the combined result.

    With *require_api* (set when the caller explicitly named a spec, code folder or app) a
    missing or unreadable API definition raises ``SpecError`` instead of being skipped, so a
    typo in a path cannot turn into a passing run.

    *root* is the code repository. If the docs are in another repository, give its folder as
    *docs_root* and the doc paths inside it.
    """
    root_p = Path(root).resolve()
    docs = list(doc_paths) if doc_paths else [root_p]
    result = AuditResult()
    if run_api:
        try:
            spec = resolve_spec(_under(root_p, spec_path), _under(root_p, code_path), app, root_p)
            result.api = verify_docs(spec, docs, Path(docs_root).resolve() if docs_root else root_p)
        except SpecError as e:
            if require_api:
                raise
            result.notes.append(f"API checks skipped: {e}")
    if run_project:
        result.project = verify_project(root_p, docs, docs_root=docs_root)
    return result


def render_audit_markdown(
    result: AuditResult, history: list[dict] | None = None, max_items: int = 50
) -> str:
    out = ["# 📚 Documentation audit", ""]
    health = result.health
    if health.score is not None:
        out += [
            "| Verdict | Checks |",
            "|---|---|",
            f"| ✅ Verified | {result.count(VERIFIED)} |",
            f"| ❌ Contradicted | {result.count(CONTRADICTED)} |",
            f"| ❓ Uncertain | {result.count(UNCERTAIN)} |",
            "",
        ]
        out += render_health(health, history)
    for note in result.notes:
        out += [f"> ℹ️ {note}", ""]
    if result.api:
        section = render_markdown(result.api, max_items=max_items)
        out += [section, ""]
    if result.project:
        out += [
            render_project_markdown(result.project, max_items=max_items).replace(
                "### 🧭", "## 🧭", 1
            )
        ]
    return "\n".join(out)


def run_from_env(post: bool = True) -> AuditResult | None:
    """Entry point for the ``[verify-docs]`` command."""
    code_root = Path.cwd()  # the checked-out code repository, before any change of directory
    docs_sub = os.environ.get("DOCS_SUBFOLDER", "").strip()
    docs_root: Path | None = None
    if docs_sub:
        doc_roots: list[str] = [str(code_root / docs_sub)]
        if (code_root / "README.md").is_file():
            doc_roots.append(str(code_root / "README.md"))
    else:
        from github_ops import setup_docs_environment

        if not setup_docs_environment():
            print("Failed to set up docs environment")
            return None
        # setup_docs_environment cloned the docs repository and moved into it; the code
        # repository stays the root for specs, code and project facts.
        docs_root = Path.cwd()
        os.chdir(code_root)
        doc_roots = [str(docs_root)]
    spec_env = os.environ.get("API_SPEC_PATH", "").strip() or None
    code_env = os.environ.get("API_CODE_PATH", "").strip() or None
    try:
        result = audit(
            code_root,
            doc_roots,
            spec_path=spec_env,
            code_path=code_env,
            require_api=bool(spec_env or code_env),
            docs_root=docs_root,
        )
    except SpecError as e:
        message = (
            f"## 📚 Documentation audit\n\n❌ Could not read the API definition you configured: {e}"
        )
        print(message)
        if post:
            post_pr_comment(message)
        return None
    markdown = render_audit_markdown(result)
    print(markdown)
    if post:
        post_pr_comment(markdown)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check documentation against the code.")
    src = parser.add_mutually_exclusive_group()
    src.add_argument("--spec", help="Path to openapi.json / openapi.yaml")
    src.add_argument("--app", help="FastAPI app as 'package.module:app' (executes project code)")
    src.add_argument("--code", help="Folder with API code, read statically and never run")
    parser.add_argument("docs", nargs="*", default=["."], help="Docs files or folders")
    parser.add_argument("--root", default=".", help="Repository root (default: current folder)")
    parser.add_argument("--no-api", action="store_true", help="Skip the API checks")
    parser.add_argument("--no-project", action="store_true", help="Skip the project checks")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of Markdown")
    parser.add_argument("--history", help="JSON file that keeps the health score over time")
    parser.add_argument(
        "--fail-under", type=float, metavar="SCORE", help="Exit 1 when health is below SCORE"
    )
    args = parser.parse_args(argv)
    try:
        result = audit(
            args.root,
            args.docs,
            spec_path=args.spec,
            code_path=args.code,
            app=args.app,
            run_api=not args.no_api,
            run_project=not args.no_project,
            require_api=bool(args.spec or args.code or args.app) and not args.no_api,
        )
    except SpecError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    history = append_history(args.history, result.health) if args.history else None
    print(
        json.dumps(result.to_dict(), indent=2)
        if args.json
        else render_audit_markdown(result, history)
    )
    score = result.health.score
    if score is None:
        print("Nothing was checked: no verifiable claims were found in the docs.", file=sys.stderr)
        if args.fail_under is not None:
            return 1  # an empty run must not pass a quality gate
    if args.fail_under is not None and score is not None and score < args.fail_under:
        print(f"Health {score:g} is below {args.fail_under:g}", file=sys.stderr)
        return 1
    return 1 if args.fail_under is None and result.has_contradictions else 0


if __name__ == "__main__":
    sys.exit(main())
