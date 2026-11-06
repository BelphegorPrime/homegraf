"""Komodo: an API key for homegraf, the git account for Forgejo, the repository resource (cloned), and the procedure update-all."""
from __future__ import annotations

import time
from urllib.parse import urlparse

from .http import Unreachable, request, wait_for
from .steps import StepError, log

STEP = "komodo"
KEY_NAME = "homegraf"
PROVISIONER_KEY_NAME = "stack-provisioner"
REPO_NAME = "infrastructure"
PROCEDURE = "update-all"
INGRESS_STACK = "ingressctl"            # builds from infra/ingress-controller and runs ingressctl once per call
INGRESS_PROCEDURE = "ingress-apply"
INGRESS_SYNC = "ingress-sync"
INGRESS_DIR = "infra/ingress-controller"
# The compose file of the seed that connects ingressctl to this stack's network and credentials; relative to INGRESS_DIR
INGRESS_COMPOSE = ["compose.yaml", "../../inventory/ingressctl.compose.yaml"]


def update_all_stages() -> list[dict]:
    """Pull the repository, then deploy every stack whose compose or env files changed. homegraf adds a stage "Build" after the
    deploy stage for stacks that build their own image (the tools look for BatchDeployStackIfChanged)."""
    return [
        {"name": "Pull", "enabled": True, "executions": [{"execution": {"type": "PullRepo", "params": {"repo": REPO_NAME}}, "enabled": True}]},
        {"name": "Deploy", "enabled": True, "executions": [{"execution": {"type": "BatchDeployStackIfChanged", "params": {"pattern": "*"}}, "enabled": True}]},
    ]


