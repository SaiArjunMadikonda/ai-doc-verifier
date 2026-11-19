"""
Check the *examples* in the docs against the API definition.

Two kinds of example are verified:

* **Calls** - ``curl`` commands, ``requests``/``httpx`` calls, ``fetch``/``axios``
  calls and raw ``GET /path HTTP/1.1`` requests: does the endpoint exist, are the
  query parameters and JSON body fields real, is a required field missing?
* **Sample responses** - a JSON block under an endpoint that is introduced as a
  response: do its fields, types and required keys match the response schema?

Only calls that clearly target *this* API are checked (relative paths, localhost,
placeholder hosts, ``example.*`` hosts and the spec's own ``servers``), so a docs
snippet that calls a third-party service is ignored instead of flagged.
"""

from __future__ import annotations

import ast
import json
import re
import shlex
from dataclasses import dataclass, field
from urllib.parse import urlparse

from api_contract import (
    CONTRADICTED,
    UNCERTAIN,
    VERIFIED,
    Claim,
    Finding,
    Operation,
    Spec,
    _pointer_escape,
)

HTTP_VERBS = ("get", "post", "put", "patch", "delete", "head", "options")
_VERB_RE = "|".join(v.upper() for v in HTTP_VERBS)
_PLACEHOLDER_HOST = re.compile(
    r"^(\$\{?\w+\}?|\{\{[^}]*\}\}|<[^>]*>|\$\(.*\)|YOUR[_-]\w*|your[_-]\w*|HOST|BASE_URL|API_URL)$"
)
_EXAMPLE_HOST = re.compile(r"(^|\.)(example\.(com|org|net)|test|local|localhost|invalid)$", re.I)
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "[::1]", "host.docker.internal"}


@dataclass
class ExampleCall:
    method: str
    path: str
    line: int
    source: str  # curl | python | js | http
    query: list[str] = field(default_factory=list)
    body: dict | None = None  # parsed JSON object, when available


@dataclass
class Fence:
    lang: str
    first_line: int  # 1-based line of the first content line
    text: str
    label: str  # nearest non-blank line before the fence
    endpoint: tuple[str, str] | None  # the endpoint the surrounding section is about


# ----------------------------------------------------------------------------- fences


_ENDPOINT_IN_LINE = re.compile(
    rf"(?<![A-Za-z0-9_])({_VERB_RE})[\s|`*_]*(/[A-Za-z0-9_\-./{{}}:<>~%@$!*+,;=]*)"
)


def find_fences(markdown: str) -> list[Fence]:
    lines = markdown.splitlines()
    fences: list[Fence] = []
    label = ""
    endpoint: tuple[str, str] | None = None
    endpoint_level = 99
    i = 0
    while i < len(lines):
        line = lines[i]
        fm = re.match(r"^\s*(```|~~~)\s*([\w+-]*)", line)
        if fm:
            marker, lang = fm.group(1), fm.group(2).lower()
            j = i + 1
            body: list[str] = []
            while j < len(lines) and not lines[j].lstrip().startswith(marker):
                body.append(lines[j])
                j += 1
            fences.append(Fence(lang, i + 2, "\n".join(body), label, endpoint))
            i = j + 1
            continue
        hm = re.match(r"^(#{1,6})\s+(.*)$", line)
        if hm:
            level = len(hm.group(1))
            found = _ENDPOINT_IN_LINE.search(line)
            if found:
                endpoint, endpoint_level = (found.group(1), found.group(2).rstrip(".,;:)")), level
            elif level <= endpoint_level:
                endpoint, endpoint_level = None, 99
            label = hm.group(2)
        elif line.strip():
            found = _ENDPOINT_IN_LINE.search(line)
            if found and not line.lstrip().startswith("|"):
                endpoint = (found.group(1), found.group(2).rstrip(".,;:)"))
                endpoint_level = 7
            label = line.strip()
        i += 1
    return fences


# ----------------------------------------------------------------------------- URL handling


