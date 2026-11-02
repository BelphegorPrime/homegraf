"""The private certificate authority in Infisical (Certificate Manager) and the identity ingressctl signs with.

Creates, by name, so a rerun finds what is there: a project of type cert-manager, an internal root CA (ten years), a policy that
allows only *.<domain>, a profile on that CA, an application with API enrollment, and the machine identity "ingressctl" (no rights
in the organisation, operator in the application). Infisical shows a client secret once, so it is kept in the state at once.
"""
from __future__ import annotations

import base64
import datetime
import json

from .http import request
from .steps import StepError, log

STEP = "certificates"
PROJECT = "homegraf-ca"
CA_NAME = "homegraf-root-ca"
NAME = "ingressctl"             # policy, profile, application and identity
ROOT_YEARS = 10
PROFILE_DEFAULTS = {"keyAlgorithm": "EC_prime256v1", "signatureAlgorithm": "ECDSA-SHA256", "ttlDays": 90,
                    "keyUsages": ["digital_signature"], "extendedKeyUsages": ["server_auth"]}
EVERYWHERE = [{"ipAddress": "0.0.0.0/0"}, {"ipAddress": "::/0"}]


def policy(domain: str) -> dict:
    return {
        "name": NAME, "description": f"TLS server certificates for *.{domain} through ingressctl (Nginx Proxy Manager)",
        "subject": [{"type": "common_name", "allowed": [f"*.{domain}"]}],
        "sans": [{"type": "dns_name", "allowed": [f"*.{domain}"]}],
        "keyUsages": {"allowed": ["digital_signature", "key_encipherment"], "required": ["digital_signature"]},
        "extendedKeyUsages": {"allowed": ["server_auth"], "required": ["server_auth"]},
        "algorithms": {"signature": ["SHA256-ECDSA", "SHA384-ECDSA"], "keyAlgorithm": ["ECDSA-P256", "ECDSA-P384"]},
        "validity": {"max": "90d"},
    }


def org_id_of(token: str) -> str:
    """The organisation an Infisical access token belongs to (claim orgId)."""
    try:
        payload = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))["orgId"]
    except (IndexError, ValueError, KeyError):
        raise StepError("Infisical's access token has no organisation") from None


