"""
Check what the docs say about the *project itself* against the repository.

Four checks, all deterministic (no AI, no code is executed):

* **Environment variables** - ``export FOO=...`` / "set ``FOO``" in the docs versus
  ``os.environ`` / ``os.getenv`` / ``process.env`` / ``.env.example`` in the code.
* **CLI flags** - ``--flag`` mentioned in the docs versus argparse / click / typer.
* **References** - file paths, relative links and ``function()`` names mentioned in
  the docs versus what exists in the repository.
* **Version claims** - "Python 3.9+" / "Node 18+" versus ``requires-python`` /
  ``engines.node``.

A name that cannot be found is only *contradicted* when there is real evidence (the
code reads other variables, a close spelling exists, ...). Otherwise it is
*uncertain*, because it may come from a dependency or a framework.
"""

from __future__ import annotations

import ast
import builtins
import difflib
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from api_contract import (
    _SKIP_DIRS,
    CONTRADICTED,
    UNCERTAIN,
    VERIFIED,
    collect_doc_files,
)
from doc_formats import kind_of, remap_line, to_markdown
from health import compute_health

MAX_CODE_BYTES = 400_000
CODE_EXTENSIONS = (".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx")

CHECK_NAMES = ("env", "flag", "reference", "version", "infra", "link", "runbook")


@dataclass
class ProjectFinding:
    verdict: str
    check: str  # env | flag | reference | version | infra | link | runbook
    file: str
    line: int
    subject: str  # what the docs claim, e.g. "`API_KEY`"
    message: str
    evidence: str = ""
    fix: str = ""

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "check": self.check,
            "file": self.file,
            "line": self.line,
            "subject": self.subject,
            "message": self.message,
            "evidence": self.evidence,
            "fix": self.fix,
        }


@dataclass
class ProjectReport:
    findings: list[ProjectFinding] = field(default_factory=list)
    root: str = "."
    docs_checked: int = 0

    @property
    def health(self):
        return compute_health(self.findings)

    def counts(self) -> dict[str, int]:
        out = {VERIFIED: 0, CONTRADICTED: 0, UNCERTAIN: 0}
        for f in self.findings:
            out[f.verdict] += 1
        return out

    def to_dict(self) -> dict:
        return {
            "docs_checked": self.docs_checked,
            "counts": self.counts(),
            "health": self.health.to_dict(),
            "findings": [f.to_dict() for f in self.findings],
        }


# ----------------------------------------------------------------------------- doc scanning


@dataclass
class DocLine:
    number: int  # 1-based, in the converted markdown
    text: str
    fence: str | None  # language of the enclosing fenced block, None for prose
    table_header: str = ""  # lower-cased header row when inside a table


def iter_doc_lines(markdown: str) -> list[DocLine]:
    out: list[DocLine] = []
    fence_marker: str | None = None
    fence_lang = ""
    header = ""
    prev_was_table = False
    for i, line in enumerate(markdown.splitlines(), start=1):
        fm = re.match(r"^\s*(```|~~~)\s*([\w+.-]*)", line)
        if fence_marker is None and fm:
            fence_marker, fence_lang = fm.group(1), fm.group(2).lower()
            continue
        if fence_marker is not None:
            if line.lstrip().startswith(fence_marker):
                fence_marker = None
            else:
                out.append(DocLine(i, line, fence_lang))
            continue
        if line.lstrip().startswith("|"):
            if not prev_was_table:
                header = line.lower()
            prev_was_table = True
            out.append(DocLine(i, line, None, header))
        else:
            prev_was_table = False
            header = ""
            out.append(DocLine(i, line, None))
    return out


_CODE_SPAN = re.compile(r"`([^`\n]+)`")


# ----------------------------------------------------------------------------- repo scanning


@dataclass
class Location:
    file: str
    line: int

    def __str__(self) -> str:
        return f"{self.file}:{self.line}"