def spec_hosts(spec: Spec) -> set[str]:
    hosts = set()
    for server in spec.data.get("servers") or []:
        if isinstance(server, dict) and server.get("url"):
            host = urlparse(str(server["url"])).netloc.lower()
            if host:
                hosts.add(host)
    if isinstance(spec.data.get("host"), str):
        hosts.add(spec.data["host"].lower())
    return hosts


def _is_our_host(host: str, known: set[str]) -> bool:
    h = host.lower()
    bare = re.sub(r":\d+$", "", h)
    return bool(
        h in known
        or bare in _LOCAL_HOSTS
        or _PLACEHOLDER_HOST.match(host)
        or _EXAMPLE_HOST.search(bare)
        or re.search(r"your|my-?api|myapp|mydomain", bare)
    )


def split_url(url: str, known_hosts: set[str]) -> tuple[str, list[str]] | None:
    """Return ``(path, query keys)`` when *url* targets this API, else ``None``."""
    url = url.strip().strip("'\"`")
    m = re.match(r"^https?://([^/?#\s]+)(/[^?#\s]*)?(?:\?([^#\s]*))?", url, re.I)
    if m:
        if not _is_our_host(m.group(1), known_hosts):
            return None
        path, query = m.group(2) or "/", m.group(3) or ""
    else:
        m = re.match(
            r"^(\$\{[^}]*\}|\$\w+|\{\{[^}]*\}\}|<[^>/]*>|\$\([^)]*\))(/[^?#\s]*)?(?:\?([^#\s]*))?",
            url,
        )
        if m:
            path, query = m.group(2) or "/", m.group(3) or ""
        elif url.startswith("/"):
            m = re.match(r"^(/[^?#\s]*)(?:\?([^#\s]*))?", url)
            path, query = m.group(1), m.group(2) or ""
        else:
            return None
    keys = [
        k.split("=", 1)[0] for k in query.split("&") if k and re.match(r"^[A-Za-z_][\w.\-\[\]]*", k)
    ]
    return path, keys


def _json_object(text: str | None):
    if not text:
        return None
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# ----------------------------------------------------------------------------- finding calls


def _curl_calls(block: Fence, hosts: set[str]) -> list[ExampleCall]:
    calls = []
    text = re.sub(r"\\\r?\n", " ", block.text)
    for offset, raw in enumerate(text.splitlines()):
        raw = raw.strip().lstrip("$ ").strip()
        if not raw.startswith("curl"):
            continue
        try:
            tokens = shlex.split(raw)
        except ValueError:
            continue
        method, url, data, has_form = None, None, None, False
        k = 1
        while k < len(tokens):
            tok = tokens[k]
            nxt = tokens[k + 1] if k + 1 < len(tokens) else None
            if tok in ("-X", "--request") and nxt:
                method = nxt.upper()
                k += 2
            elif (
                tok in ("-d", "--data", "--data-raw", "--data-binary", "--json", "--data-urlencode")
                and nxt
            ):
                data = nxt if data is None else data
                k += 2
            elif tok in ("-F", "--form") and nxt:
                has_form = True
                k += 2
            elif (
                tok
                in (
                    "-H",
                    "--header",
                    "-u",
                    "--user",
                    "-o",
                    "--output",
                    "-A",
                    "--user-agent",
                    "-b",
                    "--cookie",
                )
                and nxt
            ):
                k += 2
            elif tok.startswith("-"):
                k += 1
            else:
                url = url or tok
                k += 1
        if not url:
            continue
        target = split_url(url, hosts)
        if target is None:
            continue
        verb = method or ("POST" if (data is not None or has_form) else "GET")
        if verb.lower() not in HTTP_VERBS:
            continue
        path, query = target
        calls.append(
            ExampleCall(verb, path, block.first_line + offset, "curl", query, _json_object(data))
        )
    return calls


