"""The whole factory against loopback services: converge, rerun without a write, and fail closed."""
import contextlib
import copy
import dataclasses
import io
import json
import os
import pathlib
import stat
import tempfile
import unittest
import urllib.parse
from unittest import mock

from hgboot import factory, handoff
from hgboot.factory_config import DEFAULT_MERGE_DENY, FactoryConfig
from hgboot.state import State
from hgboot.steps import StepError

from .fakes import Server
from .helpers import PASSWORD, config
from .test_agent_profiles import BACKEND_KEY, CHEAP_MODEL, GATEWAY_KEY, STRONG_MODEL, FakeOpenHands

REPOSITORY = "owner/infrastructure"
HUMAN = "owner"
AGENT = "homegraf-agent"
OPERATOR_TOKEN = "operator-token-abc"
GITHUB_REPO = "owner/mirror"
WEBHOOK_SECRET = "webhook-secret-0123456789abcdef"
ROUTE_KEYS = {"ai:plan", "ai:ready", "ai:escalate", "ai:review", "ai:changes", "ci:failure", "ci:success", "epic:done"}
AGENT_EVENTS = {"issues", "pull_request_label", "action_run_failure", "action_run_success", "push"}


def page(rows, query, size=50):
    number = int((query.get("page") or ["1"])[0])
    return rows[(number - 1) * size:number * size]


