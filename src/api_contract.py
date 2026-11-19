"""
API Contract Verifier.

Compares what the *documentation* says about an HTTP API (endpoints, methods,
parameters, response codes) with what the *OpenAPI specification* says, and
reports every check as one of three verdicts:

* ``verified``      - the docs agree with the spec.
* ``contradicted``  - the docs disagree with the spec (evidence is quoted).
* ``uncertain``     - the tool cannot tell (unresolved ``$ref``, a possible
                      deployment prefix, an ``default`` response, ...).

The core is fully deterministic: no LLM and no network access is needed, so
results are reproducible and cheap. For FastAPI projects the spec is simply
``app.openapi()``; use :func:`load_spec_from_fastapi` locally, or commit an
``openapi.json`` for CI.

Command line::

    python src/api_contract.py --spec openapi.json --docs docs README.md
    python src/api_contract.py --app myproject.main:app --docs docs
"""

from __future__ import annotations

import argparse
import difflib
import importlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

VERIFIED = "verified"
CONTRADICTED = "contradicted"
UNCERTAIN = "uncertain"

HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
_METHODS_RE = "|".join(m.upper() for m in HTTP_METHODS)

DOC_EXTENSIONS = (".md", ".mdx", ".markdown")
_SKIP_DIRS = {".git", "node_modules", "venv", ".venv", "__pycache__", ".tox", "site-packages"}

SPEC_CANDIDATES = (
    "openapi.json",
    "openapi.yaml",
    "openapi.yml",
    "docs/openapi.json",
    "docs/openapi.yaml",
    "docs/openapi.yml",
    "api/openapi.json",
    "api/openapi.yaml",
)


class SpecError(Exception):
    """Raised when the OpenAPI spec cannot be loaded."""


# ---------------------------------------------------------------------------
# Spec model
# ---------------------------------------------------------------------------


@dataclass
class SpecParam:
    name: str
    location: str  # query | path | header | cookie | body
    required: bool
    types: frozenset[str] = frozenset()  # empty = unknown
    pointer: str = ""


@dataclass
class Operation:
    method: str  # upper-case
    path: str  # as written in the spec
    params: list[SpecParam] = field(default_factory=list)
    responses: set[str] = field(default_factory=set)
    unresolved: bool = False  # something could not be resolved ($ref to elsewhere)
    pointer: str = ""


def _pointer_escape(part: str) -> str:
    return part.replace("~", "~0").replace("/", "~1")


def normalize_path(path: str) -> str:
    """Normalise a URL path so ``{id}``, ``:id`` and ``<int:id>`` compare equal."""
    path = path.split("?", 1)[0].split("#", 1)[0].strip()
    if not path.startswith("/"):
        path = "/" + path
    path = re.sub(r"\{[^}/]*\}", "{}", path)
    path = re.sub(r"<[^>/]*>", "{}", path)
    path = re.sub(r"(?<=/):[A-Za-z_][\w]*", "{}", path)
    if len(path) > 1:
        path = path.rstrip("/")
    return path or "/"


def _param_names_in_path(path: str) -> list[str]:
    names = re.findall(r"\{([^}/]*)\}", path)
    names += [n.split(":")[-1] for n in re.findall(r"<([^>/]*)>", path)]
    names += re.findall(r"(?<=/):([A-Za-z_]\w*)", path)
    return names


