"""
Check the commands in runbooks and setup guides.

A runbook that says ``make deploy`` or ``npm run migrate`` or ``./scripts/rotate.sh`` is
only useful while those things exist. This module reads commands from shell code blocks
(and inline commands) and checks:

* ``make <target>``           the target is defined in a Makefile.
* ``npm|yarn|pnpm|bun run X`` the script is in some ``package.json``.
* ``./path``, ``bash x.sh``, ``python x.py``, ``node x.js``  the file exists.

Commands are never run. A check is only made when it can be answered: a repository with no
Makefile is not told that ``make test`` is wrong.
"""

from __future__ import annotations

import difflib
import json
import re
from pathlib import Path

_SHELL_FENCES = {"", "bash", "sh", "shell", "zsh", "console", "terminal", "shell-session", "text"}
_SPLIT = re.compile(r"&&|\|\||;|\|")
_PROMPT = re.compile(r"^\s*(?:[$>#]\s+)?(?:sudo\s+(?:-\w+\s+)*)?")
_MAKE_VALUE_FLAGS = {"-C", "-f", "-I", "-o", "-W", "--directory", "--file"}
_INTERPRETERS = {
    "bash": ("sh", "bash"),
    "sh": ("sh", "bash"),
    "python": ("py",),
    "python3": ("py",),
    "node": ("js", "mjs", "cjs"),
    "ruby": ("rb",),
    "source": ("sh", "bash"),
    ".": ("sh", "bash"),
}


class RunbookFacts:
    def __init__(self, idx):
        self.make_targets: set[str] = set()
        self.has_makefile = False
        self.npm_scripts: set[str] = set()
        self.has_package_json = False
        root = Path(idx.root)
        for rel in sorted(idx.files):
            name = rel.rsplit("/", 1)[-1]
            is_make = name in ("Makefile", "makefile", "GNUmakefile") or name.endswith(".mk")
            if not (is_make or name == "package.json"):
                continue
            try:
                text = (root / rel).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if is_make:
                self.has_makefile = True
                for line in text.splitlines():
                    if line.startswith(("\t", " ", "#")):
                        continue
                    m = re.match(r"^(\.PHONY\s*:\s*)?([^:=#]+?)\s*:(?!=)", line)
                    if m:
                        for target in m.group(2).split():
                            if not re.search(r"[%$]", target):
                                self.make_targets.add(target)
            else:
                try:
                    scripts = (json.loads(text) or {}).get("scripts") or {}
                except (ValueError, AttributeError):
                    continue
                self.has_package_json = True
                self.npm_scripts.update(k for k in scripts if isinstance(k, str))


def _commands(text: str, fenced: bool) -> list[str]:
    """Shell commands on a line (a code-block line, or inline code that looks like one)."""
    candidates = [text] if fenced else re.findall(r"`([^`\n]+)`", text)
    out = []
    for cand in candidates:
        for part in _SPLIT.split(cand.split("#", 1)[0] if fenced else cand):
            cmd = _PROMPT.sub("", part, count=1).strip()
            if cmd:
                out.append(cmd)
    return out


def _near(name: str, options) -> str | None:
    match = difflib.get_close_matches(name, sorted(options), n=1, cutoff=0.7)
    return match[0] if match else None


def check_runbook(lines, file, idx, linemap):
    from project_checks import CONTRADICTED, VERIFIED, ProjectFinding, _resolve_path, remap_line

    facts = RunbookFacts(idx)
    doc_dir = file.rsplit("/", 1)[0] if "/" in file else ""
    out: list[ProjectFinding] = []
    seen: set[tuple[int, str]] = set()

    def add(verdict, number, subject, message, evidence="", fix=""):
        if (number, subject) in seen:
            return
        seen.add((number, subject))
        out.append(
            ProjectFinding(
                verdict,
                "runbook",
                file,
                remap_line(linemap, number),
                subject,
                message,
                evidence,
                fix,
            )
        )

    for dl in lines:
        fenced = dl.fence is not None
        if fenced and dl.fence not in _SHELL_FENCES:
            continue
        if not fenced and dl.table_header:
            continue
        for cmd in _commands(dl.text, fenced):
            tokens = cmd.split()
            head = tokens[0]
            # ---- make
            if head == "make" and facts.has_makefile:
                flags = [t for t in tokens[1:] if t.startswith("-")]
                if any(f in _MAKE_VALUE_FLAGS or f.startswith(("-C", "-f")) for f in flags):
                    continue  # a different Makefile than the ones we read
                skip_next = False
                for tok in tokens[1:]:
                    if skip_next:
                        skip_next = False
                        continue
                    if tok == "-j":
                        skip_next = True
                        continue
                    if tok.startswith("-") or "=" in tok or re.search(r"[$<>{}\"'*]", tok):
                        continue
                    if not re.fullmatch(r"[\w./-]+", tok):
                        break
                    subject = f"`make {tok}`"
                    if tok in facts.make_targets:
                        add(VERIFIED, dl.number, subject, f"Make target `{tok}` exists.")
                    else:
                        hint = _near(tok, facts.make_targets)
                        add(
                            CONTRADICTED, dl.number, subject,
                            f"The docs run `make {tok}` but the Makefile has no `{tok}` target "
                            f"(targets: {', '.join(sorted(facts.make_targets)[:8])}).",
                            "searched the Makefile(s)",
                            f"Use `make {hint}`." if hint else "Fix the target name or add it.",
                        )  # fmt: skip
            # ---- package scripts
            elif (
                head in ("npm", "yarn", "pnpm", "bun")
                and len(tokens) >= 3
                and tokens[1] in ("run", "run-script")
                and facts.has_package_json
                and not any(t.startswith(("--prefix", "-w", "--workspace", "-C")) for t in tokens)
            ):
                script = tokens[2]
                if not re.fullmatch(r"[\w:.-]+", script):
                    continue
                subject = f"`{head} run {script}`"
                if script in facts.npm_scripts:
                    add(VERIFIED, dl.number, subject, f"Script `{script}` exists in package.json.")
                else:
                    hint = _near(script, facts.npm_scripts)
                    add(
                        CONTRADICTED, dl.number, subject,
                        f"The docs run `{head} run {script}` but no package.json defines that script.",
                        "searched package.json scripts",
                        f"Use `{head} run {hint}`." if hint else "Fix the script name or add it.",
                    )  # fmt: skip
            # ---- scripts and interpreters
            else:
                target = None
                if head.startswith("./") and len(head) > 2:
                    target = head[2:]
                elif head in _INTERPRETERS and len(tokens) > 1:
                    nxt = next((t for t in tokens[1:] if not t.startswith("-")), "")
                    if nxt.rsplit(".", 1)[-1] in _INTERPRETERS[head] and "." in nxt:
                        target = nxt
                if not target or re.search(r"[$<>{}*~\"'`=]", target) or target.startswith("/"):
                    continue
                found = _resolve_path(target, doc_dir, idx)
                subject = f"`{target}`"
                if found:
                    add(VERIFIED, dl.number, subject, f"The script {subject} exists.", found)
                else:
                    add(
                        CONTRADICTED, dl.number, subject,
                        f"The docs run {subject} but that file does not exist in the repository.",
                        "searched the repository file list",
                        "Fix the path or remove the step.",
                    )  # fmt: skip
    return out
