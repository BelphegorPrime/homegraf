"""The initial content of the user's repository: the seed folder with its placeholders filled in.

Placeholders (written @@NAME@@ in the seed, text files only):
  @@DOMAIN@@         the domain the services and apps are reached under
  @@HOST_IP@@        the address of this host in the LAN
  @@SERVER@@         the name of the Komodo server this host is registered as
  @@REPOSITORY@@     owner/name of this repository on Forgejo
  @@REPOS_DIR@@      where Komodo's agent keeps its clones of repositories on this host (absolute)
  @@FORGEJO_URL@@    Forgejo as the containers reach it
  @@INFISICAL_URL@@  Infisical as the containers reach it
  @@ADMIN_USER@@     the administrator's name
  @@NETWORK@@        the compose network of the stack (ingressctl joins it to reach the proxy and Infisical by name)
  @@CREDENTIALS_VOLUME@@  the volume the bootstrap puts ingressctl's credentials on
  @@STATE_VOLUME@@   the volume of ingressctl's state
"""
from __future__ import annotations

import pathlib
import re

PLACEHOLDER = re.compile(r"@@([A-Z_]+)@@")
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".pytest_cache"}


def substitutions(cfg, server: str) -> dict[str, str]:
    return {"DOMAIN": cfg.domain, "HOST_IP": cfg.host_ip, "SERVER": server, "REPOSITORY": cfg.repository, "REPOS_DIR": cfg.repos_dir,
            "FORGEJO_URL": cfg.forgejo_url, "INFISICAL_URL": cfg.infisical_url, "ADMIN_USER": cfg.admin_user,
            "NETWORK": cfg.network, "CREDENTIALS_VOLUME": cfg.credentials_volume,
            "STATE_VOLUME": cfg.state_volume}


def seed_dir(cfg) -> pathlib.Path:
    """The real seed when the image has one, else the stub (a README and an empty inventory)."""
    real = pathlib.Path(cfg.seed_dir)
    return real if real.is_dir() and any(real.iterdir()) else pathlib.Path(cfg.seed_dir + "-stub")


def collect(directory: pathlib.Path, subs: dict[str, str]) -> list[tuple[str, bytes]]:
    """(path in the repository, content) for every file, placeholders replaced in text files; unknown placeholders are an error."""
    files = []
    for path in sorted(p for p in directory.rglob("*") if p.is_file() and not (SKIP_DIRS & set(p.relative_to(directory).parts))):
        raw = path.read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            files.append((path.relative_to(directory).as_posix(), raw))
            continue
        unknown = {m for m in PLACEHOLDER.findall(text) if m not in subs}
        if unknown:
            raise ValueError(f"{path.relative_to(directory)}: unknown placeholder(s) {sorted(unknown)}")
        files.append((path.relative_to(directory).as_posix(), PLACEHOLDER.sub(lambda m: subs[m.group(1)], text).encode()))
    return files