def _python_calls(block: Fence, hosts: set[str]) -> list[ExampleCall]:
    calls: list[ExampleCall] = []
    try:
        tree = ast.parse(block.text)
    except SyntaxError:
        return calls
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        verb = node.func.attr
        if verb not in HTTP_VERBS or not node.args:
            continue
        url = _const_text(node.args[0])
        if url is None:
            continue
        kwargs = {k.arg for k in node.keywords}
        receiver = ast.unparse(node.func.value).lower()
        if not (
            re.search(r"requests|httpx|client|session|api|http", receiver)
            or kwargs & {"json", "params", "data", "headers"}
        ):
            continue
        target = split_url(url, hosts)
        if target is None:
            continue
        path, query = target
        for k in node.keywords:
            if k.arg == "params" and isinstance(k.value, ast.Dict):
                query += [
                    c.value
                    for c in k.value.keys
                    if isinstance(c, ast.Constant) and isinstance(c.value, str)
                ]
        body = None
        for k in node.keywords:
            if k.arg == "json" and isinstance(k.value, ast.Dict):
                body = {
                    c.value: True
                    for c in k.value.keys
                    if isinstance(c, ast.Constant) and isinstance(c.value, str)
                }
        calls.append(
            ExampleCall(
                verb.upper(), path, block.first_line + node.lineno - 1, "python", query, body
            )
        )
    return calls


def _const_text(node) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for i, v in enumerate(node.values):
            if isinstance(v, ast.Constant):
                parts.append(str(v.value))
            else:  # a leading expression is almost always the base URL
                parts.append("${base}" if i == 0 else "{x}")
        return "".join(parts)
    return None


_JS_FETCH = re.compile(r"fetch\(\s*[`'\"]([^`'\"]+)[`'\"]")
_JS_AXIOS = re.compile(r"axios\.(get|post|put|patch|delete)\(\s*[`'\"]([^`'\"]+)[`'\"]")


def _balanced(text: str, start: int) -> str:
    """Return the ``{...}`` object literal that begins at *start* (string-aware)."""
    depth, quote = 0, ""
    for i in range(start, len(text)):
        c = text[i]
        if quote:
            if c == quote and text[i - 1] != "\\":
                quote = ""
        elif c in "'\"`":
            quote = c
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return text[start:]


def _top_keys(obj: str) -> dict[str, bool]:
    """Top-level keys of a JavaScript object literal."""
    keys: dict[str, bool] = {}
    depth, quote, i = 0, "", 0
    while i < len(obj):
        c = obj[i]
        if quote:
            if c == quote and obj[i - 1] != "\\":
                quote = ""
        elif c in "'\"`":
            quote = c
        elif c in "{[(":
            depth += 1
        elif c in "}])":
            depth -= 1
        elif depth == 1:
            m = re.match(r"['\"]?([A-Za-z_$][\w$-]*)['\"]?\s*:", obj[i:])
            if m and (i == 0 or obj[i - 1] in "{, \n\t"):
                keys[m.group(1)] = True
                i += m.end() - 1
        i += 1
    return keys


def _js_body(window: str) -> dict | None:
    sm = re.search(r"JSON\.stringify\(\s*(?=\{)", window)
    if not sm:
        return None
    return _top_keys(_balanced(window, sm.end()))


def _js_url(url: str) -> str:
    # `${BASE}/users/${id}` -> a leading placeholder stays, inner ones become {x}
    lead = re.match(r"^\$\{[^}]*\}", url)
    head = lead.group(0) if lead else ""
    return head + re.sub(r"\$\{[^}]*\}", "{x}", url[len(head) :])


def _js_calls(block: Fence, hosts: set[str]) -> list[ExampleCall]:
    calls = []
    text = block.text
    for m in _JS_FETCH.finditer(text):
        target = split_url(_js_url(m.group(1)), hosts)
        if target is None:
            continue
        end = text.find(");", m.end())
        window = text[m.end() : end if end != -1 else m.end() + 800]
        mm = re.search(r"method\s*:\s*['\"](\w+)['\"]", window)
        verb = (mm.group(1) if mm else "GET").upper()
        line = block.first_line + text[: m.start()].count("\n")
        calls.append(ExampleCall(verb, target[0], line, "js", target[1], _js_body(window)))
    for m in _JS_AXIOS.finditer(text):
        target = split_url(_js_url(m.group(2)), hosts)
        if target is None:
            continue
        end = text.find(");", m.end())
        window = text[m.end() : end if end != -1 else m.end() + 800]
        body = None
        if m.group(1) != "get":
            brace = window.find("{")
            if brace != -1:
                body = _top_keys(_balanced(window, brace))
        line = block.first_line + text[: m.start()].count("\n")
        calls.append(ExampleCall(m.group(1).upper(), target[0], line, "js", target[1], body))
    return calls


