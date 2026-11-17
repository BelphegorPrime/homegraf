"""An explicitly scoped smoke check of the agent factory: one named, marked, disposable repository.

``python -m hgboot factory-smoke --repo OWNER/REPO --confirm-scratch OWNER/REPO [--keep]`` runs the
factory in a scratch repository and reports the chain ticket -> conversation -> draft pull request ->
CI -> review comment/label -> merged/issue closed, plus a protected-path case that must stay
approved, green, open and free of ``WIP:``.  Nothing here runs during ``hgboot run``: the operator
names the target twice, and the target has to be allowlisted, marked and covered by an explicit
merge policy before the first write.  Writes start only after the token-cost warning, waits are
bounded, and cleanup removes only what this run created.

The stage report carries metadata only (numbers, states, a sanitised conversation URL); issue
bodies stay out of it, and the operator's token and the webhook secret are redacted.
"""
from __future__ import annotations

import base64
import fnmatch
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass, field
from urllib.parse import quote, urlsplit, urlunsplit

from . import factory as factory_step
from . import factory_ci, factory_repos
from .config import Config, ConfigError
from .factory_config import FactoryConfig
from .http import Unreachable, request
from .state import State
from .steps import StepError, log, warn

STEP = "factory-smoke"
# A scratch repository carries this marker in its description; an unmarked repository is never reused.
MARKER = "homegraf factory-smoke scratch"
NAME_PREFIX = "factory-smoke-"
PROTECTED_FIXTURE = "infra/smoke-guard.sh"
PROTECTED_GLOB = "infra/**"
CHECK_CONTEXT = "ci / seed"
DEFAULT_TIMEOUT = 1800.0
DEFAULT_INTERVAL = 15.0
SETTLE_POLLS = 3
PAGE_SIZE = 50

CHAIN = ("ticket", "conversation", "draft pull request", "ci", "review", "merged", "issue closed")

WORKFLOW = """\
# Tests only: this workflow of the factory smoke repository never publishes or deploys anything.
name: ci
on:
  push:
    branches: [main]
  pull_request:
  workflow_dispatch:
concurrency:
  group: ci-${{ github.ref }}
  cancel-in-progress: true
permissions: {}
jobs:
  seed:
    name: seed
    runs-on: docker
    timeout-minutes: 10
    container:
      image: python:3.11-slim-bookworm@sha256:a83ba18aef0f865b1a4a611c2cf460fe446f2795673bbb9fe43be570439e3b39
    steps:
      - name: checkout
        env:
          TOKEN: ${{ github.token }}
        run: |
          git init -q . && git config --global --add safe.directory "$PWD"
          git -c http.extraheader="Authorization: token $TOKEN" fetch -q --no-tags "${{ github.server_url }}/${{ github.repository }}.git" "${{ github.sha }}"
          git checkout -q "${{ github.sha }}"
      - name: check
        run: bash check.sh
"""

CHECK = """\
#!/usr/bin/env sh
set -eu
test -f AGENTS.md
test -f .forgejo/workflows/ci.yaml
grep -q '^# ' README.md
echo "factory smoke check ok"
"""

README = """\
# Factory smoke scratch

This repository is disposable. `hgboot factory-smoke` creates it, runs the agent factory against
it and deletes it again; never keep anything here.
"""

AGENTS_MD = """\
# Factory smoke fixture

This repository belongs to the factory smoke check. Change only the file the issue names, and do
not touch `infra/` unless the issue asks for it: that path is protected by the merge policy.
"""

SEED_FILES = (
    ("README.md", README),
    ("AGENTS.md", AGENTS_MD),
    ("check.sh", CHECK),
    (".forgejo/workflows/ci.yaml", WORKFLOW),
)

