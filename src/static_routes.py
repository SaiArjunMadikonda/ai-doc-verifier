"""
Read an HTTP API straight from source code - no spec file and no code execution.

Supports FastAPI, Flask, Django (``urls.py``, function/class views, DRF routers) and
Express. The code is parsed (Python with :mod:`ast`, JavaScript with conservative
regexes), never imported or run, so it is safe to use on untrusted repositories.

The result is an OpenAPI-shaped dictionary that :class:`api_contract.Spec` can use,
so every doc check works the same as with a real spec. Static analysis cannot see
everything, so the generated operations are *honest about uncertainty*:

* ``x-unresolved`` is set when parameters can come from somewhere we cannot read
  (``Depends(...)``, a custom class, ``request.args`` in Flask, ...). A documented
  parameter that is not found then becomes *uncertain* instead of *contradicted*.
* ``x-unknown-methods`` lists paths (Django, DRF routers) where the code defines the
  URL but the HTTP methods cannot be determined.
"""

from __future__ import annotations

import ast
import os
import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path

SKIP_DIRS = {
    ".git",
    "node_modules",
    "venv",
    ".venv",
    "env",
    "__pycache__",
    ".tox",
    "site-packages",
    "dist",
    "build",
    "tests",
    "test",
    "__tests__",
    "migrations",
}
HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options")

_SCALARS = {
    "int": "integer",
    "str": "string",
    "float": "number",
    "bool": "boolean",
    "bytes": "string",
    "UUID": "string",
    "datetime": "string",
    "date": "string",
    "time": "string",
    "Decimal": "number",
    "EmailStr": "string",
    "HttpUrl": "string",
    "AnyUrl": "string",
    "Path": "string",
}
_CONTAINERS = {"list": "array", "List": "array", "set": "array", "Set": "array", "tuple": "array"}
_DICTS = {"dict": "object", "Dict": "object", "Mapping": "object"}
_SPECIAL_ARGS = {
    "request",
    "response",
    "self",
    "cls",
    "background_tasks",
    "websocket",
}
_SPECIAL_TYPES = {
    "Request",
    "Response",
    "BackgroundTasks",
    "WebSocket",
    "HTTPConnection",
    "UploadFile",
    "Session",
    "AsyncSession",
}
_LOC_FUNCS = {
    "Query": "query",
    "Header": "header",
    "Cookie": "cookie",
    "Path": "path",
    "Body": "body",
    "Form": "body",
    "File": "body",
}


class StaticRouteError(Exception):
    """Raised when no routes can be found in the code."""


@dataclass
class RouteDef:
    method: str  # upper-case, or "" when unknown (Django)
    path: str
    params: list[dict] = field(default_factory=list)  # {"name","in","required","type"}
    body: dict[str, tuple[str | None, bool]] = field(default_factory=dict)
    responses: dict[str, dict | None] = field(default_factory=dict)  # code -> schema
    unresolved: bool = False
    source: str = ""  # file:line


# ------------------------------------------------------------------------ small helpers


def join_paths(*parts: str) -> str:
    out = ""
    for part in parts:
        if not part:
            continue
        out += "/" + part.strip("/") if part.strip("/") else ""
    return out or "/"


def _walk_files(root: Path, suffixes: tuple[str, ...]) -> list[Path]:
    files: list[Path] = []
    for base, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        files += [Path(base) / n for n in sorted(names) if n.endswith(suffixes)]
    return files


def _const(node):
    return node.value if isinstance(node, ast.Constant) else None


def _name_of(node) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _kw(call: ast.Call, name: str):
    for k in call.keywords:
        if k.arg == name:
            return k.value
    return None


def _str_list(node) -> list[str]:
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return [v for v in (_const(e) for e in node.elts) if isinstance(v, str)]
    return []


# ------------------------------------------------------------------------ python project


@dataclass
class PyModule:
    path: Path
    rel: str
    tree: ast.Module
    # local name -> (module dotted name or "", original name or "", relative level)
    imports: dict[str, tuple[str, str, int]] = field(default_factory=dict)
    # module-level ``NAME = <expression>`` assignments, used to resolve path constants
    assigns: dict[str, ast.expr] = field(default_factory=dict)


