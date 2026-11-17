import copy
import pathlib
import shutil
import tempfile
import unittest
import urllib.parse
import uuid

from hgboot.agent_profiles import (CONDENSER_MAX_SIZE, LLM_CHEAP, LLM_STRONG, ROLES, TRIMMED_SKILLS,
                                   AgentProfiles, STATE_SECTION)
from hgboot.factory_config import FactoryConfig
from hgboot.state import State
from hgboot.steps import StepError

from .fakes import Server

BACKEND_KEY = "backend-key-123"
GATEWAY_KEY = "gateway-key-456"
STRONG_MODEL = "openai/strong-model"
CHEAP_MODEL = "openai/cheap-model"
GATEWAY_URL = "http://gateway.internal:4000"
DEFAULT_ID = "00000000-0000-4000-8000-000000000001"
PROMPTS = pathlib.Path(__file__).resolve().parents[1] / "hgboot" / "prompts"
FULL_SKILLS = ["add-skill", "agent-canvas-environment", "agent-memory", "code-review", "github", "security"]

AGENT_FIELDS = {
    "schema_version", "agent_kind", "id", "name", "revision", "mcp_server_refs", "secret_refs",
    "integration_refs", "llm_profile_ref", "agent", "tools", "persona", "system_message_suffix",
    "disabled_skills", "public_skills", "condenser", "verification",
    "enable_classify_and_switch_llm_tool", "meta_profile_ref", "tool_concurrency_limit",
}
LLM_FIELDS = {"model", "base_url", "api_key"}


def profile(name, **over):
    value = {
        "schema_version": 1,
        "agent_kind": "openhands",
        "id": str(uuid.uuid4()),
        "name": name,
        "revision": 0,
        "mcp_server_refs": None,
        "secret_refs": ["forgejo-token"],
        "integration_refs": None,
        "llm_profile_ref": "default",
        "agent": "CodeActAgent",
        "tools": [{"name": "terminal", "params": {}}],
        "persona": None,
        "system_message_suffix": None,
        "disabled_skills": [],
        "public_skills": list(FULL_SKILLS),
        "condenser": {"condenser_kind": "llm_summarizing", "enabled": True, "max_size": 240},
        "verification": {"critic_enabled": False},
        "enable_classify_and_switch_llm_tool": False,
        "meta_profile_ref": None,
        "tool_concurrency_limit": 10,
    }
    value.update(over)
    return value