USAGE = """\
usage: python -m hgboot factory-smoke --repo OWNER/REPO --confirm-scratch OWNER/REPO [--keep] [--preflight]

Runs the agent factory end to end in one named scratch repository and reports the stages.

  --repo OWNER/REPO            the scratch repository (name starts with factory-smoke-)
  --confirm-scratch OWNER/REPO the same repository, repeated: the guard against a wrong target
  --keep                       keep the repository and its issues and list them as retained
  --preflight                  read-only: check the target, write nothing and spend no tokens
  --timeout SECONDS            bound on each wait (default 1800)
  --interval SECONDS           poll interval (default 15)
  -h, --help                   show this help

Refuses, before the first write: absent or mismatched confirmation, a target outside
FORGEHANDS_ALLOWED_REPOS, the stack's own repository, a name that is not a scratch name, an
existing repository without the smoke marker, and a target without an explicit
FORGEHANDS_MERGE_POLICY entry that denies infra/**.

The run creates issues that start agent runs, and every run spends model tokens. The smoke never
widens the allowlist, the merge policy, the token scopes or any other global permission.
"""

_REPOSITORY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$")
_SCRATCH_NAME = re.compile(r"^factory-smoke-[a-z0-9][a-z0-9-]*$")


class UsageError(Exception):
    pass


class SmokeRefused(StepError):
    """A refusal before the first write; nothing was changed."""


class SmokeFailure(StepError):
    """The factory ran but ended in a state the smoke does not accept."""


class SmokeTimeout(StepError):
    """A wait ran out of its bound."""


@dataclass(frozen=True)
class Stage:
    name: str
    status: str
    detail: str = ""


@dataclass
class SmokeReport:
    repository: str
    stages: list[Stage] = field(default_factory=list)
    retained: tuple[str, ...] = ()
    passed: bool = True

    def add(self, name: str, status: str, detail: str = "") -> None:
        self.stages.append(Stage(name, status, detail))

    def render(self, secrets=()) -> str:
        lines = [f"factory smoke {self.repository}: {'passed' if self.passed else 'failed'}"]
        for stage in self.stages:
            detail = _sanitize(stage.detail, secrets)
            lines.append(f"  {stage.name}: {stage.status}" + (f" ({detail})" if detail else ""))
        if self.retained:
            lines.append("  retained: " + ", ".join(self.retained))
        return "\n".join(lines)


@dataclass(frozen=True)
class Options:
    repo: str = ""
    confirm: str = ""
    keep: bool = False
    preflight: bool = False
    timeout: float = DEFAULT_TIMEOUT
    interval: float = DEFAULT_INTERVAL
    help: bool = False


@dataclass(frozen=True)
class Plan:
    repository: str
    owner: str
    name: str
    existing: bool
    policy: tuple[str, ...]


@dataclass
class Observation:
    labels: tuple[str, ...]
    conversation: str = ""
    pr: dict | None = None
    pr_labels: tuple[str, ...] = ()
    ci: bool = False
    review: bool = False
    merged: bool = False
    closed: bool = False

    @property
    def draft(self) -> bool:
        if not self.pr:
            return False
        return bool(self.pr.get("draft")) or str(self.pr.get("title") or "").startswith("WIP: ")

    @property
    def open(self) -> bool:
        return bool(self.pr) and str(self.pr.get("state") or "open") == "open"

    def reached(self) -> str:
        if self.merged and self.closed:
            return "issue closed"
        if self.review:
            return "review"
        if self.ci:
            return "ci"
        if self.pr:
            return "draft pull request"
        if self.conversation:
            return "conversation"
        return "ticket"

    def chain(self) -> str:
        return " -> ".join(CHAIN[:CHAIN.index(self.reached()) + 1])


def _sanitize(value: str, secrets=()) -> str:
    text = " ".join(str(value).split())
    for secret in secrets:
        if isinstance(secret, str) and secret:
            text = text.replace(secret, "[redacted]")
    return _clean_urls(text)[:300]


def _clean_urls(text: str) -> str:
    """Drops the query and fragment of every URL: a conversation link can carry a token."""

    def clean(match):
        parts = urlsplit(match.group(0))
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))

    return re.sub(r"https?://[^\s)>\]]+", clean, text)


def _repository(value) -> str:
    repository = value.strip() if isinstance(value, str) else ""
    if not _REPOSITORY.fullmatch(repository) or ".." in repository:
        raise UsageError("--repo must be an explicit owner/name repository")
    return repository


