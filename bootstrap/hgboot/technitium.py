"""Technitium DNS Server (only with DNS_MODE=technitium, the default): the zone of the domain, the wildcard to this host, an API token.

The server starts with the administrator's password from the environment (DNS_SERVER_ADMIN_PASSWORD, the same as everywhere: ADMIN_PASSWORD),
so there is no first-run page. This step makes, by name, so a rerun finds what is there: the Primary zone <domain>, the record
*.<domain> -> HOST_IP (every future app is reachable without another DNS step) and an API token `homegraf` that does not expire. Unlike
AdGuard Home, Technitium has real API tokens: the tools use the token and never see the password. The token is kept in the state.
"""
from __future__ import annotations

import urllib.parse

from .http import Unreachable, request, wait_for
from .steps import StepError, log

STEP = "technitium"
USER = "admin"            # the built-in administrator of Technitium; its password is ADMIN_PASSWORD
TOKEN_NAME = "homegraf"
WILDCARD_TTL = 300


class Technitium:
    def __init__(self, cfg, state, *, http=request, wait=wait_for):
        self.cfg, self.state, self.http, self.wait = cfg, state, http, wait

    def call(self, path: str, token: str | None = None, **params):
        """GET /api/...; Technitium answers 200 with {"status": "ok" | "error" | "invalid-token", ...}. Returns the decoded body."""
        query = urllib.parse.urlencode({k: str(v) for k, v in params.items()})
        r = self.http("GET", f"{self.cfg.technitium_url}{path}" + (f"?{query}" if query else ""),
                      headers={"Authorization": f"Bearer {token}"} if token else None)
        if not r.ok:
            raise StepError(f"Technitium {path}: {r.status} {r.text[:200]}")
        return r.json()

    def token_ok(self, token: str) -> bool:
        try:
            return bool(token) and self.call("/api/zones/list", token).get("status") == "ok"
        except (StepError, Unreachable):
            return False

    def ensure_token(self) -> str:
        token = self.state.get(STEP, "token")
        if token and self.token_ok(token):
            log(STEP, f"API token {TOKEN_NAME}: already there")
            return token
        return self.create_token()

    def create_token(self) -> str:
        r = self.call("/api/user/createToken", **{"user": USER, "pass": self.cfg.admin_password, "tokenName": TOKEN_NAME})
        if r.get("status") != "ok" or not r.get("token"):
            raise StepError("Technitium refused the administrator's password: it is set from ADMIN_PASSWORD on the very first start only; if "
                            f"Technitium ran before with another one, sign in at {self.cfg.technitium_url} and change it, or remove its volume "
                            f"(technitium-config). Answer: {str(r)[:160]}")
        self.state.set(STEP, "token", r["token"])
        log(STEP, f"API token {TOKEN_NAME}: created (does not expire; the tools use it instead of the password)")
        return r["token"]

    def run(self) -> None:
        self.wait(lambda: self.http("GET", self.cfg.technitium_url + "/api/status").ok, "Technitium DNS Server", timeout=120)
        token = self.ensure_token()
        domain, host = self.cfg.domain, self.cfg.host_ip

        zones = {z["name"] for z in self.call("/api/zones/list", token).get("response", {}).get("zones", [])}
        if domain in zones:
            log(STEP, f"zone {domain}: already there")
        else:
            r = self.call("/api/zones/create", token, zone=domain, type="Primary")
            if r.get("status") != "ok":
                raise StepError(f"Technitium refused the zone {domain}: {str(r)[:200]}")
            log(STEP, f"zone {domain}: created")

        r = self.call("/api/zones/records/add", token, zone=domain, domain=f"*.{domain}", type="A", ipAddress=host, ttl=WILDCARD_TTL, overwrite="true")
        if r.get("status") != "ok":
            raise StepError(f"Technitium refused the record *.{domain}: {str(r)[:200]}")
        log(STEP, f"record *.{domain} -> {host}: set")