class FakeOpenHands:
    """The profile endpoints of the agent-server, on a real loopback server.

    Mirrors what the server does with a write: the path name wins, a client ``id`` is ignored, an
    overwrite keeps the server's id and bumps ``revision``, and missing LLM fields take server
    defaults.  ``ignore_*_writes`` accepts a write (201, revision bump) but keeps the old values,
    the lost update the read-back exists for.
    """

    def __init__(self, key=BACKEND_KEY):
        self.key = key
        self.llm = {"default": {"model": "default-model", "base_url": "http://default.invalid",
                                "api_key": "default-key", "timeout": 300, "num_retries": 2}}
        self.active_llm = "default"
        self.agents = {"default": profile("default", id=DEFAULT_ID, revision=4)}
        self.active_agent = DEFAULT_ID
        self.ignore_llm_writes: set[str] = set()
        self.ignore_agent_writes: set[str] = set()
        self.bad_ids: dict[str, object] = {}
        self.failures: dict[tuple[str, str], tuple[int, object]] = {}
        self.writes: list[tuple[str, str, object]] = []
        self.activations: list[str] = []

    def snapshot(self):
        return copy.deepcopy((self.llm, self.agents, self.active_llm, self.active_agent))

    def written(self, path=None):
        return [w for w in self.writes if path is None or w[1] == path]

    def __call__(self, req):
        path = req.path.partition("?")[0]
        if req.headers.get("x-session-api-key") != self.key:
            return 401, {"detail": "Invalid or missing X-Session-API-Key"}
        failure = self.failures.pop((req.method, path), None)
        if failure:
            return failure
        parts = path.strip("/").split("/")
        if parts[:2] == ["api", "profiles"]:
            return self._llm_route(req, path, parts[2:])
        if parts[:2] == ["api", "agent-profiles"]:
            return self._agent_route(req, path, parts[2:])
        return 404, {"detail": f"unexpected {req.method} {path}"}

    # -- LLM profiles -----------------------------------------------------------------------------------------------
    def _llm_route(self, req, path, rest):
        if not rest:
            if req.method != "GET":
                return 405, {"detail": "method not allowed"}
            profiles = [{"name": name, "model": config.get("model"), "base_url": config.get("base_url"),
                         "provider_connection_id": None, "provider_connection_broken": False,
                         "api_key_set": bool(config.get("api_key"))}
                        for name, config in self.llm.items()]
            return 200, {"profiles": profiles, "active_profile": self.active_llm}
        name = urllib.parse.unquote(rest[0])
        if len(rest) == 2 and rest[1] == "activate":
            self.activations.append(name)
            self.active_llm = name
            return 200, {"name": name, "message": "activated"}
        if len(rest) > 1:
            return 404, {"detail": "not found"}
        if name not in self.llm:
            if req.method == "GET":
                return 404, {"detail": f"Profile '{name}' not found"}
            if req.method != "POST":
                return 405, {"detail": "method not allowed"}
        if req.method == "GET":
            expose = (req.headers.get("x-expose-secrets") or "").lower().strip()
            if expose not in ("", "plaintext", "encrypted", "true"):
                return 400, {"detail": f"Invalid X-Expose-Secrets header value: '{expose}'."}
            config = copy.deepcopy(self.llm[name])
            api_key_set = bool(config.get("api_key"))
            if expose != "plaintext":
                config["api_key"] = None
            return 200, {"name": name, "config": config, "api_key_set": api_key_set}
        if req.method == "POST":
            body = req.json
            if not isinstance(body, dict) or set(body) - {"llm", "include_secrets"}:
                return 422, {"detail": "invalid save request"}
            llm = body.get("llm")
            if not isinstance(llm, dict) or set(llm) - LLM_FIELDS:
                return 422, {"detail": "invalid llm configuration"}
            self.writes.append(("POST", path, copy.deepcopy(body)))
            if name in self.ignore_llm_writes:
                return 201, {"name": name, "message": f"Profile '{name}' saved"}
            stored = {"model": llm.get("model"), "base_url": llm.get("base_url"), "api_key": llm.get("api_key"),
                      "timeout": 300, "num_retries": 2}
            self.llm[name] = stored
            return 201, {"name": name, "message": f"Profile '{name}' saved"}
        return 405, {"detail": "method not allowed"}

    # -- agent profiles ---------------------------------------------------------------------------------------------
    def _summary(self, name, value):
        summary = {key: value[key] for key in ("name", "agent_kind", "revision", "llm_profile_ref")
                   if key in value}
        if value.get("mcp_server_refs") is not None:
            summary["mcp_server_refs"] = value["mcp_server_refs"]
        profile_id = self.bad_ids[name] if name in self.bad_ids else value.get("id")
        if profile_id is not None:
            summary["id"] = profile_id
        return summary

    def _detail(self, name, value):
        value = copy.deepcopy(value)
        if name in self.bad_ids:
            if self.bad_ids[name] is None:
                value.pop("id", None)
            else:
                value["id"] = self.bad_ids[name]
        return value

    def _agent_route(self, req, path, rest):
        if not rest:
            if req.method != "GET":
                return 405, {"detail": "method not allowed"}
            summaries = [self._summary(name, value) for name, value in self.agents.items()]
            return 200, {"profiles": summaries, "active_agent_profile_id": self.active_agent}
        if len(rest) > 1:
            return 404, {"detail": "not found"}
        name = urllib.parse.unquote(rest[0])
        if req.method == "GET":
            if name not in self.agents:
                return 404, {"detail": f"Profile '{name}' not found"}
            return 200, {"name": name, "profile": self._detail(name, self.agents[name])}
        if req.method != "POST":
            return 405, {"detail": "method not allowed"}
        body = req.json
        if not isinstance(body, dict) or set(body) - AGENT_FIELDS:
            return 422, {"detail": "Invalid agent profile"}
        if not isinstance(body.get("llm_profile_ref"), str) or not body["llm_profile_ref"]:
            return 422, {"detail": "Invalid agent profile"}
        stored = copy.deepcopy(body)
        stored["name"] = name
        existing = self.agents.get(name)
        if existing is None:
            stored["id"] = str(uuid.uuid4())
            stored["revision"] = body.get("revision", 0)
        else:
            stored["id"] = existing["id"]
            stored["revision"] = existing.get("revision", 0) + 1
        stored.setdefault("schema_version", 1)
        stored.setdefault("agent_kind", "openhands")
        self.writes.append(("POST", path, copy.deepcopy(req.json)))
        if name in self.ignore_agent_writes:
            # The observed lost update: 201 and a new revision, the old values stay.
            if existing is not None:
                existing["revision"] = existing.get("revision", 0) + 1
            return 201, {"name": name, "message": f"Agent profile '{name}' saved"}
        self.agents[name] = stored
        return 201, {"name": name, "message": f"Agent profile '{name}' saved"}


