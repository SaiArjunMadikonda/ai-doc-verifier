"""
Documentation health score.

Turns verifier findings into a 0-100 score per doc file and overall, and keeps an
optional JSON history so a trend can be shown over time.

Score = 100 * (verified + 0.5 * uncertain) / total checks. A contradicted check
counts as zero; an uncertain one counts as half because it is not proven wrong.
Files with no checks have no score (``None``) rather than a misleading 100.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

_BLOCKS = "▁▂▃▄▅▆▇█"


def finding_file(finding) -> str:
    """File a finding belongs to (works for API findings and project findings)."""
    claim = getattr(finding, "claim", None)
    return getattr(claim, "file", None) or getattr(finding, "file", "") or "(unknown)"


@dataclass
class FileHealth:
    verified: int = 0
    contradicted: int = 0
    uncertain: int = 0

    @property
    def total(self) -> int:
        return self.verified + self.contradicted + self.uncertain

    @property
    def score(self) -> float | None:
        if not self.total:
            return None
        return round(100 * (self.verified + 0.5 * self.uncertain) / self.total, 1)


@dataclass
class Health:
    overall: FileHealth = field(default_factory=FileHealth)
    per_file: dict[str, FileHealth] = field(default_factory=dict)

    @property
    def score(self) -> float | None:
        return self.overall.score

    def worst_files(self, limit: int = 5) -> list[tuple[str, FileHealth]]:
        scored = [(f, h) for f, h in self.per_file.items() if h.score is not None]
        return sorted(scored, key=lambda item: (item[1].score, item[0]))[:limit]

    def to_dict(self) -> dict:
        return {
            "score": self.score,
            "verified": self.overall.verified,
            "contradicted": self.overall.contradicted,
            "uncertain": self.overall.uncertain,
            "files": {
                name: {
                    "score": h.score,
                    "verified": h.verified,
                    "contradicted": h.contradicted,
                    "uncertain": h.uncertain,
                }
                for name, h in sorted(self.per_file.items())
            },
        }


def compute_health(findings) -> Health:
    health = Health()
    for f in findings:
        entry = health.per_file.setdefault(finding_file(f), FileHealth())
        verdict = f.verdict
        if verdict not in ("verified", "contradicted", "uncertain"):
            continue
        for target in (entry, health.overall):
            setattr(target, verdict, getattr(target, verdict) + 1)
    return health


# ----------------------------------------------------------------------------- history


def load_history(path: str | Path) -> list[dict]:
    p = Path(path)
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return (
        [e for e in data if isinstance(e, dict) and "score" in e] if isinstance(data, list) else []
    )


def append_history(
    path: str | Path, health: Health, when: datetime | None = None, label: str = "", keep: int = 100
) -> list[dict]:
    """Append the current score to the history file and return the new history."""
    history = load_history(path)
    when = when or datetime.now(UTC)
    history.append(
        {
            "time": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "label": label,
            "score": health.score,
            "verified": health.overall.verified,
            "contradicted": health.overall.contradicted,
            "uncertain": health.overall.uncertain,
        }
    )
    history = history[-keep:]
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(history, indent=2) + "\n", encoding="utf-8")
    return history


def sparkline(values: list[float | None]) -> str:
    nums = [v for v in values if v is not None]
    if not nums:
        return ""
    lo, hi = min(nums), max(nums)
    span = hi - lo
    out = []
    for v in values:
        if v is None:
            out.append(" ")
        elif span == 0:
            out.append(_BLOCKS[-1])
        else:
            out.append(_BLOCKS[round((v - lo) / span * (len(_BLOCKS) - 1))])
    return "".join(out)


def render_health(health: Health, history: list[dict] | None = None, worst: int = 3) -> list[str]:
    """Markdown lines for the report."""
    if health.score is None:
        return []
    lines = [f"**Documentation health: {health.score:g} / 100**"]
    if history and len(history) > 1:
        scores = [e.get("score") for e in history[-20:]]
        previous = history[-2].get("score")
        delta = ""
        if previous is not None and health.score is not None:
            diff = round(health.score - previous, 1)
            delta = f" ({'+' if diff >= 0 else ''}{diff:g} since last run)"
        lines[0] += delta
        lines.append(f"Trend: `{sparkline(scores)}`")
    weak = [(n, h) for n, h in health.worst_files(worst) if h.score is not None and h.score < 100]
    if weak:
        lines.append("")
        lines.append("Needs the most attention:")
        for name, h in weak:
            lines.append(
                f"- `{name}`: {h.score:g}/100 ({h.contradicted} contradicted, {h.uncertain} uncertain)"
            )
    lines.append("")
    return lines
