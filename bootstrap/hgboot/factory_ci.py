"""The CI half of the software factory: the seed workflow, runner readiness and required status checks.

The generated repository gets ``.forgejo/workflows/ci.yaml`` from the seed.  The workflow is tests only: it runs on the
Forgejo runner labelled ``docker``, fetches the triggering commit with a plain authenticated git fetch and uses a
container pinned by version and digest.  Its workflow name and job name are deliberately stable because Forgejo builds
the commit-status context as ``<workflow name> / <job name> (<event>)``; the branch protection therefore uses the glob
``ci / seed (*)`` so that push, pull_request and manual runs all satisfy the same required check.

Runner readiness is checked across the scopes Forgejo exposes for a repository: the repository endpoint with
``visible=true`` (which also includes runners owned by the owner, an organization or the instance) and the admin
endpoint with ``visible=true`` (which returns every scope).  Missing, offline or inaccessible runners are reported as
blocked, never raised as a successful check.

Required checks are reconciled on the repository's own default branch, read from Forgejo rather than guessed.  Only
``enable_status_check`` and the existing ``status_check_contexts`` are changed; unrelated protections and every push
right are preserved.  The seed workflow is never overwritten and the bootstrap never pushes to an existing repository:
a repository that already has content but no CI workflow gets an explicit remediation instead.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import quote

from .http import Unreachable, request
from .steps import StepError, log, warn

STEP = "factory-ci"
SEED_WORKFLOW = ".forgejo/workflows/ci.yaml"
RUNNER_LABEL = "docker"
# Forgejo reports these while a runner can take work; anything else (notably "offline") is blocked.
RUNNER_ONLINE = frozenset({"idle", "active"})
# Commit status context is "<workflow name> / <job name> (<event>)".  The glob keeps one stable required check.
REQUIRED_CHECKS = ("ci / seed (*)",)
PAGE_SIZE = 50

_REPOSITORY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$")


def _repository(value) -> str:
    repository = value.strip() if isinstance(value, str) else ""
    if not _REPOSITORY.fullmatch(repository) or ".." in repository:
        raise ValueError(f"not an explicit owner/name repository: {value!r}")
    return repository


def _checks(values) -> tuple[str, ...]:
    if isinstance(values, str):
        values = [values]
    try:
        items = list(values)
    except TypeError:
        raise ValueError("required status checks must be an iterable of context names") from None
    result: list[str] = []
    for value in items:
        context = value.strip() if isinstance(value, str) else ""
        if not context or "\r" in context or "\n" in context:
            raise ValueError("a required status check must be a non-empty single-line string")
        if context not in result:
            result.append(context)
    if not result:
        raise ValueError("at least one required status check context is required")
    return tuple(result)


def _label(value) -> str:
    label = value.strip() if isinstance(value, str) else ""
    if not label or "\r" in label or "\n" in label:
        raise ValueError("the runner label must be a non-empty single-line string")
    return label


@dataclass(frozen=True)
class RunnerReadiness:
    """Whether a runner that can take the workflow's label exists; ``reason`` is safe to log."""

    ready: bool
    label: str
    reason: str
    scopes: tuple[str, ...] = ()
    runners: tuple[str, ...] = ()


@dataclass(frozen=True)
class CheckReconciliation:
    """What the required-check reconciliation did and the effective context list."""

    branch: str
    action: str                 # created, updated or unchanged
    required: tuple[str, ...]
    contexts: tuple[str, ...]


@dataclass(frozen=True)
class WorkflowState:
    """The state of the seed CI workflow in an existing repository; this audit never writes."""

    path: str
    state: str                  # present, custom or missing
    message: str


def missing_ci_remediation(repository: str = "the repository") -> str:
    """What to do when an existing repository has no CI workflow.  Rerunning the bootstrap must not push to main."""
    return (f"{repository}: {SEED_WORKFLOW} is missing, but the repository already has content. The bootstrap never "
            f"pushes to an existing repository. Add the seed workflow in a pull request, or re-create the empty "
            f"repository and rerun the bootstrap.")


def _workflow(files) -> bytes:
    if isinstance(files, dict):
        content = files.get(SEED_WORKFLOW)
    else:
        content = dict(files).get(SEED_WORKFLOW)
    if content is None:
        raise ValueError(f"the collected seed has no {SEED_WORKFLOW}")
    if isinstance(content, str):
        content = content.encode()
    return bytes(content)