class Spec:
    """A parsed OpenAPI (3.x) or Swagger (2.0) document."""

    def __init__(self, data: dict, source: str = "openapi"):
        if not isinstance(data, dict) or not isinstance(data.get("paths"), dict):
            raise SpecError("Not an OpenAPI document: missing top-level 'paths'.")
        self.data = data
        self.source = source
        self.base_paths = self._base_paths()
        self.operations: dict[tuple[str, str], Operation] = {}
        self._index: dict[str, str] = {}  # normalized path -> original spec path
        self._build()

    # -- construction ------------------------------------------------------

    def _base_paths(self) -> list[str]:
        bases: list[str] = []
        for server in self.data.get("servers") or []:
            if isinstance(server, dict) and server.get("url"):
                p = urlparse(str(server["url"])).path.rstrip("/")
                if p and p not in bases:
                    bases.append(p)
        bp = self.data.get("basePath")  # Swagger 2
        if isinstance(bp, str) and bp.rstrip("/") and bp.rstrip("/") not in bases:
            bases.append(bp.rstrip("/"))
        return bases

    def resolve_ref(self, node, _depth: int = 0):
        """Resolve local ``$ref`` chains. Returns ``(node, ok)``."""
        while isinstance(node, dict) and "$ref" in node:
            ref = node["$ref"]
            if not isinstance(ref, str) or not ref.startswith("#/") or _depth > 20:
                return node, False
            target = self.data
            for part in ref[2:].split("/"):
                part = part.replace("~1", "/").replace("~0", "~")
                if isinstance(target, dict) and part in target:
                    target = target[part]
                else:
                    return node, False
            node = target
            _depth += 1
        return node, True

    def _schema_types(self, schema) -> tuple[frozenset[str], bool]:
        schema, ok = self.resolve_ref(schema)
        if not ok or not isinstance(schema, dict):
            return frozenset(), False
        types: set[str] = set()
        t = schema.get("type")
        if isinstance(t, str):
            types.add(t)
        elif isinstance(t, list):
            types.update(x for x in t if isinstance(x, str))
        elif "properties" in schema:
            types.add("object")
        for key in ("anyOf", "oneOf"):
            for sub in schema.get(key) or []:
                sub_types, sub_ok = self._schema_types(sub)
                if not sub_ok:
                    return frozenset(), False
                types |= sub_types
        types.discard("null")
        return frozenset(types), True

    def _body_properties(self, schema, depth: int = 0) -> tuple[dict[str, tuple], set[str], bool]:
        """Return ({name: (types, pointer-less)}, required_names, fully_resolved)."""
        schema, ok = self.resolve_ref(schema)
        if not ok or not isinstance(schema, dict) or depth > 10:
            return {}, set(), False
        props: dict[str, tuple] = {}
        required: set[str] = set(schema.get("required") or [])
        resolved = True
        for name, sub in (schema.get("properties") or {}).items():
            types, _ = self._schema_types(sub)
            props[name] = (types,)
        for sub in schema.get("allOf") or []:
            p, r, ok2 = self._body_properties(sub, depth + 1)
            props.update(p)
            required |= r
            resolved = resolved and ok2
        return props, required, resolved

    def _build(self) -> None:
        for raw_path, item in self.data["paths"].items():
            item, ok = self.resolve_ref(item)
            if not isinstance(item, dict):
                continue
            norm = normalize_path(raw_path)
            self._index.setdefault(norm, raw_path)
            path_params = item.get("parameters") or []
            for method in HTTP_METHODS:
                op = item.get(method)
                if not isinstance(op, dict):
                    continue
                pointer = f"#/paths/{_pointer_escape(raw_path)}/{method}"
                operation = Operation(method.upper(), raw_path, pointer=pointer)
                operation.unresolved = not ok
                merged: dict[tuple[str, str], tuple[dict, str]] = {}
                for i, p in enumerate(path_params):
                    pr, pok = self.resolve_ref(p)
                    if not pok or not isinstance(pr, dict):
                        operation.unresolved = True
                        continue
                    merged[(pr.get("name", ""), pr.get("in", ""))] = (
                        pr,
                        f"#/paths/{_pointer_escape(raw_path)}/parameters/{i}",
                    )
                for i, p in enumerate(op.get("parameters") or []):
                    pr, pok = self.resolve_ref(p)
                    if not pok or not isinstance(pr, dict):
                        operation.unresolved = True
                        continue
                    merged[(pr.get("name", ""), pr.get("in", ""))] = (
                        pr,
                        f"{pointer}/parameters/{i}",
                    )
                for (name, loc), (pr, ptr) in merged.items():
                    types, tok = self._schema_types(pr.get("schema") or {"type": pr.get("type")})
                    operation.params.append(
                        SpecParam(
                            name=name,
                            location=loc,
                            required=bool(pr.get("required", loc == "path")),
                            types=types if tok else frozenset(),
                            pointer=ptr,
                        )
                    )
                self._add_body_params(op, operation, pointer)
                for code in (op.get("responses") or {}).keys():
                    operation.responses.add(str(code).upper())
                self.operations[(method.upper(), norm)] = operation

    def _add_body_params(self, op: dict, operation: Operation, pointer: str) -> None:
        body = op.get("requestBody")
        if body is None:
            # Swagger 2: in=body parameter
            for p in op.get("parameters") or []:
                pr, _ = self.resolve_ref(p)
                if isinstance(pr, dict) and pr.get("in") == "body" and "schema" in pr:
                    self._collect_body(pr["schema"], operation, f"{pointer}/parameters")
            return
        body, ok = self.resolve_ref(body)
        if not ok or not isinstance(body, dict):
            operation.unresolved = True
            return
        content = body.get("content") or {}
        for media, media_obj in content.items():
            if "json" in media and isinstance(media_obj, dict) and "schema" in media_obj:
                self._collect_body(media_obj["schema"], operation, f"{pointer}/requestBody")
                return
        for media_obj in content.values():
            if isinstance(media_obj, dict) and "schema" in media_obj:
                self._collect_body(media_obj["schema"], operation, f"{pointer}/requestBody")
                return

    def _collect_body(self, schema, operation: Operation, pointer: str) -> None:
        props, required, resolved = self._body_properties(schema)
        if not resolved:
            operation.unresolved = True
        for name, (types,) in props.items():
            operation.params.append(
                SpecParam(name, "body", name in required, types, f"{pointer}/properties/{name}")
            )

    # -- lookup ------------------------------------------------------------

    @property
    def paths(self) -> list[str]:
        return list(self._index.values())

    def methods_for(self, norm_path: str) -> list[str]:
        return sorted(m for (m, p) in self.operations if p == norm_path)

    def spec_path_for(self, norm_path: str) -> str:
        return self._index.get(norm_path, norm_path)

    def find_path(self, doc_path: str) -> tuple[str | None, str]:
        """Locate a documented path in the spec.

        Returns ``(normalized spec path | None, how)`` where *how* is one of
        ``exact``, ``example`` (concrete values in place of placeholders),
        ``prefix`` (a server base path was stripped), ``suffix`` (docs path has
        an unknown extra prefix) or ``none``.
        """
        norm = normalize_path(doc_path)
        if norm in self._index:
            return norm, "exact"
        found = self._match_template(norm)
        if found:
            return found, "example"
        for base in self.base_paths:
            if norm == base or norm.startswith(base + "/"):
                stripped = normalize_path(norm[len(base) :] or "/")
                if stripped in self._index:
                    return stripped, "prefix"
                found = self._match_template(stripped)
                if found:
                    return found, "prefix"
        # Docs path carries an unknown prefix (e.g. /api/v1) that the spec lacks.
        best = None
        for cand in self._index:
            if cand != "/" and norm.endswith(cand) and norm != cand:
                if best is None or len(cand) > len(best):
                    best = cand
        if best:
            return best, "suffix"
        return None, "none"

    def _match_template(self, norm: str) -> str | None:
        segs = norm.strip("/").split("/")
        best: tuple[int, str] | None = None
        for cand in self._index:
            csegs = cand.strip("/").split("/")
            if len(csegs) != len(segs):
                continue
            literal = 0
            ok = True
            for c, d in zip(csegs, segs, strict=True):
                if c == "{}":
                    continue
                if c != d:
                    ok = False
                    break
                literal += 1
            if ok and (best is None or literal > best[0]):
                best = (literal, cand)
        return best[1] if best else None

    def suggest(self, doc_path: str) -> str | None:
        norm = normalize_path(doc_path)
        close = difflib.get_close_matches(norm, list(self._index), n=1, cutoff=0.75)
        return self.spec_path_for(close[0]) if close else None


