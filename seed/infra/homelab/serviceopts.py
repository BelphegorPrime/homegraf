"""Options of the app's own service beyond image and port: volumes, networks, limits, GPU, plain environment.

One place for the three things the wizard does with them: parse and check what the flags (or the form) say, write them
into the compose file, and read them back from a compose file (so the form can show what an edited file says).
"""
from __future__ import annotations

import dataclasses
import re

VOLUME_KINDS = ("data", "bind", "named", "tmpfs", "repo")
GPUS = ("nvidia", "dri")
ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}")
SIZE = re.compile(r"\d+(\.\d+)?[kKmMgG]?[bB]?")


@dataclasses.dataclass(frozen=True)
class Volume:
    """kind: data = /srv/docker/<app>[/source], bind = a host path, named = a Docker volume, tmpfs = memory,
    repo = a path relative to stacks/<app> (a file or folder of this repository)."""
    kind: str
    source: str
    target: str
    read_only: bool = False
    size: str = ""        # tmpfs only, e.g. 8g

    def spec(self) -> str:
        """The flag grammar KIND:SOURCE:TARGET[:ro|:size=N], the one --volume takes and parse_volume reads."""
        tail = ":ro" if self.read_only else (f":size={self.size}" if self.size else "")
        return f"{self.kind}:{self.source}:{self.target}{tail}"


@dataclasses.dataclass(frozen=True)
class Network:
    name: str
    external: bool = False   # an existing Docker network (created elsewhere), otherwise a network of this stack


@dataclasses.dataclass
class ServiceOptions:
    volumes: list[Volume] = dataclasses.field(default_factory=list)
    networks: list[Network] = dataclasses.field(default_factory=list)
    cpus: str = ""
    memory: str = ""
    gpu: str = ""
    env: dict[str, str] = dataclasses.field(default_factory=dict)


def parse_volume(spec: str) -> Volume:
    kind, _, rest = spec.partition(":")
    if kind not in VOLUME_KINDS:
        raise SystemExit(f"--volume {spec!r}: the kind is one of {', '.join(VOLUME_KINDS)} (KIND:SOURCE:TARGET[:ro])")
    parts = rest.split(":")
    if len(parts) not in (2, 3) or not parts[1]:
        raise SystemExit(f"--volume {spec!r}: expected KIND:SOURCE:TARGET[:ro], e.g. data::/data or bind:/mnt/tank/media:/media:ro")
    source, target, opt = parts[0], parts[1], (parts[2] if len(parts) == 3 else "")
    size = ""
    if opt.startswith("size="):
        size, opt = opt[5:], ""
    if opt not in ("", "ro"):
        raise SystemExit(f"--volume {spec!r}: the option is ro (read-only), or size=8g for tmpfs")
    if not re.fullmatch(r"/[\w./@+-]*", target) or "//" in target:
        raise SystemExit(f"--volume {spec!r}: the target is an absolute path inside the container")
    if kind == "data" and not re.fullmatch(r"([\w.@+-]+(/[\w.@+-]+)*)?", source):
        raise SystemExit(f"--volume {spec!r}: a data source is a folder name below /srv/docker/<app> (or empty)")
    if kind == "bind" and not re.fullmatch(r"/[\w./@+-]+", source):
        raise SystemExit(f"--volume {spec!r}: a bind source is an absolute path on the server")
    if kind == "named" and not NAME.fullmatch(source):
        raise SystemExit(f"--volume {spec!r}: a named volume needs a name (letters, digits, - _ .)")
    if kind == "repo" and not re.fullmatch(r"\.{1,2}(/[\w.@+-]+)+/?|\./", source):
        raise SystemExit(f"--volume {spec!r}: a repo source is a relative path like ./config or ../shared/file")
    if kind == "tmpfs" and source:
        raise SystemExit(f"--volume {spec!r}: tmpfs has no source (tmpfs::/tmp)")
    if size and (kind != "tmpfs" or not SIZE.fullmatch(size)):
        raise SystemExit(f"--volume {spec!r}: size= is for tmpfs, e.g. size=512m")
    if opt == "ro" and kind == "tmpfs":
        raise SystemExit(f"--volume {spec!r}: tmpfs cannot be read-only")
    return Volume(kind, source, target, opt == "ro", size)


