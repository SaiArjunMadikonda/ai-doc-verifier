"""
Score the documentation checkers against the hand-labeled cases.

    python -m evaluation.run_eval            # print a report
    python -m evaluation.run_eval --json     # machine-readable

Metrics, per verdict:

* **precision** - of the findings the checker reported with this verdict, how many
  were right?  (Low precision = false alarms.)
* **recall** - of the claims that really deserve this verdict, how many did it find?
  (Low recall = missed mistakes.)

``contradicted`` is the headline number: it is the verdict users act on.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from api_contract import CONTRADICTED, UNCERTAIN, VERIFIED  # noqa: E402
from docaudit import audit  # noqa: E402
from evaluation.cases import ALL_CASES, Case  # noqa: E402

VERDICTS = (VERIFIED, CONTRADICTED, UNCERTAIN)


@dataclass
class Tally:
    tp: int = 0
    fp: int = 0
    fn: int = 0

    @property
    def precision(self) -> float | None:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else None

    @property
    def recall(self) -> float | None:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else None


@dataclass
class CaseResult:
    name: str
    problems: list[str] = field(default_factory=list)
    tallies: dict[str, Tally] = field(default_factory=lambda: {v: Tally() for v in VERDICTS})


def _norm(f):
    """(check, message, verdict) for either API or project findings."""
    return getattr(f, "check", ""), f.message, f.verdict


def run_case(case: Case) -> CaseResult:
    result = CaseResult(case.name)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for rel, text in {**case.code, **{f"docs/{k}": v for k, v in case.docs.items()}}.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        spec_path = None
        if case.spec is not None:
            spec_path = str(root / "openapi.json")
            Path(spec_path).write_text(json.dumps(case.spec), encoding="utf-8")
        outcome = audit(
            root,
            [root / "docs"],
            spec_path=spec_path,
            code_path=str(root) if case.spec is None and case.code and case.run == "api" else None,
            run_api=case.run == "api",
            run_project=case.run == "project",
        )
        actual = [_norm(f) for f in outcome.findings]
    used: set[int] = set()
    for check, needle, verdict in case.expected:
        hits = [
            i for i, (c, m, _) in enumerate(actual) if c == check and needle.lower() in m.lower()
        ]
        exact = [i for i in hits if actual[i][2] == verdict]
        if exact:
            result.tallies[verdict].tp += 1
            used.add(exact[0])
            continue
        result.tallies[verdict].fn += 1
        if hits:
            wrong = actual[hits[0]]
            result.tallies[wrong[2]].fp += 1
            used.add(hits[0])
            result.problems.append(
                f"expected {verdict} for [{check}] '{needle}' but got {wrong[2]}: {wrong[1]}"
            )
        else:
            result.problems.append(f"missed {verdict} [{check}] '{needle}'")
    for check, needle in case.never_verified:
        for c, m, v in actual:
            if v == VERIFIED and c == check and needle.lower() in m.lower():
                result.tallies[VERIFIED].fp += 1
                result.problems.append(f"false confirmation [{check}] '{needle}': {m}")
    for i, (check, message, verdict) in enumerate(actual):
        if i in used or verdict == VERIFIED:
            continue  # unlabeled verified findings are not judged
        result.tallies[verdict].fp += 1
        result.problems.append(f"unexpected {verdict} [{check}]: {message}")
    return result


@dataclass
class Evaluation:
    cases: list[CaseResult]

    @property
    def totals(self) -> dict[str, Tally]:
        total = {v: Tally() for v in VERDICTS}
        for c in self.cases:
            for v in VERDICTS:
                total[v].tp += c.tallies[v].tp
                total[v].fp += c.tallies[v].fp
                total[v].fn += c.tallies[v].fn
        return total

    @property
    def problems(self) -> list[tuple[str, str]]:
        return [(c.name, p) for c in self.cases for p in c.problems]

    def to_dict(self) -> dict:
        return {
            "cases": len(self.cases),
            "verdicts": {
                v: {
                    "tp": t.tp,
                    "fp": t.fp,
                    "fn": t.fn,
                    "precision": t.precision,
                    "recall": t.recall,
                }
                for v, t in self.totals.items()
            },
            "problems": [{"case": n, "problem": p} for n, p in self.problems],
        }


def evaluate(cases: list[Case] | None = None) -> Evaluation:
    return Evaluation([run_case(c) for c in (cases if cases is not None else ALL_CASES)])


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{100 * x:.0f}%"


def render(ev: Evaluation) -> str:
    lines = [f"Evaluated {len(ev.cases)} hand-labeled cases", ""]
    lines.append(f"{'verdict':<14}{'precision':>10}{'recall':>9}{'TP':>5}{'FP':>5}{'FN':>5}")
    for v, t in ev.totals.items():
        lines.append(
            f"{v:<14}{_pct(t.precision):>10}{_pct(t.recall):>9}{t.tp:>5}{t.fp:>5}{t.fn:>5}"
        )
    if ev.problems:
        lines += ["", "Disagreements with the labels:"]
        lines += [f"- [{name}] {p}" for name, p in ev.problems]
    else:
        lines += ["", "Every finding matches the labels."]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    ev = evaluate()
    print(json.dumps(ev.to_dict(), indent=2) if args.json else render(ev))
    return 0 if not ev.problems else 1


if __name__ == "__main__":
    sys.exit(main())
