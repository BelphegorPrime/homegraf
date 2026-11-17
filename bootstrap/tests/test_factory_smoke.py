"""The factory smoke check against a loopback Forgejo: refusals, the two normal cases, the protected case,
bounded waits, failures and ownership of the cleanup.

The loopback service is the external API boundary.  It answers the same endpoints as Forgejo for the
parts the smoke touches (repository, contents, labels, hooks, branch protections, runners, issues,
comments, pull requests, reviews and commit statuses) and plays the factory's part: an agent comment
with a conversation link, a draft pull request, a green check, a review and a merge, or a stall.
"""
import base64
import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from hgboot import factory_smoke, main as hgmain
from hgboot.factory_config import FactoryConfig
from hgboot.factory_smoke import (MARKER, PROTECTED_FIXTURE, Smoke, SmokeFailure, SmokeRefused,
                                  SmokeReport, SmokeTimeout, Stage)
from hgboot.state import State

from .fakes import Server
from .helpers import config

REPO = "owner/factory-smoke-ci"
STACK = "owner/infrastructure"
TOKEN = "operator-token-1"
SECRET = "webhook-secret-1234567890"
LABELS = ("ai:plan", "ai:ready", "ai:escalate", "ai:review", "ai:changes", "ai:approved", "ai:close")