def factory(url, **over):
    base = dict(enabled=True, repository="owner/repo", openhands_url=url, openhands_backend_key=BACKEND_KEY,
                strong_model=STRONG_MODEL, cheap_model=CHEAP_MODEL, gateway_url=GATEWAY_URL + "/",
                gateway_api_key=GATEWAY_KEY)
    base.update(over)
    return FactoryConfig(**base)


class AgentProfilesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.fake = FakeOpenHands()
        self.srv = Server(self.fake)
        self.addCleanup(self.srv.close)
        self.cfg = factory(self.srv.url)
        self.state = State(pathlib.Path(self.tmp) / "state.json")

    def run_profiles(self, cfg=None, state=None):
        return AgentProfiles(cfg or self.cfg, state or self.state).run()

    def assert_read_after_every_write(self):
        requests = self.srv.requests
        for index, req in enumerate(requests):
            if req.method != "POST" or not req.path.startswith("/api/"):
                continue
            if req.path.endswith("/activate"):
                continue
            following = requests[index + 1] if index + 1 < len(requests) else None
            self.assertIsNotNone(following, f"{req.path}: a write without a read")
            self.assertEqual((following.method, following.path), ("GET", req.path),
                             f"{req.path}: the write is not followed by a read of the same profile")

    def test_creates_the_llm_and_agent_profiles_from_the_prompt_files(self):
        default_before = copy.deepcopy(self.fake.agents["default"])
        default_llm_before = copy.deepcopy(self.fake.llm["default"])

        ids = self.run_profiles()

        self.assertEqual(set(ids), {role.name for role in ROLES})
        self.assertEqual(len(set(ids.values())), len(ROLES))
        for role, profile_id in ids.items():
            self.assertEqual(str(uuid.UUID(profile_id)), profile_id)
            self.assertEqual(self.fake.agents[role]["id"], profile_id)

        strong = self.fake.written(f"/api/profiles/{LLM_STRONG}")
        cheap = self.fake.written(f"/api/profiles/{LLM_CHEAP}")
        self.assertEqual(len(strong), 1)
        self.assertEqual(len(cheap), 1)
        self.assertEqual(strong[0][2]["llm"],
                         {"model": STRONG_MODEL, "base_url": GATEWAY_URL, "api_key": GATEWAY_KEY})
        self.assertEqual(cheap[0][2]["llm"],
                         {"model": CHEAP_MODEL, "base_url": GATEWAY_URL, "api_key": GATEWAY_KEY})
        self.assertTrue(strong[0][2]["include_secrets"])

        expected_llm = {"planner": LLM_STRONG, "worker": LLM_CHEAP, "worker-strong": LLM_STRONG,
                        "reviewer": LLM_STRONG, "sensitive-reviewer": LLM_STRONG}
        expected_prompt = {"planner": "planner.md", "worker": "worker.md", "worker-strong": "worker-strong.md",
                           "reviewer": "reviewer.md", "sensitive-reviewer": "sensitive-reviewer.md"}
        for role in ROLES:
            stored = self.fake.agents[role.name]
            prompt = (PROMPTS / expected_prompt[role.name]).read_text(encoding="utf-8")
            self.assertEqual(stored["llm_profile_ref"], expected_llm[role.name])
            self.assertEqual(stored["system_message_suffix"], prompt)
            self.assertEqual(stored["public_skills"], list(TRIMMED_SKILLS))
            self.assertEqual(stored["mcp_server_refs"], [])
            self.assertEqual(stored["condenser"],
                             {"condenser_kind": "llm_summarizing", "enabled": True, "max_size": 100})
            self.assertEqual(stored["secret_refs"], ["forgejo-token"])
            self.assertEqual(stored["tools"], [{"name": "terminal", "params": {}}])
            self.assertEqual(stored["tool_concurrency_limit"], 10)
            body = self.fake.written(f"/api/agent-profiles/{role.name}")[0][2]
            self.assertNotIn("id", body)
            self.assertNotIn("revision", body)
            self.assertEqual(body["name"], role.name)

        reviewer = (PROMPTS / "reviewer.md").read_text(encoding="utf-8")
        self.assertIn('list every skipped test or check in the evidence comment under "Not run here"',
                      reviewer)
        self.assertIn("The `WIP: ` prefix of the title and a draft state are never a reason for NOT OK",
                      reviewer)
        self.assertIn("Run the repository's checks on the pull request head", reviewer)
        self.assertIn("Only if one of them fails, run that same check on the default branch", reviewer)
        self.assertNotIn("Run the repository's checks once on the default branch", reviewer)
        sensitive = (PROMPTS / "sensitive-reviewer.md").read_text(encoding="utf-8")
        self.assertIn("`## Security review: OK`", sensitive)
        self.assertIn("`## Security review: CONCERNS`", sensitive)
        self.assertIn("never set or remove a label", sensitive)
        for prompt_name in ("worker.md", "worker-strong.md"):
            worker = (PROMPTS / prompt_name).read_text(encoding="utf-8")
            for phrase in ("BEFORE YOU OPEN THE PULL REQUEST", "empty page", "redirect", "egg-info", "-rs",
                           "run only the fast check of the area you change",
                           "full validation once, when the change is complete",
                           "before you open or update the pull request",
                           "Read only the files the issue names and what they import",
                           "do not list or read whole directories"):
                self.assertIn(phrase, worker)
        self.assertEqual(self.fake.agents["default"], default_before)
        self.assertEqual(self.fake.llm["default"], default_llm_before)
        self.assertEqual(self.fake.active_agent, DEFAULT_ID)
        self.assertEqual(self.fake.activations, [])
        self.assertEqual(self.fake.written("/api/agent-profiles/default"), [])

        for role, profile_id in ids.items():
            self.assertEqual(self.state.get(STATE_SECTION, role), profile_id)
        self.assert_read_after_every_write()
        for req in self.srv.requests:
            self.assertEqual(req.headers.get("x-session-api-key"), BACKEND_KEY)

    def test_the_sensitive_reviewer_role_is_created_and_reconciled_without_writes(self):
        ids = self.run_profiles()

        prompt = (PROMPTS / "sensitive-reviewer.md").read_text(encoding="utf-8")
        stored = self.fake.agents["sensitive-reviewer"]
        self.assertEqual(stored["llm_profile_ref"], LLM_STRONG)
        self.assertEqual(stored["system_message_suffix"], prompt)
        self.assertEqual(ids["sensitive-reviewer"], stored["id"])
        self.assertEqual(self.state.get(STATE_SECTION, "sensitive-reviewer"), stored["id"])
        self.fake.writes.clear()

        self.assertEqual(self.run_profiles(), ids)
        self.assertEqual(self.fake.written("/api/agent-profiles/sensitive-reviewer"), [])

    def test_a_rerun_changes_nothing_even_without_the_state(self):
        first = self.run_profiles()
        self.fake.writes.clear()

        second = self.run_profiles()

        self.assertEqual(second, first)
        self.assertEqual(self.fake.writes, [])

        lost = State(pathlib.Path(self.tmp) / "lost.json")
        third = self.run_profiles(state=lost)

        self.assertEqual(third, first)
        self.assertEqual(self.fake.writes, [])
        for role, profile_id in first.items():
            self.assertEqual(lost.get(STATE_SECTION, role), profile_id)

    def test_a_240_event_condenser_is_corrected_to_100_and_read_back(self):
        self.assertEqual(CONDENSER_MAX_SIZE, 100)
        ids = self.run_profiles()
        self.fake.agents["worker"]["condenser"] = {
            "condenser_kind": "llm_summarizing", "enabled": True, "max_size": 240, "keep_first": 4}
        self.fake.writes.clear()

        self.assertEqual(self.run_profiles(), ids)

        expected = {"condenser_kind": "llm_summarizing", "enabled": True, "max_size": 100, "keep_first": 4}
        self.assertEqual({path for _, path, _ in self.fake.writes}, {"/api/agent-profiles/worker"})
        self.assertEqual(self.fake.written("/api/agent-profiles/worker")[0][2]["condenser"], expected)
        self.assertEqual(self.fake.agents["worker"]["condenser"], expected)

        self.fake.writes.clear()
        self.assertEqual(self.run_profiles(), ids)
        self.assertEqual(self.fake.writes, [])

    def test_a_base_profile_without_a_condenser_gets_no_condenser(self):
        self.fake.agents["default"].pop("condenser")

        self.run_profiles()

        for role in ROLES:
            body = self.fake.written(f"/api/agent-profiles/{role.name}")[0][2]
            self.assertNotIn("condenser", body)
            self.assertNotIn("condenser", self.fake.agents[role.name])
        self.fake.writes.clear()

        self.run_profiles()

        self.assertEqual(self.fake.writes, [])

    def test_only_drifted_managed_fields_are_written(self):
        ids = self.run_profiles()
        self.fake.agents["worker"].update({"llm_profile_ref": LLM_STRONG,
                                           "system_message_suffix": "stale prompt",
                                           "public_skills": list(FULL_SKILLS),
                                           "mcp_server_refs": None})
        self.fake.agents["reviewer"]["system_message_suffix"] = "stale reviewer"
        self.fake.llm[LLM_STRONG]["model"] = "openai/wrong-model"
        self.fake.writes.clear()

        again = self.run_profiles()

        self.assertEqual(again, ids)
        written = {path for _, path, _ in self.fake.writes}
        self.assertEqual(written, {f"/api/profiles/{LLM_STRONG}", "/api/agent-profiles/worker",
                                   "/api/agent-profiles/reviewer"})
        self.assertEqual(self.fake.agents["worker"]["mcp_server_refs"], [])
        self.assertEqual(self.fake.agents["worker"]["public_skills"], list(TRIMMED_SKILLS))
        self.assertEqual(self.fake.agents["worker"]["llm_profile_ref"], LLM_CHEAP)
        self.assertEqual(self.fake.agents["reviewer"]["system_message_suffix"],
                         (PROMPTS / "reviewer.md").read_text(encoding="utf-8"))
        self.assertEqual(self.fake.llm[LLM_STRONG]["model"], STRONG_MODEL)
        self.assertNotIn("/api/agent-profiles/planner", written)

    def test_an_ignored_agent_update_blocks_readiness(self):
        ids = self.run_profiles()
        self.fake.agents["reviewer"]["system_message_suffix"] = "stale reviewer"
        revision = self.fake.agents["reviewer"]["revision"]
        self.fake.ignore_agent_writes.add("reviewer")
        self.fake.writes.clear()

        with self.assertRaisesRegex(StepError, "reviewer.*did not stick"):
            self.run_profiles()

        self.assertEqual(len(self.fake.written("/api/agent-profiles/reviewer")), 1)
        # The server answered 201 and bumped the revision, yet kept the old prompt.
        self.assertEqual(self.fake.agents["reviewer"]["revision"], revision + 1)
        self.assertEqual(self.state.get(STATE_SECTION), ids)
        self.assert_read_after_every_write()

    def test_an_ignored_llm_update_blocks_readiness(self):
        self.fake.ignore_llm_writes.add(LLM_STRONG)

        with self.assertRaisesRegex(StepError, f"{LLM_STRONG}.*did not keep"):
            self.run_profiles()

        self.assertEqual(len(self.fake.written(f"/api/profiles/{LLM_STRONG}")), 1)
        self.assertEqual(self.fake.written("/api/agent-profiles/planner"), [])
        self.assertEqual(self.state.get(STATE_SECTION), {})

    def test_an_absent_or_malformed_id_is_rejected(self):
        for index, (bad, pattern) in enumerate(((None, "has no id"), ("", "has no id"),
                                                ("not-a-uuid", "malformed id"), (12, "has no id"))):
            with self.subTest(bad=bad):
                fake = FakeOpenHands()
                fake.bad_ids["worker"] = bad
                srv = Server(fake)
                try:
                    state = State(pathlib.Path(self.tmp) / f"state-{index}.json")
                    with self.assertRaisesRegex(StepError, pattern):
                        self.run_profiles(cfg=factory(srv.url), state=state)
                    self.assertFalse((pathlib.Path(self.tmp) / f"state-{index}.json").exists())
                finally:
                    srv.close()

    def test_failures_are_sanitized(self):
        self.fake.failures[("POST", "/api/agent-profiles/planner")] = (
            500, {"detail": f"{GATEWAY_KEY} {BACKEND_KEY} " + "x" * 400})
        with self.assertRaises(StepError) as raised:
            self.run_profiles()
        message = str(raised.exception)
        self.assertNotIn(GATEWAY_KEY, message)
        self.assertNotIn(BACKEND_KEY, message)
        self.assertIn("[redacted]", message)
        self.assertLessEqual(len(message), 280)
        self.assertEqual(self.state.get(STATE_SECTION), {})

    def test_a_non_json_error_body_is_not_logged(self):
        self.fake.failures[("GET", "/api/agent-profiles")] = (502, b"UPSTREAM-BODY-MARKER " + GATEWAY_KEY.encode())
        with self.assertRaises(StepError) as raised:
            self.run_profiles()
        self.assertNotIn("UPSTREAM-BODY-MARKER", str(raised.exception))
        self.assertNotIn(GATEWAY_KEY, str(raised.exception))

    def test_a_wrong_backend_key_fails_without_leaking_it(self):
        cfg = factory(self.srv.url, openhands_backend_key="wrong-key")
        with self.assertRaisesRegex(StepError, "401"):
            self.run_profiles(cfg=cfg)
        self.assertEqual(self.fake.writes, [])

    def test_an_unreachable_backend_is_reported_as_such(self):
        cfg = factory("http://127.0.0.1:9")
        with self.assertRaisesRegex(StepError, "unreachable"):
            self.run_profiles(cfg=cfg)

    def test_a_disabled_factory_refuses_to_run(self):
        cfg = factory(self.srv.url, enabled=False)
        with self.assertRaisesRegex(StepError, "FORGEHANDS_BOOTSTRAP=1"):
            self.run_profiles(cfg=cfg)
        self.assertEqual(self.srv.requests, [])

    def test_missing_default_cannot_be_cloned(self):
        self.fake.agents.clear()
        with self.assertRaisesRegex(StepError, "default agent profile does not exist"):
            self.run_profiles()
        self.assertEqual(self.state.get(STATE_SECTION), {})


if __name__ == "__main__":
    unittest.main()
