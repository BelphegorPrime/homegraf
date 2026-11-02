"""AdGuard Home (only with DNS_MODE=adguard): the first-run setup and the wildcard rewrite *.<domain> -> this host."""
from __future__ import annotations

from .http import Unreachable, request, wait_for
from .steps import StepError, log

STEP = "adguard"


class AdGuard:
    def __init__(self, cfg, state, *, http=request, wait=wait_for):
        self.cfg, self.state, self.http, self.wait = cfg, state, http, wait
        self.basic = (cfg.admin_user, cfg.admin_password)

    def configured(self) -> bool:
        """After the setup AdGuard serves its admin UI (port 80 in the container) and asks for the login."""
        try:
            return self.http("GET", self.cfg.adguard_url + "/control/status", basic=self.basic).status == 200
        except Unreachable:
            return False

    def run(self) -> None:
        def answers() -> bool:
            if self.configured():
                return True
            try:
                return self.http("GET", self.cfg.adguard_setup_url + "/control/install/get_addresses").ok
            except Unreachable:
                return False
        self.wait(answers, "AdGuard Home", timeout=120)
        if self.configured():
            log(STEP, "first-run setup: already done")
        else:
            r = self.http("POST", self.cfg.adguard_setup_url + "/control/install/configure", body={
                "web": {"ip": "0.0.0.0", "port": 80}, "dns": {"ip": "0.0.0.0", "port": 53},
                "username": self.cfg.admin_user, "password": self.cfg.admin_password})
            if not r.ok:
                raise StepError(f"AdGuard Home refused its setup: {r.status} {r.text[:200]}")
            self.wait(self.configured, "the AdGuard Home admin UI", timeout=60)
            log(STEP, f"first-run setup: done (administrator {self.cfg.admin_user})")
        self.ensure_rewrite()

    def ensure_rewrite(self) -> None:
        domain, answer = f"*.{self.cfg.domain}", self.cfg.host_ip
        rewrites = self.http("GET", self.cfg.adguard_url + "/control/rewrite/list", basic=self.basic).json() or []
        have = next((r for r in rewrites if r["domain"] == domain), None)
        if have and have["answer"] == answer:
            log(STEP, f"rewrite {domain} -> {answer}: already there")
            return
        if have:
            self.http("POST", self.cfg.adguard_url + "/control/rewrite/delete", body=have, basic=self.basic)
        r = self.http("POST", self.cfg.adguard_url + "/control/rewrite/add", body={"domain": domain, "answer": answer}, basic=self.basic)
        if not r.ok:
            raise StepError(f"AdGuard Home refused the rewrite: {r.status} {r.text[:200]}")
        log(STEP, f"rewrite {domain} -> {answer}: created")
