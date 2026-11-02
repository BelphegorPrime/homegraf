#!/usr/bin/env python3
"""Show, create, change and delete the Infisical secrets of a stack (plan 05).
Interactive, with menus: ./homelab.py; the same subcommands: ./homelab.py secrets ...

  python3 infra/infisical/secret.py list   <stack>                       # e.g. list paperless
  python3 infra/infisical/secret.py get    <stack> <KEY>                 # e.g. get paperless POSTGRES_PASSWORD
  python3 infra/infisical/secret.py set    <stack> <KEY>                 # asks for the value, hidden, twice
  printf %s "$VALUE" | python3 infra/infisical/secret.py set <stack> <KEY>   # value from stdin
  python3 infra/infisical/secret.py set    <stack> <KEY> --generate [BYTES] [--show]   # random hex value
  python3 infra/infisical/secret.py delete <stack> <KEY> [--yes] [--force]
  ... set/delete --deploy                                                # then DeployStack in Komodo

Access: org-admin identity (Universal Auth) from INFISICAL_URL, INFISICAL_BOOTSTRAP_CLIENT_ID,
INFISICAL_BOOTSTRAP_CLIENT_SECRET; if they are missing, the script asks in the terminal. Deploys use
infra/komodo/komodo.env. <stack> is the ID in inventory/stacks.yaml (with `secrets: infisical`); it maps to project
stacks-<server>, environment prod, folder /<stack>. Never pass values as arguments (shell history, process list).
A changed value takes effect on the next deploy; Komodo does not redeploy when a secret changes.
"""
from __future__ import annotations

import argparse
import getpass
import os
import pathlib
import re
import secrets
import sys
import urllib.parse

import yaml

HERE = pathlib.Path(__file__).resolve().parent
CODE = HERE.parents[1]                                          # where the tools are
ROOT = pathlib.Path(os.environ.get("HOMELAB_ROOT") or CODE)   # the repository they work on
sys.path.insert(0, str(HERE))
import stack_secrets as ss  # noqa: E402

sys.path.insert(0, str(CODE / "infra/homelab"))
import ui  # noqa: E402

KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
UNREFERENCED = "   (not used in any compose file)"


def default_url() -> str:
    import settings
    return settings.infisical_url(ROOT) or ""


def secret_stacks() -> list[dict]:
    return ss.secret_stacks(yaml.safe_load((ROOT / "inventory/stacks.yaml").read_text()).get("stacks") or [])


def find_stack(stack_id: str) -> dict:
    stacks = {s["id"]: s for s in yaml.safe_load((ROOT / "inventory/stacks.yaml").read_text()).get("stacks") or []}
    s = stacks.get(stack_id)
    if s is None:
        raise SystemExit(f"stack {stack_id!r} is not in inventory/stacks.yaml")
    if s.get("secrets") != "infisical":
        raise SystemExit(f"stack {stack_id} has no `secrets: infisical`; set it up first (docs/plans/05-infisical-stack-secrets.md)")
    return s


def check_key(key: str) -> str:
    if not KEY_RE.fullmatch(key):
        raise SystemExit(f"invalid key {key!r}: letters, digits and _ only, like an environment variable (e.g. DB_PASSWORD)")
    return key


def compose_refs(stack: dict, key: str) -> bool:
    """Does a compose file of the stack use ${KEY}?"""
    pat = re.compile(rf"\$\{{{re.escape(key)}(?=[}}:?-])|\${re.escape(key)}(?![A-Za-z0-9_])")
    return any(pat.search((ROOT / stack["path"] / f).read_text()) for f in stack["compose_files"]
               if (ROOT / stack["path"] / f).is_file())


class Folder:
    """Folder /<stack> in project stacks-<server>, environment prod."""

    def __init__(self, inf, stack: dict, must_exist: bool = True):
        self.inf, self.path = inf, "/" + stack["id"]
        slug = ss.project_slug(stack["server"])
        self.where = f"{slug}/{ss.ENV}{self.path}"
        self.project = next((p["id"] for p in inf.call("GET", "/api/v1/projects?type=secret-manager")["projects"]
                             if p["slug"] == slug), None)
        folders = {f["name"] for f in inf.call("GET", "/api/v2/folders?" + urllib.parse.urlencode(
            {"projectId": self.project, "environment": ss.ENV, "path": "/"}))["folders"]} if self.project else set()
        self.exists = stack["id"] in folders
        if must_exist and not self.exists:
            raise SystemExit(f"folder {self.where} does not exist: python3 infra/infisical/stack_secrets.py --apply")

    def _q(self, **extra) -> str:
        return "?" + urllib.parse.urlencode({"projectId": self.project, "environment": ss.ENV, "secretPath": self.path} | extra)

    def _body(self, **extra) -> dict:
        return {"projectId": self.project, "environment": ss.ENV, "secretPath": self.path} | extra

    def keys(self) -> list[str]:
        return sorted(s["secretKey"] for s in self.inf.call("GET", "/api/v4/secrets" + self._q(viewSecretValue="false"))["secrets"])

    def get(self, key: str) -> str:
        return self.inf.call("GET", f"/api/v4/secrets/{key}" + self._q(expandSecretReferences="false"))["secret"]["secretValue"]

    def set(self, key: str, value: str) -> str:
        if value == "":
            raise SystemExit("empty value; delete the key instead")
        if key in self.keys():
            self.inf.call("PATCH", f"/api/v4/secrets/{key}", self._body(secretValue=value))
            return "changed"
        self.inf.call("POST", f"/api/v4/secrets/{key}", self._body(secretValue=value))
        return "created"

    def delete(self, key: str) -> None:
        self.inf.call("DELETE", f"/api/v4/secrets/{key}", self._body())

    def drop(self) -> None:
        """Delete the whole folder with all its secrets (Infisical refuses a non-empty folder without forceDelete)."""
        self.inf.call("DELETE", f"/api/v2/folders/{self.path.lstrip('/')}",
                      {"projectId": self.project, "environment": ss.ENV, "path": "/", "forceDelete": True})
        self.exists = False