@dataclass
class RepoIndex:
    root: Path
    files: set[str] = field(default_factory=set)  # every non-skipped file, posix, relative
    basenames: dict[str, str] = field(default_factory=dict)
    env_vars: dict[str, Location] = field(default_factory=dict)
    env_reads_by_framework: bool = False  # pydantic settings etc: names may be implicit
    flags: dict[str, Location] = field(default_factory=dict)
    flags_implicit: bool = False  # typer/fire: some flags come from function signatures
    cli_names: set[str] = field(default_factory=set)
    defined: dict[str, Location] = field(default_factory=dict)  # function/class/method names
    imported: set[str] = field(default_factory=set)
    modules: set[str] = field(default_factory=set)
    python_files: int = 0
    # files of a separate docs repository (relative to it), for resolving links between docs
    doc_files: set[str] = field(default_factory=set)
    requires_python: tuple[str, Location] | None = None
    engines_node: tuple[str, Location] | None = None
    infra: object | None = None  # infra_checks.InfraIndex, built by verify_project
    docs_root: Path | None = None  # set when the docs live in a separate repository


_ENV_CALL = re.compile(
    r"""(?:os\.environ(?:\.get|\.setdefault|\.pop)?|environ\.get|getenv|get_env|env|config|
        process\.env|Env|env\.\w+)\s*[\(\[]\s*['"]([A-Z][A-Z0-9_]{1,})['"]""",
    re.X,
)
_PROCESS_ENV = re.compile(r"process\.env\.([A-Z][A-Z0-9_]{1,})")
_DOTENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]{1,})\s*=", re.M)
_FLAG_STRING = re.compile(r"""['"](--[A-Za-z][\w-]*)(?:[=\s][^'"]*)?['"]""")


def _skip_dir(name: str) -> bool:
    return name in _SKIP_DIRS or name.startswith(".git") and name != ".github"


def index_repo(
    root: str | os.PathLike, exclude: list[str | os.PathLike] | None = None
) -> RepoIndex:
    root_p = Path(root).resolve()
    idx = RepoIndex(root_p)
    skipped = {Path(e).resolve() for e in exclude or []}
    for dirpath, dirs, names in os.walk(root_p):
        dirs[:] = sorted(
            d for d in dirs if not _skip_dir(d) and (Path(dirpath) / d).resolve() not in skipped
        )
        for name in sorted(names):
            path = Path(dirpath) / name
            rel = path.relative_to(root_p).as_posix()
            idx.files.add(rel)
            idx.basenames.setdefault(name, rel)
            suffix = path.suffix.lower()
            if name in ("pyproject.toml", "setup.cfg", "package.json"):
                _read_manifest(idx, path, rel)
            if name.startswith(".env") and name.lower().endswith(
                (".example", ".sample", ".template", "env")
            ):
                _read_dotenv(idx, path, rel)
            if suffix in CODE_EXTENSIONS:
                try:
                    if path.stat().st_size > MAX_CODE_BYTES:
                        continue
                    text = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                if suffix == ".py":
                    idx.python_files += 1
                    idx.modules.add(path.stem)
                    if name == "__init__.py":
                        idx.modules.add(path.parent.name)
                    _scan_python(idx, text, rel)
                else:
                    _scan_js(idx, text, rel)
    return idx


def _read_dotenv(idx: RepoIndex, path: Path, rel: str) -> None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return
    for m in _DOTENV_LINE.finditer(text):
        idx.env_vars.setdefault(m.group(1), Location(rel, text[: m.start()].count("\n") + 1))


