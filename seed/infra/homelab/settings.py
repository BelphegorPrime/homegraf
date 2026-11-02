"""Settings of one installation of the homelab tools, from inventory/homegraf.yaml. Every key is optional.

  domain: lab.example                 # apps are reachable at <app>.<domain>; default: the domain of the CA policy, else homegraf.lan
  repository: owner/infrastructure    # this repository on the forge (the Komodo repo resources clone it); default: from `origin`
  infisical_url: http://10.0.0.45:8079
  platform_stacks: [infisical, reverseProxy]   # stack ids that stay out of remove-app, redeploy and secrets in the UI
  platform_prefixes: ["git--"]                 # the same for every stack id that starts like this
  platform:                           # what the map shows as the platform around the stacks (id: label, stack that runs it, kind)
    npm: {label: Nginx Proxy Manager, stack: reverseProxy, kind: proxy}

homegraf itself is always part of the platform: the UI cannot remove or redeploy the container it runs in.
"""
from __future__ import annotations

import fnmatch
import os
import pathlib
import re
import subprocess

import yaml

CODE = pathlib.Path(__file__).resolve().parents[2]                      # where the tools are
ROOT = pathlib.Path(os.environ.get("HOMELAB_ROOT") or CODE)             # the repository they work on
FILE = "inventory/homegraf.yaml"
SELF = "homegraf"
FALLBACK_DOMAIN = "homegraf.lan"


def load(root: pathlib.Path | None = None) -> dict:
    path = pathlib.Path(root or ROOT) / FILE
    return (yaml.safe_load(path.read_text()) or {}) if path.is_file() else {}


def _yaml(root: pathlib.Path, name: str) -> dict:
    path = root / "inventory" / name
    return (yaml.safe_load(path.read_text()) or {}) if path.is_file() else {}


def policy_domains(root: pathlib.Path | None = None) -> list[str]:
    """The name patterns the certificate authority may issue for (inventory/ingress.yaml), e.g. ['*.lab.example']."""
    issuers = _yaml(pathlib.Path(root or ROOT), "ingress.yaml").get("issuers") or {}
    return [d for i in issuers.values() for d in (i or {}).get("allowed_domains") or []]


def domain(root: pathlib.Path | None = None) -> str:
    """The suffix of the apps' names: the setting, else the first domain of the CA policy, else homegraf.lan."""
    root = pathlib.Path(root or ROOT)
    if configured := load(root).get("domain"):
        return str(configured).lstrip("*.")
    for pattern in policy_domains(root):
        if pattern.startswith("*."):
            return pattern[2:]
    return FALLBACK_DOMAIN


def matches_policy(name: str, root: pathlib.Path | None = None) -> bool:
    """May a certificate be issued for this name? Without any CA policy every name under the domain is fine."""
    patterns = policy_domains(root)
    return any(fnmatch.fnmatch(name, p) for p in patterns) if patterns else name.endswith("." + domain(root))


def repository(root: pathlib.Path | None = None) -> str | None:
    """owner/name of this repository on the forge: the setting, else read from the `origin` remote."""
    root = pathlib.Path(root or ROOT)
    if configured := load(root).get("repository"):
        return str(configured)
    try:
        url = subprocess.run(["git", "-C", str(root), "remote", "get-url", "origin"], capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    m = re.match(r"^(?:[a-z+]+://)?(?:[^@/]+@)?[^/:]+(?::\d+)?[:/](?P<repo>[^/].*?)(?:\.git)?/?$", url)
    return m["repo"] if m else None


def infisical_url(root: pathlib.Path | None = None) -> str | None:
    return load(root).get("infisical_url") or None


def repos_dir(root: pathlib.Path | None = None) -> str | None:
    """The host directory Komodo's agent keeps its repository clones in; None: derive it from a stack that exists."""
    return load(root).get("repos_dir") or None


def is_platform(stack_id: str, root: pathlib.Path | None = None) -> bool:
    """A stack the UI never removes, redeploys or manages secrets of."""
    cfg = load(root)
    return (stack_id == SELF or stack_id in (cfg.get("platform_stacks") or [])
            or any(stack_id.startswith(p) for p in cfg.get("platform_prefixes") or []))


def platform(root: pathlib.Path | None = None) -> dict[str, tuple[str, str, str]]:
    """The platform services of the map: id -> (label, stack that runs it, kind)."""
    return {k: (v.get("label") or k, v.get("stack") or "", v.get("kind") or "service")
            for k, v in (load(root).get("platform") or {}).items()}
