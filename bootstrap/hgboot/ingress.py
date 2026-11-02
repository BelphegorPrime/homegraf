"""Certificates for the proxy hosts: the private CA, the accounts ingressctl needs, and ingressctl itself.

ingressctl is the tool of the seed that reconciles every ingress.yaml of the repository with Nginx Proxy Manager and gets a
certificate for each host from the CA. Its credentials live on their own volume (not in the repository); the services of the
stack itself are described in external/ingress.yaml of the seed, and the proxy hosts the bootstrap created for them are adopted.
"""
from __future__ import annotations

import os
import pathlib
import re

from .certificates import Certificates
from .steps import StepError, log

STEP = "ingress"
INVENTORY = "inventory/ingress.yaml"
ROOT_CA = "inventory/homegraf-root-ca.pem"
EXTERNAL = "external/ingress.yaml"
NAMESPACE = "infrastructure"


def patch_ingress_yaml(text: str, ids: dict) -> str:
    """The three ids of the CA in inventory/ingress.yaml (null in the seed)."""
    for key in ("project_id", "profile_id", "application_id"):
        text, n = re.subn(rf"(\n    {key}: )\S+", lambda m, v=ids[key]: m.group(1) + v, text, count=1)
        if not n:
            raise StepError(f"{INVENTORY} has no {key}: the seed is not the one this bootstrap belongs to")
    return text


def credentials_env(npm_url: str, npm_identity: str, npm_secret: str, client_id: str, client_secret: str) -> str:
    def q(v: str) -> str:
        return "'" + v.replace("'", "'\\''") + "'"
    return "".join(f"export {k}={q(v)}\n" for k, v in (
        ("NPM_API_URL", npm_url), ("NPM_IDENTITY", npm_identity), ("NPM_SECRET", npm_secret),
        ("INFISICAL_CLIENT_ID", client_id), ("INFISICAL_CLIENT_SECRET", client_secret)))


def resource_ids(cfg) -> list[tuple[str, str]]:
    """(resource id in ingressctl, domain prefix) of the services of the stack."""
    return [(f"{NAMESPACE}/{sub}", sub) for sub, *_ in cfg.services]


def entry(domain: str, sub: str, host: str, port: int, label: str) -> str:
    """The block of a service in external/ingress.yaml (the same shape as the seed's)."""
    return f"""- id: {sub}        # {label}
  stack: external
  lifecycle: present
  proxy:
    domains: [{sub}.{domain}]
    upstream: {{scheme: http, host: {host}, port: {port}}}
    websockets: true
    force_https: false
  certificate: {{issuer_ref: homelab-private-ca, validity_days: 90, renew_before_days: 30}}
  healthcheck: {{path: /, accepted_statuses: [200, 301, 302, 303, 307, 308, 401, 502]}}
"""


def with_optional_services(text: str, cfg) -> str:
    """external/ingress.yaml with the services of the profiles that are on (the seed lists the base five), each once."""
    for sub, host, port, label in cfg.optional_services:
        if f"\n- id: {sub} " not in text and f"\n- id: {sub}\n" not in text:
            text = text.rstrip("\n") + "\n" + entry(cfg.domain, sub, host, port, label)
    return text


class Ingress:
    def __init__(self, cfg, state, forgejo, komodo, npm, *, certificates=None):
        self.cfg, self.state, self.forgejo, self.komodo, self.npm = cfg, state, forgejo, komodo, npm
        self.certificates = certificates or Certificates(cfg, state)

    def run(self) -> None:
        if self.state.get(STEP, "done"):
            log(STEP, "certificates and ingressctl: already set up")
            return
        ids = self.certificates.run()
        email, password = self.npm.ensure_technical_user()
        self.write_credentials(email, password, ids)
        text = self.forgejo.read_file(INVENTORY)
        if text is None:
            raise StepError(f"the repository has no {INVENTORY}: it was not seeded by this bootstrap")
        external = self.forgejo.read_file(EXTERNAL)
        if external is None:
            raise StepError(f"the repository has no {EXTERNAL}: it was not seeded by this bootstrap")
        self.forgejo.put_files([(INVENTORY, patch_ingress_yaml(text, ids).encode()), (ROOT_CA, ids["root_pem"].encode()),
                                (EXTERNAL, with_optional_services(external, self.cfg).encode())],
                               "Certificate authority: the ids of the Infisical CA and its root certificate")
        self.komodo.ensure_ingress()
        self.komodo.deploy_ingress()
        self.adopt_hosts()
        ok, out = self.komodo.run_ingress(["apply", "--root", "/repo", "--all"])
        if not ok:
            raise StepError("ingressctl apply did not succeed:\n" + out[-1500:])
        log(STEP, "ingressctl apply: every proxy host of the stack has a certificate from your CA")
        self.komodo.apply_ingress_in_update_all()
        self.state.set(STEP, "done", True)

    def write_credentials(self, email: str, password: str, ids: dict) -> None:
        path = pathlib.Path(self.cfg.credentials_dir) / "ingressctl.env"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(credentials_env(f"{self.cfg.npm_url}/api", email, password, ids["client_id"], ids["client_secret"]))
        os.chmod(path, 0o644)         # read by ingressctl (another user) in a container that mounts the volume read-only
        log(STEP, f"credentials of ingressctl: written to the volume ({path.name})")

    def adopt_hosts(self) -> None:
        """The proxy hosts the bootstrap made for the services of the stack become ingressctl's (it refuses a host it does not own)."""
        adopted = set(self.state.get(STEP, "adopted", []))
        hosts = self.npm.host_ids()
        for resource, sub in resource_ids(self.cfg):
            if resource in adopted:
                continue
            host = hosts.get(f"{sub}.{self.cfg.domain}")
            if host is None:
                raise StepError(f"no proxy host for {sub}.{self.cfg.domain} to adopt")
            ok, out = self.komodo.run_ingress(["adopt", "--resource", resource, "--npm-host-id", str(host)])
            if not ok:
                raise StepError(f"ingressctl adopt {resource}: {out[-500:]}")
            adopted.add(resource)
            self.state.set(STEP, "adopted", sorted(adopted))
            log(STEP, f"proxy host {sub}.{self.cfg.domain}: adopted by ingressctl")
