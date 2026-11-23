"""
Breaking-change detector for APIs, with the documentation it would affect.

Compares the API at two git refs (or one ref and the working tree), then tells the author
which changes would break existing clients and which **documented** endpoints are touched.

    python src/api_diff.py origin/main HEAD --code . docs
    python src/api_diff.py v1.4.0 --code .            # v1.4.0 against the working tree

An API definition is read the same way as everywhere else: an OpenAPI file in the
repository, or routes read statically from the code. Nothing is executed. Old refs are
unpacked into a temporary folder with ``git archive``; the working tree is not touched.
"""

from __future__ import annotations

import argparse
import difflib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from api_contract import (
    Spec,
    SpecError,
    collect_doc_files,
    extract_claims,
    normalize_path,
    resolve_spec,
)
from doc_formats import kind_of, remap_line, to_markdown

BREAKING = "breaking"
WARNING = "warning"
INFO = "info"
SEVERITY_ORDER = {BREAKING: 0, WARNING: 1, INFO: 2}


class RefError(RuntimeError):
    pass


# ----------------------------------------------------------------------------- reading a ref


def _safe_extract(data: bytes, dest: Path) -> None:
    """Unpack a git archive, refusing anything that would land outside *dest*."""
    dest_r = dest.resolve()
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        for member in tar.getmembers():
            target = (dest / member.name).resolve()
            if dest_r != target and dest_r not in target.parents:
                raise RefError(f"Unsafe path in archive: {member.name}")
            if member.issym() or member.islnk() or member.isdev():
                continue  # links and devices are never needed to read an API definition
            kwargs = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
            tar.extract(member, dest, set_attrs=False, **kwargs)


