#!/usr/bin/env python3
"""Idempotent CA bootstrap for ingressctl in Infisical v0.167.x (Certificate Manager).

  # the operator's org-admin identity (Universal Auth); claude-bootstrap was deleted after the bootstrap:
  export INFISICAL_URL=http://10.0.0.45:8079 INFISICAL_BOOTSTRAP_CLIENT_ID=... INFISICAL_BOOTSTRAP_CLIENT_SECRET=...
  python infra/infisical/bootstrap.py            # dry run: shows what would be created
  python infra/infisical/bootstrap.py --apply    # create (existing objects with the same name are reused)

Creates (names are fixed so that repeated runs create nothing twice):
- policy  `ingressctl-local-codes`: only *.<domain> (inventory/homegraf.yaml), server_auth, EC P-256/P-384, max 90 days
- profile `ingressctl` on the existing root CA (free self-hosted allows exactly one CA)
- application `ingressctl` with the profile and API enrollment enabled
- machine identity `ingressctl` (org role no-access, Universal Auth, access token 1 h),
  member of the application with role `operator`; client secret -> infra/secrets/ingressctl.env
It also writes profile_id/application_id into inventory/ingress.yaml and the root certificate to
docs/homelab-root-ca.pem. Endpoints and schemas: backend/src/server/routes/v1 at tag v0.167.2.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import pathlib
import re
import sys
import urllib.error
import urllib.request

CODE = pathlib.Path(__file__).resolve().parents[2]                      # where the tools are
ROOT = pathlib.Path(os.environ.get("HOMELAB_ROOT") or CODE)             # the repository they work on
sys.path.insert(0, str(CODE / "infra/homelab"))
import settings  # noqa: E402

NAME = "ingressctl"
DOMAIN = settings.domain(ROOT)   # inventory/homegraf.yaml: the names the CA may issue for are *.<domain>
POLICY = {
    "name": "ingressctl-local-codes",   # the name stays: an installation that already has the policy reuses it
    "description": f"TLS server certificates for *.{DOMAIN} via ingressctl (NPM)",
    "subject": [{"type": "common_name", "allowed": [f"*.{DOMAIN}"]}],
    "sans": [{"type": "dns_name", "allowed": [f"*.{DOMAIN}"]}],
    "keyUsages": {"allowed": ["digital_signature", "key_encipherment"], "required": ["digital_signature"]},
    "extendedKeyUsages": {"allowed": ["server_auth"], "required": ["server_auth"]},
    "algorithms": {"signature": ["SHA256-ECDSA", "SHA384-ECDSA"], "keyAlgorithm": ["ECDSA-P256", "ECDSA-P384"]},
    "validity": {"max": "90d"},
}
PROFILE_DEFAULTS = {"keyAlgorithm": "EC_prime256v1", "signatureAlgorithm": "ECDSA-SHA256", "ttlDays": 90,
                    "keyUsages": ["digital_signature"], "extendedKeyUsages": ["server_auth"]}


class Api:
    def __init__(self):
        self.base = os.environ["INFISICAL_URL"].rstrip("/")
        body = self.call("POST", "/api/v1/auth/universal-auth/login", {
            "clientId": os.environ["INFISICAL_BOOTSTRAP_CLIENT_ID"],
            "clientSecret": os.environ["INFISICAL_BOOTSTRAP_CLIENT_SECRET"]}, auth=False)
        self.token = body["accessToken"]
        claims = json.loads(base64.urlsafe_b64decode(self.token.split(".")[1] + "==="))
        self.org_id = claims.get("orgId")

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
            raise SystemExit(f"{method} {path}: HTTP {e.code}: {e.read().decode()[:400]}") from None


def _login_ok(api: Api, secrets_file: pathlib.Path) -> bool:
    if not secrets_file.exists():
        return False
    env = dict(re.findall(r"export (\w+)=(\S+)", secrets_file.read_text()))
    try:
        api.call("POST", "/api/v1/auth/universal-auth/login",
                 {"clientId": env.get("INFISICAL_CLIENT_ID"), "clientSecret": env.get("INFISICAL_CLIENT_SECRET")}, auth=False)
        return True
    except SystemExit:
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--identity-id", help="reuse an existing identity ingressctl")
    args = ap.parse_args()
    api = Api()
    say = (lambda m: print(m)) if args.apply else (lambda m: print("[Dry run] " + m))

    cas = [c for c in api.call("GET", "/api/v1/cert-manager/ca")["certificateAuthorities"]
           if c["type"] == "internal" and c["status"] == "active" and c["configuration"]["type"] == "root"]
    if len(cas) != 1:
        raise SystemExit(f"expected exactly one active internal root CA, got: {[c['name'] for c in cas]}")
    ca = cas[0]
    print(f"Root CA: {ca['name']} ({ca['configuration']['dn']}, until {ca['configuration']['notAfter'][:10]})")

    policy = next((p for p in api.call("GET", "/api/v1/cert-manager/certificate-policies")["certificatePolicies"]
                   if p["name"] == POLICY["name"]), None)
    if policy is None:
        say(f"create policy {POLICY['name']}")
        if args.apply:
            policy = api.call("POST", "/api/v1/cert-manager/certificate-policies", POLICY)["certificatePolicy"]
    else:
        print(f"policy {POLICY['name']} present ({policy['id']})")

    profile = next((p for p in api.call("GET", "/api/v1/cert-manager/certificate-profiles")["certificateProfiles"]
                    if p["slug"] == NAME), None)
    if profile is None:
        say(f"create profile {NAME} (CA {ca['name']})")
        if args.apply:
            profile = api.call("POST", "/api/v1/cert-manager/certificate-profiles", {
                "slug": NAME, "description": "ingressctl / NPM", "caId": ca["id"],
                "certificatePolicyId": policy["id"], "issuerType": "ca", "defaults": PROFILE_DEFAULTS,
            })["certificateProfile"]
    else:
        print(f"profile {NAME} present ({profile['id']})")

    apps = api.call("GET", "/api/v1/cert-manager/applications")["applications"]
    app = next((a for a in apps if a["name"] == NAME), None)
    if app is None:
        say(f"create application {NAME}")
        if args.apply:
            app = api.call("POST", "/api/v1/cert-manager/applications",
                           {"name": NAME, "description": "ingressctl (NPM ingress)", "profileIds": [profile["id"]]})["application"]
    else:
        print(f"application {NAME} present ({app['id']})")
    if app and profile:
        linked = {p["profileId"]: p for p in api.call("GET", f"/api/v1/cert-manager/applications/{app['id']}/profiles")["profiles"]}
        if profile["id"] not in linked:
            say("attach profile to application")
            if args.apply:
                api.call("POST", f"/api/v1/cert-manager/applications/{app['id']}/profiles", {"profileIds": [profile["id"]]})
                linked = {p["profileId"]: p for p in api.call("GET", f"/api/v1/cert-manager/applications/{app['id']}/profiles")["profiles"]}
        if not (linked.get(profile["id"]) or {}).get("apiConfigId"):
            say("enable API enrollment (no server-side auto-renewal; ingressctl renews on its own)")
            if args.apply:
                api.call("PUT", f"/api/v1/cert-manager/applications/{app['id']}/profiles/{profile['id']}/enrollment/api",
                         {"autoRenew": False})

    secrets_file = ROOT / "infra/secrets/ingressctl.env"
    ident_id = args.identity_id
    if not ident_id and not secrets_file.exists():
        say(f"create machine identity {NAME} (org role no-access)")
        if args.apply:
            ident_id = api.call("POST", "/api/v1/identities",
                                {"name": NAME, "organizationId": api.org_id, "role": "no-access"})["identity"]["id"]
            print(f"identity created: {ident_id}")
    if ident_id and args.apply:
        ua_path = f"/api/v1/auth/universal-auth/identities/{ident_id}"
        try:
            ua = api.call("GET", ua_path)["identityUniversalAuth"]
        except SystemExit:
            ua = api.call("POST", ua_path, {"accessTokenTTL": 3600, "accessTokenMaxTTL": 86400})["identityUniversalAuth"]
        if not _login_ok(api, secrets_file):
            for old in api.call("GET", f"{ua_path}/client-secrets").get("clientSecretData", []):
                if not old.get("isClientSecretRevoked"):  # revoke secrets of aborted runs that were never stored
                    api.call("POST", f"{ua_path}/client-secrets/{old['id']}/revoke")
            secret = api.call("POST", f"{ua_path}/client-secrets", {"description": "ingressctl"})["clientSecret"]
            secrets_file.write_text(  # store it at once: Infisical shows the secret only once
                "# Infisical machine identity \"ingressctl\" (role operator in application ingressctl).\n"
                "# Created by infra/infisical/bootstrap.py; in the repository at the operator's request.\n"
                f"export INFISICAL_IDENTITY_ID={ident_id}\n"
                f"export INFISICAL_CLIENT_ID={ua['clientId']}\nexport INFISICAL_CLIENT_SECRET={secret}\n")
            print(f"credentials written: {secrets_file.relative_to(ROOT)}")
        project_id = ca["projectId"]
        members = api.call("GET", f"/api/v1/projects/{project_id}/identity-memberships").get("identityMemberships", [])
        if not any((m.get("identity") or {}).get("id") == ident_id for m in members):
            # prerequisite for the application membership; Certificate Manager allows only admin/member
            api.call("POST", f"/api/v1/projects/{project_id}/identity-memberships/{ident_id}", {"role": "member"})
            print("identity is a project member (member)")
        app_members = api.call("GET", f"/api/v1/cert-manager/applications/{app['id']}/identities")["memberships"]
        if not any(m.get("actorIdentityId") == ident_id for m in app_members):
            api.call("POST", f"/api/v1/cert-manager/applications/{app['id']}/identities/{ident_id}", {"role": "operator"})
        print(f"identity {NAME} is a member of the application (operator)")
    elif not ident_id:
        say(f"identity {NAME}: check memberships (pass --identity-id)")

    if args.apply:
        pem = api.call("GET", f"/api/v1/cert-manager/ca/internal/{ca['id']}/certificate")["certificate"]
        (ROOT / "docs/homelab-root-ca.pem").write_text(pem.strip() + "\n")
        inv = ROOT / "inventory/ingress.yaml"
        text = inv.read_text()
        text = re.sub(r"(    profile_id: )\S+", rf"\g<1>{profile['id']}", text, count=1)
        text = re.sub(r"(    application_id: )\S+", rf"\g<1>{app['id']}", text, count=1)
        text = re.sub(r"(    ca_bundle: )\S+", r"\g<1>docs/homelab-root-ca.pem", text, count=1)
        inv.write_text(text)
        print(f"inventory/ingress.yaml: profile_id={profile['id']} application_id={app['id']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
