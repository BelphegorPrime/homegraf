"""Minimal Forgejo client for the pull-request mode of the homelab tool (new-app --pr).

Access: FORGEJO_TOKEN (needs write access to the repository and pull requests). The server and repository come from
the `origin` remote; FORGEJO_URL overrides the server, FORGEJO_CA_BUNDLE adds a CA file for a private certificate.
"""
from __future__ import annotations

import base64
import json
import os
import re
import ssl
import urllib.error
import urllib.request


def parse_remote(url: str) -> tuple[str, str, str]:
    """(scheme, host, owner/repo) of an ssh, scp-style or http(s) remote URL."""
    m = re.match(r"^(?P<scheme>ssh|https?|git)://(?:[^@/]+@)?(?P<host>[^/:]+)(?::\d+)?/(?P<repo>.+?)(?:\.git)?/?$", url) \
        or re.match(r"^(?:[^@/]+@)?(?P<host>[^/:]+):(?P<repo>[^/].*?)(?:\.git)?/?$", url)
    if not m:
        raise SystemExit(f"cannot read the repository from the remote URL {url!r}")
    return m.groupdict().get("scheme") or "ssh", m["host"], m["repo"]


class Forgejo:
    def __init__(self, base: str, repo: str, token: str, cafile: str | None = None) -> None:
        self.base, self.repo, self.token = base.rstrip("/"), repo, token
        self._ctx = ssl.create_default_context(cafile=cafile) if cafile else None

    @classmethod
    def from_env(cls, remote_url: str) -> "Forgejo":
        token = os.environ.get("FORGEJO_TOKEN")
        if not token:
            raise SystemExit("--pr: FORGEJO_TOKEN is not set (token with write access to the repository and its pull requests)")
        _, host, repo = parse_remote(remote_url)
        return cls(os.environ.get("FORGEJO_URL") or f"https://{host}", repo, token, os.environ.get("FORGEJO_CA_BUNDLE"))

    def _call(self, method: str, path: str, body: dict | None = None):
        req = urllib.request.Request(
            f"{self.base}/api/v1{path}", method=method, data=None if body is None else json.dumps(body).encode(),
            headers={"Authorization": f"token {self.token}", "Content-Type": "application/json", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30, context=self._ctx) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            raise SystemExit(f"Forgejo {method} {path}: HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}") from None
        except (urllib.error.URLError, OSError) as e:
            raise SystemExit(f"Forgejo {self.base} not reachable: {e}") from None

    def login(self) -> str:
        return self._call("GET", "/user")["login"]

    def find_pull(self, branch: str, base: str = "main") -> dict | None:
        """The open pull request from branch into base, if there is one."""
        pulls = self._call("GET", f"/repos/{self.repo}/pulls?state=open&limit=50")
        return next((p for p in pulls if p["head"]["ref"] == branch and p["base"]["ref"] == base), None)

    def create_pull(self, branch: str, title: str, body: str, base: str = "main") -> dict:
        return self._call("POST", f"/repos/{self.repo}/pulls", {"head": branch, "base": base, "title": title, "body": body})

    def git_env(self, remote_url: str) -> dict[str, str]:
        """Environment that lets `git push` authenticate when the remote is http(s); empty for ssh remotes.
        Set through GIT_CONFIG_* so the token never appears on a command line."""
        if parse_remote(remote_url)[0] not in ("http", "https"):
            return {}
        basic = base64.b64encode(f"{self.login()}:{self.token}".encode()).decode()
        env = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.extraHeader",
               "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}"}
        if self._ctx and os.environ.get("FORGEJO_CA_BUNDLE"):
            env.update(GIT_CONFIG_COUNT="2", GIT_CONFIG_KEY_1="http.sslCAInfo", GIT_CONFIG_VALUE_1=os.environ["FORGEJO_CA_BUNDLE"])
        return env