class FakeFactory:
    """Forgejo plus a factory that finishes the three cases, unless a test asks it to stall or break."""

    def __init__(self):
        self.repos: dict[str, dict] = {}
        self.next_number = 0
        self.stall = False
        self.protected_merged = False
        self.protected_wip = False
        self.escalate_normal = False
        self.conversation = True
        self.pr_author = "homegraf-agent"
        self.merge_author = "homegraf-agent"
        self.approval_actor = "homegraf-agent"
        self.now = 0.0

    # -- clock injection --------------------------------------------------------------------
    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def seed(self, name: str = REPO, description: str = MARKER, private: bool = True, mirror: bool = False):
        self.repos[name] = {"full_name": name, "description": description, "private": private, "mirror": mirror,
                            "default_branch": "main", "files": {}, "labels": {}, "hooks": {}, "protections": {},
                            "issues": {}, "pulls": {}, "comments": {}, "reviews": {}, "statuses": {}}
        return self.repos[name]

    def repo(self, name: str = REPO) -> dict:
        return self.repos.setdefault(name, self.seed(name=name))

    def next_id(self) -> int:
        self.next_number += 1
        return 1000 + self.next_number

    # -- the factory's part ------------------------------------------------------------------
    def _evolve(self, repo: dict, issue: dict) -> None:
        issue["polls"] += 1
        polls = issue["polls"]
        number = issue["number"]
        if self.stall:
            return
        if polls == 1 and self.conversation:
            repo["comments"][number] = [{"id": self.next_id(), "user": {"login": "homegraf-agent"},
                                         "body": "Working in http://openhands.local:8000/conversations/abc123"
                                                 "?access_token=conversation-secret"}]
        if polls >= 2 and number not in repo["pulls"]:
            repo["pulls"][number] = {
                "id": self.next_id(), "number": number, "state": "open", "draft": True,
                "title": f"WIP: {issue['title']}", "body": f"Closes #{number}",
                "head": {"ref": f"ai/issue-{number}", "sha": f"sha{number}"},
                "labels": [], "merged": False, "merged_at": None,
                "user": {"login": self.pr_author}}
        pull = repo["pulls"].get(number)
        if pull is None:
            return
        if polls >= 3:
            repo["statuses"][pull["head"]["sha"]] = [
                {"context": "ci / seed (pull_request)", "state": "success"},
                {"context": "ci / seed (push)", "state": "success"}]
        if polls >= 4 and not (issue["kind"] == "protected" and self.protected_wip):
            pull["draft"] = False
            pull["title"] = pull["title"].removeprefix("WIP: ")
            if not pull["labels"]:
                pull["labels"] = [{"name": "ai:approved"}]
                issue["timeline"].append({"type": "label", "user": {"login": self.approval_actor},
                                          "label": {"name": "ai:approved"}})
            repo["reviews"][number] = [{"id": self.next_id(), "state": "APPROVED", "body": "approved"}]
        if self.escalate_normal and issue["kind"] == "normal" and polls >= 3:
            issue["labels"].add("ai:escalate")
        merged = polls >= 5 if issue["kind"] != "protected" else (self.protected_merged and polls >= 6)
        if merged:
            pull.update({"merged": True, "merged_at": "2026-01-01T00:00:00Z", "state": "closed",
                         "merged_by": {"login": self.merge_author}})
            issue["state"] = "closed"
            issue["timeline"].append({"type": "close", "user": {"login": "homegraf-agent"}})

    def issue_view(self, repo: dict, issue: dict) -> dict:
        return {"number": issue["number"], "title": issue["title"], "body": issue["body"], "state": issue["state"],
                "labels": [{"name": name} for name in sorted(issue["labels"])]}

    def pull_view(self, repo: dict, pull: dict) -> dict:
        view = {k: pull[k] for k in ("number", "state", "draft", "title", "body", "head", "labels", "merged",
                                     "merged_at", "user")}
        view["merged"] = bool(pull["merged"])
        if pull.get("merged_by"):
            view["merged_by"] = pull["merged_by"]
        return view

    # -- HTTP --------------------------------------------------------------------------------
    def __call__(self, req):
        path, _, query = req.path.partition("?")
        if req.headers.get("authorization") != f"token {TOKEN}":
            return 401, {"message": "unauthorized"}
        if path == "/api/v1/user":
            return 200, {"login": "owner"}
        if path in ("/api/v1/user/repos", "/api/v1/admin/users/owner/repos") and req.method == "POST":
            body = req.json
            self.seed(f"owner/{body['name']}", description=body.get("description", ""),
                      private=body.get("private", False))
            return 201, {"full_name": f"owner/{body['name']}"}
        if path == "/api/v1/admin/actions/runners":
            return 200, {"runners": []}
        parts = path.split("/")
        if len(parts) >= 6 and parts[3] == "repos":
            name = f"{parts[4]}/{parts[5]}"
            repo = self.repos.get(name)
            if repo is None:
                return 404, {"message": "not found"}
            rest = "/".join(parts[6:])
            if not rest:
                if req.method == "DELETE":
                    del self.repos[name]
                    return 204, None
                return 200, {"full_name": name, "description": repo["description"], "private": repo["private"],
                             "mirror": repo["mirror"], "default_branch": repo["default_branch"]}
            if rest == "actions/runners":
                return 200, {"runners": [{"id": 1, "name": "smoke-runner", "labels": ["docker"], "status": "idle"}]}
            if rest == "labels":
                if req.method == "POST":
                    label = {"id": self.next_id(), **req.json}
                    repo["labels"][label["name"]] = label
                    return 201, label
                return 200, list(repo["labels"].values())
            if rest.startswith("labels/"):
                label_id = int(rest.rsplit("/", 1)[1])
                label = next((value for value in repo["labels"].values() if value["id"] == label_id), None)
                if label is None:
                    return 404, {"message": "not found"}
                if req.method == "PATCH":
                    label.update(req.json)
                return 200, label
            if rest == "hooks":
                if req.method == "POST":
                    hook = {"id": self.next_id(), "active": req.json["active"], "events": req.json["events"],
                            "type": req.json["type"], "config": req.json["config"]}
                    repo["hooks"][hook["id"]] = hook
                    return 201, hook
                return 200, list(repo["hooks"].values())
            if rest.startswith("hooks/"):
                hook_id = int(rest.rsplit("/", 1)[1])
                if req.method == "DELETE":
                    repo["hooks"].pop(hook_id, None)
                    return 204, None
                return (200, repo["hooks"][hook_id]) if hook_id in repo["hooks"] else (404, {"message": "not found"})
            if rest == "branch_protections":
                if req.method == "POST":
                    protection = {"id": self.next_id(), **req.json}
                    repo["protections"][protection["rule_name"]] = protection
                    return 201, protection
                return 200, list(repo["protections"].values())
            if rest.startswith("branch_protections/"):
                rule = rest.rsplit("/", 1)[1]
                if rule not in repo["protections"]:
                    return 404, {"message": "not found"}
                if req.method == "PATCH":
                    repo["protections"][rule].update(req.json)
                return 200, repo["protections"][rule]
            if rest.startswith("contents/"):
                file_path = rest[len("contents/"):]
                if req.method == "GET":
                    if file_path not in repo["files"]:
                        return 404, {"message": "not found"}
                    import base64
                    return 200, {"content": base64.b64encode(repo["files"][file_path].encode()).decode(),
                                 "sha": "sha-" + file_path.replace("/", "-")}
                import base64
                repo["files"][file_path] = base64.b64decode(req.json["content"]).decode()
                return 201, {"content": {"path": file_path}}
            if rest == "issues" and req.method == "POST":
                number = len(repo["issues"]) + 1
                by_id = {label["id"]: label["name"] for label in repo["labels"].values()}
                issue = {"number": number, "title": req.json["title"], "body": req.json["body"], "state": "open",
                         "labels": {by_id[i] for i in req.json.get("labels") or []}, "polls": 0,
                         "timeline": [{"type": "label", "user": {"login": "owner"}, "label": {"name": by_id[i]}}
                                      for i in req.json.get("labels") or []],
                         "kind": "protected" if "protected" in req.json["title"] else
                                 ("later" if "added later" in req.json["body"] else "normal")}
                repo["issues"][number] = issue
                return 201, self.issue_view(repo, issue)
            if rest.startswith("issues/"):
                issue_parts = rest.split("/")
                number = int(issue_parts[1])
                issue = repo["issues"].get(number)
                if issue is None:
                    return 404, {"message": "not found"}
                if len(issue_parts) == 2:
                    if req.method == "DELETE":
                        repo["issues"].pop(number, None)
                        return 204, None
                    self._evolve(repo, issue)
                    return 200, self.issue_view(repo, issue)
                if issue_parts[2] == "comments":
                    return 200, repo["comments"].get(number, [])
                if issue_parts[2] == "timeline":
                    return 200, issue["timeline"]
                if issue_parts[2] == "labels" and req.method == "POST":
                    by_id = {label["id"]: label["name"] for label in repo["labels"].values()}
                    added = {by_id[i] for i in req.json["labels"]}
                    issue["labels"] |= added
                    issue["timeline"] += [{"type": "label", "user": {"login": "owner"}, "label": {"name": name}}
                                          for name in sorted(added)]
                    return 200, self.issue_view(repo, issue)
            if rest == "pulls":
                return 200, [self.pull_view(repo, pull) for pull in repo["pulls"].values()]
            if rest.startswith("pulls/"):
                pull_parts = rest.split("/")
                number = int(pull_parts[1])
                pull = repo["pulls"].get(number)
                if pull is None:
                    return 404, {"message": "not found"}
                if len(pull_parts) == 2:
                    return 200, self.pull_view(repo, pull)
                if pull_parts[2] == "reviews":
                    return 200, repo["reviews"].get(number, [])
            if rest.startswith("commits/"):
                sha = rest.split("/")[1]
                statuses = repo["statuses"].get(sha, [])
                state = "success" if statuses and all(s["state"] == "success" for s in statuses) else "pending"
                return 200, {"state": state, "statuses": statuses}
        return 404, {"message": f"unexpected {req.method} {path}"}


class SmokeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.fake = FakeFactory()
        self.srv = Server(self.fake)
        self.addCleanup(self.srv.close)
        self.cfg = config(tmp=self.tmp, forgejo_url=self.srv.url)
        self.state = State(f"{self.tmp}/state.json")
        self.state.set("forgejo", "token", TOKEN)
        self.factory = FactoryConfig(
            enabled=True, repository=STACK, allowed_repos=(REPO,), allowed_senders=("owner", "homegraf-agent"),
            agent_login="homegraf-agent", human_login="owner", forgejo_webhook_secret=SECRET,
            merge_policy={REPO: {"deny": ("infra/**", "AGENTS.md")}})

    def smoke(self, factory=None, **options):
        return Smoke(factory or self.factory, self.cfg, self.state, token=TOKEN, sleep=self.fake.sleep,
                     clock=self.fake.clock, timeout=options.pop("timeout", 120), interval=5, **options)

    def writes(self):
        return [request for request in self.srv.requests if request.method != "GET"]

    def refused(self, *args, **options):
        with self.assertRaises(SmokeRefused):
            self.smoke(**options).run(*args)
        self.assertEqual(self.writes(), [], "a refusal wrote to Forgejo")


class RefusalTests(SmokeTest):
    def test_a_missing_confirmation_is_refused_before_anything_is_written(self):
        with self.assertRaises(SmokeRefused):
            self.smoke().preflight(REPO, "")
        self.assertEqual(self.writes(), [])

    def test_a_mismatched_confirmation_is_refused(self):
        self.refused(REPO, "owner/other-scratch")

    def test_a_target_outside_the_allowlist_is_refused(self):
        factory = FactoryConfig(**{**self.factory.__dict__, "allowed_repos": (STACK,)})
        self.refused(REPO, REPO, factory=factory)

    def test_the_stack_repository_is_refused_even_when_it_is_allowlisted(self):
        factory = FactoryConfig(**{**self.factory.__dict__, "allowed_repos": (STACK,),
                                   "merge_policy": {STACK: {"deny": ("infra/**",)}}})
        with self.assertRaises(SmokeRefused) as caught:
            self.smoke(factory).preflight(STACK, STACK)
        self.assertIn("stack's own repository", str(caught.exception))

    def test_a_production_name_is_refused(self):
        factory = FactoryConfig(**{**self.factory.__dict__, "allowed_repos": ("owner/infrastructure-app",),
                                   "merge_policy": {"owner/infrastructure-app": {"deny": ("infra/**",)}}})
        with self.assertRaises(SmokeRefused) as caught:
            self.smoke(factory).preflight("owner/infrastructure-app", "owner/infrastructure-app")
        self.assertIn("factory-smoke-", str(caught.exception))

    def test_a_target_without_a_merge_policy_is_refused_instead_of_being_widened(self):
        factory = FactoryConfig(**{**self.factory.__dict__, "merge_policy": {}})
        with self.assertRaises(SmokeRefused) as caught:
            self.smoke(factory).preflight(REPO, REPO)
        self.assertIn("FORGEHANDS_MERGE_POLICY", str(caught.exception))
        self.assertEqual(self.factory.denied_paths(REPO), ("infra/**", "AGENTS.md"))

    def test_a_policy_that_does_not_deny_the_protected_fixture_is_refused(self):
        factory = FactoryConfig(**{**self.factory.__dict__, "merge_policy": {REPO: {"deny": ("AGENTS.md",)}}})
        with self.assertRaises(SmokeRefused) as caught:
            self.smoke(factory).preflight(REPO, REPO)
        self.assertIn(PROTECTED_FIXTURE, str(caught.exception))

    def test_an_unmarked_existing_repository_is_refused_and_left_alone(self):
        self.fake.seed(REPO, description="an application repository")
        self.refused(REPO, REPO)
        self.assertEqual(self.fake.repos[REPO]["description"], "an application repository")

    def test_an_existing_repository_that_is_not_private_is_refused(self):
        self.fake.seed(REPO, private=False)
        self.refused(REPO, REPO)

    def test_an_existing_pull_mirror_is_refused(self):
        self.fake.seed(REPO, mirror=True)
        self.refused(REPO, REPO)

    def test_the_allowlist_and_the_policy_are_never_extended(self):
        factory = FactoryConfig(**{**self.factory.__dict__, "allowed_repos": (REPO,)})
        self.smoke(factory).run(REPO, REPO)
        self.assertNotIn("infrastructure-app", factory.allowed_repos)
        self.assertEqual(factory.denied_paths(REPO), ("infra/**", "AGENTS.md"))


