"""What a server needs before the first stack of it can keep secrets in Infisical: the project stacks-<server>, the machine identity
that reads it at deploy time, and the Komodo variables the compose wrapper (infra/infisical/compose-wrap.sh) is filled from.

The seed's tools (infra/infisical/stack_secrets.py) do the same for every server that has stacks with `secrets: infisical`. The
bootstrap does it once for the server it registered, so a folder with a compose.yaml and an ingress.yaml sets itself up on the very
first push (infra/komodo/reconcile_stacks.py would otherwise stop with "run stack_secrets.py once").

Names and variables are those of stack_secrets.py: project stacks-<server>, identity stacks-<server> (organisation role no-access,
project role viewer), Komodo variables INFISICAL_API_URL, INFISICAL_PROJECT_ID_<SERVER>, INFISICAL_CLIENT_ID_<SERVER> and the secret
INFISICAL_CLIENT_SECRET_<SERVER>. Infisical shows a client secret once, so it goes to Komodo at once.
"""
from __future__ import annotations

import re

from .certificates import EVERYWHERE, org_id_of
from .http import request
from .steps import StepError, log

STEP = "server-secrets"
ROLE = "viewer"           # built-in project role: read secrets, change nothing
TOKEN_TTL = 600           # the wrapper needs a token for one compose call only


def project_slug(server: str) -> str:
    return "stacks-" + re.sub(r"[^a-z0-9-]+", "-", server.lower()).strip("-")


def var_suffix(server: str) -> str:
    return re.sub(r"[^A-Z0-9]", "_", server.upper())


class ServerSecrets:
    def __init__(self, cfg, state, komodo, *, http=request):
        self.cfg, self.state, self.komodo, self.http = cfg, state, komodo, http
        self.token = ""

    def call(self, method: str, path: str, body=None):
        r = self.http(method, self.cfg.infisical_url + path, body=body, headers={"Authorization": f"Bearer {self.token}"})
        if not r.ok:
            raise StepError(f"Infisical {method} {path}: {r.status} {r.text[:300]}")
        return r.json()

    def login(self, client_id: str, client_secret: str) -> str:
        r = self.http("POST", self.cfg.infisical_url + "/api/v1/auth/universal-auth/login", body={"clientId": client_id, "clientSecret": client_secret})
        return r.json()["accessToken"] if r.ok else ""

    def set_variable(self, existing: dict, name: str, value: str, secret: bool) -> None:
        if name not in existing:
            self.komodo.call("write", "CreateVariable", {"name": name, "value": value, "description": "Infisical for compose-wrap.sh (homegraf bootstrap)",
                                                        "is_secret": secret})
        elif existing[name] != value:
            self.komodo.call("write", "UpdateVariableValue", {"name": name, "value": value})
        existing[name] = value

    def run(self, server: str) -> None:
        self.token = self.login(self.state.get("infisical", "client_id"), self.state.get("infisical", "client_secret"))
        if not self.token:
            raise StepError("the machine identity homegraf cannot sign in to Infisical any more: the secrets of the server are set up with it")
        slug, sfx = project_slug(server), var_suffix(server)
        done = self.state.get(STEP, server)
        variables = {v["name"]: v["value"] for v in self.komodo.call("read", "ListVariables") or []}
        if done and all(f"{n}_{sfx}" in variables for n in ("INFISICAL_PROJECT_ID", "INFISICAL_CLIENT_ID", "INFISICAL_CLIENT_SECRET")):
            log(STEP, f"server {server}: secrets are already set up ({slug})")
            return
        project = next((p["id"] for p in self.call("GET", "/api/v1/projects?type=secret-manager").get("projects", []) if p["slug"] == slug), None)
        if project is None:
            project = self.call("POST", "/api/v1/projects", {"projectName": slug, "slug": slug, "type": "secret-manager"})["project"]["id"]
            log(STEP, f"project {slug}: created")
        self.set_variable(variables, "INFISICAL_API_URL", self.cfg.infisical_url, False)
        self.set_variable(variables, f"INFISICAL_PROJECT_ID_{sfx}", project, False)

        iid = self.state.get(STEP, f"identity_{server}")
        if not iid:
            iid = next((m["identity"]["id"] for m in self.call("GET", "/api/v1/identities?orgId=" + org_id_of(self.token)).get("identities", [])
                        if m["identity"]["name"] == slug), None)
        if not iid:
            iid = self.call("POST", "/api/v1/identities", {"name": slug, "organizationId": org_id_of(self.token), "role": "no-access"})["identity"]["id"]
        self.state.set(STEP, f"identity_{server}", iid)
        ua = f"/api/v1/auth/universal-auth/identities/{iid}"
        r = self.http("GET", self.cfg.infisical_url + ua, headers={"Authorization": f"Bearer {self.token}"})
        client_id = r.json()["identityUniversalAuth"]["clientId"] if r.ok else self.call("POST", ua, {
            "accessTokenTTL": TOKEN_TTL, "accessTokenMaxTTL": TOKEN_TTL, "accessTokenNumUsesLimit": 0,
            "clientSecretTrustedIps": EVERYWHERE, "accessTokenTrustedIps": EVERYWHERE})["identityUniversalAuth"]["clientId"]
        for old in self.call("GET", f"{ua}/client-secrets").get("clientSecretData", []):
            if not old.get("isClientSecretRevoked"):
                self.call("POST", f"{ua}/client-secrets/{old['id']}/revoke")
        secret = self.call("POST", f"{ua}/client-secrets", {"description": f"compose-wrap.sh on {server}"})["clientSecret"]
        self.set_variable(variables, f"INFISICAL_CLIENT_ID_{sfx}", client_id, True)
        self.set_variable(variables, f"INFISICAL_CLIENT_SECRET_{sfx}", secret, True)

        members = self.call("GET", f"/api/v1/projects/{project}/identity-memberships").get("identityMemberships", [])
        if not any((m.get("identity") or {}).get("id") == iid for m in members):
            self.call("POST", f"/api/v1/projects/{project}/identity-memberships/{iid}", {"role": ROLE})
        self.state.set(STEP, server, True)
        log(STEP, f"server {server}: project {slug}, identity {slug} ({ROLE}) and the Komodo variables for the compose wrapper are set up")
