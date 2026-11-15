import pathlib
import shutil
import tempfile
import unittest
from urllib.parse import unquote

from hgboot import seed
from hgboot.factory_ci import (REQUIRED_CHECKS, SEED_WORKFLOW, FactoryCI, audit_seed_workflow,
                               missing_ci_remediation)
from hgboot.state import State
from hgboot.steps import StepError

from .fakes import Server
from .helpers import config

SEED = pathlib.Path(__file__).resolve().parents[2] / "seed"
REPO = "owner/infrastructure"


class FakeCIForgejo:
    """The Forgejo endpoints the CI reconciliation uses, on a real loopback server."""

    def __init__(self):
        self.default_branch = "trunk"
        self.protections: list[dict] = []
        self.runners = {"repository": [], "global": []}
        self.runner_status = {"repository": 200, "global": 200}
        self.identity = "owner"
        self.writes: list[tuple] = []

    def __call__(self, req):
        path, _, _query = req.path.partition("?")
        if path == "/api/v1/user":
            return 200, {"login": self.identity}
        if path == f"/api/v1/repos/{REPO}":
            return 200, {"full_name": REPO, "default_branch": self.default_branch}
        if path == f"/api/v1/repos/{REPO}/branch_protections":
            if req.method == "GET":
                return 200, self.protections
            if req.method == "POST":
                protection = dict(req.json)
                self.protections.append(protection)
                self.writes.append(("POST", path, req.json))
                return 201, protection
        if path.startswith(f"/api/v1/repos/{REPO}/branch_protections/"):
            name = unquote(path.rsplit("/", 1)[1])
            protection = next((p for p in self.protections if p.get("rule_name") == name), None)
            if protection is None:
                return 404, {"message": "not found"}
            if req.method == "GET":
                return 200, protection
            if req.method == "PATCH":
                protection.update(req.json)
                self.writes.append(("PATCH", path, req.json))
                return 200, protection
        if path == f"/api/v1/repos/{REPO}/actions/runners":
            status = self.runner_status["repository"]
            return (status, self.runners["repository"]) if status == 200 else (status, {"message": "forbidden"})
        if path == "/api/v1/admin/actions/runners":
            status = self.runner_status["global"]
            return (status, self.runners["global"]) if status == 200 else (status, {"message": "forbidden"})
        return 404, {"message": f"unexpected {req.method} {path}"}


class FactoryCITest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.fake = FakeCIForgejo()
        self.srv = Server(self.fake)
        self.addCleanup(self.srv.close)
        self.cfg = config(tmp=self.tmp, forgejo_url=self.srv.url)
        self.state = State(f"{self.tmp}/state.json")
        self.state.set("forgejo", "token", "secret-token")

    def ci(self, **over):
        return FactoryCI(self.cfg, self.state, REPO, **over)


class RunnerReadinessTests(FactoryCITest):
    def test_a_runner_with_the_matching_label_and_online_status_is_ready(self):
        self.fake.runners["repository"] = [{"id": 1, "name": "homegraf-runner",
                                            "labels": ["docker:docker://node:22-bookworm"], "status": "idle"}]
        readiness = self.ci().runner_readiness()
        self.assertTrue(readiness.ready)
        self.assertIn("homegraf-runner", readiness.reason)
        self.assertEqual(readiness.scopes, ("repository", "global"))

    def test_the_plain_label_matches_too(self):
        self.fake.runners["repository"] = [{"id": 1, "name": "short", "labels": ["docker"], "status": "active"}]
        self.assertTrue(self.ci().runner_readiness().ready)

    def test_runner_absence_blocks_readiness(self):
        self.fake.runners["repository"] = [{"id": 1, "name": "other", "labels": ["linux"], "status": "idle"}]
        readiness = self.ci().runner_readiness()
        self.assertFalse(readiness.ready)
        self.assertIn("no runner labelled docker", readiness.reason)

    def test_an_offline_runner_blocks_readiness(self):
        self.fake.runners["repository"] = [{"id": 1, "name": "sleeping", "labels": ["docker"], "status": "offline"}]
        readiness = self.ci().runner_readiness()
        self.assertFalse(readiness.ready)
        self.assertIn("offline", readiness.reason)
        self.assertIn("sleeping", readiness.reason)

    def test_a_runner_in_another_applicable_scope_counts(self):
        self.fake.runners["global"] = [{"id": 7, "name": "global-runner", "labels": ["docker"], "status": "idle"}]
        readiness = self.ci().runner_readiness()
        self.assertTrue(readiness.ready)
        self.assertIn("global-runner", readiness.reason)

    def test_an_inaccessible_runner_api_blocks_readiness(self):
        self.fake.runner_status = {"repository": 403, "global": 403}
        readiness = self.ci().runner_readiness()
        self.assertFalse(readiness.ready)
        self.assertIn("blocked", readiness.reason)
        self.assertIn("403", readiness.reason)

    def test_a_missing_token_blocks_readiness_without_a_request(self):
        readiness = FactoryCI(self.cfg, self.state, REPO, token="").runner_readiness()
        self.assertFalse(readiness.ready)
        self.assertIn("no Forgejo token", readiness.reason)
        self.assertEqual(self.srv.requests, [])


