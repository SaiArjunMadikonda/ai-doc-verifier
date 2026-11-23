"""
Check links between documentation pages, including ``#anchor`` fragments.

A link such as ``[setup](setup.md#install)`` can break in two ways: the file may be gone
(the reference check already reports that), or the heading may have been renamed so the
anchor no longer exists. This module checks the second. Anchors are computed the way
GitHub does it (lower case, punctuation removed, spaces become hyphens, repeated headings
get ``-1``, ``-2`` ...), including ``<a name="x">`` / ``id="x"`` tags.

Only Markdown targets are read; anchors into other file types (``#L10`` line links,
reStructuredText, source files) are left alone.
"""

from __future__ import annotations

import difflib
import re
from pathlib import Path

_LINK = re.compile(r"!?\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
_ATX = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_HTML_ID = re.compile(r"""<[a-zA-Z][^>]*\b(?:id|name)\s*=\s*["']([^"']+)["']""")
_MARKDOWN_SUFFIXES = (".md", ".markdown", ".mdx")


def github_slug(heading: str) -> str:
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", heading)
    text = re.sub(r"<[^>]+>", "", text).replace("`", "")
    text = re.sub(r"[*~]", "", text).strip().lower()
    text = re.sub(r"[^\w\- ]", "", text, flags=re.UNICODE)
    return text.replace(" ", "-")


def heading_anchors(lines: list[str]) -> set[str]:
    """All anchors a Markdown document offers, given its lines."""
    anchors: set[str] = set()
    seen: dict[str, int] = {}
    in_fence = False
    previous = ""
    for line in lines:
        if re.match(r"^\s*(```|~~~)", line):
            in_fence = not in_fence
            previous = ""
            continue
        if in_fence:
            continue
        heading = None
        m = _ATX.match(line)
        if m:
            heading = m.group(1)
        elif previous.strip() and re.match(r"^\s{0,3}(=+|-{2,})\s*$", line):
            heading = previous  # setext heading
        if heading:
            slug = github_slug(heading)
            n = seen.get(slug, 0)
            seen[slug] = n + 1
            anchors.add(slug if n == 0 else f"{slug}-{n}")
        anchors.update(_HTML_ID.findall(line))
        previous = line
    return {a.lower() for a in anchors}


def _target_lines(found: str, idx) -> list[str] | None:
    """Lines of the Markdown file *found* (relative path), or None if it cannot be read."""
    if not found.lower().endswith(_MARKDOWN_SUFFIXES):
        return None
    candidates = []
    docs_root = getattr(idx, "docs_root", None)
    if docs_root is not None and found in idx.doc_files:
        candidates.append(Path(docs_root) / found)
    candidates.append(Path(idx.root) / found)
    for path in candidates:
        try:
            return path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
    return None


def check_links(lines, file, idx, linemap):
    from project_checks import CONTRADICTED, VERIFIED, ProjectFinding, _resolve_path, remap_line

    own: set[str] | None = None
    doc_dir = file.rsplit("/", 1)[0] if "/" in file else ""
    cache: dict[str, set[str] | None] = {}
    out: list[ProjectFinding] = []
    seen: set[tuple[int, str]] = set()
    for dl in lines:
        if dl.fence is not None:
            continue
        for raw in _LINK.findall(dl.text):
            if "#" not in raw or re.match(r"^[a-z][a-z0-9+.-]*:", raw, re.I):
                continue
            target, anchor = raw.split("#", 1)
            anchor = anchor.split("?")[0]
            if not anchor or re.fullmatch(r"L\d+(?:-L?\d+)?", anchor) or "{" in raw or "<" in raw:
                continue
            key = (dl.number, raw)
            if key in seen:
                continue
            seen.add(key)
            if target:
                found = _resolve_path(target.split("?")[0], doc_dir, idx)
                if found is None:
                    continue  # a missing file is reported by the reference check
                if found not in cache:
                    text = _target_lines(found, idx)
                    cache[found] = heading_anchors(text) if text is not None else None
                anchors, where = cache[found], found
            else:
                if own is None:
                    own = heading_anchors([d.text for d in lines if d.fence is None])
                anchors, where = own, file
            if anchors is None:
                continue
            line = remap_line(linemap, dl.number)
            subject = f"`{raw}`"
            if anchor.lower() in anchors:
                out.append(
                    ProjectFinding(
                        VERIFIED,
                        "link",
                        file,
                        line,
                        subject,
                        f"The heading for {subject} exists.",
                        where,
                    )
                )
                continue
            near = difflib.get_close_matches(anchor.lower(), sorted(anchors), n=1, cutoff=0.6)
            fix = f"Did you mean `#{near[0]}`?" if near else "Fix the anchor or add the heading."
            out.append(
                ProjectFinding(
                    CONTRADICTED,
                    "link",
                    file,
                    line,
                    subject,
                    f"The link {subject} points to a heading that does not exist in `{where}`.",
                    f"searched the headings of {where}",
                    fix,
                )
            )
    return out