def parse_env(spec: str) -> tuple[str, str]:
    key, sep, value = spec.partition("=")
    if not sep or not ENV_KEY.fullmatch(key):
        raise SystemExit(f"--env {spec!r}: expected NAME=value (letters, digits, _ in the name)")
    return key, value


def check_limits(cpus: str, memory: str, gpu: str) -> None:
    if cpus and not (re.fullmatch(r"\d+(\.\d+)?", cpus) and 0 < float(cpus) <= 256):
        raise SystemExit(f"--cpus {cpus}: a number of CPUs, e.g. 1.5")
    if memory and not re.fullmatch(r"[1-9]\d*[kKmMgG]", memory):
        raise SystemExit(f"--memory {memory}: a size like 512m or 2g")
    if gpu and gpu not in GPUS:
        raise SystemExit(f"--gpu {gpu}: one of {', '.join(GPUS)}")


def options_from(p: dict) -> ServiceOptions:
    """From the wizard's presets (flags): volume, network, external_network, cpus, memory, gpu, env."""
    networks = [Network(n) for n in p.get("network") or []] + [Network(n, True) for n in p.get("external_network") or []]
    for n in networks:
        if not NAME.fullmatch(n.name) or n.name == "default":
            raise SystemExit(f"network {n.name!r}: a name (letters, digits, - _ .), not 'default'")
    names = [n.name for n in networks]
    if len(set(names)) != len(names):
        raise SystemExit("a network is listed twice")
    volumes = [parse_volume(s) for s in p.get("volume") or []]
    targets = [v.target for v in volumes]
    if len(set(targets)) != len(targets):
        raise SystemExit("two volumes have the same target path inside the container")
    env = dict(parse_env(s) for s in p.get("env") or [])
    check_limits(p.get("cpus") or "", p.get("memory") or "", p.get("gpu") or "")
    return ServiceOptions(volumes, networks, p.get("cpus") or "", p.get("memory") or "", p.get("gpu") or "", env)


# --- writing -------------------------------------------------------------------------------------------------------

def volume_entry(v: Volume, app_id: str) -> str:
    """The text in the compose file: a volumes: entry, or for tmpfs a tmpfs: entry."""
    if v.kind == "tmpfs":
        return v.target + (f":size={v.size}" if v.size else "")
    source = {"data": f"/srv/docker/{app_id}" + (f"/{v.source}" if v.source else ""), "bind": v.source,
              "named": v.source, "repo": v.source}[v.kind]
    return f"{source}:{v.target}" + (":ro" if v.read_only else "")


def service_lines(o: ServiceOptions, app_id: str) -> dict[str, list[str]]:
    """Compose lines (indented for a service) by section, for what the options add to the app's service."""
    out: dict[str, list[str]] = {}
    vols = [volume_entry(v, app_id) for v in o.volumes if v.kind != "tmpfs"]
    if vols:
        out["volumes"] = ["    volumes:"] + [f"      - {_q(x)}" for x in vols]
    tmp = [volume_entry(v, app_id) for v in o.volumes if v.kind == "tmpfs"]
    if tmp:
        out["tmpfs"] = ["    tmpfs:"] + [f"      - {_q(x)}" for x in tmp]
    if o.networks:
        out["networks"] = ["    networks:", "      - default"] + [f"      - {n.name}" for n in o.networks]
    if o.cpus:
        out["cpus"] = [f'    cpus: "{o.cpus}"']
    if o.memory:
        out["mem_limit"] = [f"    mem_limit: {o.memory}"]
    if o.gpu == "dri":
        out["devices"] = ["    devices:", "      - /dev/dri:/dev/dri"]
    if o.gpu == "nvidia":
        out["deploy"] = ["    deploy:", "      resources:", "        reservations:", "          devices:", "            - driver: nvidia",
                         "              count: all", "              capabilities:", "                - gpu"]
    return out


def top_level(o: ServiceOptions) -> list[str]:
    """Compose text for the top-level networks: and volumes: sections the options need."""
    lines: list[str] = []
    if o.networks:
        lines += ["", "networks:"]
        for n in o.networks:
            lines += [f"  {n.name}:", "    external: true"] if n.external else [f"  {n.name}: {{}}"]
    named = list(dict.fromkeys(v.source for v in o.volumes if v.kind == "named"))
    if named:
        lines += ["", "volumes:"] + [f"  {n}: {{}}" for n in named]
    return lines


