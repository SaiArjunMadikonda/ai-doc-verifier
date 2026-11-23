"""
Baseline and ignore rules for documentation audits.

A team adopting the checker usually has old documentation problems it cannot fix at once.
A *baseline* records today's findings so the build only fails on **new** ones, and an
*ignore file* lists findings that are deliberate. Findings are matched by a fingerprint
that does not include the line number, so editing the docs above a finding does not make
it look new.

Files (both JSON):

* ``.docaudit-baseline.json`` – written by ``docaudit --update-baseline``.
* ``.docaudit.json`` – hand-written::

      {"ignore": [{"file": "docs/legacy/*", "check": "reference"},
                  {"subject": "`LEGACY_TOKEN`"}]}

  A rule matches when every key it names matches (``file`` is a glob; the rest compare
  exactly, case-insensitively).
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
from pathlib import Path

BASELINE_VERSION = 1
RULE_KEYS = ("file", "check", "subject", "verdict", "message")


def describe(finding) -> dict[str, str]:
    """The identifying fields of any finding (API, project or meeting)."""
    claim = getattr(finding, "claim", None)
    if claim is not None:
        file = claim.file
        subject = claim.label
        line = claim.line
    else:
        file = getattr(finding, "file", "") or ""
        subject = getattr(finding, "subject", "") or ""
        line = getattr(finding, "line", 0)
    return {
        "file": file,
        "check": getattr(finding, "check", "") or "",
        "subject": subject,
        "verdict": getattr(finding, "verdict", ""),
        "message": getattr(finding, "message", "") or "",
        "line": str(line),
    }


def fingerprint(finding) -> str:
    """Stable id for a finding: file, check, subject and message, but not the line."""
    d = describe(finding)
    raw = "\x1f".join(d[k].strip().lower() for k in ("file", "check", "subject", "message"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


# ----------------------------------------------------------------------------- baseline


def load_baseline(path: str | Path | None) -> set[str]:
    """Fingerprints stored in *path* (empty when the file does not exist)."""
    if not path:
        return set()
    p = Path(path)
    if not p.is_file():
        return set()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ValueError(f"Cannot read baseline {p}: {e}") from e
    entries = data.get("findings") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise ValueError(f"Baseline {p} has no 'findings' list.")
    return {str(e["id"]) for e in entries if isinstance(e, dict) and "id" in e}


def write_baseline(path: str | Path, findings: list, only_verdicts=("contradicted",)) -> int:
    """Record the current problems; returns how many were written."""
    entries = []
    seen: set[str] = set()
    for f in findings:
        if f.verdict not in only_verdicts:
            continue
        fid = fingerprint(f)
        if fid in seen:
            continue
        seen.add(fid)
        d = describe(f)
        entries.append(
            {
                "id": fid,
                "file": d["file"],
                "check": d["check"],
                "subject": d["subject"],
                "message": d["message"],
            }
        )
    entries.sort(key=lambda e: (e["file"], e["check"], e["subject"], e["id"]))
    Path(path).write_text(
        json.dumps({"version": BASELINE_VERSION, "findings": entries}, indent=2) + "\n",
        encoding="utf-8",
    )
    return len(entries)


# ----------------------------------------------------------------------------- ignore rules


def load_ignore_rules(path: str | Path | None) -> list[dict[str, str]]:
    if not path:
        return []
    p = Path(path)
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ValueError(f"Cannot read ignore file {p}: {e}") from e
    rules = data.get("ignore") if isinstance(data, dict) else None
    if rules is None:
        return []
    if not isinstance(rules, list):
        raise ValueError(f"'ignore' in {p} must be a list of rules.")
    out = []
    for rule in rules:
        if not isinstance(rule, dict) or not any(k in rule for k in RULE_KEYS):
            raise ValueError(f"Bad ignore rule in {p}: {rule!r}")
        out.append({k: str(rule[k]) for k in RULE_KEYS if k in rule})
    return out


def is_ignored(finding, rules: list[dict[str, str]]) -> bool:
    if not rules:
        return False
    d = describe(finding)
    for rule in rules:
        ok = True
        for key, want in rule.items():
            have = d[key]
            if key == "file":
                ok = fnmatch.fnmatch(have, want)
            else:
                ok = have.strip().lower() == want.strip().lower()
            if not ok:
                break
        if ok:
            return True
    return False
