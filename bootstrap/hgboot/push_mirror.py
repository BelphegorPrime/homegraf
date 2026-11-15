"""The optional push mirror to GitHub, guarded and reconciled in place.

Forgejo owns the writable repository; GitHub is only a target. The helper never
creates, replaces or migrates the source repository, never adds a pull mirror and
never deletes a ref. Before the first write it compares every branch page on both
sides and refuses when GitHub holds a branch the source does not (a push mirror
would remove it) or when a shared branch has diverged from or is ahead of the
source (syncing would rewrite history). A missing token, divergence and an API
error are failures, not reasons to guess.

A push mirror is asynchronous, so GitHub lagging behind on a shared branch is
normal: GitHub's compare endpoint decides, and when GitHub does not know the
newer commit yet Forgejo's compare confirms the older commit is an ancestor.
Only a branch GitHub is ahead of or has diverged from refuses. A lagging branch
is brought up to date by the sync that follows.

A drifted mirror is repaired by deleting it and creating it again, because
Forgejo's API has no way to update a push mirror in place.

The GitHub token is sent only to the GitHub API and, as the remote password, to
the Forgejo push mirror credential field. It never appears in a remote address,
a log line or an error: everything echoed back by a service is cleaned first,
and an authenticated request never follows a redirect to another origin. The
caller supplies the operator's token; Forgejo itself enforces who may manage a
mirror, so the helper does not claim to check the caller's rights.
"""
from __future__ import annotations

import datetime
import re
import time
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

from .http import request
from .steps import StepError, log

STEP = "push-mirror"
INTERVAL = "8h0m0s"
# The loopback tests send "git"; no live mirror was contacted, so the GitHub
# account name the operator's instance expects is not verified here.
REMOTE_USER = "git"
PER_PAGE = 100
MAX_PAGES = 50
LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})
REPO_NAME = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
CREDENTIALS = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*://)[^/\s@]+@")


@dataclass(frozen=True)
class MirrorResult:
    """What a run of the reconciler found and changed, backed by the branch data it verified."""

    created: bool
    repaired: bool
    synced: bool
    branches: tuple[str, ...]
    default_branch: str
    default_sha: str