def audit_seed_workflow(files, read_file, *, repository: str = "the repository") -> WorkflowState:
    """Compare the collected seed workflow with what an existing repository has, without writing anything.

    ``read_file(path)`` returns the current text or bytes (``None`` when the file is absent).  A matching file is
    ``present``; a different file is the user's own workflow and is left untouched; a missing file gets the explicit
    remediation because the bootstrap never pushes to a repository that already has content.
    """
    wanted = _workflow(files)
    existing = read_file(SEED_WORKFLOW)
    if existing is None:
        return WorkflowState(SEED_WORKFLOW, "missing", missing_ci_remediation(repository))
    if isinstance(existing, str):
        existing = existing.encode()
    if bytes(existing) == wanted:
        return WorkflowState(SEED_WORKFLOW, "present", f"{repository}: {SEED_WORKFLOW} is present")
    return WorkflowState(SEED_WORKFLOW, "custom",
                         f"{repository}: {SEED_WORKFLOW} differs from the seed workflow and is left untouched")


class FactoryCI:
    """Check the runner and reconcile the required CI checks for one explicit repository."""

    def __init__(self, cfg, state, repository, *, token: str | None = None, http=request,
                 label: str = RUNNER_LABEL, checks=REQUIRED_CHECKS, step: str = STEP):
        self.cfg, self.state, self.http, self.step = cfg, state, http, step
        self.repository = _repository(repository)
        self.token = (token if token is not None else state.get("forgejo", "token")) or ""
        self.label = _label(label)
        self.checks = _checks(checks)
        self.api = cfg.forgejo_url.rstrip("/") + "/api/v1"

    def _headers(self) -> dict:
        return {"Authorization": f"token {self.token}"}

    def _redact(self, value: str) -> str:
        return value.replace(self.token, "[redacted]") if self.token else value

    def _reason(self, response) -> str:
        try:
            data = response.json()
        except ValueError:
            return ""
        if not isinstance(data, dict):
            return ""
        message = data.get("message") or data.get("error") or ""
        return " ".join(self._redact(str(message)).split())[:200] if message else ""

    def _call(self, method: str, path: str, *, body=None, what: str):
        try:
            response = self.http(method, self.api + path, body=body, headers=self._headers())
        except Unreachable as exc:
            raise StepError(f"{what}: Forgejo is unreachable ({exc})") from None
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
            if isinstance(batch, dict):
                batch = batch.get("runners") or batch.get("items") or []
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

    def _runner_scopes(self) -> tuple[tuple[str, str], ...]:
        repository = quote(self.repository, safe="/")
        return (("repository", f"/repos/{repository}/actions/runners?visible=true"),
                ("global", "/admin/actions/runners?visible=true"))

    def _runners(self, path: str, scope: str) -> tuple[list[dict], str]:
        try:
            return self._paged(path, f"reading {scope} runners"), ""
        except StepError as exc:
            return [], str(exc)

    @staticmethod
    def _matches(runner: dict, label: str) -> bool:
        for raw in runner.get("labels") or []:
            if not isinstance(raw, str):
                continue
            if raw.split(":", 1)[0].strip() == label:
                return True
        return False

    @staticmethod
    def _name(runner: dict, scope: str) -> str:
        return str(runner.get("name") or runner.get("uuid") or f"{scope}:{runner.get('id')}")

    def runner_readiness(self) -> RunnerReadiness:
        """Return blocked readiness for a missing token, an inaccessible API, or no online runner with the label."""
        if not self.token:
            return RunnerReadiness(False, self.label,
                                   "runner readiness is blocked: the factory has no Forgejo token to inspect runners")
        scopes: list[str] = []
        problems: list[str] = []
        matching: list[tuple[str, dict]] = []
        seen: set = set()
        for scope, path in self._runner_scopes():
            scopes.append(scope)
            runners, error = self._runners(path, scope)
            if error:
                problems.append(error)
            for runner in runners:
                if self._matches(runner, self.label):
                    key = runner.get("id") or runner.get("uuid") or (scope, self._name(runner, scope))
                    if key not in seen:
                        seen.add(key)
                        matching.append((scope, runner))
        online = [self._name(runner, scope) for scope, runner in matching
                  if str(runner.get("status") or "").lower() in RUNNER_ONLINE]
        if online:
            return RunnerReadiness(True, self.label, f"runner(s) labelled {self.label} are online: {', '.join(online)}",
                                   tuple(scopes), tuple(online))
        if matching:
            offline = [self._name(runner, scope) for scope, runner in matching]
            return RunnerReadiness(False, self.label,
                                   f"runner(s) labelled {self.label} are offline: {', '.join(offline)}",
                                   tuple(scopes), tuple(offline))
        if len(problems) == len(scopes):
            return RunnerReadiness(False, self.label, "runner readiness is blocked: " + "; ".join(problems),
                                   tuple(scopes))
        reason = f"runner readiness is blocked: no runner labelled {self.label} in the applicable Forgejo scopes"
        if problems:
            reason += " (" + "; ".join(problems) + ")"
        return RunnerReadiness(False, self.label, reason, tuple(scopes))

    def _warn_shared_identity(self) -> None:
        """The token owner is informational: no user is created and no push right or whitelist is changed."""
        try:
            response = self.http("GET", self.api + "/user", headers=self._headers())
        except Unreachable:
            return
        if not response.ok:
            return
        try:
            login = (response.json() or {}).get("login")
        except ValueError:
            return
        if login and login in {self.cfg.forgejo_user, self.cfg.admin_user}:
            warn(self.step, f"the factory uses {login}, the Forgejo administrator account; no branch push right "
                            "or push whitelist is changed")

    def _protection(self, branch: str) -> dict | None:
        protections = self._json(self._call("GET", f"/repos/{self.repository}/branch_protections",
                                            what=f"{self.repository}: reading branch protections"),
                                 f"{self.repository}: reading branch protections")
        if isinstance(protections, dict):
            protections = protections.get("branch_protections") or []
        if not isinstance(protections, list):
            raise StepError(f"{self.repository}: Forgejo did not answer branch protections with a list")
        for protection in protections:
            if not isinstance(protection, dict):
                continue
            name = protection.get("rule_name") or protection.get("branch_name")
            if name == branch:
                return protection
        return None

    def reconcile_required_checks(self) -> CheckReconciliation:
        """Ensure the required contexts on the repository's own default branch, preserving unrelated protections."""
        if not self.token:
            raise StepError("the factory has no Forgejo token: pass one explicitly or store it in the state")
        self._warn_shared_identity()
        info = self._json(self._call("GET", f"/repos/{self.repository}",
                                     what=f"{self.repository}: reading the repository"),
                          f"{self.repository}: reading the repository")
        if not isinstance(info, dict):
            raise StepError(f"{self.repository}: Forgejo did not answer with a repository object")
        branch = info.get("default_branch")
        if not isinstance(branch, str) or not branch.strip():
            raise StepError(f"{self.repository}: Forgejo reports no default branch; refusing to guess")
        branch = branch.strip()
        protection = self._protection(branch)
        existing = [str(c) for c in (protection or {}).get("status_check_contexts") or []]
        merged = list(existing)
        for context in self.checks:
            if context not in merged:
                merged.append(context)
        if protection is not None:
            if protection.get("enable_status_check") is True and merged == existing:
                return CheckReconciliation(branch, "unchanged", self.checks, tuple(existing))
            name = protection.get("rule_name") or protection.get("branch_name") or branch
            self._call("PATCH", f"/repos/{self.repository}/branch_protections/{quote(str(name), safe='')}",
                       body={"enable_status_check": True, "status_check_contexts": merged},
                       what=f"{self.repository}: updating the required checks on {branch}")
            log(self.step, f"{self.repository}: required checks on {branch}: {', '.join(self.checks)}")
            return CheckReconciliation(branch, "updated", self.checks, tuple(merged))
        self._call("POST", f"/repos/{self.repository}/branch_protections",
                   body={"rule_name": branch, "enable_push": True, "enable_status_check": True,
                         "status_check_contexts": merged},
                   what=f"{self.repository}: creating the protection on {branch}")
        log(self.step, f"{self.repository}: protected {branch} with required checks: {', '.join(self.checks)}")
        return CheckReconciliation(branch, "created", self.checks, tuple(merged))


def reconcile(cfg, state, repository, *, token: str | None = None, http=request,
              label: str = RUNNER_LABEL, checks=REQUIRED_CHECKS, step: str = STEP) -> CheckReconciliation:
    """Entry point for the bootstrap; a blocked runner is reported, while a failed protection write raises."""
    ci = FactoryCI(cfg, state, repository, token=token, http=http, label=label, checks=checks, step=step)
    readiness = ci.runner_readiness()
    if not readiness.ready:
        log(step, readiness.reason)
    return ci.reconcile_required_checks()