class PreflightTests(SmokeTest):
    def test_preflight_of_a_new_target_is_read_only(self):
        report = self.smoke(preflight_only=True).run(REPO, REPO)
        self.assertTrue(report.passed)
        self.assertIn("read-only", report.render())
        self.assertIn("would create", report.render())
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.fake.repos, {})

    def test_preflight_of_a_marked_target_says_reuse(self):
        self.fake.seed(REPO)
        report = self.smoke(preflight_only=True).run(REPO, REPO)
        self.assertIn("would reuse", report.render())
        self.assertEqual(self.writes(), [])


class NormalCaseTests(SmokeTest):
    def test_the_normal_and_later_cases_pass_and_report_the_chain(self):
        report = self.smoke().run(REPO, REPO)
        self.assertTrue(report.passed, report.render())
        text = report.render()
        self.assertIn("normal path: passed (ticket -> conversation -> draft pull request -> ci -> review -> merged -> issue closed", text)
        self.assertIn("ai:ready added later: passed", text)
        self.assertIn("protected path: passed", text)

    def test_the_ready_label_is_set_at_creation_and_later_through_the_labels_endpoint(self):
        self.smoke().run(REPO, REPO)
        created = [request for request in self.srv.requests
                   if request.path.endswith("/issues") and request.method == "POST"]
        self.assertEqual(len(created), 3)
        by_slug = {("protected" if "protected" in request.json["title"] else
                    "later" if "added later" in request.json["body"] else "normal"): request for request in created}
        self.assertTrue(by_slug["normal"].json.get("labels"))
        self.assertTrue(by_slug["protected"].json.get("labels"))
        self.assertFalse(by_slug["later"].json.get("labels"))
        added = [request for request in self.srv.requests
                 if request.path.endswith("/labels") and request.method == "POST" and "/issues/" in request.path]
        self.assertEqual(len(added), 1)
        self.assertTrue(added[0].json["labels"])

    def test_the_seeded_files_are_the_pinned_tests_only_workflow(self):
        self.smoke().run(REPO, REPO)   # cleanup deletes the repository again
        seeded = [request for request in self.srv.requests
                  if request.path.endswith("/contents/README.md") and request.method == "POST"]
        self.assertEqual(len(seeded), 1)
        workflow = [request for request in self.srv.requests
                    if request.path.endswith("/.forgejo/workflows/ci.yaml") and request.method == "POST"]
        self.assertEqual(len(workflow), 1)
        content = base64.b64decode(workflow[0].json["content"]).decode()
        self.assertIn("name: ci", content)
        self.assertIn("name: seed", content)
        self.assertIn("@sha256:", content)
        self.assertIn("bash check.sh", content)
        self.assertNotIn("docker push", content)

    def test_no_global_permission_is_widened(self):
        self.smoke().run(REPO, REPO)
        for request in self.srv.requests:
            path = request.path.split("?")[0]
            if request.method == "GET" and path == "/api/v1/admin/actions/runners":
                continue                      # runner readiness only reads the global runner list
            self.assertFalse(path.startswith("/api/v1/admin"), f"the smoke used an admin endpoint: {path}")
            self.assertNotIn("/tokens", path)
            self.assertNotIn("/collaborators", path)

    def test_runner_configuration_needs_the_docker_label(self):
        original = self.fake.__call__

        def without_runner(req):
            if req.path.split("?")[0].endswith("/actions/runners"):
                return 200, {"runners": [{"id": 1, "name": "other", "labels": ["linux"], "status": "idle"}]}
            return original(req)

        self.srv.close()
        self.srv = Server(without_runner)
        self.addCleanup(self.srv.close)
        self.cfg = config(tmp=self.tmp, forgejo_url=self.srv.url)
        report = self.smoke().run(REPO, REPO)
        self.assertFalse(report.passed)
        self.assertIn("no usable CI runner", report.render())

    def test_a_draft_pull_request_is_seen_as_draft(self):
        from hgboot.factory_smoke import Observation
        self.assertTrue(Observation(labels=(), pr={"title": "WIP: x", "state": "open"}).draft)
        self.assertTrue(Observation(labels=(), pr={"draft": True, "title": "x", "state": "open"}).draft)
        self.assertFalse(Observation(labels=(), pr={"title": "x", "state": "open"}).draft)


