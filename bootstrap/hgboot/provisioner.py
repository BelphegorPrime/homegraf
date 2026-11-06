"""The identity that sets up the stacks which are only a folder, and its credentials file in the user's repository.

A push of stacks/<id>/ with a compose.yaml and an ingress.yaml is all a new app takes: the reconcile stage of the Komodo procedure
update-all (infra/komodo/reconcile_stacks.py, run by RunStackService in the stack ingressctl) generates the app's secrets in
Infisical, creates its Komodo stack and deploys it. It signs in to Infisical as the machine identity "stack-provisioner" and to
Komodo with an API key of its own (made by the bootstrap, so it can be revoked alone); both come from infra/secrets/provisioner.env, which this step commits to the repository
(the seed has no such file: its values exist only after the bootstrap).

The identity has no rights in the organisation and the project role `member` in every project stacks-<server>, the narrowest
built-in role that can create secrets (Infisical Free has no custom roles). Infisical shows a client secret once, so it is kept in the
state at once. tools/stack_secrets.py gives the identity the same role in each project it creates later.
"""
from __future__ import annotations

from .certificates import EVERYWHERE, org_id_of
from .http import request
from .steps import StepError, log

STEP = "provisioner"
NAME = "stack-provisioner"
FILE = "infra/secrets/provisioner.env"
ROLE = "member"
SLUG_PREFIX = "stacks-"


def shell_env(pairs: list[tuple[str, str]]) -> str:
    def q(v: str) -> str:
        return "'" + v.replace("'", "'\\''") + "'"
    return "".join(f"export {k}={q(v)}\n" for k, v in pairs)


def env_file(cfg, state, client_id: str, client_secret: str) -> str:
    return ("# Machine identity stack-provisioner (member of the stacks-* projects) and the Komodo API key, for the reconcile stage of\n"
            "# update-all (infra/komodo/reconcile_stacks.py). Written by the bootstrap; the repository is private to you.\n"
            + shell_env([("INFISICAL_URL", cfg.infisical_url), ("INFISICAL_PROVISIONER_CLIENT_ID", client_id),
                         ("INFISICAL_PROVISIONER_CLIENT_SECRET", client_secret), ("KOMODO_URL", cfg.komodo_url),
                         ("KOMODO_API_KEY", state.get("komodo", "provisioner_key")),
                         ("KOMODO_API_SECRET", state.get("komodo", "provisioner_secret"))]))


class Provisioner:
    def __init__(self, cfg, state, forgejo, *, http=request):
        self.cfg, self.state, self.forgejo, self.http = cfg, state, forgejo, http
        self.token = ""

    def call(self, method: str, path: str, body=None):
        r = self.http(method, self.cfg.infisical_url + path, body=body, headers={"Authorization": f"Bearer {self.token}"})
        if not r.ok:
            raise StepError(f"Infisical {method} {path}: {r.status} {r.text[:300]}")
        return r.json()

    def login(self, client_id: str, client_secret: str) -> str:
        r = self.http("POST", self.cfg.infisical_url + "/api/v1/auth/universal-auth/login", body={"clientId": client_id, "clientSecret": client_secret})
        return r.json()["accessToken"] if r.ok else ""

    def run(self) -> None:
        self.token = self.login(self.state.get("infisical", "client_id"), self.state.get("infisical", "client_secret"))
        if not self.token:
            raise StepError("the machine identity homegraf cannot sign in to Infisical any more: the provisioner identity is created with it")
        client_id, client_secret = self.ensure_identity()
        self.ensure_memberships(self.state.get(STEP, "identity_id"))
        if self.forgejo.put_files([(FILE, env_file(self.cfg, self.state, client_id, client_secret).encode())],
                                  "Credentials of the stack provisioner: new apps set themselves up on push"):
            log(STEP, f"{FILE}: committed")
        else:
            log(STEP, f"{FILE}: already as it should be")

    def ensure_identity(self) -> tuple[str, str]:
        cid, csec = self.state.get(STEP, "client_id"), self.state.get(STEP, "client_secret")
        if cid and csec and self.login(cid, csec):
            log(STEP, f"identity {NAME}: already there")
            return cid, csec
        iid = self.state.get(STEP, "identity_id")
        if not iid:
            iid = self.call("POST", "/api/v1/identities", {"name": NAME, "organizationId": org_id_of(self.token), "role": "no-access"})["identity"]["id"]
            self.state.set(STEP, "identity_id", iid)
        ua = f"/api/v1/auth/universal-auth/identities/{iid}"
        r = self.http("GET", self.cfg.infisical_url + ua, headers={"Authorization": f"Bearer {self.token}"})
        if r.ok:
            client_id = r.json()["identityUniversalAuth"]["clientId"]
        else:
            client_id = self.call("POST", ua, {"accessTokenTTL": 600, "accessTokenMaxTTL": 600, "accessTokenNumUsesLimit": 0,
                                               "clientSecretTrustedIps": EVERYWHERE, "accessTokenTrustedIps": EVERYWHERE})["identityUniversalAuth"]["clientId"]
        for old in self.call("GET", f"{ua}/client-secrets").get("clientSecretData", []):
            if not old.get("isClientSecretRevoked"):     # the secret of an interrupted run was never stored
                self.call("POST", f"{ua}/client-secrets/{old['id']}/revoke")
        secret = self.call("POST", f"{ua}/client-secrets", {"description": NAME})["clientSecret"]
        self.state.set(STEP, "client_id", client_id)
        self.state.set(STEP, "client_secret", secret)
        log(STEP, f"identity {NAME}: created (Universal Auth, no organisation rights)")
        return client_id, secret

    def ensure_memberships(self, identity: str) -> None:
        """Member of each project stacks-<server> that exists now; the ones made later get it from stack_secrets.py."""
        for project in self.call("GET", "/api/v1/projects?type=secret-manager").get("projects", []):
            if not str(project.get("slug", "")).startswith(SLUG_PREFIX):
                continue
            members = self.call("GET", f"/api/v1/projects/{project['id']}/identity-memberships").get("identityMemberships", [])
            if not any((m.get("identity") or {}).get("id") == identity for m in members):
                self.call("POST", f"/api/v1/projects/{project['id']}/identity-memberships/{identity}", {"role": ROLE})
                log(STEP, f"project {project['slug']}: {NAME} is a member")
