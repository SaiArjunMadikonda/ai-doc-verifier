"""
SARIF 2.1.0 output, so GitHub shows documentation findings in its Code Scanning tab.

Contradicted findings become ``error`` results and uncertain ones ``note``. Verified
findings are left out; SARIF lists problems, not passes.
"""

from __future__ import annotations

import json

from baseline import describe, fingerprint

SCHEMA = "https://json.schemastore.org/sarif-2.1.0.json"
LEVELS = {"contradicted": "error", "uncertain": "note"}


def _line(finding) -> int:
    claim = getattr(finding, "claim", None)
    line = claim.line if claim is not None else getattr(finding, "line", 1)
    return max(int(line or 1), 1)


def to_sarif(findings: list, tool_version: str = "1.0") -> dict:
    rules: dict[str, dict] = {}
    results = []
    for f in findings:
        level = LEVELS.get(f.verdict)
        if level is None:
            continue
        d = describe(f)
        check = d["check"] or "docs"
        rule_id = f"docaudit/{check}"
        rules.setdefault(
            rule_id,
            {
                "id": rule_id,
                "name": f"{check}-check",
                "shortDescription": {"text": f"Documentation {check} check"},
            },
        )
        text = d["message"]
        evidence = getattr(f, "evidence", "")
        fix = getattr(f, "fix", "")
        if evidence:
            text += f" Evidence: {evidence}."
        if fix:
            text += f" Suggested fix: {fix}"
        results.append(
            {
                "ruleId": rule_id,
                "level": level,
                "message": {"text": text},
                "locations": [
                    {
                        "physicalLocation": {
                            "artifactLocation": {"uri": d["file"] or "unknown"},
                            "region": {"startLine": _line(f)},
                        }
                    }
                ],
                "partialFingerprints": {"docauditFingerprint/v1": fingerprint(f)},
            }
        )
    return {
        "$schema": SCHEMA,
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "docaudit",
                        "version": tool_version,
                        "rules": list(rules.values()),
                    }
                },
                "results": results,
            }
        ],
    }


def dumps(findings: list) -> str:
    return json.dumps(to_sarif(findings), indent=2)
