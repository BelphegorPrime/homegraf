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


class LabelRoutesTests(unittest.TestCase):
    def test_every_trigger_label_routes_to_its_profile_and_tier(self):
        routes = json.loads(label_routes(PROFILE_IDS))
        self.assertEqual(set(routes), {"ai:plan", "ai:ready", "ai:escalate", "ai:review"})
        self.assertEqual(routes["ai:plan"], {"profile": PROFILE_IDS["planner"], "tier": "planner", "max_iterations": 80})
        self.assertEqual(routes["ai:ready"], {"profile": PROFILE_IDS["worker"], "tier": "worker", "max_iterations": 120})
        self.assertEqual(routes["ai:escalate"], {"profile": PROFILE_IDS["worker-strong"], "tier": "worker-strong",
                                                 "max_iterations": 120})
        self.assertEqual(routes["ai:review"], {"profile": PROFILE_IDS["reviewer"], "tier": "reviewer", "max_iterations": 60})

    def test_a_profile_without_an_id_is_an_error_not_a_name(self):
        ids = dict(PROFILE_IDS)
        del ids["reviewer"]
        with self.assertRaisesRegex(ValueError, "reviewer"):
            label_routes(ids)

    def test_one_tier_per_label_and_the_strong_tier_is_the_escalation(self):
        tiers = {label: tier for label, _, tier, _ in LABELS}
        self.assertEqual(tiers["ai:escalate"], "worker-strong")
        self.assertEqual(len(set(tiers.values())), 4)


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
