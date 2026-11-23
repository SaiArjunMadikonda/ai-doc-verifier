"""
Check documentation against the project's infrastructure files.

Docs often say "the app listens on port 8080", "run ``docker compose up worker``", "set
``--set image.tag=...``" or "pass ``-var region=...``". This module reads the files those
claims come from and checks each claim:

* **Dockerfile / Containerfile**  ``EXPOSE`` ports.
* **docker-compose**              service names and published/target ports.
* **Helm**                        keys in ``values.yaml`` (for ``--set a.b.c=...``).
* **Terraform**                   ``variable "name"`` blocks (for ``-var``, ``TF_VAR_*``, ``var.x``).

Nothing is executed: files are only read. A check is only made when the matching file
exists, so a repository without Terraform is never told its docs are wrong about Terraform.
"""

from __future__ import annotations

import difflib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

MAX_BYTES = 400_000

_DOCKERFILE = re.compile(r"^(?:.*\.)?(?:Dockerfile|Containerfile)(?:\..+)?$", re.I)
_COMPOSE = re.compile(r"^(?:docker-)?compose(?:\.[\w-]+)?\.ya?ml$", re.I)
_VALUES = re.compile(r"^values(?:[-.][\w.-]+)?\.ya?ml$", re.I)


@dataclass
class InfraIndex:
    ports: dict[int, str] = field(default_factory=dict)  # port -> "file:line"
    compose_services: dict[str, str] = field(default_factory=dict)  # name -> "file:line"
    helm_keys: dict[str, str] = field(default_factory=dict)  # every dotted key and prefix
    helm_deps: set[str] = field(default_factory=set)  # sub-chart names / aliases
    tf_vars: dict[str, str] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not (self.ports or self.compose_services or self.helm_keys or self.tf_vars)


def _read(path: Path) -> str | None:
    try:
        if path.stat().st_size > MAX_BYTES:
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _line_of(text: str, pattern: str, start: int = 0) -> int:
    rx = re.compile(pattern)
    for i, line in enumerate(text.splitlines()[start:], start + 1):
        if rx.search(line):
            return i
    return 1


def _flatten(node, prefix: str, out: dict[str, str], where: str) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            out.setdefault(path, where)
            _flatten(value, path, out, where)
    elif isinstance(node, list):
        for item in node:  # list indexes are written ``[0]`` in --set; keys under them count
            _flatten(item, prefix, out, where)


def _ports_from_compose_item(item) -> list[int]:
    found: list[int] = []
    if isinstance(item, dict):
        for k in ("published", "target"):
            try:
                found.append(int(item[k]))
            except (KeyError, TypeError, ValueError):
                pass
        return found
    text = str(item).split("/")[0]
    for part in text.split(":"):
        part = part.strip()
        if part.isdigit():
            found.append(int(part))
        elif re.fullmatch(r"\d+-\d+", part):
            lo, hi = (int(x) for x in part.split("-"))
            found.extend(range(lo, min(hi, lo + 50) + 1))
    return found