def _read_manifest(idx: RepoIndex, path: Path, rel: str) -> None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return
    if path.name == "pyproject.toml":
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            return
        project = data.get("project") or {}
        req = project.get("requires-python") or (
            data.get("tool", {}).get("poetry", {}).get("dependencies") or {}
        ).get("python")
        if isinstance(req, str) and rel.count("/") == 0:
            line = next(
                (n for n, ln in enumerate(text.splitlines(), 1) if "python" in ln and req in ln), 1
            )
            idx.requires_python = (req, Location(rel, line))
        scripts = dict(project.get("scripts") or {})
        scripts.update((data.get("tool", {}).get("poetry", {}).get("scripts")) or {})
        idx.cli_names.update(scripts)
    elif path.name == "setup.cfg":
        m = re.search(r"^python_requires\s*=\s*(.+)$", text, re.M)
        if m and idx.requires_python is None:
            idx.requires_python = (
                m.group(1).strip(),
                Location(rel, text[: m.start()].count("\n") + 1),
            )
    else:
        import json

        try:
            data = json.loads(text)
        except ValueError:
            return
        node = (data.get("engines") or {}).get("node") if isinstance(data, dict) else None
        if isinstance(node, str) and rel.count("/") == 0:
            idx.engines_node = (node, Location(rel, 1))
        if isinstance(data, dict):
            bins = data.get("bin")
            if isinstance(bins, dict):
                idx.cli_names.update(bins)
            elif isinstance(data.get("name"), str) and bins:
                idx.cli_names.add(data["name"])


def _scan_python(idx: RepoIndex, text: str, rel: str) -> None:
    for m in _ENV_CALL.finditer(text):
        idx.env_vars.setdefault(m.group(1), Location(rel, text[: m.start()].count("\n") + 1))
    if re.search(r"BaseSettings|pydantic_settings|env_prefix|from decouple|environs", text):
        idx.env_reads_by_framework = True
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            idx.defined.setdefault(node.name, Location(rel, node.lineno))
        elif isinstance(node, ast.Import):
            for a in node.names:
                idx.imported.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                idx.imported.add(a.asname or a.name)
        elif isinstance(node, ast.Call):
            fn = node.func
            fname = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if fname in ("add_argument", "option", "Option", "argument", "add_parser"):
                for arg in node.args:
                    if (
                        isinstance(arg, ast.Constant)
                        and isinstance(arg.value, str)
                        and arg.value.startswith("--")
                    ):
                        idx.flags.setdefault(arg.value.split("=")[0], Location(rel, node.lineno))
            if fname in ("ArgumentParser", "Typer", "Fire", "command", "group"):
                for kw in node.keywords:
                    if kw.arg == "prog" and isinstance(kw.value, ast.Constant):
                        idx.cli_names.add(str(kw.value.value))
        if isinstance(node, ast.FunctionDef):
            for deco in node.decorator_list:
                d = ast.unparse(deco)
                if re.search(r"(app|typer)\.command|\.callback", d):
                    idx.flags_implicit = True
                    for a in node.args.args + node.args.kwonlyargs:
                        idx.flags.setdefault(
                            "--" + a.arg.replace("_", "-"), Location(rel, node.lineno)
                        )
    if "import fire" in text or "import typer" in text or "from typer" in text:
        idx.flags_implicit = True
    if "__main__" in text and rel.endswith(".py"):
        idx.cli_names.add(Path(rel).name)


def _scan_js(idx: RepoIndex, text: str, rel: str) -> None:
    for m in _PROCESS_ENV.finditer(text):
        idx.env_vars.setdefault(m.group(1), Location(rel, text[: m.start()].count("\n") + 1))
    for m in _ENV_CALL.finditer(text):
        idx.env_vars.setdefault(m.group(1), Location(rel, text[: m.start()].count("\n") + 1))
    if re.search(r"\.option\(|\.command\(|yargs|commander|minimist", text):
        for m in _FLAG_STRING.finditer(text):
            idx.flags.setdefault(m.group(1), Location(rel, text[: m.start()].count("\n") + 1))


# ----------------------------------------------------------------------------- checks


_SYSTEM_ENV = {
    "PATH", "HOME", "USER", "PWD", "SHELL", "LANG", "TERM", "TMPDIR", "TEMP", "TMP", "PYTHONPATH",
    "PYTHONUNBUFFERED", "PYTHONDONTWRITEBYTECODE", "VIRTUAL_ENV", "CI", "EDITOR", "DISPLAY", "LD_LIBRARY_PATH",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "DEBIAN_FRONTEND", "TZ", "LOGNAME", "HOSTNAME", "APPDATA",
}  # fmt: skip
_ENV_CONTEXT = re.compile(
    r"env(?:ironment)?[\s-]*(?:var(?:iable)?s?)?|\.env\b|\bexport\b|dotenv", re.I
)
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{2,}$")
_EXPORT_LINE = re.compile(
    r"^\s*(?:export\s+|set\s+|ENV\s+|\$env:)?([A-Z][A-Z0-9_]{2,})\s*[=:]\s*\S*"
)