def parse_args(argv: list[str]) -> Options:
    values: dict = {}
    flags: set[str] = set()
    index = 0
    while index < len(argv):
        argument = argv[index]
        if argument in ("-h", "--help"):
            return Options(help=True)
        if argument in ("--keep", "--preflight"):
            flags.add(argument)
            index += 1
            continue
        name, _, inline = argument.partition("=")
        if name not in ("--repo", "--confirm-scratch", "--timeout", "--interval"):
            raise UsageError(f"unknown argument {argument!r}")
        if inline:
            value = inline
            index += 1
        else:
            if index + 1 >= len(argv):
                raise UsageError(f"{name} needs a value")
            value = argv[index + 1]
            index += 2
        if name in values:
            raise UsageError(f"{name} is given more than once")
        values[name] = value
    repo = _repository(values.get("--repo", ""))
    confirm = (values.get("--confirm-scratch") or "").strip()
    if not confirm:
        raise UsageError("--confirm-scratch OWNER/REPO is required and must repeat --repo")
    timeout = _seconds(values.get("--timeout"), DEFAULT_TIMEOUT, "--timeout")
    interval = _seconds(values.get("--interval"), DEFAULT_INTERVAL, "--interval")
    return Options(repo=repo, confirm=confirm, keep="--keep" in flags, preflight="--preflight" in flags,
                   timeout=timeout, interval=interval)


def _seconds(value, default: float, name: str) -> float:
    if value is None:
        return default
    try:
        seconds = float(value)
    except ValueError:
        raise UsageError(f"{name} must be a number of seconds") from None
    if not 0 < seconds <= 86400:
        raise UsageError(f"{name} must be between 0 and 86400 seconds")
    return seconds


class _Api:
    """The Forgejo endpoints of the smoke, with the operator's token and no credential in a message."""

    def __init__(self, cfg, token: str, http=request):
        self.base = cfg.forgejo_url.rstrip("/") + "/api/v1"
        self.token = token
        self.http = http

    def headers(self) -> dict:
        return {"Authorization": f"token {self.token}"}

    def call(self, method: str, path: str, *, body=None, allow: tuple = ()):
        try:
            response = self.http(method, self.base + path, body=body, headers=self.headers())
        except Unreachable as exc:
            raise StepError(f"Forgejo is unreachable ({exc})") from None
        if response.status in allow:
            return response
        if not response.ok:
            raise StepError(f"Forgejo answered {response.status} to {method} {path.split('?')[0]}")
        return response

    def json(self, method: str, path: str, *, body=None, allow: tuple = ()):
        response = self.call(method, path, body=body, allow=allow)
        if response.status == 404 and 404 in allow:
            return None
        try:
            return response.json()
        except ValueError:
            if response.status in allow:
                return None
            raise StepError(f"Forgejo did not answer {method} {path.split('?')[0]} with JSON") from None

    def paged(self, path: str) -> list[dict]:
        items: list[dict] = []
        seen: set = set()
        page = 1
        separator = "&" if "?" in path else "?"
        while True:
            batch = self.json("GET", f"{path}{separator}page={page}&limit={PAGE_SIZE}")
            if not isinstance(batch, list):
                raise StepError(f"Forgejo did not answer {path} with a list")
            fresh = []
            for item in batch:
                key = item.get("id") if isinstance(item, dict) else None
                if key is None:
                    key = repr(item)
                if key not in seen:
                    seen.add(key)
                    fresh.append(item)
            if not fresh:
                return items
            items.extend(fresh)
            page += 1