class FactoryServices:
    """Forgejo and the GitHub API on one loopback server, with the state the reconcilers read back."""

    def __init__(self):
        self.admin = HUMAN
        self.password = PASSWORD
        self.users = {HUMAN: {"password": PASSWORD, "is_admin": True, "tokens": {}}}
        self.user_tokens: dict[str, str] = {OPERATOR_TOKEN: HUMAN}
        self.repos = {REPOSITORY: {"default_branch": "main", "mirror": False}}
        self.labels: dict[str, list[dict]] = {REPOSITORY: []}
        self.hooks: dict[str, list[dict]] = {REPOSITORY: []}
        self.protections: dict[str, dict] = {}
        self.runner_status = "idle"
        self.branches = {"main": "1" * 40}
        self.github_branches = {"main": "1" * 40}
        self.mirrors: list[dict] = []
        self.fail_hook_once = False
        self.notification = "enabled"
        self.next_id = 10

    def _new_id(self):
        self.next_id += 1
        return self.next_id

    def _login(self, req):
        auth = req.headers.get("authorization", "")
        if auth.startswith("token "):
            return self.user_tokens.get(auth[6:])
        return None

    def _admin(self, req):
        return req.basic() == (self.admin, self.password)

    def __call__(self, req):
        parts = urllib.parse.urlsplit(req.path)
        path, query = parts.path, urllib.parse.parse_qs(parts.query)
        api = path.startswith("/api/v1/")
        if api:
            answer = self._forgejo(req, path[len("/api/v1"):], query)
            if answer is not None:
                return answer
        if path.startswith("/api/v3/"):
            answer = self._github(req, path[len("/api/v3"):], query)
            if answer is not None:
                return answer
        return self._web(req, path)

    def _forgejo(self, req, path, query):
        if path == "/user":
            login = self._login(req)
            if login is None:
                return 401, {"message": "no token"}
            return 200, {"login": login, "is_admin": self.users[login]["is_admin"]}
        if path == "/admin/users":
            login = self._login(req)
            if req.method == "POST":
                if not self._admin(req):
                    return 403, {"message": "forbidden"}
                body = req.json
                self.users[body["username"]] = {"password": body["password"], "is_admin": False, "tokens": {}}
                return 201, {"login": body["username"]}
            if login is None or not self.users[login]["is_admin"]:
                return 403, {"message": "forbidden"}
            return 200, [{"login": name} for name in self.users]
        if path == "/admin/actions/runners":
            return 200, {"runners": []}
        if path.startswith("/users/") and path.endswith("/tokens"):
            login = path.split("/")[2]
            if req.basic() is None or req.basic()[0] != login or self.users.get(login, {}).get("password") != req.basic()[1]:
                return 401, {"message": "bad credentials"}
            if req.method == "GET":
                return 200, [{"name": name} for name in self.users[login]["tokens"]]
            if req.method == "POST":
                name = req.json["name"]
                token = f"agent-token-{login}-{name}"
                self.users[login]["tokens"][name] = token
                self.user_tokens[token] = login
                return 201, {"name": name, "sha1": token}
        if path.startswith("/users/") and "/tokens/" in path:
            login = path.split("/")[2]
            name = path.rsplit("/", 1)[1]
            if req.method == "DELETE":
                token = self.users[login]["tokens"].pop(name, None)
                if token is None:
                    return 404, {"message": "not found"}
                self.user_tokens.pop(token, None)
                return 204, None
        if path.startswith("/users/"):
            login = path.split("/")[2]
            if login in self.users:
                return 200, {"login": login, "is_admin": self.users[login]["is_admin"]}
            return 404, {"message": "not found"}
        if path.startswith("/admin/users/") and req.method == "PATCH":
            if not self._admin(req):
                return 403, {"message": "forbidden"}
            self.users[path.rsplit("/", 1)[1]]["password"] = req.json["password"]
            return 200, {"login": path.rsplit("/", 1)[1]}
        if path.startswith("/repos/"):
            return self._repo(req, path[len("/repos/"):], query)
        return 404, {"message": f"unexpected {req.method} {path}"}

    def _repo(self, req, rest, query):
        repository, _, tail = rest.partition("/")
        if repository not in self.repos:
            repository = f"{repository}/{tail.partition('/')[0]}"
            tail = tail.partition("/")[2] if "/" in tail else ""
        if repository not in self.repos:
            return 404, {"message": "no repository"}
        base = self.repos[repository]
        if not tail:
            return 200, {"default_branch": base["default_branch"], "mirror": base["mirror"]}
        if tail == "labels":
            labels = self.labels[repository]
            if req.method == "GET":
                return 200, labels
            body = copy.deepcopy(req.json)
            body["id"] = self._new_id()
            labels.append(body)
            return 201, body
        if tail.startswith("labels/"):
            label_id = int(tail.split("/")[1])
            for label in self.labels[repository]:
                if label["id"] == label_id:
                    label.update(req.json)
                    return 200, label
            return 404, {"message": "no label"}
        if tail == "hooks":
            hooks = self.hooks[repository]
            if req.method == "GET":
                return 200, hooks
            if self.fail_hook_once:
                self.fail_hook_once = False
                return 500, {"message": "webhook store unavailable"}
            body = copy.deepcopy(req.json)
            body["id"] = self._new_id()
            hooks.append(body)
            return 201, body
        if tail.startswith("hooks/"):
            hook_id = int(tail.split("/")[1])
            if req.method == "DELETE":
                self.hooks[repository] = [h for h in self.hooks[repository] if h["id"] != hook_id]
                return 204, None
            for hook in self.hooks[repository]:
                if hook["id"] == hook_id:
                    return 200, hook
            return 404, {"message": "no hook"}
        if tail == "branch_protections":
            if req.method == "GET":
                return 200, list(self.protections.values())
            body = copy.deepcopy(req.json)
            self.protections[body["rule_name"]] = body
            return 201, body
        if tail.startswith("branch_protections/"):
            name = urllib.parse.unquote(tail.split("/", 1)[1])
            self.protections[name].update(req.json)
            return 200, self.protections[name]
        if tail == "actions/runners":
            return 200, {"runners": [{"id": 1, "name": "docker-1", "status": self.runner_status,
                                      "labels": ["docker", "ubuntu-latest"]}]}
        if tail == "branches":
            rows = [{"name": name, "commit": {"id": sha}} for name, sha in self.branches.items()]
            return 200, page(rows, query)
        if tail.startswith("compare/"):
            return 200, {"total_commits": 0}
        if tail == "push_mirrors":
            if req.method == "GET":
                return 200, self.mirrors
            body = copy.deepcopy(req.json)
            body.update({"remote_name": "github", "last_update": None, "last_error": ""})
            self.mirrors.append(body)
            return 200, body
        if tail == "push_mirrors-sync":
            for mirror in self.mirrors:
                mirror["last_update"] = "2026-01-01T00:00:00Z"
            self.github_branches = dict(self.branches)
            return 200, {}
        if tail.startswith("push_mirrors/"):
            name = urllib.parse.unquote(tail.split("/", 1)[1])
            self.mirrors = [m for m in self.mirrors if m.get("remote_name") != name]
            return 204, None
        if tail.startswith("collaborators/"):
            return 204, None
        if tail == "admin/actions/runners":
            return 200, {"runners": []}
        return 404, {"message": f"unexpected {req.method} {tail}"}

    def _github(self, req, path, query):
        parts = path.strip("/").split("/")
        if parts[0] != "repos" or len(parts) < 4:
            return 404, {"message": "not found"}
        if parts[3] == "branches":
            rows = [{"name": name, "commit": {"sha": sha}} for name, sha in self.github_branches.items()]
            return 200, page(rows, query, size=100)
        if parts[3] == "compare":
            return 200, {"status": "identical"}
        return 404, {"message": "not found"}

    def _web(self, req, path):
        if path == "/user/login":
            if req.method == "GET":
                return 200, b"<html><form method=post></form></html>", {"content-type": "text/html"}
            return 302, b"", {"Location": "/user/settings/account", "content-type": "text/html"}
        if path in ("/user/settings/account", "/user/settings/account/email"):
            if req.method == "POST":
                form = urllib.parse.parse_qs(req.body.decode())
                self.notification = form.get("preference", [self.notification])[0]
                return 302, b"", {"Location": "/user/settings/account", "content-type": "text/html"}
            page = f'<html><input type="hidden" name="preference" value="{self.notification}"></html>'
            return 200, page.encode(), {"content-type": "text/html"}
        return 404, b"", {"content-type": "text/html"}


class FactoryTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.services = FactoryServices()
        self.server = Server(self.services)
        self.addCleanup(self.server.close)
        self.openhands = FakeOpenHands()
        self.openhands_server = Server(self.openhands)
        self.addCleanup(self.openhands_server.close)
        self.state = State(pathlib.Path(self.temporary.name) / "state.json")
        self.state.set("forgejo", "token", OPERATOR_TOKEN)
        self.cfg = config(forgejo_url=self.server.url, state_dir=self.temporary.name)
        self.runtime_dir = pathlib.Path(self.temporary.name) / "runtime"
        self.env = {
            "FORGEHANDS_BOOTSTRAP": "1",
            "FORGEHANDS_OPENHANDS_API_URL": self.openhands_server.url,
            "OPENHANDS_LOCAL_BACKEND_API_KEY": BACKEND_KEY,
            "FORGEHANDS_STRONG_MODEL": STRONG_MODEL,
            "FORGEHANDS_CHEAP_MODEL": CHEAP_MODEL,
            "FORGEHANDS_GATEWAY_URL": "http://gateway.invalid",
            "FORGEHANDS_GATEWAY_API_KEY": GATEWAY_KEY,
            "FORGEHANDS_ALLOWED_REPOS": REPOSITORY,
            "FORGEHANDS_ALLOWED_SENDERS": f"{HUMAN},{AGENT}",
            "FORGEHANDS_AGENT_LOGIN": AGENT,
            "FORGEHANDS_HUMAN_LOGIN": HUMAN,
            "FORGEHANDS_FORGEJO_WEBHOOK_SECRET": WEBHOOK_SECRET,
            "FORGEHANDS_RUNTIME_DIR": str(self.runtime_dir),
            "FORGEHANDS_AGENT_SECRETS": "FORGEJO_TOKEN,FORGEJO_URL",
            "FORGEHANDS_TIER_WEEKLY_BUDGETS": '{"worker":25}',
        }

    def run_factory(self, cfg=None):
        cfg = cfg or FactoryConfig.from_env(self.env, REPOSITORY)
        with mock.patch.dict(os.environ, {"HGBOOT_QUIET": "1"}), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            return factory.run(cfg, self.cfg, self.state, self.env,
                               sleep=lambda _: None, mirror_attempts=3)

    def runtime(self):
        return self.runtime_dir / factory.RUNTIME_FILE

    def run_factory_with_stderr(self, env=None):
        env = env or self.env
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"HGBOOT_QUIET": ""}), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err):
            report = factory.run(FactoryConfig.from_env(env, REPOSITORY), self.cfg, self.state, env,
                                 sleep=lambda _: None, mirror_attempts=3)
        return report, err.getvalue()

    def test_mailer_warning_is_silent_for_the_enabled_values(self):
        for value in ("true", "TRUE", " 1 ", "yes"):
            with self.subTest(value=value):
                self.assertIsNone(factory.mailer_warning({**self.env, "FORGEJO_MAILER_ENABLED": value}))

    def test_mailer_warning_names_the_human_when_it_cannot_see_an_enabled_mailer(self):
        unset = {name: value for name, value in self.env.items() if name != "FORGEJO_MAILER_ENABLED"}
        for env in (unset, {**self.env, "FORGEJO_MAILER_ENABLED": "false"}):
            with self.subTest(env=env.get("FORGEJO_MAILER_ENABLED")):
                message = factory.mailer_warning(env)
                self.assertIn(HUMAN, message)
                self.assertIn("FORGEJO_MAILER_ENABLED", message)

    def test_a_run_without_a_mailer_warns_on_stderr_and_still_publishes(self):
        report, err = self.run_factory_with_stderr()
        self.assertTrue(self.runtime().exists())
        self.assertEqual(report.stages[-1].status, "published")
        self.assertNotIn("failed", {stage.status for stage in report.stages})
        self.assertIn("mailer looks unconfigured", err)
        self.assertIn(f"mention of {HUMAN}", err)

    def test_a_run_with_an_enabled_mailer_does_not_warn(self):
        report, err = self.run_factory_with_stderr({**self.env, "FORGEJO_MAILER_ENABLED": "true"})
        self.assertEqual(report.stages[-1].status, "published")
        self.assertNotIn("mailer", err)
        self.assertNotIn("FORGEJO_MAILER_ENABLED", err)

    def writes(self):
        return [r for r in self.server.requests if r.method in ("POST", "PUT", "PATCH", "DELETE")]

    def test_the_disabled_factory_makes_no_request_and_writes_nothing(self):
        disabled = FactoryConfig.from_env({}, REPOSITORY)
        self.server.requests.clear()
        self.assertIsNone(factory.run(disabled, self.cfg, self.state, {}, sleep=lambda _: None))
        self.assertEqual(self.server.requests, [])
        self.assertEqual(self.openhands.writes, [])
        self.assertFalse(self.runtime_dir.exists())

    def test_a_fresh_run_verifies_four_ids_eight_routes_and_publishes_ready(self):
        report = self.run_factory()
        runtime = self.runtime()
        document = json.loads(runtime.read_text())
        env = document["env"]
        roles = self.openhands.agents
        self.assertEqual(report.profiles, 4)
        self.assertEqual(report.routes, 8)
        self.assertTrue(document["ready"])
        self.assertEqual(set(json.loads(env["LABEL_ROUTES"])), ROUTE_KEYS)
        self.assertEqual(len({json.loads(env["LABEL_ROUTES"])[label]["profile"] for label in ROUTE_KEYS}), 4)
        self.assertEqual({role: roles[role]["id"] for role in factory.ROUTE_ROLES},
                         {role: json.loads(env["LABEL_ROUTES"])[LABEL]["profile"]
                          for role, LABEL in (("planner", "ai:plan"), ("worker", "ai:ready"),
                                              ("worker-strong", "ai:escalate"), ("reviewer", "ai:review"))})
        self.assertEqual(stat.S_IMODE(runtime.stat().st_mode), 0o600)
        self.assertEqual(env["FORGEJO_TOKEN"], self.state.get(factory.SECTION, factory.TOKEN_KEY))
        self.assertNotEqual(env["FORGEJO_TOKEN"], OPERATOR_TOKEN)
        self.assertEqual(env["ALLOWED_SENDERS"], "owner, homegraf-agent")
        self.assertEqual(env["HUMAN_LOGIN"], HUMAN)
        self.assertEqual(json.loads(env["MERGE_POLICY"]),
                         {"owner/infrastructure": {"deny": list(DEFAULT_MERGE_DENY)}})
        label_names = {label["name"] for label in self.services.labels[REPOSITORY]}
        self.assertTrue({"ai:plan", "ai:ready", "ai:escalate", "ai:review", "ai:changes", "ai:approved"} <= label_names)
        hooks = self.services.hooks[REPOSITORY]
        self.assertEqual(len(hooks), 1)
        self.assertEqual(set(hooks[0]["events"]), AGENT_EVENTS)
        self.assertEqual(hooks[0]["config"]["url"], factory.FORGEHANDS_ENDPOINT)
        self.assertTrue(hooks[0]["active"])
        self.assertIn("ci / seed (*)", self.services.protections["main"]["status_check_contexts"])
        self.assertNotIn(OPERATOR_TOKEN, runtime.read_text())
        summary = "\n".join(handoff.factory_summary(report))
        self.assertIn("configured", summary)
        self.assertIn(AGENT, summary)
        for secret in (BACKEND_KEY, GATEWAY_KEY, WEBHOOK_SECRET, OPERATOR_TOKEN, env["FORGEJO_TOKEN"]):
            self.assertNotIn(secret, summary)

    def test_a_second_run_writes_nothing_and_keeps_the_runtime_bytes_and_mtime(self):
        self.run_factory()
        runtime = self.runtime()
        before = runtime.read_bytes()
        before_mtime = runtime.stat().st_mtime_ns
        self.server.requests.clear()
        self.openhands.writes.clear()
        self.run_factory()
        self.assertEqual(runtime.read_bytes(), before)
        self.assertEqual(runtime.stat().st_mtime_ns, before_mtime)
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.openhands.writes, [])

    def test_an_ignored_profile_update_blocks_activation_and_recovers(self):
        self.openhands.ignore_agent_writes.add("planner")
        with self.assertRaises(StepError) as caught:
            self.run_factory()
        message = str(caught.exception)
        self.assertIn("blocked", message)
        for secret in (BACKEND_KEY, GATEWAY_KEY, WEBHOOK_SECRET, OPERATOR_TOKEN):
            self.assertNotIn(secret, message)
        self.assertFalse(self.runtime().exists())
        self.openhands.ignore_agent_writes.clear()
        self.run_factory()
        self.assertTrue(json.loads(self.runtime().read_text())["ready"])

    def test_a_missing_runner_blocks_before_the_first_remote_write(self):
        self.services.runner_status = "offline"
        with self.assertRaises(StepError) as caught:
            self.run_factory()
        self.assertIn("runner", str(caught.exception).lower())
        self.assertEqual(self.writes(), [])
        self.assertFalse(self.runtime().exists())

    def test_an_interrupted_hook_write_leaves_no_ready_file_and_the_rerun_recovers(self):
        self.services.fail_hook_once = True
        with self.assertRaises(StepError):
            self.run_factory()
        self.assertFalse(self.runtime().exists())
        self.run_factory()
        self.assertTrue(json.loads(self.runtime().read_text())["ready"])
        self.assertEqual(len(self.services.hooks[REPOSITORY]), 1)

    def test_a_hook_with_wrong_events_is_replaced_not_patched(self):
        self.run_factory()
        hook = self.services.hooks[REPOSITORY][0]
        hook["events"] = ["issues"]
        old_id = hook["id"]
        self.server.requests.clear()
        self.run_factory()
        self.assertEqual(len(self.services.hooks[REPOSITORY]), 1)
        self.assertNotEqual(self.services.hooks[REPOSITORY][0]["id"], old_id)
        methods = [(r.method, urllib.parse.urlsplit(r.path).path) for r in self.server.requests]
        self.assertIn(("DELETE", f"/api/v1/repos/{REPOSITORY}/hooks/{old_id}"), methods)
        self.assertIn(("POST", f"/api/v1/repos/{REPOSITORY}/hooks"), methods)
        self.assertNotIn("PATCH", [method for method, _ in methods])

    def test_github_branches_that_forgejo_lacks_refuse_the_mirror_before_writing(self):
        cfg = dataclasses.replace(FactoryConfig.from_env(self.env, REPOSITORY),
                                  mirror_url=f"{self.server.url}/{GITHUB_REPO}", mirror_token="mirror-token-123")
        self.services.github_branches["release"] = "2" * 40
        with self.assertRaises(StepError) as caught:
            self.run_factory(cfg)
        self.assertIn("release", str(caught.exception))
        self.assertEqual(self.writes(), [])
        self.assertFalse(self.runtime().exists())

    def test_a_stale_ready_file_is_invalidated_by_a_failed_run(self):
        runtime = self.runtime()
        runtime.parent.mkdir(parents=True, exist_ok=True)
        runtime.write_text('{"ready": true, "env": {"FORGEJO_TOKEN": "stale"}}\n')
        runtime.chmod(0o600)
        self.services.runner_status = "offline"
        with self.assertRaises(StepError):
            self.run_factory()
        self.assertFalse(json.loads(runtime.read_text())["ready"])

    def test_the_runtime_file_is_atomic_and_not_world_readable(self):
        self.run_factory()
        runtime = self.runtime()
        self.assertEqual(stat.S_IMODE(runtime.stat().st_mode), 0o600)
        leftovers = [p for p in runtime.parent.iterdir() if p.name.startswith(".forgehands-")]
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