def _env_claims(lines: list[DocLine]) -> list[tuple[str, int]]:
    found: dict[str, int] = {}
    for dl in lines:
        if dl.fence is not None:
            if dl.fence in (
                "",
                "bash",
                "sh",
                "shell",
                "zsh",
                "console",
                "env",
                "dotenv",
                "ini",
                "powershell",
                "dockerfile",
            ):
                m = _EXPORT_LINE.match(dl.text.lstrip("$ ").rstrip())
                if m and (
                    dl.fence in ("env", "dotenv", "ini")
                    or re.match(r"^\s*(export|set|ENV|\$env:)\b", dl.text.lstrip("$ "))
                ):
                    found.setdefault(m.group(1), dl.number)
            continue
        in_env_table = (
            dl.table_header
            and re.search(r"variable|env", dl.table_header)
            and not re.match(r"^\s*\|[\s:-]+\|", dl.text)
        )
        if not (in_env_table or _ENV_CONTEXT.search(dl.text)):
            continue
        spans = _CODE_SPAN.findall(dl.text)
        if in_env_table:
            spans = spans[:1]  # first cell holds the variable name
        for span in spans:
            name = span.split("=")[0].strip().lstrip("$").strip("{}")
            if _ENV_NAME.match(name) and (in_env_table or "_" in name):
                found.setdefault(name, dl.number)
    return [(n, ln) for n, ln in found.items() if n not in _SYSTEM_ENV]


def check_env(lines, file, idx, linemap) -> list[ProjectFinding]:
    out = []
    for name, number in _env_claims(lines):
        line = remap_line(linemap, number)
        subject = f"`{name}`"
        loc = idx.env_vars.get(name)
        if loc:
            out.append(
                ProjectFinding(
                    VERIFIED,
                    "env",
                    file,
                    line,
                    subject,
                    f"Environment variable {subject} is read by the code.",
                    str(loc),
                )
            )
            continue
        near = difflib.get_close_matches(name, idx.env_vars, n=1, cutoff=0.75)
        if near:
            out.append(
                ProjectFinding(
                    CONTRADICTED, "env", file, line, subject,
                    f"Environment variable {subject} is documented but the code reads `{near[0]}`.",
                    str(idx.env_vars[near[0]]), f"Rename `{name}` to `{near[0]}` in the docs.",
                )
            )  # fmt: skip
        elif idx.env_vars and not idx.env_reads_by_framework:
            out.append(
                ProjectFinding(
                    CONTRADICTED, "env", file, line, subject,
                    f"Environment variable {subject} is documented but never read by the code "
                    f"(the code reads {len(idx.env_vars)} other variable(s)).",
                    "searched os.environ / os.getenv / process.env / .env.example",
                    f"Remove `{name}` from the docs or add support for it in the code.",
                )
            )  # fmt: skip
        else:
            why = (
                "settings are loaded by a framework"
                if idx.env_reads_by_framework
                else "no environment variable reads were found in the code"
            )
            out.append(
                ProjectFinding(
                    UNCERTAIN,
                    "env",
                    file,
                    line,
                    subject,
                    f"Cannot confirm environment variable {subject}: {why}.",
                )
            )
    return out


_FLAG = re.compile(r"(?<![\w-])(--[a-z][\w-]*)(?![\w-])")
_EXTERNAL_TOOLS = re.compile(
    r"\b(pip3?|pipx|uv|poetry|npm|npx|yarn|pnpm|docker|docker-compose|git|gh|curl|wget|kubectl|helm|terraform|"
    r"make|apt(?:-get)?|brew|pytest|ruff|black|mypy|node|conda|gcloud|aws|az|cargo|go|java|mvn|gradle|ssh|tar)\b"
)
_BUILTIN_FLAGS = {"--help", "--version"}


