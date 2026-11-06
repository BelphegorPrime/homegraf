#!/usr/bin/env python3
"""Machine identity `stack-provisioner`: lets infra/komodo/reconcile_stacks.py create the secrets of a new stack.

  INFISICAL_URL=... INFISICAL_BOOTSTRAP_CLIENT_ID=... INFISICAL_BOOTSTRAP_CLIENT_SECRET=... \\
    python3 infra/infisical/provisioner.py            # dry run
    python3 infra/infisical/provisioner.py --apply    # create what is missing, write infra/secrets/provisioner.env

Needs the org-admin identity of the other Infisical tools (Universal Auth). Safe to repeat: it adds the identity to
every project stacks-<server> that exists, so run it again after stack_secrets.py created the project of a new server.

The identity has org role no-access, Universal Auth, and the project role `member` in the stacks-* projects. That is
more than it needs (it only creates): Infisical Free has no custom roles, and `member` is the narrowest built-in role
that can create folders and secrets. It can read and change the secrets of those projects, like the Komodo variables
that hold the per-server viewer identities. It has no access to the Certificate Manager project or any other.

INFISICAL_URL in the file is the issuer URL of inventory/ingress.yaml (the direct address, not the NPM host), so the
reconcile step inside ingress-apply does not depend on the proxy it is about to change.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import sys

import yaml

HERE = pathlib.Path(__file__).resolve().parent
CODE = HERE.parents[1]
ROOT = pathlib.Path(os.environ.get("HOMELAB_ROOT") or CODE)
sys.path.insert(0, str(HERE))
from bootstrap import Api  # noqa: E402

NAME = "stack-provisioner"
ROLE = "member"
FILE = "infra/secrets/provisioner.env"


def _query(**params) -> str:
    import urllib.parse
    return "?" + urllib.parse.urlencode(params)


def direct_url() -> str:
    ingress = yaml.safe_load((ROOT / "inventory/ingress.yaml").read_text()) or {}
    issuers = ingress.get("issuers") or {}
    return next((i["api_url"] for i in issuers.values() if i.get("api_url")), os.environ["INFISICAL_URL"]).rstrip("/")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)
    say = (lambda m: print(m)) if args.apply else (lambda m: print("[dry run] " + m))
    api = Api()
    path = ROOT / FILE

    identities = {m["identity"]["name"]: m["identity"]["id"]
                  for m in api.call("GET", "/api/v1/identities" + _query(orgId=api.org_id))["identities"]}
    iid = identities.get(NAME)
    if iid is None:
        say(f"create machine identity {NAME} (org role no-access)")
        if args.apply:
            iid = api.call("POST", "/api/v1/identities", {"name": NAME, "organizationId": api.org_id, "role": "no-access"})["identity"]["id"]
    else:
        print(f"identity {NAME} exists ({iid})")

    if iid and args.apply:
        ua_path = f"/api/v1/auth/universal-auth/identities/{iid}"
        try:
            ua = api.call("GET", ua_path)["identityUniversalAuth"]
        except SystemExit:
            ua = api.call("POST", ua_path, {"accessTokenTTL": 600, "accessTokenMaxTTL": 600})["identityUniversalAuth"]
        stored = dict(line.removeprefix("export ").split("=", 1) for line in path.read_text().splitlines()
                      if line.startswith("export ")) if path.exists() else {}
        ok = False
        if stored.get("INFISICAL_PROVISIONER_CLIENT_ID") == ua["clientId"]:
            try:
                api.call("POST", "/api/v1/auth/universal-auth/login", {
                    "clientId": ua["clientId"], "clientSecret": stored["INFISICAL_PROVISIONER_CLIENT_SECRET"]}, auth=False)
                ok = True
            except (SystemExit, KeyError):
                ok = False
        if ok:
            print(f"{FILE} holds valid credentials")
        else:
            for old in api.call("GET", f"{ua_path}/client-secrets").get("clientSecretData", []):
                if not old.get("isClientSecretRevoked"):
                    api.call("POST", f"{ua_path}/client-secrets/{old['id']}/revoke")
            secret = api.call("POST", f"{ua_path}/client-secrets", {"description": "reconcile_stacks.py"})["clientSecret"]
            path.write_text(  # at once: Infisical shows the secret only once
                f'# Infisical machine identity "{NAME}" (member of the stacks-* projects), for infra/komodo/reconcile_stacks.py.\n'
                "# Created by infra/infisical/provisioner.py; kept in the repo by decision of the operator.\n"
                f"export INFISICAL_URL={direct_url()}\n"
                f"export INFISICAL_PROVISIONER_CLIENT_ID={ua['clientId']}\n"
                f"export INFISICAL_PROVISIONER_CLIENT_SECRET={secret}\n")
            path.chmod(0o600)
            print(f"credentials written to {FILE}")

    for p in api.call("GET", "/api/v1/projects" + _query(type="secret-manager"))["projects"]:
        if not p["slug"].startswith("stacks-"):
            continue
        members = api.call("GET", f"/api/v1/projects/{p['id']}/identity-memberships").get("identityMemberships", [])
        if iid and any((m.get("identity") or {}).get("id") == iid for m in members):
            print(f"{p['slug']}: already a member")
            continue
        say(f"{p['slug']}: add {NAME} as {ROLE}")
        if args.apply and iid:
            api.call("POST", f"/api/v1/projects/{p['id']}/identity-memberships/{iid}", {"role": ROLE})
    return 0


if __name__ == "__main__":
    sys.exit(main())