class PyProject:
    """All Python modules under a root, with import resolution between them."""

    def __init__(self, root: Path):
        self.root = root
        self.modules: dict[str, PyModule] = {}
        for path in _walk_files(root, (".py",)):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError, OSError, ValueError):
                continue
            rel = path.relative_to(root).as_posix()
            mod = PyModule(path, rel, tree)
            for stmt in tree.body:
                if (
                    isinstance(stmt, ast.Assign)
                    and len(stmt.targets) == 1
                    and isinstance(stmt.targets[0], ast.Name)
                ):
                    mod.assigns[stmt.targets[0].id] = stmt.value
                elif (
                    isinstance(stmt, ast.AnnAssign)
                    and isinstance(stmt.target, ast.Name)
                    and stmt.value is not None
                ):
                    mod.assigns[stmt.target.id] = stmt.value
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    for a in node.names:
                        mod.imports[a.asname or a.name] = (node.module or "", a.name, node.level)
                elif isinstance(node, ast.Import):
                    for a in node.names:
                        mod.imports[a.asname or a.name.split(".")[0]] = (a.name, "", 0)
            self.modules[rel] = mod

    def find_module(self, dotted: str, importer: PyModule, level: int = 0) -> PyModule | None:
        parts = [p for p in dotted.split(".") if p]
        if level:
            base = Path(importer.rel).parent
            for _ in range(level - 1):
                base = base.parent
            candidates = [(base.joinpath(*parts)).as_posix()]
        else:
            candidates = []
            joined = "/".join(parts)
            for rel in self.modules:
                stem = rel[:-3] if rel.endswith(".py") else rel
                if stem.endswith("/__init__"):
                    stem = stem[: -len("/__init__")]
                if stem == joined or stem.endswith("/" + joined):
                    candidates.append(stem)
        for stem in candidates:
            for rel in (f"{stem}.py", f"{stem}/__init__.py"):
                rel = posixpath.normpath(rel)
                if rel in self.modules:
                    return self.modules[rel]
        return None

    def string_value(self, mod: PyModule, node, _depth: int = 0) -> str | None:
        """Value of a string expression: a literal, a module constant, ``A + B``, or ``mod.CONST``.

        Returns ``None`` when it cannot be known statically (never guesses).
        """
        if node is None or _depth > 6:
            return None
        if isinstance(node, ast.Constant):
            return node.value if isinstance(node.value, str) else None
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left = self.string_value(mod, node.left, _depth + 1)
            right = self.string_value(mod, node.right, _depth + 1)
            return left + right if left is not None and right is not None else None
        if isinstance(node, ast.Name):
            if node.id in mod.assigns:
                return self.string_value(mod, mod.assigns[node.id], _depth + 1)
            if node.id in mod.imports:
                dotted, original, level = mod.imports[node.id]
                target = self.find_module(dotted, mod, level)
                if target and original and original in target.assigns:
                    return self.string_value(target, target.assigns[original], _depth + 1)
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            alias = node.value.id
            if alias in mod.imports:
                dotted, original, level = mod.imports[alias]
                full = f"{dotted}.{original}".strip(".") if original else dotted
                target = self.find_module(full, mod, level)
                if target and node.attr in target.assigns:
                    return self.string_value(target, target.assigns[node.attr], _depth + 1)
        return None

    def resolve(self, mod: PyModule, node, defined) -> tuple[PyModule, str] | None:
        """Resolve a ``Name`` / ``module.attr`` expression to ``(module, variable)``.

        *defined(module, name)* says whether a module defines that variable of interest.
        """
        if isinstance(node, ast.Name):
            if defined(mod, node.id):
                return mod, node.id
            if node.id in mod.imports:
                dotted, original, level = mod.imports[node.id]
                target = self.find_module(dotted, mod, level)
                if target and original and defined(target, original):
                    return target, original
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            alias = node.value.id
            if alias in mod.imports:
                dotted, original, level = mod.imports[alias]
                full = f"{dotted}.{original}".strip(".") if original else dotted
                target = self.find_module(full, mod, level)
                if target and defined(target, node.attr):
                    return target, node.attr
        return None


# ------------------------------------------------------------------------ pydantic models