class Certificates:
    def __init__(self, cfg, state, *, http=request):
        self.cfg, self.state, self.http = cfg, state, http
        self.token = ""
        self.org = ""
        self.project = ""

    def call(self, method: str, path: str, body=None, *, auth: bool = True):
        r = self.http(method, self.cfg.infisical_url + path, body=body, headers={"Authorization": f"Bearer {self.token}"} if auth else None)
        if not r.ok:
            raise StepError(f"Infisical {method} {path}: {r.status} {r.text[:300]}")
        return r.json()

    def login(self, client_id: str, client_secret: str) -> str:
        r = self.http("POST", self.cfg.infisical_url + "/api/v1/auth/universal-auth/login", body={"clientId": client_id, "clientSecret": client_secret})
        return r.json()["accessToken"] if r.ok else ""

    def run(self) -> dict:
        """Returns what ingress needs: the ids for inventory/ingress.yaml, the root certificate, the identity's client id and secret."""
        self.token = self.login(self.state.get("infisical", "client_id"), self.state.get("infisical", "client_secret"))
        if not self.token:
            raise StepError("the machine identity homegraf cannot sign in to Infisical any more: the certificate authority is set up with it")
        self.org = org_id_of(self.token)
        project = self.project = self.ensure_project()
        self.activate(project)
        ca = self.ensure_ca(project)
        policy_id = self.ensure_policy()
        profile = self.ensure_profile(ca, policy_id)
        app = self.ensure_application(profile)
        client_id, client_secret = self.ensure_identity(project, app)
        pem = self.call("GET", f"/api/v1/cert-manager/ca/internal/{ca['id']}/certificate")["certificate"].strip() + "\n"
        return {"project_id": project, "profile_id": profile, "application_id": app, "root_pem": pem,
                "client_id": client_id, "client_secret": client_secret}

    def ensure_project(self) -> str:
        have = next((p for p in self.call("GET", "/api/v1/projects?type=cert-manager")["projects"] if p["slug"] == PROJECT), None)
        if have:
            log(STEP, f"project {PROJECT}: already there")
            return have["id"]
        pid = self.call("POST", "/api/v1/projects", {"projectName": PROJECT, "slug": PROJECT, "type": "cert-manager"})["project"]["id"]
        log(STEP, f"project {PROJECT} (Certificate Manager): created")
        return pid

    def activate(self, project: str) -> None:
        """Certificate Manager works on one project per organisation, the active one; applications refuse to work before it is set."""
        self.call("POST", "/api/v1/cert-manager/instance/active-project", {"projectId": project})

    def ensure_ca(self, project: str) -> dict:
        cas = [c for c in self.call("GET", f"/api/v1/cert-manager/ca?projectId={project}")["certificateAuthorities"] if c["projectId"] == project and c["type"] == "internal"]
        have = next((c for c in cas if c["name"] == CA_NAME), None)
        if have:
            log(STEP, f"root CA {CA_NAME}: already there")
            return have
        if cas:
            # The free self-hosted edition allows one CA; this is not ours, so it is not guessed which is meant
            raise StepError(f"the project {PROJECT} has a certificate authority that is not {CA_NAME!r}: remove it in Infisical or set up the "
                            "certificates by hand")
        year = _next_year_end(ROOT_YEARS)
        ca = self.call("POST", "/api/v1/cert-manager/ca/internal", {
            "projectId": project, "name": CA_NAME, "type": "root", "status": "active", "enableDirectIssuance": True,
            "configuration": {"type": "root", "friendlyName": "homegraf root CA", "commonName": f"homegraf root CA ({self.cfg.domain})",
                              "organization": "homegraf", "keyAlgorithm": "EC_prime256v1", "notAfter": year, "maxPathLength": -1}})
        log(STEP, f"root CA {CA_NAME}: created (valid until {year[:10]})")
        return ca

    def ensure_policy(self) -> str:
        have = next((p for p in self.call("GET", f"/api/v1/cert-manager/certificate-policies?projectId={self.project}")["certificatePolicies"] if p["name"] == NAME), None)
        if have:
            log(STEP, f"policy {NAME}: already there")
            return have["id"]
        pid = self.call("POST", "/api/v1/cert-manager/certificate-policies", {**policy(self.cfg.domain), "projectId": self.project})["certificatePolicy"]["id"]
        log(STEP, f"policy {NAME}: created (only *.{self.cfg.domain}, server authentication, at most 90 days)")
        return pid

    def ensure_profile(self, ca: dict, policy_id: str) -> str:
        have = next((p for p in self.call("GET", f"/api/v1/cert-manager/certificate-profiles?projectId={self.project}")["certificateProfiles"] if p["slug"] == NAME), None)
        if have:
            log(STEP, f"profile {NAME}: already there")
            return have["id"]
        pid = self.call("POST", "/api/v1/cert-manager/certificate-profiles", {
            "slug": NAME, "description": "ingressctl / Nginx Proxy Manager", "caId": ca["id"], "certificatePolicyId": policy_id,
            "issuerType": "ca", "defaults": PROFILE_DEFAULTS, "projectId": self.project})["certificateProfile"]["id"]
        log(STEP, f"profile {NAME}: created")
        return pid

    def ensure_application(self, profile: str) -> str:
        app = next((a for a in self.call("GET", f"/api/v1/cert-manager/applications?projectId={self.project}")["applications"] if a["name"] == NAME), None)
        if app is None:
            app = self.call("POST", "/api/v1/cert-manager/applications", {"name": NAME, "description": "ingressctl (proxy hosts)", "profileIds": [profile], "projectId": self.project})["application"]
            log(STEP, f"application {NAME}: created")
        base = f"/api/v1/cert-manager/applications/{app['id']}"
        linked = {p["profileId"]: p for p in self.call("GET", f"{base}/profiles")["profiles"]}
        if profile not in linked:
            self.call("POST", f"{base}/profiles", {"profileIds": [profile]})
            linked = {p["profileId"]: p for p in self.call("GET", f"{base}/profiles")["profiles"]}
        if not (linked.get(profile) or {}).get("apiConfigId"):
            self.call("PUT", f"{base}/profiles/{profile}/enrollment/api", {"autoRenew": False})     # ingressctl renews itself
            log(STEP, f"application {NAME}: API enrollment on")
        return app["id"]

    def ensure_identity(self, project: str, app: str) -> tuple[str, str]:
        cid, csec = self.state.get(STEP, "client_id"), self.state.get(STEP, "client_secret")
        if cid and csec and self.login(cid, csec):
            log(STEP, f"identity {NAME}: already there")
            self.ensure_membership(project, app, self.state.get(STEP, "identity_id"))
            return cid, csec
        iid = self.state.get(STEP, "identity_id")
        if not iid:
            iid = self.call("POST", "/api/v1/identities", {"name": NAME, "organizationId": self.org, "role": "no-access"})["identity"]["id"]
            self.state.set(STEP, "identity_id", iid)
        ua = f"/api/v1/auth/universal-auth/identities/{iid}"
        r = self.http("GET", self.cfg.infisical_url + ua, headers={"Authorization": f"Bearer {self.token}"})
        if r.ok:
            client_id = r.json()["identityUniversalAuth"]["clientId"]
        else:
            client_id = self.call("POST", ua, {"accessTokenTTL": 3600, "accessTokenMaxTTL": 86400, "accessTokenNumUsesLimit": 0,
                                               "clientSecretTrustedIps": EVERYWHERE, "accessTokenTrustedIps": EVERYWHERE})["identityUniversalAuth"]["clientId"]
        for old in self.call("GET", f"{ua}/client-secrets").get("clientSecretData", []):
            if not old.get("isClientSecretRevoked"):     # the secret of an interrupted run was never stored
                self.call("POST", f"{ua}/client-secrets/{old['id']}/revoke")
        secret = self.call("POST", f"{ua}/client-secrets", {"description": NAME})["clientSecret"]
        self.state.set(STEP, "client_id", client_id)
        self.state.set(STEP, "client_secret", secret)
        self.ensure_membership(project, app, iid)
        log(STEP, f"identity {NAME}: created (Universal Auth, operator of the application)")
        return client_id, secret

    def ensure_membership(self, project: str, app: str, identity: str) -> None:
        members = self.call("GET", f"/api/v1/projects/{project}/identity-memberships").get("identityMemberships", [])
        if not any((m.get("identity") or {}).get("id") == identity for m in members):
            # a precondition of the application membership; the Certificate Manager knows only the roles admin and member
            self.call("POST", f"/api/v1/projects/{project}/identity-memberships/{identity}", {"role": "member"})
        base = f"/api/v1/cert-manager/applications/{app}/identities"
        if not any(m.get("actorIdentityId") == identity for m in self.call("GET", base)["memberships"]):
            self.call("POST", f"{base}/{identity}", {"role": "operator"})


def _next_year_end(years: int, now=None) -> str:
    today = now or datetime.datetime.now(datetime.timezone.utc)
    return f"{today.year + years}-12-31T00:00:00.000Z"
