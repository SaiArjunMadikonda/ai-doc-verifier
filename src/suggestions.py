"""
Optional AI-written rewrites for contradicted documentation.

The verifier already attaches a deterministic ``fix`` to each contradiction. This
module can additionally ask an OpenAI-compatible model to rewrite the affected
doc snippet so it matches the spec evidence. The model never decides *whether*
something is wrong - that stays deterministic - it only drafts the wording, and
the result is always labelled as AI-generated for a human to review.
"""

from __future__ import annotations

import re

SYSTEM_PROMPT = (
    "You fix technical documentation so that it matches facts taken from an API "
    "specification. You are given a problem that was already verified against the "
    "spec, a suggested fix, and the documentation snippet. Rewrite ONLY the part of "
    "the snippet that is wrong, keep the original style and formatting, and do not "
    "invent facts that are not in the problem or the fix. Reply with the corrected "
    "text only, no explanations."
)

MAX_AI_FIX_CHARS = 600


def snippet_around(text: str, line: int, before: int = 2, after: int = 8) -> str:
    """Return the doc lines around a 1-based line number."""
    lines = text.splitlines()
    start = max(0, line - 1 - before)
    end = min(len(lines), line + after)
    return "\n".join(lines[start:end])


def build_prompt(finding, snippet: str) -> str:
    return (
        f"Problem (verified against the spec): {finding.message}\n"
        f"Evidence: {finding.evidence}\n"
        f"Suggested fix: {finding.fix or 'none'}\n\n"
        f"Documentation snippet from {finding.claim.file}:{finding.claim.line}:\n"
        f"---\n{snippet}\n---"
    )


def clean_ai_text(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", text).strip()
    text = " ".join(text.split())
    if len(text) > MAX_AI_FIX_CHARS:
        text = text[: MAX_AI_FIX_CHARS - 1].rstrip() + "…"
    return text


def refine_fixes(report, client, model: str, read_text, max_items: int = 10) -> int:
    """Fill ``ai_fix`` on up to *max_items* contradicted findings.

    ``read_text(path) -> str`` returns the documentation file's text. Errors on one
    finding never stop the others; returns how many rewrites were produced.
    """
    done = 0
    for finding in report.findings:
        if done >= max_items:
            break
        if finding.verdict != "contradicted" or not finding.fix:
            continue
        try:
            text = read_text(finding.claim.file)
            snippet = snippet_around(text, finding.claim.line)
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": build_prompt(finding, snippet)},
                ],
            )
            finding.ai_fix = clean_ai_text(response.choices[0].message.content)
        except Exception as e:  # noqa: BLE001 - a failed rewrite must never break the report
            print(f"AI rewrite skipped for {finding.claim.label}: {type(e).__name__}")
            continue
        if finding.ai_fix:
            done += 1
    return done