class ProtectedCaseTests(SmokeTest):
    def test_the_protected_pull_request_has_to_settle_approved_green_open_and_without_wip(self):
        report = self.smoke().run(REPO, REPO)
        self.assertTrue(report.passed, report.render())
        self.assertIn("approved, green, open, without WIP:", report.render())

    def test_a_merged_protected_pull_request_fails_the_smoke(self):
        self.fake.protected_merged = True
        report = self.smoke().run(REPO, REPO)
        self.assertFalse(report.passed)
        self.assertIn("protected path was merged", report.render())

    def test_a_protected_pull_request_that_stays_a_draft_times_out(self):
        self.fake.protected_wip = True
        report = self.smoke(timeout=60).run(REPO, REPO)
        self.assertFalse(report.passed)
        self.assertIn("protected path stalled", report.render())
        self.assertIn("draft", report.render())


class IdentityTests(SmokeTest):
    """The agent acts as its own Forgejo user: its pull request, labels and merge must carry that login."""

    def test_a_pull_request_opened_by_the_human_fails_the_smoke(self):
        self.fake.pr_author = "owner"
        report = self.smoke().run(REPO, REPO)
        self.assertFalse(report.passed)
        self.assertIn("attributed to owner, not the agent homegraf-agent", report.render())

    def test_a_merge_performed_by_the_human_fails_the_smoke(self):
        self.fake.merge_author = "owner"
        report = self.smoke().run(REPO, REPO)
        self.assertFalse(report.passed)
        self.assertIn("the merge is attributed to owner", report.render())

    def test_a_verdict_label_applied_by_the_human_fails_the_protected_case(self):
        self.fake.approval_actor = "owner"
        report = self.smoke().run(REPO, REPO)
        self.assertFalse(report.passed)
        self.assertIn("the ai:approved label is attributed to owner", report.render())

    def test_the_normal_case_needs_the_agent_to_have_started_a_conversation(self):
        self.fake.conversation = False
        report = self.smoke(timeout=60).run(REPO, REPO)
        self.assertFalse(report.passed)
        self.assertIn("stalled at issue closed", report.render())

    def test_a_label_without_an_agent_attribution_does_not_pass(self):
        original = self.fake.__call__

        def without_timeline(req):
            if req.path.split("?")[0].endswith("/timeline"):
                return 200, []
            return original(req)

        self.srv.close()
        self.srv = Server(without_timeline)
        self.addCleanup(self.srv.close)
        self.cfg = config(tmp=self.tmp, forgejo_url=self.srv.url)
        report = self.smoke(timeout=60).run(REPO, REPO)
        self.assertFalse(report.passed)
        self.assertIn("stalled at issue closed", report.render())