def _q(s: str) -> str:
    """Quote a compose scalar only when YAML would misread it (a colon followed by a digit pair is a sexagesimal in YAML 1.1)."""
    return f'"{s}"' if re.search(r"[#{}\[\],&*!|>'\"%@`]|:\s|^\s|\s$", s) else s


def env_lines(env: dict[str, str]) -> list[str]:
    return [f"      {k}: {_val(v)}" for k, v in env.items()]


def _val(v: str) -> str:
    """A compose scalar for an environment value: plain when YAML reads it back as the same string, else double-quoted
    (empty values, true/false/null/numbers, anything with YAML syntax). Compose still interpolates ${VAR} in quotes."""
    plain = re.fullmatch(r"[A-Za-z0-9_./@+=-][A-Za-z0-9_./@+=:,-]*", v) and not re.fullmatch(r"(?i)(true|false|yes|no|on|off|null|y|n|~)|[-+]?\d[\d_.:eE+-]*", v)
    return v if plain else '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'


# --- reading back ---------------------------------------------------------------------------------------------------

def read_volume(entry, app_id: str) -> Volume | None:
    """A volumes: entry (short string or long mapping) as the form shows it; None for what the form has no row for."""
    if isinstance(entry, dict):
        src, target, ro = str(entry.get("source") or ""), str(entry.get("target") or ""), bool(entry.get("read_only"))
        if entry.get("type") == "tmpfs":
            return Volume("tmpfs", "", target) if target else None
    elif isinstance(entry, str):
        parts = entry.split(":")
        if len(parts) < 2:
            return None
        src, target, ro = parts[0], parts[1], "ro" in parts[2:]
    else:
        return None
    base = f"/srv/docker/{app_id}"
    if not target.startswith("/") or not src:
        return None
    if src == base or src.startswith(base + "/"):
        return Volume("data", src[len(base) + 1:], target, ro)
    if src.startswith("/"):
        return Volume("bind", src, target, ro)
    if src.startswith("."):
        return Volume("repo", src, target, ro)
    return Volume("named", src, target, ro)


def read_tmpfs(entry) -> Volume | None:
    if not isinstance(entry, str):
        return None
    path, _, opts = entry.partition(":")
    size = next((o[5:] for o in opts.split(",") if o.startswith("size=")), "")
    return Volume("tmpfs", "", path, False, size) if path.startswith("/") else None


def env_dict(environment) -> dict[str, str]:
    """environment: as a mapping, from either of compose's two forms (mapping or NAME=value list)."""
    if isinstance(environment, dict):
        return {str(k): "" if v is None else str(v) for k, v in environment.items()}
    if isinstance(environment, list):
        return {k: v for k, _, v in (str(e).partition("=") for e in environment) if k}
    return {}


def read_options(svc: dict, app_id: str, compose: dict, skip_env: set[str]) -> dict:
    """What the form shows for the app's service: volumes, networks, limits, gpu and the plain environment."""
    volumes = [v for v in (read_volume(e, app_id) for e in svc.get("volumes") or []) if v]
    volumes += [v for v in (read_tmpfs(e) for e in svc.get("tmpfs") or []) if v]
    top = compose.get("networks") or {}
    listed = svc.get("networks") or []
    names = list(listed) if isinstance(listed, list) else list(listed or {})
    networks = [{"name": str(n), "external": bool((top.get(n) or {}).get("external"))} for n in names if n != "default"]
    cpus = svc.get("cpus")
    mem = svc.get("mem_limit")
    gpu = ""
    if any((isinstance(d, dict) and d.get("driver") == "nvidia")
           for d in (((svc.get("deploy") or {}).get("resources") or {}).get("reservations") or {}).get("devices") or []):
        gpu = "nvidia"
    elif any(str(d).startswith("/dev/dri") for d in svc.get("devices") or []):
        gpu = "dri"
    env = {k: v for k, v in env_dict(svc.get("environment")).items() if k not in skip_env and v != "${%s}" % k}
    return {"volumes": [{"kind": v.kind, "source": v.source, "target": v.target, "readOnly": v.read_only, "size": v.size} for v in volumes],
            "networks": networks, "cpus": "" if cpus is None else str(cpus), "memory": "" if mem is None else str(mem),
            "gpu": gpu, "env": env}