def _raw_http_calls(block: Fence, hosts: set[str]) -> list[ExampleCall]:
    calls = []
    lines = block.text.splitlines()
    for idx, line in enumerate(lines):
        m = re.match(rf"^({_VERB_RE})\s+(\S+)(?:\s+HTTP/\d(?:\.\d)?)?\s*$", line.strip())
        if not m or (idx and lines[idx - 1].strip() and not lines[idx - 1].startswith(("#", "//"))):
            continue
        target = split_url(m.group(2), hosts)
        if target is None:
            continue
        body_text = None
        for k in range(idx + 1, len(lines)):
            if not lines[k].strip():
                body_text = "\n".join(lines[k + 1 :]).split("\n\n")[0]
                break
        calls.append(
            ExampleCall(
                m.group(1),
                target[0],
                block.first_line + idx,
                "http",
                target[1],
                _json_object(body_text),
            )
        )
    return calls


def find_calls(fence: Fence, hosts: set[str]) -> list[ExampleCall]:
    lang = fence.lang
    if lang in ("python", "py", "python3"):
        return _python_calls(fence, hosts)
    if lang in ("js", "javascript", "ts", "typescript", "node", "jsx", "tsx"):
        return _js_calls(fence, hosts)
    if lang in ("http", "https", "rest"):
        return _raw_http_calls(fence, hosts)
    if lang in ("bash", "sh", "shell", "zsh", "console", "curl", "cmd", "powershell", ""):
        return _curl_calls(fence, hosts) or (_raw_http_calls(fence, hosts) if lang == "" else [])
    return []


# ----------------------------------------------------------------------------- verifying calls


def _body_fields(op: Operation):
    return {p.name: p for p in op.params if p.location == "body"}


def verify_call(call: ExampleCall, file: str, spec: Spec) -> list[Finding]:
    claim = Claim(call.method, call.path, file, call.line)
    findings: list[Finding] = []
    label = f"Example ({call.source}) `{call.method} {call.path}`"

    def add(verdict, message, evidence="", fix=""):
        if evidence.startswith("#"):
            evidence = f"{spec.source}{evidence}"
        findings.append(Finding(verdict, claim, "example", message, evidence, fix))

    norm, how = spec.find_path(call.path)
    if norm is None and spec.unresolved_routes:
        add(
            UNCERTAIN,
            f"{label} targets a path that is not among the routes that could be read; "
            f"{len(spec.unresolved_routes)} route(s) in the code could not be resolved statically.",
            f"{spec.source} (static analysis)",
        )
        return findings
    if norm is None:
        hint = spec.suggest(call.path)
        add(
            CONTRADICTED,
            f"{label} targets a path the spec does not define.",
            f"{spec.source} (searched {len(spec.paths)} paths)",
            f"Change the example to use `{hint}`."
            if hint
            else "Fix the path in the example or add the endpoint to the API.",
        )
        return findings
    spec_path = spec.spec_path_for(norm)
    if how == "suffix":
        add(
            UNCERTAIN,
            f"{label} may include a deployment prefix the spec does not list.",
            f"#/paths/{_pointer_escape(spec_path)}",
        )
        return findings
    op = spec.operations.get((call.method, norm))
    if op is None:
        if norm in spec.unknown_method_paths:
            add(
                UNCERTAIN,
                f"{label}: the code defines `{spec_path}` but not which methods it accepts.",
                f"{spec.source} (static analysis)",
            )
            return findings
        defined = ", ".join(spec.methods_for(norm)) or "no operations"
        add(
            CONTRADICTED,
            f"{label} uses a method the spec does not define for `{spec_path}` (it defines {defined}).",
            f"#/paths/{_pointer_escape(spec_path)}",
            f"Use one of {defined} in the example.",
        )
        return findings

    problems = 0
    spec_query = {p.name for p in op.params if p.location == "query"}
    for key in call.query:
        if key in spec_query:
            continue
        problems += 1
        if op.unresolved:
            add(
                UNCERTAIN,
                f"{label} sends query parameter `{key}`, which was not found; parts of the operation could not be resolved.",
                op.pointer,
            )
        else:
            known = ", ".join(f"`{n}`" for n in sorted(spec_query)) or "none"
            add(
                CONTRADICTED,
                f"{label} sends query parameter `{key}` that is not in the spec (spec query parameters: {known}).",
                op.pointer,
                f"Remove `{key}` from the example, or add it to the API.",
            )
    fields = _body_fields(op)
    if call.body is not None and fields:
        for key in call.body:
            if key not in fields:
                problems += 1
                verdict = UNCERTAIN if op.unresolved else CONTRADICTED
                add(
                    verdict,
                    f"{label} sends body field `{key}` that is not in the spec (spec fields: {', '.join(f'`{n}`' for n in fields)}).",
                    op.pointer,
                    f"Remove `{key}` from the example body, or add it to the API."
                    if verdict == CONTRADICTED
                    else "",
                )
        for name, f in fields.items():
            if f.required and name not in call.body and not op.unresolved:
                problems += 1
                add(
                    CONTRADICTED,
                    f"{label} omits the required body field `{name}`.",
                    f.pointer,
                    f"Add `{name}` to the example body.",
                )
    if not problems:
        add(VERIFIED, f"{label} matches the spec.", op.pointer)
    return findings