def build_index(root: str | os.PathLike, files: set[str]) -> InfraIndex:
    """Read the infrastructure files among *files* (relative posix paths under *root*)."""
    try:
        import yaml
    except ImportError:  # pragma: no cover - PyYAML ships with the action
        yaml = None
    root_p = Path(root)
    idx = InfraIndex()
    for rel in sorted(files):
        name = rel.rsplit("/", 1)[-1]
        if not (
            _DOCKERFILE.match(name)
            or _COMPOSE.match(name)
            or _VALUES.match(name)
            or name == "Chart.yaml"
            or name.endswith(".tf")
        ):
            continue
        text = _read(root_p / rel)
        if text is None:
            continue
        if _DOCKERFILE.match(name):
            for number, line in enumerate(text.splitlines(), 1):
                m = re.match(r"^\s*EXPOSE\s+(.+)$", line, re.I)
                if m:
                    for tok in m.group(1).split():
                        port = tok.split("/")[0]
                        if port.isdigit():
                            idx.ports.setdefault(int(port), f"{rel}:{number}")
        elif _COMPOSE.match(name) and yaml:
            try:
                data = yaml.safe_load(text)
            except yaml.YAMLError:
                continue
            services = data.get("services") if isinstance(data, dict) else None
            if not isinstance(services, dict):
                continue
            for svc, body in services.items():
                line = _line_of(text, rf"^\s+{re.escape(str(svc))}\s*:")
                idx.compose_services.setdefault(str(svc), f"{rel}:{line}")
                if not isinstance(body, dict):
                    continue
                for key in ("ports", "expose"):
                    for item in body.get(key) or []:
                        for port in _ports_from_compose_item(item):
                            idx.ports.setdefault(port, f"{rel}:{line}")
        elif _VALUES.match(name) and yaml:
            try:
                data = yaml.safe_load(text)
            except yaml.YAMLError:
                continue
            _flatten(data, "", idx.helm_keys, rel)
        elif name == "Chart.yaml" and yaml:
            try:
                data = yaml.safe_load(text)
            except yaml.YAMLError:
                continue
            for dep in (data or {}).get("dependencies") or [] if isinstance(data, dict) else []:
                if isinstance(dep, dict):
                    for key in ("name", "alias"):
                        if dep.get(key):
                            idx.helm_deps.add(str(dep[key]))
        elif name.endswith(".tf"):
            for number, line in enumerate(text.splitlines(), 1):
                m = re.match(r'^\s*variable\s+"([\w-]+)"', line)
                if m:
                    idx.tf_vars.setdefault(m.group(1), f"{rel}:{number}")
    return idx


# ----------------------------------------------------------------------------- doc claims

_INFRA_CONTEXT = re.compile(
    r"docker|container|expose|compose|image|pod\b|helm|chart|kubernetes|k8s|listens?\b|"
    r"listening|runs on|served on|published",
    re.I,
)
_PORT_WORDS = re.compile(r"\bports?\s+`?(\d{2,5})`?\b", re.I)
_EXPOSE_LINE = re.compile(r"^\s*EXPOSE\s+(.+)$")
_P_FLAG = re.compile(r"(?<![\w-])-p\s+(?:[\d.]+:)?(\d{2,5}):(\d{2,5})(?:/\w+)?")
_LOCAL_PORT = re.compile(r"(?:localhost|127\.0\.0\.1|0\.0\.0\.0):(\d{2,5})\b")
_COMPOSE_CMD = re.compile(r"\bdocker[ -]compose\b(.*)", re.I)
_SERVICE_PHRASE = re.compile(
    r"`([A-Za-z][\w.-]*)`\s+(?:service|container)\b|\b(?:service|container)\s+`([A-Za-z][\w.-]*)`"
)
_HELM_SET = re.compile(r"--set(?:-string|-json|-file)?[ =]\s*['\"]?([^\s'\"]+)")
_HELM_KEY_IN_TEXT = re.compile(r"`([a-z][\w-]*(?:\.[\w-]+)+)`")
_TF_VAR_FLAG = re.compile(r"(?<![\w-])-var[ =]\s*['\"]?([A-Za-z_][\w-]*)=")
_TF_ENV = re.compile(r"\bTF_VAR_([A-Za-z_][\w-]*)")
_TF_REF = re.compile(r"(?<![\w.])var\.([A-Za-z_][\w-]*)")
_FILE_ENDINGS = {
    "yaml", "yml", "json", "md", "py", "js", "ts", "txt", "tf", "toml", "cfg", "ini", "sh",
    "io", "com", "org", "net", "dev", "html", "css", "lock", "log",
}  # fmt: skip

