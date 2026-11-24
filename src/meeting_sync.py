"""Collect meeting decisions, check them against one repo, and propose documentation edits."""

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

from api_contract import SpecError, collect_doc_files, resolve_spec
from meeting_notes import (
    CONTRADICTED,
    Corpus,
    Statement,
    _stale,
    extract_statements,
    token_kind,
    verify_statement,
)
from meeting_sources import graph_notes, read_export
from security_utils import sanitize_output


@dataclass(frozen=True, order=True)
class Decision:
    kind: str
    old: str
    new: str
    subject: str

    @property
    def key(self):
        encoded = json.dumps([self.kind, self.old, self.new, self.subject]).encode()
        return hashlib.sha256(encoded).hexdigest()[:24]

    def as_statement(self):
        return Statement(self.kind, "", old=self.old, new=self.new, subject=self.subject)


def remember(database, sources):
    """A revised source replaces its own decisions. A failed import rolls back as a unit."""
    with closing(sqlite3.connect(database, timeout=45)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "CREATE TABLE IF NOT EXISTS sources (id TEXT PRIMARY KEY, decisions TEXT NOT NULL)"
        )
        for source, utterances in sources:
            records = set()
            for s in extract_statements(utterances):
                if s.kind == "action":
                    continue
                # Persist technical identifiers and values only. Backtick spans in notes
                # can otherwise contain arbitrary prose, which belongs in Microsoft 365.
                if not all(
                    re.fullmatch(r"[\w./{}:%$@+-]{0,160}", t) for t in (s.old, s.new, s.subject)
                ):
                    continue
                records.add((s.kind, s.old, s.new, s.subject))
            key = hashlib.sha256(source.encode()).hexdigest()
            db.execute(
                "INSERT INTO sources VALUES (?, ?) ON CONFLICT(id) DO UPDATE "
                "SET decisions=excluded.decisions WHERE decisions != excluded.decisions",
                (key, json.dumps(sorted(records))),
            )
        return sorted(
            {
                Decision(*fields)
                for (encoded,) in db.execute("SELECT decisions FROM sources")
                for fields in json.loads(encoded)
            }
        )


@dataclass
class Proposal:
    decision: Decision
    status: str
    evidence: str = ""
    # Repository path -> one-based line -> (original line, proposed line).
    changes: dict = field(default_factory=dict)


def plan(decisions, corpus, *, can_check_removed_routes=False):
    """Draft edits only for current mentions supported by the existing verifier."""
    by_subject = defaultdict(set)
    for d in decisions:
        by_subject[(d.kind == "change", d.subject or d.old)].add((d.kind, d.new))
    proposals = []
    for d in decisions:
        finding = verify_statement(d.as_statement(), corpus, "meeting")
        proposal = Proposal(d, finding.verdict, finding.evidence.removeprefix("meeting; "))
        proposals.append(proposal)
        if len(by_subject[(d.kind == "change", d.subject or d.old)]) > 1:
            proposal.status = "conflicting decisions"
            continue
        if finding.verdict != CONTRADICTED:
            if finding.pending:
                proposal.status = "pending"
            continue
        if d.kind == "change":
            hits = corpus.find_value_in_docs(d.subject, d.old)
            pattern = re.compile(r"(?<!\w)(?<!\d\.)" + re.escape(d.old) + r"(?!\w|\.\d)")
        else:
            hits = corpus.find_in_docs(d.old)
            pattern = corpus._pattern(d.old)
        for hit in _stale(hits, d.old):
            line = corpus.docs[hit.file].splitlines()[hit.line - 1]
            if d.kind == "remove":
                # Mark a Markdown endpoint section as retired without deleting examples
                # or adjacent sections. Other prose removals need editorial judgment.
                if (
                    not can_check_removed_routes
                    or token_kind(d.old) != "path"
                    or Path(hit.file).suffix.lower()
                    not in {
                        ".md",
                        ".markdown",
                    }
                ):
                    continue
                heading = (
                    r"(\s*#{1,6})\s+`?(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+"
                    + re.escape(d.old)
                    + r"`?\s*"
                )
                match = re.fullmatch(heading, line, re.IGNORECASE)
                if not match:
                    continue
                after = f"{match[1]} Removed {match[2].upper()} endpoint `{d.old}`"
            else:
                if d.kind == "change" and (
                    len(pattern.findall(line)) != 1
                    or not re.search(
                        r"\b"
                        + re.escape(d.subject)
                        + r"\b`?\s*(?:(?:is|of|=|:)\s*)?`?"
                        + re.escape(d.old)
                        + r"(?!\w|\.\d)",
                        line,
                        re.IGNORECASE,
                    )
                ):
                    continue
                after = pattern.sub(d.new, line)
            if after != line:
                proposal.changes.setdefault(hit.file, {})[hit.line] = (line, after)
        proposal.status = "ready" if proposal.changes else "manual review"
    return proposals


