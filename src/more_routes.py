"""
Static route readers for Go, Spring (Java/Kotlin), Rails and ASP.NET.

Like the Python and Express readers in ``static_routes`` these only *read* source files;
no project code is run. They are deliberately modest: each finds routes written the usual
way, and marks every route ``unresolved`` because parameter and body details are not read
from these languages. A documented parameter the reader did not see is therefore reported
as "uncertain", never as "wrong".

Each reader returns ``(routes, unknown_method_paths, skipped_paths)``:

* ``unknown_method_paths``: a path was found but its HTTP method was not (``@RequestMapping``
  without a method, ``http.HandleFunc("/x", ...)`` before Go 1.22).
* ``skipped_paths``: a route whose path is built at run time and could not be read.
"""

from __future__ import annotations

import re
from pathlib import Path

from static_routes import RouteDef, _path_params, _walk_files, join_paths

VERBS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _line(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _route(verb: str, path: str, source: str, local: str | None = None) -> RouteDef:
    r = RouteDef(verb, path or "/", source=source, unresolved=True)
    r.responses["200"] = None
    for name, ptype in _path_params(_brace_style(local if local is not None else path)):
        r.params.append({"name": name, "in": "path", "required": True, "type": ptype})
    return r


def _brace_style(path: str) -> str:
    """Turn ``:id``, ``*rest`` and ``{id?}`` into plain ``{id}`` placeholders."""
    path = re.sub(r"(?<=/)[:*]([A-Za-z_]\w*)", r"{\1}", path)
    return re.sub(r"\{(\w+)\?\}", r"{\1}", path)


# ----------------------------------------------------------------------------- Go

_GO_SERVER = re.compile(
    r"gin-gonic/gin|labstack/echo|go-chi/chi|gofiber/fiber|gorilla/mux|julienschmidt/httprouter|net/http"
)
_GO_VERB = re.compile(
    r"(?P<recv>\b[A-Za-z_]\w*)\s*\.\s*(?P<verb>GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS|Get|Post|Put|Patch|Delete|Head|Options)"
    r"\s*\(\s*\"(?P<path>/[^\"]*|)\""
)
_GO_GROUP = re.compile(
    r"(?P<var>[A-Za-z_]\w*)\s*:?=\s*(?P<parent>[A-Za-z_]\w*)\s*\.\s*(?:Group|PathPrefix|Subrouter)\s*\(\s*\"(?P<path>[^\"]*)\""
)
_GO_CHI_ROUTE = re.compile(r"\.\s*Route\s*\(\s*\"(?P<path>/[^\"]*)\"\s*,\s*func\s*\(")
_GO_HANDLEFUNC = re.compile(
    r"\.\s*(?:HandleFunc|Handle)\s*\(\s*\"(?:(?P<verb>[A-Z]+)\s+)?(?P<path>/[^\"\s]*)\"(?P<rest>[^\n]*)"
)
_GO_METHODS = re.compile(r"\.\s*Methods\s*\(\s*\"(?P<verb>[A-Za-z]+)\"")
_NOT_ROUTERS = {"http", "client", "resp", "req", "w", "res"}  # HTTP clients, not routers


def _brace_span(text: str, open_paren: int) -> int:
    """Index just after the ``}`` closing the first ``{`` at or after *open_paren*."""
    depth = 0
    started = False
    i = open_paren
    in_str: str | None = None
    while i < len(text):
        ch = text[i]
        if in_str:
            if ch == "\\" and in_str != "`":
                i += 1
            elif ch == in_str:
                in_str = None
        elif ch in "\"`'":
            in_str = ch
        elif ch == "{":
            depth += 1
            started = True
        elif ch == "}":
            depth -= 1
            if started and depth == 0:
                return i + 1
        i += 1
    return len(text)


def go_routes(root: Path) -> tuple[list[RouteDef], list[str], list[str]]:
    routes: list[RouteDef] = []
    unknown: list[str] = []
    for path in _walk_files(root, (".go",)):
        if path.name.endswith("_test.go"):
            continue
        text = _read(path)
        if not text or not _GO_SERVER.search(text):
            continue
        rel = path.relative_to(root).as_posix()
        groups: dict[str, str] = {}
        for _ in range(3):  # resolve a group declared from another group
            for m in _GO_GROUP.finditer(text):
                groups[m.group("var")] = join_paths(
                    groups.get(m.group("parent"), ""), m.group("path")
                )
        regions = [
            (m.start(), _brace_span(text, m.end()), m.group("path"))
            for m in _GO_CHI_ROUTE.finditer(text)
        ]

        def prefix_at(pos: int, regions=regions) -> str:
            enclosing = [r for r in regions if r[0] < pos < r[1]]
            enclosing.sort(key=lambda r: r[0])
            return join_paths(*(r[2] for r in enclosing)) if enclosing else ""

        for m in _GO_VERB.finditer(text):
            recv = m.group("recv")
            if recv in _NOT_ROUTERS:
                continue
            full = join_paths(groups.get(recv, ""), prefix_at(m.start()), m.group("path"))
            routes.append(
                _route(m.group("verb").upper(), full or "/", f"{rel}:{_line(text, m.start())}")
            )
        for m in _GO_HANDLEFUNC.finditer(text):
            local = m.group("path")
            full = join_paths(prefix_at(m.start()), local) or "/"
            verb = m.group("verb")
            if not verb:
                mm = _GO_METHODS.search(m.group("rest"))
                verb = mm.group("verb") if mm else ""
            verb = verb.upper()
            if verb in VERBS:
                routes.append(_route(verb, full, f"{rel}:{_line(text, m.start())}"))
            else:
                unknown.append(full)
    return routes, unknown, []


# ----------------------------------------------------------------------------- Spring

_SPRING_ANN = re.compile(
    r"@(?P<kind>Get|Post|Put|Patch|Delete|Request)Mapping\b(?P<args>\s*\((?:[^()]|\([^()]*\))*\))?"
)
_STRING = re.compile(r"\"([^\"]*)\"")
_REQUEST_METHOD = re.compile(r"RequestMethod\.(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)")
_SPRING_REQUEST_PARAM = re.compile(
    r"@RequestParam\s*(?:\((?P<args>[^)]*)\))?\s*(?:final\s+)?(?:[\w<>\[\],.? ]+?\s+(?P<name>\w+)|(?P<kname>\w+)\s*:)"
)


def _spring_paths(args: str | None) -> list[str]:
    if not args:
        return [""]
    inner = args.strip()[1:-1]
    # only look at value= / path= / a leading positional value, not at e.g. produces="..."
    named = re.search(r"\b(?:value|path)\s*=\s*(\{[^}]*\}|\"[^\"]*\")", inner)
    if named:
        found = _STRING.findall(named.group(1))
    elif not re.match(r"\s*\w+\s*=", inner):
        found = _STRING.findall(
            inner.split(",")[0] if not inner.lstrip().startswith("{") else inner.split("}")[0]
        )
    else:
        found = []
    return found or [""]


def spring_routes(root: Path) -> tuple[list[RouteDef], list[str], list[str]]:
    routes: list[RouteDef] = []
    unknown: list[str] = []
    for path in _walk_files(root, (".java", ".kt")):
        text = _read(path)
        if not text or "springframework" not in text:
            continue
        rel = path.relative_to(root).as_posix()
        matches = list(_SPRING_ANN.finditer(text))
        # a class-level @RequestMapping is the one directly before ``class``/``interface``
        class_prefix = [""]
        class_pos = None
        for m in matches:
            tail = text[m.end() : m.end() + 400]
            if m.group("kind") == "Request" and re.match(
                r"(?:\s*@\w+(?:\([^)]*\))?)*\s*(?:public\s+|open\s+|abstract\s+)*(?:class|interface)\b",
                tail,
            ):
                class_prefix = _spring_paths(m.group("args"))
                class_pos = m.start()
                break
        for m in matches:
            if m.start() == class_pos:
                continue
            kind = m.group("kind")
            verbs = (
                [kind.upper()]
                if kind != "Request"
                else _REQUEST_METHOD.findall(m.group("args") or "")
            )
            signature = text[m.end() : m.end() + 600].split("{", 1)[0]
            for prefix in class_prefix:
                for local in _spring_paths(m.group("args")):
                    full = join_paths(prefix, local) or "/"
                    if not verbs:
                        unknown.append(full)
                        continue
                    for verb in verbs:
                        r = _route(verb, full, f"{rel}:{_line(text, m.start())}")
                        seen = {p["name"] for p in r.params}
                        for qm in _SPRING_REQUEST_PARAM.finditer(signature):
                            args = qm.group("args") or ""
                            nm = re.search(r"\b(?:name|value)\s*=\s*\"(\w+)\"|^\s*\"(\w+)\"", args)
                            name = (
                                (nm.group(1) or nm.group(2))
                                if nm
                                else (qm.group("name") or qm.group("kname"))
                            )
                            if name and name not in seen:
                                seen.add(name)
                                optional = (
                                    "required = false"
                                    in args.replace("=false", "= false").replace(
                                        "required=false", "required = false"
                                    )
                                    or "defaultValue" in args
                                )
                                r.params.append(
                                    {
                                        "name": name,
                                        "in": "query",
                                        "required": not optional,
                                        "type": None,
                                    }
                                )
                        routes.append(r)
    return routes, unknown, []


# ----------------------------------------------------------------------------- Rails

_RAILS_VERB = re.compile(
    r"^\s*(?P<verb>get|post|put|patch|delete)\s+(?P<path>['\"][^'\"]+['\"]|:\w+)", re.M
)
_RAILS_ACTIONS = {
    "index": ("GET", ""),
    "create": ("POST", ""),
    "show": ("GET", "/:id"),
    "update": ("PATCH", "/:id"),
    "destroy": ("DELETE", "/:id"),
}
_RAILS_ONLY = re.compile(r"\b(only|except)\s*:\s*(\[[^\]]*\]|:\w+)")


def _rails_list(raw: str) -> set[str]:
    return set(re.findall(r":(\w+)", raw))


def rails_routes(root: Path) -> tuple[list[RouteDef], list[str], list[str]]:
    routes: list[RouteDef] = []
    unknown: list[str] = []
    for path in _walk_files(root, (".rb",)):
        if path.name != "routes.rb" and "routes" not in path.parent.name:
            continue
        text = _read(path)
        if not text or "routes.draw" not in text and not path.name.endswith("routes.rb"):
            continue
        rel = path.relative_to(root).as_posix()
        stack: list[tuple[str, str]] = []  # (kind, prefix contribution)

        def prefix(stack=stack) -> str:
            parts = [p for _k, p in stack]
            for i, (kind, _p) in enumerate(stack):
                if kind in ("member", "collection") and i > 0:
                    # inside ``member do`` the id is ``:id``; inside ``collection do`` there is none
                    parts[i - 1] = re.sub(
                        r"/:\w+_id$", "/:id" if kind == "member" else "", parts[i - 1]
                    )
            return join_paths(*parts)

        for number, raw in enumerate(text.splitlines(), 1):
            line = raw.split("#", 1)[0].rstrip()
            stripped = line.strip()
            if not stripped:
                continue
            src = f"{rel}:{number}"
            opens = bool(re.search(r"\bdo(?:\s*\|[^|]*\|)?\s*$", stripped))
            m = re.match(r"(namespace|scope)\s+(?:path:\s*)?[:'\"]?([\w/\-]+)['\"]?", stripped)
            res = re.match(r"(resources|resource)\s+:(\w+)(.*)", stripped)
            if res:
                kind, name, rest = res.groups()
                opt = _RAILS_ONLY.search(rest)
                only = _rails_list(opt.group(2)) if opt and opt.group(1) == "only" else None
                exc = _rails_list(opt.group(2)) if opt and opt.group(1) == "except" else set()
                base = join_paths(prefix(), name)
                for action, (verb, suffix) in _RAILS_ACTIONS.items():
                    if (only is not None and action not in only) or action in exc:
                        continue
                    if kind == "resource":
                        if action == "index" or suffix:
                            suffix = ""
                            if action == "index":
                                continue
                    full = base + (suffix if kind == "resources" else "")
                    routes.append(_route(verb, full, src))
                    if action == "update":
                        routes.append(_route("PUT", full, src))
                if opens:
                    stack.append(
                        (
                            "resources",
                            f"{name}/:{name.rstrip('s')}_id" if kind == "resources" else name,
                        )
                    )
                continue
            if m and opens:
                kind, val = m.groups()
                stack.append(
                    (kind, val if kind == "namespace" or "/" in val or "path" in stripped else "")
                )
                continue
            if re.match(r"(member|collection)\s+do", stripped):
                stack.append((stripped.split()[0], ""))
                continue
            vm = _RAILS_VERB.match(line)
            if vm:
                raw_path = vm.group("path")
                local = raw_path.strip("'\"") if raw_path[0] in "'\"" else raw_path[1:]
                full = join_paths(prefix(), local) or "/"
                routes.append(_route(vm.group("verb").upper(), full, src))
                if opens:
                    stack.append(("block", ""))
                continue
            mm = re.match(r"match\s+['\"]([^'\"]+)['\"].*via:\s*(\[[^\]]*\]|:\w+)", stripped)
            if mm:
                full = join_paths(prefix(), mm.group(1)) or "/"
                for verb in _rails_list(mm.group(2)):
                    if verb.upper() in VERBS:
                        routes.append(_route(verb.upper(), full, src))
                continue
            if re.match(r"root\b", stripped):
                routes.append(_route("GET", join_paths(prefix()) or "/", src))
                continue
            if opens or re.match(r"(if|unless|case|begin|while)\b", stripped):
                stack.append(("block", ""))
                continue
            if stripped == "end" and stack:
                stack.pop()
    return routes, unknown, []


# ----------------------------------------------------------------------------- ASP.NET

_CS_ATTR = r"\[(?:[^\[\]]|\[[^\]]*\])*\]"  # an attribute; may contain ``[controller]``
_CS_CLASS = re.compile(
    rf"(?P<attrs>(?:\s*{_CS_ATTR}\s*)*)\s*(?:public\s+|internal\s+)?(?:sealed\s+|abstract\s+)*class\s+(?P<name>\w+)"
)
_CS_ROUTE_ATTR = re.compile(r"\[\s*Route\s*\(\s*\"([^\"]*)\"")
_CS_HTTP = re.compile(
    r"\[\s*Http(?P<verb>Get|Post|Put|Patch|Delete|Head|Options)\b(?:\s*\(\s*(?:template\s*:\s*)?\"(?P<path>[^\"]*)\")?[^\]]*\]"
    rf"(?P<between>(?:\s*{_CS_ATTR})*)\s*(?:public|protected|internal)?\s*(?:async\s+)?[\w<>\[\],.? ]+?\s+(?P<action>\w+)\s*\("
)
_CS_MAP = re.compile(
    r"(?P<recv>[A-Za-z_]\w*)\s*\.\s*Map(?P<verb>Get|Post|Put|Patch|Delete)\s*\(\s*\"(?P<path>[^\"]*)\""
)
_CS_GROUP = re.compile(
    r"(?P<var>[A-Za-z_]\w*)\s*=\s*(?P<parent>[A-Za-z_]\w*)\s*\.\s*MapGroup\s*\(\s*\"(?P<path>[^\"]*)\""
)


def aspnet_routes(root: Path) -> tuple[list[RouteDef], list[str], list[str]]:
    routes: list[RouteDef] = []
    skipped: list[str] = []
    for path in _walk_files(root, (".cs",)):
        text = _read(path)
        if (
            not text
            or "AspNetCore" not in text
            and "MapGet" not in text
            and "[ApiController]" not in text
        ):
            continue
        rel = path.relative_to(root).as_posix()
        # controllers
        classes = [m for m in _CS_CLASS.finditer(text) if "Controller" in m.group("name")]
        for index, cls in enumerate(classes):
            end = classes[index + 1].start() if index + 1 < len(classes) else len(text)
            body = text[cls.end() : end]
            controller = cls.group("name").removesuffix("Controller")
            rm = _CS_ROUTE_ATTR.search(cls.group("attrs") or "")
            prefix = rm.group(1) if rm else ""
            prefix = re.sub(r"\[controller\]", controller.lower(), prefix, flags=re.I)
            for hm in _CS_HTTP.finditer(body):
                local = hm.group("path") or ""
                action = hm.group("action")
                if local.startswith("/"):
                    full = local
                else:
                    full = join_paths(prefix, local)
                full = re.sub(r"\[action\]", action.lower(), full, flags=re.I)
                full = re.sub(r"\[controller\]", controller.lower(), full, flags=re.I)
                if "[" in full:
                    skipped.append(full)
                    continue
                routes.append(
                    _route(
                        hm.group("verb").upper(),
                        full or "/",
                        f"{rel}:{_line(text, cls.end() + hm.start())}",
                    )
                )
        # minimal APIs
        groups: dict[str, str] = {}
        for _ in range(3):
            for m in _CS_GROUP.finditer(text):
                groups[m.group("var")] = join_paths(
                    groups.get(m.group("parent"), ""), m.group("path")
                )
        for m in _CS_MAP.finditer(text):
            full = join_paths(groups.get(m.group("recv"), ""), m.group("path")) or "/"
            routes.append(_route(m.group("verb").upper(), full, f"{rel}:{_line(text, m.start())}"))
    return routes, [], skipped


# ----------------------------------------------------------------------------- detection

READERS = {
    "go": go_routes,
    "spring": spring_routes,
    "rails": rails_routes,
    "aspnet": aspnet_routes,
}


def detect(root: Path) -> list[str]:
    found: list[str] = []
    for path in _walk_files(root, (".go", ".java", ".kt", ".cs", ".rb")):
        suffix = path.suffix
        if suffix == ".go" and "go" not in found:
            text = _read(path) or ""
            if _GO_SERVER.search(text) and not path.name.endswith("_test.go"):
                found.append("go")
        elif suffix in (".java", ".kt") and "spring" not in found:
            if "org.springframework.web" in (_read(path) or ""):
                found.append("spring")
        elif suffix == ".cs" and "aspnet" not in found:
            text = _read(path) or ""
            if "Microsoft.AspNetCore" in text or "[ApiController]" in text or ".MapGet(" in text:
                found.append("aspnet")
        elif suffix == ".rb" and "rails" not in found and path.name == "routes.rb":
            found.append("rails")
    return found
