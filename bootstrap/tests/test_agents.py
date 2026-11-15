"""The label routes of ForgeHands direct mode and the defaults a new installation gets."""
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

from hgboot.agents import LABELS, label_routes

ROOT = pathlib.Path(__file__).resolve().parents[2]
PROFILE_IDS = {"planner": "11111111-1111-1111-1111-111111111111",
               "worker": "22222222-2222-2222-2222-222222222222",
               "worker-strong": "33333333-3333-3333-3333-333333333333",
               "reviewer": "44444444-4444-4444-4444-444444444444"}
EXPECTED_ROUTES = {
    "ai:plan": {"profile": PROFILE_IDS["planner"], "tier": "planner", "max_iterations": 80},
    "ai:ready": {"profile": PROFILE_IDS["worker"], "tier": "worker", "max_iterations": 120},
    "ai:escalate": {"profile": PROFILE_IDS["worker-strong"], "tier": "worker-strong", "max_iterations": 120},
    "ai:review": {"profile": PROFILE_IDS["reviewer"], "tier": "reviewer", "max_iterations": 60},
    "ai:changes": {"profile": PROFILE_IDS["worker"], "tier": "worker", "max_iterations": 120,
                   "on": "pull_request"},
    "ci:failure": {"profile": PROFILE_IDS["worker"], "tier": "worker", "max_iterations": 120,
                   "on": "ci_failure"},
    "ci:success": {"profile": PROFILE_IDS["reviewer"], "tier": "reviewer", "max_iterations": 60,
                   "on": "ci_success"},
    "epic:done": {"profile": PROFILE_IDS["reviewer"], "tier": "reviewer", "max_iterations": 60,
                  "on": "epic_done"},
}


class LabelRoutesTests(unittest.TestCase):
    def test_all_eight_direct_mode_routes_use_the_profile_ids_and_limits(self):
        routes = json.loads(label_routes(PROFILE_IDS))
        self.assertEqual(set(routes), {"ai:plan", "ai:ready", "ai:escalate", "ai:review", "ai:changes",
                                       "ci:failure", "ci:success", "epic:done"})
        self.assertEqual(routes, EXPECTED_ROUTES)
        self.assertEqual(set(route["profile"] for route in routes.values()), set(PROFILE_IDS.values()))

    def test_the_json_is_stable_and_keeps_the_route_order(self):
        self.assertEqual(label_routes(PROFILE_IDS), json.dumps(EXPECTED_ROUTES, separators=(",", ":")))

    def test_only_the_follow_up_and_synthetic_routes_name_an_event(self):
        routes = json.loads(label_routes(PROFILE_IDS))
        self.assertEqual({label for label, route in routes.items() if "on" in route},
                         {"ai:changes", "ci:failure", "ci:success", "epic:done"})
        self.assertEqual(routes["ai:changes"]["on"], "pull_request")
        self.assertEqual(routes["ci:failure"]["on"], "ci_failure")
        self.assertEqual(routes["ci:success"]["on"], "ci_success")
        self.assertEqual(routes["epic:done"]["on"], "epic_done")

    def test_a_profile_without_an_id_is_an_error_not_a_name(self):
        ids = dict(PROFILE_IDS)
        del ids["reviewer"]
        with self.assertRaisesRegex(ValueError, "reviewer"):
            label_routes(ids)

    def test_a_blank_profile_id_is_an_error_not_a_name(self):
        ids = dict(PROFILE_IDS)
        ids["reviewer"] = "   "
        with self.assertRaisesRegex(ValueError, "reviewer"):
            label_routes(ids)

    def test_a_blank_role_name_is_an_error(self):
        ids = dict(PROFILE_IDS)
        ids[""] = PROFILE_IDS["worker"]
        with self.assertRaisesRegex(ValueError, "name"):
            label_routes(ids)

    def test_one_tier_per_route_and_the_strong_tier_is_the_escalation(self):
        tiers = {label: tier for label, _, tier, _, _ in LABELS}
        self.assertEqual(tiers["ai:escalate"], "worker-strong")
        self.assertEqual(tiers["ci:failure"], "worker")
        self.assertEqual(tiers["ci:success"], "reviewer")
        self.assertEqual(tiers["epic:done"], "reviewer")
        self.assertEqual(len(tiers), 8)
        self.assertEqual(set(tiers.values()), {"planner", "worker", "worker-strong", "reviewer"})


class GeneratedDefaultsTests(unittest.TestCase):
    def test_setup_does_not_enable_direct_mode_with_profile_names(self):
        """A fresh stack has no profile ids, so direct mode stays off (#25 provides them)."""
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copy(ROOT / "setup.sh", tmp)
            env = {**os.environ, "HOST_IP": "10.0.0.10", "DNS_MODE": "external",
                   "ADMIN_EMAIL": "owner@lab.example", "ADMIN_PASSWORD": "correct-horse-1",
                   "OPTIONAL_SERVICES": "agents"}
            subprocess.run(["bash", "setup.sh", "--yes"], cwd=tmp, env=env, check=True, capture_output=True)
            generated = dict(line.split("=", 1) for line in (pathlib.Path(tmp) / ".env").read_text().splitlines()
                             if "=" in line and not line.startswith("#"))
        self.assertEqual(generated.get("FORGEHANDS_OPENHANDS_API_URL", ""), "")
        self.assertEqual(generated.get("FORGEHANDS_LABEL_ROUTES", ""), "")
