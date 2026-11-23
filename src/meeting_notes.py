"""
Turn meeting notes into documentation follow-ups.

Many documentation mistakes start in a meeting: someone says "we're renaming the
``SHOP_DB_URL`` variable" or "the ``/invoices`` endpoint is going away" and nobody edits
the docs. This module reads a transcript (Microsoft Teams ``.vtt`` or ``.docx``, or plain
text), pulls out the statements that affect documentation and checks each one against the
real code and docs:

* **verified**    the code already matches the decision and the docs agree with it.
* **contradicted** the code matches the decision but the docs still describe the old thing.
* **uncertain**   the decision is not in the code yet (a *pending* item), or cannot be found.

Statements are found with plain rules, so the tool works with no model and gives the same
answer every time. With ``--ai`` a model can *also* spot decisions in loose conversation;
it only proposes statements. The verdict always comes from searching the code.

The transcript is read locally. Nothing leaves the machine unless ``--ai`` is used, and
then speaker names and e-mail addresses are redacted before the text is sent.

    python src/meeting_notes.py meeting.vtt --root . docs
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

CONTRADICTED = "contradicted"
UNCERTAIN = "uncertain"
VERIFIED = "verified"

MAX_FILE_BYTES = 400_000
CODE_SUFFIXES = (
    ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".go", ".java", ".kt", ".rb", ".cs",
    ".sh", ".yaml", ".yml", ".toml", ".json", ".cfg", ".ini", ".tf", ".env",
)  # fmt: skip
DOC_SUFFIXES = (".md", ".markdown", ".mdx", ".rst", ".adoc", ".asciidoc", ".txt")
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".tox"}

# ----------------------------------------------------------------------------- transcripts


@dataclass
class Utterance:
    speaker: str
    time: str  # "00:02:14" or ""
    text: str
    line: int  # 1-based line in the source file


_CUE_TIME = re.compile(r"^\s*((?:\d{1,2}:)?\d{1,2}:\d{2})(?:[.,]\d+)?\s*-->")
_V_TAG = re.compile(r"^<v(?:\.\w+)*\s+([^>]+)>(.*?)(?:</v>)?$", re.S)
_TAGS = re.compile(r"<[^>]+>")
_TEXT_LINE = re.compile(
    r"^\s*(?:\[(?P<t1>[\d:]+)\]\s*)?(?P<who>[A-Z][\w .'-]{0,40}?)\s*(?:\((?P<t2>[\d:]+)\))?\s*:\s+(?P<text>\S.*)$"
)
_DOCX_HEADER_A = re.compile(r"^(?P<time>\d{1,2}:\d{2}(?::\d{2})?)\s*(?:[-–—/|]\s*)?(?P<who>\D.*)$")
_DOCX_HEADER_B = re.compile(r"^(?P<who>\D.*?)\s+(?P<time>\d{1,2}:\d{2}(?::\d{2})?)$")


def _clock(raw: str) -> str:
    parts = raw.split(":")
    if len(parts) == 2:
        parts = ["00", *parts]
    return ":".join(p.zfill(2) for p in parts)


def _merge(items: list[Utterance]) -> list[Utterance]:
    merged: list[Utterance] = []
    for u in items:
        # join a sentence that was split across captions; keep separate remarks apart
        if (
            merged
            and u.speaker
            and merged[-1].speaker == u.speaker
            and not merged[-1].text.rstrip().endswith((".", "?", "!", ":"))
        ):
            merged[-1].text += " " + u.text
        else:
            merged.append(Utterance(u.speaker, u.time, u.text, u.line))
    return merged


def parse_vtt(text: str) -> list[Utterance]:
    """WebVTT as exported by Microsoft Teams, Zoom and Google Meet."""
    out: list[Utterance] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = _CUE_TIME.match(lines[i])
        if not m:
            i += 1
            continue
        start = _clock(m.group(1))
        i += 1
        first_line = i + 1
        chunk: list[str] = []
        while i < len(lines) and lines[i].strip():
            chunk.append(lines[i].strip())
            i += 1
        body = " ".join(chunk)
        speaker = ""
        vm = _V_TAG.match(body)
        if vm:
            speaker, body = vm.group(1).strip(), vm.group(2)
        body = html.unescape(_TAGS.sub("", body)).strip()
        if not speaker:
            tm = re.match(r"^([A-Z][\w .'-]{0,40}?):\s+(\S.*)$", body)
            if tm:
                speaker, body = tm.group(1), tm.group(2)
        if body:
            out.append(Utterance(speaker, start, body, first_line))
    return _merge(out)


def parse_docx(path: str | os.PathLike) -> list[Utterance]:
    """The Word transcript Teams offers: a speaker/time line followed by what was said."""
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8", "replace")
    paragraphs = []
    for chunk in xml.split("</w:p>"):
        runs = re.findall(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", chunk, flags=re.S)
        text = html.unescape("".join(runs)).strip()
        if text:
            paragraphs.append(text)
    out: list[Utterance] = []
    speaker, time = "", ""
    for number, para in enumerate(paragraphs, 1):
        header = _DOCX_HEADER_A.match(para) or _DOCX_HEADER_B.match(para)
        if header and len(para) < 80 and not para.endswith((".", "?", "!")):
            speaker, time = header.group("who").strip(), _clock(header.group("time"))
            continue
        out.append(Utterance(speaker, time, para, number))
    return _merge(out)


def parse_text(text: str) -> list[Utterance]:
    """Plain notes: ``[00:01:02] Priya: text``, ``Priya: text`` or free text."""
    out: list[Utterance] = []
    for number, raw in enumerate(text.splitlines(), 1):
        if not raw.strip():
            continue
        m = _TEXT_LINE.match(raw)
        if m:
            time = m.group("t1") or m.group("t2") or ""
            out.append(
                Utterance(
                    m.group("who").strip(), _clock(time) if time else "", m.group("text"), number
                )
            )
        elif out and raw.startswith((" ", "\t")):
            out[-1].text += " " + raw.strip()
        else:
            out.append(Utterance("", "", raw.strip().lstrip("-*• "), number))
    return _merge(out)


def parse_transcript(path: str | os.PathLike) -> list[Utterance]:
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix == ".docx":
        return parse_docx(p)
    text = p.read_text(encoding="utf-8-sig")
    if suffix == ".vtt" or text.lstrip().startswith("WEBVTT"):
        return parse_vtt(text)
    return parse_text(text)


# ----------------------------------------------------------------------------- statements

_TOKEN = (
    r"(?-i:`[^`\n]+`|--[a-z][\w-]*|/[\w{}\-./:]*[\w}]|[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+"
    r"|[\w./-]+\.(?:py|js|ts|go|java|rb|cs|sh|ya?ml|json|toml|md)\b|\w+\(\))"
)
_VALUE = r"[\w.%$-]+"
_RENAME = re.compile(
    rf"\b(?:renam\w*|replac\w*|migrat\w*|mov\w*|switch\w*|chang\w*)\b[^.\n]*?"
    rf"(?P<old>{_TOKEN})(?:\s+\w+)?\s*(?:to|with|by|into|->|→)\s*(?:the\s+|a\s+)?(?P<new>{_TOKEN})",
    re.I,
)
_WAS_RENAMED = re.compile(
    rf"(?P<old>{_TOKEN})(?:\s+\w+)?\s+(?:is|was|will be|gets|got|is being|is getting|has been)\s+"
    rf"(?:\w+\s+)?(?:renamed|replaced|changed|moved|switched)\s+(?:\w+\s+)?"
    rf"(?:to|with|by)\s+(?:the\s+)?(?P<new>{_TOKEN})",
    re.I,
)
_IS_NOW = re.compile(
    rf"(?P<old>{_TOKEN})\s+(?:is|will be|becomes|became)\s+(?:now\s+|called\s+)+(?P<new>{_TOKEN})",
    re.I,
)
_REMOVE_A = re.compile(
    rf"\b(?P<verb>remov\w*|retir\w*|deprecat\w*|drop\w*|sunset\w*|delet\w*|kill\w*)\b[^.\n]*?"
    rf"(?P<tok>{_TOKEN})",
    re.I,
)
_REMOVE_B = re.compile(
    rf"(?P<tok>{_TOKEN})(?:\s+\w+)?\s+(?:is|was|has been|have been|will be|is being|got|are)\s+"
    r"(?:\w+\s+)?(?P<verb>removed|retired|deprecated|dropped|deleted|sunset|going away)\b",
    re.I,
)
_VALUE_CHANGE = re.compile(
    r"\b(?:chang\w*|bump\w*|rais\w*|lower\w*|increas\w*|decreas\w*|updat\w*|set|going|moving)\s+"
    rf"(?:the\s+)?(?P<subj>[\w`\-_ ]{{2,40}}?)\s+(?:is\s+)?from\s+(?P<a>{_VALUE})\s+to\s+(?P<b>{_VALUE})",
    re.I,
)
_ACTION = re.compile(
    r"\b(?P<who>(?-i:[A-Z][a-z]+|I|we))(?:\s+(?:will|to|should|needs? to|can)|'ll)\s+"
    r"(?:update|fix|write|add|refresh|document|revise)\b[^.\n]*?"
    r"\b(?:docs?|documentation|readme|runbook|guide|wiki)\b",
    re.I,
)
_FUTURE = re.compile(r"\b(will|going to|gonna|plan|planning|next sprint|soon|later|we'll)\b", re.I)


@dataclass
class Statement:
    kind: str  # rename | remove | change | action
    text: str  # the sentence it came from
    speaker: str = ""
    time: str = ""
    line: int = 0
    old: str = ""
    new: str = ""
    subject: str = ""  # for kind == "change": the thing whose value changes
    owner: str = ""  # for kind == "action"
    source: str = "rules"  # "rules" or "ai"
    planned: bool = False  # said in the future tense


def _clean_token(tok: str) -> str:
    tok = tok.strip().strip("`").strip()
    if tok.endswith("()"):
        return tok[:-2]
    return tok.rstrip(".,;:)")


def token_kind(tok: str) -> str:
    if tok.startswith("--"):
        return "flag"
    if tok.startswith("/") and not re.search(r"\.\w{1,4}$", tok):
        return "path"
    if re.fullmatch(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+", tok):
        return "env"
    if re.search(r"\.\w{1,5}$", tok) or "/" in tok:
        return "file"
    return "name"


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z`/\-])", text) if s.strip()]


def extract_statements(utterances: list[Utterance]) -> list[Statement]:
    out: list[Statement] = []
    for u in utterances:
        for sent in _sentences(u.text):
            base = {"text": sent, "speaker": u.speaker, "time": u.time, "line": u.line}
            planned = bool(_FUTURE.search(sent))
            m = _WAS_RENAMED.search(sent) or _RENAME.search(sent) or _IS_NOW.search(sent)
            if m:
                old, new = _clean_token(m.group("old")), _clean_token(m.group("new"))
                if old != new and token_kind(old) == token_kind(new):
                    out.append(Statement("rename", old=old, new=new, planned=planned, **base))
                    continue
            m = _VALUE_CHANGE.search(sent)
            if m:
                words = [
                    w for w in re.findall(r"[\w-]+", m.group("subj")) if w.lower() != "default"
                ]
                if words:
                    out.append(
                        Statement(
                            "change",
                            old=m.group("a").rstrip(".,;:"),
                            new=m.group("b").rstrip(".,;:"),
                            subject=_clean_token(words[-1]),
                            planned=planned,
                            **base,
                        )
                    )
                    continue
            m = _REMOVE_B.search(sent) or _REMOVE_A.search(sent)
            if m:
                tok = _clean_token(m.group("tok"))
                if tok:
                    out.append(Statement("remove", old=tok, planned=planned, **base))
                    continue
            m = _ACTION.search(sent)
            if m:
                who = m.group("who")
                owner = u.speaker if who.lower() in ("i", "we") and u.speaker else who
                out.append(Statement("action", owner=owner, **base))
    return out


# ----------------------------------------------------------------------------- what is true now


@dataclass
class Hit:
    file: str
    line: int
    text: str = ""

    def __str__(self) -> str:
        return f"{_ref(self)}"


class Corpus:
    """The code and docs a statement is checked against."""

    def __init__(
        self,
        code: dict[str, str] | None = None,
        docs: dict[str, str] | None = None,
        routes: set[str] | None = None,
        files: set[str] | None = None,
        api_only: bool = False,
    ):
        # api_only: only an API definition is available (no source tree), so anything that is
        # not an endpoint path cannot be confirmed or ruled out.
        self.api_only = api_only
        self.code = code or {}
        self.docs = docs or {}
        self.routes = routes or set()
        self.files = files if files is not None else set(self.code) | set(self.docs)
        self._cache: dict[str, Hit | None] = {}

    @classmethod
    def from_repo(
        cls,
        root: str | os.PathLike,
        doc_paths: list[str | os.PathLike] | None = None,
        spec=None,
        skip: list[str | os.PathLike] | None = None,
        docs_root: str | os.PathLike | None = None,
    ) -> Corpus:
        root_p = Path(root).resolve()
        skipped = {Path(s).resolve() for s in skip or []}
        code: dict[str, str] = {}
        files: set[str] = set()
        for dirpath, dirs, names in os.walk(root_p):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith(".git"))
            for name in sorted(names):
                path = Path(dirpath) / name
                if path.resolve() in skipped:
                    continue
                rel = path.relative_to(root_p).as_posix()
                files.add(rel)
                suffix = path.suffix.lower()
                if suffix in DOC_SUFFIXES or not (
                    suffix in CODE_SUFFIXES or name.startswith(".env") or name in ("Dockerfile",)
                ):
                    continue
                try:
                    if path.stat().st_size > MAX_FILE_BYTES:
                        continue
                    code[rel] = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
        docs_base = Path(docs_root).resolve() if docs_root else root_p
        docs: dict[str, str] = {}
        from api_contract import collect_doc_files

        for f in collect_doc_files(list(doc_paths) if doc_paths else [root_p]):
            try:
                rel = f.resolve().relative_to(docs_base).as_posix()
            except ValueError:
                rel = str(f)
            try:
                docs[rel] = f.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
        routes: set[str] = set()
        if spec is not None:
            from api_contract import normalize_path

            routes = {normalize_path(p) for (_m, p) in spec.operations}
        return cls(code, docs, routes, files)

    # -- searching ----------------------------------------------------------

    @staticmethod
    def _pattern(tok: str) -> re.Pattern:
        if tok.startswith("/"):
            return re.compile(r"(?<![\w/])" + re.escape(tok) + r"(?![\w/{-])")
        return re.compile(r"(?<![\w-])" + re.escape(tok) + r"(?![\w-])")

    def find_in_code(self, tok: str) -> Hit | None:
        """Where the code uses *tok*: a route, a file, or a word in a source file."""
        if tok in self._cache:
            return self._cache[tok]
        hit: Hit | None = None
        kind = token_kind(tok)
        if kind == "path":
            from api_contract import normalize_path

            if normalize_path(tok) in self.routes:
                hit = Hit("(API definition)", 0, tok)
        if hit is None and kind in ("file",):
            if tok in self.files:
                hit = Hit(tok, 0, tok)
            else:
                base = tok.rsplit("/", 1)[-1]
                for f in sorted(self.files):
                    if f.endswith("/" + tok) or f == base:
                        hit = Hit(f, 0, tok)
                        break
        if hit is None:
            pat = self._pattern(tok)
            for file, text in self.code.items():
                for number, line in enumerate(text.splitlines(), 1):
                    if pat.search(line):
                        hit = Hit(file, number, line.strip())
                        break
                if hit:
                    break
        self._cache[tok] = hit
        return hit

    def find_in_docs(self, tok: str) -> list[Hit]:
        pat = self._pattern(tok)
        hits = []
        for file, text in self.docs.items():
            for number, line in enumerate(text.splitlines(), 1):
                if pat.search(line):
                    hits.append(Hit(file, number, line.strip()))
        return hits

    def find_value_in_code(self, subject: str, value: str) -> Hit | None:
        subj = re.compile(r"(?<![\w-])" + re.escape(subject), re.I)
        val = re.compile(r"(?<!\w)(?<!\d\.)" + re.escape(value) + r"(?!\w|\.\d)")
        for file, text in self.code.items():
            for number, line in enumerate(text.splitlines(), 1):
                if subj.search(line) and val.search(line):
                    return Hit(file, number, line.strip())
        return None

    def find_value_in_docs(self, subject: str, value: str) -> list[Hit]:
        subj = re.compile(r"(?<![\w-])" + re.escape(subject), re.I)
        val = re.compile(r"(?<!\w)(?<!\d\.)" + re.escape(value) + r"(?!\w|\.\d)")
        return [
            Hit(file, number, line.strip())
            for file, text in self.docs.items()
            for number, line in enumerate(text.splitlines(), 1)
            if subj.search(line) and val.search(line)
        ]


# ----------------------------------------------------------------------------- verdicts


@dataclass
class MeetingFinding:
    verdict: str
    kind: str  # rename | remove | change
    file: str  # the doc to edit, or the transcript when no doc is involved
    line: int
    subject: str
    message: str
    evidence: str = ""
    fix: str = ""
    speaker: str = ""
    time: str = ""
    quote: str = ""
    pending: bool = False  # the decision is not in the code yet
    source: str = "rules"
    check: str = "meeting"

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "kind": self.kind,
            "file": self.file,
            "line": self.line,
            "subject": self.subject,
            "message": self.message,
            "evidence": self.evidence,
            "fix": self.fix,
            "speaker": self.speaker,
            "time": self.time,
            "quote": self.quote,
            "pending": self.pending,
            "source": self.source,
        }


@dataclass
class ActionItem:
    owner: str
    text: str
    speaker: str = ""
    time: str = ""


@dataclass
class MeetingReport:
    findings: list[MeetingFinding] = field(default_factory=list)
    actions: list[ActionItem] = field(default_factory=list)
    source: str = ""
    utterances: int = 0
    ai_used: bool = False

    def count(self, verdict: str) -> int:
        return sum(1 for f in self.findings if f.verdict == verdict)

    @property
    def stale_docs(self) -> list[MeetingFinding]:
        return [f for f in self.findings if f.verdict == CONTRADICTED]

    @property
    def pending(self) -> list[MeetingFinding]:
        return [f for f in self.findings if f.pending]

    @property
    def has_contradictions(self) -> bool:
        return bool(self.stale_docs)

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "utterances": self.utterances,
            "ai_used": self.ai_used,
            "counts": {v: self.count(v) for v in (VERIFIED, CONTRADICTED, UNCERTAIN)},
            "findings": [f.to_dict() for f in self.findings],
            "actions": [a.__dict__ for a in self.actions],
        }


_HISTORY_WORDS = re.compile(
    r"\b(removed|deprecated|legacy|no longer|renamed|retired|replaced|formerly|previously|"
    r"obsolete|old|used to|was)\b",
    re.I,
)


def _stale(hits: list[Hit], tok: str = "") -> list[Hit]:
    """Doc mentions that present the thing as current (not as history).

    The identifier itself is blanked out first so a name such as ``/legacy`` is not
    mistaken for the word "legacy".
    """
    return [
        h for h in hits if not _HISTORY_WORDS.search(h.text.replace(tok, " ") if tok else h.text)
    ]


def _quote(s: Statement) -> str:
    text = " ".join(s.text.split())
    return text if len(text) <= 160 else text[:159].rstrip() + "…"


def _who(s: Statement) -> str:
    bits = [b for b in (f"at {s.time}" if s.time else "", s.speaker) if b]
    return "meeting " + ", ".join(bits) if bits else "meeting"


def _ref(hit: Hit) -> str:
    if hit.file.startswith("("):
        return "the API definition"
    return f"{hit.file}:{hit.line}" if hit.line else hit.file


def _where(hits: list[Hit]) -> str:
    return ", ".join(f"{_ref(h)}" for h in hits[:4]) + (" …" if len(hits) > 4 else "")


def verify_statement(s: Statement, corpus: Corpus, source_file: str) -> MeetingFinding | None:
    common = {
        "kind": s.kind,
        "speaker": s.speaker,
        "time": s.time,
        "quote": _quote(s),
        "source": s.source,
    }
    mention = (source_file, s.line)
    if corpus.api_only:
        names = [t for t in (s.old, s.new) if t]
        if s.kind == "change" or any(token_kind(t) != "path" for t in names):
            subject = (
                f"{s.subject}: {s.old} → {s.new}"
                if s.kind == "change"
                else " → ".join(f"`{t}`" for t in names)
            )
            return MeetingFinding(
                UNCERTAIN, file=mention[0], line=mention[1], subject=subject,
                message="Cannot check this decision: only the API definition is available, "
                "not the source code. Run the command-line tool on the repository to check it.",
                evidence=_who(s), **common,
            )  # fmt: skip
    if s.kind == "rename":
        old, new = s.old, s.new
        subject = f"`{old}` → `{new}`"
        in_old, in_new = corpus.find_in_code(old), corpus.find_in_code(new)
        stale = _stale(corpus.find_in_docs(old), old)
        if in_new and not in_old:
            if stale:
                first = stale[0]
                return MeetingFinding(
                    CONTRADICTED, file=first.file, line=first.line, subject=subject,
                    message=f"The team renamed `{old}` to `{new}` and the code uses `{new}`, "
                    f"but the docs still say `{old}`.",
                    evidence=f"{_who(s)}; code: {_ref(in_new)}",
                    fix=f"Replace `{old}` with `{new}` at {_where(stale)}.", **common,
                )  # fmt: skip
            return MeetingFinding(
                VERIFIED, file=mention[0], line=mention[1], subject=subject,
                message=f"Rename `{old}` → `{new}` is in the code and the docs do not use the old name.",
                evidence=f"{_who(s)}; code: {_ref(in_new)}", **common,
            )  # fmt: skip
        if in_old and not in_new:
            where = f" Docs to update when it ships: {_where(stale)}." if stale else ""
            return MeetingFinding(
                UNCERTAIN, file=mention[0], line=mention[1], subject=subject, pending=True,
                message=f"Rename `{old}` → `{new}` is not in the code yet: the code still uses `{old}`.{where}",
                evidence=f"{_who(s)}; code: {_ref(in_old)}",
                fix=f"When the rename ships, update the docs from `{old}` to `{new}`.", **common,
            )  # fmt: skip
        if in_old and in_new:
            return MeetingFinding(
                UNCERTAIN, file=mention[0], line=mention[1], subject=subject, pending=True,
                message=f"Both `{old}` and `{new}` exist in the code: the rename looks half done.",
                evidence=f"{_who(s)}; `{old}` at {_ref(in_old)}, `{new}` at {_ref(in_new)}",
                **common,
            )  # fmt: skip
        return MeetingFinding(
            UNCERTAIN, file=mention[0], line=mention[1], subject=subject,
            message=f"Cannot check the rename: neither `{old}` nor `{new}` was found in the code.",
            evidence=_who(s), **common,
        )  # fmt: skip
    if s.kind == "remove":
        tok = s.old
        subject = f"`{tok}`"
        in_code = corpus.find_in_code(tok)
        stale = _stale(corpus.find_in_docs(tok), tok)
        if not in_code:
            if stale:
                first = stale[0]
                return MeetingFinding(
                    CONTRADICTED, file=first.file, line=first.line, subject=subject,
                    message=f"The team removed {subject} and it is gone from the code, "
                    "but the docs still describe it.",
                    evidence=f"{_who(s)}; searched the code for {subject}",
                    fix=f"Remove or mark as removed: {_where(stale)}.", **common,
                )  # fmt: skip
            return MeetingFinding(
                VERIFIED, file=mention[0], line=mention[1], subject=subject,
                message=f"{subject} is gone from the code and the docs do not present it as current.",
                evidence=_who(s), **common,
            )  # fmt: skip
        where = f" Docs mentioning it: {_where(stale)}." if stale else ""
        return MeetingFinding(
            UNCERTAIN, file=mention[0], line=mention[1], subject=subject, pending=True,
            message=f"Removal of {subject} is not done yet: the code still has it.{where}",
            evidence=f"{_who(s)}; code: {_ref(in_code)}",
            fix=f"When {subject} is removed, delete or mark it as removed in the docs.", **common,
        )  # fmt: skip
    if s.kind == "change":
        subject = f"{s.subject}: {s.old} → {s.new}"
        old_hit = corpus.find_value_in_code(s.subject, s.old)
        new_hit = corpus.find_value_in_code(s.subject, s.new)
        doc_old = corpus.find_value_in_docs(s.subject, s.old)
        if new_hit and not old_hit:
            if doc_old:
                first = doc_old[0]
                return MeetingFinding(
                    CONTRADICTED, file=first.file, line=first.line, subject=subject,
                    message=f"The {s.subject} changed from {s.old} to {s.new} and the code says {s.new}, "
                    f"but the docs still say {s.old}.",
                    evidence=f"{_who(s)}; code: {_ref(new_hit)}",
                    fix=f"Change {s.old} to {s.new} at {_where(doc_old)}.", **common,
                )  # fmt: skip
            return MeetingFinding(
                VERIFIED, file=mention[0], line=mention[1], subject=subject,
                message=f"The new {s.subject} ({s.new}) is in the code and the docs do not use {s.old}.",
                evidence=f"{_who(s)}; code: {_ref(new_hit)}", **common,
            )  # fmt: skip
        if old_hit and not new_hit:
            return MeetingFinding(
                UNCERTAIN, file=mention[0], line=mention[1], subject=subject, pending=True,
                message=f"The {s.subject} change is not in the code yet: it is still {s.old}.",
                evidence=f"{_who(s)}; code: {_ref(old_hit)}",
                fix=f"When it ships, change {s.old} to {s.new} in the docs"
                + (f" ({_where(doc_old)})." if doc_old else "."), **common,
            )  # fmt: skip
        return MeetingFinding(
            UNCERTAIN, file=mention[0], line=mention[1], subject=subject,
            message=f"Cannot check the {s.subject} change: no line in the code ties it to {s.old} or {s.new}.",
            evidence=_who(s), **common,
        )  # fmt: skip
    return None


# ----------------------------------------------------------------------------- optional AI


_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
AI_SYSTEM = (
    "You read meeting notes and list decisions that would change technical documentation. "
    "Reply with JSON only: a list of objects with keys kind (rename, remove or change), "
    "old, new, subject, quote, index. For rename give the exact old and new identifiers "
    "(environment variable, endpoint path, flag, file or function name). For remove give the "
    "identifier in old. For change give the thing in subject and old/new values. "
    "index is the number of the line the decision appeared on. Do not invent identifiers "
    "that are not in the text. If nothing qualifies reply []."
)


def redact(utterances: list[Utterance]) -> list[str]:
    """Numbered lines with speaker names replaced by Speaker N and e-mails removed."""
    names: dict[str, str] = {}
    out = []
    for i, u in enumerate(utterances):
        who = ""
        if u.speaker:
            who = names.setdefault(u.speaker, f"Speaker {len(names) + 1}") + ": "
        out.append(f"{i}: {who}{_EMAIL.sub('[email]', u.text)}")
    return out


def extract_ai(utterances: list[Utterance], client, model: str, max_chars: int = 24_000):
    """Ask a model to propose statements. Returns ``(statements, error)``."""
    body = "\n".join(redact(utterances))[:max_chars]
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": AI_SYSTEM}, {"role": "user", "content": body}],
        )
        raw = response.choices[0].message.content or ""
        raw = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", raw.strip()).strip()
        data = json.loads(raw)
    except Exception as e:  # noqa: BLE001 - the model is optional; never break the report
        return [], f"{type(e).__name__}"
    if not isinstance(data, list):
        return [], "unexpected reply"
    found: list[Statement] = []
    for item in data:
        if not isinstance(item, dict) or item.get("kind") not in ("rename", "remove", "change"):
            continue
        try:
            u = utterances[int(item.get("index", -1))]
        except (ValueError, TypeError, IndexError):
            continue
        old = _clean_token(str(item.get("old") or ""))[:80]
        new = _clean_token(str(item.get("new") or ""))[:80]
        subject = _clean_token(str(item.get("subject") or ""))[:40]
        if not old or (item["kind"] != "remove" and not new):
            continue
        if item["kind"] == "rename" and token_kind(old) != token_kind(new):
            continue
        if item["kind"] == "change" and not subject:
            continue
        # the model may only point at words that really are in the sentence
        if old not in u.text or (item["kind"] == "rename" and new not in u.text):
            continue
        found.append(
            Statement(
                item["kind"],
                text=u.text,
                speaker=u.speaker,
                time=u.time,
                line=u.line,
                old=old,
                new=new if item["kind"] != "remove" else "",
                subject=subject,
                source="ai",
                planned=bool(_FUTURE.search(u.text)),
            )  # fmt: skip
        )
    return found, ""


# ----------------------------------------------------------------------------- entry points


def _dedupe(statements: list[Statement]) -> list[Statement]:
    seen: set[tuple] = set()
    out = []
    for s in statements:
        key = (s.kind, s.old, s.new, s.subject, s.owner)
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def verify_meeting(
    utterances: list[Utterance],
    corpus: Corpus,
    source: str = "meeting",
    client=None,
    model: str = "",
) -> MeetingReport:
    report = MeetingReport(source=source, utterances=len(utterances))
    statements = extract_statements(utterances)
    if client is not None:
        extra, err = extract_ai(utterances, client, model)
        report.ai_used = not err
        statements += extra
    for s in _dedupe(statements):
        if s.kind == "action":
            report.actions.append(ActionItem(s.owner, _quote(s), s.speaker, s.time))
            continue
        finding = verify_statement(s, corpus, source)
        if finding:
            report.findings.append(finding)
    return report


def run(
    transcript: str | os.PathLike,
    root: str | os.PathLike = ".",
    doc_paths: list[str | os.PathLike] | None = None,
    spec_path: str | None = None,
    code_path: str | None = None,
    use_ai: bool = False,
    docs_root: str | os.PathLike | None = None,
) -> MeetingReport:
    """Read *transcript* and check it against the repository at *root*."""
    from api_contract import SpecError, resolve_spec

    root_p = Path(root).resolve()
    t = Path(transcript).resolve()
    spec = None
    try:
        spec = resolve_spec(spec_path, code_path, None, root_p)
    except SpecError:
        spec = None
    corpus = Corpus.from_repo(root_p, doc_paths, spec, skip=[t], docs_root=docs_root)
    client = model = None
    if use_ai and os.environ.get("MODEL_API_BASE"):
        from config import get_client, get_model_name

        client, model = get_client(), get_model_name()
    elif use_ai:
        print("AI extraction skipped: MODEL_API_BASE is not set.", file=sys.stderr)
    try:
        label = t.relative_to(root_p).as_posix()
    except ValueError:
        label = t.name
    try:
        utterances = parse_transcript(t)
    except (zipfile.BadZipFile, KeyError) as e:
        raise ValueError(f"Cannot read the transcript {t.name}: {e}") from e
    return verify_meeting(utterances, corpus, label, client, model or "")


# ----------------------------------------------------------------------------- report

_ICON = {VERIFIED: "✅", CONTRADICTED: "❌", UNCERTAIN: "❓"}


def render_meeting_markdown(report: MeetingReport, max_items: int = 40) -> str:
    out = ["## 🗣️ Meeting follow-ups", ""]
    out.append(
        f"Source: `{report.source}` · {report.utterances} remark(s) read · "
        f"{len(report.findings)} decision(s) checked · {len(report.actions)} action item(s)"
        + (" · decisions also proposed by AI, verdicts from code" if report.ai_used else "")
    )
    out.append("")
    if not report.findings and not report.actions:
        out += [
            "Nothing in these notes looked like a documentation-affecting decision. The checker "
            "looks for statements such as “rename X to Y”, “X is being removed” and "
            "“change the limit from 20 to 50”.",
            "",
        ]
        return "\n".join(out)
    out += [
        "| Verdict | Decisions |",
        "|---|---|",
        f"| ✅ Reflected in code and docs | {report.count(VERIFIED)} |",
        f"| ❌ Docs no longer match | {report.count(CONTRADICTED)} |",
        f"| ❓ Not shipped yet / cannot tell | {report.count(UNCERTAIN)} |",
        "",
    ]

    def block(title: str, items: list[MeetingFinding]) -> None:
        if not items:
            return
        out.append(f"### {title}")
        out.append("")
        for f in items[:max_items]:
            out.append(f"- `{_ref(f)}` {f.message}")
            out.append(f"  - Said: “{f.quote}” ({f.time or 'no time'}, {f.speaker or 'unknown'})")
            if f.evidence:
                out.append(f"  - Evidence: {f.evidence}")
            if f.fix:
                out.append(f"  - Suggested fix: {f.fix}")
        if len(items) > max_items:
            out.append(f"- …and {len(items) - max_items} more")
        out.append("")

    block("❌ Docs that no longer match a decision", report.stale_docs)
    block("⏳ Decisions not shipped yet (pending)", report.pending)
    block(
        "❓ Could not be checked",
        [f for f in report.findings if f.verdict == UNCERTAIN and not f.pending],
    )
    block("✅ Decisions already reflected", [f for f in report.findings if f.verdict == VERIFIED])
    if report.actions:
        out += ["### 📝 Action items about documentation", ""]
        for a in report.actions:
            when = f" ({a.time})" if a.time else ""
            out.append(f"- **{a.owner or 'Someone'}**{when}: {a.text}")
        out.append("")
    records = render_decision_records(report)
    if records:
        out += [
            "<details><summary>🧾 Draft decision records</summary>",
            "",
            records,
            "</details>",
            "",
        ]
    return "\n".join(out)


def render_decision_records(report: MeetingReport) -> str:
    blocks = []
    for f in report.findings:
        status = "Proposed (not shipped)" if f.pending else "Accepted (in the code)"
        if f.verdict == UNCERTAIN and not f.pending:
            continue
        blocks.append(
            "\n".join(
                [
                    f"#### Decision: {f.subject}",
                    f"- **Status:** {status}",
                    f"- **Raised:** {f.time or 'unknown time'}, {f.speaker or 'unknown'} "
                    f"(`{report.source}`)",
                    f"- **Decision:** {f.quote}",
                    f"- **Docs to update:** {f.fix or 'none; the docs already agree'}",
                ]
            )
        )
    return "\n\n".join(blocks)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check meeting notes against code and docs.")
    parser.add_argument("transcript", help="Teams .vtt / .docx transcript or a text file")
    parser.add_argument("docs", nargs="*", help="Docs files or folders (default: whole repo)")
    parser.add_argument("--root", default=".", help="Repository root")
    parser.add_argument("--spec", help="OpenAPI spec (optional; improves endpoint checks)")
    parser.add_argument("--code", help="Folder with API code (optional)")
    parser.add_argument("--ai", action="store_true", help="Also let a model propose decisions")
    parser.add_argument("--json", action="store_true", help="Print JSON")
    args = parser.parse_args(argv)
    try:
        report = run(args.transcript, args.root, args.docs or None, args.spec, args.code, args.ai)
    except (OSError, ValueError) as e:
        print(f"Error: cannot read the transcript: {e}", file=sys.stderr)
        return 2
    print(json.dumps(report.to_dict(), indent=2) if args.json else render_meeting_markdown(report))
    return 1 if report.has_contradictions else 0


if __name__ == "__main__":
    sys.exit(main())
