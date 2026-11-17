"""The label routes of ForgeHands direct mode and the defaults a new installation gets."""
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

from hgboot.agents import _validate, label_routes

ROOT = pathlib.Path(__file__).resolve().parents[2]
PROFILE_IDS = {"planner": "11111111-1111-1111-1111-111111111111",
               "worker": "22222222-2222-2222-2222-222222222222",
               "worker-strong": "33333333-3333-3333-3333-333333333333",
               "reviewer": "44444444-4444-4444-4444-444444444444",
               "sensitive-reviewer": "55555555-5555-5555-5555-555555555555",
               "worker-free": "66666666-6666-6666-6666-666666666666"}
WITHOUT_FREE = {name: profile_id for name, profile_id in PROFILE_IDS.items() if name != "worker-free"}
ROUTE_KEYS = {"ai:plan", "ai:ready", "ai:ready-free", "ai:escalate", "ai:review", "ai:changes",
              "ai:changes-free", "ci:failure", "ci:failure-free", "ci:success", "epic:done",
              "pr:conflict", "sensitive:review"}
EXPECTED_ROUTES = {
    "ai:plan": {"profile": PROFILE_IDS["planner"], "tier": "smart", "max_iterations": 80},
    "ai:ready": {"profile": PROFILE_IDS["worker"], "tier": "cheap", "max_iterations": 120,
                 "fallback_to": "ai:ready-free"},
    "ai:ready-free": {"profile": PROFILE_IDS["worker-free"], "tier": "free"},
    "ai:escalate": {"profile": PROFILE_IDS["worker-strong"], "tier": "smart", "max_iterations": 120},
    "ai:review": {"profile": PROFILE_IDS["reviewer"], "tier": "smart", "max_iterations": 60},
    "ai:changes": {"profile": PROFILE_IDS["worker"], "tier": "cheap", "max_iterations": 120,
                   "on": "pull_request", "fallback_to": "ai:changes-free", "escalate_to": "ai:escalate"},
    "ai:changes-free": {"profile": PROFILE_IDS["worker-free"], "tier": "free"},
    "ci:failure": {"profile": PROFILE_IDS["worker"], "tier": "cheap", "max_iterations": 120,
                   "on": "ci_failure", "fallback_to": "ci:failure-free", "escalate_to": "ai:escalate"},
    "ci:failure-free": {"profile": PROFILE_IDS["worker-free"], "tier": "free"},
    "ci:success": {"profile": PROFILE_IDS["reviewer"], "tier": "smart", "max_iterations": 60,
                   "on": "ci_success"},
    "epic:done": {"profile": PROFILE_IDS["reviewer"], "tier": "smart", "max_iterations": 60,
                  "on": "epic_done"},
    "pr:conflict": {"profile": PROFILE_IDS["worker-strong"], "tier": "smart", "max_iterations": 120,
                    "on": "conflict"},
    "sensitive:review": {"profile": PROFILE_IDS["sensitive-reviewer"], "tier": "smart",
                         "max_iterations": 60, "on": "sensitive_review"},
}


