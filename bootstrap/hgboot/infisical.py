"""Infisical: bootstrap the instance (administrator and organisation), then a machine identity for homegraf with Universal Auth.

The tools create what a stack needs themselves (project stacks-<server>, folders, per-server identities) with that identity.
"""
from __future__ import annotations

import re
import secrets
import string

from .http import request, wait_for
from .steps import StepError, log

STEP = "infisical"
ORG = "homegraf"
IDENTITY = "homegraf"
EVERYWHERE = [{"ipAddress": "0.0.0.0/0"}, {"ipAddress": "::/0"}]


def meets_policy(password: str) -> bool:
    """Infisical's default password policy: 14 to 100 characters, a letter, a digit or special character, no run of four equal
    characters, no escape characters."""
    return (14 <= len(password) <= 100 and re.search(r"[^\W\d_]", password) is not None
            and re.search(r"[\d!@#$%^&*(),.?\":{}|<>]|[^\w\s]", password) is not None
            and not re.search(r"(.)\1{3}", password) and not re.search(r"[\n\t\r\\]", password))


def generate_password() -> str:
    while True:
        candidate = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(24))
        if meets_policy(candidate):
            return candidate


class Infisical:
    def __init__(self, cfg, state, *, http=request, wait=wait_for, password_factory=generate_password):
        self.cfg, self.state, self.http, self.wait, self.password_factory = cfg, state, http, wait, password_factory

    def call(self, method: str, path: str, body=None, token: str | None = None):
        r = self.http(method, self.cfg.infisical_url + path, body=body, headers={"Authorization": f"Bearer {token}"} if token else None)
        if not r.ok:
            raise StepError(f"Infisical {method} {path}: {r.status} {r.text[:200]}")
        return r.json()

    def login_ok(self, client_id: str, client_secret: str) -> bool:
        return self.http("POST", self.cfg.infisical_url + "/api/v1/auth/universal-auth/login",
                         body={"clientId": client_id, "clientSecret": client_secret}).ok

    def run(self) -> None:
        self.wait(lambda: self.http("GET", self.cfg.infisical_url + "/api/status").ok, "Infisical", timeout=300)
        cid, csec = self.state.get(STEP, "client_id"), self.state.get(STEP, "client_secret")
        if cid and csec and self.login_ok(cid, csec):
            log(STEP, "machine identity for homegraf: already there")
            return
        if self.call("GET", "/api/v1/admin/config")["config"]["initialized"]:
            raise StepError("Infisical was set up before (not by this bootstrap), so the machine identity cannot be created from here: "
                            "in Infisical create an identity with the organisation role Admin and Universal Auth, and put its client id and "
                            "secret into .env as INFISICAL_CLIENT_ID and INFISICAL_CLIENT_SECRET")
        password = self.cfg.admin_password
        if not meets_policy(password):
            password = self.password_factory()
            self.state.set(STEP, "admin_password", password)
            log(STEP, "ADMIN_PASSWORD does not meet Infisical's password policy (14 characters, a letter, a digit): the administrator "
                      "gets a generated password; show it with: docker compose run --rm bootstrap show infisical-password")
        boot = self.call("POST", "/api/v1/admin/bootstrap", {"email": self.cfg.admin_email, "password": password, "organization": ORG})
        token, org = boot["identity"]["credentials"]["token"], boot["organization"]["id"]
        log(STEP, f"instance bootstrapped: administrator {self.cfg.admin_email}, organisation {ORG}")
        iid = self.call("POST", "/api/v1/identities", {"name": IDENTITY, "organizationId": org, "role": "admin"}, token)["identity"]["id"]
        client_id = self.call("POST", f"/api/v1/auth/universal-auth/identities/{iid}", {
            "accessTokenTTL": 3600, "accessTokenMaxTTL": 3600, "accessTokenNumUsesLimit": 0,
            "clientSecretTrustedIps": EVERYWHERE, "accessTokenTrustedIps": EVERYWHERE}, token)["identityUniversalAuth"]["clientId"]
        client_secret = self.call("POST", f"/api/v1/auth/universal-auth/identities/{iid}/client-secrets", {"description": "homegraf"}, token)["clientSecret"]
        if not self.login_ok(client_id, client_secret):
            raise StepError("the new machine identity cannot sign in to Infisical")
        self.state.set(STEP, "client_id", client_id)
        self.state.set(STEP, "client_secret", client_secret)
        log(STEP, f"machine identity {IDENTITY} (organisation role admin, Universal Auth): created")
