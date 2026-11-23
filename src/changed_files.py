"""
Work out what a pull request touches, so an audit can look at only that.

``docs_to_check`` returns the documentation files an audit should read:

* every documentation file the PR changed, and
* every other documentation file that mentions a code file the PR changed (by path or by
  file name), because a code change can make an unchanged page wrong.

Nothing here runs project code; it only calls ``git diff``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

DOC_SUFFIXES = (".md", ".markdown", ".mdx", ".rst", ".adoc", ".asciidoc", ".txt")
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".tox"}


class GitError(RuntimeError):
    pass


def git(root: str | Path, *args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=str(root), capture_output=True, text=True, timeout=60
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
    out = git(root, "diff", "--name-only", "--diff-filter=ACMR", f"{base}...{head}")
    return [line.strip() for line in out.splitlines() if line.strip()]


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