class Models:
    """Pydantic-style models found in the project: name -> {field: (type, required)}."""

    def __init__(self, project: PyProject):
        self.classes: dict[str, ast.ClassDef] = {}
        # names such as ``SessionDep = Annotated[Session, Depends(get_db)]``
        self.dependency_aliases: set[str] = set()
        for mod in project.modules.values():
            for node in mod.tree.body:
                if isinstance(node, ast.ClassDef):
                    self.classes[node.name] = node
                elif isinstance(node, ast.Assign) and len(node.targets) == 1:
                    target = node.targets[0]
                    if isinstance(target, ast.Name) and "Depends" in ast.dump(node.value):
                        self.dependency_aliases.add(target.id)
        self._cache: dict[str, dict[str, tuple[str | None, bool]] | None] = {}

    def is_model(self, name: str) -> bool:
        return self.fields(name) is not None

    def fields(self, name: str, _seen: frozenset = frozenset()):
        if name in self._cache:
            return self._cache[name]
        cls = self.classes.get(name)
        if cls is None or name in _seen:
            return None
        self._cache[name] = None  # guards against self-referencing models while computing
        base_names = [_name_of(b) for b in cls.bases]
        result: dict[str, tuple[str | None, bool]] = {}
        known_base = False
        for b in base_names:
            if b in ("BaseModel", "BaseSettings", "SQLModel"):
                known_base = True
            elif b in self.classes:
                parent = self.fields(b, _seen | {name})
                if parent is not None:
                    known_base = True
                    result.update(parent)
        if not known_base:
            self._cache[name] = None
            return None
        for stmt in cls.body:
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                fname = stmt.target.id
                if fname.startswith("_") or fname == "model_config":
                    continue
                if "ClassVar" in ast.dump(stmt.annotation):
                    continue
                result[fname] = (self.ann_type(stmt.annotation), self._required(stmt.value))
        self._cache[name] = result
        return result

    @staticmethod
    def _required(default) -> bool:
        if default is None:
            return True
        if isinstance(default, ast.Constant) and default.value is Ellipsis:
            return True
        if isinstance(default, ast.Call) and _name_of(default.func) in ("Field", "field"):
            if _kw(default, "default") is not None or _kw(default, "default_factory") is not None:
                return False
            if default.args:
                first = default.args[0]
                return isinstance(first, ast.Constant) and first.value is Ellipsis
            return True
        return False

    def ann_type(self, node) -> str | None:
        """Map a type annotation to an OpenAPI type name (``None`` when unknown)."""
        if node is None:
            return None
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            try:
                return self.ann_type(ast.parse(node.value, mode="eval").body)
            except SyntaxError:
                return None
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            for side in (node.left, node.right):
                if not (isinstance(side, ast.Constant) and side.value is None):
                    t = self.ann_type(side)
                    if t:
                        return t
            return None
        if isinstance(node, ast.Subscript):
            outer = _name_of(node.value)
            inner = node.slice
            if outer in ("Optional", "Annotated", "Union"):
                elts = inner.elts if isinstance(inner, ast.Tuple) else [inner]
                for e in elts:
                    if not (isinstance(e, ast.Constant) and e.value is None):
                        t = self.ann_type(e)
                        if t:
                            return t
                return None
            if outer in _CONTAINERS:
                return "array"
            if outer in _DICTS:
                return "object"
            if outer == "Literal":
                elts = inner.elts if isinstance(inner, ast.Tuple) else [inner]
                kinds = {type(_const(e)).__name__ for e in elts}
                return {"str": "string", "int": "integer", "bool": "boolean"}.get(
                    kinds.pop() if len(kinds) == 1 else "", None
                )
            return None
        name = _name_of(node)
        if name in _SCALARS:
            return _SCALARS[name]
        if name in _CONTAINERS:
            return "array"
        if name in _DICTS:
            return "object"
        if name and self.is_model(name):
            return "object"
        return None

    def model_of(self, node) -> tuple[str | None, bool]:
        """Return ``(model name, is_list)`` for an annotation that is a (list of a) model."""
        if isinstance(node, ast.Subscript) and _name_of(node.value) in ("Optional", "Annotated"):
            inner = node.slice.elts[0] if isinstance(node.slice, ast.Tuple) else node.slice
            return self.model_of(inner)
        if isinstance(node, ast.Subscript) and _name_of(node.value) in _CONTAINERS:
            inner = node.slice.elts[0] if isinstance(node.slice, ast.Tuple) else node.slice
            name = _name_of(inner)
            return (name, True) if name and self.is_model(name) else (None, False)
        name = _name_of(node)
        return (name, False) if name and self.is_model(name) else (None, False)

    def schema(self, name: str, is_list: bool = False) -> dict:
        fields = self.fields(name) or {}
        obj = {
            "type": "object",
            "properties": {k: ({"type": t} if t else {}) for k, (t, _) in fields.items()},
            "required": [k for k, (_, req) in fields.items() if req],
        }
        return {"type": "array", "items": obj} if is_list else obj


# ------------------------------------------------------------------------ FastAPI / Flask


def _decorator_route(dec) -> tuple[str, str, ast.Call, list[str]] | None:
    """Return (receiver variable, method-or-'route', call, methods) for ``@x.get('/p')``."""
    if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)):
        return None
    attr = dec.func.attr
    recv = dec.func.value
    if not isinstance(recv, (ast.Name, ast.Attribute)):
        return None
    if attr not in HTTP_METHODS and attr not in ("route", "api_route"):
        return None
    if not dec.args and _kw(dec, "path") is None and _kw(dec, "rule") is None:
        return None
    methods = _str_list(_kw(dec, "methods")) if attr in ("route", "api_route") else []
    return _name_of(recv), attr, dec, [m.upper() for m in methods]


def _route_path(call: ast.Call, project: PyProject, mod: PyModule) -> str | None:
    node = call.args[0] if call.args else (_kw(call, "path") or _kw(call, "rule"))
    return project.string_value(mod, node)


def _status_codes_raised(func: ast.AST) -> list[str]:
    codes: list[str] = []
    for node in ast.walk(func):
        if not (isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)):
            continue
        if _name_of(node.exc.func) != "HTTPException":
            continue
        value = _kw(node.exc, "status_code") or (node.exc.args[0] if node.exc.args else None)
        if isinstance(value, ast.Constant) and isinstance(value.value, int):
            codes.append(str(value.value))
        elif isinstance(value, ast.Attribute):
            m = re.match(r"HTTP_(\d{3})_", value.attr)
            if m:
                codes.append(m.group(1))
    return codes


