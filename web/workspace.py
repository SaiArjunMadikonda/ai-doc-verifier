"""Read-only repository dashboard and portable, fixture-only portfolio export."""

import json
import re
import sqlite3
import subprocess
import tempfile
from collections import defaultdict
from contextlib import closing
from functools import lru_cache
from pathlib import Path

from evaluation.run_eval import evaluate

from api_contract import SpecError, collect_doc_files, resolve_spec
from docaudit import audit
from meeting_notes import Corpus, extract_statements
from meeting_release import saved_decisions
from meeting_review import conflicts, owners_file, owners_for, pages, release_sentence
from meeting_sources import read_export
from meeting_sync import Decision, command, plan, remember

WEB = Path(__file__).resolve().parent
DEMO = WEB.parent / "examples" / "shop-demo"


@lru_cache(maxsize=1)
def evaluation():
    result = evaluate()
    return {
        **result.to_dict(),
        "results": [{"name": case.name, "problems": case.problems} for case in result.cases],
    }


def github_activity(root, repo, base):
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        raise ValueError("Use OWNER/REPOSITORY for --github-repo.")
    pulls = []
    for p in pages(command, root, f"repos/{repo}/pulls?state=all&per_page=100"):
        head = p["head"]
        match = re.fullmatch(r"meeting-docs/([a-f0-9]{24})", head["ref"])
        same_repo = (head.get("repo") or {}).get("full_name", "").casefold() == repo.casefold()
        if match and same_repo and p["base"]["ref"] == base:
            status = (
                "merged"
                if p.get("merged_at")
                else "draft"
                if p["state"] == "open" and p["draft"]
                else p["state"]
            )
            pulls.append(
                {
                    "id": match[1],
                    "number": p["number"],
                    "title": p["title"],
                    "status": status,
                    "owners": [u["login"] for u in p.get("requested_reviewers", [])]
                    + [t["slug"] for t in p.get("requested_teams", [])],
                }
            )
    issues = [
        {"number": i["number"], "title": i["title"], "status": i["state"]}
        for i in pages(command, root, f"repos/{repo}/issues?state=all&per_page=100")
        if "pull_request" not in i
        and re.search(r"<!-- meeting-docs-conflict:[a-f0-9]{24} -->", i.get("body") or "")
    ]
    return {"repo": repo, "pulls": pulls, "issues": issues}


def snapshot(
    project=DEMO, *, docs="docs", notes=(), state=None, release=None, repo=None, base=None
):
    root = Path(project).resolve()
    folder = (root / docs).resolve()
    if not folder.exists() or not folder.is_relative_to(root):
        raise ValueError("Select a documentation file or directory inside the project.")
    demo = root == DEMO
    notes = [Path(p).resolve() for p in notes]
    if demo and not notes:
        notes = [root / "meetings/api-sync.vtt"]
    doc_files = [p for p in collect_doc_files([folder]) if p.resolve() not in notes]
    report = audit(root, doc_files) if doc_files else None
    if report is None:
        raise ValueError("No supported documentation files were found.")
    try:
        spec = resolve_spec(None, None, None, root)
    except SpecError:
        spec = None
    corpus = Corpus.from_repo(root, doc_files, spec, skip=notes, docs_root=root)
    sources = [(p.as_posix(), read_export(p)) for p in notes]
    provenance = defaultdict(list)
    for path, remarks in sources:
        for s in extract_statements(remarks):
            key = Decision(s.kind, s.old, s.new, s.subject).key
            provenance[key].append(
                {"file": Path(path).name, "time": s.time, "speaker": s.speaker, "quote": s.text}
            )
    if sources:
        with tempfile.TemporaryDirectory() as tmp:
            preview_db = Path(tmp) / "preview.db"
            if state:
                with (
                    closing(
                        sqlite3.connect(state.resolve().as_uri() + "?mode=ro", uri=True)
                    ) as src,
                    closing(sqlite3.connect(preview_db)) as dst,
                ):
                    src.backup(dst)
            decisions = remember(preview_db, sources)
    else:
        decisions = sorted(saved_decisions(state).values())
    routes = owners_file(root)
    proposals = plan(
        decisions,
        corpus,
        can_check_removed_routes=bool(spec and not spec.unresolved_routes),
    )
    alerts = conflicts(proposals, corpus, routes, "HEAD")
    by_status = defaultdict(int)
    items = []
    for p in proposals:
        by_status[p.status] += 1
        items.append(
            {
                "id": p.decision.key,
                "kind": p.decision.kind,
                "old": p.decision.old,
                "new": p.decision.new,
                "subject": p.decision.subject,
                "status": p.status,
                "evidence": p.evidence,
                "wording": release_sentence(p.decision),
                "sources": provenance[p.decision.key],
                "edits": [
                    {"file": rel, "line": line, "before": before, "after": after}
                    for rel, lines in p.changes.items()
                    for line, (before, after) in lines.items()
                ],
                "owners": owners_for(p.changes, routes) if p.changes else [],
            }
        )
    activity = None
    activity_error = None
    if repo:
        try:
            activity = github_activity(root, repo, base)
        except (OSError, subprocess.CalledProcessError):
            activity_error = (
                "Check the GitHub CLI login and repository access, then rerun the audit."
            )
    return {
        "project": root.name,
        "demo": demo,
        "health": report.health.to_dict(),
        "findings": [
            f for part in (report.api, report.project) if part for f in part.to_dict()["findings"]
        ],
        "notes": report.notes,
        "decisions": items,
        "decision_counts": dict(by_status),
        "conflicts": [
            {"title": title, "description": body.split("-->\n", 1)[1].split("\n<!--")[0]}
            for title, body in alerts.values()
        ],
        "inputs": {
            "documents": [p.relative_to(root).as_posix() for p in doc_files],
            "meetings": [p.name for p in notes],
            "state": state.name if state else None,
            "owners": routes,
            "api": spec.source if spec else None,
        },
        "release": release.read_text(encoding="utf-8-sig") if release else None,
        "github": activity,
        "github_error": activity_error,
        "evaluation": evaluation(),
    }


def dashboard(data=None):
    html = (WEB / "dashboard.html").read_text(encoding="utf-8")
    embedded = json.dumps(data, ensure_ascii=True).replace("<", "\\u003c")
    return (
        html.replace("/* DASHBOARD_STYLES */", (WEB / "dashboard.css").read_text(encoding="utf-8"))
        .replace("/* DASHBOARD_APP */", (WEB / "dashboard.js").read_text(encoding="utf-8"))
        .replace("<!-- SNAPSHOT_DATA -->", embedded)
    )
