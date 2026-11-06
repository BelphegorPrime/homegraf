#!/usr/bin/env python3
"""Set up Infisical stack secrets for Komodo stacks (plan 05).

  . infra/komodo/komodo.env
  # org-admin identity (Universal Auth) of the operator, as for bootstrap.py:
  export INFISICAL_URL=http://10.0.0.45:8079 INFISICAL_BOOTSTRAP_CLIENT_ID=... INFISICAL_BOOTSTRAP_CLIENT_SECRET=...
  python3 infra/infisical/stack_secrets.py                                        # dry run
  python3 infra/infisical/stack_secrets.py --apply                                # create / reconcile
  python3 infra/infisical/stack_secrets.py --apply --import searxng=stacks/searxng/.env   # take values over once

Per server that has stacks with `secrets: infisical` in inventory/stacks.yaml:
- project `stacks-<server>` with one folder /<stack-id> per stack in environment prod
- machine identity `stacks-<server>` (org role no-access, Universal Auth, token 10 min), project role viewer.
  One project per server instead of per-folder rights: custom roles and folder RBAC are paid features in Infisical
  (rbac, secretsFolderRbac), and additional privileges per identity only work in projects from before v0.167.
- Komodo variables INFISICAL_PROJECT_ID_<SERVER> and (secret) INFISICAL_CLIENT_ID_<SERVER>/INFISICAL_CLIENT_SECRET_<SERVER>,
  plus INFISICAL_API_URL
- compose_cmd_wrapper of the Komodo stack: infra/infisical/compose-wrap.sh from the server's clone
--import only creates missing keys; existing values in Infisical are never overwritten.
Endpoints and schemas: backend/src/{server,ee}/routes at tag v0.167.2.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import re
import sys
import urllib.parse

import yaml

HERE = pathlib.Path(__file__).resolve().parent
CODE = HERE.parents[1]                                          # where the tools are
ROOT = pathlib.Path(os.environ.get("HOMELAB_ROOT") or CODE)   # the repository they work on
ENV = "prod"
ROLE = "viewer"  # built-in project role: read secrets, change nothing
PROVISIONER = "stack-provisioner"   # made by infra/infisical/provisioner.py or by the bootstrap; member of every stacks-* project
PROVISIONER_ROLE = "member"         # the narrowest built-in role that can create secrets
TOKEN_TTL = 600  # seconds; the wrapper needs the token for one compose call only
WRAP_INCLUDE = ["config", "build", "pull", "up", "run"]  # every compose call that resolves ${VAR}


def secret_stacks(stacks: list[dict]) -> list[dict]:
    return [s for s in stacks if s.get("secrets") == "infisical"]


def var_suffix(server: str) -> str:
    return re.sub(r"[^A-Z0-9]", "_", server.upper())


def project_slug(server: str) -> str:
    """Infisical slugs are lower case letters, digits and hyphens; a server name (Komodo's default one is `Local`) may not be."""
    return "stacks-" + re.sub(r"[^a-z0-9-]+", "-", server.lower()).strip("-")


def wrapper(stack: dict, run_directory: str) -> str:
    """compose_cmd_wrapper; Komodo fills in the [[VARIABLES]] and replaces [[COMPOSE_COMMAND]]."""
    path, run_dir = stack["path"].rstrip("/"), run_directory.rstrip("/")
    if not run_dir.endswith("/" + path):
        raise SystemExit(f"{stack['id']}: run_directory {run_directory} does not end with {path}")
    clone = run_dir[: -len(path) - 1]
    sfx = var_suffix(stack["server"])
    return (f"INFISICAL_API_URL='[[INFISICAL_API_URL]]' INFISICAL_PROJECT_ID='[[INFISICAL_PROJECT_ID_{sfx}]]' "
            f"INFISICAL_UNIVERSAL_AUTH_CLIENT_ID='[[INFISICAL_CLIENT_ID_{sfx}]]' "
            f"INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET='[[INFISICAL_CLIENT_SECRET_{sfx}]]' "
            f"sh {clone}/infra/infisical/compose-wrap.sh /{stack['id']} -- [[COMPOSE_COMMAND]]")


def parse_env_file(text: str) -> dict[str, str]:
    """KEY=VALUE as in compose .env files; empty values are skipped."""
    out = {}
    for line in text.splitlines():
        m = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$", line)
        if not m:
            continue
        key, value = m.group(1), m.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0]
        if value:
            out[key] = value
    return out


def _q(**params) -> str:
    return "?" + urllib.parse.urlencode(params)


