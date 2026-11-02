"""Renovate, the way a Forgejo does it without a Renovate server: a user renovate-bot, a repository renovate-bot/configure with a workflow
that runs Renovate on the Actions runner twice a day, and autodiscover: Renovate looks at every repository the bot is a collaborator of.
The repository of the stack gets the bot as collaborator, so Renovate opens its onboarding pull request there.

Needs the profile renovate (which starts the Actions runner). What gets in: the token of the bot as the secret RENOVATE_TOKEN of its repository.
"""
from __future__ import annotations

import base64
import secrets

from .http import request
from .steps import StepError, log

STEP = "renovate"
BOT = "renovate-bot"
REPO = "configure"
SECRET = "RENOVATE_TOKEN"
TOKEN_NAME = "renovate"
# What Renovate does on Forgejo (platform gitea): repositories and issues/pull requests to write, users and organisations to read.
TOKEN_SCOPES = ["write:repository", "write:issue", "read:user", "read:organization"]
IMAGE = "ghcr.io/renovatebot/renovate:43.220.0"        # the -full image adds the tools of every package manager (several GB); name it here for those


def workflow() -> str:
    return f"""name: renovate

on:
  workflow_dispatch:
  schedule:
    - cron: "0 22,10 * * *"
  push:
    branches:
      - main

jobs:
  renovate:
    runs-on: docker
    container: {IMAGE}
    steps:
      - uses: actions/checkout@v4
      - run: renovate
        env:
          RENOVATE_CONFIG_FILE: ${{{{ github.workspace }}}}/config.js
          RENOVATE_TOKEN: ${{{{ secrets.{SECRET} }}}}
"""


def config_js(endpoint: str, email: str) -> str:
    return f"""// Renovate for this Forgejo: every repository the user {BOT} is a collaborator of is looked after.
// The endpoint is the address of this host: the job runs in its own Docker daemon, which does not see the containers' network.
module.exports = {{
\tplatform: 'gitea',
\tendpoint: '{endpoint}',
\tgitUrl: 'endpoint',
\tgitAuthor: 'Renovate Bot <{email}>',
\tusername: '{BOT}',
\tautodiscover: true,
\tonboardingConfig: {{
\t\t$schema: 'https://docs.renovatebot.com/renovate-schema.json',
\t\textends: ['config:recommended'],
\t}},
\toptimizeForDisabled: true,
\tpersistRepoData: true,
}};
"""


RENOVATE_JSON = """{
  "$schema": "https://docs.renovatebot.com/renovate-schema.json",
  "extends": ["config:recommended", ":semanticCommits"]
}
"""


class Renovate:
    def __init__(self, cfg, state, forgejo):
        self.cfg, self.state, self.forgejo = cfg, state, forgejo
        self.api, self.basic, self.http = forgejo.api, forgejo.basic, forgejo.http
        self.email = f"{BOT}@{cfg.domain}"

    def call(self, method: str, path: str, body=None, *, auth=None):
        r = self.http(method, self.api + path, body=body, basic=auth or self.basic)
        if not r.ok:
            raise StepError(f"Forgejo {method} {path}: {r.status} {r.text[:200]}")
        return r

    def run(self) -> None:
        password = self.ensure_user()
        token = self.ensure_token(password)
        self.ensure_repository()
        self.ensure_secret(token)
        self.ensure_files()
        self.ensure_collaborator()

    def ensure_user(self) -> str:
        password = self.state.get(STEP, "password") or secrets.token_urlsafe(24)
        self.state.set(STEP, "password", password)
        if self.http("GET", f"{self.api}/users/{BOT}", basic=self.basic).ok:
            log(STEP, f"user {BOT}: already there")
            return password
        self.call("POST", "/admin/users", {"username": BOT, "email": self.email, "password": password, "must_change_password": False,
                                           "send_notify": False, "visibility": "private"})
        log(STEP, f"user {BOT}: created")
        return password

    def ensure_token(self, password: str) -> str:
        token = self.state.get(STEP, "token")
        bot = (BOT, password)
        if token and self.http("GET", self.api + "/user", headers={"Authorization": f"token {token}"}).ok:
            log(STEP, "token of the bot: already there")
            return token
        base = f"/users/{BOT}/tokens"
        if any(t["name"] == TOKEN_NAME for t in self.call("GET", base, auth=bot).json() or []):
            self.call("DELETE", f"{base}/{TOKEN_NAME}", auth=bot)          # its value is lost: replace it
        token = self.call("POST", base, {"name": TOKEN_NAME, "scopes": TOKEN_SCOPES}, auth=bot).json()["sha1"]
        self.state.set(STEP, "token", token)
        log(STEP, f"token of the bot: created (scopes {', '.join(TOKEN_SCOPES)})")
        return token

    def ensure_repository(self) -> None:
        if self.http("GET", f"{self.api}/repos/{BOT}/{REPO}", basic=self.basic).ok:
            log(STEP, f"repository {BOT}/{REPO}: already there")
            return
        self.call("POST", f"/admin/users/{BOT}/repos", {"name": REPO, "private": True, "default_branch": "main",
                                                        "description": "Renovate for this Forgejo (homegraf)"})
        log(STEP, f"repository {BOT}/{REPO}: created (private)")

    def ensure_secret(self, token: str) -> None:
        """The workflow's secret. Set before the files are committed: their push already starts a run."""
        self.call("PUT", f"/repos/{BOT}/{REPO}/actions/secrets/{SECRET}", {"data": token})
        log(STEP, f"secret {SECRET} of {BOT}/{REPO}: set")

    def ensure_files(self) -> None:
        endpoint = f"http://{self.cfg.host_ip}:{self.cfg.ports['FORGEJO']}"
        wanted = [(".forgejo/workflows/renovate.yml", workflow()), ("config.js", config_js(endpoint, self.email)), ("renovate.json", RENOVATE_JSON)]
        ops = []
        for path, text in wanted:
            have = self.http("GET", f"{self.api}/repos/{BOT}/{REPO}/contents/{path}?ref=main", basic=self.basic)
            data = text.encode()
            if have.ok and isinstance(have.json(), dict):          # an empty repository answers 200 with a list
                if base64.b64decode(have.json().get("content") or "") != data:
                    ops.append({"operation": "update", "path": path, "sha": have.json()["sha"], "content": base64.b64encode(data).decode()})
            else:
                ops.append({"operation": "create", "path": path, "content": base64.b64encode(data).decode()})
        if not ops:
            log(STEP, "workflow and configuration: up to date")
            return
        self.call("POST", f"/repos/{BOT}/{REPO}/contents", {"branch": "main", "message": "Renovate for this Forgejo (homegraf bootstrap)", "files": ops,
                                                          "author": {"name": "homegraf bootstrap", "email": self.cfg.admin_email}})
        log(STEP, f"committed: {', '.join(o['path'] for o in ops)} (the push runs Renovate once)")

    def ensure_collaborator(self) -> None:
        repo = self.cfg.repository
        self.call("PUT", f"/repos/{repo}/collaborators/{BOT}", {"permission": "write"})
        log(STEP, f"{BOT} is a collaborator of {repo}: Renovate looks after it (an onboarding pull request opens there)")