class Smoke:
    """One smoke run against one repository; ``run`` is the only method that writes."""

    def __init__(self, factory, cfg, state, *, token: str, http=request, sleep=time.sleep,
                 clock=time.monotonic, timeout: float = DEFAULT_TIMEOUT, interval: float = DEFAULT_INTERVAL,
                 keep: bool = False, preflight_only: bool = False):
        self.factory, self.cfg, self.state = factory, cfg, state
        self.token = token
        self.http, self.sleep, self.clock = http, sleep, clock
        self.timeout, self.interval = timeout, interval
        self.keep, self.preflight_only = keep, preflight_only
        self.api = _Api(cfg, token, http=http)
        self.secrets = (token, getattr(factory, "forgejo_webhook_secret", ""))
        self.run_token = uuid.uuid4().hex[:8]
        self.report = SmokeReport(cfg.repository)
        self.owned_repository = False
        self.owned_issues: list[int] = []

    # -- preflight (read-only) ---------------------------------------------------------------
    def preflight(self, repository: str, confirm: str) -> Plan:
        repository = _repository(repository)
        self.report.repository = repository
        if not confirm:
            raise SmokeRefused("--confirm-scratch OWNER/REPO is required and must repeat --repo")
        if confirm.strip() != repository:
            raise SmokeRefused("--confirm-scratch does not repeat --repo: refusing to touch a repository the operator did not name twice")
        owner, _, name = repository.partition("/")
        if repository == self.cfg.repository:
            raise SmokeRefused(f"{repository} is the stack's own repository; the smoke needs a disposable scratch repository")
        if not _SCRATCH_NAME.fullmatch(name.lower()):
            raise SmokeRefused(f"{repository}: the name must start with {NAME_PREFIX}; the smoke never touches a production repository")
        if repository not in self.factory.allowed_repos:
            raise SmokeRefused(f"{repository} is not in FORGEHANDS_ALLOWED_REPOS; the smoke never widens the allowlist")
        policy = self.factory.denied_paths(repository)
        if policy is None:
            raise SmokeRefused(f"{repository} has no FORGEHANDS_MERGE_POLICY entry; the protected-path case needs one "
                               "and the smoke never widens the policy")
        if not _covered(PROTECTED_FIXTURE, policy):
            raise SmokeRefused(f"{repository}: the merge policy denies none of {PROTECTED_FIXTURE}; "
                               f"add {PROTECTED_GLOB} to that entry and rerun")
        info = self._repository_info(repository)
        existing = info is not None
        if existing:
            if not _marked(info):
                raise SmokeRefused(f"{repository} exists without the smoke marker in its description; "
                                   "the smoke only reuses a marked scratch repository")
            if info.get("private") is not True:
                raise SmokeRefused(f"{repository} exists but is not private; the smoke only uses a private scratch repository")
            if info.get("mirror"):
                raise SmokeRefused(f"{repository} is a pull mirror and read-only; the smoke needs a writable scratch repository")
        return Plan(repository, owner, name, existing, tuple(policy))

    def _repository_info(self, repository: str) -> dict | None:
        info = self.api.json("GET", f"/repos/{repository}", allow=(404,))
        if info is not None and not isinstance(info, dict):
            raise StepError(f"{repository}: Forgejo did not answer with a repository object")
        return info

    # -- run ---------------------------------------------------------------------------------
    def run(self, repository: str, confirm: str) -> SmokeReport:
        plan = self.preflight(repository, confirm)
        if self.preflight_only:
            action = "reuse" if plan.existing else "create"
            self.report.add("preflight", "read-only", f"would {action} {plan.repository}; nothing written, no tokens spent")
            return self.report
        warn(STEP, "this run creates issues that start agent runs: every run spends model tokens. "
                   "It uses the operator's existing token and widens no allowlist, policy or permission.")
        try:
            self._ensure_repository(plan)
            self._configure(plan)
            self._normal_case(plan)
            self._later_ready_case(plan)
            self._protected_case(plan)
        except (SmokeFailure, SmokeTimeout) as exc:
            self.report.passed = False
            self.report.add("result", "failed", str(exc))
        except (StepError, Unreachable, TimeoutError, ValueError, OSError) as exc:
            self.report.passed = False
            self.report.add("result", "error", str(exc))
        finally:
            try:
                self._cleanup(plan)
            except (StepError, Unreachable, TimeoutError, ValueError, OSError) as exc:
                self.report.passed = False
                self.report.add("cleanup", "failed", str(exc))
        return self.report

    def _ensure_repository(self, plan: Plan) -> None:
        if plan.existing:
            self.report.add("repository", "reused", "marked private scratch repository, files verified")
        else:
            self._create_repository(plan)
            self.owned_repository = True
            self.report.add("repository", "created", plan.repository)
        for path, text in SEED_FILES:
            self._ensure_file(plan.repository, path, text)

    def _create_repository(self, plan: Plan) -> None:
        login = self.api.json("GET", "/user")
        login = login.get("login") if isinstance(login, dict) else None
        body = {"name": plan.name, "private": True, "description": MARKER, "auto_init": False,
                "default_branch": "main"}
        path = "/user/repos" if login == plan.owner else f"/admin/users/{plan.owner}/repos"
        self.api.call("POST", path, body=body)
        log(STEP, f"{plan.repository}: scratch repository created")

    def _ensure_file(self, repository: str, path: str, text: str) -> None:
        current = self.api.json("GET", f"/repos/{repository}/contents/{quote(path, safe='/')}", allow=(404,))
        content = base64.b64encode(text.encode()).decode()
        if current is None:
            self.api.call("POST", f"/repos/{repository}/contents/{quote(path, safe='/')}",
                          body={"content": content, "message": f"factory smoke: seed {path}"})
            return
        if not isinstance(current, dict):
            raise StepError(f"{repository}: Forgejo did not answer the contents of {path}")
        if current.get("content") and base64.b64decode(current["content"]) == text.encode():
            return
        sha = current.get("sha")
        self.api.call("PUT", f"/repos/{repository}/contents/{quote(path, safe='/')}",
                      body={"content": content, "message": f"factory smoke: restore {path}", "sha": sha})

    def _configure(self, plan: Plan) -> None:
        factory_repos.RepositoryReconciler(
            self.cfg, self.state, (plan.repository,), factory_step.FORGEHANDS_ENDPOINT,
            self.factory.forgejo_webhook_secret, token=self.token, http=self.http, step=STEP).run()
        ci = factory_ci.FactoryCI(self.cfg, self.state, plan.repository, token=self.token,
                                  http=self.http, step=STEP)
        readiness = ci.runner_readiness()
        if not readiness.ready:
            raise SmokeFailure(f"{plan.repository}: no usable CI runner: {readiness.reason}")
        checks = ci.reconcile_required_checks()
        self.report.add("configuration", "reconciled",
                        f"labels and webhook, required checks on {checks.branch} ({checks.action})")

    # -- cases -------------------------------------------------------------------------------
    def _normal_case(self, plan: Plan) -> None:
        body = (f"Factory smoke {self.run_token} (normal path).\n\n"
                "Add the line `smoke " + self.run_token + "` at the end of README.md, change nothing else, "
                "and do not touch `infra/`.\n\n"
                "Reply on this issue with the link to your conversation, open a draft pull request that "
                "closes this issue, and let CI and the reviewer finish.")
        number = self._open_issue(plan.repository, "normal", body, ready_at_creation=True)
        observation = self._track_merged(number)
        strong = " with the strong tier (reference)" if "ai:escalate" in observation.labels else ""
        self.report.add("normal path", "passed", f"{observation.chain()}{strong}" + _conversation(observation))

    def _later_ready_case(self, plan: Plan) -> None:
        body = (f"Factory smoke {self.run_token} (ai:ready added later).\n\n"
                "Add the line `later " + self.run_token + "` at the end of README.md, change nothing else, "
                "and do not touch `infra/`.\n\n"
                "Reply on this issue with the link to your conversation, open a draft pull request that "
                "closes this issue, and let CI and the reviewer finish.")
        number = self._open_issue(plan.repository, "ready later", body, ready_at_creation=False)
        self._add_labels(plan.repository, number, ("ai:ready",))
        observation = self._track_merged(number)
        self.report.add("ai:ready added later", "passed", observation.chain())

    def _protected_case(self, plan: Plan) -> None:
        body = (f"Factory smoke {self.run_token} (protected path).\n\n"
                f"Update the comment in `{PROTECTED_FIXTURE}` to mention the smoke run {self.run_token}, "
                "change nothing else.\n\n"
                f"The merge policy of this repository denies `{PROTECTED_GLOB}`: reply on this issue with "
                "the link to your conversation, open a draft pull request that closes this issue, and leave "
                "it open for a human review and merge.")
        number = self._open_issue(plan.repository, "protected path", body, ready_at_creation=True)
        observation = self._track_protected(number)
        self.report.add("protected path", "passed",
                        "approved, green, open, without WIP: " + observation.chain())

    def _open_issue(self, repository: str, slug: str, body: str, *, ready_at_creation: bool) -> int:
        title = f"Factory smoke: {slug} [{self.run_token}]"
        payload = {"title": title, "body": body}
        if ready_at_creation:
            payload["labels"] = self._label_ids(repository, ("ai:ready",))
        issue = self.api.json("POST", f"/repos/{repository}/issues", body=payload)
        number = issue.get("number") if isinstance(issue, dict) else None
        if not isinstance(number, int):
            raise StepError(f"{repository}: Forgejo did not return the created issue")
        self.owned_issues.append(number)
        log(STEP, f"{repository}: issue #{number} opened ({slug})")
        return number

    def _label_ids(self, repository: str, names: tuple[str, ...]) -> list[int]:
        labels = self.api.paged(f"/repos/{repository}/labels")
        by_name = {label.get("name"): label.get("id") for label in labels if isinstance(label, dict)}
        ids = [by_name[name] for name in names if isinstance(by_name.get(name), int)]
        if len(ids) != len(names):
            raise StepError(f"{repository}: the labels {', '.join(names)} are missing after the configuration step")
        return ids

    def _add_labels(self, repository: str, number: int, names: tuple[str, ...]) -> None:
        body = {"labels": self._label_ids(repository, names)}
        self.api.call("POST", f"/repos/{repository}/issues/{number}/labels", body=body)

    # -- observation and bounded waits -------------------------------------------------------
    def _observe(self, repository: str, number: int) -> Observation:
        issue = self.api.json("GET", f"/repos/{repository}/issues/{number}")
        if not isinstance(issue, dict):
            raise StepError(f"{repository}: Forgejo did not answer issue #{number}")
        labels = tuple(label.get("name", "") for label in issue.get("labels") or [] if isinstance(label, dict))
        comments = self.api.paged(f"/repos/{repository}/issues/{number}/comments")
        conversation = ""
        for comment in comments:
            user = comment.get("user") if isinstance(comment.get("user"), dict) else {}
            body = str(comment.get("body") or "")
            if user.get("login") == self.factory.agent_login and "conversation" in body.lower():
                conversation = _first_url(body)
                if conversation:
                    break
        observation = Observation(labels=labels, conversation=conversation,
                                  closed=str(issue.get("state") or "open") == "closed")
        pulls = self.api.json("GET", f"/repos/{repository}/pulls?state=all&limit={PAGE_SIZE}")
        pr = self._find_pull(pulls if isinstance(pulls, list) else [], number)
        if pr is None:
            return observation
        detail = self.api.json("GET", f"/repos/{repository}/pulls/{pr.get('number')}")
        if isinstance(detail, dict):
            pr = detail
        observation.pr = pr
        observation.pr_labels = tuple(label.get("name", "") for label in pr.get("labels") or [] if isinstance(label, dict))
        observation.merged = bool(pr.get("merged") or pr.get("merged_at"))
        head = pr.get("head") if isinstance(pr.get("head"), dict) else {}
        sha = head.get("sha")
        if sha:
            status = self.api.json("GET", f"/repos/{repository}/commits/{sha}/status") or {}
            statuses = status.get("statuses") if isinstance(status, dict) else None
            observation.ci = any(str(s.get("context") or "").startswith(CHECK_CONTEXT) and s.get("state") == "success"
                                 for s in statuses or [] if isinstance(s, dict))
        reviews = self.api.json("GET", f"/repos/{repository}/pulls/{pr.get('number')}/reviews") or []
        observation.review = "ai:approved" in observation.pr_labels or any(
            str(review.get("state") or "").upper() == "APPROVED" for review in reviews if isinstance(review, dict))
        return observation

    @staticmethod
    def _find_pull(pulls: list, number: int) -> dict | None:
        for pull in pulls:
            if not isinstance(pull, dict):
                continue
            head = pull.get("head") if isinstance(pull.get("head"), dict) else {}
            body = str(pull.get("body") or "")
            reference = f"#{number}"
            if reference in body or str(head.get("ref") or "").endswith(f"issue-{number}"):
                return pull
        return None

    def _track_merged(self, number: int) -> Observation:
        repository = self.report.repository
        deadline = self.clock() + self.timeout
        while True:
            observation = self._observe(repository, number)
            if observation.merged and observation.closed and observation.ci and observation.review:
                return observation
            if observation.pr and str(observation.pr.get("state") or "") == "closed" and not observation.merged:
                raise SmokeFailure(f"#{number}: the pull request was closed without a merge ({observation.chain()})")
            if observation.closed and not observation.merged:
                raise SmokeFailure(f"#{number}: the issue was closed without a merged pull request ({observation.chain()})")
            self._bound(deadline, f"#{number}: stalled at {observation.reached()}, reached {observation.chain()}")
            self.sleep(self.interval)

    def _track_protected(self, number: int) -> Observation:
        repository = self.report.repository
        deadline = self.clock() + self.timeout
        settled = 0
        while True:
            observation = self._observe(repository, number)
            if observation.merged:
                raise SmokeFailure(f"#{number}: the protected path was merged; it must stay open for a human merge")
            if observation.pr and str(observation.pr.get("state") or "") == "closed":
                raise SmokeFailure(f"#{number}: the protected pull request was closed without a human review")
            ready = observation.open and observation.review and observation.ci and not observation.draft
            settled = settled + 1 if ready else 0
            if settled >= SETTLE_POLLS:
                return observation
            self._bound(deadline, f"#{number}: protected path stalled at {observation.reached()} "
                                  f"({'draft ' if observation.draft else ''}approved={observation.review}, "
                                  f"green={observation.ci}, open={observation.open})")
            self.sleep(self.interval)

    def _bound(self, deadline: float, message: str) -> None:
        if self.clock() >= deadline:
            raise SmokeTimeout(message + f" (waited {int(self.timeout)} s)")

    # -- cleanup -----------------------------------------------------------------------------
    def _cleanup(self, plan: Plan) -> None:
        if self.keep:
            self.report.retained = tuple([plan.repository] + [f"#{number}" for number in self.owned_issues])
            self.report.add("cleanup", "kept", "--keep: the run leaves its artifacts in place")
            return
        if self.owned_repository:
            self.api.call("DELETE", f"/repos/{plan.repository}")
            self.report.add("cleanup", "deleted", "the scratch repository created by this run")
            return
        failed = []
        for number in self.owned_issues:
            try:
                self.api.call("DELETE", f"/repos/{plan.repository}/issues/{number}")
            except (StepError, Unreachable, TimeoutError, ValueError, OSError) as exc:
                failed.append(f"#{number} ({exc})")
        detail = "issues created by this run" if not failed else f"could not delete {', '.join(failed)}"
        self.report.add("cleanup", "deleted" if not failed else "incomplete", detail)


