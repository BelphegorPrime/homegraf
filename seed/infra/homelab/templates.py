"""Templates for new apps: a starting point such as a web server or a ready-made app, with what it needs.

A template is a folder in templates/<id>/. Its compose.yaml (so Renovate keeps the images current) has the service
"app", whose image, port, volumes and environment are the defaults of the form, and `x-template` for the metadata:

    label, description, category      what the gallery shows
    health_path, websockets, statuses routing defaults (what the app needs behind the proxy)
    addons: [postgres, ...]           building blocks that come with it (the app's environment wires them: POSTGRES_SERVER: postgres)
    addon_env: true                   also add the blocks' own variables (POSTGRES_URL, ...) to the app; default: the template wires itself
    host_paths: [/media]              container paths whose host folder the person has to choose (the volume's source is a stand-in)
    first_run: text                   what to do once it runs (create the first user, ...)

Every other service of the compose file that is not a building block is a sidecar (a second container the app needs,
for example a converter): it is added to the app's compose file as it is. The text may use __APP__ (the app ID), __URL__
(https://<domain> where the app is reached) and __DOMAIN__. Every ${VAR} in the file is a secret that is generated.
The other files of the folder are copied to stacks/<id>/ when the app's compose file mounts them from the repo
(./default.conf, ./html).

Several templates can share one compose file (new-app --template A --template B): the first is the app <id>, every
further one is the service <id>-<template> with building blocks, sidecars, folders (/srv/docker/<id>-<template>) and
secrets (<TEMPLATE>_VAR) of its own, so two never share a database. See newapp.isolate.
"""
from __future__ import annotations

import dataclasses
import pathlib
import re

import yaml

DIR = pathlib.Path(__file__).parent / "templates"
COMPOSE = "compose.yaml"
CATEGORIES = ("web server", "media", "documents", "productivity", "ai", "tools")

# What the form models for the app's service: everything else of it (healthcheck, user, command, ...) is copied as it is.
MODELLED = {"image", "container_name", "restart", "ports", "network_mode", "environment", "volumes", "tmpfs", "networks",
            "deploy", "devices", "depends_on", "cpus", "mem_limit"}


@dataclasses.dataclass(frozen=True)
class Template:
    id: str
    label: str
    description: str
    health_path: str
    compose: str                # the template's own compose file: the form's defaults come from its service "app"
    files: dict[str, str]       # path relative to the template folder -> text, everything but compose.yaml
    category: str = "tools"
    addons: tuple[str, ...] = ()
    addon_env: bool = False
    websockets: bool = False
    statuses: tuple[int, ...] = ()
    host_paths: tuple[str, ...] = ()
    first_run: str = ""
    sidecars: dict[str, str] = dataclasses.field(default_factory=dict)     # service name -> its text (2 spaces indented, as in compose.yaml)
    extras: dict = dataclasses.field(default_factory=dict)                 # keys of the app's service the form does not model

    def secrets(self) -> set[str]:
        """The variables the template uses as ${VAR} without a default: all of them are generated."""
        import newapp
        return newapp.compose_vars("\n".join(service_blocks(self.compose).values()))[0]


def service_blocks(text: str) -> dict[str, str]:
    """The text of each service under `services:`, as written (comments stay), in file order."""
    out: dict[str, list[str]] = {}
    name = None
    inside = False
    for line in text.splitlines():
        if re.match(r"services:\s*$", line):
            inside = True
        elif inside and re.match(r"\S", line) and not line.startswith("#"):
            inside = False
        if not inside or line.startswith("services:"):
            continue
        if m := re.match(r"  ([A-Za-z0-9._-]+):\s*$", line):
            name = m.group(1)
            out[name] = []
        if name is not None:
            out[name].append(line)
    return {n: "\n".join(lines).rstrip("\n") + "\n" for n, lines in out.items()}


def catalog(directory: pathlib.Path = DIR) -> dict[str, Template]:
    import addons
    known = addons.catalog()
    out = {}
    for folder in sorted(p for p in directory.iterdir() if (p / COMPOSE).is_file()):
        text = (folder / COMPOSE).read_text()
        doc = yaml.safe_load(text)
        meta = doc["x-template"]
        files = {f.relative_to(folder).as_posix(): f.read_text() for f in sorted(folder.rglob("*"))
                 if f.is_file() and f.relative_to(folder).as_posix() != COMPOSE}
        blocks = tuple(meta.get("addons") or ())
        sidecars = {n: t for n, t in service_blocks(text).items() if n != "app" and n not in known}
        extras = {k: v for k, v in doc["services"]["app"].items() if k not in MODELLED}
        out[folder.name] = Template(
            folder.name, meta["label"], meta["description"], str(meta.get("health_path") or "/"), text, files,
            str(meta.get("category") or "tools"), blocks, bool(meta.get("addon_env")), bool(meta.get("websockets")),
            tuple(int(s) for s in meta.get("statuses") or ()), tuple(meta.get("host_paths") or ()),
            str(meta.get("first_run") or "").strip(), sidecars, extras)
    return out


def referenced(template: Template, sources: list[str]) -> list[str]:
    """The template files a compose file uses: those a repo volume source (./file or ./folder) names or contains."""
    roots = [s.removeprefix("./").rstrip("/") for s in sources if s.startswith("./")]
    return [name for name in template.files if any(name == r or name.startswith(r + "/") for r in roots)]