def load_spec(path: str | os.PathLike) -> Spec:
    """Load an OpenAPI spec from a JSON or YAML file."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as e:
        raise SpecError(f"Cannot read spec '{p}': {e}") from e
    if p.suffix.lower() == ".json":
        try:
            return Spec(json.loads(text), source=p.name)
        except json.JSONDecodeError as e:
            raise SpecError(f"Spec '{p}' is not valid JSON: {e}") from e
    try:
        import yaml
    except ImportError as e:  # pragma: no cover - PyYAML ships with the image
        raise SpecError("PyYAML is required to read YAML specs.") from e
    try:
        return Spec(yaml.safe_load(text), source=p.name)
    except yaml.YAMLError as e:
        raise SpecError(f"Spec '{p}' is not valid YAML: {e}") from e


def load_spec_from_fastapi(target: str, source: str = "app.openapi()") -> Spec:
    """Build the spec from a FastAPI app given as ``package.module:attr``.

    NOTE: this imports (executes) the target project's code, so use it only on
    code you trust - it is intended for local runs, not for the GitHub Action.
    """
    module_name, _, attr = target.partition(":")
    if not module_name or not attr:
        raise SpecError("Use the form 'package.module:app' for --app.")
    sys.path.insert(0, os.getcwd())
    try:
        module = importlib.import_module(module_name)
        app = getattr(module, attr)
        return Spec(app.openapi(), source=source)
    except SpecError:
        raise
    except Exception as e:
        raise SpecError(f"Could not build the spec from '{target}': {e}") from e


def find_spec_file(root: str | os.PathLike = ".") -> Path | None:
    for candidate in SPEC_CANDIDATES:
        p = Path(root) / candidate
        if p.is_file():
            return p
    return None


# ---------------------------------------------------------------------------
# Documentation claims
# ---------------------------------------------------------------------------


@dataclass
class DocParam:
    name: str
    location: str | None = None
    type: str | None = None  # normalised: string/integer/number/boolean/array/object
    required: bool | None = None


@dataclass
class Claim:
    method: str
    raw_path: str
    file: str
    line: int
    params: list[DocParam] = field(default_factory=list)
    statuses: list[str] = field(default_factory=list)

    @property
    def path(self) -> str:
        return normalize_path(self.raw_path)

    @property
    def label(self) -> str:
        return f"{self.method} {self.raw_path}"


_ENDPOINT_RE = re.compile(
    rf"(?<![A-Za-z0-9_])(?P<method>{_METHODS_RE})[\s|`*_]*"
    r"(?P<path>/[A-Za-z0-9_\-./{}:<>~%@$!*+,;=]*)(?:\?(?P<query>[A-Za-z0-9_\-.=&\[\]%{}]*))?"
)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_STATUS_CELL_RE = re.compile(r"^\W*([1-5]\d\d|[1-5]XX)\W*$", re.I)
_STATUS_KEYWORD_RE = re.compile(
    r"\b(?:returns?|responds?\s+with|responses?|status(?:\s+code)?|http)\b", re.I
)
_STATUS_CODE_RE = re.compile(
    r"(?<![\w./-])`?([2-5]\d\d)`?(?![\w.]*\d)(?!\s*(?:items?|results?|records?|rows?|entries|users?|"
    r"objects?|bytes?|kb|mb|chars?|characters?|seconds?|secs?|ms|requests?|per\b|times?))",
    re.I,
)
_BULLET_RE = re.compile(r"^\s*[-*+]\s+`?\*{0,2}([A-Za-z_][\w.\-\[\]]*)\*{0,2}`?\s*(?:\(([^)]*)\))?")

_TYPE_ALIASES = {
    "str": "string",
    "string": "string",
    "text": "string",
    "uuid": "string",
    "date": "string",
    "datetime": "string",
    "int": "integer",
    "integer": "integer",
    "long": "integer",
    "number": "number",
    "float": "number",
    "double": "number",
    "decimal": "number",
    "bool": "boolean",
    "boolean": "boolean",
    "list": "array",
    "array": "array",
    "object": "object",
    "dict": "object",
    "json": "object",
    "map": "object",
}
_LOCATIONS = {
    "query": "query",
    "path": "path",
    "header": "header",
    "cookie": "cookie",
    "body": "body",
    "json": "body",
    "form": "body",
    "formdata": "body",
}


def _prose_statuses(line: str) -> list[str]:
    """Status codes mentioned after words like "returns" / "status" in one sentence."""
    codes: list[str] = []
    for kw in _STATUS_KEYWORD_RE.finditer(line):
        sentence = re.split(r"\.(?:\s|$)", line[kw.end() :], maxsplit=1)[0]
        for code in _STATUS_CODE_RE.findall(sentence):
            if code not in codes:
                codes.append(code)
    return codes


def _clean(cell: str) -> str:
    return re.sub(r"[`*_]", "", cell).strip()


def _parse_type(text: str | None) -> str | None:
    if not text:
        return None
    m = re.match(r"[a-z]+", _clean(text).lower())
    return _TYPE_ALIASES.get(m.group(0)) if m else None


def _parse_location(text: str | None) -> str | None:
    if not text:
        return None
    return _LOCATIONS.get(re.sub(r"[^a-z]", "", _clean(text).lower()))


def _parse_required(text: str | None) -> bool | None:
    if text is None:
        return None
    t = _clean(text).lower()
    if not t:
        return None
    if t in {"yes", "y", "true", "required", "✓", "✔", "x", "mandatory"} or t.startswith(
        "required"
    ):
        return True
    if t in {"no", "n", "false", "optional", "-", "—", "✗"} or t.startswith("optional"):
        return False
    return None


def _split_row(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _is_separator(line: str) -> bool:
    return bool(re.match(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$", line))


def _find_endpoints(line: str) -> list[tuple[str, str, str | None]]:
    """Return ``(METHOD, path, query)`` triples mentioned on a line."""
    found = []
    for m in _ENDPOINT_RE.finditer(line):
        path = m.group("path").rstrip(".,;:)!")
        if path:
            found.append((m.group("method"), path, m.group("query")))
    if not found and line.lstrip().startswith("|"):
        cells = [_clean(c) for c in _split_row(line)]
        methods = [c for c in cells if c in {m.upper() for m in HTTP_METHODS}]
        paths = [c for c in cells if c.startswith("/")]
        if methods and paths:
            found.append((methods[0], paths[0].split()[0].rstrip(".,;:)"), None))
    return found


def _strip_fences(lines: list[str]) -> list[str | None]:
    """Blank out fenced code (kept as ``None``) so it never produces claims."""
    out: list[str | None] = []
    in_fence = False
    for line in lines:
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            out.append(None)
        else:
            out.append(None if in_fence else line)
    return out


def extract_claims(text: str, file: str = "docs.md") -> list[Claim]:
    """Extract endpoint claims (with parameters and status codes) from Markdown."""
    lines = _strip_fences(text.splitlines())
    # Locate endpoint mentions: (line index, level, endpoints)
    mentions: list[tuple[int, int, list[tuple[str, str, str | None]]]] = []
    headings: list[tuple[int, int, bool]] = []  # (idx, level, has_endpoint)
    for i, line in enumerate(lines):
        if line is None:
            continue
        hm = _HEADING_RE.match(line)
        eps = _find_endpoints(line)
        if hm:
            headings.append((i, len(hm.group(1)), bool(eps)))
            if eps:
                mentions.append((i, len(hm.group(1)), eps))
            continue
        if not eps:
            continue
        stripped = re.sub(r"^[\s*_`]+", "", line)
        leading = bool(re.match(rf"({_METHODS_RE})\b", stripped)) and not line.lstrip().startswith(
            "|"
        )
        mentions.append((i, 7 if leading else 99, eps))  # 99 = single-line mention

    claims: list[Claim] = []
    for n, (idx, level, eps) in enumerate(mentions):
        end = idx + 1
        if level != 99:
            nxt_mention = mentions[n + 1][0] if n + 1 < len(mentions) else len(lines)
            end = nxt_mention
            for h_idx, h_level, _ in headings:
                if h_idx > idx and h_level <= level:
                    end = min(end, h_idx)
                    break
            block = lines[idx + 1 : end]
        else:
            block = []
        for method, path, query in eps:
            claim = Claim(method, path, file, idx + 1)
            if query:
                for part in query.split("&"):
                    name = part.split("=", 1)[0]
                    if name and re.match(r"^[A-Za-z_][\w.\-\[\]]*$", name):
                        claim.params.append(DocParam(name, "query"))
            _parse_block(block, claim)
            for code in _prose_statuses(_ENDPOINT_RE.sub(" ", lines[idx] or "")):
                if code not in claim.statuses:
                    claim.statuses.append(code)
            claims.append(claim)
    return _merge_duplicates(claims)


def _parse_block(block: list[str | None], claim: Claim) -> None:
    section = None  # "params" | "responses" | None
    i = 0
    while i < len(block):
        line = block[i]
        if line is None:
            i += 1
            continue
        hm = _HEADING_RE.match(line)
        label = None
        if hm:
            label = hm.group(2)
        elif re.match(r"^\s*(\*\*|__)[^*_]+(\*\*|__):?\s*$", line) or re.match(
            r"^\s*[A-Za-z ]{3,30}:\s*$", line
        ):
            label = line
        if label is not None:
            low = label.lower()
            if re.search(r"respon|return|status|error", low):
                section = "responses"
            elif re.search(r"param|query|body|request|argument|field|header", low):
                section = "params"
            else:
                section = None
            i += 1
            continue
        if line.lstrip().startswith("|") and i + 1 < len(block) and block[i + 1] is not None:
            if _is_separator(block[i + 1] or ""):
                j = i + 2
                rows = []
                while j < len(block) and block[j] and (block[j] or "").lstrip().startswith("|"):
                    rows.append(_split_row(block[j] or ""))
                    j += 1
                _parse_table(_split_row(line), rows, section, claim)
                i = j
                continue
        bm = _BULLET_RE.match(line)
        if bm and (section == "params" or (section is None and _looks_like_param(bm.group(2)))):
            _add_bullet_param(bm.group(1), bm.group(2), claim)
        elif bm and section == "responses":
            m = re.match(r"^\s*[-*+]\s+`?\*{0,2}([1-5]\d\d)\b", line)
            if m and m.group(1) not in claim.statuses:
                claim.statuses.append(m.group(1))
        else:
            sm = re.match(r"^\s*[-*+]\s+`?\*{0,2}([1-5]\d\d)\b", line)
            if sm and section == "responses" and sm.group(1) not in claim.statuses:
                claim.statuses.append(sm.group(1))
            else:
                for code in _prose_statuses(line):
                    if code not in claim.statuses:
                        claim.statuses.append(code)
        i += 1


def _looks_like_param(attrs: str | None) -> bool:
    """True when the parenthesised text of a bullet reads like ``(string, required)``."""
    tokens = [t.strip().lower() for t in re.split(r"[,;/]", attrs or "") if t.strip()]
    if not tokens:
        return False
    return all(
        t.startswith(("required", "optional")) or t in _LOCATIONS or _parse_type(t) is not None
        for t in tokens
    )


def _add_bullet_param(name: str, attrs: str | None, claim: Claim) -> None:
    if name.lower() in {"note", "example", "returns", "default"}:
        return
    param = DocParam(name)
    for token in re.split(r"[,;/]", attrs or ""):
        token = token.strip().lower()
        if not token:
            continue
        if token.startswith("required"):
            param.required = True
        elif token.startswith("optional"):
            param.required = False
        elif _parse_location(token) and token.split()[0] in _LOCATIONS:
            param.location = _parse_location(token)
        elif _parse_type(token) and param.type is None:
            param.type = _parse_type(token)
    claim.params.append(param)


def _parse_table(header: list[str], rows: list[list[str]], section, claim: Claim) -> None:
    h = [_clean(c).lower() for c in header]

    def col(*names):
        for n in names:
            for i, c in enumerate(h):
                if c == n:
                    return i
        return None

    if any(c in {"method", "endpoint", "path", "url", "route"} for c in h) and not any(
        c in {"name", "parameter", "param", "field", "property"} for c in h
    ):
        return  # an endpoint listing table, not parameters/responses
    name_i = col("name", "parameter", "param", "field", "property", "key", "argument")
    status_i = col("status", "status code", "code", "http status", "http code")
    if name_i is not None:
        type_i = col("type", "data type", "datatype")
        req_i = next((i for i, c in enumerate(h) if "required" in c or c == "optional"), None)
        loc_i = col("in", "location", "where", "sent in", "param type", "parameter type")
        for row in rows:
            if name_i >= len(row):
                continue
            raw = _clean(row[name_i])
            starred = re.sub(r"[`_]", "", row[name_i]).strip()
            is_bold = bool(re.match(r"^\*+[^*]+\*+$", starred))
            nm = re.match(r"[A-Za-z_][\w.\-\[\]]*", raw)
            if not nm:
                continue
            required = (
                _parse_required(row[req_i]) if req_i is not None and req_i < len(row) else None
            )
            if required is None and raw.endswith("?"):
                required = False
            if required is None and starred.endswith("*") and not is_bold:
                required = True
            if req_i is not None and "optional" in h[req_i]:
                required = None if required is None else (not required)
            claim.params.append(
                DocParam(
                    nm.group(0),
                    _parse_location(row[loc_i]) if loc_i is not None and loc_i < len(row) else None,
                    _parse_type(row[type_i]) if type_i is not None and type_i < len(row) else None,
                    required,
                )
            )
    elif status_i is not None or section == "responses":
        idx = status_i if status_i is not None else 0
        for row in rows:
            if idx < len(row):
                m = _STATUS_CELL_RE.match(_clean(row[idx]))
                if m and m.group(1).upper() not in claim.statuses:
                    claim.statuses.append(m.group(1).upper())


def _merge_duplicates(claims: list[Claim]) -> list[Claim]:
    merged: dict[tuple[str, str], Claim] = {}
    for c in claims:
        key = (c.method, c.path)
        first = merged.get(key)
        if first is None:
            merged[key] = c
            continue
        known = {p.name for p in first.params}
        first.params += [p for p in c.params if p.name not in known]
        first.statuses += [s for s in c.statuses if s not in first.statuses]
    return list(merged.values())


def collect_doc_files(paths: list[str | os.PathLike]) -> list[Path]:
    files: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_file() and p.suffix.lower() in DOC_EXTENSIONS:
            files.append(p)
        elif p.is_dir():
            for root, dirs, names in os.walk(p):
                dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS)
                files += [
                    Path(root) / n for n in sorted(names) if n.lower().endswith(DOC_EXTENSIONS)
                ]
    return files


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


@dataclass
class Finding:
    verdict: str
    claim: Claim
    check: str  # endpoint | parameter | response | example | example-response
    message: str
    evidence: str = ""
    fix: str = ""  # deterministic suggestion for the docs
    ai_fix: str = ""  # optional LLM-written rewrite (always shown as AI-generated)


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)
    undocumented_operations: list[str] = field(default_factory=list)
    missing_required_params: list[str] = field(default_factory=list)
    claims_checked: int = 0
    spec_source: str = ""
    docs_root: str = "."  # where doc files live, used to re-read snippets for AI rewrites

    def count(self, verdict: str) -> int:
        return sum(1 for f in self.findings if f.verdict == verdict)

    @property
    def health(self):
        from health import compute_health

        return compute_health(self.findings)

    @property
    def has_contradictions(self) -> bool:
        return self.count(CONTRADICTED) > 0

    def to_dict(self) -> dict:
        return {
            "spec": self.spec_source,
            "claims_checked": self.claims_checked,
            "summary": {v: self.count(v) for v in (VERIFIED, CONTRADICTED, UNCERTAIN)},
            "health": self.health.to_dict(),
            "findings": [
                {
                    "verdict": f.verdict,
                    "check": f.check,
                    "claim": f.claim.label,
                    "file": f.claim.file,
                    "line": f.claim.line,
                    "message": f.message,
                    "evidence": f.evidence,
                    "fix": f.fix,
                    "ai_fix": f.ai_fix,
                }
                for f in self.findings
            ],
            "undocumented_operations": self.undocumented_operations,
            "missing_required_params": self.missing_required_params,
        }


def _types_compatible(doc_type: str, spec_types: frozenset[str]) -> bool:
    if not spec_types:
        return True
    if doc_type in spec_types:
        return True
    return {doc_type} <= {"integer", "number"} and bool(spec_types & {"integer", "number"})


def verify_claim(claim: Claim, spec: Spec) -> tuple[list[Finding], Operation | None]:
    findings: list[Finding] = []

    def add(verdict, check, message, evidence="", fix=""):
        if evidence.startswith("#"):
            evidence = f"{spec.source}{evidence}"
        findings.append(Finding(verdict, claim, check, message, evidence, fix))

    norm, how = spec.find_path(claim.raw_path)
    if norm is None:
        hint = spec.suggest(claim.raw_path)
        msg = f"The spec does not define the path `{claim.raw_path}`."
        if hint:
            msg += f" Closest path in the spec: `{hint}`."
        fix = (
            f"Change `{claim.raw_path}` to `{hint}`."
            if hint
            else f"Remove `{claim.method} {claim.raw_path}` from the docs, or add the endpoint to the API."
        )
        add(
            CONTRADICTED,
            "endpoint",
            msg,
            f"{spec.source} (searched {len(spec.paths)} paths)",
            fix,
        )
        return findings, None

    spec_path = spec.spec_path_for(norm)
    op = spec.operations.get((claim.method, norm))
    if how == "suffix":
        add(
            UNCERTAIN,
            "endpoint",
            f"`{claim.raw_path}` ends with the spec path `{spec_path}`; it may include a "
            "deployment prefix the spec does not list, so it cannot be confirmed.",
            f"#/paths/{_pointer_escape(spec_path)}",
        )
        return findings, op
    if op is None:
        defined = ", ".join(spec.methods_for(norm)) or "no operations"
        add(
            CONTRADICTED,
            "endpoint",
            f"Docs say `{claim.method}` but the spec defines {defined} for `{spec_path}`.",
            f"#/paths/{_pointer_escape(spec_path)}",
            f"Use one of {defined} for `{spec_path}` in the docs."
            if spec.methods_for(norm)
            else f"Remove `{spec_path}` from the docs or define it in the API.",
        )
        return findings, None

    note = ""
    if how == "example":
        note = " (docs use example values in place of path placeholders)"
    elif how == "prefix":
        note = " (after removing the server base path)"
    doc_names = _param_names_in_path(claim.raw_path)
    spec_names = _param_names_in_path(spec_path)
    if how == "exact" and doc_names and spec_names and doc_names != spec_names:
        spec_list = ", ".join(f"`{n}`" for n in spec_names)
        doc_list = ", ".join(f"`{n}`" for n in doc_names)
        note += f" (path parameter is named {spec_list} in the spec but {doc_list} in the docs)"
    add(VERIFIED, "endpoint", f"`{claim.method} {spec_path}` exists in the spec{note}.", op.pointer)

    for dp in claim.params:
        candidates = [
            p
            for p in op.params
            if p.name == dp.name and (dp.location is None or p.location == dp.location)
        ]
        evidence = op.pointer
        if not candidates:
            if dp.name in {n for n in doc_names} or dp.name in spec_names:
                continue  # path placeholders are already covered by the path match
            others = [p for p in op.params if p.name == dp.name]
            if others:
                o = others[0]
                add(
                    CONTRADICTED,
                    "parameter",
                    f"Parameter `{dp.name}` is documented as `{dp.location}` but the spec "
                    f"has it in `{o.location}`.",
                    o.pointer,
                    f"Say that `{dp.name}` is sent in the {o.location}.",
                )
            elif op.unresolved:
                add(
                    UNCERTAIN,
                    "parameter",
                    f"Parameter `{dp.name}` was not found, but parts of this operation could "
                    "not be resolved (external or broken `$ref`).",
                    evidence,
                )
            else:
                known = ", ".join(f"`{p.name}`" for p in op.params) or "none"
                add(
                    CONTRADICTED,
                    "parameter",
                    f"Parameter `{dp.name}` is documented but not in the spec for "
                    f"`{claim.method} {spec_path}`. Spec parameters: {known}.",
                    evidence,
                    f"Remove `{dp.name}` from the docs, or add it to the API if it should exist.",
                )
            continue
        sp = candidates[0]
        problems = []
        fixes = []
        if dp.required is not None and dp.required != sp.required:
            problems.append(
                f"docs say {'required' if dp.required else 'optional'}, spec says "
                f"{'required' if sp.required else 'optional'}"
            )
            fixes.append(f"mark `{dp.name}` as {'required' if sp.required else 'optional'}")
        if dp.type and not _types_compatible(dp.type, sp.types):
            spec_type = "/".join(sorted(sp.types))
            problems.append(f"docs say type `{dp.type}`, spec says `{spec_type}`")
            fixes.append(f"change the type of `{dp.name}` to `{spec_type}`")
        if problems:
            fix = fixes[0].capitalize() + "".join(f"; {f}" for f in fixes[1:]) + "."
            add(
                CONTRADICTED,
                "parameter",
                f"Parameter `{dp.name}`: " + "; ".join(problems) + ".",
                sp.pointer,
                fix,
            )
        else:
            add(
                VERIFIED,
                "parameter",
                f"Parameter `{dp.name}` ({sp.location}) matches the spec.",
                sp.pointer,
            )

    for code in claim.statuses:
        code_u = code.upper()
        family = code_u[0] + "XX"
        if code_u in op.responses or family in op.responses:
            add(
                VERIFIED,
                "response",
                f"Response `{code}` is defined in the spec.",
                f"{op.pointer}/responses",
            )
        elif "DEFAULT" in op.responses:
            add(
                UNCERTAIN,
                "response",
                f"Response `{code}` is not listed, but the spec has a `default` response that "
                "may cover it.",
                f"{op.pointer}/responses",
            )
        else:
            listed = ", ".join(f"`{c}`" for c in sorted(op.responses)) or "none"
            add(
                CONTRADICTED,
                "response",
                f"Response `{code}` is documented but the spec only defines {listed}.",
                f"{op.pointer}/responses",
                f"Remove `{code}` from the docs, or add that response to the API.",
            )
    return findings, op


def verify(claims: list[Claim], spec: Spec) -> Report:
    report = Report(claims_checked=len(claims), spec_source=spec.source)
    documented: set[tuple[str, str]] = set()
    for claim in claims:
        findings, op = verify_claim(claim, spec)
        report.findings += findings
        if op is not None:
            documented.add((op.method, normalize_path(op.path)))
            doc_names = {p.name for p in claim.params}
            for sp in op.params:
                if sp.required and sp.location != "path" and sp.name not in doc_names:
                    report.missing_required_params.append(
                        f"`{op.method} {op.path}`: required {sp.location} parameter "
                        f"`{sp.name}` is not documented (docs: {claim.file}:{claim.line})"
                    )
    for (method, norm), op in sorted(spec.operations.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        if (method, norm) not in documented:
            report.undocumented_operations.append(f"`{method} {op.path}`")
    return report


def verify_docs(
    spec: Spec, doc_paths: list[str | os.PathLike], root: str | os.PathLike = "."
) -> Report:
    """Convenience wrapper: read docs, extract claims, verify them."""
    claims: list[Claim] = []
    root_p = Path(root).resolve()
    for f in collect_doc_files(doc_paths):
        try:
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            rel = str(f.resolve().relative_to(root_p))
        except ValueError:
            rel = str(f)
        claims += extract_claims(text, rel.replace("\\", "/"))
    report = verify(claims, spec)
    report.docs_root = str(root_p)
    return report


def apply_ai_rewrites(report: Report, max_items: int = 10) -> int:
    """Ask the configured model to draft rewrites for contradictions (optional).

    Needs ``MODEL_API_BASE`` (and usually ``MODEL_API_KEY`` / ``MODEL_NAME``), the same
    settings the rest of the action uses. Returns how many rewrites were added.
    """
    if not os.environ.get("MODEL_API_BASE"):
        print("AI rewrites skipped: MODEL_API_BASE is not set.")
        return 0
    from config import get_client, get_model_name
    from suggestions import refine_fixes

    root = Path(report.docs_root)

    def read_text(rel: str) -> str:
        return (root / rel).read_text(encoding="utf-8")

    return refine_fixes(report, get_client(), get_model_name(), read_text, max_items)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

_ICON = {VERIFIED: "✅", CONTRADICTED: "❌", UNCERTAIN: "❓"}


def render_markdown(report: Report, max_items: int = 50, history: list[dict] | None = None) -> str:
    out = ["## 🔎 API Contract Verification", ""]
    if report.claims_checked == 0:
        out += [
            f"No endpoint claims such as `GET /path` were found in the documentation checked "
            f"against `{report.spec_source}`.",
            "",
        ]
        return "\n".join(out)
    out += [
        f"Checked **{report.claims_checked}** documented endpoint(s) against `{report.spec_source}`.",
        "",
        "| Verdict | Checks |",
        "|---|---|",
        f"| ✅ Verified | {report.count(VERIFIED)} |",
        f"| ❌ Contradicted | {report.count(CONTRADICTED)} |",
        f"| ❓ Uncertain | {report.count(UNCERTAIN)} |",
        "",
    ]
    from health import render_health

    out += render_health(report.health, history)

    def section(title, verdict, open_=True):
        items = [f for f in report.findings if f.verdict == verdict]
        if not items:
            return
        out.append(f"### {_ICON[verdict]} {title} ({len(items)})")
        out.append("")
        if not open_:
            out.append("<details><summary>Show</summary>\n")
        for f in items[:max_items]:
            out.append(f"- **{f.claim.label}** — {f.message}")
            out.append(f"  `{f.claim.file}:{f.claim.line}` · evidence: `{f.evidence}`")
            if f.fix:
                out.append(f"  💡 Suggested fix: {f.fix}")
            if f.ai_fix:
                out.append(f"  🤖 AI-suggested rewrite (review before use): {f.ai_fix}")
        if len(items) > max_items:
            out.append(f"- … and {len(items) - max_items} more")
        if not open_:
            out.append("\n</details>")
        out.append("")

    section("Contradicted by the spec", CONTRADICTED)
    section("Could not be verified", UNCERTAIN)
    section("Verified", VERIFIED, open_=False)
    gaps = []
    if report.missing_required_params:
        gaps += ["**Required parameters missing from the docs**", ""]
        gaps += [f"- {m}" for m in report.missing_required_params[:max_items]]
        gaps.append("")
    if report.undocumented_operations:
        gaps += ["**Spec operations never mentioned in the docs**", ""]
        gaps += [f"- {m}" for m in report.undocumented_operations[:max_items]]
        gaps.append("")
    if gaps:
        out += ["<details><summary>Documentation gaps</summary>\n", *gaps, "</details>", ""]
    return "\n".join(out)


# ---------------------------------------------------------------------------
# GitHub Action entry point and CLI
# ---------------------------------------------------------------------------


def post_pr_comment(markdown: str) -> bool:
    """Post *markdown* on the PR using the same ``gh`` mechanism as the rest of the action."""
    from security_utils import run_command_safe

    pr_number = os.environ.get("PR_NUMBER")
    token = os.environ.get("GH_TOKEN")
    if not pr_number or pr_number == "unknown" or not token:
        return False
    msg_file = Path("/tmp/api_contract_report.md")
    msg_file.write_text(markdown, encoding="utf-8")
    run_command_safe(
        ["gh", "pr", "comment", str(pr_number), "--body-file", str(msg_file)],
        env={**os.environ, "GH_TOKEN": token},
        check=False,
    )
    return True


def run_from_env(post: bool = True) -> Report | None:
    """Entry point for the ``[verify-api]`` command (no LLM involved)."""
    spec_env = os.environ.get("API_SPEC_PATH", "").strip()
    spec_path = Path(spec_env) if spec_env else find_spec_file(".")
    notice = None
    spec = None
    if spec_path is None or not Path(spec_path).is_file():
        notice = (
            "Could not find an OpenAPI spec. Commit one as `openapi.json` / `openapi.yaml` "
            '(for FastAPI: `python -c "import json; from app.main import app; '
            'print(json.dumps(app.openapi()))" > openapi.json`) or set the `api-spec-path` input.'
        )
    else:
        spec_path = Path(spec_path).resolve()
        try:
            spec = load_spec(spec_path)
        except SpecError as e:
            notice = str(e)
    if notice:
        print(f"API contract verification skipped: {notice}")
        if post:
            post_pr_comment(f"## 🔎 API Contract Verification\n\n{notice}")
        return None

    docs_sub = os.environ.get("DOCS_SUBFOLDER", "").strip()
    if docs_sub:
        doc_roots: list[str] = [docs_sub]
        if Path("README.md").is_file():
            doc_roots.append("README.md")
        root = Path(".")
    else:
        from github_ops import setup_docs_environment

        if not setup_docs_environment():
            print("Failed to set up docs environment")
            return None
        doc_roots = ["."]
        root = Path(".")
    report = verify_docs(spec, doc_roots, root)
    if "--ai" in os.environ.get("COMMENT_BODY", "").lower():
        apply_ai_rewrites(report)
    markdown = render_markdown(report)
    print(markdown)
    if post:
        post_pr_comment(markdown)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify API documentation against an OpenAPI spec."
    )
    src = parser.add_mutually_exclusive_group()
    src.add_argument("--spec", help="Path to openapi.json / openapi.yaml")
    src.add_argument("--app", help="FastAPI app as 'package.module:app' (executes project code)")
    parser.add_argument("docs", nargs="*", default=["."], help="Markdown files or folders")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of Markdown")
    parser.add_argument(
        "--ai", action="store_true", help="Also draft AI rewrites (needs MODEL_API_BASE)"
    )
    parser.add_argument("--history", help="JSON file that keeps the health score over time")
    args = parser.parse_args(argv)
    try:
        if args.app:
            spec = load_spec_from_fastapi(args.app)
        else:
            path = Path(args.spec) if args.spec else find_spec_file(".")
            if path is None:
                print("No spec found. Pass --spec or --app.", file=sys.stderr)
                return 2
            spec = load_spec(path)
    except SpecError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    report = verify_docs(spec, args.docs)
    if args.ai:
        apply_ai_rewrites(report)
    history = None
    if args.history:
        from health import append_history

        history = append_history(args.history, report.health)
    print(
        json.dumps(report.to_dict(), indent=2)
        if args.json
        else render_markdown(report, history=history)
    )
    return 1 if report.has_contradictions else 0


if __name__ == "__main__":
    sys.exit(main())