def _command_tokens(idx: RepoIndex) -> set[str]:
    tokens = {n for n in idx.cli_names if n}
    return tokens


def check_flags(lines, file, idx, linemap) -> list[ProjectFinding]:
    if not idx.flags:
        return []  # no CLI found in the code: nothing to compare against
    commands = _command_tokens(idx)
    found: dict[str, int] = {}
    for dl in lines:
        candidates: list[str] = []
        if dl.fence is not None:
            if dl.fence not in ("", "bash", "sh", "shell", "zsh", "console", "powershell", "cmd"):
                continue
            text = dl.text.lstrip("$> ").strip()
            mentions_project = any(
                re.search(rf"(?<![\w-]){re.escape(c)}(?![\w-])", text) for c in commands
            )
            if mentions_project and not (
                _EXTERNAL_TOOLS.match(text) and not re.match(r"^python3?\b", text)
            ):
                candidates = _FLAG.findall(text)
        else:
            for span in _CODE_SPAN.findall(dl.text):
                span = span.strip()
                if re.fullmatch(r"--[a-z][\w-]*(?:[= ]\S+)?", span) and not _EXTERNAL_TOOLS.search(
                    dl.text
                ):
                    candidates.append(span.split("=")[0].split(" ")[0])
                elif any(re.search(rf"(?<![\w-]){re.escape(c)}(?![\w-])", span) for c in commands):
                    candidates += _FLAG.findall(span)
        for flag in candidates:
            found.setdefault(flag, dl.number)
    out = []
    for flag, number in found.items():
        if flag in _BUILTIN_FLAGS:
            continue
        line = remap_line(linemap, number)
        subject = f"`{flag}`"
        loc = idx.flags.get(flag)
        if loc:
            out.append(
                ProjectFinding(
                    VERIFIED, "flag", file, line, subject, f"CLI flag {subject} exists.", str(loc)
                )
            )
            continue
        near = difflib.get_close_matches(flag, idx.flags, n=1, cutoff=0.75)
        if near:
            out.append(
                ProjectFinding(
                    CONTRADICTED, "flag", file, line, subject,
                    f"CLI flag {subject} is documented but the code defines `{near[0]}`.",
                    str(idx.flags[near[0]]), f"Use `{near[0]}` in the docs.",
                )
            )  # fmt: skip
        elif idx.flags_implicit:
            out.append(
                ProjectFinding(
                    UNCERTAIN,
                    "flag",
                    file,
                    line,
                    subject,
                    f"Cannot confirm CLI flag {subject}: some flags come from function signatures that were not fully resolved.",
                )
            )
        else:
            out.append(
                ProjectFinding(
                    CONTRADICTED, "flag", file, line, subject,
                    f"CLI flag {subject} is documented but not defined in the code "
                    f"({len(idx.flags)} flag(s) are defined).",
                    "searched argparse / click / commander definitions",
                    f"Remove {subject} from the docs or add the option to the CLI.",
                )
            )  # fmt: skip
    return out