class Komodo:
    def __init__(self, cfg, state, *, http=request, wait=wait_for, sleep=time.sleep):
        self.cfg, self.state, self.http, self.wait, self.sleep = cfg, state, http, wait, sleep
        self.jwt = ""
        self.server_id = ""
        self.server_name = ""
        self.git_domain = urlparse(cfg.forgejo_url).netloc          # forgejo:3000, as the agent reaches it

    # -- calls ---------------------------------------------------------------------------------------------------------
    def call(self, kind: str, typ: str, body: dict | None = None, *, auth: dict | None = None):
        r = self.http("POST", f"{self.cfg.komodo_url}/{kind}/{typ}", body=body or {}, headers=auth or {"Authorization": self.jwt}, timeout=60)
        if not r.ok:
            raise StepError(f"Komodo {kind}/{typ}: {r.status} {r.text[:200]}")
        return r.json()

    def finish(self, update: dict, what: str, *, seconds: int = 120) -> dict:
        """Waits for an execution to complete; raises with the failing stage's last lines. Returns the finished update."""
        uid = update["_id"]["$oid"]
        for _ in range(seconds):
            u = self.call("read", "GetUpdate", {"id": uid})
            if u.get("status") == "Complete":
                if u.get("success"):
                    return u
                bad = next((log_ for log_ in u.get("logs", []) if not log_.get("success")), {})
                raise StepError(f"Komodo: {what} failed at {bad.get('stage', '?')}: {(bad.get('stderr') or bad.get('stdout') or '')[-300:].strip()}")
            self.sleep(1)
        raise StepError(f"Komodo: {what} did not finish within {seconds // 60} minutes")

    # -- steps ---------------------------------------------------------------------------------------------------------
    def run(self, forgejo_token: str) -> str:
        """Returns the name of the server this host is registered as."""
        self.wait(lambda: self.http("GET", self.cfg.komodo_url + "/version").ok, "Komodo", timeout=240)
        self.login()
        self.ensure_api_key()
        server_id, server = self.server()
        self.ensure_git_account(forgejo_token)
        self.ensure_repository(server_id)
        self.ensure_procedure()
        return server

    def connect(self) -> None:
        """Sign in and find the server, for a step that runs apart from run() (the certificates, after homegraf is up)."""
        self.wait(lambda: self.http("GET", self.cfg.komodo_url + "/version").ok, "Komodo", timeout=240)
        self.login()
        self.server()

    def login(self) -> None:
        r = self.http("POST", self.cfg.komodo_url + "/auth/login/LoginLocalUser", body={"username": self.cfg.admin_user, "password": self.cfg.admin_password})
        if not r.ok:
            raise StepError(f"Komodo refused the administrator {self.cfg.admin_user!r} ({r.status}): the account is created from ADMIN_USER and "
                            "ADMIN_PASSWORD on the very first start only; if Komodo ran before with other values, sign in and fix it there")
        self.jwt = r.json()["data"]["jwt"]
        log(STEP, f"administrator {self.cfg.admin_user}: signed in")

    def ensure_api_key(self) -> None:
        self.ensure_key(KEY_NAME, "api_key", "api_secret")

    def ensure_provisioner_key(self) -> None:
        """A key of its own for the reconcile stage of update-all (infra/secrets/provisioner.env), so it can be revoked on its own."""
        self.ensure_key(PROVISIONER_KEY_NAME, "provisioner_key", "provisioner_secret")

    def ensure_key(self, name: str, key_field: str, secret_field: str) -> None:
        key, secret = self.state.get(STEP, key_field), self.state.get(STEP, secret_field)
        if key and secret and self.http("POST", self.cfg.komodo_url + "/read/GetVersion", body={}, headers={"X-Api-Key": key, "X-Api-Secret": secret}).ok:
            log(STEP, f"API key {name}: already there")
            return
        for k in self.call("read", "ListApiKeys") or []:
            if k.get("name") == name:       # Komodo shows the secret once; the one of an interrupted run was never stored
                self.http("POST", self.cfg.komodo_url + "/auth/manage/DeleteApiKey", body={"key": k["key"]}, headers={"Authorization": self.jwt})
        r = self.http("POST", self.cfg.komodo_url + "/auth/manage/CreateApiKey", body={"name": name}, headers={"Authorization": self.jwt})
        if not r.ok:
            raise StepError(f"Komodo refused to create the API key {name}: {r.status} {r.text[:200]}")
        self.state.set(STEP, key_field, r.json()["key"])
        self.state.set(STEP, secret_field, r.json()["secret"])
        log(STEP, f"API key {name}: created")

    def server(self) -> tuple[str, str]:
        def ready() -> bool:
            servers = self.call("read", "ListServers")
            return bool(servers) and servers[0]["info"]["state"] == "Ok"
        try:
            self.wait(ready, "the Komodo agent (periphery)", timeout=180)
        except TimeoutError as e:
            raise StepError(f"{e}: check docker compose logs komodo-periphery") from None
        s = self.call("read", "ListServers")[0]
        log(STEP, f"server {s['name']}: Ok")
        self.server_id, self.server_name = s["id"], s["name"]
        return s["id"], s["name"]

    def ensure_git_account(self, token: str, domain: str | None = None) -> None:
        """The account the agents clone with; `domain` is Forgejo as that agent reaches it (the agent here: forgejo:3000)."""
        user, domain = self.cfg.forgejo_user, domain or self.git_domain
        have = next((a for a in self.call("read", "ListGitProviderAccounts") or [] if a["domain"] == domain and a["username"] == user), None)
        if have and have.get("token") == token:
            log(STEP, f"git account {user}@{domain}: already there")
        elif have:
            self.call("write", "UpdateGitProviderAccount", {"id": have["_id"]["$oid"], "account": {"token": token}})
            log(STEP, f"git account {user}@{domain}: token updated")
        else:
            self.call("write", "CreateGitProviderAccount", {"account": {"domain": domain, "https": False, "username": user, "token": token}})
            log(STEP, f"git account {user}@{domain}: created")

    def ensure_repository(self, server_id: str) -> None:
        wanted = {"server_id": server_id, "git_provider": self.git_domain, "git_https": False, "git_account": self.cfg.forgejo_user,
                  "repo": self.cfg.repository, "branch": "main"}
        have = next((r for r in self.call("read", "ListRepos") if r["name"] == REPO_NAME), None)
        if have is None:
            self.call("write", "CreateRepo", {"name": REPO_NAME, "config": wanted})
            log(STEP, f"repository resource {REPO_NAME}: created")
        else:
            current = self.call("read", "GetRepo", {"repo": REPO_NAME})
            if any(current["config"].get(k) != v for k, v in wanted.items()):
                self.call("write", "UpdateRepo", {"id": have["id"], "config": wanted})
                log(STEP, f"repository resource {REPO_NAME}: updated")
            else:
                log(STEP, f"repository resource {REPO_NAME}: already there")
        if not self.call("read", "GetRepo", {"repo": REPO_NAME})["info"].get("latest_hash"):
            self.finish(self.call("execute", "CloneRepo", {"repo": REPO_NAME}), "cloning the repository")
            log(STEP, f"repository cloned to {self.cfg.repos_dir}/{REPO_NAME}")

    def ensure_procedure(self) -> None:
        if any(p["name"] == PROCEDURE for p in self.call("read", "ListProcedures")):
            log(STEP, f"procedure {PROCEDURE}: already there (left as it is)")
            return
        self.call("write", "CreateProcedure", {"name": PROCEDURE, "config": {"stages": update_all_stages(), "webhook_enabled": True}})
        log(STEP, f"procedure {PROCEDURE}: created (a push to main pulls the repository and deploys what changed)")

    # -- ingressctl: certificates for the proxy hosts ----------------------------------------------------------------------
    def ensure_ingress(self) -> None:
        """The Komodo stack ingressctl and the procedure ingress-apply (deploys the stack, then applies every ingress.yaml); a push
        runs the procedure as the last stage of update-all."""
        self.finish(self.call("execute", "PullRepo", {"repo": REPO_NAME}), "pulling the repository")
        if any(s["name"] == INGRESS_STACK for s in self.call("read", "ListStacks")):
            log(STEP, f"stack {INGRESS_STACK}: already there")
        else:
            self.call("write", "CreateStack", {"name": INGRESS_STACK, "config": {
                "server_id": self.server_id, "files_on_host": True, "run_directory": f"{self.cfg.repos_dir}/{REPO_NAME}/{INGRESS_DIR}",
                "file_paths": INGRESS_COMPOSE, "run_build": True, "auto_pull": False, "webhook_enabled": False}})
            self.call("write", "RefreshStackCache", {"stack": INGRESS_STACK})
            log(STEP, f"stack {INGRESS_STACK}: created (builds ingressctl from {INGRESS_DIR})")
        if any(p["name"] == INGRESS_PROCEDURE for p in self.call("read", "ListProcedures")):
            log(STEP, f"procedure {INGRESS_PROCEDURE}: already there")
        else:
            self.call("write", "CreateProcedure", {"name": INGRESS_PROCEDURE, "config": {"stages": [
                {"name": "controller", "enabled": True, "executions": [{"execution": {"type": "DeployStack", "params": {"stack": INGRESS_STACK}}, "enabled": True}]},
                {"name": "apply", "enabled": True, "executions": [{"execution": {"type": "RunStackService", "params": {
                    "stack": INGRESS_STACK, "service": "ingressctl", "command": ["apply", "--root", "/repo", "--all"], "no_deps": True}}, "enabled": True}]}]}})
            log(STEP, f"procedure {INGRESS_PROCEDURE}: created")

    def deploy_ingress(self) -> None:
        """Builds the ingressctl image (the first time takes minutes) and runs `status` once."""
        self.finish(self.call("execute", "DeployStack", {"stack": INGRESS_STACK}), "building ingressctl", seconds=1200)

    def run_ingress(self, argv: list[str]) -> tuple[bool, str]:
        """(ok, output) of one ingressctl call. Komodo calls a run successful whatever the exit code, so the output decides, as in
        infra/komodo/run_ingressctl.py."""
        u = self.finish(self.call("execute", "RunStackService", {"stack": INGRESS_STACK, "service": "ingressctl", "command": argv,
                                                                  "no_deps": True, "no_tty": True}), f"ingressctl {argv[0]}", seconds=900)
        out = "".join((lg.get("stdout") or "") + (lg.get("stderr") or "") for lg in u.get("logs") or [])
        return not ('"result": "error"' in out or "BLOCKER" in out or "FEHLER" in out), out

    def ensure_ingress_sync(self) -> None:
        """A procedure that only applies the ingress.yaml files, every 15 minutes. A failing stack deploy stops update-all before its
        last stage (ingress-apply), so the proxy hosts and certificates would wait for that stack; this keeps them converging.
        No alert: an upstream that is not up yet is an expected state, ingress-apply reports real errors."""
        if not any(s["name"] == INGRESS_STACK for s in self.call("read", "ListStacks")):
            log(STEP, f"stack {INGRESS_STACK} does not exist yet: the procedure {INGRESS_SYNC} waits for it")
            return
        if any(p["name"] == INGRESS_SYNC for p in self.call("read", "ListProcedures")):
            log(STEP, f"procedure {INGRESS_SYNC}: already there")
            return
        self.call("write", "CreateProcedure", {"name": INGRESS_SYNC, "config": {
            "stages": [{"name": "apply", "enabled": True, "executions": [{"execution": {"type": "RunStackService", "params": {
                "stack": INGRESS_STACK, "service": "ingressctl", "command": ["apply", "--root", "/repo", "--all"], "no_deps": True}}, "enabled": True}]}],
            "schedule_format": "Cron", "schedule": "0 7,22,37,52 * * * *", "schedule_timezone": "Etc/UTC", "schedule_enabled": True,
            "schedule_alert": False, "failure_alert": False, "webhook_enabled": False}})
        log(STEP, f"procedure {INGRESS_SYNC}: created (applies the ingress.yaml files every 15 minutes, whatever update-all did)")

    def reconcile_in_update_all(self) -> None:
        """update-all gets a stage right after the pull that sets up the stacks which are only a folder with an ingress.yaml
        (the service reconcile of the stack ingressctl, before the stage that deploys what changed), unless it has one."""
        if not any(s["name"] == INGRESS_STACK for s in self.call("read", "ListStacks")):
            log(STEP, f"stack {INGRESS_STACK} does not exist yet: the reconcile stage of {PROCEDURE} waits for it")
            return
        current = self.call("read", "GetProcedure", {"procedure": PROCEDURE})
        stages = current["config"]["stages"]
        if any(e["execution"].get("type") == "RunStackService" and e["execution"]["params"].get("service") == "reconcile"
               for s in stages for e in s.get("executions", [])):
            log(STEP, f"procedure {PROCEDURE}: already has the reconcile stage")
            return
        stage = {"name": "Reconcile", "enabled": True, "executions": [{"execution": {"type": "RunStackService", "params": {
            "stack": INGRESS_STACK, "service": "reconcile", "command": ["--apply"], "no_deps": True}}, "enabled": True}]}
        pulls = [i for i, s in enumerate(stages) if any(e["execution"].get("type") in ("PullRepo", "BatchPullRepo") for e in s.get("executions", []))]
        stages = stages[:pulls[0] + 1] + [stage] + stages[pulls[0] + 1:] if pulls else [stage] + stages
        self.call("write", "UpdateProcedure", {"id": current["_id"]["$oid"], "config": {"stages": stages}})
        log(STEP, f"procedure {PROCEDURE}: a push now sets up new apps (stacks that are only a folder) before it deploys")

    def apply_ingress_in_update_all(self) -> None:
        """update-all gets a last stage that runs ingress-apply, unless it has one."""
        current = self.call("read", "GetProcedure", {"procedure": PROCEDURE})
        stages = current["config"]["stages"]
        if any(e["execution"].get("type") == "RunProcedure" and e["execution"]["params"].get("procedure") == INGRESS_PROCEDURE
               for s in stages for e in s.get("executions", [])):
            log(STEP, f"procedure {PROCEDURE}: already ends with {INGRESS_PROCEDURE}")
            return
        stages = stages + [{"name": "Ingress", "enabled": True, "executions": [
            {"execution": {"type": "RunProcedure", "params": {"procedure": INGRESS_PROCEDURE}}, "enabled": True}]}]
        self.call("write", "UpdateProcedure", {"id": current["_id"]["$oid"], "config": {"stages": stages}})
        log(STEP, f"procedure {PROCEDURE}: a push now ends with {INGRESS_PROCEDURE} (certificates and proxy hosts follow the repository)")