# ----------------------------------------------------------------------------- sample responses


_JSON_TYPES = {
    bool: "boolean",
    int: "integer",
    float: "number",
    str: "string",
    list: "array",
    dict: "object",
    type(None): "null",
}


def _json_type(value) -> str:
    return _JSON_TYPES.get(type(value), "object")


def _flatten(spec: Spec, schema, depth: int = 0):
    """Resolve refs / allOf into ``(types, properties, required, complete)``."""
    schema, ok = spec.resolve_ref(schema)
    if not ok or not isinstance(schema, dict) or depth > 6:
        return set(), {}, set(), False
    types: set[str] = set()
    t = schema.get("type")
    if isinstance(t, str):
        types.add(t)
    elif isinstance(t, list):
        types.update(x for x in t if isinstance(x, str))
    props = dict(schema.get("properties") or {})
    required = set(schema.get("required") or [])
    complete = schema.get("additionalProperties") in (None, False) or bool(props)
    if schema.get("additionalProperties") is True or isinstance(
        schema.get("additionalProperties"), dict
    ):
        complete = False
    for sub in schema.get("allOf") or []:
        st, sp, sr, sc = _flatten(spec, sub, depth + 1)
        types |= st
        props.update(sp)
        required |= sr
        complete = complete and sc
    for key in ("anyOf", "oneOf"):
        options = schema.get(key) or []
        if options:
            sub_types = []
            for sub in options:
                st, sp, sr, sc = _flatten(spec, sub, depth + 1)
                sub_types.append((st, sp, sr, sc))
            non_null = [o for o in sub_types if o[0] != {"null"}]
            for st, _, _, _ in sub_types:
                types |= st
            if len(non_null) == 1:  # Optional[X]
                _, sp, sr, sc = non_null[0]
                props.update(sp)
                required |= sr
                complete = complete and sc
            else:
                complete = False
    if not types and props:
        types.add("object")
    return types, props, required, complete


def compare_json(spec: Spec, schema, value, path: str = "$", depth: int = 0) -> list[str]:
    """Differences between a JSON example and a schema (empty list = consistent)."""
    types, props, required, complete = _flatten(spec, schema)
    if not types and not props:
        return []  # nothing known to compare against
    actual = _json_type(value)
    allowed = set(types)
    if actual == "null":
        return (
            []
            if ("null" in allowed or _nullable(spec, schema))
            else [f"`{path}` is null but the schema does not allow null"]
        )
    if allowed and actual not in allowed and not (actual == "integer" and "number" in allowed):
        return [
            f"`{path}` is {actual} in the example but the schema says {'/'.join(sorted(allowed))}"
        ]
    problems: list[str] = []
    if isinstance(value, dict) and (props or "object" in allowed) and depth < 4:
        for key in value:
            if key not in props:
                if complete and props:
                    problems.append(f"`{path}.{key}` is in the example but not in the schema")
                continue
            problems += compare_json(spec, props[key], value[key], f"{path}.{key}", depth + 1)
        for key in sorted(required):
            if key not in value:
                problems.append(
                    f"`{path}.{key}` is required by the schema but missing from the example"
                )
    elif isinstance(value, list) and depth < 4:
        resolved, ok = spec.resolve_ref(schema)
        items = resolved.get("items") if ok and isinstance(resolved, dict) else None
        if items is not None and value:
            problems += compare_json(spec, items, value[0], f"{path}[0]", depth + 1)
    return problems


