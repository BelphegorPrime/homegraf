"""Create one Forgejo repository and wire it for the agents, idempotently.

The function never guesses an input: the operator token, owner, name, description, visibility, default branch, agent login, the
files to commit and the ForgeHands endpoint and secret all come from the caller. The token is only used as a request header; it is
never stored, logged or returned.

A repository this function did not create is refused unless every requested file is already there with the same content: then a
commit would be a no-op, which is how a repeated run reports "no change" without touching an existing history. A file whose content
differs is never overwritten.
"""
from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

from .factory_repos import AGENT_EVENTS, LABELS, reconcile
from .http import Unreachable, request
from .steps import StepError, log

STEP = "new-repo"
PAGE_SIZE = 50
VISIBILITIES = ("private", "public")
COMMIT_MESSAGE = "Add the repository files from the homegraf bootstrap"
_WRITE = ("write", "admin", "owner")
_OWNER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$")
_LOGIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")


@dataclass(frozen=True)
class Result:
    """What the run read back, and whether it created it or found it already there."""

    repository: str
    repository_status: str          # created | reused | already there
    collaborator: str
    collaborator_status: str        # added | updated | already there
    default_branch: str
    commit: str                     # created | no change
    files: tuple[str, ...]
    labels_created: tuple[str, ...]
    labels_updated: tuple[str, ...]
    hook_status: str                # created | already there
    hook_events: tuple[str, ...]


def _token(value) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("an operator token is required; it is never read from the environment or the state")
    return value


def _match(pattern: re.Pattern, value, what: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value) or ".." in value:
        raise ValueError(f"{what} is not a valid Forgejo name: {value!r}")
    return value


def _branch(value) -> str:
    if (not isinstance(value, str) or not _BRANCH.fullmatch(value) or ".." in value or "//" in value
            or value.endswith(("/", "."))):
        raise ValueError(f"the default branch is not a valid branch name: {value!r}")
    return value


def _endpoint(value) -> str:
    endpoint = value.strip() if isinstance(value, str) else ""
    parts = urlsplit(endpoint)
    if parts.scheme not in ("http", "https") or not parts.netloc or parts.username or parts.password:
        raise ValueError("the ForgeHands endpoint must be an http(s) URL without credentials")
    if parts.query or parts.fragment:
        raise ValueError("the ForgeHands endpoint must not carry a query or a fragment")
    return endpoint


def _secret(value) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("the ForgeHands webhook secret must be a non-empty string")
    return value


def _files(value) -> dict[str, bytes]:
    if not isinstance(value, dict) or not value:
        raise ValueError("files must be a non-empty mapping from a relative path to its text")
    files: dict[str, bytes] = {}
    for path, content in value.items():
        if not isinstance(path, str) or not path or path.startswith("/") or "\\" in path or "\n" in path or "\r" in path:
            raise ValueError(f"not a relative file path: {path!r}")
        if any(part in ("", ".", "..") for part in path.split("/")):
            raise ValueError(f"not a relative file path: {path!r}")
        if isinstance(content, str):
            files[path] = content.encode("utf-8")
        elif isinstance(content, (bytes, bytearray)):
            files[path] = bytes(content)
        else:
            raise ValueError(f"{path}: the content must be text or bytes")
    return files