class LabelRoutesTests(unittest.TestCase):
    def test_all_thirteen_routes_use_the_profile_ids_tiers_and_limits(self):
        routes = json.loads(label_routes(PROFILE_IDS))
        self.assertEqual(set(routes), ROUTE_KEYS)
        self.assertEqual(routes, EXPECTED_ROUTES)
        self.assertEqual(set(route["profile"] for route in routes.values()), set(PROFILE_IDS.values()))

    def test_the_json_is_stable_and_keeps_the_route_order(self):
        self.assertEqual(label_routes(PROFILE_IDS), json.dumps(EXPECTED_ROUTES, separators=(",", ":")))

    def test_only_the_follow_up_and_synthetic_routes_name_an_event(self):
        routes = json.loads(label_routes(PROFILE_IDS))
        self.assertEqual({label for label, route in routes.items() if "on" in route},
                         {"ai:changes", "ci:failure", "ci:success", "epic:done", "pr:conflict", "sensitive:review"})
        self.assertEqual(routes["ai:changes"]["on"], "pull_request")
        self.assertEqual(routes["ci:failure"]["on"], "ci_failure")
        self.assertEqual(routes["ci:success"]["on"], "ci_success")
        self.assertEqual(routes["epic:done"]["on"], "epic_done")
        self.assertEqual(routes["pr:conflict"]["on"], "conflict")
        self.assertEqual(routes["sensitive:review"]["on"], "sensitive_review")

    def test_the_worker_routes_name_a_free_fallback_and_rounds_escalate_to_the_strong_worker(self):
        routes = json.loads(label_routes(PROFILE_IDS))
        self.assertEqual({label: route["fallback_to"] for label, route in routes.items() if "fallback_to" in route},
                         {"ai:ready": "ai:ready-free", "ai:changes": "ai:changes-free",
                          "ci:failure": "ci:failure-free"})
        for label in ("ai:ready-free", "ai:changes-free", "ci:failure-free"):
            self.assertEqual(routes[label], {"profile": PROFILE_IDS["worker-free"], "tier": "free"})
        for label in ("ai:changes", "ci:failure"):
            self.assertEqual(routes[label]["escalate_to"], "ai:escalate")
        self.assertEqual(routes["ai:escalate"]["profile"], PROFILE_IDS["worker-strong"])
        self.assertEqual(routes["ai:escalate"]["tier"], "smart")

    def test_a_conflict_round_runs_on_the_strong_worker_on_the_smart_tier(self):
        routes = json.loads(label_routes(PROFILE_IDS))
        self.assertEqual(routes["pr:conflict"]["profile"], PROFILE_IDS["worker-strong"])
        self.assertEqual(routes["pr:conflict"]["tier"], "smart")

    def test_without_a_worker_free_id_there_are_no_fallback_routes_and_no_fallback_to(self):
        routes = json.loads(label_routes(WITHOUT_FREE))
        self.assertEqual(set(routes), ROUTE_KEYS - {"ai:ready-free", "ai:changes-free", "ci:failure-free"})
        self.assertEqual({label for label, route in routes.items() if "fallback_to" in route}, set())
        self.assertNotIn(PROFILE_IDS["worker-free"], json.dumps(routes))
        self.assertEqual(routes["ai:changes"]["escalate_to"], "ai:escalate")

    def test_a_profile_without_an_id_is_an_error_not_a_name(self):
        ids = dict(PROFILE_IDS)
        del ids["reviewer"]
        with self.assertRaisesRegex(ValueError, "reviewer"):
            label_routes(ids)

    def test_a_missing_sensitive_reviewer_id_is_an_error(self):
        ids = dict(PROFILE_IDS)
        del ids["sensitive-reviewer"]
        with self.assertRaisesRegex(ValueError, "sensitive-reviewer"):
            label_routes(ids)

    def test_a_blank_profile_id_is_an_error_not_a_name(self):
        ids = dict(PROFILE_IDS)
        ids["worker-free"] = "   "
        with self.assertRaisesRegex(ValueError, "worker-free"):
            label_routes(ids)

    def test_a_blank_role_name_is_an_error(self):
        ids = dict(PROFILE_IDS)
        ids[""] = PROFILE_IDS["worker"]
        with self.assertRaisesRegex(ValueError, "name"):
            label_routes(ids)

    def test_the_reference_tiers_are_cheap_for_workers_smart_for_the_strong_side_and_free_for_fallbacks(self):
        tiers = {label: route["tier"] for label, route in json.loads(label_routes(PROFILE_IDS)).items()}
        self.assertEqual({label for label, tier in tiers.items() if tier == "cheap"},
                         {"ai:ready", "ai:changes", "ci:failure"})
        self.assertEqual({label for label, tier in tiers.items() if tier == "free"},
                         {"ai:ready-free", "ai:changes-free", "ci:failure-free"})
        self.assertEqual({label for label, tier in tiers.items() if tier == "smart"},
                         ROUTE_KEYS - {"ai:ready", "ai:changes", "ci:failure",
                                       "ai:ready-free", "ai:changes-free", "ci:failure-free"})
        self.assertEqual(set(tiers.values()), {"cheap", "smart", "free"})


class RouteValidationTests(unittest.TestCase):
    """The rules ForgeHands enforces when it loads LABEL_ROUTES."""

    def test_a_fallback_to_an_unknown_route_is_an_error(self):
        with self.assertRaisesRegex(ValueError, "fallback_to must name another route"):
            _validate({"ai:ready": {"profile": "id", "tier": "cheap", "fallback_to": "ai:ready-free"}})

    def test_a_fallback_route_must_not_have_a_fallback_of_its_own(self):
        with self.assertRaisesRegex(ValueError, "without a fallback_to of its own"):
            _validate({"ai:ready": {"profile": "id", "tier": "cheap", "fallback_to": "ai:ready-free"},
                       "ai:ready-free": {"profile": "id", "tier": "free", "fallback_to": "ai:ready"}})

    def test_escalation_to_an_unknown_route_is_an_error(self):
        with self.assertRaisesRegex(ValueError, "escalate_to must name another route"):
            _validate({"ai:changes": {"profile": "id", "tier": "cheap", "on": "pull_request",
                                      "escalate_to": "ai:escalate"}})

    def test_escalation_from_a_route_that_is_not_a_round_is_an_error(self):
        with self.assertRaisesRegex(ValueError, "only applies to a round route"):
            _validate({"ai:ready": {"profile": "id", "tier": "cheap", "escalate_to": "ai:escalate"},
                       "ai:escalate": {"profile": "id", "tier": "smart"}})


class GeneratedDefaultsTests(unittest.TestCase):
    def test_setup_opts_in_but_does_not_enable_direct_mode_with_profile_names(self):
        """The factory is opted in, but a fresh stack has no profile ids, so direct mode stays off (#25 provides them)."""
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copy(ROOT / "setup.sh", tmp)
            env = {**os.environ, "HOST_IP": "10.0.0.10", "DNS_MODE": "external",
                   "ADMIN_EMAIL": "owner@lab.example", "ADMIN_PASSWORD": "correct-horse-1",
                   "OPTIONAL_SERVICES": "agents",
                   "FORGEHANDS_STRONG_MODEL": "vendor/strong-model",
                   "FORGEHANDS_CHEAP_MODEL": "vendor/cheap-model",
                   "FORGEHANDS_GATEWAY_URL": "https://llm.example/v1",
                   "FORGEHANDS_GATEWAY_API_KEY": "gateway-key"}
            subprocess.run(["bash", "setup.sh", "--yes"], cwd=tmp, env=env, check=True, capture_output=True)
            generated = dict(line.split("=", 1) for line in (pathlib.Path(tmp) / ".env").read_text().splitlines()
                             if "=" in line and not line.startswith("#"))
        self.assertEqual(generated.get("FORGEHANDS_BOOTSTRAP"), "1")
        self.assertEqual(generated.get("FORGEHANDS_OPENHANDS_API_URL", ""), "")
        self.assertEqual(generated.get("FORGEHANDS_LABEL_ROUTES", ""), "")
