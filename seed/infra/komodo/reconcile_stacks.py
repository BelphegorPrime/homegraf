#!/usr/bin/env python3
"""Set up every stack that exists only as a folder: stacks/<id>/ with a compose file and an ingress.yaml.

A push of such a folder is all it takes. Per stack, in this order, and only what is still missing:

  1. its ${VAR} secrets, generated, in Infisical (project stacks-<server>, folder /<id>); existing values stay
  2. the Komodo stack on the server of the manifest's upstream (files_on_host in the server's clone)
  3. the Infisical wrapper of that stack (compose_cmd_wrapper), so a deploy resolves ${VAR}
  4. a deploy, when the stack or a secret was just created, and on every run when a compose service has `build:`
     (the build cache makes that quick; it takes the place of stage Build of update-all)

Runs as the first stage of the Komodo procedure ingress-apply (RunStackService ingressctl, after the repos were
pulled), so the files are in the clone. It changes nothing for stacks that have an entry in inventory/stacks.yaml.

  python3 infra/komodo/reconcile_stacks.py            # dry run: print what would happen
  python3 infra/komodo/reconcile_stacks.py --apply

Infisical access: INFISICAL_URL, INFISICAL_PROVISIONER_CLIENT_ID, INFISICAL_PROVISIONER_CLIENT_SECRET (machine
identity `stack-provisioner`, Universal Auth, member of the stacks-<server> projects; see infra/infisical/provisioner.py).
Komodo access: infra/komodo/komodo.env. Exit code 1 if a stack could not be set up (the others are still done).
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
CODE = HERE.parents[1]                                          # where the tools are
ROOT = pathlib.Path(os.environ.get("HOMELAB_ROOT") or CODE)   # the repository they work on
for sub in ("infra/komodo", "infra/infisical", "infra/homelab", "infra/ingress-controller/src"):
    sys.path.insert(0, str(CODE / sub))

SECRET_BYTES = 24   # token_hex(24): 48 characters

# ${VAR} and ${VAR:?msg} / ${VAR?msg} need a value; ${VAR:-x}, ${VAR-x}, ${VAR:+x} and $$ do not
_REQUIRED = re.compile(r"(?<!\$)\$\{([A-Za-z_][A-Za-z0-9_]*)(?:\}|:?\?)")


def required_vars(root: pathlib.Path, entry: dict) -> list[str]:
    """The ${VAR}s of the stack's compose files that have no default, in order of appearance."""
    found: list[str] = []
    for name in entry["compose_files"]:
        path = root / entry["path"] / name
        if path.is_file():
            for v in _REQUIRED.findall(path.read_text()):
                if v not in found:
                    found.append(v)
    return found


class Infisical:
    """What the provisioner needs from Infisical: find a stack's folder, create it, create missing secrets."""

    def __init__(self, url: str, client_id: str, client_secret: str):
        self.base = url.rstrip("/")
        self.token = self.call("POST", "/api/v1/auth/universal-auth/login",
                               {"clientId": client_id, "clientSecret": client_secret}, auth=False)["accessToken"]

    def call(self, method: str, path: str, body=None, auth: bool = True):
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(self.base + path, method=method, headers=headers,
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Infisical {method} {path}: HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}") from None

    def project(self, slug: str) -> str | None:
        projects = self.call("GET", "/api/v1/projects?type=secret-manager")["projects"]
        return next((p["id"] for p in projects if p["slug"] == slug), None)

    def has_folder(self, project: str, env: str, name: str) -> bool:
        q = urllib.parse.urlencode({"projectId": project, "environment": env, "path": "/"})
        return name in {f["name"] for f in self.call("GET", "/api/v2/folders?" + q)["folders"]}

    def make_folder(self, project: str, env: str, name: str) -> None:
        self.call("POST", "/api/v2/folders", {"projectId": project, "environment": env, "name": name, "path": "/"})

    def keys(self, project: str, env: str, path: str) -> set[str]:
        q = urllib.parse.urlencode({"projectId": project, "environment": env, "secretPath": path, "viewSecretValue": "false"})
        return {s["secretKey"] for s in self.call("GET", "/api/v4/secrets?" + q)["secrets"]}

    def make_secret(self, project: str, env: str, path: str, key: str, value: str) -> None:
        self.call("POST", f"/api/v4/secrets/{urllib.parse.quote(key)}",
                  {"projectId": project, "environment": env, "secretPath": path, "secretValue": value})


def provision_secrets(inf, entry: dict, keys: list[str], apply: bool, say) -> bool:
    """Create the missing secrets of a stack; True if any was (or, in a dry run, would be) created."""
    import stack_secrets as ss
    slug = ss.project_slug(entry["server"])
    project = inf.project(slug)
    if project is None:
        raise RuntimeError(f"Infisical project {slug} does not exist or the provisioner is not a member; "
                           f"create it once with infra/infisical/stack_secrets.py --apply")
    have: set[str] = set()
    if inf.has_folder(project, ss.ENV, entry["id"]):
        have = inf.keys(project, ss.ENV, "/" + entry["id"])
    else:
        say(f"create folder {slug}/{ss.ENV}/{entry['id']}")
        if apply:
            inf.make_folder(project, ss.ENV, entry["id"])
    missing = [k for k in keys if k not in have]
    for key in missing:
        say(f"generate secret {slug}/{ss.ENV}/{entry['id']}/{key}")
        if apply:
            inf.make_secret(project, ss.ENV, "/" + entry["id"], key, secrets.token_hex(SECRET_BYTES))
    return bool(missing)