def export_ref(root: str | os.PathLike, ref: str, dest: str | os.PathLike) -> None:
    try:
        proc = subprocess.run(
            ["git", "archive", "--format=tar", ref],
            cwd=str(root),
            capture_output=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RefError(f"Could not run git: {e}") from e
    if proc.returncode != 0:
        raise RefError(
            f"git archive {ref} failed: {proc.stderr.decode('utf-8', 'replace').strip()[:300]}"
        )
    _safe_extract(proc.stdout, Path(dest))


def _under(base: Path, path: str | None) -> str | None:
    if not path:
        return None
    p = Path(path)
    return str(p if p.is_absolute() else base / p)


def _relative(root: Path, path: str | None) -> str | None:
    """Make a path inside the repository relative, so it can be applied to any ref."""
    if not path:
        return None
    p = Path(path)
    if p.is_absolute():
        try:
            return p.resolve().relative_to(root).as_posix() or "."
        except ValueError:
            return str(p)
    return path


def load_api_at(
    root: str | os.PathLike,
    ref: str | None,
    spec_path: str | None = None,
    code_path: str | None = None,
) -> Spec:
    """The API definition at *ref*, or in the working tree when *ref* is ``None``."""
    root_p = Path(root).resolve()
    spec_path, code_path = (_relative(root_p, p) for p in (spec_path, code_path))
    if ref is None:
        return resolve_spec(_under(root_p, spec_path), _under(root_p, code_path), None, root_p)
    with tempfile.TemporaryDirectory(prefix="api_diff_") as tmp:
        tmp_p = Path(tmp)
        export_ref(root_p, ref, tmp_p)
        return resolve_spec(_under(tmp_p, spec_path), _under(tmp_p, code_path), None, tmp_p)


# ----------------------------------------------------------------------------- diffing


@dataclass
class Change:
    severity: str
    kind: str  # removed | added | deprecated | param-removed | param-required | param-type | ...
    method: str
    path: str
    message: str
    detail: str = ""  # e.g. the parameter name

    @property
    def key(self) -> tuple[str, str]:
        return (self.method, normalize_path(self.path))

    def to_dict(self) -> dict:
        return {
            "severity": self.severity,
            "kind": self.kind,
            "method": self.method,
            "path": self.path,
            "message": self.message,
            "detail": self.detail,
        }


def _label(op) -> str:
    return f"`{op.method} {op.path}`"


def _is_success(code: str) -> bool:
    return code.startswith("2")


def diff_specs(old: Spec, new: Spec) -> list[Change]:
    changes: list[Change] = []
    removed = {k: v for k, v in old.operations.items() if k not in new.operations}
    added = {k: v for k, v in new.operations.items() if k not in old.operations}
    for key, op in sorted(removed.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        # an endpoint that vanished next to a similar new one was probably renamed
        same_method_added = [(p, o) for (m, p), o in added.items() if m == key[0]]
        only_pair = len(same_method_added) == 1 and (
            sum(1 for (m, _p) in removed if m == key[0]) == 1
        )
        similar = [
            new_op
            for p, new_op in same_method_added
            if only_pair
            or difflib.SequenceMatcher(None, key[1], p).ratio() >= 0.6
            or key[1].rsplit("/", 1)[-1] == p.rsplit("/", 1)[-1]
        ]
        hint = (
            f" It may have been renamed to `{similar[0].method} {similar[0].path}`."
            if similar
            else ""
        )
        changes.append(
            Change(BREAKING, "removed", op.method, op.path, f"{_label(op)} was removed.{hint}")
        )
    for _key, op in sorted(added.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        changes.append(Change(INFO, "added", op.method, op.path, f"{_label(op)} was added."))
    for key, new_op in sorted(new.operations.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        old_op = old.operations.get(key)
        if old_op is None:
            continue
        label = _label(new_op)
        if new_op.deprecated and not old_op.deprecated:
            changes.append(
                Change(WARNING, "deprecated", new_op.method, new_op.path,
                       f"{label} is now marked deprecated.")
            )  # fmt: skip
        old_params = {(p.name, p.location): p for p in old_op.params}
        new_params = {(p.name, p.location): p for p in new_op.params}
        for (name, loc), p in sorted(new_params.items()):
            before = old_params.get((name, loc))
            if before is None:
                if p.required:
                    changes.append(
                        Change(BREAKING, "param-required", new_op.method, new_op.path,
                               f"{label} has a new required {loc} parameter `{name}`.", name)
                    )  # fmt: skip
                continue
            if p.required and not before.required:
                changes.append(
                    Change(BREAKING, "param-required", new_op.method, new_op.path,
                           f"{label}: {loc} parameter `{name}` is now required.", name)
                )  # fmt: skip
            if (
                before.types
                and p.types
                and not before.types_unresolved
                and not p.types_unresolved
                and before.types != p.types
            ):
                changes.append(
                    Change(BREAKING, "param-type", new_op.method, new_op.path,
                           f"{label}: `{name}` changed type from {'/'.join(sorted(before.types))} "
                           f"to {'/'.join(sorted(p.types))}.", name)
                )  # fmt: skip
        for name, loc in sorted(old_params):
            if (name, loc) not in new_params:
                changes.append(
                    Change(WARNING, "param-removed", new_op.method, new_op.path,
                           f"{label}: {loc} parameter `{name}` was removed; clients that send it "
                           "may now be ignored or rejected.", name)
                )  # fmt: skip
        for code in sorted(old_op.responses - new_op.responses):
            sev = BREAKING if _is_success(code) else WARNING
            changes.append(
                Change(sev, "response-removed", new_op.method, new_op.path,
                       f"{label} no longer returns `{code}`.", code)
            )  # fmt: skip
        for code in sorted(new_op.responses - old_op.responses):
            changes.append(
                Change(INFO, "response-added", new_op.method, new_op.path,
                       f"{label} can now return `{code}`.", code)
            )  # fmt: skip
    changes.sort(key=lambda c: (SEVERITY_ORDER[c.severity], c.path, c.method, c.kind, c.detail))
    return changes


# ----------------------------------------------------------------------------- docs impact


@dataclass
class DocImpact:
    change: Change
    file: str
    line: int

    def to_dict(self) -> dict:
        return {"change": self.change.to_dict(), "file": self.file, "line": self.line}


def documented_claims(doc_paths, root) -> list:
    root_p = Path(root).resolve()
    claims = []
    for f in collect_doc_files(list(doc_paths)):
        try:
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            rel = f.resolve().relative_to(root_p).as_posix()
        except ValueError:
            rel = str(f)
        markdown, linemap = to_markdown(text, kind_of(f))
        for c in extract_claims(markdown, rel):
            c.line = remap_line(linemap, c.line)
            claims.append(c)
    return claims


def docs_impact(changes: list[Change], claims: list) -> list[DocImpact]:
    by_key: dict[tuple[str, str], list[Change]] = {}
    for ch in changes:
        if ch.severity != INFO:
            by_key.setdefault(ch.key, []).append(ch)
    out = []
    for claim in claims:
        for ch in by_key.get((claim.method.upper(), claim.path), []):
            out.append(DocImpact(ch, claim.file, claim.line))
    return out


@dataclass
class DiffReport:
    old_label: str
    new_label: str
    changes: list[Change] = field(default_factory=list)
    impacts: list[DocImpact] = field(default_factory=list)

    def count(self, severity: str) -> int:
        return sum(1 for c in self.changes if c.severity == severity)

    @property
    def breaking(self) -> list[Change]:
        return [c for c in self.changes if c.severity == BREAKING]

    @property
    def documented_breaking(self) -> list[DocImpact]:
        return [i for i in self.impacts if i.change.severity == BREAKING]

    def to_dict(self) -> dict:
        return {
            "old": self.old_label,
            "new": self.new_label,
            "counts": {s: self.count(s) for s in (BREAKING, WARNING, INFO)},
            "changes": [c.to_dict() for c in self.changes],
            "docs_impact": [i.to_dict() for i in self.impacts],
        }


def compare(
    root: str | os.PathLike,
    old_ref: str,
    new_ref: str | None = None,
    doc_paths: list | None = None,
    spec_path: str | None = None,
    code_path: str | None = None,
) -> DiffReport:
    """Compare the API at *old_ref* with *new_ref* (default: the working tree)."""
    old = load_api_at(root, old_ref, spec_path, code_path)
    new = load_api_at(root, new_ref, spec_path, code_path)
    report = DiffReport(old_ref, new_ref or "working tree", diff_specs(old, new))
    docs = doc_paths if doc_paths else [Path(root)]
    report.impacts = docs_impact(report.changes, documented_claims(docs, root))
    return report


# ----------------------------------------------------------------------------- report

_ICON = {BREAKING: "💥", WARNING: "⚠️", INFO: "➕"}


def render_diff_markdown(report: DiffReport, max_items: int = 40) -> str:
    out = [f"## 🔀 API changes: `{report.old_label}` → `{report.new_label}`", ""]
    if not report.changes:
        return "\n".join([*out, "No API changes between these versions. ✅", ""])
    out += [
        "| Kind | Changes |",
        "|---|---|",
        f"| 💥 Breaking for clients | {report.count(BREAKING)} |",
        f"| ⚠️ Worth a note | {report.count(WARNING)} |",
        f"| ➕ Additions | {report.count(INFO)} |",
        "",
    ]
    where: dict[tuple, list[DocImpact]] = {}
    for i in report.impacts:
        where.setdefault((i.change.key, i.change.kind, i.change.detail), []).append(i)
    for severity, title in (
        (BREAKING, "💥 Breaking changes"),
        (WARNING, "⚠️ Changes to mention"),
    ):
        items = [c for c in report.changes if c.severity == severity]
        if not items:
            continue
        out += [f"### {title}", ""]
        for c in items[:max_items]:
            hits = where.get((c.key, c.kind, c.detail), [])
            out.append(f"- {c.message}")
            if hits:
                places = ", ".join(f"`{h.file}:{h.line}`" for h in hits[:4])
                out.append(f"  - Documented at {places}: update these pages with the change.")
            else:
                out.append("  - Not described in the docs you checked.")
        if len(items) > max_items:
            out.append(f"- …and {len(items) - max_items} more")
        out.append("")
    added = [c for c in report.changes if c.severity == INFO]
    if added:
        out += ["<details><summary>➕ Additions</summary>", ""]
        out += [f"- {c.message}" for c in added[:max_items]]
        out += ["", "</details>", ""]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Find breaking API changes between two git refs.")
    parser.add_argument("old", help="Older git ref (tag, branch or commit)")
    parser.add_argument("new", nargs="?", help="Newer ref (default: the working tree)")
    parser.add_argument("docs", nargs="*", help="Docs files or folders to check for impact")
    parser.add_argument("--root", default=".", help="Git repository (default: current folder)")
    parser.add_argument("--spec", help="Path of the OpenAPI file inside the repository")
    parser.add_argument("--code", help="Folder with API code inside the repository")
    parser.add_argument("--json", action="store_true", help="Print JSON")
    parser.add_argument(
        "--fail-on-breaking", action="store_true", help="Exit 1 when there are breaking changes"
    )
    args = parser.parse_args(argv)
    try:
        report = compare(args.root, args.old, args.new, args.docs or None, args.spec, args.code)
    except (RefError, SpecError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    print(json.dumps(report.to_dict(), indent=2) if args.json else render_diff_markdown(report))
    return 1 if args.fail_on_breaking and report.breaking else 0


if __name__ == "__main__":
    sys.exit(main())