def command(args, cwd, *, check=True):
    return subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=check)


def apply_changes(root, proposal):
    """Apply only to matching, tracked files inside this checkout, preserving line endings."""
    for rel, edits in proposal.changes.items():
        path = (root / rel).resolve()
        if not path.is_relative_to(root.resolve()) or path != root.resolve() / rel:
            raise ValueError(f"Document path escaped the checkout or uses a symlink: {rel}")
        entry = command(["git", "--literal-pathspecs", "ls-files", "--stage", "--", rel], root)
        if not entry.stdout.startswith(("100644 ", "100755 ")):
            raise ValueError(f"Document is not a tracked regular file: {rel}")
        lines = path.read_bytes().decode("utf-8").splitlines(keepends=True)
        for number, (before, after) in edits.items():
            original = lines[number - 1]
            if original.rstrip("\r\n") != before:
                raise ValueError(f"Document changed while preparing the draft: {rel}:{number}")
            lines[number - 1] = after + original[len(before) :]
        path.write_bytes("".join(lines).encode("utf-8"))


def publish(root, proposal, repo, base):
    """Use the same branch across imports, retries, reruns and duplicate sources."""
    owner = repo.split("/")[0]
    branch = "meeting-docs/" + proposal.decision.key
    lookup = [
        "gh",
        "api",
        "--method",
        "GET",
        f"repos/{repo}/pulls",
        "-f",
        "state=all",
        "-f",
        f"head={owner}:{branch}",
        "-f",
        f"base={base}",
        "-f",
        "per_page=1",
    ]
    existing = json.loads(command(lookup, root).stdout)
    if existing:
        return f"already reviewed or open (PR #{existing[0]['number']})"
    title = (
        f"docs: update {proposal.decision.subject or proposal.decision.old} from meeting decision"
    )
    with tempfile.TemporaryDirectory() as tmp:
        tree = Path(tmp) / "tree"
        command(["git", "worktree", "add", "--quiet", "--detach", str(tree), "HEAD"], root)
        try:
            apply_changes(tree, proposal)
            command(["git", "--literal-pathspecs", "add", "--", *proposal.changes], tree)
            command(["git", "commit", "--quiet", "-m", title], tree)
            ref = f"refs/heads/{branch}"
            remote = command(
                ["git", "ls-remote", "--exit-code", "--heads", "origin", ref], tree, check=False
            )
            if remote.returncode == 2:
                command(["git", "push", "origin", f"HEAD:{ref}"], tree)
            elif remote.returncode == 0:
                # Recover if the push succeeded but creating the PR failed. Never overwrite
                # another commit. Verify the branch contains the exact proposed patch.
                command(["git", "fetch", "--quiet", "origin", ref], tree)

                def diff(commit):
                    return command(
                        ["git", "diff", "--no-ext-diff", commit + "^", commit], tree
                    ).stdout

                if diff("HEAD") != diff("FETCH_HEAD"):
                    raise ValueError(f"Existing draft branch has different changes: {branch}")
            else:
                remote.check_returncode()
            body = Path(tmp) / "review.md"
            body.write_text(
                "The meeting checker found a shipped change that the documentation still describes "
                "in its old form.\n\n"
                + "Repository evidence: "
                + proposal.evidence
                + "\n\nPlease review the edited lines:\n"
                + "".join(
                    f"- `{rel}:{line}`\n"
                    for rel, edits in proposal.changes.items()
                    for line in edits
                ),
                encoding="utf-8",
            )
            result = command(
                [
                    "gh",
                    "pr",
                    "create",
                    "--repo",
                    repo,
                    "--base",
                    base,
                    "--head",
                    branch,
                    "--draft",
                    "--title",
                    title,
                    "--body-file",
                    str(body),
                ],
                tree,
            )
            return result.stdout.strip()
        finally:
            command(["git", "worktree", "remove", "--force", str(tree)], root)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument(
        "--docs", default="docs", help="Documentation file or folder, relative to the repo"
    )
    parser.add_argument("--code", default=".", help="Implementation folder, relative to the repo")
    parser.add_argument("--spec", help="Optional explicit OpenAPI file, relative to the repo")
    parser.add_argument(
        "--notes", type=Path, action="append", default=[], help="Import a local export"
    )
    parser.add_argument(
        "--graph", action="store_true", help="Collect the configured Microsoft sources"
    )
    parser.add_argument(
        "--state", required=True, type=Path, help="Persistent SQLite file outside the checkout"
    )
    parser.add_argument(
        "--publish", action="store_true", help="Create draft PRs (default: preview)"
    )
    parser.add_argument("--base", default=os.environ.get("MEETING_BASE_BRANCH"))
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"))
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        docs, code = (root / args.docs).resolve(), (root / args.code).resolve()
        if (
            not docs.exists()
            or not docs.is_relative_to(root)
            or not code.is_dir()
            or not code.is_relative_to(root)
        ):
            raise ValueError("Select existing docs and code inside the repository.")
        if args.state.resolve().is_relative_to(root):
            raise ValueError("Choose a state file outside the checkout.")
        if args.publish:
            if not args.base or not args.repo:
                raise ValueError("Publishing requires --repo OWNER/REPO and --base BRANCH.")
            if command(["git", "status", "--porcelain"], root).stdout:
                raise ValueError("Publish from a clean checkout of the target branch.")
            top = command(["git", "rev-parse", "--show-toplevel"], root).stdout.strip()
            if Path(top).resolve() != root:
                raise ValueError("--root must be the top of the target Git checkout.")
            head = command(["git", "rev-parse", "HEAD"], root).stdout.strip()
            remote = command(
                ["git", "ls-remote", "--exit-code", "--heads", "origin", "refs/heads/" + args.base],
                root,
            ).stdout.split()
            if remote[0] != head:
                raise ValueError("The base branch has moved. Retry from its latest commit.")
        try:
            spec = resolve_spec(str(root / args.spec) if args.spec else None, None, None, code)
        except SpecError:
            if args.spec:
                raise
            spec = None
        note_paths = {n.resolve() for n in args.notes}
        allowed = [
            p
            for p in collect_doc_files([docs])
            if p.resolve() == p.absolute() and p.resolve() not in note_paths
        ]
        if not allowed:
            raise ValueError("No supported documentation files in the selected path.")
        corpus = Corpus.from_repo(code, allowed, spec, skip=args.notes, docs_root=root)
        if code != root:
            prefix = code.relative_to(root)
            corpus.code = {(prefix / name).as_posix(): text for name, text in corpus.code.items()}
            corpus.files = {(prefix / name).as_posix() for name in corpus.files}

        def sources():
            for path in args.notes:
                yield path.resolve().as_posix(), read_export(path)
            if args.graph:
                yield from graph_notes()

        decisions = remember(args.state, sources())
        print(f"Checking {len(decisions)} unique meeting decision(s).")
        for proposal in plan(
            decisions,
            corpus,
            can_check_removed_routes=spec is not None and not spec.unresolved_routes,
        ):
            d = proposal.decision
            description = " ".join(filter(None, (d.kind, d.subject, d.old, d.new)))
            print(f"{proposal.status}: {description}")
            for rel, edits in proposal.changes.items():
                for number, (before, after) in edits.items():
                    print(f"  {rel}:{number}\n  - {before}\n  + {after}")
            if args.publish and proposal.status == "ready":
                print(publish(root, proposal, args.repo, args.base))
    except subprocess.CalledProcessError as exc:
        detail = sanitize_output((exc.stderr or "").strip()) or "command returned no diagnostic"
        print(
            f"Meeting sync failed: {exc.cmd[0]} exited {exc.returncode}: {detail}", file=sys.stderr
        )
        return 1
    except (OSError, ValueError, SpecError, sqlite3.Error) as exc:
        print(f"Meeting sync failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