_COMPOSE_SUBCOMMANDS = {
    "up", "run", "exec", "logs", "start", "stop", "restart", "build", "ps", "pull", "push",
    "rm", "kill", "pause", "unpause", "top", "port", "images", "create", "down", "config",
    "watch", "cp", "events", "version", "ls", "convert",
}  # fmt: skip
_COMPOSE_FIRST_ONLY = {"run", "exec", "port", "cp"}
_COMPOSE_NO_SERVICES = {"down", "config", "ls", "version", "convert", "images", "events", "cp"}
_COMPOSE_VALUE_FLAGS = {
    "-f", "--file", "-p", "--project-name", "--profile", "--env-file", "--project-directory",
    "-e", "--env", "-u", "--user", "-w", "--workdir", "--scale", "--timeout", "--tail",
    "-n", "--index", "--parallel", "--ansi", "--progress", "--pull", "--build-arg", "--name",
    "-v", "--volume", "--entrypoint", "--format",
}  # fmt: skip


def _near(name: str, options) -> str | None:
    match = difflib.get_close_matches(name, list(options), n=1, cutoff=0.7)
    return match[0] if match else None


def compose_service_args(rest: str) -> list[str]:
    """Service names a ``docker compose ...`` command line refers to."""
    tokens = re.split(r"\s+", rest.strip().split("#", 1)[0])
    sub = None
    services: list[str] = []
    skip = False
    for tok in tokens:
        if not tok:
            continue
        if tok in ("&&", "||", "|", ";") or tok.endswith(";"):
            break
        if skip:
            skip = False
            continue
        if tok.startswith("-"):
            if "=" not in tok and tok in _COMPOSE_VALUE_FLAGS:
                skip = True
            continue
        if sub is None:
            if tok.strip("`") in _COMPOSE_SUBCOMMANDS:
                sub = tok.strip("`")
                if sub in _COMPOSE_NO_SERVICES:
                    return []
                continue
            return []  # not a command we understand
        name = tok.strip("`'\"")
        if name.isdigit():
            continue  # a number is the value of a flag such as ``-t 20``, not a service
        if (
            re.fullmatch(r"[A-Za-z0-9][\w.-]*", name)
            and "." not in name
            and re.search(r"[A-Za-z]", name)
        ):
            services.append(name)
        else:
            break  # a path, variable or placeholder ends the service list
        if sub in _COMPOSE_FIRST_ONLY:
            break
    return services


