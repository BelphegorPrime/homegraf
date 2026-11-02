from __future__ import annotations

import datetime as dt
import pathlib
import subprocess
import textwrap

import pytest

from fakes import FakeChecker, FakeInfisical, FakeNpm, FakeCa, session_for
from ingressctl.infisical import InfisicalClient
from ingressctl.inventory import load_inventory
from ingressctl.manifest import load_all
from ingressctl.npm import NpmClient
from ingressctl.reconcile import Context
from ingressctl.state import State

INVENTORY = {
    "inventory/stacks.yaml": """
        stacks:
          - {id: omniroute, namespace: ai-sandbox, path: stacks/ai/omniroute, compose_files: [compose.yaml], status: verified}
          - {id: seerr, namespace: nas-compose, path: stacks/nas/seerr, compose_files: [compose.yaml], status: verified}
    """,
    "inventory/servers.yaml": """
        servers:
          worker: {address: 10.0.0.20, status: verified}
          unas: {address: 10.0.0.30, status: verified}
    """,
    "inventory/namespaces.yaml": """
        namespaces:
          nas-compose: {paths: [stacks/nas/]}
          ai-sandbox: {paths: [stacks/ai/]}
    """,
    "inventory/ingress.yaml": """
        npm_instances:
          main: {server: unas, api_url: http://npm.test/api, tls_address: 10.0.0.30, tls_port: 443}
        issuers:
          homelab-private-ca:
            type: infisical
            api_url: http://infisical.test
            project_id: proj
            profile_id: prof
            application_id: app
            max_validity_days: 90
            ca_bundle: CA_BUNDLE
            allowed_domains: ["*.lab.example"]
        controller: {default_npm_instance: main, allowed_repos: [owner/infrastructure]}
    """,
}

OMNI = """
    apiVersion: homelab.ingress/v1
    namespace: ai-sandbox
    services:
      - id: omniroute
        stack: omniroute
        lifecycle: present
        proxy:
          domains: [Omni.Lab.Example.]
          upstream: {scheme: http, server: worker, port: 20128}
          websockets: true
          force_https: true
        certificate: {issuer_ref: homelab-private-ca, validity_days: 90, renew_before_days: 30}
        healthcheck: {path: /, accepted_statuses: [200, 302, 401]}
"""

SEERR = """
    apiVersion: homelab.ingress/v1
    namespace: nas-compose
    services:
      - id: seerr
        stack: seerr
        lifecycle: present
        proxy:
          domains: [seerr.lab.example]
          upstream: {scheme: http, server: unas, port: 8089}
        certificate: {issuer_ref: homelab-private-ca}
        healthcheck: {path: /, accepted_statuses: [200]}
"""


def git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    return subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()


class Env:
    def __init__(self, tmp: pathlib.Path):
        self.root = tmp / "repo"
        self.ca = FakeCa()
        bundle = tmp / "ca.pem"
        bundle.write_text(self.ca.pem)
        for rel, content in INVENTORY.items():
            p = self.root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(textwrap.dedent(content).replace("CA_BUNDLE", str(bundle)))
        for d in ("stacks/ai/omniroute", "stacks/nas/seerr"):
            (self.root / d).mkdir(parents=True)
            (self.root / d / "compose.yaml").write_text("services: {app: {image: x}}\n")
        self.write("stacks/ai/omniroute/ingress.yaml", OMNI)
        self.write("stacks/nas/seerr/ingress.yaml", SEERR)
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
        self.commit_sha = git(self.root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
        self.npm = FakeNpm()
        self.inf = FakeInfisical(self.ca)
        self.checker = FakeChecker(self.npm)
        self.state_dir = tmp / "state"
        self.now = dt.datetime.now(dt.timezone.utc)

    def write(self, rel, content):
        (self.root / rel).write_text(textwrap.dedent(content))

    def commit(self, msg="change"):
        git(self.root, "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
        self.commit_sha = git(self.root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", msg)
        return self.commit_sha

    def ctx(self) -> Context:
        inv = load_inventory(self.root)
        npm = NpmClient("http://npm.test/api", "bot@test", "pw", session=session_for(self.npm))
        npm.login()
        inf = InfisicalClient("http://infisical.test", "cid", "csecret", session=session_for(self.inf))
        inf.login()
        return Context(root=self.root, inv=inv, state=State(self.state_dir), npm={"main": npm},
                       issuers={"homelab-private-ca": inf}, checker=self.checker, clock=lambda: self.now,
                       verify_attempts=3, verify_delay=0)

    def specs(self, stack=None):
        specs = load_all(self.root, load_inventory(self.root))
        return [s for s in specs if stack is None or s.stack_id == stack]


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)