_PATH_EXT_BARE = (
    ".py",
    ".md",
    ".yml",
    ".yaml",
    ".toml",
    ".cfg",
    ".ini",
    ".sh",
    ".json",
    ".txt",
    ".rst",
    ".adoc",
)
_PATH_EXT_ANY = _PATH_EXT_BARE + (
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".mjs",
    ".cjs",
    ".html",
    ".css",
    ".go",
    ".rs",
    ".java",
    ".rb",
    ".tf",
    ".dockerfile",
)
_LINK = re.compile(r"(?<!\\)!?\[[^\]\n]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_SYMBOL = re.compile(r"^([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\([^`]*\)$")


def _resolve_path(rel: str, doc_dir: str, idx: RepoIndex) -> str | None:
    rel = rel.lstrip("./") if rel.startswith("./") else rel
    if idx.doc_files:  # docs live in their own repository: links resolve inside it first
        base = Path(doc_dir) if doc_dir else Path(".")
        for cand in ((base / rel).as_posix(), rel.lstrip("/")):
            norm = os.path.normpath(cand).replace("\\", "/")
            if norm in idx.doc_files or any(
                f.startswith(norm.rstrip("/") + "/") for f in idx.doc_files
            ):
                return norm
    candidates = []
    if rel.startswith("/"):
        candidates.append(rel.lstrip("/"))
    else:
        base = Path(doc_dir) if doc_dir else Path(".")
        candidates.append((base / rel).as_posix())
        candidates.append(rel)
    for cand in candidates:
        norm = os.path.normpath(cand).replace("\\", "/")
        if norm in idx.files or any(f.startswith(norm.rstrip("/") + "/") for f in idx.files):
            return norm
    if "/" in rel:
        tail = os.path.normpath(rel).replace("\\", "/").lstrip("/")
        for f in idx.files:
            if f.endswith("/" + tail):
                return f
    return None


def _suggest_path(target: str, idx: RepoIndex) -> str | None:
    """A likely intended file: a near-identical path, or a near-identical name with the same extension."""
    stem, ext = os.path.splitext(target.rsplit("/", 1)[-1])
    stems = {os.path.splitext(b)[0]: rel for b, rel in idx.basenames.items() if b.endswith(ext)}
    near = difflib.get_close_matches(stem, sorted(stems), n=1, cutoff=0.8)
    return stems[near[0]] if near else None


def _looks_like_path(span: str) -> bool:
    if (
        not re.fullmatch(r"[\w.@\-/]+", span)
        or span.startswith(("/", "http", "www.", "~", ".."))
        and not span.startswith("../")
    ):
        return False
    if span.startswith("/") or "//" in span:
        return False
    lower = span.lower()
    if "/" in span:
        return lower.endswith(_PATH_EXT_ANY) and not re.search(r"\.(com|org|net|io)/", lower)
    return (
        lower.endswith(_PATH_EXT_BARE)
        and not lower.startswith(".")
        and "." in span.rsplit("/", 1)[-1]
    )


def check_references(lines, file, idx, linemap) -> list[ProjectFinding]:
    out = []
    doc_dir = os.path.dirname(file)
    seen: set[tuple[str, str]] = set()

    def add_path_finding(target: str, number: int, kind: str) -> None:
        if ("path", target) in seen:
            return
        seen.add(("path", target))
        line = remap_line(linemap, number)
        subject = f"`{target}`"
        found = _resolve_path(target, doc_dir, idx)
        if found is None and "/" not in target:
            base = target.rsplit("/", 1)[-1]
            found = idx.basenames.get(base)
        if found:
            out.append(
                ProjectFinding(
                    VERIFIED,
                    "reference",
                    file,
                    line,
                    subject,
                    f"The {kind} {subject} exists.",
                    found,
                )
            )
        else:
            suggestion = _suggest_path(target, idx)
            fix = (
                f"Did you mean `{suggestion}`?"
                if suggestion
                else f"Fix or remove the reference to {subject}."
            )
            out.append(
                ProjectFinding(
                    CONTRADICTED,
                    "reference",
                    file,
                    line,
                    subject,
                    f"The {kind} {subject} does not exist in the repository.",
                    "searched the repository file list",
                    fix,
                )
            )

    for dl in lines:
        if dl.fence is not None:
            continue
        for target in _LINK.findall(dl.text):
            target = target.split("#")[0].split("?")[0]
            if (
                not target
                or re.match(r"^[a-z][a-z0-9+.-]*:", target, re.I)
                or "{" in target
                or "<" in target
            ):
                continue
            add_path_finding(target, dl.number, "linked file")
        for span in _CODE_SPAN.findall(dl.text):
            span = span.strip()
            if _looks_like_path(span) and "*" not in span:
                add_path_finding(span, dl.number, "file")
                continue
            sm = _SYMBOL.match(span)
            if sm and idx.python_files and ("sym", sm.group(1)) not in seen:
                seen.add(("sym", sm.group(1)))
                finding = _check_symbol(sm.group(1), file, dl.number, idx, linemap)
                if finding:
                    out.append(finding)
    return out


def _check_symbol(
    dotted: str, file: str, number: int, idx: RepoIndex, linemap
) -> ProjectFinding | None:
    parts = dotted.split(".")
    name = parts[-1]
    line = remap_line(linemap, number)
    subject = f"`{dotted}()`"
    if len(parts) > 1:
        # only judge references that start inside this repo (module or class defined here)
        if parts[0] not in idx.modules and parts[0] not in idx.defined:
            return None
    elif name in dir(builtins) or name in idx.imported:
        return None
    loc = idx.defined.get(name)
    if loc:
        return ProjectFinding(
            VERIFIED,
            "reference",
            file,
            line,
            subject,
            f"`{name}` is defined in the code.",
            str(loc),
        )
    near = difflib.get_close_matches(name, idx.defined, n=1, cutoff=0.8)
    if near:
        return ProjectFinding(
            CONTRADICTED, "reference", file, line, subject,
            f"{subject} is documented but the code defines `{near[0]}` instead.",
            str(idx.defined[near[0]]), f"Use `{near[0]}()` in the docs.",
        )  # fmt: skip
    return ProjectFinding(
        UNCERTAIN,
        "reference",
        file,
        line,
        subject,
        f"{subject} is not defined in this repository; it may come from a dependency.",
    )


_VERSION_SIGNAL = r"(?:\+|≥|>=|\s+or\s+(?:newer|higher|later|above|greater)|at\s+least|minimum|min\.?|requires?|needs?)"
_PY_CLAIM = re.compile(
    rf"(?:{_VERSION_SIGNAL}\s*)?\bPython\s*(?:>=|≥)?\s*(3\.\d{{1,2}})(?:\.\d+)?(\+|\s+or\s+(?:newer|higher|later|above))?",
    re.I,
)
_NODE_CLAIM = re.compile(
    r"\bNode(?:\.js)?\s*(?:>=|≥|v)?\s*(\d{1,2})(?:\.\d+)*(\+|\s+or\s+(?:newer|higher|later|above))?",
    re.I,
)


def _min_version(spec: str, pattern: str) -> tuple[int, ...] | None:
    m = re.search(pattern, spec)
    return tuple(int(x) for x in m.group(1).split(".")) if m else None


def check_versions(lines, file, idx, linemap) -> list[ProjectFinding]:
    out = []
    py_req = idx.requires_python
    node_req = idx.engines_node
    py_min = _min_version(py_req[0], r">=\s*(3(?:\.\d+)?)") if py_req else None
    node_min = _min_version(node_req[0], r">=\s*(\d+)") if node_req else None
    seen: set[str] = set()
    for dl in lines:
        if dl.fence is not None:
            continue
        text = _CODE_SPAN.sub(lambda m: m.group(1), dl.text)
        for regex, name, expected, loc in (
            (_PY_CLAIM, "Python", py_min, py_req),
            (_NODE_CLAIM, "Node.js", node_min, node_req),
        ):
            if expected is None or loc is None:
                continue
            for m in regex.finditer(text):
                prefix = text[max(0, m.start() - 24) : m.start()]
                is_minimum = bool(m.group(2)) or re.search(
                    _VERSION_SIGNAL, prefix + m.group(0) if prefix else m.group(0), re.I
                )
                if not is_minimum:
                    continue
                claimed = tuple(int(x) for x in m.group(1).split("."))
                if len(claimed) < len(expected):
                    claimed = claimed + (0,) * (len(expected) - len(claimed))
                shown = ".".join(str(x) for x in claimed[: max(len(expected), 1)])
                key = f"{name}{shown}"
                if key in seen:
                    continue
                seen.add(key)
                line = remap_line(linemap, dl.number)
                subject = f"{name} {shown}+"
                want = ".".join(str(x) for x in expected)
                evidence = f"{loc[1]} ({loc[0]})"
                if claimed[: len(expected)] == expected:
                    out.append(
                        ProjectFinding(
                            VERIFIED,
                            "version",
                            file,
                            line,
                            subject,
                            f"The documented minimum {subject} matches the project.",
                            evidence,
                        )
                    )
                else:
                    out.append(
                        ProjectFinding(
                            CONTRADICTED, "version", file, line, subject,
                            f"The docs say {name} {shown}+ but the project requires {name} {want}+.",
                            evidence, f"Change the docs to say {name} {want}+.",
                        )
                    )  # fmt: skip
    return out


# ----------------------------------------------------------------------------- entry point


def verify_project(
    root: str | os.PathLike,
    doc_paths: list[str | os.PathLike] | None = None,
    checks: tuple[str, ...] = CHECK_NAMES,
    docs_root: str | os.PathLike | None = None,
) -> ProjectReport:
    """Run the project checks over the docs under *doc_paths* (default: the whole repo).

    *root* is the code repository. When the docs live in a different repository, pass its
    folder as *docs_root*: doc paths are then reported relative to it, links between docs
    resolve inside it, and it is not mistaken for part of the code.
    """
    root_p = Path(root).resolve()
    docs_root_p = Path(docs_root).resolve() if docs_root else None
    idx = index_repo(root_p, exclude=[docs_root_p] if docs_root_p else None)
    if docs_root_p:
        for dirpath, dirs, names in os.walk(docs_root_p):
            dirs[:] = [d for d in dirs if not _skip_dir(d)]
            idx.doc_files.update(
                (Path(dirpath) / n).relative_to(docs_root_p).as_posix() for n in names
            )
    rel_base = docs_root_p or root_p
    report = ProjectReport(root=str(root_p))
    from infra_checks import build_index as build_infra_index
    from infra_checks import check_infra
    from link_checks import check_links
    from runbook_checks import check_runbook

    idx.docs_root = docs_root_p
    if "infra" in checks:
        idx.infra = build_infra_index(root_p, idx.files)
    runners = {
        "env": check_env,
        "flag": check_flags,
        "reference": check_references,
        "version": check_versions,
        "infra": check_infra,
        "link": check_links,
        "runbook": check_runbook,
    }
    for f in collect_doc_files(doc_paths or [root_p]):
        try:
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            rel = f.resolve().relative_to(rel_base).as_posix()
        except ValueError:
            rel = str(f)
        markdown, linemap = to_markdown(text, kind_of(f))
        lines = iter_doc_lines(markdown)
        report.docs_checked += 1
        for name in checks:
            report.findings += runners[name](lines, rel, idx, linemap)
    return report


def render_project_markdown(report: ProjectReport, max_items: int = 25) -> str:
    counts = report.counts()
    health = report.health.score
    out = [
        "### 🧭 Project facts check",
        "",
        f"Checked **{len(report.findings)}** claim(s) in **{report.docs_checked}** doc file(s): "
        f"✅ {counts[VERIFIED]} verified · ❌ {counts[CONTRADICTED]} contradicted · ❓ {counts[UNCERTAIN]} uncertain"
        + (f" · health **{health:g}/100**" if health is not None else ""),
        "",
    ]
    bad = [f for f in report.findings if f.verdict == CONTRADICTED]
    unsure = [f for f in report.findings if f.verdict == UNCERTAIN]
    if bad:
        out += ["#### ❌ Contradicted", ""]
        for f in bad[:max_items]:
            out.append(f"- `{f.file}:{f.line}` ({f.check}) {f.message}")
            if f.evidence:
                out.append(f"  - Evidence: {f.evidence}")
            if f.fix:
                out.append(f"  - Suggested fix: {f.fix}")
        if len(bad) > max_items:
            out.append(f"- …and {len(bad) - max_items} more")
        out.append("")
    if unsure:
        out += ["<details><summary>❓ Uncertain</summary>", ""]
        out += [f"- `{f.file}:{f.line}` ({f.check}) {f.message}" for f in unsure[:max_items]]
        out += ["", "</details>", ""]
    if not bad and not unsure and report.findings:
        out += ["All checked claims match the repository. ✅", ""]
    if not report.findings:
        out += [
            "No environment variables, CLI flags, file references or version claims were found to check.",
            "",
        ]
    return "\n".join(out)