class PushMirror:
    """Reconcile one Forgejo push mirror to one GitHub repository, if the token is configured."""

    def __init__(self, *, forgejo_url: str, forgejo_repo: str, forgejo_token: str,
                 github_repo: str, github_token: str, github_url: str = "https://github.com",
                 github_api: str = "https://api.github.com", http=request, sleep=time.sleep,
                 attempts: int = 30, interval: float = 2.0):
        if not github_token or not github_token.strip():
            raise StepError("the GitHub token is not set: the optional GitHub push mirror stays off")
        if not forgejo_token or not forgejo_token.strip():
            raise StepError("the Forgejo token is not set: it is needed to reconcile the GitHub push mirror")
        if attempts < 1:
            raise StepError("the number of sync checks must be at least 1")
        self.forgejo_repo = self._repo(forgejo_repo, "Forgejo")
        self.github_repo = self._repo(github_repo, "GitHub")
        self.forgejo_url = self._origin(forgejo_url, "Forgejo")
        self.github_url = self._origin(github_url, "GitHub")
        self.github_api = self._github_api(github_api)
        self.forgejo_token, self.github_token = forgejo_token, github_token
        self.forgejo_api = self.forgejo_url + "/api/v1"
        self.remote_address = f"{self.github_url}/{self.github_repo}.git"
        self.http, self.sleep, self.attempts, self.interval = http, sleep, attempts, interval

    def sync(self) -> MirrorResult:
        """Guard, reconcile the mirror only on drift, sync a new or lagging mirror, then verify it."""
        source, target, lagging = self._guard()
        branch = self._default_branch()
        mirrors = [m for m in self._mirrors() if self._same_remote(m.get("remote_address"))]
        if len(mirrors) > 1:
            raise StepError(f"several Forgejo push mirrors point at {self.github_repo}: refusing to choose one")
        mirror = mirrors[0] if mirrors else None
        created = repaired = synced = False
        if mirror is None:
            mirror = self._create()
            created = True
        elif self._drifted(mirror):
            mirror = self._replace(mirror)
            repaired = True
        if created or self._never_synced(mirror) or set(target) != set(source) or lagging:
            self._sync_now(mirror)
            synced = True
            source, target = self._read()
        self._verify(source, target, branch)
        log(STEP, f"{self.github_repo}: {'created' if created else 'repaired' if repaired else 'mirror already converged'}"
                  f"{', synced' if synced else ''} ({len(source)} branches, {branch} {source[branch][:8]})")
        return MirrorResult(created, repaired, synced, tuple(sorted(source)), branch, source[branch])

    # -- Forgejo side -------------------------------------------------------------------------------------------------
    def _call(self, method: str, url: str, **kwargs):
        """A request that carries a token must not follow a redirect with that token to another origin."""
        return self.http(method, url, redirect=False, **kwargs)

    def _forgejo_headers(self) -> dict:
        return {"Authorization": f"token {self.forgejo_token}"}

    def _default_branch(self) -> str:
        r = self._call("GET", f"{self.forgejo_api}/repos/{self.forgejo_repo}", headers=self._forgejo_headers())
        if not r.ok:
            raise StepError(f"Forgejo answered {r.status} when reading {self.forgejo_repo}: {self._clean(r.text)}")
        data = r.json()
        branch = data.get("default_branch") if isinstance(data, dict) else None
        if not isinstance(branch, str) or not branch:
            raise StepError(f"Forgejo returned no default branch for {self.forgejo_repo}: "
                            "cannot verify the GitHub push mirror")
        return branch

    def _mirrors(self) -> list[dict]:
        r = self._call("GET", f"{self.forgejo_api}/repos/{self.forgejo_repo}/push_mirrors", headers=self._forgejo_headers())
        if not r.ok:
            raise StepError(f"Forgejo answered {r.status} when reading the push mirrors of {self.forgejo_repo}: "
                            f"{self._clean(r.text)}")
        mirrors = r.json()
        if not isinstance(mirrors, list):
            raise StepError(f"Forgejo returned no push mirror list for {self.forgejo_repo}")
        return mirrors

    def _create(self, *, repaired: bool = False) -> dict:
        r = self._call("POST", f"{self.forgejo_api}/repos/{self.forgejo_repo}/push_mirrors", headers=self._forgejo_headers(), body={
            "remote_address": self.remote_address, "remote_username": REMOTE_USER,
            "remote_password": self.github_token, "sync_on_commit": True, "interval": INTERVAL})
        if not r.ok:
            raise StepError(f"Forgejo refused the push mirror to {self.github_repo}: {r.status} {self._clean(r.text)}")
        mirror = r.json() if r.body else None
        if not isinstance(mirror, dict) or not mirror:
            mirror = next((m for m in self._mirrors() if self._same_remote(m.get("remote_address"))), {})
        if "last_update" not in mirror:
            mirror = self._current() or mirror
        log(STEP, f"{self.github_repo}: push mirror {'repaired' if repaired else 'created'}")
        return mirror

    def _replace(self, mirror: dict) -> dict:
        name = mirror.get("remote_name") or mirror.get("name") or "github"
        r = self._call("DELETE", f"{self.forgejo_api}/repos/{self.forgejo_repo}/push_mirrors/{quote(str(name), safe='')}",
                      headers=self._forgejo_headers())
        if not r.ok:
            raise StepError(f"Forgejo refused to remove the drifted push mirror to {self.github_repo}: "
                            f"{r.status} {self._clean(r.text)}")
        return self._create(repaired=True)

    def _current(self) -> dict | None:
        return next((m for m in self._mirrors() if self._same_remote(m.get("remote_address"))), None)

    def _drifted(self, mirror: dict) -> bool:
        return mirror.get("sync_on_commit") is not True or mirror.get("interval") != INTERVAL

    @staticmethod
    def _never_synced(mirror: dict) -> bool:
        value = mirror.get("last_update")
        if not value:
            return True
        try:
            when = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return False
        if when.tzinfo is None:
            when = when.replace(tzinfo=datetime.timezone.utc)
        return when.timestamp() <= 0

    def _sync_now(self, mirror: dict) -> None:
        before = mirror.get("last_update")
        r = self._call("POST", f"{self.forgejo_api}/repos/{self.forgejo_repo}/push_mirrors-sync", headers=self._forgejo_headers())
        if not r.ok:
            raise StepError(f"Forgejo refused to sync the push mirror to {self.github_repo}: "
                            f"{r.status} {self._clean(r.text)}")
        for attempt in range(self.attempts):
            current = self._current()
            if current is None:
                raise StepError(f"the push mirror to {self.github_repo} disappeared during its first sync")
            if current.get("last_error"):
                raise StepError(f"the push mirror to {self.github_repo} failed: {self._clean(str(current['last_error']))}")
            if current.get("last_update") and current.get("last_update") != before:
                return
            if attempt + 1 < self.attempts:
                self.sleep(self.interval)
        raise StepError(f"the push mirror to {self.github_repo} did not report a finished sync "
                        f"within {self.attempts} checks")

    # -- branches -----------------------------------------------------------------------------------------------------
    def _github_headers(self) -> dict:
        return {"Authorization": f"Bearer {self.github_token}", "X-GitHub-Api-Version": "2022-11-28"}

    def _read(self) -> tuple[dict[str, str], dict[str, str]]:
        source = self._branches(self.forgejo_api, self.forgejo_repo, "limit", self._forgejo_headers(), "Forgejo")
        target = self._branches(self.github_api, self.github_repo, "per_page", self._github_headers(), "GitHub")
        return source, target

    def _guard(self) -> tuple[dict[str, str], dict[str, str], frozenset[str]]:
        source, target = self._read()
        extra = sorted(set(target) - set(source))
        if extra:
            raise StepError(f"GitHub has branches that {self.forgejo_repo} does not: {', '.join(extra)}; "
                            "refusing to mirror (the sync would delete or overwrite them)")
        lagging: set[str] = set()
        diverged: list[str] = []
        for name in sorted(source):
            if name not in target or source[name] == target[name]:
                continue
            status = self._compare(target[name], source[name])
            if status is None:
                if self._forgejo_lagging(target[name], source[name], name):
                    lagging.add(name)
                else:
                    diverged.append(name)
            elif status in ("identical", "ahead"):
                lagging.add(name)
            else:
                diverged.append(name)
        if diverged:
            raise StepError(f"GitHub is ahead of or has diverged from {self.forgejo_repo} on: {', '.join(diverged)}; "
                            "refusing to force-push history")
        return source, target, frozenset(lagging)

    def _compare(self, base: str, head: str) -> str | None:
        """GitHub's relation of head to base, or None when GitHub does not know the newer commit yet."""
        r = self._call("GET", f"{self.github_api}/repos/{self.github_repo}/compare/"
                              f"{quote(base, safe='')}...{quote(head, safe='')}", headers=self._github_headers())
        if r.status == 404:
            return None
        if not r.ok:
            raise StepError(f"GitHub answered {r.status} when comparing commits of {self.github_repo}: "
                            f"{self._clean(r.text)}")
        data = r.json()
        status = data.get("status") if isinstance(data, dict) else None
        if status not in ("identical", "ahead", "behind", "diverged"):
            raise StepError(f"GitHub returned no comparison status for {self.github_repo}")
        return status

    def _forgejo_compare(self, base: str, head: str) -> int | None:
        """Commits Forgejo has in head but not base, or None when it does not know a commit."""
        r = self._call("GET", f"{self.forgejo_api}/repos/{self.forgejo_repo}/compare/"
                              f"{quote(base, safe='')}...{quote(head, safe='')}", headers=self._forgejo_headers())
        if r.status == 404:
            return None
        if not r.ok:
            raise StepError(f"Forgejo answered {r.status} when comparing commits of {self.forgejo_repo}: "
                            f"{self._clean(r.text)}")
        data = r.json()
        total = data.get("total_commits") if isinstance(data, dict) else None
        if not isinstance(total, int) or total < 0:
            raise StepError(f"Forgejo returned no comparison for {self.forgejo_repo}")
        return total

    def _forgejo_lagging(self, base: str, head: str, branch: str) -> bool:
        """True when GitHub's commit is an ancestor of the newer Forgejo commit on this branch."""
        forward = self._forgejo_compare(base, head)
        if forward is None:
            raise StepError(f"Forgejo does not know the GitHub commit of {branch}: refusing to mirror it")
        reverse = self._forgejo_compare(head, base)
        if reverse is None:
            raise StepError(f"Forgejo does not know a commit of {branch}: refusing to mirror it")
        return forward > 0 and reverse == 0

    def _branches(self, api: str, repo: str, page_param: str, headers: dict, service: str) -> dict[str, str]:
        branches: dict[str, str] = {}
        for page in range(1, MAX_PAGES + 1):
            r = self._call("GET", f"{api}/repos/{repo}/branches?{page_param}={PER_PAGE}&page={page}", headers=headers)
            if not r.ok:
                raise StepError(f"{service} answered {r.status} when reading the branches of {repo}: "
                                f"{self._clean(r.text)}")
            rows = r.json()
            if not isinstance(rows, list):
                raise StepError(f"{service} returned no branch list for {repo}")
            for row in rows:
                name = row.get("name") if isinstance(row, dict) else None
                commit = (row.get("commit") or {}) if isinstance(row, dict) else {}
                sha = commit.get("id") or commit.get("sha")
                if not name or not sha:
                    raise StepError(f"{service} returned a branch without a name or commit for {repo}")
                branches[name] = sha
            if len(rows) < PER_PAGE:
                return branches
        raise StepError(f"{service} returned more than {MAX_PAGES * PER_PAGE} branches for {repo}: "
                        "refusing to compare a truncated list")

    def _verify(self, source: dict[str, str], target: dict[str, str], branch: str | None = None) -> None:
        branch = branch or self._default_branch()
        missing = sorted(set(source) - set(target))
        extra = sorted(set(target) - set(source))
        if missing or extra:
            details = []
            if missing:
                details.append(f"missing {', '.join(missing)}")
            if extra:
                details.append(f"unexpected {', '.join(extra)}")
            raise StepError(f"after the sync GitHub branches differ from {self.forgejo_repo}: {'; '.join(details)}")
        if branch not in source:
            raise StepError(f"{self.forgejo_repo} has no {branch} branch: cannot verify the GitHub push mirror")
        if source[branch] != target[branch]:
            raise StepError(f"after the sync {branch} differs: {self.forgejo_repo} {source[branch][:8]}, "
                            f"GitHub {target[branch][:8]}")
        diverged = sorted(name for name in source if source[name] != target[name])
        if diverged:
            raise StepError(f"after the sync GitHub commits differ from {self.forgejo_repo} on: {', '.join(diverged)}")

    # -- validation and redaction -------------------------------------------------------------------------------------
    @staticmethod
    def _repo(value: str, what: str) -> str:
        if not isinstance(value, str) or not REPO_NAME.match(value):
            raise StepError(f"the {what} repository must be written owner/name without a URL: {value!r}")
        return value

    @staticmethod
    def _origin(value: str, what: str) -> str:
        try:
            parts = urlsplit(value)
        except ValueError:
            raise StepError(f"the {what} address is not a URL") from None
        if parts.scheme not in ("https", "http") or not parts.hostname:
            raise StepError(f"the {what} address is not an http(s) URL")
        if parts.username or parts.password:
            raise StepError(f"the {what} address must not carry credentials")
        if parts.scheme != "https" and parts.hostname not in LOOPBACK:
            raise StepError(f"the {what} address must use https")
        return value.rstrip("/")

    def _github_api(self, value: str) -> str:
        api = self._origin(value, "GitHub API")
        target = urlsplit(self.github_url)
        host = urlsplit(api).hostname
        if host not in {target.hostname, f"api.{target.hostname}"} and not (
                host in LOOPBACK and target.hostname in LOOPBACK):
            raise StepError(f"the GitHub API address {host!r} is not the host of the mirror target")
        return api

    def _same_remote(self, address) -> bool:
        return isinstance(address, str) and self._normal(address) == self._normal(self.remote_address)

    @staticmethod
    def _normal(address: str) -> str:
        parts = urlsplit(address.strip())
        path = parts.path.rstrip("/")
        if path.endswith(".git"):
            path = path[:-4]
        return f"{parts.scheme.lower()}://{(parts.hostname or '').lower()}:{parts.port or ''}{path}"

    def _clean(self, text: str) -> str:
        cleaned = CREDENTIALS.sub(r"\1***@", str(text))
        for secret in (self.github_token, self.forgejo_token):
            cleaned = cleaned.replace(secret, "***")
        return cleaned.strip()[:300]