class TimeoutTests(SmokeTest):
    def test_a_stalled_worker_does_not_pass_the_smoke(self):
        self.fake.stall = True
        report = self.smoke(timeout=60).run(REPO, REPO)
        self.assertFalse(report.passed)
        self.assertIn("stalled at ticket", report.render())
        self.assertIn("waited 60 s", report.render())

    def test_escalating_a_stalled_worker_is_a_reference_not_a_pass(self):
        self.fake.stall = True
        self.fake.escalate_normal = True
        report = self.smoke(timeout=60).run(REPO, REPO)
        self.assertFalse(report.passed)
        self.assertIn("stalled at ticket", report.render())

    def test_the_strong_tier_is_reported_as_a_reference(self):
        self.fake.escalate_normal = True
        report = self.smoke().run(REPO, REPO)
        self.assertTrue(report.passed, report.render())
        self.assertIn("strong tier (reference)", report.render())


class CleanupTests(SmokeTest):
    def test_a_repository_created_by_the_run_is_deleted(self):
        report = self.smoke().run(REPO, REPO)
        self.assertIn("cleanup: deleted", report.render())
        self.assertEqual(self.fake.repos, {})
        deletes = [request for request in self.srv.requests if request.method == "DELETE"]
        self.assertEqual([request.path.split("?")[0] for request in deletes], [f"/api/v1/repos/{REPO}"])

    def test_a_marked_repository_that_already_existed_keeps_its_repository(self):
        self.fake.seed(REPO)
        report = self.smoke().run(REPO, REPO)
        self.assertTrue(report.passed, report.render())
        self.assertIn(REPO, self.fake.repos)
        self.assertIn("issues created by this run", report.render())
        deletes = [request.path.split("?")[0] for request in self.srv.requests if request.method == "DELETE"]
        self.assertEqual(len(deletes), 3)
        self.assertTrue(all("/issues/" in path for path in deletes))

    def test_keep_retains_the_artifacts_and_deletes_nothing(self):
        report = self.smoke(keep=True).run(REPO, REPO)
        self.assertTrue(report.passed, report.render())
        self.assertIn("cleanup: kept", report.render())
        self.assertEqual(report.retained[0], REPO)
        self.assertEqual(len(report.retained), 4)
        self.assertEqual([request for request in self.srv.requests if request.method == "DELETE"], [])

    def test_cleanup_runs_after_a_failure(self):
        self.fake.stall = True
        report = self.smoke(timeout=60).run(REPO, REPO)
        self.assertFalse(report.passed)
        self.assertIn("cleanup: deleted", report.render())
        self.assertEqual(self.fake.repos, {})

    def test_cleanup_never_touches_an_unrelated_repository(self):
        self.fake.seed("owner/unrelated")
        self.smoke(keep=False).run(REPO, REPO)
        self.assertIn("owner/unrelated", self.fake.repos)


