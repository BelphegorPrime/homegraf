"""The factory's LLM profiles and agent profiles, reconciled and verified by reading them back.

Verified against a running OpenHands agent-server (the Agent Canvas backend, agent-server 1.62.0):

* ``GET /api/profiles`` -> ``{"profiles": [...], "active_profile": name | None}``
* ``GET /api/profiles/{name}`` -> ``{"name", "config", "api_key_set"}``; without ``X-Expose-Secrets``
  the ``config.api_key`` is null, with ``X-Expose-Secrets: plaintext`` it is the stored key
* ``POST /api/profiles/{name}`` takes ``{"llm": {...}, "include_secrets": true}`` and answers 201; the
  response body is not the stored profile, so it proves nothing
* ``GET /api/agent-profiles`` -> ``{"profiles": [{id, name, agent_kind, revision, llm_profile_ref,
  mcp_server_refs}], "active_agent_profile_id"}``
* ``GET /api/agent-profiles/{name}`` -> ``{"name", "profile"}`` with the whole profile
* ``POST /api/agent-profiles/{name}`` takes the profile object itself; the path name wins, a client
  ``id`` is ignored, and overwriting keeps the server's id while bumping ``revision`` -> 201

Every request authenticates with ``X-Session-API-Key``.  A 201 and a bumped revision are no proof
that a write stuck, so each write is followed by a read and a comparison of the managed fields.  A
read that still shows other values fails closed, publishes no ids, and is not retried in a loop.

The LLM profile names are the ones of the reference installation: ``smart`` for the strong tier,
``cheap`` for the worker tier and, when ``FORGEHANDS_FREE_MODEL`` is set, ``free`` for the fallback
worker.  The agent profiles clone ``default`` when they are missing, but ``default`` itself is never
written or activated.  Without a free model neither the ``free`` LLM profile nor the ``worker-free``
role exists.
"""
from __future__ import annotations

import pathlib
import uuid
from dataclasses import dataclass
from urllib.parse import quote

from .http import Unreachable, request
from .steps import StepError, log

STEP = "factory-profiles"
STATE_SECTION = "factory_agent_profiles"
DEFAULT_PROFILE = "default"
LLM_STRONG = "smart"
LLM_CHEAP = "cheap"
LLM_FREE = "free"
# Every role allows the same three public skills, so no role inherits the full default catalog (about
# 35 skills and 58k prompt tokens on a trivial task). The user skills are hidden per role instead.
PUBLIC_SKILLS = ("agent-canvas-environment", "code-review", "security")
# The two user skills of the reference installation: workers run them, every other role hides them.
WORKER_SKILLS = ("systematic-debugging", "test-driven-development")
# Condense after 100 events, as the live installation does, to keep the context of every step smaller.
CONDENSER_MAX_SIZE = 100
REASON_MAX = 200


@dataclass(frozen=True)
class Role:
    name: str
    llm: str
    prompt: str
    public_skills: tuple[str, ...]
    disabled_skills: tuple[str, ...]


ROLES = (
    Role("planner", LLM_STRONG, "planner.md", PUBLIC_SKILLS, WORKER_SKILLS),
    Role("worker", LLM_CHEAP, "worker.md", PUBLIC_SKILLS, ()),
    Role("worker-strong", LLM_STRONG, "worker-strong.md", PUBLIC_SKILLS, ()),
    Role("reviewer", LLM_STRONG, "reviewer.md", PUBLIC_SKILLS, WORKER_SKILLS),
    Role("sensitive-reviewer", LLM_STRONG, "sensitive-reviewer.md", PUBLIC_SKILLS, WORKER_SKILLS),
)
ROLE_WORKER_FREE = Role("worker-free", LLM_FREE, "worker.md", PUBLIC_SKILLS, ())


def roles_for(cfg) -> tuple[Role, ...]:
    """The roles to reconcile; ``worker-free`` exists only when a free model is configured."""
    return (*ROLES, ROLE_WORKER_FREE) if cfg.free_model else ROLES


def _condenser_max_size(profile: dict):
    condenser = profile.get("condenser")
    return condenser.get("max_size") if isinstance(condenser, dict) else None


