"""Reconcile the Forgejo side of the agent factory for repositories named by the caller.

Only the repositories in `allowed_repos` are touched: the caller never falls back to listing writable repositories. Labels are matched by
name and updated in place; the ForgeHands hook is recreated when its events or secret change because Forgejo only accepts the
`action_run_*` events when a hook is created, not on PATCH.
"""
from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import urlsplit

from .http import Unreachable, request
from .steps import StepError, log

STEP = "factory-repos"
STATE_SECTION = "factory_repos"
PAGE_SIZE = 50
HOOK_TYPE = "forgejo"
CONTENT_TYPE = "json"
AGENT_EVENTS = ("issues", "pull_request_label", "action_run_failure", "action_run_success", "push")

# Stable names, colors and descriptions. `ai:blocked` is left to ForgeHands.
LABELS = {
    "ai:plan": {"color": "1d76db", "description": "Plan the issue before any implementation"},
    "ai:ready": {"color": "0e8a16", "description": "Start the worker agent on this issue"},
    "ai:escalate": {"color": "b60205", "description": "Start the stronger worker agent on this issue"},
    "ai:review": {"color": "5319e7", "description": "Review the agent pull request"},
    "ai:changes": {"color": "d93f0b", "description": "Reviewer asks the worker for another round"},
    "ai:approved": {"color": "2ea44f", "description": "Reviewer approved the pull request"},
    "ai:close": {"color": "6a737d", "description": "Review and close this ticket when its last dependency is closed, no planner run"},
}

_REPOSITORY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$")


def _explicit_repositories(allowed_repos) -> tuple[str, ...]:
    if isinstance(allowed_repos, str):
        allowed_repos = [allowed_repos]
    try:
        values = list(allowed_repos)
    except TypeError:
        raise ValueError("allowed_repos must be an iterable of explicit owner/name repositories") from None
    repositories: list[str] = []
    for value in values:
        repository = value.strip() if isinstance(value, str) else ""
        if not _REPOSITORY.fullmatch(repository) or ".." in repository:
            raise ValueError(f"not an explicit owner/name repository: {value!r}")
        if repository not in repositories:
            repositories.append(repository)
    if not repositories:
        raise ValueError("allowed_repos names no repository; an empty list never means every writable repository")
    return tuple(repositories)


def _forgehands_endpoint(value) -> str:
    endpoint = value.strip() if isinstance(value, str) else ""
    parts = urlsplit(endpoint)
    if parts.scheme not in ("http", "https") or not parts.netloc or parts.username or parts.password:
        raise ValueError("the ForgeHands endpoint must be an http(s) URL without credentials")
    if parts.query or parts.fragment:
        raise ValueError("the ForgeHands endpoint must not carry a query or a fragment")
    return endpoint


def _webhook_secret(value) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("the ForgeHands webhook secret must be a non-empty string")
    return value


def _same_url(left, right) -> bool:
    return isinstance(left, str) and left.rstrip("/") == right.rstrip("/")


def _hook_url(hook: dict):
    config = hook.get("config")
    return config.get("url") if isinstance(config, dict) else None


