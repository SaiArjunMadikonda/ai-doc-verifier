"""
Work out what a pull request touches, so an audit can look at only that.

An audit combines two selections:

* every documentation file the PR changed, and
* pages that mention a changed file, and pages whose API checks change across the PR.

Git supplies the changed paths and revision snapshots. API source is inspected statically.
"""

from __future__ import annotations

import subprocess
from collections import defaultdict
from pathlib import Path

from api_contract import Spec, SpecError, collect_doc_files, verify_docs
from api_diff import RefError, load_api_at

DOC_SUFFIXES = (".md", ".markdown", ".mdx", ".rst", ".adoc", ".asciidoc", ".txt")
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".tox"}


class GitError(RuntimeError):
    pass


def git(root: str | Path, *args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(root),
            capture_output=True,
            encoding="utf-8",
            errors="surrogateescape",
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise GitError(f"Could not run git: {e}") from e
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip()[:300]}")
    return proc.stdout


def changed_files(root: str | Path, base: str, head: str = "HEAD") -> list[str]:
    """Paths (relative to *root*, forward slashes) changed between *base* and *head*.

    Uses the merge base (``base...head``) so only the PR's own changes count.
    """
    # A move affects references to both names. NUL separators preserve literal filenames.
    out = git(root, "diff", "--no-renames", "--name-only", "-z", f"{base}...{head}", "--")
    return [path for path in out.split("\0") if path]


def is_doc(path: str) -> bool:
    return path.lower().endswith(DOC_SUFFIXES)


def _all_docs(root: Path) -> list[Path]:
    found = []
    for p in root.rglob("*"):
        if any(part in SKIP_DIRS for part in p.relative_to(root).parts):
            continue
        if p.is_file() and is_doc(p.name):
            found.append(p)
    return sorted(found)


def docs_to_check(root: str | Path, changed: list[str]) -> list[Path]:
    """Docs changed by the PR plus docs that mention a changed code file."""
    root_p = Path(root).resolve()
    changed_docs = {root_p / c for c in changed if is_doc(c) and (root_p / c).is_file()}
    code = [c for c in changed if not is_doc(c)]
    needles: set[str] = set()
    for c in code:
        needles.add(c.lower())
        name = Path(c).name.lower()
        if len(name) > 3:  # skip names too short to be meaningful, e.g. "a.py"
            needles.add(name)
    chosen = set(changed_docs)
    if needles:
        for doc in _all_docs(root_p):
            if doc in chosen:
                continue
            try:
                text = doc.read_text(encoding="utf-8").lower()
            except (OSError, UnicodeDecodeError):
                continue
            if any(n in text for n in needles):
                chosen.add(doc)
    return sorted(chosen)


def api_docs_to_check(
    root: Path,
    base: str,
    head: str,
    docs,
    *,
    spec_path: str | None = None,
    code_path: str | None = None,
) -> set[Path]:
    """Compare the same current pages against both contracts, including code examples.

    Comparing verifier results also covers shared schemas, base URLs and router prefixes
    without a second parser for each supported framework or documentation format.
    """
    candidates = collect_doc_files(docs)
    if not candidates:
        return set()
    ancestor = git(root, "merge-base", base, head).strip()
    contracts = []
    for ref in (ancestor, head):
        try:
            contracts.append(load_api_at(root, ref, spec_path, code_path))
        except SpecError:
            if ref == head and (spec_path or code_path):
                raise  # Explicitly configured inputs must be readable even for a scoped audit.
            contracts.append(None)
        except RefError as exc:
            raise GitError(str(exc)) from exc
    old, new = contracts
    if (old is None and new is None) or (old and new and old.data == new.data):
        return set()

    def checks(spec: Spec | None):
        by_file = defaultdict(set)
        if spec is not None:
            for f in verify_docs(spec, candidates, root).findings:
                by_file[f.claim.file].add(
                    (f.claim.line, f.claim.label, f.check, f.verdict, f.message)
                )
        return by_file

    before, after = checks(old), checks(new)
    if before and new is None:
        raise SpecError(
            "The base revision had a documented API, but its definition cannot be read at the PR head."
        )
    return {root / name for name in before.keys() | after.keys() if before[name] != after[name]}