def check_infra(lines, file, idx, linemap):
    """Check ports, compose services, Helm values and Terraform variables named in the docs."""
    from project_checks import CONTRADICTED, UNCERTAIN, VERIFIED, ProjectFinding, remap_line

    infra: InfraIndex | None = getattr(idx, "infra", None)
    if infra is None or infra.empty:
        return []
    out: list[ProjectFinding] = []
    seen: set[tuple] = set()

    def add(verdict, number, subject, message, evidence="", fix=""):
        key = (number, subject, verdict)
        if key in seen:
            return
        seen.add(key)
        out.append(
            ProjectFinding(
                verdict, "infra", file, remap_line(linemap, number), subject, message, evidence, fix
            )
        )

    declared = sorted(infra.ports)
    for dl in lines:
        text = dl.text
        # ---- ports
        if declared:
            strong: list[int] = []
            m = _EXPOSE_LINE.match(text) if dl.fence is not None else None
            if m:
                strong += [
                    int(t.split("/")[0]) for t in m.group(1).split() if t.split("/")[0].isdigit()
                ]
            for pm in _P_FLAG.finditer(text):
                strong.append(int(pm.group(2)))
            if _INFRA_CONTEXT.search(text):
                strong += [int(p) for p in _PORT_WORDS.findall(text)]
            for port in dict.fromkeys(strong):
                if port in infra.ports:
                    add(VERIFIED, dl.number, f"port {port}", f"Port {port} is exposed by the project.",
                        infra.ports[port])  # fmt: skip
                else:
                    add(CONTRADICTED, dl.number, f"port {port}",
                        f"The docs say port {port} but the project exposes {', '.join(map(str, declared[:6]))}.",
                        "; ".join(f"{p} at {infra.ports[p]}" for p in declared[:3]),
                        f"Use one of the exposed ports ({', '.join(map(str, declared[:4]))}) or expose {port}.")  # fmt: skip
            for port in dict.fromkeys(int(p) for p in _LOCAL_PORT.findall(text)):
                if port in strong:
                    continue
                if port in infra.ports:
                    add(VERIFIED, dl.number, f"port {port}", f"Port {port} is exposed by the project.",
                        infra.ports[port])  # fmt: skip
                else:
                    add(UNCERTAIN, dl.number, f"port {port}",
                        f"Cannot confirm port {port}: the project exposes {', '.join(map(str, declared[:4]))}; "
                        "a local mapping may differ.")  # fmt: skip
        # ---- compose services
        if infra.compose_services:
            names: list[str] = []
            cm = _COMPOSE_CMD.search(text)
            if cm:
                names += compose_service_args(cm.group(1))
            if dl.fence is None:
                for sm in _SERVICE_PHRASE.finditer(text):
                    names.append(sm.group(1) or sm.group(2))
            for name in dict.fromkeys(names):
                if name in infra.compose_services:
                    add(VERIFIED, dl.number, f"service `{name}`",
                        f"Compose service `{name}` exists.", infra.compose_services[name])  # fmt: skip
                else:
                    hint = _near(name, infra.compose_services)
                    add(CONTRADICTED, dl.number, f"service `{name}`",
                        f"The docs mention compose service `{name}` but no such service is defined "
                        f"(services: {', '.join(sorted(infra.compose_services)[:8])}).",
                        "searched the docker-compose file(s)",
                        f"Use `{hint}`." if hint else "Fix the service name or remove the sentence.")  # fmt: skip
        # ---- helm values
        if infra.helm_keys:
            keys: list[str] = []
            for hm in _HELM_SET.finditer(text):
                for assignment in re.split(r",(?=[\w.\[\]-]+=)", hm.group(1)):
                    if "=" in assignment:
                        keys.append(assignment.split("=", 1)[0])
            if re.search(r"values|helm|chart", text, re.I):
                keys += [
                    k
                    for k in _HELM_KEY_IN_TEXT.findall(text)
                    if k.rsplit(".", 1)[-1] not in _FILE_ENDINGS
                ]
            for raw in dict.fromkeys(keys):
                if "\\" in raw:
                    continue  # an escaped dot: the key itself contains a dot
                key = re.sub(r"\[\d+\]", "", raw)
                first = key.split(".")[0]
                if first == "global" or first in infra.helm_deps:
                    add(UNCERTAIN, dl.number, f"value `{key}`",
                        f"Cannot confirm `{key}`: it belongs to a sub-chart or the global values.")  # fmt: skip
                elif key in infra.helm_keys:
                    add(VERIFIED, dl.number, f"value `{key}`",
                        f"Helm value `{key}` exists.", infra.helm_keys[key])  # fmt: skip
                else:
                    hint = _near(key, infra.helm_keys)
                    add(CONTRADICTED, dl.number, f"value `{key}`",
                        f"The docs use Helm value `{key}` but values.yaml has no such key.",
                        "searched values.yaml",
                        f"Use `{hint}`." if hint else "Fix the key or add it to values.yaml.")  # fmt: skip
        # ---- terraform variables
        if infra.tf_vars:
            names = _TF_VAR_FLAG.findall(text) + _TF_ENV.findall(text) + _TF_REF.findall(text)
            for name in dict.fromkeys(names):
                if name in infra.tf_vars:
                    add(VERIFIED, dl.number, f"variable `{name}`",
                        f"Terraform variable `{name}` is declared.", infra.tf_vars[name])  # fmt: skip
                else:
                    hint = _near(name, infra.tf_vars)
                    add(CONTRADICTED, dl.number, f"variable `{name}`",
                        f"The docs use Terraform variable `{name}` but no `variable \"{name}\"` block exists.",
                        "searched *.tf files",
                        f"Use `{hint}`." if hint else "Fix the name or declare the variable.")  # fmt: skip
    return out
