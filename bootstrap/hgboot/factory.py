"""Connect the factory's reconcilers, in the order that keeps a half-done setup from activating direct mode.

The operator's credential performs the operator-level steps (#72 agent user, #52 labels and webhooks,
#53 mirror, #54 CI).  The profiles (#50) follow, and only a complete run publishes the runtime handoff
that ForgeHands reads (#56).  A profile readback, hook, runner or mirror problem leaves ``ready`` false:
a rerun is idempotent and repairs what the previous run left behind.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import tempfile
import time
import uuid
from dataclasses import dataclass
from urllib.parse import urlsplit

from . import agent_profiles, agent_user, agents, factory_ci, factory_repos, push_mirror
from .http import Unreachable, request
from .steps import StepError, log, warn

STEP = "factory"
RUNTIME_FILE = "forgehands.json"
SECTION = "factory_agent"
TOKEN_KEY = agent_user.TOKEN
REPOSITORIES_KEY = "repositories"
LOGIN_KEY = "login"
MAX_RUNTIME_BYTES = 64 * 1024
ROUTE_LABELS = (
    "ai:plan", "ai:ready", "ai:ready-free", "ai:escalate", "ai:review",
    "ai:changes", "ai:changes-free", "ci:failure", "ci:failure-free",
    "ci:success", "epic:done", "pr:conflict", "sensitive:review",
)
ROUTE_ROLES = ("planner", "worker", "worker-strong", "reviewer", "sensitive-reviewer")
FORGEHANDS_ENDPOINT = "http://forgehands:8080/webhooks/forgejo"
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
_LOGIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,38}$")
_LAUNCHER_REPOSITORY = re.compile(r"^[a-z0-9_.-]+/[a-z0-9_.-]+$")
_MAX_ROUNDS = 100
_LAUNCHER_LIMITS = {"max_concurrent_per_tier": 100, "max_concurrent": 100, "max_runs_per_hour": 10000,
                    "max_run_minutes": 1440}
_RESERVED_AGENT_SECRETS = {"PATH", "HOME", "PWD", "SHELL", "TMPDIR", "USER", "HOSTNAME", "NODE_OPTIONS",
                           "NODE_PATH", "LD_PRELOAD", "LD_LIBRARY_PATH", "DYLD_INSERT_LIBRARIES"}
_RUNTIME_NAMES = {
    "FORGEJO_URL", "FORGEJO_TOKEN", "FORGEJO_WEBHOOK_SECRET", "OPENHANDS_API_URL", "OPENHANDS_API_KEY",
    "AGENT_SECRETS", "LABEL_ROUTES", "ALLOWED_REPOS", "ALLOWED_SENDERS", "HUMAN_LOGIN", "MERGE_POLICY",
    "MAX_CONCURRENT_PER_TIER", "MAX_CONCURRENT", "MAX_RUNS_PER_HOUR", "MAX_RUN_MINUTES", "MAX_ROUNDS",
}


@dataclass(frozen=True)
class Stage:
    name: str
    status: str


@dataclass(frozen=True)
class FactoryReport:
    agent_login: str
    repositories: tuple[str, ...]
    profiles: int
    routes: int
    runner: str
    mirror: str
    runtime_file: str
    stages: tuple[Stage, ...]


class _AgentStore:
    """The agent user step's small store over State, so its token never lands in the operator's slot."""

    def __init__(self, state, configured: str = ""):
        self.state = state
        self.configured = configured or ""

    def get(self, name):
        stored = self.state.get(SECTION, name)
        if name == TOKEN_KEY:
            return (stored if isinstance(stored, str) and stored else "") or self.configured
        return stored

    def set(self, name, value):
        self.state.set(SECTION, name, value)

    def token(self) -> str:
        return self.get(TOKEN_KEY)

    def repositories(self) -> list[str]:
        stored = self.state.get(SECTION, REPOSITORIES_KEY)
        return list(stored) if isinstance(stored, list) else []


def run(factory, cfg, state, env=os.environ, *, http=request, sleep=time.sleep,
        mirror_attempts: int = 30) -> FactoryReport | None:
    """Reconcile everything and publish the ready handoff last.  The caller parses FactoryConfig first."""
    if not factory.enabled:
        return None
    operator_token = _operator_token(state)
    store = _AgentStore(state, getattr(factory, "forgejo_agent_token", ""))
    stages: list[Stage] = []
    stage = "preflight"
    try:
        _validate_contract(factory, env)
        _validate_runtime_dir(factory.runtime_dir)
        mirror = _preflight(factory, cfg, state, operator_token, http, mirror_attempts, sleep)
        warning = mailer_warning(env, factory.human_login)
        if warning:
            warn(STEP, warning)

        stage = "agent user"
        token, login = _ensure_agent(factory, cfg, store, operator_token, http)
        stages.append(Stage(stage, "configured"))

        stage = "labels and hooks"
        _reconcile_repositories(factory, cfg, state, operator_token, http)
        stages.append(Stage(stage, "configured"))

        stage = "GitHub mirror"
        mirror_status = "off (not configured)"
        if mirror is not None:
            mirror.sync()
            mirror_status = "configured"
        stages.append(Stage(stage, mirror_status))

        stage = "CI"
        runner = _reconcile_ci(factory, cfg, state, operator_token, http)
        stages.append(Stage(stage, "configured"))

        stage = "profiles"
        ids = agent_profiles.AgentProfiles(factory, state, http=http).run()
        _verified_roles(ids)
        stages.append(Stage(stage, "configured"))

        stage = "routes"
        routes = _routes(ids)
        table = json.loads(routes)
        stages.append(Stage(stage, "configured"))

        stage = "runtime handoff"
        runtime_env = _runtime_env(factory, cfg, env, token, routes)
        runtime_file = _write_runtime(factory.runtime_dir, True, runtime_env)
        stages.append(Stage(stage, "published"))
        log(STEP, f"direct mode is ready: {runtime_file}")
        return FactoryReport(login, factory.allowed_repos, len({route["profile"] for route in table.values()}),
                             len(table), runner, mirror_status, runtime_file, tuple(stages))
    except Exception as exc:
        _invalidate(factory.runtime_dir)
        if isinstance(exc, (StepError, Unreachable, TimeoutError, ValueError, OSError)):
            raise StepError(f"factory: {stage} blocked: {_sanitize(str(exc), factory, state, env)}") from None
        raise


def mailer_warning(env, human_login="") -> str | None:
    """A mailer that Forgejo does not have turns the mention handover into a silent no-op; warn, never fail."""
    enabled = env.get("FORGEJO_MAILER_ENABLED", "").strip().lower()
    if enabled in ("true", "1", "yes"):
        return None
    login = (human_login or env.get("FORGEHANDS_HUMAN_LOGIN") or "").strip() or "the human"
    return (f"Forgejo mailer looks unconfigured (FORGEJO_MAILER_ENABLED is not true): a mention of {login} "
            "will not be mailed. If you configure [mailer] in app.ini instead, ignore this.")


def _operator_token(state) -> str:
    token = state.get("forgejo", "token")
    if not isinstance(token, str) or not token.strip():
        raise StepError("the operator's Forgejo token is missing from the state: run the Forgejo seed first")
    return token


def _validate_contract(factory, env) -> None:
    """The same bounds the launcher enforces, checked before the first remote write."""
    if len(factory.forgejo_webhook_secret) < 16:
        raise StepError("FORGEHANDS_FORGEJO_WEBHOOK_SECRET must be at least 16 characters")
    for repository in factory.allowed_repos:
        if not _LAUNCHER_REPOSITORY.fullmatch(repository):
            raise StepError("allowed repositories must be lower-case owner/name")
    for repository in factory.merge_policy:
        if not _LAUNCHER_REPOSITORY.fullmatch(repository):
            raise StepError("FORGEHANDS_MERGE_POLICY keys must be lower-case owner/name")
    for login in (*factory.allowed_senders, factory.human_login):
        if not _LOGIN.fullmatch(login):
            raise StepError("agent and human logins must be Forgejo user names")
    for attribute, maximum in _LAUNCHER_LIMITS.items():
        value = getattr(factory, attribute)
        if not isinstance(value, int) or not 1 <= value <= maximum:
            raise StepError(f"{attribute} must be between 1 and {maximum}")
    _max_rounds(env)
    for name in _agent_secrets(env).split(","):
        if name in _RESERVED_AGENT_SECRETS:
            raise StepError(f"AGENT_SECRETS must not name the process setting {name}")
        if name not in _RUNTIME_NAMES and not env.get(name):
            raise StepError(f"AGENT_SECRETS names {name}, but the bootstrap has no value for it")


def _validate_runtime_dir(value: str) -> None:
    path = pathlib.Path(value)
    if not path.is_absolute() or path == pathlib.Path("/"):
        raise StepError("FORGEHANDS_RUNTIME_DIR must be an absolute directory below the root")
    if path.exists() and not path.is_dir():
        raise StepError("FORGEHANDS_RUNTIME_DIR is not a directory")


def _preflight(factory, cfg, state, operator_token, http, mirror_attempts, sleep):
    """Read-only checks before the first write: explicit repositories, their runner and the mirror guard."""
    for repository in factory.allowed_repos:
        factory_repos.RepositoryReconciler(
            cfg, state, (repository,), "http://factory.invalid", "unused-webhook-secret",
            token=operator_token, http=http, step=STEP)._writable_primary(repository)
        readiness = factory_ci.FactoryCI(cfg, state, repository, token=operator_token, http=http,
                                         step=STEP).runner_readiness()
        if not readiness.ready:
            raise StepError(f"{repository}: {readiness.reason}")
    return _mirror(factory, cfg, operator_token, http, mirror_attempts=mirror_attempts, sleep=sleep)


def _mirror(factory, cfg, operator_token, http, *, mirror_attempts, sleep):
    if not factory.mirror_url:
        return None
    parts = urlsplit(factory.mirror_url)
    repository = parts.path.strip("/")
    if repository.endswith(".git"):
        repository = repository[:-4]
    if not repository:
        raise StepError("FORGEHANDS_MIRROR_URL must name a GitHub repository as owner/name")
    origin = f"{parts.scheme}://{parts.netloc}"
    api = "https://api.github.com" if parts.hostname == "github.com" else origin + "/api/v3"
    mirror = push_mirror.PushMirror(
        forgejo_url=cfg.forgejo_url, forgejo_repo=factory.repository, forgejo_token=operator_token,
        github_repo=repository, github_token=factory.mirror_token, github_url=origin, github_api=api,
        http=http, sleep=sleep, attempts=mirror_attempts)
    mirror._guard()                 # read-only: refuses extra GitHub branches before any mirror write
    return mirror


def _ensure_agent(factory, cfg, store, operator_token, http) -> tuple[str, str]:
    token = store.token()
    if not token or not _agent_ready(factory, cfg, store, token, http):
        agent_user.ensure_agent_user(
            cfg, store, http=http, human=factory.human_login, login=factory.agent_login,
            repositories=factory.allowed_repos)
        store.state.set(SECTION, REPOSITORIES_KEY, list(factory.allowed_repos))
        store.state.set(SECTION, LOGIN_KEY, factory.agent_login)
        token = store.token()
    if not token:
        raise StepError("the agent user step produced no token")
    if any(character.isspace() for character in token):
        raise StepError("the agent token contains white space")
    if token == operator_token:
        raise StepError("the agent token is the operator token; the factory must use the agent user's own token")
    identity = _identity(token, cfg, http)
    login = identity.get("login")
    if identity.get("is_admin") is not False:
        warn(STEP, f"the token belongs to {login}, an administrator; the factory needs the agent user")
        raise StepError(f"the agent token belongs to {login}, which is an administrator")
    if login != factory.agent_login:
        raise StepError(f"the agent token belongs to {login}, not to {factory.agent_login}")
    if str(login).casefold() == str(factory.human_login).casefold():
        raise StepError(f"the agent token belongs to {login}, the human")
    return token, login


def _agent_ready(factory, cfg, store, token, http) -> bool:
    try:
        identity = _identity(token, cfg, http)
    except StepError:
        return False
    if identity.get("login") != factory.agent_login or identity.get("is_admin") is not False:
        return False
    try:
        refused = http("GET", cfg.forgejo_url.rstrip("/") + "/api/v1/admin/users",
                       headers={"Authorization": f"token {token}"})
    except Unreachable:
        return False
    if refused.status not in (401, 403):
        return False
    return store.repositories() == list(factory.allowed_repos)


def _identity(token, cfg, http) -> dict:
    try:
        response = http("GET", cfg.forgejo_url.rstrip("/") + "/api/v1/user",
                        headers={"Authorization": f"token {token}"})
    except Unreachable as exc:
        raise StepError(f"Forgejo is unreachable while checking the agent token ({exc})") from None
    if not response.ok:
        raise StepError(f"Forgejo refused the agent token ({response.status})")
    try:
        identity = response.json()
    except ValueError:
        raise StepError("Forgejo did not answer the agent token with JSON") from None
    if not isinstance(identity, dict):
        raise StepError("Forgejo did not answer the agent token with a user object")
    return identity


def _reconcile_repositories(factory, cfg, state, operator_token, http) -> None:
    factory_repos.reconcile(cfg, state, factory.allowed_repos,
                            FORGEHANDS_ENDPOINT, factory.forgejo_webhook_secret,
                            token=operator_token, http=http, step=STEP)


def _reconcile_ci(factory, cfg, state, operator_token, http) -> str:
    runner = ""
    for repository in factory.allowed_repos:
        ci = factory_ci.FactoryCI(cfg, state, repository, token=operator_token, http=http, step=STEP)
        readiness = ci.runner_readiness()
        if not readiness.ready:
            raise StepError(f"{repository}: {readiness.reason}")
        runner = readiness.reason
        ci.reconcile_required_checks()
        after = ci.runner_readiness()
        if not after.ready:
            raise StepError(f"{repository}: {after.reason}")
    return runner


def _verified_roles(ids) -> None:
    if not isinstance(ids, dict):
        raise StepError("the profile step returned no role ids")
    seen = set()
    for role in ROUTE_ROLES:
        profile_id = ids.get(role)
        if not isinstance(profile_id, str) or not profile_id.strip():
            raise StepError(f"the profile step did not verify an id for {role}")
        try:
            uuid.UUID(profile_id)
        except ValueError:
            raise StepError(f"the profile id of {role} is not a UUID") from None
        if profile_id in seen:
            raise StepError(f"the profile id of {role} is shared with another role")
        seen.add(profile_id)


def _routes(ids) -> str:
    """Publish every route `label_routes()` builds: ForgeHands 0.4.2 refuses to start when a target is missing."""
    routes = json.loads(agents.label_routes(ids))
    missing = [label for label in ROUTE_LABELS if label not in routes and label not in agents.FREE_ROUTE_LABELS]
    if missing:
        raise StepError(f"the route table has no entry for {', '.join(missing)}")
    for label, route in routes.items():
        for key in ("fallback_to", "escalate_to"):
            target = route.get(key, "")
            if target and target not in routes:
                raise StepError(f"{label}: {key} names {target}, which the route table does not publish")
    return json.dumps(routes, separators=(",", ":"), sort_keys=True)


def _runtime_env(factory, cfg, env, token, routes) -> dict[str, str]:
    agent_secrets = _agent_secrets(env)
    values = {
        "FORGEJO_URL": cfg.forgejo_url.rstrip("/"),
        "FORGEJO_TOKEN": token,
        "FORGEJO_WEBHOOK_SECRET": factory.forgejo_webhook_secret,
        "OPENHANDS_API_URL": factory.openhands_url.rstrip("/"),
        "OPENHANDS_API_KEY": factory.openhands_backend_key,
        "AGENT_SECRETS": agent_secrets,
        "LABEL_ROUTES": routes,
        "ALLOWED_REPOS": ",".join(factory.allowed_repos),
        "ALLOWED_SENDERS": ", ".join(factory.allowed_senders),
        "HUMAN_LOGIN": factory.human_login,
        "MERGE_POLICY": json.dumps(_policy(factory.merge_policy), separators=(",", ":"), sort_keys=True),
        "MAX_CONCURRENT_PER_TIER": str(factory.max_concurrent_per_tier),
        "MAX_CONCURRENT": str(factory.max_concurrent),
        "MAX_RUNS_PER_HOUR": str(factory.max_runs_per_hour),
        "MAX_RUN_MINUTES": str(factory.max_run_minutes),
        "MAX_ROUNDS": _max_rounds(env),
    }
    for name in agent_secrets.split(","):
        if name in values:
            continue
        named = env.get(name) or ""
        if not named:
            raise StepError(f"AGENT_SECRETS names {name}, but the bootstrap has no value for it")
        values[name] = named
    for name, value in values.items():
        if not isinstance(value, str) or not value:
            raise StepError(f"the runtime setting {name} is empty")
        if "\r" in value or "\n" in value or "\x00" in value:
            raise StepError(f"the runtime setting {name} is not a single line")
    return values


def _agent_secrets(env) -> str:
    raw = (env.get("FORGEHANDS_AGENT_SECRETS") or "FORGEJO_TOKEN,FORGEJO_URL").strip()
    names = [part.strip() for part in raw.split(",")]
    if not names or any(not name for name in names):
        raise StepError("FORGEHANDS_AGENT_SECRETS must be a comma-separated list without empty entries")
    for name in names:
        if not _ENV_NAME.fullmatch(name):
            raise StepError("FORGEHANDS_AGENT_SECRETS entries must be environment names")
    if len(set(names)) != len(names):
        raise StepError("FORGEHANDS_AGENT_SECRETS repeats a name")
    return ",".join(names)


def _max_rounds(env) -> str:
    raw = (env.get("FORGEHANDS_MAX_ROUNDS") or "3").strip()
    if not raw.isascii() or not raw.isdigit() or not 1 <= int(raw) <= _MAX_ROUNDS:
        raise StepError(f"FORGEHANDS_MAX_ROUNDS must be an integer between 1 and {_MAX_ROUNDS}")
    return raw


def _policy(policy) -> dict:
    return {repository: {"deny": list(rule.get("deny", ()))} for repository, rule in sorted(policy.items())}


def _write_runtime(runtime_dir: str, ready: bool, env: dict[str, str] | None = None) -> str:
    directory = pathlib.Path(runtime_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / RUNTIME_FILE
    document = {"ready": ready, "env": dict(sorted((env or {}).items()))}
    payload = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()
    if len(payload) > MAX_RUNTIME_BYTES:
        raise StepError("the runtime handoff is larger than 64 KiB")
    if path.is_file() and path.read_bytes() == payload:
        if (path.stat().st_mode & 0o777) != 0o600:
            path.chmod(0o600)
        return str(path)
    fd, temporary = tempfile.mkstemp(dir=directory, prefix=".forgehands-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return str(path)


def _invalidate(runtime_dir: str) -> None:
    """A stale ready file must not survive a failed run; a fresh failure leaves no file behind."""
    path = pathlib.Path(runtime_dir) / RUNTIME_FILE
    if not path.is_file():
        return
    try:
        _write_runtime(runtime_dir, False, {})
    except OSError:
        pass


def _sanitize(message: str, factory, state, env) -> str:
    secrets = [getattr(factory, name, "") for name in
               ("openhands_backend_key", "gateway_api_key", "forgejo_agent_token", "forgejo_webhook_secret", "mirror_token")]
    secrets.append(state.get("forgejo", "token") or "")
    secrets.append(state.get(SECTION, TOKEN_KEY) or "")
    for name in (env.get("FORGEHANDS_AGENT_SECRETS") or "").split(","):
        if name.strip():
            secrets.append(env.get(name.strip()) or "")
    for secret in secrets:
        if isinstance(secret, str) and secret:
            message = message.replace(secret, "[redacted]")
    return " ".join(message.split())[:300]
