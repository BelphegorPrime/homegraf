"""Repository profiles: the files a new repository starts with, per project type.

A profile is a folder in repo_profiles/<name>/ with a profile.yaml and its templates:

    name, description          what ./homelab.py repo-profiles shows
    validate_command           the command AGENTS.md names and the CI job runs (e.g. python -m pytest -q)
    files: [{source, target}]  source is the template in the profile folder, target the path it is written to

A template is named so that it does not look like the file it becomes (agents.md.tmpl, forgejo-ci.yaml.tmpl);
the target carries the real path (AGENTS.md, .forgejo/workflows/ci.yaml), so a template is never mistaken for a
file of this repository. The templates use {{name}}, {{description}}, {{owner}}, {{default_branch}} and
{{validate_command}}; everything else is as written, so a pinned image of a CI job is a constant of the profile
rather than a value the caller has to know.

A workflow expression, ${{ github.token }}, is not a placeholder: only a "{{" without a "$" in front of it is
one. render therefore refuses a profile that leaves such a "{{" behind, and a template that names an unknown
placeholder or needs a value the caller did not pass.

Adding a profile means adding a folder; ./homelab.py repo-profiles lists it and
render(profile, values) writes it, without a code change.
"""
from __future__ import annotations

import dataclasses
import pathlib
import re

import yaml

DIR = pathlib.Path(__file__).parent / "repo_profiles"
PROFILE = "profile.yaml"
VALUES = ("name", "description", "owner", "default_branch", "validate_command")
OPEN = re.compile(r"(?<!\$)\{\{")            # a placeholder, but not an expression of a workflow (${{ ... }})
PLACEHOLDER = re.compile(r"(?<!\$)\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


class ProfileError(ValueError):
    """A profile or one of its templates cannot be used; the message says what to fix."""


@dataclasses.dataclass(frozen=True)
class File:
    """One file of the profile: the template in the profile folder, and the path it is written to."""
    source: str
    target: str


@dataclasses.dataclass(frozen=True)
class Profile:
    name: str
    description: str
    validate_command: str
    files: tuple[File, ...]
    folder: pathlib.Path

    def info(self) -> dict:
        """What `repo-profiles --json` prints for the app."""
        return {"name": self.name, "description": self.description,
                "validate_command": self.validate_command, "files": [f.target for f in self.files]}


def load(folder: pathlib.Path) -> Profile:
    """The profile in this folder, or ProfileError naming the file to fix."""
    folder = pathlib.Path(folder)
    meta = folder / PROFILE
    if not meta.is_file():
        raise ProfileError(f"{folder} is not a profile: it has no {PROFILE}")
    doc = yaml.safe_load(meta.read_text()) or {}
    if not isinstance(doc, dict):
        raise ProfileError(f"{meta}: expected a mapping with name, description, validate_command and files")
    for key in ("name", "description", "validate_command"):
        if not str(doc.get(key) or "").strip():
            raise ProfileError(f"{meta}: {key} is missing or empty")
    files = []
    for entry in doc.get("files") or ():
        if not isinstance(entry, dict) or not entry.get("source") or not entry.get("target"):
            raise ProfileError(f"{meta}: every entry of files needs a source and a target, not {entry!r}")
        source, target = str(entry["source"]), str(entry["target"])
        if not (folder / source).is_file():
            raise ProfileError(f"{meta}: source {source} is not in {folder}")
        if target.startswith("/") or ".." in pathlib.PurePosixPath(target).parts:
            raise ProfileError(f"{meta}: target {target} must be a path inside the new repository")
        files.append(File(source, target))
    if not files:
        raise ProfileError(f"{meta}: files is empty: a profile writes at least AGENTS.md")
    if len({f.target for f in files}) != len(files):
        raise ProfileError(f"{meta}: two entries write the same target")
    return Profile(str(doc["name"]), str(doc["description"]), str(doc["validate_command"]), tuple(files), folder)


def catalog(directory: pathlib.Path = DIR) -> dict[str, Profile]:
    """Every profile of this directory, by folder name: a new folder is a new profile, no code change."""
    directory = pathlib.Path(directory)
    return {folder.name: load(folder) for folder in sorted(p for p in directory.iterdir() if p.is_dir())}


def render(profile: Profile, values: dict[str, str]) -> dict[str, str]:
    """The files of the profile as {target: text}, every placeholder replaced.

    Refuses a missing value, an unknown placeholder, and a template that leaves a "{{" behind (an unclosed
    one, or one a value brought in), so a half-filled file never reaches a new repository.
    """
    out = {}
    for f in profile.files:
        source = profile.folder / f.source
        text = source.read_text()
        for match in OPEN.finditer(text):
            end = text.find("}}", match.end())
            if end == -1:
                raise ProfileError(f"{source}: leaves a {{{{ behind: the placeholder is not closed")
            name = text[match.end():end].strip()
            if name not in VALUES:
                raise ProfileError(f"{source}: unknown placeholder {{{{{name}}}}}; the profiles use "
                                   f"{', '.join('{{' + v + '}}' for v in VALUES)}")
            if not str(values.get(name) or "").strip():
                raise ProfileError(f"{source}: needs a value for {{{{{name}}}}}")
        text = PLACEHOLDER.sub(lambda m: str(values[m.group(1)]), text)
        if OPEN.search(text):
            raise ProfileError(f"{source}: leaves a {{{{ behind after filling in the values")
        out[f.target] = text
    return out