def setup_stack(entry: dict, komodo, inf, create, apply: bool, root: pathlib.Path = ROOT, say=print) -> str:
    """Everything one stack still needs; returns what was done, '' if nothing."""
    import stack_secrets as ss
    done = []
    keys = required_vars(root, entry)
    new_secrets = provision_secrets(inf, entry, keys, apply, say) if keys else False
    if new_secrets:
        done.append("secrets")

    exists = any(s["name"] == entry["komodo_stack"] for s in komodo.read("ListStacks"))
    config = None if exists else create.stack_config(entry)
    if not exists:
        say(f"create Komodo stack {entry['komodo_stack']} on {entry['server']} ({config['run_directory']})")
        if apply:
            missing = create.create(entry, config)
            if missing:
                say(f"WARNING: Komodo cannot see {missing} yet (the repo was not pulled?)")
        done.append("stack")

    if keys:
        live = komodo.read("GetStack", {"stack": entry["komodo_stack"]})["config"] if exists or apply else {}
        wanted = ss.wrapper(entry, live.get("run_directory") or config["run_directory"])
        if live.get("compose_cmd_wrapper") != wanted:
            sfx = ss.var_suffix(entry["server"])
            variables = {v["name"] for v in komodo.read("ListVariables")}
            lacking = [n for n in ("INFISICAL_API_URL", f"INFISICAL_PROJECT_ID_{sfx}", f"INFISICAL_CLIENT_ID_{sfx}",
                                   f"INFISICAL_CLIENT_SECRET_{sfx}") if n not in variables]
            if lacking:
                raise RuntimeError(f"Komodo variables {lacking} are missing: run infra/infisical/stack_secrets.py --apply once for {entry['server']}")
            say(f"set the Infisical wrapper of {entry['komodo_stack']}")
            if apply:
                komodo.write("UpdateStack", {"id": entry["komodo_stack"], "config": {
                    "compose_cmd_wrapper": wanted, "compose_cmd_wrapper_include": ss.WRAP_INCLUDE}})
            done.append("wrapper")

    if create.builds_image(entry, root) and (exists or apply):
        live = komodo.read("GetStack", {"stack": entry["komodo_stack"]})["config"]
        if live.get("auto_pull", True):   # a stack made by an older version: `compose pull` fails for the image built here
            say(f"turn off auto_pull of {entry['komodo_stack']} (it builds its own image)")
            if apply:
                komodo.write("UpdateStack", {"id": entry["komodo_stack"], "config": {"auto_pull": False}})
            done.append("auto_pull")

    # A stack that builds its own image is deployed on every run: `up --build` is quick when nothing changed (build cache,
    # no container is recreated), and it replaces stage Build of update-all, which cannot be edited from inside that
    # procedure ("Procedure busy").
    rebuilds = create.builds_image(entry, root)
    if "stack" in done or "secrets" in done or rebuilds:
        say(f"deploy {entry['komodo_stack']}" + ("" if "stack" in done or "secrets" in done else " (builds its own image)"))
    if apply and ("stack" in done or "secrets" in done or rebuilds):
        update = komodo.execute_and_poll("DeployStack", {"stack": entry["komodo_stack"]}, timeout=1800)
        if not update.get("success"):
            raise RuntimeError(f"deploy of {entry['komodo_stack']} failed, see update {update.get('id')} in Komodo")
        done.append("deploy")
    return ", ".join(done)


def derived_stacks(root: pathlib.Path) -> list[dict]:
    from ingressctl.inventory import load_inventory
    return [s for s in load_inventory(root).stacks.values() if s.get("status") == "derived"]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", "--yes", dest="apply", action="store_true",
                    help="do it; without it only print what would happen")
    ap.add_argument("--stack", action="append", help="only this stack (repeatable)")
    args = ap.parse_args(argv)

    stacks = [s for s in derived_stacks(ROOT) if not args.stack or s["id"] in args.stack]
    if not stacks:
        print("no stack that exists only as a folder")
        return 0
    import komodo_api as komodo
    import create_stack as create
    komodo.load_env()
    # the provisioner identity (inside ingress-apply), or the org-admin identity the other tools use (by hand)
    for prefix in ("INFISICAL_PROVISIONER", "INFISICAL_BOOTSTRAP"):
        if all(os.environ.get(n) for n in ("INFISICAL_URL", f"{prefix}_CLIENT_ID", f"{prefix}_CLIENT_SECRET")):
            break
    else:
        print("set INFISICAL_URL and INFISICAL_PROVISIONER_CLIENT_ID/_SECRET (or INFISICAL_BOOTSTRAP_CLIENT_ID/_SECRET)", file=sys.stderr)
        return 1
    inf = Infisical(os.environ["INFISICAL_URL"], os.environ[f"{prefix}_CLIENT_ID"], os.environ[f"{prefix}_CLIENT_SECRET"])
    failed = 0
    for entry in stacks:
        prefix = "" if args.apply else "[dry run] "
        print(f"== {entry['id']} on {entry['server']}")
        try:
            done = setup_stack(entry, komodo, inf, create, args.apply, ROOT, lambda m: print(prefix + m))
            print(f"   {done or 'nothing to do'}")
        except (RuntimeError, SystemExit) as e:
            failed += 1
            print(f"   FAILED: {e}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