class NewRepository:
    def __init__(self, cfg, state, *, token, owner, name, description, visibility, default_branch, agent_login, files,
                 forgehands_endpoint, webhook_secret, http=request, step: str = STEP):
        self.cfg, self.state, self.http, self.step = cfg, state, http, step
        self.token = _token(token)
        self.owner = _match(_OWNER, owner, "the owner")
        self.name = _match(_NAME, name, "the repository name")
        self.full_name = f"{self.owner}/{self.name}"
        if not isinstance(description, str):
            raise ValueError("the description must be a string")
        self.description = description
        if visibility not in VISIBILITIES:
            raise ValueError(f"visibility must be {' or '.join(VISIBILITIES)}, not {visibility!r}")
        self.private = visibility == "private"
        self.branch = _branch(default_branch)
        self.agent_login = _match(_LOGIN, agent_login, "the agent login")
        self.files = _files(files)
        self.endpoint = _endpoint(forgehands_endpoint)
        self.secret = _secret(webhook_secret)
        self.api = cfg.forgejo_url + "/api/v1"
        self._multi_file: bool | None = None

    def _headers(self) -> dict:
        return {"Authorization": f"token {self.token}"}

    def _redact(self, value: str) -> str:
        for secret in (self.token, self.secret):
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

    def _call(self, method: str, path: str, *, body=None, what: str, allow=()):
        try:
            response = self.http(method, self.api + path, body=body, headers=self._headers())
        except Unreachable as exc:
            raise StepError(f"{what}: Forgejo is unreachable ({exc})") from None
        if response.status in allow:
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
            if not batch:
                break
            if not isinstance(batch, list):
                raise StepError(f"{what}: Forgejo did not answer with a list")
            fresh = []
            for item in batch:
                if not isinstance(item, dict):
                    raise StepError(f"{what}: Forgejo answered an entry that is not an object")
                key = item.get("id")
                if key in seen:
                    continue
                seen.add(key)
                fresh.append(item)
            if not fresh:
                break
            items.extend(fresh)
            page += 1
        return items

    def run(self) -> Result:
        repo, repository_status = self._repository()
        operations = self._plan(repository_status, repo.get("empty") is True)
        self._ensure_settings(repo, repository_status)
        collaborator_status = self._ensure_collaborator()
        labels_before = self._labels()
        hooks_before = self._owned_hooks()
        reconcile(self.cfg, self.state, [self.full_name], self.endpoint, self.secret, token=self.token, http=self.http)
        commit = self._commit(operations)
        return self._verify(repository_status, collaborator_status, labels_before, hooks_before, commit)

    # -- repository -------------------------------------------------------------------------------------------------------

    def _repository(self) -> tuple[dict, str]:
        what = f"new repository {self.full_name}: reading the repository"
        response = self._call("GET", f"/repos/{self.full_name}", what=what, allow=(404,))
        if response.status == 404:
            return self._create_repository()
        repo = self._json(response, what)
        if not isinstance(repo, dict):
            raise StepError(f"{what}: Forgejo did not answer with a repository object")
        if repo.get("empty") is True:
            log(self.step, f"{self.full_name}: the existing empty repository is reused")
            return repo, "reused"
        log(self.step, f"{self.full_name}: the repository exists with content; nothing is overwritten")
        return repo, "already there"

    def _read_back(self) -> dict:
        what = f"new repository {self.full_name}: reading the repository back"
        repo = self._json(self._call("GET", f"/repos/{self.full_name}", what=what), what)
        if not isinstance(repo, dict):
            raise StepError(f"{what}: Forgejo did not answer with a repository object")
        return repo

    def _owner_path(self) -> str:
        what = f"new repository {self.full_name}: checking the owner {self.owner}"
        me = self._json(self._call("GET", "/user", what=f"new repository {self.full_name}: reading the token's user"),
                        f"new repository {self.full_name}: reading the token's user")
        login = me.get("login") if isinstance(me, dict) else None
        if not isinstance(login, str) or not login:
            raise StepError(f"new repository {self.full_name}: Forgejo did not say which user the token belongs to")
        if login == self.owner:
            return "/user/repos"
        if self._call("GET", f"/orgs/{quote(self.owner, safe='')}", what=what, allow=(404,)).status != 404:
            return f"/orgs/{quote(self.owner, safe='')}/repos"
        if self._call("GET", f"/users/{quote(self.owner, safe='')}", what=what, allow=(404,)).status != 404:
            raise StepError(f"{what}: Forgejo cannot create a repository for another user with this token; use the token's own "
                            "user or an organisation")
        raise StepError(f"{what}: Forgejo has no user or organisation {self.owner!r}")

    def _create_repository(self) -> tuple[dict, str]:
        path = self._owner_path()
        what = f"new repository {self.full_name}: creating the repository"
        response = self._call("POST", path, what=what, allow=(409,), body={
            "name": self.name, "description": self.description, "private": self.private,
            "auto_init": False, "default_branch": self.branch})
        if response.status == 409:
            log(self.step, f"{self.full_name}: the repository appeared while it was created; reading it back")
            repo = self._read_back()
            return repo, "reused" if repo.get("empty") is True else "already there"
        created = self._json(response, what)
        if not isinstance(created, dict):
            raise StepError(f"{what}: Forgejo did not answer with a repository object")
        log(self.step, f"{self.full_name}: repository created ({'private' if self.private else 'public'})")
        return self._read_back(), "created"

    def _ensure_settings(self, repo: dict, status: str) -> None:
        """Sets the explicit description, visibility and default branch on a repository this run created or reused empty. A
        repository with history is left as it is; the read-back then refuses a visibility or default branch that differs."""
        if status == "already there":
            return
        body = {}
        if repo.get("default_branch") != self.branch:
            body["default_branch"] = self.branch
        if bool(repo.get("private")) != self.private:
            body["private"] = self.private
        if (repo.get("description") or "") != self.description:
            body["description"] = self.description
        if not body:
            return
        what = f"new repository {self.full_name}: setting {', '.join(sorted(body))}"
        self._call("PATCH", f"/repos/{self.full_name}", body=body, what=what)
        log(self.step, f"{self.full_name}: set {', '.join(sorted(body))}")

    # -- files ------------------------------------------------------------------------------------------------------------

    def _content(self, path: str) -> bytes | None:
        what = f"new repository {self.full_name}: reading {path} at {self.branch}"
        response = self._call("GET", f"/repos/{self.full_name}/contents/{quote(path, safe='/')}?ref={quote(self.branch, safe='')}",
                              what=what, allow=(404,))
        if response.status == 404:
            return None
        data = self._json(response, what)
        if isinstance(data, list):                 # Forgejo answers 200 with a list for any path of an empty repository
            return None
        if not isinstance(data, dict):
            raise StepError(f"{what}: Forgejo did not answer with a file object")
        if data.get("type") not in (None, "file"):
            raise StepError(f"{what}: {path} is a {data.get('type')}, not a file")
        if data.get("encoding") not in (None, "base64") or not isinstance(data.get("content"), str):
            raise StepError(f"{what}: Forgejo did not answer the file content as base64")
        try:
            return base64.b64decode(data["content"])
        except (ValueError, TypeError):
            raise StepError(f"{what}: Forgejo answered content that is not base64") from None

    def _plan(self, status: str, empty: bool) -> list[dict]:
        if empty:
            return [{"operation": "create", "path": path, "content": base64.b64encode(self.files[path]).decode("ascii")}
                    for path in sorted(self.files)]
        operations = []
        for path in sorted(self.files):
            have = self._content(path)
            if have is None:
                operations.append({"operation": "create", "path": path,
                                   "content": base64.b64encode(self.files[path]).decode("ascii")})
            elif have != self.files[path]:
                raise StepError(f"new repository {self.full_name}: refusing to overwrite {path}: the existing repository already "
                                "holds different content")
        if status == "already there" and operations:
            raise StepError(f"new repository {self.full_name}: the repository already exists and is not empty; refusing to touch "
                            "its history")
        return operations

    def _multi_file_supported(self) -> bool:
        if self._multi_file is None:
            url = self.cfg.forgejo_url + "/swagger.v1.json"
            try:
                response = self.http("GET", url)
            except Unreachable as exc:
                raise StepError(f"new repository {self.full_name}: checking the contents API: Forgejo is unreachable ({exc})") from None
            self._multi_file = False
            if response.ok:
                try:
                    swagger = response.json()
                except ValueError:
                    swagger = None
                paths = swagger.get("paths") if isinstance(swagger, dict) else None
                operation = paths.get("/repos/{owner}/{repo}/contents") if isinstance(paths, dict) else None
                self._multi_file = isinstance(operation, dict) and "post" in operation
            if not self._multi_file:
                log(self.step, "the instance's swagger.v1.json has no POST /repos/{owner}/{repo}/contents: one request per file")
        return self._multi_file

    def _commit(self, operations: list[dict]) -> str:
        if not operations:
            log(self.step, f"{self.full_name}: the files are already there with the same content; nothing to commit")
            return "no change"
        if self._multi_file_supported():
            what = f"new repository {self.full_name}: committing the files"
            self._call("POST", f"/repos/{self.full_name}/contents", what=what, body={
                "branch": self.branch, "message": COMMIT_MESSAGE, "files": operations})
            log(self.step, f"{self.full_name}: committed {len(operations)} files in one request")
        else:
            for operation in operations:
                path = operation["path"]
                what = f"new repository {self.full_name}: committing {path}"
                self._call("POST", f"/repos/{self.full_name}/contents/{quote(path, safe='/')}", what=what, body={
                    "branch": self.branch, "message": COMMIT_MESSAGE, "content": operation["content"]})
            log(self.step, f"{self.full_name}: committed {len(operations)} files with one request per file")
        return "created"

    # -- collaborator, labels and hook ------------------------------------------------------------------------------------

    def _permission(self) -> str | None:
        what = f"new repository {self.full_name}: reading the permission of {self.agent_login}"
        response = self._call("GET", f"/repos/{self.full_name}/collaborators/{quote(self.agent_login, safe='')}/permission",
                              what=what, allow=(404,))
        if response.status == 404:
            return None
        data = self._json(response, what)
        permission = data.get("permission") if isinstance(data, dict) else None
        return permission if isinstance(permission, str) else None

    def _ensure_collaborator(self) -> str:
        permission = self._permission()
        if permission in _WRITE:
            log(self.step, f"{self.full_name}: {self.agent_login} is already a collaborator with {permission}")
            return "already there"
        what = f"new repository {self.full_name}: adding {self.agent_login} as a collaborator with write"
        self._call("PUT", f"/repos/{self.full_name}/collaborators/{quote(self.agent_login, safe='')}", what=what,
                   body={"permission": "write"})
        log(self.step, f"{self.full_name}: {self.agent_login} added as a collaborator with write")
        return "updated" if permission is not None else "added"

    def _labels(self) -> dict[str, dict]:
        labels = self._paged(f"/repos/{self.full_name}/labels", f"new repository {self.full_name}: reading the labels back")
        return {label.get("name"): label for label in labels}

    @staticmethod
    def _label_ok(label: dict, wanted: dict) -> bool:
        color = str(label.get("color") or "").lstrip("#").lower()
        return color == wanted["color"] and (label.get("description") or "") == wanted["description"]

    def _owned_hooks(self) -> list[dict]:
        hooks = self._paged(f"/repos/{self.full_name}/hooks", f"new repository {self.full_name}: reading the webhooks back")
        owned = []
        for hook in hooks:
            config = hook.get("config")
            url = config.get("url") if isinstance(config, dict) else None
            if isinstance(url, str) and url.rstrip("/") == self.endpoint.rstrip("/"):
                owned.append(hook)
        return owned

    # -- read back --------------------------------------------------------------------------------------------------------

    def _verify(self, repository_status: str, collaborator_status: str, labels_before: dict, hooks_before: list[dict],
                commit: str) -> Result:
        repo = self._read_back()
        name = str(repo.get("full_name") or "")
        if name.lower() != self.full_name.lower():
            raise StepError(f"new repository {self.full_name}: Forgejo read back the repository {name!r}")
        if bool(repo.get("private")) != self.private:
            raise StepError(f"new repository {self.full_name}: the repository is "
                            f"{'private' if repo.get('private') else 'public'}, not {'private' if self.private else 'public'}")
        if repo.get("default_branch") != self.branch:
            raise StepError(f"new repository {self.full_name}: the default branch is {repo.get('default_branch')!r}, "
                            f"not {self.branch!r}")
        if repository_status != "already there" and (repo.get("description") or "") != self.description:
            raise StepError(f"new repository {self.full_name}: the description was not stored")

        permission = self._permission()
        if permission not in _WRITE:
            raise StepError(f"new repository {self.full_name}: {self.agent_login} read back with permission {permission!r}, not write")

        labels_after = self._labels()
        labels_created = tuple(name for name in LABELS if name not in labels_before)
        labels_updated = tuple(name for name in LABELS
                               if name in labels_before and not self._label_ok(labels_before[name], LABELS[name]))
        for name, wanted in LABELS.items():
            label = labels_after.get(name)
            if label is None:
                raise StepError(f"new repository {self.full_name}: the label {name} is missing after the reconciliation")
            if not self._label_ok(label, wanted):
                raise StepError(f"new repository {self.full_name}: the label {name} reads back with another color or description")

        hooks_after = self._owned_hooks()
        if len(hooks_after) != 1:
            raise StepError(f"new repository {self.full_name}: read back {len(hooks_after)} webhooks for {self.endpoint}")
        hook = hooks_after[0]
        if str(hook.get("type") or "").lower() != "forgejo" or hook.get("active") is not True or hook.get("branch_filter"):
            raise StepError(f"new repository {self.full_name}: the webhook is not an active full-event forgejo hook")
        if not set(AGENT_EVENTS) <= set(hook.get("events") or []):
            raise StepError(f"new repository {self.full_name}: the webhook lacks an event the factory needs")
        before_ids = {hook.get("id") for hook in hooks_before}
        hook_status = "already there" if hooks_before and all(h.get("id") in before_ids for h in hooks_after) else "created"

        for path in sorted(self.files):
            have = self._content(path)
            if have != self.files[path]:
                raise StepError(f"new repository {self.full_name}: {path} does not read back with the committed content")

        return Result(repository=self.full_name, repository_status=repository_status, collaborator=self.agent_login,
                      collaborator_status=collaborator_status, default_branch=self.branch, commit=commit,
                      files=tuple(sorted(self.files)), labels_created=labels_created, labels_updated=labels_updated,
                      hook_status=hook_status, hook_events=tuple(sorted(hook.get("events") or [])))
