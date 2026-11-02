"""Nginx Proxy Manager: the administrator (NPM 2.16 has no default user, the first one is created through the setup call) and a
proxy host for every service of the stack under <name>.<domain> (plain HTTP; certificates come with the private CA)."""
from __future__ import annotations

import secrets
import time

from .http import request, wait_for
from .steps import StepError, log, warn

STEP = "npm"


def host_payload(domain: str, forward_host: str, forward_port: int) -> dict:
    return {"domain_names": [domain], "forward_scheme": "http", "forward_host": forward_host, "forward_port": forward_port,
            "access_list_id": 0, "certificate_id": 0, "ssl_forced": False, "caching_enabled": False, "block_exploits": True,
            "allow_websocket_upgrade": True, "http2_support": False, "hsts_enabled": False, "hsts_subdomains": False,
            "meta": {}, "advanced_config": "", "locations": []}


class Npm:
    def __init__(self, cfg, state, *, http=request, wait=wait_for, sleep=time.sleep):
        self.cfg, self.state, self.http, self.wait, self.sleep = cfg, state, http, wait, sleep
        self.api = cfg.npm_url + "/api"
        self.token = ""

    def call(self, method: str, path: str, body=None):
        r = self.http(method, self.api + path, body=body, headers={"Authorization": f"Bearer {self.token}"} if self.token else None)
        if not r.ok:
            raise StepError(f"Nginx Proxy Manager {method} {path}: {r.status} {r.text[:200]}")
        return r.json()

    def run(self) -> None:
        self.wait(lambda: (self.http("GET", self.api + "/").json() or {}).get("status") == "OK", "Nginx Proxy Manager", timeout=240)
        self.ensure_admin()
        for sub, host, port, label in self.cfg.services:
            self.ensure_host(f"{sub}.{self.cfg.domain}", host, port, label)
        self.verify()

    def connect(self) -> None:
        """Sign in as the administrator without touching the hosts (for a step that runs apart from run())."""
        self.wait(lambda: (self.http("GET", self.api + "/").json() or {}).get("status") == "OK", "Nginx Proxy Manager", timeout=240)
        self.ensure_admin()

    def ensure_admin(self) -> None:
        email, password = self.cfg.admin_email, self.cfg.admin_password
        if not (self.http("GET", self.api + "/").json() or {}).get("setup"):
            r = self.http("POST", self.api + "/users", body={"name": self.cfg.admin_user, "nickname": self.cfg.admin_user, "email": email,
                                                               "roles": ["admin"], "is_disabled": False, "auth": {"type": "password", "secret": password}})
            if not r.ok:
                raise StepError(f"Nginx Proxy Manager refused to create the administrator: {r.status} {r.text[:200]}")
            log(STEP, f"administrator {email}: created")
        r = self.http("POST", self.api + "/tokens", body={"identity": email, "secret": password})
        if not r.ok:
            raise StepError(f"Nginx Proxy Manager does not know {email} with the administrator password: it was set up before with other "
                            "credentials; sign in to its admin page with those and create the proxy hosts there")
        self.token = r.json()["token"]
        log(STEP, f"administrator {email}: signed in")

    def ensure_technical_user(self) -> tuple[str, str]:
        """The account ingressctl uses: no administrator, no 2FA, sees every host (visibility all). Returns (email, password); the
        password is made here and kept in the state, since NPM never shows it again."""
        email = f"ingressctl@{self.cfg.domain}"
        password = self.state.get(STEP, "technical_password")
        if not password:
            password = secrets.token_urlsafe(24)
            self.state.set(STEP, "technical_password", password)
        if self.http("POST", self.api + "/tokens", body={"identity": email, "secret": password}).ok:
            log(STEP, f"technical user {email}: already there")
            return email, password
        user = next((u for u in self.call("GET", "/users") if u.get("email") == email), None)
        if user is None:
            r = self.http("POST", self.api + "/users", headers={"Authorization": f"Bearer {self.token}"}, body={
                "name": "ingressctl", "nickname": "ingressctl", "email": email, "roles": [], "is_disabled": False,
                "auth": {"type": "password", "secret": password}})
            if not r.ok:
                raise StepError(f"Nginx Proxy Manager refused to create the technical user: {r.status} {r.text[:200]}")
            user = r.json()
            log(STEP, f"technical user {email}: created")
        else:
            self.call("PUT", f"/users/{user['id']}/auth", {"type": "password", "secret": password})
            log(STEP, f"technical user {email}: password replaced")
        self.call("PUT", f"/users/{user['id']}/permissions", {"visibility": "all", "proxy_hosts": "manage", "redirection_hosts": "manage",
                                                               "dead_hosts": "manage", "streams": "manage", "access_lists": "manage", "certificates": "manage"})
        if not self.http("POST", self.api + "/tokens", body={"identity": email, "secret": password}).ok:
            raise StepError(f"the technical user {email} cannot sign in to Nginx Proxy Manager")
        return email, password

    def host_ids(self) -> dict[str, int]:
        """domain -> id of the proxy hosts that exist."""
        return {d: h["id"] for h in self.call("GET", "/nginx/proxy-hosts") for d in h["domain_names"]}

    def ensure_host(self, domain: str, forward_host: str, port: int, label: str) -> None:
        wanted = host_payload(domain, forward_host, port)
        have = next((h for h in self.call("GET", "/nginx/proxy-hosts") if domain in h["domain_names"]), None)
        if have is None:
            self.call("POST", "/nginx/proxy-hosts", wanted)
            log(STEP, f"proxy host {domain} -> {forward_host}:{port} ({label}): created")
        elif (have["forward_host"], have["forward_port"], have["forward_scheme"]) != (forward_host, port, "http"):
            self.call("PUT", f"/nginx/proxy-hosts/{have['id']}", wanted)
            log(STEP, f"proxy host {domain} -> {forward_host}:{port} ({label}): corrected")
        else:
            log(STEP, f"proxy host {domain} ({label}): already there")

    def verify(self) -> None:
        """The proxy reloads a moment after the host is saved; a host it does not know yet answers 404 (an upstream that is not up
        yet, homegraf starts after the bootstrap, answers 502, which is fine)."""
        for sub, _, _, label in self.cfg.services:
            name = f"{sub}.{self.cfg.domain}"
            for _ in range(15):
                if self.http("GET", self.cfg.npm_proxy_url + "/", headers={"Host": name}).status != 404:
                    break
                self.sleep(1)
            else:
                warn(STEP, f"the proxy does not answer for {name} yet (404); it may need a moment more")