def _nullable(spec: Spec, schema) -> bool:
    resolved, ok = spec.resolve_ref(schema)
    if not ok or not isinstance(resolved, dict):
        return False
    if resolved.get("nullable"):
        return True
    return any(
        _flatten(spec, o)[0] == {"null"}
        for o in (resolved.get("anyOf") or resolved.get("oneOf") or [])
    )


def _clean_json(text: str):
    cleaned = re.sub(r"^\s*//.*$", "", text, flags=re.M)
    cleaned = re.sub(r",\s*([}\]])", r"\1", cleaned)
    try:
        return json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        return None


def verify_response_example(fence: Fence, file: str, spec: Spec) -> list[Finding]:
    if fence.endpoint is None:
        return []
    method, raw_path = fence.endpoint
    label_text = fence.label.lower()
    if re.search(r"request|payload|body of the|curl|body:", label_text) and not re.search(
        r"respon|return", label_text
    ):
        return []
    if not re.search(r"respon|return|output|result|reply|example", label_text):
        return []
    is_json_lang = fence.lang in ("json", "jsonc", "json5") or (
        fence.lang == "" and fence.text.lstrip()[:1] in "{["
    )
    if not is_json_lang:
        return []
    value = _clean_json(fence.text)
    if value is None or not isinstance(value, (dict, list)):
        return []
    norm, how = spec.find_path(raw_path)
    claim = Claim(method, raw_path, file, fence.first_line)
    if norm is None or how == "suffix":
        return []  # the endpoint claim itself already reports this
    op = spec.operations.get((method, norm))
    if op is None:
        return []
    code_match = re.search(r"\b([1-5]\d\d)\b", fence.label)
    code = (
        code_match.group(1)
        if code_match
        else next((c for c in sorted(op.response_schemas) if c.startswith("2")), None)
    )
    findings: list[Finding] = []

    def add(verdict, message, evidence, fix=""):
        findings.append(
            Finding(
                verdict,
                claim,
                "example-response",
                message,
                f"{spec.source}{evidence}" if evidence.startswith("#") else evidence,
                fix,
            )
        )

    label = f"Sample response for `{method} {spec.spec_path_for(norm)}`" + (
        f" ({code})" if code else ""
    )
    schema = op.response_schemas.get(code) if code else None
    if schema is None:
        add(
            UNCERTAIN,
            f"{label} cannot be checked: the spec has no JSON schema for that response.",
            f"{op.pointer}/responses",
        )
        return findings
    problems = compare_json(spec, schema, value)
    pointer = f"{op.pointer}/responses/{code}"
    if problems:
        for problem in problems[:8]:
            fix = "Update the sample to match the schema, or update the schema if the sample is right."
            add(CONTRADICTED, f"{label}: {problem}.", pointer, fix)
    else:
        add(VERIFIED, f"{label} matches the schema.", pointer)
    return findings


# ----------------------------------------------------------------------------- entry point


def check_examples(markdown: str, file: str, spec: Spec) -> tuple[list[Finding], int]:
    """Verify all examples in one document. Returns ``(findings, examples_checked)``."""
    hosts = spec_hosts(spec)
    findings: list[Finding] = []
    checked = 0
    for fence in find_fences(markdown):
        calls = find_calls(fence, hosts)
        for call in calls:
            checked += 1
            findings += verify_call(call, file, spec)
        if not calls:
            more = verify_response_example(fence, file, spec)
            if more:
                checked += 1
                findings += more
    return findings, checked
