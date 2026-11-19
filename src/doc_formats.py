"""
Read reStructuredText and AsciiDoc documentation with the Markdown claim extractor.

``to_markdown`` converts the parts that matter for API documentation - headings,
code/literal blocks, tables, ``sphinxcontrib-httpdomain`` endpoint directives and
inline literals - into Markdown, and returns a *line map* so a finding can still point
at the right line of the original file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

RST_EXTENSIONS = (".rst",)
ADOC_EXTENSIONS = (".adoc", ".asciidoc")
MARKDOWN_EXTENSIONS = (".md", ".mdx", ".markdown")
ALL_EXTENSIONS = MARKDOWN_EXTENSIONS + RST_EXTENSIONS + ADOC_EXTENSIONS


def kind_of(path: str | Path) -> str:
    suffix = Path(path).suffix.lower()
    if suffix in RST_EXTENSIONS:
        return "rst"
    if suffix in ADOC_EXTENSIONS:
        return "adoc"
    return "md"


@dataclass
class _Out:
    lines: list[str] = field(default_factory=list)
    linemap: list[int] = field(default_factory=list)

    def emit(self, text: str, source_line: int) -> None:
        self.lines.append(text)
        self.linemap.append(source_line)


def to_markdown(text: str, kind: str) -> tuple[str, list[int]]:
    """Convert *text* to Markdown. Returns ``(markdown, linemap)``.

    ``linemap[i]`` is the 1-based line of the original file that produced output line
    *i* (0-based). Markdown input is returned unchanged with an identity map.
    """
    lines = text.splitlines()
    if kind == "rst":
        out = _rst(lines)
    elif kind == "adoc":
        out = _adoc(lines)
    else:
        return text, list(range(1, len(lines) + 1))
    return "\n".join(out.lines), out.linemap


def remap_line(linemap: list[int], line: int) -> int:
    if 1 <= line <= len(linemap):
        return linemap[line - 1]
    return line


def _inline_rst(text: str) -> str:
    text = re.sub(r"``([^`]+)``", r"`\1`", text)
    text = re.sub(
        r":(?:class|func|meth|ref|doc|mod|attr|term|samp|code|py:\w+|http:\w+):`~?([^`<]*?)(?:\s*<[^>]*>)?`",
        r"`\1`",
        text,
    )
    return re.sub(r"(?<!`)`([^`<]+?)\s*<([^>]+)>`_+", r"\1 (\2)", text)


# ----------------------------------------------------------------------------- RST

_RST_UNDERLINE = re.compile(r"^([=\-~^\"#*+`:._'])\1{2,}\s*$")
_HTTP_DIRECTIVE = re.compile(
    r"^\.\.\s+http:(get|post|put|patch|delete|head|options)::\s*(\S+)", re.I
)
_CODE_DIRECTIVE = re.compile(r"^\.\.\s+(?:code-block|sourcecode|code|highlight)::")
_FIELD = re.compile(r"^\s+:(<?\w+>?)(?:\s+([^:]*?))?:\s*(.*)$")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _rst_http_path(path: str) -> str:
    return re.sub(r"\((?:\w+:)?(\w+)\)", r"{\1}", path)


def _rst(lines: list[str]) -> _Out:
    out = _Out()
    levels: dict[tuple[str, bool], int] = {}
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        src = i + 1
        stripped = line.strip()

        m = _HTTP_DIRECTIVE.match(stripped) if not line.startswith(" ") else None
        if m:
            out.emit("", src)
            out.emit(f"### {m.group(1).upper()} {_rst_http_path(m.group(2))}", src)
            i = _rst_http_body(lines, i + 1, out)
            continue

        # code-block / literal block ("::")
        if _CODE_DIRECTIVE.match(stripped) or (
            stripped.endswith("::") and not stripped.startswith("..")
        ):
            if stripped.endswith("::") and not _CODE_DIRECTIVE.match(stripped):
                lead = stripped[:-2].rstrip()
                if lead:
                    out.emit(_inline_rst(lead + ":"), src)
            i = _rst_literal(lines, i + 1, out, src)
            continue

        # tables
        if stripped.startswith("+") and re.match(r"^\+[-=+]+\+$", stripped):
            i = _rst_grid_table(lines, i, out)
            continue
        if re.match(r"^=+(\s+=+)+$", stripped):
            i = _rst_simple_table(lines, i, out)
            continue

        # headings (title + underline, optionally overlined)
        if stripped and i + 1 < n and _RST_UNDERLINE.match(lines[i + 1].strip()):
            under = lines[i + 1].strip()
            if len(under) >= len(stripped) and not _RST_UNDERLINE.match(stripped):
                style = (under[0], False)
                level = levels.setdefault(style, len(levels) + 1)
                out.emit("#" * min(level, 6) + " " + _inline_rst(stripped), src)
                i += 2
                continue
        if (
            _RST_UNDERLINE.match(stripped)
            and i + 2 < n
            and lines[i + 1].strip()
            and _RST_UNDERLINE.match(lines[i + 2].strip())
        ):
            style = (stripped[0], True)
            level = levels.setdefault(style, len(levels) + 1)
            out.emit("#" * min(level, 6) + " " + _inline_rst(lines[i + 1].strip()), src + 1)
            i += 3
            continue
        if _RST_UNDERLINE.match(stripped) and out.lines:
            i += 1  # stray transition / overline we already consumed
            continue

        out.emit(_inline_rst(line), src)
        i += 1
    return out


def _rst_literal(lines: list[str], i: int, out: _Out, src: int) -> int:
    """Consume an indented literal block starting at *i*; emits a Markdown fence."""
    n = len(lines)
    j = i
    while j < n and not lines[j].strip():
        j += 1
    out.emit("```", src)
    if j >= n or _indent(lines[j]) == 0:
        out.emit("```", src)
        return i
    while j < n and (not lines[j].strip() or re.match(r"^\s+:\w[\w-]*:", lines[j])):
        j += 1  # skip directive options such as ``:linenos:``
    base = _indent(lines[j]) if j < n else 0
    while j < n and (not lines[j].strip() or _indent(lines[j]) >= base):
        if lines[j].strip():
            out.emit(lines[j][base:], j + 1)
        j += 1
    out.emit("```", min(j, n))
    return j


def _rst_http_body(lines: list[str], i: int, out: _Out) -> int:
    n = len(lines)
    emitted_params = False
    emitted_responses = False
    while i < n:
        line = lines[i]
        if line.strip() and _indent(line) == 0:
            break
        src = i + 1
        fm = _FIELD.match(line)
        if fm:
            role = fm.group(1).strip("<>").lower()
            args = (fm.group(2) or "").split()
            desc = _inline_rst(fm.group(3))
            if role in ("statuscode", "status", "resstatuscode") and args:
                if not emitted_responses:
                    out.emit("Responses:", src)
                    emitted_responses = True
                out.emit(f"- `{args[0]}` {desc}".rstrip(), src)
            elif (
                role
                in (
                    "query",
                    "queryparam",
                    "param",
                    "parameter",
                    "arg",
                    "key",
                    "reqheader",
                    "header",
                    "json",
                    "jsonparam",
                    "reqjson",
                    "form",
                    "formparam",
                )
                and args
            ):
                name = args[-1]
                kind = args[0] if len(args) > 1 else ""
                attrs = [a for a in (kind, _rst_role_location(role)) if a]
                if not emitted_params:
                    out.emit("Parameters:", src)
                    emitted_params = True
                suffix = f" ({', '.join(attrs)})" if attrs else ""
                out.emit(f"- `{name}`{suffix}: {desc}".rstrip(": "), src)
            elif role in (
                "reqjsonarr",
                "resjson",
                "resheader",
                "responseheader",
                "resjsonarr",
                "reqheaders",
            ):
                pass  # response body/headers: not part of what we verify here
            else:
                out.emit(line.strip(), src)
        elif line.strip():
            if emitted_params or emitted_responses:
                emitted_params = emitted_responses = False
            out.emit(_inline_rst(line.strip()), src)
        else:
            out.emit("", src)
        i += 1
    return i


def _rst_role_location(role: str) -> str:
    if role in ("query", "queryparam"):
        return "query"
    if role in ("reqheader", "header"):
        return "header"
    if role in ("json", "jsonparam", "reqjson", "form", "formparam"):
        return "body"
    return ""


def _rst_grid_table(lines: list[str], i: int, out: _Out) -> int:
    n = len(lines)
    rows: list[tuple[list[str], int]] = []
    header_after: int | None = None
    j = i
    while j < n and (lines[j].strip().startswith(("+", "|"))):
        s = lines[j].strip()
        if s.startswith("+"):
            if "=" in s and header_after is None and rows:
                header_after = len(rows)
        else:
            rows.append(([_inline_rst(c.strip()) for c in s.strip("|").split("|")], j + 1))
        j += 1
    _emit_table(out, rows, header_after if header_after is not None else 1)
    return j


def _rst_simple_table(lines: list[str], i: int, out: _Out) -> int:
    n = len(lines)
    border = lines[i].strip()
    spans = [(m.start(), m.end()) for m in re.finditer(r"=+", border)]
    j = i + 1
    rows: list[tuple[list[str], int]] = []
    header_after = None
    while j < n:
        s = lines[j]
        if re.match(r"^=+(\s+=+)+$", s.strip()):
            if header_after is None and rows:
                header_after = len(rows)
                j += 1
                continue
            j += 1
            break
        if not s.strip():
            j += 1
            continue
        cells = []
        for k, (a, b) in enumerate(spans):
            end = len(s) if k == len(spans) - 1 else b
            cells.append(_inline_rst(s[a:end].strip()))
        rows.append((cells, j + 1))
        j += 1
    _emit_table(out, rows, header_after if header_after is not None else 1)
    return j


def _emit_table(out: _Out, rows: list[tuple[list[str], int]], header_rows: int) -> None:
    if not rows:
        return
    out.emit("", rows[0][1])
    header, header_line = rows[0]
    out.emit("| " + " | ".join(header) + " |", header_line)
    out.emit("|" + "|".join("---" for _ in header) + "|", header_line)
    for cells, line in rows[max(header_rows, 1) :]:
        out.emit("| " + " | ".join(cells) + " |", line)
    out.emit("", rows[-1][1])


# ----------------------------------------------------------------------------- AsciiDoc

_ADOC_HEADING = re.compile(r"^(={1,6})\s+(\S.*)$")
_ADOC_ATTR = re.compile(r"^\[[^\]]*\]\s*$")
_ADOC_TITLE = re.compile(r"^\.([A-Za-z][^.].*)$")


def _adoc(lines: list[str]) -> _Out:
    out = _Out()
    i, n = 0, len(lines)
    pending_header = False
    pending_cols: int | None = None
    while i < n:
        line = lines[i]
        src = i + 1
        s = line.strip()

        if s in ("----", "....") or re.match(r"^-{4,}$|^\.{4,}$", s):
            out.emit("```", src)
            i += 1
            while i < n and not re.match(r"^-{4,}$|^\.{4,}$", lines[i].strip()):
                out.emit(lines[i], i + 1)
                i += 1
            out.emit("```", min(i + 1, n))
            i += 1
            continue

        if _ADOC_ATTR.match(s):
            if re.search(r"header|%header", s):
                pending_header = True
            cm = re.search(r'cols\s*=\s*"?([^"\]]+)"?', s)
            if cm:
                pending_cols = 0
                for part in re.split(r"[,;]", cm.group(1)):
                    rep = re.match(r"\s*(\d+)\*", part)
                    if rep:
                        pending_cols += int(rep.group(1))  # "3*" means three columns
                    elif part.strip():
                        pending_cols += 1
            i += 1
            continue

        if s.startswith("|==="):
            rows: list[list[str]] = []
            row_lines: list[int] = []
            blank_after_first = False
            i += 1
            while i < n and not lines[i].strip().startswith("|==="):
                t = lines[i].strip()
                if t.startswith("|"):
                    cells = [c.strip() for c in t[1:].split("|")]
                    rows.append([_inline_rst(c) for c in cells])
                    row_lines.append(i + 1)
                elif not t and len(rows) == 1:
                    blank_after_first = True
                i += 1
            i += 1
            if rows and all(len(r) == 1 for r in rows) and pending_cols and pending_cols > 1:
                flat = [r[0] for r in rows]
                chunks = [flat[k : k + pending_cols] for k in range(0, len(flat), pending_cols)]
                rows = chunks
                row_lines = [
                    row_lines[min(k * pending_cols, len(row_lines) - 1)] for k in range(len(chunks))
                ]
            has_header = pending_header or blank_after_first or len(rows) > 1
            if rows and has_header:
                out.emit("", row_lines[0])
                out.emit("| " + " | ".join(rows[0]) + " |", row_lines[0])
                out.emit("|" + "|".join("---" for _ in rows[0]) + "|", row_lines[0])
                for cells, ln in zip(rows[1:], row_lines[1:], strict=True):
                    out.emit("| " + " | ".join(cells) + " |", ln)
                out.emit("", row_lines[-1])
            pending_header, pending_cols = False, None
            continue

        hm = _ADOC_HEADING.match(s)
        if hm:
            out.emit("#" * len(hm.group(1)) + " " + _inline_rst(hm.group(2)), src)
            i += 1
            continue

        tm = _ADOC_TITLE.match(s)
        if tm and not s.startswith(".."):
            out.emit(tm.group(1).strip() + ":", src)
            i += 1
            continue

        out.emit(
            re.sub(r"^\*+\s", "- ", line.strip(), count=1) if re.match(r"^\*+\s", s) else line, src
        )
        i += 1
    return out