def _marked(info: dict) -> bool:
    return MARKER in str(info.get("description") or "")


def _covered(path: str, globs) -> bool:
    return any(fnmatch.fnmatchcase(path, glob) for glob in globs)


def _first_url(text: str) -> str:
    match = re.search(r"https?://[^\s)>\]]+", text)
    return match.group(0) if match else ""


def _conversation(observation: Observation) -> str:
    if not observation.conversation:
        return ""
    return ", conversation on " + _clean_urls(observation.conversation)


def command(argv: list[str], env=os.environ) -> int:
    """The ``factory-smoke`` subcommand: usage 2, refusals 2, a failed smoke 1, a pass 0."""
    try:
        options = parse_args(argv)
    except UsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print(USAGE.split("\n\n")[0], file=sys.stderr)
        return 2
    if options.help:
        print(USAGE.strip())
        return 0
    try:
        cfg = Config.from_env(env)
        factory = FactoryConfig.from_env(env, cfg.repository)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not factory.enabled:
        print("error: the factory is not opted in (FORGEHANDS_BOOTSTRAP=1); the smoke never activates it", file=sys.stderr)
        return 2
    state = State(f"{cfg.state_dir}/state.json")
    token = state.get("forgejo", "token")
    if not isinstance(token, str) or not token.strip():
        print("error: the operator's Forgejo token is missing from the state: run the Forgejo seed first", file=sys.stderr)
        return 2
    smoke = Smoke(factory, cfg, state, token=token, keep=options.keep, preflight_only=options.preflight,
                  timeout=options.timeout, interval=options.interval)
    try:
        report = smoke.run(options.repo, options.confirm)
    except SmokeRefused as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(report.render(smoke.secrets), flush=True)
    return 0 if report.passed else 1
