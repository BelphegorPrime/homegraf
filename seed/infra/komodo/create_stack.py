#!/usr/bin/env python3
"""Create the Komodo stack for an inventory entry (files_on_host in the server's infrastructure clone).

  . infra/komodo/komodo.env
  python3 infra/komodo/create_stack.py <inventory-id>            # dry run: show the stack that would be created
  python3 infra/komodo/create_stack.py <inventory-id> --apply    # create it (does not deploy)

The inventory entry (inventory/stacks.yaml) must set `server` and `komodo_stack`. The run_directory is
<periphery-root>/data/komodo/repos/<infrastructure repo resource of that server>/<path>, the same layout
all migrated stacks use. The periphery root is taken from an existing stack on the same server.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import re
import sys

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import komodo_api as k  # noqa: E402

CODE = pathlib.Path(__file__).resolve().parents[2]                      # where the tools are
ROOT = pathlib.Path(os.environ.get("HOMELAB_ROOT") or CODE)             # the repository they work on
sys.path.insert(0, str(CODE / "infra/homelab"))
import settings  # noqa: E402


def repo_resource(server: str) -> tuple[str, str]:
    """(Komodo server id, name of the infrastructure repo resource cloned on that server)."""
    MONOREPO = settings.repository(ROOT) or sys.exit("the repository on the forge is unknown: set `repository` in inventory/homegraf.yaml")
    servers = {s["name"]: s["id"] for s in k.read("ListServers")}
    server_id = servers.get(server) or sys.exit(f"unknown Komodo server {server}")
    repos = [r["name"] for r in k.read("ListFullRepos")
             if (r["config"].get("repo") or "").removesuffix(".git") == MONOREPO and r["config"].get("server_id") == server_id]
    if len(repos) != 1:
        sys.exit(f"expected one {MONOREPO} repo resource on {server}, found {repos}")
    return server_id, repos[0]


def builds_image(entry: dict, root: pathlib.Path = ROOT) -> bool:
    """True if a compose file of the entry has a service with `build:`: Komodo then has to build on deploy (run_build)."""
    for name in entry.get("compose_files") or []:
        try:
            services = (yaml.safe_load((root / entry["path"] / name).read_text()) or {}).get("services") or {}
        except (OSError, yaml.YAMLError):
            continue
        if any(isinstance(svc, dict) and "build" in svc for svc in services.values()):
            return True
    return False


def stack_config(entry: dict, repos_dir: str | None = None) -> dict:
    """Komodo stack config for an inventory entry; exits if the stack already exists."""
    if not entry.get("server") or not entry.get("komodo_stack"):
        sys.exit("set `server` and `komodo_stack` in the inventory entry first")
    if any(s["name"] == entry["komodo_stack"] for s in k.read("ListStacks")):
        sys.exit(f"Komodo stack {entry['komodo_stack']} already exists")
    server_id, repo = repo_resource(entry["server"])
    repos_dir = repos_dir or settings.repos_dir(ROOT)   # the flag, then inventory/homegraf.yaml, then an existing stack
    if not repos_dir:
        for st in k.read("ListFullStacks", {"limit": 0}):
            m = re.match(r"^(.*/data/komodo/repos)/", st["config"].get("run_directory") or "")
            if st["config"].get("server_id") == server_id and m:
                repos_dir = m.group(1)
                break
    if not repos_dir:
        sys.exit("no existing stack on this server to derive the periphery root from; set `repos_dir` in inventory/homegraf.yaml or pass --repos-dir")
    config = {
        "server_id": server_id,
        "files_on_host": True,
        "run_directory": f"{repos_dir}/{repo}/{entry['path']}",
        "file_paths": entry["compose_files"],
    }
    if entry.get("project_name") and entry["project_name"] != entry["komodo_stack"]:
        config["project_name"] = entry["project_name"]
    if builds_image(entry):
        config["run_build"] = True   # without it a redeploy would not rebuild the image from the changed sources
        config["auto_pull"] = False  # `compose pull` would look for the locally built image in a registry and fail
    return config


def create(entry: dict, config: dict) -> list:
    """Create the stack; returns the files Komodo cannot see yet (not pulled)."""
    k.write("CreateStack", {"name": entry["komodo_stack"], "config": config})
    k.write("RefreshStackCache", {"stack": entry["komodo_stack"]})
    return k.read("GetStack", {"stack": entry["komodo_stack"]})["info"].get("missing_files") or []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("stack_id")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--repos-dir", help="<periphery-root>/data/komodo/repos, if no stack exists on that server yet")
    args = ap.parse_args()

    inv = {s["id"]: s for s in yaml.safe_load((ROOT / "inventory/stacks.yaml").read_text()).get("stacks") or []}
    entry = inv.get(args.stack_id) or sys.exit(f"{args.stack_id} is not in inventory/stacks.yaml")
    config = stack_config(entry, args.repos_dir)
    print(f"stack {entry['komodo_stack']} on {entry['server']}:")
    for key, value in config.items():
        print(f"  {key} = {value}")
    if not args.apply:
        print("dry run; add --apply to create")
        return 0
    missing = create(entry, config)
    if missing:
        print(f"WARNING: Komodo cannot see {missing} yet; push the commit and wait for update-all to pull")
    print("created; deploy it in Komodo or wait for the next update-all run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
