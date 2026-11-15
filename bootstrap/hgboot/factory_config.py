"""The factory's opt-in settings and merge policy, apart from the stack's ``Config``.

``FactoryConfig.from_env(env, repository)`` reads them.  Only ``FORGEHANDS_BOOTSTRAP=1`` opts in;
an unset or false flag leaves the factory disabled and reads none of the new variables, so a
legacy installation keeps booting exactly as before.  The agents profile alone never opts in.

Field contract (environment -> attribute, reused names first)::

    FORGEHANDS_BOOTSTRAP=1                     enabled
    FORGEHANDS_OPENHANDS_API_URL               openhands_url              required
    OPENHANDS_LOCAL_BACKEND_API_KEY            openhands_backend_key      required
    FORGEHANDS_STRONG_MODEL                    strong_model               required
    FORGEHANDS_CHEAP_MODEL                     cheap_model                required
    FORGEHANDS_GATEWAY_URL                     gateway_url                required
    FORGEHANDS_GATEWAY_API_KEY                 gateway_api_key            required
    FORGEHANDS_ALLOWED_REPOS                   allowed_repos              required, comma separated
    FORGEHANDS_ALLOWED_SENDERS                 allowed_senders            required, comma separated
    FORGEHANDS_FORGEJO_AGENT_TOKEN             forgejo_agent_token        FORGEJO_TOKEN falls back
    FORGEHANDS_FORGEJO_WEBHOOK_SECRET          forgejo_webhook_secret     required
    FORGEHANDS_MIRROR_URL                      mirror_url                 optional
    FORGEHANDS_MIRROR_TOKEN                    mirror_token               optional
    FORGEHANDS_RUNTIME_DIR                     runtime_dir                /factory-runtime
    FORGEHANDS_MAX_CONCURRENT_PER_TIER         max_concurrent_per_tier    2
    FORGEHANDS_MAX_CONCURRENT                  max_concurrent             4
    FORGEHANDS_MAX_RUNS_PER_HOUR               max_runs_per_hour          30
    FORGEHANDS_MAX_RUN_MINUTES                 max_run_minutes            120
    FORGEHANDS_MERGE_POLICY                    merge_policy               JSON, see below

``FORGEHANDS_MERGE_POLICY`` is a JSON object ``repository -> {"deny": [globs]}``.  A repository
without an entry stays manual (``repository_merge_policy()`` returns ``None``); an explicit
``{}`` stays empty.  Only when the variable is unset does a fresh opt-in get
``DEFAULT_MERGE_DENY`` for its own repository.  Operator policy is never replaced by the
default and is never read from a repository file.

Secrets (the backend key, gateway key, agent token, webhook secret and mirror token) are
excluded from ``repr()``; validation errors name the variable, never its value.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .config import ConfigError

OPT_IN = "FORGEHANDS_BOOTSTRAP"
DEFAULT_RUNTIME_DIR = "/factory-runtime"
DEFAULT_MERGE_DENY = (
    ".forgejo/**",
    ".github/**",
    "AGENTS.md",
    "CLAUDE.md",
    "CONTRIBUTING.md",
    ".agents/**",
    ".claude/**",
    "compose.yaml",
    "**/compose.yaml",
    "setup.sh",
    "scripts/**",
    "infra/**",
    "stacks/**",
)
MAX_CONCURRENT = 1000
MAX_RUNS_PER_HOUR = 100000
MAX_RUN_MINUTES = 10080

_REPO_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SENDER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,38}$")
_MODEL_MAX = 200
_URL_MAX = 2048
_GLOB_MAX = 500
_SECRET_MAX = 4096


class FactoryConfigError(ConfigError):
    """The factory was opted in with input that would make an unattended run unsafe."""


@dataclass
class FactoryConfig:
    enabled: bool
    repository: str
    openhands_url: str = "http://openhands:8000"
    openhands_backend_key: str = field(default="", repr=False)
    strong_model: str = ""
    cheap_model: str = ""
    gateway_url: str = ""
    gateway_api_key: str = field(default="", repr=False)
    allowed_repos: tuple[str, ...] = ()
    allowed_senders: tuple[str, ...] = ()
    forgejo_agent_token: str = field(default="", repr=False)
    forgejo_webhook_secret: str = field(default="", repr=False)
    mirror_url: str = ""
    mirror_token: str = field(default="", repr=False)
    runtime_dir: str = DEFAULT_RUNTIME_DIR
    max_concurrent_per_tier: int = 2
    max_concurrent: int = 4
    max_runs_per_hour: int = 30
    max_run_minutes: int = 120
    merge_policy: dict[str, dict[str, tuple[str, ...]]] = field(default_factory=dict)

    def repository_merge_policy(self, repository: str) -> dict[str, tuple[str, ...]] | None:
        """The deny rule of one repository; None means merges there stay manual."""
        return self.merge_policy.get(repository)

    def denied_paths(self, repository: str) -> tuple[str, ...] | None:
        """The protected-path globs of one repository; None means manual, () means nothing denied."""
        rule = self.repository_merge_policy(repository)
        return None if rule is None else rule["deny"]

    @classmethod
    def from_env(cls, env, repository: str) -> "FactoryConfig":
        disabled = cls(enabled=False, repository=(repository or "").strip())
        flag = (env.get(OPT_IN) or "").strip().lower()
        if flag in ("", "0"):
            return disabled
        if flag != "1":
            raise FactoryConfigError(f"{OPT_IN} must be 1 to opt in, or unset")
        repository = _repository((repository or "").strip(), "repository")
        openhands_url = _url(env, "FORGEHANDS_OPENHANDS_API_URL")
        gateway_url = _url(env, "FORGEHANDS_GATEWAY_URL")
        mirror_url = _url(env, "FORGEHANDS_MIRROR_URL", required=False)
        mirror_token = _secret(env, "FORGEHANDS_MIRROR_TOKEN", required=False)
        if mirror_token:
            if not mirror_url:
                raise FactoryConfigError("FORGEHANDS_MIRROR_URL is not set while FORGEHANDS_MIRROR_TOKEN is")
            if urlsplit(mirror_url).scheme != "https":
                raise FactoryConfigError("FORGEHANDS_MIRROR_URL must use https when FORGEHANDS_MIRROR_TOKEN is set")
        max_concurrent_per_tier = _limit(env, "FORGEHANDS_MAX_CONCURRENT_PER_TIER", 2, MAX_CONCURRENT)
        max_concurrent = _limit(env, "FORGEHANDS_MAX_CONCURRENT", 4, MAX_CONCURRENT)
        if max_concurrent_per_tier > max_concurrent:
            raise FactoryConfigError("FORGEHANDS_MAX_CONCURRENT_PER_TIER must not exceed FORGEHANDS_MAX_CONCURRENT")
        agent_token = (_secret(env, "FORGEHANDS_FORGEJO_AGENT_TOKEN", required=False)
                       or _secret(env, "FORGEJO_TOKEN", required=False))
        if not agent_token:
            raise FactoryConfigError("FORGEHANDS_FORGEJO_AGENT_TOKEN (or FORGEJO_TOKEN) is not set "
                                     f"(the factory is opted in with {OPT_IN}=1)")
        return cls(
            enabled=True,
            repository=repository,
            openhands_url=openhands_url,
            openhands_backend_key=_secret(env, "OPENHANDS_LOCAL_BACKEND_API_KEY"),
            strong_model=_model(env, "FORGEHANDS_STRONG_MODEL"),
            cheap_model=_model(env, "FORGEHANDS_CHEAP_MODEL"),
            gateway_url=gateway_url,
            gateway_api_key=_secret(env, "FORGEHANDS_GATEWAY_API_KEY"),
            allowed_repos=_repositories(env, "FORGEHANDS_ALLOWED_REPOS"),
            allowed_senders=_senders(env, "FORGEHANDS_ALLOWED_SENDERS"),
            forgejo_agent_token=agent_token,
            forgejo_webhook_secret=_secret(env, "FORGEHANDS_FORGEJO_WEBHOOK_SECRET"),
            mirror_url=mirror_url,
            mirror_token=mirror_token,
            runtime_dir=_runtime_dir(env),
            max_concurrent_per_tier=max_concurrent_per_tier,
            max_concurrent=max_concurrent,
            max_runs_per_hour=_limit(env, "FORGEHANDS_MAX_RUNS_PER_HOUR", 30, MAX_RUNS_PER_HOUR),
            max_run_minutes=_limit(env, "FORGEHANDS_MAX_RUN_MINUTES", 120, MAX_RUN_MINUTES),
            merge_policy=_merge_policy(env, repository),
        )


def _present(env, name: str) -> str:
    value = env.get(name)
    return value.strip() if isinstance(value, str) else ""


def _required(env, name: str) -> str:
    value = _present(env, name)
    if not value:
        raise FactoryConfigError(f"{name} is not set (the factory is opted in with {OPT_IN}=1)")
    return value


def _secret(env, name: str, *, required: bool = True) -> str:
    value = _present(env, name)
    if not value:
        if required:
            raise FactoryConfigError(f"{name} is not set (the factory is opted in with {OPT_IN}=1)")
        return ""
    if len(value) > _SECRET_MAX or any(c in value for c in "\r\n\x00"):
        raise FactoryConfigError(f"{name} is not a usable secret")
    return value


def _model(env, name: str) -> str:
    value = _required(env, name)
    if len(value) > _MODEL_MAX or any(c.isspace() or ord(c) < 32 for c in value):
        raise FactoryConfigError(f"{name} is not a model identifier")
    return value


def _url(env, name: str, *, default: str = "", required: bool = True) -> str:
    value = _present(env, name) or default
    if not value:
        if required:
            raise FactoryConfigError(f"{name} is not set (the factory is opted in with {OPT_IN}=1)")
        return ""
    if len(value) > _URL_MAX or any(c.isspace() or ord(c) < 32 for c in value):
        raise FactoryConfigError(f"{name} must be a URL without embedded credentials")
    try:
        parts = urlsplit(value)
        parts.port                                                          # a bad port raises here
    except ValueError:
        raise FactoryConfigError(f"{name} must be a URL without embedded credentials") from None
    if (parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password
            or "@" in parts.netloc):
        raise FactoryConfigError(f"{name} must be a URL without embedded credentials")
    if parts.query or parts.fragment:
        raise FactoryConfigError(f"{name} must be a base URL without a query or fragment")
    return value.rstrip("/")


def _runtime_dir(env) -> str:
    value = (_present(env, "FORGEHANDS_RUNTIME_DIR") or DEFAULT_RUNTIME_DIR).rstrip("/")
    parts = value.split("/")
    if (not os.path.isabs(value) or value in ("", "/") or any(c in value for c in "\r\n\x00")
            or any(part in ("", ".", "..") for part in parts[1:]) or len(value) > _URL_MAX):
        raise FactoryConfigError("FORGEHANDS_RUNTIME_DIR must be an absolute directory below the root")
    return value


def _limit(env, name: str, default: int, maximum: int) -> int:
    raw = _present(env, name)
    if not raw:
        return default
    if not raw.isascii() or not raw.isdigit():
        raise FactoryConfigError(f"{name} must be an integer between 1 and {maximum}")
    value = int(raw)
    if not 1 <= value <= maximum:
        raise FactoryConfigError(f"{name} must be an integer between 1 and {maximum}")
    return value


def _repository(value: str, name: str) -> str:
    owner, sep, repo = value.partition("/")
    if (not sep or "/" in repo or not _REPO_PART.fullmatch(owner) or not _REPO_PART.fullmatch(repo)
            or owner in (".", "..") or repo in (".", "..") or len(owner) > 100 or len(repo) > 100):
        raise FactoryConfigError(f"{name} must be a Forgejo repository as owner/name")
    return value


def _list(env, name: str, item, label: str) -> tuple[str, ...]:
    raw = _required(env, name)
    values = []
    for part in raw.split(","):
        value = part.strip()
        if not value:
            raise FactoryConfigError(f"{name} must be a comma-separated list of {label} without empty entries")
        value = item(value, name)
        if value not in values:
            values.append(value)
    return tuple(values)


def _repositories(env, name: str) -> tuple[str, ...]:
    return _list(env, name, _repository, "repositories")


def _senders(env, name: str) -> tuple[str, ...]:
    def sender(value, field_name):
        if not _SENDER.fullmatch(value):
            raise FactoryConfigError(f"{field_name} must be a comma-separated list of Forgejo user names")
        return value
    return _list(env, name, sender, "user names")


def _glob(value, repository: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > _GLOB_MAX:
        raise FactoryConfigError(f"FORGEHANDS_MERGE_POLICY deny globs of {repository} must be non-empty paths")
    if ("\\" in value or value.startswith("/") or any(ord(c) < 32 or ord(c) == 127 for c in value)
            or any(part in (".", "..") for part in value.split("/"))):
        raise FactoryConfigError(f"FORGEHANDS_MERGE_POLICY deny globs of {repository} must be relative paths")
    return value


def _merge_policy(env, repository: str) -> dict[str, dict[str, tuple[str, ...]]]:
    raw = _present(env, "FORGEHANDS_MERGE_POLICY")
    if not raw:
        return {repository: {"deny": DEFAULT_MERGE_DENY}}

    def no_duplicates(pairs):
        seen = {}
        for key, value in pairs:
            if key in seen:
                raise FactoryConfigError("FORGEHANDS_MERGE_POLICY repeats a repository")
            seen[key] = value
        return seen

    try:
        document = json.loads(raw, object_pairs_hook=no_duplicates)
    except (ValueError, TypeError):
        raise FactoryConfigError("FORGEHANDS_MERGE_POLICY must be a JSON object of repository -> {deny: [globs]}") from None
    if not isinstance(document, dict):
        raise FactoryConfigError("FORGEHANDS_MERGE_POLICY must be a JSON object of repository -> {deny: [globs]}")
    policy = {}
    for name, rule in document.items():
        name = _repository(name, "FORGEHANDS_MERGE_POLICY repository")
        if not isinstance(rule, dict) or set(rule) != {"deny"}:
            raise FactoryConfigError(f"FORGEHANDS_MERGE_POLICY entry for {name} must be exactly {{deny: [globs]}}")
        if not isinstance(rule["deny"], list):
            raise FactoryConfigError(f"FORGEHANDS_MERGE_POLICY deny of {name} must be a list of globs")
        denied = []
        for value in rule["deny"]:
            value = _glob(value, name)
            if value not in denied:
                denied.append(value)
        policy[name] = {"deny": tuple(denied)}
    return policy
