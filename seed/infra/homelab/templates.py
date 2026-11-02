"""Templates for new apps: a starting point such as a web server, with the configuration files it needs.

A template is a folder in templates/<id>/. Its compose.yaml (so Renovate keeps the image current) has the service
"app", whose image, port and volumes are the defaults of the form, and `x-template` for label and description. The
other files are copied to stacks/<id>/ when the app's compose file mounts them from the repo (./default.conf, ./html).
"""
from __future__ import annotations

import dataclasses
import pathlib

import yaml

DIR = pathlib.Path(__file__).parent / "templates"
COMPOSE = "compose.yaml"


@dataclasses.dataclass(frozen=True)
class Template:
    id: str
    label: str
    description: str
    health_path: str
    compose: str                # the template's own compose file: the form's defaults come from its service "app"
    files: dict[str, str]       # path relative to the template folder -> text, everything but compose.yaml


def catalog(directory: pathlib.Path = DIR) -> dict[str, Template]:
    out = {}
    for folder in sorted(p for p in directory.iterdir() if (p / COMPOSE).is_file()):
        text = (folder / COMPOSE).read_text()
        meta = yaml.safe_load(text)["x-template"]
        files = {f.relative_to(folder).as_posix(): f.read_text() for f in sorted(folder.rglob("*"))
                 if f.is_file() and f.relative_to(folder).as_posix() != COMPOSE}
        out[folder.name] = Template(folder.name, meta["label"], meta["description"], str(meta.get("health_path") or "/"), text, files)
    return out


def referenced(template: Template, sources: list[str]) -> list[str]:
    """The template files a compose file uses: those a repo volume source (./file or ./folder) names or contains."""
    roots = [s.removeprefix("./").rstrip("/") for s in sources if s.startswith("./")]
    return [name for name in template.files if any(name == r or name.startswith(r + "/") for r in roots)]