class ReportTests(unittest.TestCase):
    def test_the_report_redacts_secrets_and_strips_url_queries(self):
        report = SmokeReport(REPO, [Stage("result", "failed", f"token {TOKEN} at http://h/x?token={TOKEN}")])
        text = report.render([TOKEN])
        self.assertNotIn(TOKEN, text)
        self.assertIn("http://h/x", text)
        self.assertNotIn("?token=", text)

    def test_a_conversation_link_with_a_token_is_sanitised_in_the_run_report(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        fake = FakeFactory()
        srv = Server(fake)
        self.addCleanup(srv.close)
        cfg = config(tmp=tmp, forgejo_url=srv.url)
        state = State(f"{tmp}/state.json")
        state.set("forgejo", "token", TOKEN)
        report = Smoke(FactoryConfig(enabled=True, repository=STACK, allowed_repos=(REPO,),
                                     allowed_senders=("owner", "homegraf-agent"), agent_login="homegraf-agent",
                                     human_login="owner", forgejo_webhook_secret=SECRET,
                                     merge_policy={REPO: {"deny": ("infra/**",)}}),
                       cfg, state, token=TOKEN, sleep=fake.sleep, clock=fake.clock, timeout=120, interval=5).run(REPO, REPO)
        rendered = report.render((TOKEN, SECRET))
        self.assertNotIn("conversation-secret", rendered)
        self.assertIn("http://openhands.local:8000/conversations/abc123", rendered)


class CommandTests(SmokeTest):
    def env(self, **over):
        values = {"ADMIN_EMAIL": "admin@example.org", "ADMIN_PASSWORD": "pw-correct-1234", "DOMAIN": "home.lab",
                  "HOST_IP": "192.168.1.10", "DATA_DIR": "/srv/hg", "KOMODO_WEBHOOK_SECRET": "hook-secret",
                  "STATE_DIR": self.tmp, "FORGEJO_URL": self.srv.url, "FORGEHANDS_BOOTSTRAP": "1",
                  "FORGEHANDS_OPENHANDS_API_URL": "http://openhands:8000", "OPENHANDS_LOCAL_BACKEND_API_KEY": "b" * 24,
                  "FORGEHANDS_STRONG_MODEL": "strong-model", "FORGEHANDS_CHEAP_MODEL": "cheap-model",
                  "FORGEHANDS_GATEWAY_URL": "http://gateway:4000", "FORGEHANDS_GATEWAY_API_KEY": "g" * 24,
                  "FORGEHANDS_ALLOWED_REPOS": REPO, "FORGEHANDS_ALLOWED_SENDERS": "owner,homegraf-agent",
                  "FORGEHANDS_HUMAN_LOGIN": "owner", "FORGEHANDS_FORGEJO_WEBHOOK_SECRET": SECRET,
                  "FORGEHANDS_RUNTIME_DIR": f"{self.tmp}/runtime",
                  "FORGEHANDS_MERGE_POLICY": json.dumps({REPO: {"deny": ["infra/**"]}})}
        values.update(over)
        return values

    def command(self, argv, **over):
        out, err = io.StringIO(), io.StringIO()
        real = factory_smoke.Smoke

        def build(*args, **kwargs):
            kwargs.setdefault("sleep", self.fake.sleep)
            kwargs.setdefault("clock", self.fake.clock)
            return real(*args, **kwargs)

        with mock.patch.object(factory_smoke, "Smoke", build), \
                mock.patch.dict(os.environ, {"HGBOOT_QUIET": ""}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = factory_smoke.command(argv, self.env(**over))
        return code, out.getvalue(), err.getvalue()

    def test_help_writes_nothing_and_needs_no_configuration(self):
        code, out, err = self.command(["--help"], ADMIN_EMAIL="")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("--confirm-scratch", out)
        self.assertIn("spend no tokens", out)
        self.assertEqual(self.srv.requests, [])

    def test_a_missing_confirmation_is_a_usage_error(self):
        code, _, err = self.command(["--repo", REPO])
        self.assertEqual(code, 2)
        self.assertIn("--confirm-scratch", err)
        self.assertEqual(self.srv.requests, [])

    def test_a_mismatched_confirmation_is_a_refusal(self):
        code, _, err = self.command(["--repo", REPO, "--confirm-scratch", "owner/other-scratch"])
        self.assertEqual(code, 2)
        self.assertIn("does not repeat", err)
        self.assertEqual(self.writes(), [])

    def test_the_preflight_flag_stops_before_any_write(self):
        code, out, _ = self.command(["--repo", REPO, "--confirm-scratch", REPO, "--preflight"])
        self.assertEqual(code, 0)
        self.assertIn("read-only", out)
        self.assertEqual(self.writes(), [])

    def test_a_disabled_factory_is_refused(self):
        code, _, err = self.command(["--repo", REPO, "--confirm-scratch", REPO], FORGEHANDS_BOOTSTRAP="")
        self.assertEqual(code, 2)
        self.assertIn("not opted in", err)

    def test_an_unknown_argument_is_a_usage_error(self):
        code, _, err = self.command(["--repo", REPO, "--confirm-scratch", REPO, "--force"])
        self.assertEqual(code, 2)
        self.assertIn("unknown argument", err)

    def test_the_token_warning_comes_before_the_writes(self):
        errors = []
        original = factory_smoke.warn
        factory_smoke.warn = lambda step, message: errors.append(message)
        self.addCleanup(setattr, factory_smoke, "warn", original)
        code, _, _ = self.command(["--repo", REPO, "--confirm-scratch", REPO])
        self.assertEqual(code, 0)
        self.assertTrue(errors and "spends model tokens" in errors[0])
        self.assertTrue(self.writes())


class IncludeEpicFlagTests(SmokeTest):
    def test_the_flag_parses_and_defaults_to_false(self):
        options = factory_smoke.parse_args(["--repo", REPO, "--confirm-scratch", REPO, "--include-epic"])
        self.assertTrue(options.include_epic)
        options = factory_smoke.parse_args(["--repo", REPO, "--confirm-scratch", REPO])
        self.assertFalse(options.include_epic)

    def test_a_repeated_flag_is_a_usage_error(self):
        with self.assertRaises(factory_smoke.UsageError):
            factory_smoke.parse_args(["--repo", REPO, "--confirm-scratch", REPO, "--include-epic", "--include-epic"])

    def test_the_help_output_lists_the_flag(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(hgmain.main(["factory-smoke", "--help"]), 0)
        self.assertIn("--include-epic", out.getvalue())

    def test_the_warning_names_the_extra_tokens_before_the_first_write(self):
        messages = []

        def capture(step, message):
            self.assertEqual(self.writes(), [], "the token warning came after a write")
            messages.append(message)

        original = factory_smoke.warn
        factory_smoke.warn = capture
        self.addCleanup(setattr, factory_smoke, "warn", original)
        report = self.smoke(include_epic=True).run(REPO, REPO)
        self.assertTrue(report.passed, report.render())
        self.assertEqual(len(messages), 1)
        self.assertIn("one epic with three children plus a follow-up issue", messages[0])
        self.assertIn("several times the tokens", messages[0])
        self.assertTrue(self.writes())
        self.assertIn("epic scenarios: skipped (not implemented yet)", report.render())

    def test_without_the_flag_the_report_has_no_epic_stage(self):
        report = self.smoke().run(REPO, REPO)
        self.assertTrue(report.passed, report.render())
        self.assertNotIn("epic scenarios", report.render())

    def test_preflight_with_the_flag_writes_nothing_and_says_what_would_be_added(self):
        report = self.smoke(preflight_only=True, include_epic=True).run(REPO, REPO)
        self.assertTrue(report.passed)
        self.assertEqual(self.writes(), [])
        self.assertIn("epic scenarios would be added", report.render())


class WiringTests(SmokeTest):
    def test_main_routes_the_subcommand_and_help_writes_nothing(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(hgmain.main(["factory-smoke", "--help"]), 0)
        self.assertIn("--confirm-scratch", out.getvalue())

    def test_an_unknown_command_lists_factory_smoke(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(hgmain.main(["factory-smoke-typo"]), 2)
        self.assertIn("factory-smoke --repo", err.getvalue())

    def test_a_normal_run_never_starts_the_smoke(self):
        from .test_main import Stack
        stack = Stack()
        self.addCleanup(stack.close)
        with mock.patch("hgboot.factory_smoke.Smoke", side_effect=AssertionError("the normal run must not smoke")):
            code, _, err = stack.run()
        self.assertEqual((code, err), (0, ""))


class ParseTests(unittest.TestCase):
    def test_defaults_and_equals_form(self):
        options = factory_smoke.parse_args(["--repo=" + REPO, "--confirm-scratch=" + REPO, "--keep", "--timeout", "42"])
        self.assertEqual((options.repo, options.confirm, options.keep), (REPO, REPO, True))
        self.assertEqual((options.timeout, options.interval), (42.0, factory_smoke.DEFAULT_INTERVAL))

    def test_a_bad_timeout_is_a_usage_error(self):
        for value in ("soon", "0", "90000"):
            with self.assertRaises(factory_smoke.UsageError):
                factory_smoke.parse_args(["--repo", REPO, "--confirm-scratch", REPO, "--timeout", value])

    def test_a_repository_with_a_dot_segment_is_a_usage_error(self):
        for value in ("owner/../x", "owner", "owner/x y"):
            with self.assertRaises(factory_smoke.UsageError):
                factory_smoke.parse_args(["--repo", value, "--confirm-scratch", value])

    def test_the_smoke_does_not_run_without_an_operator_token(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        out, err = io.StringIO(), io.StringIO()
        env = {"ADMIN_EMAIL": "admin@example.org", "ADMIN_PASSWORD": "pw", "DOMAIN": "home.lab", "HOST_IP": "1.2.3.4",
               "DATA_DIR": "/srv/hg", "KOMODO_WEBHOOK_SECRET": "hook-secret", "STATE_DIR": tmp,
               "FORGEHANDS_BOOTSTRAP": "1", "FORGEHANDS_OPENHANDS_API_URL": "http://openhands:8000",
               "OPENHANDS_LOCAL_BACKEND_API_KEY": "b" * 24, "FORGEHANDS_STRONG_MODEL": "s", "FORGEHANDS_CHEAP_MODEL": "c",
               "FORGEHANDS_GATEWAY_URL": "http://gateway:4000", "FORGEHANDS_GATEWAY_API_KEY": "g" * 24,
               "FORGEHANDS_ALLOWED_REPOS": REPO, "FORGEHANDS_ALLOWED_SENDERS": "owner,homegraf-agent",
               "FORGEHANDS_HUMAN_LOGIN": "owner", "FORGEHANDS_FORGEJO_WEBHOOK_SECRET": SECRET,
               "FORGEHANDS_RUNTIME_DIR": f"{tmp}/runtime"}
        with mock.patch.dict(os.environ, {"HGBOOT_QUIET": ""}), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = factory_smoke.command(["--repo", REPO, "--confirm-scratch", REPO], env)
        self.assertEqual(code, 2)
        self.assertIn("token is missing", err.getvalue())


class ExceptionTests(unittest.TestCase):
    def test_the_errors_are_step_errors_so_main_can_catch_them(self):
        from hgboot.steps import StepError
        for error in (SmokeRefused, SmokeFailure, SmokeTimeout):
            self.assertTrue(issubclass(error, StepError))