class RepositoryReconciler:
    """Ensures the factory labels and one active full-event webhook in each explicitly allowed repository."""

    def __init__(self, cfg, state, allowed_repos, endpoint, secret, *, token: str | None = None, http=request, step: str = STEP):
        self.cfg, self.state, self.http, self.step = cfg, state, http, step
        self.repositories = _explicit_repositories(allowed_repos)
        self.endpoint = _forgehands_endpoint(endpoint)
        self.secret = _webhook_secret(secret)
        self.token = (token if token is not None else state.get("forgejo", "token")) or ""
        self.api = cfg.forgejo_url + "/api/v1"

    def _headers(self) -> dict:
        return {"Authorization": f"token {self.token}"}

    def _redact(self, value: str) -> str:
        for secret in (self.secret, self.token):
            if secret:
                value = value.replace(secret, "[redacted]")
        return " ".join(value.split())[:200]

    def _reason(self, response) -> str:
        try:
            data = response.json()
        except ValueError:
            return ""
        if not isinstance(data, dict):
            return ""
        message = data.get("message") or data.get("error") or ""
        return self._redact(str(message)) if message else ""

    def _call(self, method: str, path: str, *, body=None, what: str, allow_404: bool = False):
        try:
            response = self.http(method, self.api + path, body=body, headers=self._headers())
        except Unreachable as exc:
            raise StepError(f"{what}: Forgejo is unreachable ({exc})") from None
        if response.status == 404 and allow_404:
            return response
        if not response.ok:
            reason = self._reason(response)
            detail = f": {reason}" if reason else ""
            raise StepError(f"{what}: Forgejo answered {response.status}{detail}")
        return response

    def _json(self, response, what: str):
        try:
            return response.json()
        except ValueError:
            raise StepError(f"{what}: Forgejo did not answer with JSON") from None

    def _paged(self, path: str, what: str) -> list[dict]:
        items: list[dict] = []
        seen: set = set()
        page = 1
        separator = "&" if "?" in path else "?"
        while True:
            response = self._call("GET", f"{path}{separator}page={page}&limit={PAGE_SIZE}", what=what)
            batch = self._json(response, what)
            if batch is None:
                break
            if not isinstance(batch, list):
                raise StepError(f"{what}: Forgejo did not answer with a list")
            fresh = []
            for item in batch:
                if not isinstance(item, dict):
                    raise StepError(f"{what}: Forgejo answered an entry that is not an object")
                key = item.get("id")
                if key is None:
                    key = json.dumps(item, sort_keys=True, default=str)
                if key not in seen:
                    seen.add(key)
                    fresh.append(item)
            if not fresh:
                break
            items.extend(fresh)
            page += 1
        return items

    def run(self) -> None:
        if not self.token:
            raise StepError("the factory has no Forgejo token: pass one explicitly or store it in the state")
        for repository in self.repositories:
            self.reconcile(repository)

    def reconcile(self, repository: str) -> None:
        self._writable_primary(repository)
        self._reconcile_labels(repository)
        self._reconcile_hook(repository)
        log(self.step, f"{repository}: labels and ForgeHands webhook reconciled")

    def _writable_primary(self, repository: str) -> dict:
        response = self._call("GET", f"/repos/{repository}", what=f"{repository}: reading the repository")
        info = self._json(response, f"{repository}: reading the repository")
        if not isinstance(info, dict):
            raise StepError(f"{repository}: Forgejo did not answer with a repository object")
        if info.get("mirror"):
            raise StepError(f"{repository}: a pull mirror is read-only; point the factory at a writable primary repository")
        return info

    @staticmethod
    def _label_matches(label: dict, name: str, wanted: dict) -> bool:
        if label.get("name") not in (None, name):
            return False
        color = str(label.get("color") or "").lstrip("#").lower()
        return color == wanted["color"] and (label.get("description") or "") == wanted["description"]

    def _reconcile_labels(self, repository: str) -> None:
        labels = self._paged(f"/repos/{repository}/labels", f"{repository}: reading labels")
        by_name = {label.get("name"): label for label in labels}
        base = f"/repos/{repository}/labels"
        for name, wanted in LABELS.items():
            label = by_name.get(name)
            if label is None:
                response = self._call("POST", base, body={"name": name, **wanted}, what=f"{repository}: creating the label {name}")
                self._verify_label(repository, name, response, None)
                log(self.step, f"{repository}: label {name} created")
            elif not self._label_matches(label, name, wanted):
                label_id = label.get("id")
                if label_id is None:
                    raise StepError(f"{repository}: the label {name} has no id, so it cannot be corrected")
                response = self._call("PATCH", f"{base}/{label_id}", body=wanted, what=f"{repository}: updating the label {name}")
                self._verify_label(repository, name, response, label_id)
                log(self.step, f"{repository}: label {name} updated")

    def _verify_label(self, repository: str, name: str, response, label_id) -> None:
        what = f"{repository}: reading back the label {name}"
        label = self._json(response, what)
        if label_id is None and isinstance(label, dict):
            label_id = label.get("id")
        if not isinstance(label, dict) or not self._label_matches(label, name, LABELS[name]):
            if label_id is None:
                raise StepError(f"{what}: Forgejo did not return the label")
            label = self._json(self._call("GET", f"/repos/{repository}/labels/{label_id}", what=what), what)
            if not isinstance(label, dict) or not self._label_matches(label, name, LABELS[name]):
                raise StepError(f"{what}: the effective color or description differs from the requested one")

    def _hook_matches_endpoint(self, hook: dict) -> bool:
        config = hook.get("config") if isinstance(hook.get("config"), dict) else {}
        if str(hook.get("type") or "").lower() != HOOK_TYPE:
            return False
        if hook.get("active") is not True:
            return False
        if not set(AGENT_EVENTS) <= set(hook.get("events") or []):
            return False
        if not _same_url(config.get("url"), self.endpoint):
            return False
        if str(config.get("content_type") or "").lower() != CONTENT_TYPE:
            return False
        if hook.get("branch_filter"):
            return False
        return True

    def _fingerprint(self) -> str:
        value = f"{self.step}\0{self.endpoint}\0{self.secret}".encode()
        return hashlib.sha256(value).hexdigest()

    def _fingerprints(self) -> dict:
        stored = self.state.get(STATE_SECTION, "hooks") or {}
        return dict(stored) if isinstance(stored, dict) else {}

    def _remember_fingerprint(self, repository: str, fingerprint: str) -> None:
        fingerprints = self._fingerprints()
        fingerprints[repository] = fingerprint
        self.state.set(STATE_SECTION, "hooks", fingerprints)

    def _reconcile_hook(self, repository: str) -> None:
        hooks = self._paged(f"/repos/{repository}/hooks", f"{repository}: reading webhooks")
        owned = [hook for hook in hooks if _same_url(_hook_url(hook), self.endpoint)]
        fingerprint = self._fingerprint()
        if (len(owned) == 1 and self._fingerprints().get(repository) == fingerprint and self._hook_matches_endpoint(owned[0])):
            return
        self._replace_hook(repository, owned, fingerprint)

    def _replace_hook(self, repository: str, owned: list[dict], fingerprint: str) -> None:
        base = f"/repos/{repository}/hooks"
        for hook in owned:
            hook_id = hook.get("id")
            if hook_id is None:
                raise StepError(f"{repository}: an owned webhook has no id")
            response = self._call("DELETE", f"{base}/{hook_id}", what=f"{repository}: deleting the old ForgeHands webhook", allow_404=True)
            if response.status not in (204, 404) and not response.ok:
                raise StepError(f"{repository}: Forgejo refused to delete the old ForgeHands webhook ({response.status})")
        if owned:
            log(self.step, f"{repository}: ForgeHands webhook replaced (delete and create, never PATCH)")
        else:
            log(self.step, f"{repository}: ForgeHands webhook created")
        response = self._call("POST", base, what=f"{repository}: creating the ForgeHands webhook", body={
            "type": HOOK_TYPE,
            "active": True,
            "events": list(AGENT_EVENTS),
            "config": {"url": self.endpoint, "content_type": CONTENT_TYPE, "secret": self.secret},
        })
        created = self._json(response, f"{repository}: creating the ForgeHands webhook")
        hook_id = created.get("id") if isinstance(created, dict) else None
        what = f"{repository}: reading back the ForgeHands webhook"
        if hook_id is None:
            matches = [hook for hook in self._paged(f"/repos/{repository}/hooks", what)
                       if _same_url(_hook_url(hook), self.endpoint)]
            if len(matches) != 1:
                raise StepError(f"{what}: Forgejo did not return exactly one webhook for the configured endpoint")
            hook = matches[0]
        else:
            hook = self._json(self._call("GET", f"{base}/{hook_id}", what=what), what)
        if not isinstance(hook, dict) or not self._hook_matches_endpoint(hook):
            effective = hook.get("events") if isinstance(hook, dict) else None
            raise StepError(f"{what}: effective events {effective!r} or configuration differ from the requested full-event hook")
        self._remember_fingerprint(repository, fingerprint)


def reconcile(cfg, state, allowed_repos, endpoint, secret, *, token: str | None = None, http=request, step: str = STEP) -> None:
    """Entry point for the bootstrap; the caller supplies every repository, the internal endpoint and the webhook secret."""
    RepositoryReconciler(cfg, state, allowed_repos, endpoint, secret, token=token, http=http, step=step).run()