def deploy(stack: dict) -> None:
    sys.path.insert(0, str(CODE / "infra/komodo"))
    import komodo_api  # noqa: E402
    komodo_api.load_env()
    print(f"Deploying {stack['komodo_stack']} ...")
    update = komodo_api.execute_and_poll("DeployStack", {"stack": stack["komodo_stack"]}, timeout=900)
    if not update.get("success"):
        raise SystemExit(f"deploy of {stack['komodo_stack']} failed, see update {update.get('id')} in Komodo")
    print("Deploy succeeded")


# --- subcommands --------------------------------------------------------------------------------------------

def run(args, inf) -> int:
    stack = find_stack(args.stack)
    if getattr(args, "key", None):
        check_key(args.key)
    folder = Folder(inf, stack)

    if args.cmd == "list":
        if args.json:
            import json
            print(json.dumps([{"key": key, "used": compose_refs(stack, key)} for key in folder.keys()]))
            return 0
        for key in folder.keys():
            print(key + ("" if compose_refs(stack, key) else UNREFERENCED))
        return 0
    if args.cmd == "get":
        if args.key not in folder.keys():
            raise SystemExit(f"{args.key} does not exist in {folder.where}")
        print(folder.get(args.key))
        return 0
    if args.cmd == "set":
        if args.generate is not None:
            value = secrets.token_hex(args.generate)
        elif not sys.stdin.isatty():
            value = sys.stdin.read()
            value = value[:-1] if value.endswith("\n") else value
        else:
            value = ui.hidden(f"Value for {args.key}")
        print(f"{folder.where}/{args.key} {folder.set(args.key, value)}")
        if args.generate is not None and args.show:
            print(value)
        if not compose_refs(stack, args.key):
            print(f"Note: no compose file of {stack['id']} uses ${{{args.key}}}")
    elif args.cmd == "delete":
        if args.key not in folder.keys():
            raise SystemExit(f"{args.key} does not exist in {folder.where}")
        if compose_refs(stack, args.key) and not args.force:
            raise SystemExit(f"{stack['id']} still uses ${{{args.key}}}; change the compose file first, or --force")
        if not args.yes and not ui.yes(f"Delete {folder.where}/{args.key}?"):
            print("cancelled")
            return 1
        folder.delete(args.key)
        print(f"{folder.where}/{args.key} deleted")
    if args.deploy:
        deploy(stack)
    else:
        print(f"Takes effect on the next deploy of {stack['komodo_stack']} (or use --deploy).")
    return 0


def parse_args(argv=None, prog: str | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog=prog, description=__doc__.split("\n\n")[0], epilog="Interactive: ./homelab.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list", help="keys of a stack (no values)")
    p.add_argument("stack")
    p.add_argument("--json", action="store_true", help="machine-readable: key and whether a compose file uses it")
    p = sub.add_parser("get", help="print a value")
    p.add_argument("stack")
    p.add_argument("key")
    p = sub.add_parser("set", help="create or change; value from stdin, hidden prompt or --generate")
    p.add_argument("stack")
    p.add_argument("key")
    p.add_argument("--generate", nargs="?", type=int, const=32, metavar="BYTES",
                   help="random value of BYTES bytes as hex (default 32)")
    p.add_argument("--show", action="store_true", help="with --generate: print the generated value")
    p.add_argument("--deploy", action="store_true", help="deploy the Komodo stack afterwards")
    p = sub.add_parser("delete", help="delete a key")
    p.add_argument("stack")
    p.add_argument("key")
    p.add_argument("--yes", action="store_true", help="do not ask")
    p.add_argument("--force", action="store_true", help="even if a compose file still uses ${KEY}")
    p.add_argument("--deploy", action="store_true", help="deploy the Komodo stack afterwards")
    return ap.parse_args(argv)


def credentials() -> None:
    """Infisical access from the environment, otherwise asked in the terminal."""
    need = ("INFISICAL_URL", "INFISICAL_BOOTSTRAP_CLIENT_ID", "INFISICAL_BOOTSTRAP_CLIENT_SECRET")
    if all(os.environ.get(k) for k in need):
        return
    if not sys.stdin.isatty():
        raise SystemExit(f"set {', '.join(need)}")
    print("\nInfisical login (org-admin machine identity, Universal Auth; Infisical -> Organization -> Access Control -> Identities)")
    if not os.environ.get("INFISICAL_URL"):
        default = default_url()
        os.environ["INFISICAL_URL"] = input(f"Infisical URL{f' [{default}]' if default else ''}: ").strip() or default
    if not os.environ.get("INFISICAL_BOOTSTRAP_CLIENT_ID"):
        os.environ["INFISICAL_BOOTSTRAP_CLIENT_ID"] = input("Client ID (a UUID, e.g. 1a2b3c4d-0000-4000-8000-000000000000): ").strip()
    if not os.environ.get("INFISICAL_BOOTSTRAP_CLIENT_SECRET"):
        os.environ["INFISICAL_BOOTSTRAP_CLIENT_SECRET"] = getpass.getpass("Client secret (hidden): ")


def main(argv=None, prog: str | None = None) -> int:
    args = parse_args(argv, prog)
    credentials()
    from bootstrap import Api  # noqa: E402
    inf = Api()
    try:
        return run(args, inf)
    except (KeyboardInterrupt, EOFError):
        print()
        return 130


if __name__ == "__main__":
    sys.exit(main())
