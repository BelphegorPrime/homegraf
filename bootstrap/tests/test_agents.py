"""The label routes of ForgeHands direct mode."""
import json
import unittest

from hgboot.agents import LABELS, label_routes


class LabelRoutesTests(unittest.TestCase):
    def test_every_trigger_label_routes_to_its_profile_and_tier(self):
        routes = json.loads(label_routes())
        self.assertEqual(set(routes), {"ai:plan", "ai:ready", "ai:escalate", "ai:review"})
        self.assertEqual(routes["ai:plan"], {"profile": "planner", "tier": "planner", "max_iterations": 40})
        self.assertEqual(routes["ai:ready"], {"profile": "worker", "tier": "worker", "max_iterations": 120})
        self.assertEqual(routes["ai:escalate"], {"profile": "worker-strong", "tier": "worker-strong", "max_iterations": 120})
        self.assertEqual(routes["ai:review"], {"profile": "reviewer", "tier": "reviewer", "max_iterations": 60})

    def test_profile_ids_replace_the_names_when_the_agents_step_has_them(self):
        routes = json.loads(label_routes({"worker": "a-2", "reviewer": "a-4"}))
        self.assertEqual(routes["ai:ready"]["profile"], "a-2")
        self.assertEqual(routes["ai:review"]["profile"], "a-4")
        self.assertEqual(routes["ai:plan"]["profile"], "planner")          # no id yet: the name is the fallback

    def test_one_tier_per_label_and_the_strong_tier_is_the_escalation(self):
        tiers = {label: tier for label, _, tier, _ in LABELS}
        self.assertEqual(tiers["ai:escalate"], "worker-strong")
        self.assertEqual(len(set(tiers.values())), 4)