class AgentProfiles:
    """Reconciles the profiles and returns the verified role -> id mapping."""

    def __init__(self, cfg, state, *, http=request, step: str = STEP):
        self.cfg, self.state, self.http, self.step = cfg, state, http, step
        self.api = cfg.openhands_url.rstrip("/") + "/api"
        self.prompts = pathlib.Path(__file__).with_name("prompts")
        self.roles = roles_for(cfg)

    def run(self) -> dict[str, str]:
        self._require()
        self._reconcile_llm()
        verified = self._verified_agents()
        for role, profile_id in verified.items():
            self.state.set(STATE_SECTION, role, profile_id)
        log(self.step, f"{len(verified)} agent profiles verified: {', '.join(verified)}")
        return verified

    def _require(self) -> None:
        if not self.cfg.enabled:
            raise StepError("the factory is off: set FORGEHANDS_BOOTSTRAP=1 before reconciling the profiles")
        required = (
            ("openhands_backend_key", "OPENHANDS_LOCAL_BACKEND_API_KEY"),
            ("strong_model", "FORGEHANDS_STRONG_MODEL"),
            ("cheap_model", "FORGEHANDS_CHEAP_MODEL"),
            ("gateway_url", "FORGEHANDS_GATEWAY_URL"),
            ("gateway_api_key", "FORGEHANDS_GATEWAY_API_KEY"),
        )
        for attribute, variable in required:
            value = getattr(self.cfg, attribute, "")
            if not isinstance(value, str) or not value.strip():
                raise StepError(f"{variable} is not set; the profile step needs it")

    def _headers(self, *, expose: str | None = None) -> dict:
        headers = {"X-Session-API-Key": self.cfg.openhands_backend_key}
        if expose:
            headers["X-Expose-Secrets"] = expose
        return headers

    def _redact(self, value: str) -> str:
        for secret in (self.cfg.gateway_api_key, self.cfg.openhands_backend_key):
            if secret:
                value = value.replace(secret, "[redacted]")
        return " ".join(value.split())[:REASON_MAX]

    def _reason(self, response) -> str:
        try:
            data = response.json()
        except ValueError:
            return ""
        if not isinstance(data, dict):
            return ""
        detail = data.get("detail") or data.get("message") or ""
        return self._redact(detail) if isinstance(detail, str) else ""

    def _call(self, method: str, path: str, *, body=None, what: str, allow_404: bool = False,
              expose: str | None = None):
        try:
            response = self.http(method, self.api + path, body=body, headers=self._headers(expose=expose))
        except Unreachable as exc:
            raise StepError(f"{what}: OpenHands is unreachable ({self._redact(str(exc))})") from None
        if response.status == 404 and allow_404:
            return response
        if not response.ok:
            reason = self._reason(response)
            detail = f": {reason}" if reason else ""
            raise StepError(f"{what}: OpenHands answered {response.status}{detail}")
        return response

    def _json(self, response, what: str):
        try:
            return response.json()
        except ValueError:
            raise StepError(f"{what}: OpenHands did not answer with JSON") from None

    # -- LLM profiles ---------------------------------------------------------------------------------------------------
    def _llm_names(self) -> tuple[str, ...]:
        return (LLM_STRONG, LLM_CHEAP, LLM_FREE) if self.cfg.free_model else (LLM_STRONG, LLM_CHEAP)

    def _llm_expected(self, name: str) -> dict:
        if name == LLM_STRONG:
            model = self.cfg.strong_model
        elif name == LLM_CHEAP:
            model = self.cfg.cheap_model
        else:
            model = self.cfg.free_model
        return {"model": model, "base_url": self.cfg.gateway_url.rstrip("/"), "api_key": self.cfg.gateway_api_key}

    def _stored_llm(self, name: str, *, what: str) -> dict | None:
        response = self._call("GET", f"/profiles/{quote(name, safe='')}", what=what, allow_404=True,
                              expose="plaintext")
        if response.status == 404:
            return None
        data = self._json(response, what)
        config = data.get("config") if isinstance(data, dict) else None
        if not isinstance(config, dict):
            raise StepError(f"{what}: OpenHands did not return the profile configuration")
        return config

    @staticmethod
    def _llm_matches(config: dict, expected: dict) -> bool:
        base_url = config.get("base_url")
        return (config.get("model") == expected["model"]
                and isinstance(base_url, str) and base_url.rstrip("/") == expected["base_url"]
                and config.get("api_key") == expected["api_key"])

    def _reconcile_llm(self) -> None:
        for name in self._llm_names():
            expected = self._llm_expected(name)
            current = self._stored_llm(name, what=f"reading the {name} LLM profile")
            if current is not None and self._llm_matches(current, expected):
                continue
            self._call("POST", f"/profiles/{quote(name, safe='')}",
                       body={"llm": expected, "include_secrets": True},
                       what=f"writing the {name} LLM profile")
            # One write, one read: a lost update fails closed instead of rewriting in a loop.
            actual = self._stored_llm(name, what=f"reading back the {name} LLM profile")
            if actual is None or not self._llm_matches(actual, expected):
                raise StepError(f"{name}: the LLM profile did not keep the model, base URL and gateway key "
                                "that were written")

    # -- agent profiles -------------------------------------------------------------------------------------------------
    def _agent_list(self, *, what: str) -> dict[str, dict]:
        data = self._json(self._call("GET", "/agent-profiles", what=what), what)
        profiles = data.get("profiles") if isinstance(data, dict) else None
        if not isinstance(profiles, list):
            raise StepError(f"{what}: OpenHands did not list the agent profiles")
        by_name: dict[str, dict] = {}
        for entry in profiles:
            name = entry.get("name") if isinstance(entry, dict) else None
            if not isinstance(name, str) or not name:
                raise StepError(f"{what}: OpenHands listed an agent profile without a name")
            by_name[name] = entry
        return by_name

    def _stored_agent(self, name: str, *, what: str) -> dict | None:
        response = self._call("GET", f"/agent-profiles/{quote(name, safe='')}", what=what, allow_404=True)
        if response.status == 404:
            return None
        data = self._json(response, what)
        profile = data.get("profile") if isinstance(data, dict) else None
        if not isinstance(profile, dict):
            raise StepError(f"{what}: OpenHands did not return the profile object")
        return profile

    @staticmethod
    def _desired_agent(base: dict, role: Role, prompt: str) -> dict:
        profile = dict(base)
        profile.pop("id", None)
        profile.pop("revision", None)
        profile["name"] = role.name
        profile["llm_profile_ref"] = role.llm
        profile["system_message_suffix"] = prompt
        profile["public_skills"] = list(role.public_skills)
        profile["disabled_skills"] = list(role.disabled_skills)
        profile["mcp_server_refs"] = []
        condenser = base.get("condenser")
        if isinstance(condenser, dict):
            profile["condenser"] = dict(condenser, max_size=CONDENSER_MAX_SIZE)
        else:
            profile.pop("condenser", None)
        return profile

    @staticmethod
    def _agent_matches(profile: dict, desired: dict) -> bool:
        return (profile.get("name") == desired["name"]
                and profile.get("llm_profile_ref") == desired["llm_profile_ref"]
                and profile.get("system_message_suffix") == desired["system_message_suffix"]
                and profile.get("public_skills") == desired["public_skills"]
                and profile.get("disabled_skills") == desired["disabled_skills"]
                and profile.get("mcp_server_refs") == []
                and _condenser_max_size(profile) == _condenser_max_size(desired))

    @staticmethod
    def _prompt(role: Role, directory: pathlib.Path) -> str:
        path = directory / role.prompt
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise StepError(f"{role.name}: the prompt file {role.prompt} cannot be read ({exc})") from None
        if not text.strip():
            raise StepError(f"{role.name}: the prompt file {role.prompt} is empty")
        return text

    def _reconcile_agent(self, role: Role, prompt: str, listed: dict[str, dict], default: dict | None) -> dict:
        current = None
        if role.name in listed:
            current = self._stored_agent(role.name, what=f"reading the {role.name} agent profile")
            if current is None:
                raise StepError(f"{role.name}: the agent profile disappeared while it was being reconciled")
            base, verb = current, "corrected"
        else:
            if default is None:
                raise StepError(f"{role.name}: the role is missing and there is no default agent profile to clone")
            if default.get("agent_kind", "openhands") != "openhands":
                raise StepError("the default agent profile is not an OpenHands profile, so it cannot be cloned")
            base, verb = default, "created"
        desired = self._desired_agent(base, role, prompt)
        if current is not None and self._agent_matches(current, desired):
            return current
        self._call("POST", f"/agent-profiles/{quote(role.name, safe='')}", body=desired,
                   what=f"writing the {role.name} agent profile")
        # The server reports 201 and bumps the revision even when it keeps the old values.
        actual = self._stored_agent(role.name, what=f"reading back the {role.name} agent profile")
        if actual is None:
            raise StepError(f"{role.name}: OpenHands accepted the write but does not list the profile")
        if not self._agent_matches(actual, desired):
            raise StepError(f"{role.name}: the write did not stick; the stored prompt, LLM, skills or MCP "
                            "servers differ from the written ones")
        if current is not None and actual.get("id") != current.get("id"):
            raise StepError(f"{role.name}: the server replaced the profile id; refusing to publish it")
        log(self.step, f"{role.name}: agent profile {verb} and verified")
        return actual

    def _profile_id(self, role: Role, profile: dict) -> str:
        if profile.get("name") != role.name:
            raise StepError(f"{role.name}: OpenHands returned the profile of {profile.get('name')!r}")
        value = profile.get("id")
        if not isinstance(value, str) or not value.strip():
            raise StepError(f"{role.name}: the verified agent profile has no id")
        try:
            parsed = uuid.UUID(value)
        except ValueError:
            raise StepError(f"{role.name}: the verified agent profile has a malformed id") from None
        return str(parsed)

    def _verified_agents(self) -> dict[str, str]:
        listed = self._agent_list(what="reading the agent profiles")
        prompts = {role.name: self._prompt(role, self.prompts) for role in self.roles}
        default = None
        if any(role.name not in listed for role in self.roles):
            default = self._stored_agent(DEFAULT_PROFILE, what="reading the default agent profile to clone")
            if default is None:
                raise StepError("the default agent profile does not exist, so the missing roles cannot be cloned")
        ids: dict[str, str] = {}
        for role in self.roles:
            verified = self._reconcile_agent(role, prompts[role.name], listed, default)
            ids[role.name] = self._profile_id(role, verified)
        if len(set(ids.values())) != len(ids):
            raise StepError("OpenHands returned the same id for two agent profiles")
        return ids


def reconcile(cfg, state, *, http=request, step: str = STEP) -> dict[str, str]:
    """Entry point for the bootstrap: reconcile the profiles and return the verified role -> id mapping."""
    return AgentProfiles(cfg, state, http=http, step=step).run()
