"""Forgejo: check the administrator, create the token homegraf needs, the repository with its first content, and the push webhook."""
from __future__ import annotations

import base64

from .http import Unreachable, request, wait_for
from .steps import StepError, log

STEP = "forgejo"
TOKEN_NAME = "homegraf"
OAUTH_APP = "homegraf"
# What homegraf does with the token: git over http, branches, pull requests, files of a repository.
TOKEN_SCOPES = ["write:repository", "write:issue", "read:user"]
CHUNK = 80          # files per commit when seeding (one request each)


class Forgejo:
    def __init__(self, cfg, state, *, http=request, wait=wait_for):
        self.cfg, self.state, self.http, self.wait = cfg, state, http, wait
        self.api = cfg.forgejo_url + "/api/v1"
        self.basic = (cfg.forgejo_user, cfg.admin_password)

    def _auth(self) -> dict:
        return {"Authorization": f"token {self.state.get(STEP, 'token')}"}

    def run(self, files: list[tuple[str, bytes]]) -> None:
        self.wait(lambda: self.http("GET", self.cfg.forgejo_url + "/api/healthz").ok, "Forgejo", timeout=240)
        self.check_admin()
        self.ensure_token()
        self.ensure_oauth_app()
        self.ensure_repository()
        self.seed(files)
        self.ensure_webhook()

    def check_admin(self) -> None:
        r = self.http("GET", self.api + "/user", basic=self.basic)
        if r.status == 401:
            raise StepError(f"Forgejo has no user {self.cfg.forgejo_user!r} with the administrator password: the service forgejo-admin "
                            "creates it (docker compose logs forgejo-admin); if the user exists from an earlier start, "
                            "its password differs from ADMIN_PASSWORD")
        if not r.ok:
            raise StepError(f"Forgejo answered {r.status} to the administrator's sign-in")
        log(STEP, f"administrator {self.cfg.forgejo_user}: signed in")

    def ensure_token(self) -> None:
        token = self.state.get(STEP, "token")
        if token and self.http("GET", self.api + "/user", headers=self._auth()).ok:
            log(STEP, "token for homegraf: already there")
            return
        base = f"{self.api}/users/{self.cfg.forgejo_user}/tokens"
        if any(t["name"] == TOKEN_NAME for t in (self.http("GET", base, basic=self.basic).json() or [])):
            self.http("DELETE", f"{base}/{TOKEN_NAME}", basic=self.basic)       # its value is lost: replace it
        r = self.http("POST", base, body={"name": TOKEN_NAME, "scopes": TOKEN_SCOPES}, basic=self.basic)
        if not r.ok:
            raise StepError(f"Forgejo refused to create the token: {r.status} {r.text[:200]}")
        self.state.set(STEP, "token", r.json()["sha1"])
        log(STEP, f"token for homegraf: created (scopes {', '.join(TOKEN_SCOPES)})")

    def ensure_oauth_app(self) -> None:
        """The application homegraf signs its users in through (single sign-on with this Forgejo). Forgejo shows the secret once, so
        an application whose secret is not in the state is replaced."""
        base = self.api + "/user/applications/oauth2"
        uris = [self.cfg.homegraf_callback_url]
        apps = self.http("GET", base, basic=self.basic).json() or []
        mine = [a for a in apps if a["name"] == OAUTH_APP]
        if (self.state.get(STEP, "oauth_client_secret") and any(a["client_id"] == self.state.get(STEP, "oauth_client_id") and a["redirect_uris"] == uris for a in mine)):
            log(STEP, "single sign-on application for homegraf: already there")
            return
        for a in mine:
            self.http("DELETE", f"{base}/{a['id']}", basic=self.basic)
        r = self.http("POST", base, basic=self.basic, body={"name": OAUTH_APP, "confidential_client": True, "redirect_uris": uris})
        if not r.ok:
            raise StepError(f"Forgejo refused to create the single sign-on application: {r.status} {r.text[:200]}")
        app = r.json()
        self.state.set(STEP, "oauth_client_id", app["client_id"])
        self.state.set(STEP, "oauth_client_secret", app["client_secret"])
        log(STEP, f"single sign-on application for homegraf: created (callback {uris[0]})")

    def ensure_repository(self) -> None:
        repo = self.cfg.repository
        r = self.http("GET", f"{self.api}/repos/{repo}", basic=self.basic)
        if r.status == 404:
            r = self.http("POST", f"{self.api}/user/repos", basic=self.basic,
                          body={"name": repo.split("/", 1)[1], "private": True, "default_branch": "main", "description": "Your homelab, as seen by homegraf"})
            if not r.ok:
                raise StepError(f"Forgejo refused to create the repository: {r.status} {r.text[:200]}")
            log(STEP, f"repository {repo}: created (private)")
        elif not r.ok:
            raise StepError(f"Forgejo answered {r.status} for the repository {repo}")
        else:
            log(STEP, f"repository {repo}: already there")

    def seed(self, files: list[tuple[str, bytes]]) -> None:
        repo = self.cfg.repository
        info = self.http("GET", f"{self.api}/repos/{repo}", basic=self.basic).json()
        if not info.get("empty"):
            log(STEP, "repository has content: the seed is not touched")
            return
        for i in range(0, len(files), CHUNK):
            chunk = files[i:i + CHUNK]
            r = self.http("POST", f"{self.api}/repos/{repo}/contents", basic=self.basic, timeout=120, body={
                "branch": "main", "message": "Seed from the homegraf bootstrap" + (f" ({i // CHUNK + 1})" if len(files) > CHUNK else ""),
                "author": {"name": "homegraf bootstrap", "email": self.cfg.admin_email},
                "files": [{"operation": "create", "path": path, "content": base64.b64encode(data).decode()} for path, data in chunk]})
            if not r.ok:
                raise StepError(f"Forgejo refused the seed ({r.status}): {r.text[:200]}")
        log(STEP, f"seeded {len(files)} files into {repo} (branch main)")

    def read_file(self, path: str) -> str | None:
        r = self.http("GET", f"{self.api}/repos/{self.cfg.repository}/contents/{path}?ref=main", basic=self.basic)
        return base64.b64decode(r.json().get("content") or "").decode() if r.ok and isinstance(r.json(), dict) else None

    def put_files(self, files: list[tuple[str, bytes]], message: str) -> bool:
        """One commit on main that creates or replaces these files; files that are already as given are left out. Returns whether
        there was a commit (the push webhook runs update-all)."""
        repo, ops = self.cfg.repository, []
        for path, data in files:
            r = self.http("GET", f"{self.api}/repos/{repo}/contents/{path}?ref=main", basic=self.basic)
            if r.ok and isinstance(r.json(), dict):                # an empty repository answers 200 with a list
                have = r.json()
                if base64.b64decode(have.get("content") or "") == data:
                    continue
                ops.append({"operation": "update", "path": path, "sha": have["sha"], "content": base64.b64encode(data).decode()})
            else:
                ops.append({"operation": "create", "path": path, "content": base64.b64encode(data).decode()})
        if not ops:
            return False
        r = self.http("POST", f"{self.api}/repos/{repo}/contents", basic=self.basic, timeout=120, body={
            "branch": "main", "message": message, "author": {"name": "homegraf bootstrap", "email": self.cfg.admin_email}, "files": ops})
        if not r.ok:
            raise StepError(f"Forgejo refused the commit ({r.status}): {r.text[:200]}")
        log(STEP, f"committed to main: {', '.join(o['path'] for o in ops)}")
        return True

    def ensure_webhook(self) -> None:
        repo, url = self.cfg.repository, self.cfg.webhook_url
        hooks = self.http("GET", f"{self.api}/repos/{repo}/hooks", basic=self.basic).json() or []
        if any((h.get("config") or {}).get("url") == url for h in hooks):
            log(STEP, "webhook to Komodo: already there")
            return
        r = self.http("POST", f"{self.api}/repos/{repo}/hooks", basic=self.basic, body={
            "type": "forgejo", "active": True, "events": ["push"], "branch_filter": "main",
            "config": {"url": url, "content_type": "json", "secret": self.cfg.webhook_secret}})
        if not r.ok:
            raise StepError(f"Forgejo refused the webhook: {r.status} {r.text[:200]}")
        log(STEP, "webhook to Komodo: created (push to main runs the procedure update-all)")