def _login_ok(inf, client_id: str, client_secret: str) -> bool:
    try:
        inf.call("POST", "/api/v1/auth/universal-auth/login",
                 {"clientId": client_id, "clientSecret": client_secret}, auth=False)
        return True
    except SystemExit:
        return False


def sync(inf, komodo, stacks: list[dict], api_url: str, imports: dict[str, dict[str, str]], apply: bool) -> int:
    say = (lambda m: print(m)) if apply else (lambda m: print("[dry run] " + m))
    by_server: dict[str, list[str]] = {}
    for s in stacks:
        by_server.setdefault(s["server"], []).append(s["id"])
    unknown = set(imports) - {s["id"] for s in stacks}
    if unknown:
        raise SystemExit(f"--import for stacks without `secrets: infisical`: {sorted(unknown)}")

    kvars = {v["name"]: v for v in komodo.read("ListVariables")}
    if any(v["is_secret"] and v["value"] and set(v["value"]) == {"#"} for v in kvars.values()):
        raise SystemExit("Komodo returns secret variables masked: the API key needs an admin user")

    def set_var(name: str, value: str, secret: bool, description: str) -> None:
        cur = kvars.get(name)
        if cur is None:
            say(f"create Komodo variable {name}")
            if apply:
                komodo.write("CreateVariable", {"name": name, "value": value, "description": description, "is_secret": secret})
        elif cur["value"] != value:
            say(f"update Komodo variable {name}")
            if apply:
                komodo.write("UpdateVariableValue", {"name": name, "value": value})
        kvars[name] = {"name": name, "value": value, "is_secret": secret}

    set_var("INFISICAL_API_URL", api_url, False, "Infisical for compose-wrap.sh (infra/infisical/stack_secrets.py)")

    projects = {p["slug"]: p["id"] for p in inf.call("GET", "/api/v1/projects" + _q(type="secret-manager"))["projects"]}
    identities = {m["identity"]["name"]: m["identity"]["id"]
                  for m in inf.call("GET", "/api/v1/identities" + _q(orgId=inf.org_id))["identities"]}
    for server, ids in sorted(by_server.items()):
        slug, sfx = project_slug(server), var_suffix(server)

        pid = projects.get(slug)
        if pid is None:
            say(f"create project {slug} (environments dev/staging/prod)")
            if apply:
                pid = inf.call("POST", "/api/v1/projects",
                               {"projectName": slug, "slug": slug, "type": "secret-manager"})["project"]["id"]
        else:
            print(f"project {slug} exists ({pid})")
        if pid:
            set_var(f"INFISICAL_PROJECT_ID_{sfx}", pid, False, f"Infisical-Projekt {slug}")

        folders = set()
        if pid:
            folders = {f["name"] for f in inf.call("GET", "/api/v2/folders" + _q(projectId=pid, environment=ENV, path="/"))["folders"]}
        for sid in ids:
            if sid not in folders:
                say(f"create folder {slug}/{ENV}/{sid}")
                if apply:
                    inf.call("POST", "/api/v2/folders", {"projectId": pid, "environment": ENV, "name": sid, "path": "/"})
            values = imports.get(sid, {})
            have = set()
            if values and pid and sid in folders:
                have = {x["secretKey"] for x in inf.call("GET", "/api/v4/secrets" + _q(
                    projectId=pid, environment=ENV, secretPath="/" + sid, viewSecretValue="false"))["secrets"]}
            for key in sorted(set(values) - have):
                say(f"create secret {slug}/{ENV}/{sid}/{key}")
                if apply:
                    inf.call("POST", f"/api/v4/secrets/{urllib.parse.quote(key)}",
                             {"projectId": pid, "environment": ENV, "secretPath": "/" + sid, "secretValue": values[key]})
            for key in sorted(set(values) & have):
                print(f"secret {slug}/{ENV}/{sid}/{key} exists, not overwritten")

        iid = identities.get(slug)
        if iid is None:
            say(f"create machine identity {slug} (org role no-access)")
            if not apply:
                continue
            iid = inf.call("POST", "/api/v1/identities",
                           {"name": slug, "organizationId": inf.org_id, "role": "no-access"})["identity"]["id"]
        ua_path = f"/api/v1/auth/universal-auth/identities/{iid}"
        try:
            ua = inf.call("GET", ua_path)["identityUniversalAuth"]
        except SystemExit:
            say(f"{slug}: set up Universal Auth")
            if not apply:
                continue
            ua = inf.call("POST", ua_path, {"accessTokenTTL": TOKEN_TTL, "accessTokenMaxTTL": TOKEN_TTL})["identityUniversalAuth"]
        cid_var, sec_var = f"INFISICAL_CLIENT_ID_{sfx}", f"INFISICAL_CLIENT_SECRET_{sfx}"
        stored = kvars.get(sec_var, {}).get("value")
        if kvars.get(cid_var, {}).get("value") == ua["clientId"] and stored and _login_ok(inf, ua["clientId"], stored):
            print(f"{slug}: credentials in Komodo are valid")
        else:
            say(f"{slug}: new client secret to Komodo ({cid_var}, {sec_var}), old ones revoked")
            if apply:
                for old in inf.call("GET", f"{ua_path}/client-secrets").get("clientSecretData", []):
                    if not old.get("isClientSecretRevoked"):
                        inf.call("POST", f"{ua_path}/client-secrets/{old['id']}/revoke")
                secret = inf.call("POST", f"{ua_path}/client-secrets",
                                  {"description": f"compose-wrap.sh on {server}"})["clientSecret"]
                set_var(cid_var, ua["clientId"], True, f"Infisical Identity {slug}")
                set_var(sec_var, secret, True, f"Infisical Identity {slug}")

        if not pid:
            continue
        member = next((m for m in inf.call("GET", f"/api/v1/projects/{pid}/identity-memberships").get("identityMemberships", [])
                       if (m.get("identity") or {}).get("id") == iid), None)
        if member is None:
            say(f"{slug}: project member with role {ROLE}")
            if apply:
                inf.call("POST", f"/api/v1/projects/{pid}/identity-memberships/{iid}", {"role": ROLE})
        elif [r.get("role") for r in member.get("roles") or []] != [ROLE]:
            say(f"{slug}: set project role to {ROLE}")
            if apply:
                inf.call("PATCH", f"/api/v1/projects/{pid}/identity-memberships/{iid}",
                         {"roles": [{"role": ROLE, "isTemporary": False}]})

        # the identity of the reconcile step of update-all (infra/komodo/reconcile_stacks.py) creates the secrets of new stacks
        prov = identities.get(PROVISIONER)
        if prov and not any((m.get("identity") or {}).get("id") == prov for m in
                            inf.call("GET", f"/api/v1/projects/{pid}/identity-memberships").get("identityMemberships", [])):
            say(f"{slug}: {PROVISIONER} as project member with role {PROVISIONER_ROLE}")
            if apply:
                inf.call("POST", f"/api/v1/projects/{pid}/identity-memberships/{prov}", {"role": PROVISIONER_ROLE})

    server_names = {s["id"]: s["name"] for s in komodo.read("ListServers")}
    for s in stacks:
        cfg = komodo.read("GetStack", {"stack": s["komodo_stack"]})["config"]
        if server_names.get(cfg["server_id"]) != s["server"]:
            raise SystemExit(f"{s['id']}: Komodo stack {s['komodo_stack']} runs on "
                             f"{server_names.get(cfg['server_id'])}, the inventory says {s['server']}")
        want = {"compose_cmd_wrapper": wrapper(s, cfg["run_directory"]), "compose_cmd_wrapper_include": WRAP_INCLUDE}
        if all(cfg.get(k) == v for k, v in want.items()):
            print(f"stack {s['komodo_stack']}: wrapper up to date")
            continue
        say(f"stack {s['komodo_stack']}: set compose_cmd_wrapper")
        if apply:
            komodo.write("UpdateStack", {"id": s["komodo_stack"], "config": want})
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--import", dest="imports", action="append", default=[], metavar="STACK=ENVDATEI",
                    help="take keys from a .env file into the stack's folder (repeatable)")
    args = ap.parse_args()
    imports = {}
    for item in args.imports:
        sid, _, path = item.partition("=")
        if not path:
            raise SystemExit(f"--import expects STACK=ENVFILE, not {item!r}")
        imports[sid] = parse_env_file((ROOT / path).read_text())

    stacks = secret_stacks(yaml.safe_load((ROOT / "inventory/stacks.yaml").read_text()).get("stacks") or [])
    if not stacks:
        print("no stack with `secrets: infisical` in inventory/stacks.yaml")
        return 0
    sys.path.insert(0, str(HERE))
    sys.path.insert(0, str(CODE / "infra/komodo"))
    import komodo_api  # noqa: E402
    from bootstrap import Api  # noqa: E402
    return sync(Api(), komodo_api, stacks, os.environ["INFISICAL_URL"].rstrip("/"), imports, args.apply)


if __name__ == "__main__":
    sys.exit(main())
