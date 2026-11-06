"""The stacks of a repository: inventory/stacks.yaml, plus the stacks that exist only as a folder.

A folder stacks/<id>/ with a compose file and an ingress.yaml is a stack without an entry in inventory/stacks.yaml (ingressctl derives
it, and the reconcile stage of update-all sets it up on push). Every tool that lists or looks up stacks reads them through here, so
such a stack shows up in the map, in `homelab.py stacks|removable|secrets`, in stack_secrets.py and in the checks. An entry in
stacks.yaml always wins over the derived one; derived entries have status "derived" and `secrets: infisical`.
"""
from __future__ import annotations

import pathlib
import sys

import yaml

CODE = pathlib.Path(__file__).resolve().parents[2]       # where the tools are


def _read(path: pathlib.Path) -> dict:
    try:
        return yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return {}


def listed(root: pathlib.Path) -> list[dict]:
    """The entries of inventory/stacks.yaml."""
    return _read(root / "inventory/stacks.yaml").get("stacks") or []


def derived(root: pathlib.Path, known: list[dict]) -> list[dict]:
    """The folder-only stacks that `known` does not list."""
    src = str(CODE / "infra/ingress-controller/src")
    if src not in sys.path:
        sys.path.insert(0, src)
    from ingressctl.inventory import derive_stacks
    namespaces = _read(root / "inventory/namespaces.yaml").get("namespaces") or {}
    return list(derive_stacks(root, namespaces, {s["id"]: s for s in known if isinstance(s, dict) and "id" in s}).values())


def load(root: pathlib.Path) -> list[dict]:
    """inventory/stacks.yaml followed by the derived stacks."""
    entries = listed(root)
    return entries + derived(root, entries)