def _status_of(call: ast.Call, default: str) -> str:
    value = _kw(call, "status_code")
    if isinstance(value, ast.Constant) and isinstance(value.value, int):
        return str(value.value)
    if isinstance(value, ast.Attribute):
        m = re.match(r"HTTP_(\d{3})_", value.attr)
        if m:
            return m.group(1)
    return default


def _path_params(template: str) -> list[tuple[str, str | None]]:
    """Names of ``{x}`` / ``{x:path}`` / ``<int:x>`` placeholders with a type hint."""
    out: list[tuple[str, str | None]] = []
    for m in re.finditer(r"\{([^}/:]+)(?::[^}]*)?\}", template):
        out.append((m.group(1), None))
    for m in re.finditer(r"<(?:(\w+):)?(\w+)>", template):
        conv = m.group(1)
        out.append((m.group(2), {"int": "integer", "float": "number"}.get(conv or "", "string")))
    return out


def _fastapi_function(func, template: str, models: Models) -> tuple[list[dict], dict, bool]:
    """Parameters, body fields and an 'unresolved' flag for a FastAPI endpoint function."""
    params: list[dict] = []
    body: dict[str, tuple[str | None, bool]] = {}
    unresolved = False
    path_names = {n for n, _ in _path_params(template)}
    args = list(func.args.posonlyargs) + list(func.args.args) + list(func.args.kwonlyargs)
    positional = list(func.args.posonlyargs) + list(func.args.args)
    defaults: dict[str, ast.AST | None] = {a.arg: None for a in args}
    for arg, default in zip(reversed(positional), reversed(func.args.defaults), strict=False):
        defaults[arg.arg] = default
    for arg, default in zip(func.args.kwonlyargs, func.args.kw_defaults, strict=True):
        defaults[arg.arg] = default

    for arg in args:
        name = arg.arg
        ann = arg.annotation
        default = defaults[name]
        ann_name = _name_of(ann.value if isinstance(ann, ast.Subscript) else ann) if ann else ""
        if name in _SPECIAL_ARGS or ann_name in _SPECIAL_TYPES:
            continue
        if isinstance(default, ast.Call) and _name_of(default.func) in ("Depends", "Security"):
            unresolved = True
            continue
        if ann is not None and (
            "Depends" in ast.dump(ann) or ann_name in models.dependency_aliases
        ):
            unresolved = True
            continue
        a_type = models.ann_type(ann)
        if isinstance(default, ast.Call) and _name_of(default.func) in _LOC_FUNCS:
            loc = _LOC_FUNCS[_name_of(default.func)]
            required = _field_required(default)
            alias = _const(_kw(default, "alias"))
            if loc == "body":
                body[alias or name] = (a_type, required)
                continue
            if loc == "header" and not alias:
                alias = name.replace("_", "-")
            params.append(
                {
                    "name": alias or name,
                    "in": loc,
                    "required": True if loc == "path" else required,
                    "type": a_type,
                }
            )
            continue
        if name in path_names:
            params.append({"name": name, "in": "path", "required": True, "type": a_type})
            continue
        model, is_list = models.model_of(ann) if ann is not None else (None, False)
        if model:
            for fname, (ftype, freq) in (models.fields(model) or {}).items():
                body[fname] = (ftype, freq)
            continue
        if a_type is not None and a_type != "object":
            params.append(
                {"name": name, "in": "query", "required": default is None, "type": a_type}
            )
        else:
            unresolved = True  # an annotation we cannot interpret may add parameters
    return params, body, unresolved


def _field_required(call: ast.Call) -> bool:
    if _kw(call, "default") is not None or _kw(call, "default_factory") is not None:
        return False
    if call.args:
        first = call.args[0]
        return isinstance(first, ast.Constant) and first.value is Ellipsis
    return True


