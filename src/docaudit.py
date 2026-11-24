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
    collect_doc_files,
    post_pr_comment,
    render_markdown,
    resolve_spec,
    verify_docs,
)
from api_diff import DiffReport, RefError, compare, render_diff_markdown
from baseline import (
    fingerprint,
    is_ignored,
    load_baseline,
    load_ignore_rules,
    write_baseline,
)
from changed_files import GitError, api_docs_to_check, changed_files, docs_to_check, is_doc
from health import Health, append_history, compute_health, render_health
from meeting_notes import MeetingReport, render_meeting_markdown
from meeting_notes import run as run_meeting
from project_checks import ProjectReport, render_project_markdown, verify_project
from sarif import dumps as sarif_dumps

BASELINE_FILE = ".docaudit-baseline.json"
IGNORE_FILE = ".docaudit.json"


@dataclass
class AuditResult:
    api: Report | None = None
    project: ProjectReport | None = None
    meeting: MeetingReport | None = None
    api_changes: DiffReport | None = None
    notes: list[str] = field(default_factory=list)
    baselined: set[str] = field(default_factory=set)  # fingerprints of known, accepted problems
    ignored: int = 0  # findings removed by ignore rules
    scope: list[str] | None = None  # set when only a PR's files were checked

    @property
    def findings(self) -> list:
        return (self.api.findings if self.api else []) + (
            self.project.findings if self.project else []
        )

    @property
    def health(self) -> Health:
        return compute_health(self.findings)

    @property
    def threshold_health(self) -> Health:
        """Health for --fail-under, excluding explicitly accepted contradictions."""
        if not self.baselined:
            return self.health
        return compute_health(
            [
                f
                for f in self.findings
                if f.verdict != CONTRADICTED or fingerprint(f) not in self.baselined
            ]
        )

    def count(self, verdict: str) -> int:
        return sum(1 for f in self.findings if f.verdict == verdict)

    @property
    def gate_findings(self) -> list:
        """Everything that can fail a run: the checks plus meeting follow-ups."""
        return self.findings + (self.meeting.findings if self.meeting else [])

    @property
    def new_contradictions(self) -> list:
        """Contradicted findings that are not already in the baseline."""
        return [
            f
            for f in self.gate_findings
            if f.verdict == CONTRADICTED and fingerprint(f) not in self.baselined
        ]

    @property
    def has_contradictions(self) -> bool:
        return bool(self.new_contradictions)

    def to_dict(self) -> dict:
        return {
            "health": self.health.to_dict(),
            "threshold_health": self.threshold_health.to_dict(),
            "counts": {v: self.count(v) for v in (VERIFIED, CONTRADICTED, UNCERTAIN)},
            "notes": self.notes,
            "new_contradictions": len(self.new_contradictions),
            "baselined": len(self.baselined),
            "ignored": self.ignored,
            "scope": self.scope,
            "api": self.api.to_dict() if self.api else None,
            "project": self.project.to_dict() if self.project else None,
            "meeting": self.meeting.to_dict() if self.meeting else None,
            "api_changes": self.api_changes.to_dict() if self.api_changes else None,
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
    baseline_path: str | os.PathLike | None = None,
    ignore_path: str | os.PathLike | None = None,
    changed_since: str | None = None,
    head: str = "HEAD",
    meeting_path: str | os.PathLike | None = None,
    use_ai: bool = False,
    compare_ref: str | None = None,
) -> AuditResult:
    """Run the selected checks and return the combined result.

    With *changed_since* (a git ref such as ``origin/main``) only the docs a change touches
    are checked: edited pages, references to changed files and pages whose API checks differ.
    *baseline_path*
    lists known problems that must not fail the run; *ignore_path* lists deliberate ones.

    With *require_api* (set when the caller explicitly named a spec, code folder or app) a
    missing or unreadable API definition raises ``SpecError`` instead of being skipped, so a
    typo in a path cannot turn into a passing run.

    *root* is the code repository. If the docs are in another repository, give its folder as
    *docs_root* and the doc paths inside it.
    """
    root_p = Path(root).resolve()
    docs = list(doc_paths) if doc_paths else [root_p]
    result = AuditResult()
    if changed_since:
        changed = changed_files(root_p, changed_since, head)
        picked = set(docs_to_check(root_p, changed))
        if run_api and any(not is_doc(path) for path in changed):
            if app:
                # Runtime apps can only be inspected in the working tree.
                picked.update(p.resolve() for p in collect_doc_files(docs))
            else:
                picked.update(
                    api_docs_to_check(
                        root_p, changed_since, head, docs, spec_path=spec_path, code_path=code_path
                    )
                )
        picked = sorted(picked)
        if doc_paths:  # an explicit doc list narrows the choice further
            allowed = [Path(d).resolve() for d in doc_paths]
            picked = [p for p in picked if any(p == a or a in p.parents for a in allowed)]
        result.scope = [
            p.relative_to(root_p).as_posix() if p.is_relative_to(root_p) else p.as_posix()
            for p in picked
        ]
        if not picked:
            result.notes.append(
                f"No documentation is affected by the {len(changed)} file(s) changed "
                f"since {changed_since}; nothing to check."
            )
            return result
        docs = picked
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
    if meeting_path:
        result.meeting = run_meeting(
            meeting_path,
            root_p,
            docs,
            spec_path=_under(root_p, spec_path),
            code_path=_under(root_p, code_path),
            use_ai=use_ai,
            docs_root=docs_root,
        )
    if compare_ref:
        try:
            result.api_changes = compare(
                root_p, compare_ref, None, docs, spec_path=spec_path, code_path=code_path
            )
        except (RefError, SpecError) as e:
            result.notes.append(f"API change comparison skipped: {e}")
    rules = load_ignore_rules(ignore_path)
    if rules:
        for report in (result.api, result.project):
            if report is None:
                continue
            kept = [f for f in report.findings if not is_ignored(f, rules)]
            result.ignored += len(report.findings) - len(kept)
            report.findings = kept
        if result.meeting:
            before = len(result.meeting.findings)
            result.meeting.findings = [
                f for f in result.meeting.findings if not is_ignored(f, rules)
            ]
            result.ignored += before - len(result.meeting.findings)
    result.baselined = load_baseline(baseline_path)
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
    if result.scope is not None and result.scope:
        out += [f"> 🔍 Checked only the {len(result.scope)} page(s) this change affects.", ""]
    known = sum(1 for f in result.gate_findings if fingerprint(f) in result.baselined)
    if known:
        out += [
            f"> 📌 {known} known problem(s) are in the baseline and do not fail the run. "
            f"{len(result.new_contradictions)} new contradiction(s).",
            "",
        ]
        score = result.threshold_health.score
        if score is None:
            out += ["No unbaselined checks contribute to the health threshold.", ""]
        else:
            out += [f"Health for the threshold after applying the baseline: {score:g}/100.", ""]
    if result.ignored:
        out += [f"> 🙈 {result.ignored} finding(s) hidden by ignore rules.", ""]
    if result.api:
        section = render_markdown(result.api, max_items=max_items)
        out += [section, ""]
    if result.project:
        out += [
            render_project_markdown(result.project, max_items=max_items).replace(
                "### 🧭", "## 🧭", 1
            )
        ]
    if result.meeting:
        out += ["", render_meeting_markdown(result.meeting, max_items=max_items)]
    if result.api_changes:
        out += ["", render_diff_markdown(result.api_changes, max_items=max_items)]
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
    base_ref = os.environ.get("DOCAUDIT_BASE_REF", "").strip() or None
    if base_ref and docs_root:
        base_ref = None  # docs live in another repository: a code diff says nothing about them
    options = {
        "baseline_path": (code_root / BASELINE_FILE)
        if (code_root / BASELINE_FILE).is_file()
        else None,
        "ignore_path": (code_root / IGNORE_FILE) if (code_root / IGNORE_FILE).is_file() else None,
    }
    try:
        try:
            result = audit(
                code_root,
                doc_roots,
                spec_path=spec_env,
                code_path=code_env,
                require_api=bool(spec_env or code_env),
                docs_root=docs_root,
                changed_since=base_ref,
                **options,
            )
        except GitError as e:  # e.g. a shallow checkout: fall back to checking everything
            print(f"Could not limit the audit to the changed files ({e}); checking everything.")
            result = audit(
                code_root,
                doc_roots,
                spec_path=spec_env,
                code_path=code_env,
                require_api=bool(spec_env or code_env),
                docs_root=docs_root,
                **options,
            )
    except (SpecError, ValueError) as e:
        what = (
            "Could not read the API definition you configured"
            if isinstance(e, SpecError)
            else "Could not run the audit"
        )
        message = f"## 📚 Documentation audit\n\n❌ {what}: {e}"
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
    parser.add_argument("docs", nargs="*", help="Docs files or folders (default: the whole repo)")
    parser.add_argument("--root", default=".", help="Repository root (default: current folder)")
    parser.add_argument("--no-api", action="store_true", help="Skip the API checks")
    parser.add_argument("--no-project", action="store_true", help="Skip the project checks")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of Markdown")
    parser.add_argument("--history", help="JSON file that keeps the health score over time")
    parser.add_argument(
        "--fail-under",
        type=float,
        metavar="SCORE",
        help="Exit 1 when health excluding baselined contradictions is below SCORE",
    )
    parser.add_argument(
        "--changed-since",
        metavar="REF",
        help="Check only docs affected by changes since this git ref (e.g. origin/main)",
    )
    parser.add_argument("--head", default="HEAD", help="Newest ref for --changed-since")
    parser.add_argument(
        "--baseline",
        help=f"Known-problems file; only new contradictions fail (default: {BASELINE_FILE})",
    )
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="Write today's contradictions to the baseline file and exit 0",
    )
    parser.add_argument("--ignore-file", help=f"Ignore rules (default: {IGNORE_FILE})")
    parser.add_argument("--sarif", metavar="FILE", help="Also write findings as SARIF 2.1.0")
    parser.add_argument(
        "--meeting", metavar="FILE", help="Teams .vtt/.docx or text notes to check as well"
    )
    parser.add_argument(
        "--ai", action="store_true", help="Let a model also propose meeting decisions"
    )
    parser.add_argument(
        "--compare-to",
        metavar="REF",
        help="Also report API changes between REF and the working tree, and the docs they affect",
    )
    parser.add_argument(
        "--fail-on-breaking",
        action="store_true",
        help="With --compare-to: exit 1 when the API has breaking changes",
    )
    args = parser.parse_args(argv)
    root_p = Path(args.root).resolve()
    baseline_path = args.baseline or (
        str(root_p / BASELINE_FILE) if (root_p / BASELINE_FILE).is_file() else None
    )
    ignore_path = args.ignore_file or (
        str(root_p / IGNORE_FILE) if (root_p / IGNORE_FILE).is_file() else None
    )
    if args.update_baseline:
        baseline_path = args.baseline or str(root_p / BASELINE_FILE)
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
            baseline_path=None if args.update_baseline else baseline_path,
            ignore_path=ignore_path,
            changed_since=args.changed_since,
            head=args.head,
            meeting_path=args.meeting,
            use_ai=args.ai,
            compare_ref=args.compare_to,
        )
    except (SpecError, GitError, ValueError, OSError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    if args.sarif:
        Path(args.sarif).write_text(sarif_dumps(result.gate_findings), encoding="utf-8")
    if args.update_baseline:
        count = write_baseline(baseline_path, result.gate_findings)
        print(f"Baseline written to {baseline_path}: {count} known contradiction(s).")
        return 0
    history = append_history(args.history, result.health) if args.history else None
    print(
        json.dumps(result.to_dict(), indent=2)
        if args.json
        else render_audit_markdown(result, history)
    )
    if args.fail_on_breaking and result.api_changes and result.api_changes.breaking:
        print("Breaking API changes found.", file=sys.stderr)
        return 1
    score = result.health.score
    if score is None and result.scope is not None and not result.scope:
        return 0  # a change that touches no documentation has nothing to fail
    if score is None:
        print("Nothing was checked: no verifiable claims were found in the docs.", file=sys.stderr)
        if args.fail_under is not None:
            return 1  # an empty run must not pass a quality gate
    threshold_score = result.threshold_health.score
    if (
        args.fail_under is not None
        and threshold_score is not None
        and threshold_score < args.fail_under
    ):
        print(f"Health {threshold_score:g} is below {args.fail_under:g}", file=sys.stderr)
        return 1
    return 1 if args.fail_under is None and result.has_contradictions else 0


if __name__ == "__main__":
    sys.exit(main())