class RequiredChecksTests(FactoryCITest):
    def test_the_default_branch_is_read_and_unrelated_protections_are_preserved(self):
        self.fake.protections = [
            {"rule_name": "trunk", "enable_status_check": False, "status_check_contexts": ["legacy/build"],
             "enable_push": True, "required_approvals": 1, "enable_push_whitelist": True,
             "push_whitelist_usernames": ["ops"]},
            {"rule_name": "release", "enable_status_check": True, "status_check_contexts": ["release-check"]},
        ]
        result = self.ci().reconcile_required_checks()
        self.assertEqual((result.branch, result.action), ("trunk", "updated"))
        self.assertEqual(result.contexts, ("legacy/build",) + REQUIRED_CHECKS)
        patches = [w for w in self.fake.writes if w[0] == "PATCH"]
        self.assertEqual(len(patches), 1)
        self.assertEqual(patches[0][1], f"/api/v1/repos/{REPO}/branch_protections/trunk")
        self.assertEqual(patches[0][2], {"enable_status_check": True,
                                         "status_check_contexts": ["legacy/build", REQUIRED_CHECKS[0]]})
        self.assertNotIn("enable_push_whitelist", patches[0][2])
        self.assertNotIn("push_whitelist_usernames", patches[0][2])
        self.assertEqual(self.fake.protections[1], {"rule_name": "release", "enable_status_check": True,
                                                    "status_check_contexts": ["release-check"]})
        self.assertFalse([r for r in self.srv.requests if "/release" in r.path])

    def test_a_missing_protection_is_created_for_the_default_branch_without_push_restrictions(self):
        result = self.ci().reconcile_required_checks()
        self.assertEqual((result.branch, result.action), ("trunk", "created"))
        posts = [w for w in self.fake.writes if w[0] == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][2], {"rule_name": "trunk", "enable_push": True, "enable_status_check": True,
                                       "status_check_contexts": [REQUIRED_CHECKS[0]]})
        for forbidden in ("enable_push_whitelist", "push_whitelist_usernames", "push_whitelist_teams"):
            self.assertNotIn(forbidden, posts[0][2])

    def test_a_second_run_writes_nothing(self):
        self.fake.protections = [{"rule_name": "trunk", "enable_status_check": True,
                                  "status_check_contexts": list(REQUIRED_CHECKS)}]
        self.assertEqual(self.ci().reconcile_required_checks().action, "unchanged")
        self.srv.requests.clear()
        self.fake.writes.clear()
        result = self.ci().reconcile_required_checks()
        self.assertEqual(result.action, "unchanged")
        self.assertEqual(self.fake.writes, [])
        self.assertFalse([r for r in self.srv.requests if r.method in ("POST", "PATCH", "PUT", "DELETE")])

    def test_a_repository_without_a_default_branch_is_not_guessed(self):
        self.fake.default_branch = ""
        with self.assertRaisesRegex(StepError, "no default branch"):
            self.ci().reconcile_required_checks()

    def test_no_token_means_no_write(self):
        ci = FactoryCI(self.cfg, self.state, REPO, token="")
        with self.assertRaisesRegex(StepError, "no Forgejo token"):
            ci.reconcile_required_checks()
        self.assertEqual(self.srv.requests, [])


class SeedWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.cfg = config(domain="lab.example", host_ip="10.0.0.5", admin_user="owner", data_dir="/srv/hg")
        self.files = seed.collect(SEED, seed.substitutions(self.cfg, "main"))
        self.workflow = dict(self.files)[SEED_WORKFLOW].decode()

    def test_the_real_seed_collects_the_test_only_workflow(self):
        self.assertIn(SEED_WORKFLOW, dict(self.files))
        self.assertIn("name: ci", self.workflow)
        self.assertIn("runs-on: docker", self.workflow)
        self.assertIn("  seed:", self.workflow)
        self.assertRegex(self.workflow, r"python:3\.\d+-slim-bookworm@sha256:[0-9a-f]{64}")
        self.assertIn('git -c http.extraheader="Authorization: token $TOKEN" fetch', self.workflow)
        self.assertIn('"${{ github.sha }}"', self.workflow)
        self.assertNotIn("uses:", self.workflow)
        for forbidden in ("docker push", "build-push-action", "komodo"):
            self.assertNotIn(forbidden, self.workflow)
        for seed_path in ("infra/ingress-controller[test]", "infra/graph", "infra/homelab", "infra/infisical",
                          "infra/stackctl", "infra/ingress-controller/tests"):
            self.assertIn(seed_path, self.workflow)
        for root_path in ("bootstrap/", "scripts/check-seed.sh", "app/"):
            self.assertNotIn(root_path, self.workflow)

    def test_collecting_the_seed_twice_changes_nothing(self):
        again = seed.collect(SEED, seed.substitutions(self.cfg, "main"))
        self.assertEqual(dict(self.files), dict(again))
        self.assertEqual(dict(again)[SEED_WORKFLOW], self.workflow.encode())

    def test_a_missing_workflow_in_an_existing_repository_is_reported_not_pushed(self):
        state = audit_seed_workflow(self.files, lambda path: None, repository=REPO)
        self.assertEqual((state.path, state.state), (SEED_WORKFLOW, "missing"))
        self.assertIn("never pushes", state.message)
        self.assertIn("pull request", state.message)
        self.assertIn(REPO, state.message)

    def test_an_existing_user_workflow_is_left_untouched(self):
        state = audit_seed_workflow(self.files, lambda path: "name: mine\n", repository=REPO)
        self.assertEqual(state.state, "custom")
        self.assertIn("left untouched", state.message)

    def test_the_same_workflow_is_present(self):
        state = audit_seed_workflow(self.files, lambda path: self.workflow, repository=REPO)
        self.assertEqual(state.state, "present")

    def test_a_seed_without_the_workflow_is_an_error(self):
        with self.assertRaisesRegex(ValueError, SEED_WORKFLOW):
            audit_seed_workflow([("README.md", b"# hi\n")], lambda path: None)
        self.assertIn("pull request", missing_ci_remediation(REPO))


if __name__ == "__main__":
    unittest.main()