def _python_routes(
    project: PyProject, models: Models, framework: str, skipped: list[str]
) -> list[RouteDef]:
    """Routes for ``fastapi`` or ``flask`` (they share the decorator/router structure)."""
    ctor = {"fastapi": ("FastAPI", "APIRouter"), "flask": ("Flask", "Blueprint")}[framework]
    prefix_kw = "prefix" if framework == "fastapi" else "url_prefix"
    include_attr = "include_router" if framework == "fastapi" else "register_blueprint"

    # (module rel, variable) -> own prefix
    containers: dict[tuple[str, str], str] = {}
    for mod in project.modules.values():
        for node in mod.tree.body:
            if isinstance(node, ast.Assign):
                target, value = node.targets[0], node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                target, value = node.target, node.value
            else:
                continue
            if isinstance(value, ast.Call) and _name_of(value.func) in ctor:
                if isinstance(target, ast.Name):
                    prefix_node = _kw(value, prefix_kw)
                    own = project.string_value(mod, prefix_node) if prefix_node else ""
                    if own is None:
                        skipped.append(f"{mod.rel}:{value.lineno} (route prefix)")
                        own = ""
                    containers[(mod.rel, target.id)] = own

    def defined(m: PyModule, n: str) -> bool:
        return (m.rel, n) in containers

    # include edges: child -> list of (parent key, extra prefix)
    parents: dict[tuple[str, str], list[tuple[tuple[str, str], str]]] = {}
    for mod in project.modules.values():
        for node in ast.walk(mod.tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr != include_attr or not node.args:
                continue
            parent = project.resolve(mod, node.func.value, defined)
            child = project.resolve(mod, node.args[0], defined)
            if parent and child:
                prefix_node = _kw(node, prefix_kw)
                extra = project.string_value(mod, prefix_node) if prefix_node else ""
                if extra is None:
                    skipped.append(f"{mod.rel}:{node.lineno} (route prefix)")
                    extra = ""
                parents.setdefault((child[0].rel, child[1]), []).append(
                    ((parent[0].rel, parent[1]), extra)
                )

    def prefixes(key: tuple[str, str], _seen: frozenset = frozenset()) -> list[str]:
        own = containers.get(key, "")
        if key in _seen or key not in parents:
            return [own]
        out = []
        for parent_key, extra in parents[key]:
            for parent_prefix in prefixes(parent_key, _seen | {key}):
                out.append(
                    join_paths(parent_prefix, extra, own) if (parent_prefix or extra or own) else ""
                )
        return out or [own]

    routes: list[RouteDef] = []
    for mod in project.modules.values():
        for func in ast.walk(mod.tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in func.decorator_list:
                found = _decorator_route(dec)
                if not found:
                    continue
                recv, attr, call, methods = found
                recv_node = dec.func.value  # type: ignore[union-attr]
                key = project.resolve(mod, recv_node, defined)
                if key is None:
                    continue
                path = _route_path(call, project, mod)
                if path is None:
                    skipped.append(f"{mod.rel}:{func.lineno}")
                    continue
                verbs = methods or (["GET"] if attr in ("route", "api_route") else [attr.upper()])
                for prefix in prefixes((key[0].rel, key[1])):
                    full = join_paths(prefix, path) if (prefix or path) else "/"
                    for verb in verbs:
                        routes.append(
                            _build_route(framework, verb, full, path, func, call, models, mod)
                        )
    return routes


def _build_route(framework, verb, full, local_path, func, call, models, mod) -> RouteDef:
    route = RouteDef(verb, full, source=f"{mod.rel}:{func.lineno}")
    if framework == "fastapi":
        params, body, unresolved = _fastapi_function(func, local_path, models)
        route.params, route.body, route.unresolved = params, body, unresolved
        model_node = _kw(call, "response_model") or func.returns
        schema = None
        if model_node is not None:
            name, is_list = models.model_of(model_node)
            if name:
                schema = models.schema(name, is_list)
        status = _status_of(call, "200")
        route.responses[status] = schema
        responses_kw = _kw(call, "responses")
        if isinstance(responses_kw, ast.Dict):
            for k in responses_kw.keys:
                code = _const(k)
                if code is not None:
                    route.responses.setdefault(str(code), None)
        for code in _status_codes_raised(func):
            route.responses.setdefault(code, None)
        if route.params or route.body:
            route.responses.setdefault("422", None)
    else:  # flask
        for name, ptype in _path_params(local_path):
            route.params.append({"name": name, "in": "path", "required": True, "type": ptype})
        route.unresolved = True  # query/body access in Flask is dynamic
        for node in ast.walk(func):
            q = _flask_request_arg(node)
            if q and not any(p["name"] == q for p in route.params):
                route.params.append({"name": q, "in": "query", "required": False, "type": None})
        route.responses["200"] = None
    return route


def _flask_request_arg(node) -> str | None:
    """Name read via ``request.args.get('q')`` or ``request.args['q']``."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if node.func.attr == "get" and "args" in ast.dump(node.func.value) and node.args:
            return _const(node.args[0]) if isinstance(_const(node.args[0]), str) else None
    if isinstance(node, ast.Subscript) and "args" in ast.dump(node.value):
        if "request" in ast.dump(node.value):
            value = _const(node.slice)
            return value if isinstance(value, str) else None
    return None


# ------------------------------------------------------------------------ Django


_DJANGO_CONV = {
    "int": "integer",
    "str": "string",
    "slug": "string",
    "uuid": "string",
    "path": "string",
}


def _django_pattern(raw: str, is_regex: bool) -> tuple[str, list[tuple[str, str | None]]]:
    params: list[tuple[str, str | None]] = []
    if is_regex:
        raw = raw.lstrip("^").rstrip("$")

        def named(m):
            name = m.group(1)
            params.append((name, "integer" if r"\d" in m.group(2) else "string"))
            return "{" + name + "}"

        raw = re.sub(r"\(\?P<(\w+)>([^)]*)\)", named, raw)
        raw = re.sub(r"\\(.)", r"\1", raw)
        raw = re.sub(r"\?$", "", raw)
        return raw, params

    def conv(m):
        params.append((m.group(2), _DJANGO_CONV.get(m.group(1) or "str", "string")))
        return "{" + m.group(2) + "}"

    return re.sub(r"<(?:(\w+):)?(\w+)>", conv, raw), params


def _django_routes(project: PyProject) -> tuple[list[RouteDef], list[str]]:
    urls_files = [m for m in project.modules.values() if m.path.name == "urls.py"]
    if not urls_files:
        return [], []
    # Classes / functions for method discovery
    classes: dict[str, ast.ClassDef] = {}
    functions: dict[str, ast.FunctionDef] = {}
    for mod in project.modules.values():
        for node in mod.tree.body:
            if isinstance(node, ast.ClassDef):
                classes.setdefault(node.name, node)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                functions.setdefault(node.name, node)

    def view_methods(node) -> list[str]:
        """HTTP methods of a view expression, or [] when unknown."""
        if isinstance(node, ast.Call) and _name_of(node.func) == "as_view":
            target = node.func.value if isinstance(node.func, ast.Attribute) else None
            cls = classes.get(_name_of(target)) if target is not None else None
            if cls:
                return [
                    m.upper()
                    for m in HTTP_METHODS
                    if any(
                        isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)) and s.name == m
                        for s in cls.body
                    )
                ]
            return []
        name = _name_of(node)
        func = functions.get(name)
        if func is None:
            return []
        for dec in func.decorator_list:
            if isinstance(dec, ast.Call) and _name_of(dec.func) in (
                "api_view",
                "require_http_methods",
            ):
                found = _str_list(dec.args[0]) if dec.args else []
                if found:
                    return [m.upper() for m in found]
            if isinstance(dec, ast.Name) and dec.id.startswith("require_"):
                verb = dec.id[len("require_") :].upper()
                if verb.lower() in HTTP_METHODS:
                    return [verb]
        return []

    def conf_for(dotted: str, importer: PyModule) -> PyModule | None:
        return project.find_module(dotted, importer, 0)

    included: set[str] = set()
    for mod in urls_files:
        for node in ast.walk(mod.tree):
            if isinstance(node, ast.Call) and _name_of(node.func) == "include" and node.args:
                target = node.args[0]
                if isinstance(target, ast.Tuple) and target.elts:
                    target = target.elts[0]
                value = _const(target)
                if isinstance(value, str):
                    found = conf_for(value, mod)
                    if found:
                        included.add(found.rel)
    roots = [m for m in urls_files if m.rel not in included] or urls_files

    routes: list[RouteDef] = []
    unknown: list[str] = []

    def emit(prefix: str, params, view_node, source: str):
        methods = view_methods(view_node) if view_node is not None else []
        if not methods:
            unknown.append(prefix)
            return
        for verb in methods:
            r = RouteDef(verb, prefix, source=source, unresolved=True)
            r.params = [{"name": n, "in": "path", "required": True, "type": t} for n, t in params]
            r.responses["200"] = None
            routes.append(r)

    def walk(mod: PyModule, prefix: str, depth: int = 0):
        if depth > 10:
            return
        routers: dict[str, list[tuple[str, str | None]]] = {}
        for stmt in mod.tree.body:
            # router = DefaultRouter(); router.register(r'users', UserViewSet)
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
                call = stmt.value
                if (
                    isinstance(call.func, ast.Attribute)
                    and call.func.attr == "register"
                    and call.args
                ):
                    base = _const(call.args[0])
                    if isinstance(base, str):
                        routers.setdefault(_name_of(call.func.value), []).append(
                            (base.strip("^$/"), None)
                        )
        for node in ast.walk(mod.tree):
            if not (
                isinstance(node, ast.Call) and _name_of(node.func) in ("path", "re_path", "url")
            ):
                continue
            if len(node.args) < 2:
                continue
            raw = _const(node.args[0])
            if not isinstance(raw, str):
                continue
            is_regex = _name_of(node.func) != "path"
            pattern, params = _django_pattern(raw, is_regex)
            full = join_paths(prefix, pattern) if (prefix or pattern) else "/"
            view = node.args[1]
            source = f"{mod.rel}:{node.lineno}"
            if isinstance(view, ast.Call) and _name_of(view.func) == "include" and view.args:
                inc = view.args[0]
                if isinstance(inc, ast.Tuple) and inc.elts:
                    inc = inc.elts[0]
                value = _const(inc)
                if isinstance(value, str):
                    target = conf_for(value, mod)
                    if target:
                        walk(target, full, depth + 1)
                elif isinstance(inc, ast.Attribute) and inc.attr == "urls":
                    for base, _ in routers.get(_name_of(inc.value), []):
                        unknown.append(join_paths(full, base))
                        unknown.append(join_paths(full, base, "{pk}"))
                continue
            emit(full, params, view, source)

    for mod in roots:
        walk(mod, "")
    return routes, sorted(set(unknown))


# ------------------------------------------------------------------------ Express


_JS_SUFFIXES = (".js", ".mjs", ".cjs", ".ts")
_EXPRESS_ROUTE_RE = re.compile(
    r"\b(?P<recv>[A-Za-z_$][\w$]*)\s*\.\s*(?P<verb>get|post|put|patch|delete|head|options)\s*\(\s*"
    r"(?P<q>['\"`])(?P<path>/[^'\"`]*)(?P=q)",
)
_EXPRESS_USE_RE = re.compile(
    r"\b(?P<recv>[A-Za-z_$][\w$]*)\s*\.\s*use\s*\(\s*(?P<q>['\"`])(?P<path>/[^'\"`]*)(?P=q)\s*,\s*"
    r"(?P<arg>[A-Za-z_$][\w$.]*)"
)
_JS_IMPORT_RE = re.compile(
    r"(?:const|let|var)\s+(?P<a>[A-Za-z_$][\w$]*)\s*=\s*require\(\s*['\"](?P<b>[^'\"]+)['\"]\s*\)"
    r"|import\s+(?P<c>[A-Za-z_$][\w$]*)\s+from\s+['\"](?P<d>[^'\"]+)['\"]"
)
_JS_QUERY_RE = re.compile(r"req\.query(?:\.([A-Za-z_$][\w$]*)|\[['\"]([\w$-]+)['\"]\])")
_JS_BODY_RE = re.compile(r"req\.body(?:\.([A-Za-z_$][\w$]*)|\[['\"]([\w$-]+)['\"]\])")
_ROUTER_DEF_RE = re.compile(
    r"(?:const|let|var)\s+(?P<name>[A-Za-z_$][\w$]*)\s*=\s*(?:express\s*\.\s*Router\s*\(|express\s*\(|Router\s*\()"
)


def _express_routes(root: Path) -> list[RouteDef]:
    files = {p.relative_to(root).as_posix(): p for p in _walk_files(root, _JS_SUFFIXES)}
    texts: dict[str, str] = {}
    for rel, path in files.items():
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "express" in text or "Router" in text:
            texts[rel] = text
    if not texts:
        return []

    def resolve_import(rel: str, spec: str) -> str | None:
        if not spec.startswith("."):
            return None
        base = Path(rel).parent
        target = (base / spec).as_posix()
        parts: list[str] = []
        for seg in target.split("/"):
            if seg == "..":
                if parts:
                    parts.pop()
            elif seg != ".":
                parts.append(seg)
        stem = "/".join(parts)
        for cand in (
            [stem] + [stem + s for s in _JS_SUFFIXES] + [stem + "/index" + s for s in _JS_SUFFIXES]
        ):
            if cand in texts:
                return cand
        return None

    imports: dict[str, dict[str, str]] = {}
    for rel, text in texts.items():
        table: dict[str, str] = {}
        for m in _JS_IMPORT_RE.finditer(text):
            name = m.group("a") or m.group("c")
            spec = m.group("b") or m.group("d")
            target = resolve_import(rel, spec)
            if target:
                table[name] = target
        imports[rel] = table

    # file -> mount prefixes (a file's default-exported router mounted by others)
    mounts: dict[str, list[str]] = {}
    for rel, text in texts.items():
        for m in _EXPRESS_USE_RE.finditer(text):
            arg = m.group("arg").split(".")[0]
            target = imports[rel].get(arg)
            if target:
                for parent_prefix in mounts.get(rel, [""]):
                    mounts.setdefault(target, []).append(join_paths(parent_prefix, m.group("path")))

    routes: list[RouteDef] = []
    for rel, text in texts.items():
        containers = {m.group("name") for m in _ROUTER_DEF_RE.finditer(text)}
        matches = list(_EXPRESS_ROUTE_RE.finditer(text))
        for i, m in enumerate(matches):
            if m.group("recv") not in containers and m.group("recv") not in (
                "app",
                "router",
                "route",
            ):
                continue
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            handler = text[m.end() : end]
            local = m.group("path")
            for prefix in mounts.get(rel, [""]):
                full = join_paths(prefix, local) if (prefix or local) else "/"
                route = RouteDef(
                    m.group("verb").upper(),
                    full,
                    source=f"{rel}:{text[: m.start()].count(chr(10)) + 1}",
                )
                for name, kind in _path_params(re.sub(r":(\w+)", r"{\1}", local)):
                    route.params.append(
                        {"name": name, "in": "path", "required": True, "type": kind}
                    )
                seen = {p["name"] for p in route.params}
                for qm in _JS_QUERY_RE.finditer(handler):
                    q = qm.group(1) or qm.group(2)
                    if q not in seen:
                        seen.add(q)
                        route.params.append(
                            {"name": q, "in": "query", "required": False, "type": None}
                        )
                for bm in _JS_BODY_RE.finditer(handler):
                    route.body.setdefault(bm.group(1) or bm.group(2), (None, False))
                route.unresolved = True
                route.responses["200"] = None
                routes.append(route)
    return routes


# ------------------------------------------------------------------------ assembling a spec


def _schema(type_: str | None) -> dict:
    return {"type": type_} if type_ else {}


def _openapi_path(path: str) -> str:
    """``/a/<int:id>``, ``/a/:id`` and ``/a/{id:path}`` all become ``/a/{id}``."""
    path = re.sub(r"<(?:\w+:)?(\w+)>", r"{\1}", path)
    path = re.sub(r"\{(\w+):[^}]*\}", r"{\1}", path)
    return re.sub(r"(?<=/):([A-Za-z_]\w*)", r"{\1}", path)


def routes_to_openapi(
    routes: list[RouteDef],
    unknown_methods: list[str],
    title: str,
    unresolved_routes: list[str] | None = None,
) -> dict:
    paths: dict[str, dict] = {}
    for r in routes:
        r.path = _openapi_path(r.path)
        item = paths.setdefault(r.path, {})
        op: dict = {"parameters": [], "responses": {}, "x-source": r.source}
        seen: set[tuple[str, str]] = set()
        for p in r.params:
            key = (p["name"], p["in"])
            if key in seen:
                continue
            seen.add(key)
            op["parameters"].append(
                {
                    "name": p["name"],
                    "in": p["in"],
                    "required": bool(p["required"]),
                    "schema": _schema(p["type"]),
                }
            )
        for name, ptype in _path_params(r.path):
            if (name, "path") not in seen:
                op["parameters"].append(
                    {"name": name, "in": "path", "required": True, "schema": _schema(ptype)}
                )
        if r.body:
            op["requestBody"] = {
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "properties": {k: _schema(t) for k, (t, _) in r.body.items()},
                            "required": [k for k, (_, req) in r.body.items() if req],
                        }
                    }
                }
            }
        for code, schema in r.responses.items():
            entry: dict = {"description": ""}
            if schema:
                entry["content"] = {"application/json": {"schema": schema}}
            op["responses"][code] = entry
        if r.unresolved:
            op["x-unresolved"] = True
        item[r.method.lower()] = op
    unknown_methods = sorted({_openapi_path(p) for p in unknown_methods})
    for path in unknown_methods:
        paths.setdefault(path, {})
    return {
        "openapi": "3.0.0",
        "info": {"title": title, "version": "static"},
        "paths": paths,
        "x-unknown-methods": unknown_methods,
        "x-unresolved-routes": sorted(set(unresolved_routes or [])),
        "x-generated-from-code": True,
    }


def detect_frameworks(root: str | os.PathLike) -> list[str]:
    root = Path(root)
    found: list[str] = []
    for path in _walk_files(root, (".py",))[:2000]:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for name, pattern in (
            ("fastapi", r"^\s*(?:from|import)\s+fastapi\b"),
            ("flask", r"^\s*(?:from|import)\s+flask\b"),
            ("django", r"^\s*from\s+django\.urls\b|^\s*from\s+django\.conf\.urls\b"),
        ):
            if name not in found and re.search(pattern, text, re.M):
                found.append(name)
    if any(
        "express" in p.read_text(encoding="utf-8", errors="ignore")
        for p in _walk_files(root, _JS_SUFFIXES)[:500]
    ):
        found.append("express")
    return found


def extract_routes(root: str | os.PathLike, frameworks: list[str] | None = None) -> dict:
    """Return an OpenAPI-shaped dict for the API defined in the code under *root*."""
    root = Path(root).resolve()
    frameworks = frameworks or detect_frameworks(root)
    if not frameworks:
        raise StaticRouteError(
            "No supported web framework (FastAPI, Flask, Django, Express) was found in the code."
        )
    routes: list[RouteDef] = []
    unknown: list[str] = []
    skipped: list[str] = []
    project = None
    models = None
    if any(f in ("fastapi", "flask", "django") for f in frameworks):
        project = PyProject(root)
        models = Models(project)
    for framework in frameworks:
        if framework in ("fastapi", "flask"):
            routes += _python_routes(project, models, framework, skipped)
        elif framework == "django":
            r, u = _django_routes(project)
            routes += r
            unknown += u
        elif framework == "express":
            routes += _express_routes(root)
    if not routes and not unknown:
        raise StaticRouteError(f"Found {', '.join(frameworks)} but could not read any routes.")
    return routes_to_openapi(
        routes, unknown, title=f"{root.name} ({', '.join(frameworks)})", unresolved_routes=skipped
    )
