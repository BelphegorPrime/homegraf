"""Technitium DNS Server (DNS_MODE=technitium): the zone, the wildcard to this host, an API token; safe to run again."""
import tempfile
import unittest

from hgboot.state import State
from hgboot.steps import StepError
from hgboot.technitium import Technitium

from .fakes import FakeTechnitium, Server
from .helpers import PASSWORD, config, no_wait


class TechnitiumTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeTechnitium(password=PASSWORD)
        self.srv = Server(self.fake)
        self.addCleanup(self.srv.close)
        self.cfg = config(technitium_url=self.srv.url, dns_mode="technitium")
        self.state = State(f"{tempfile.mkdtemp()}/state.json")

    def run_step(self):
        Technitium(self.cfg, self.state, wait=no_wait).run()

    def test_makes_the_zone_the_wildcard_and_a_token(self):
        self.run_step()
        self.assertEqual(self.fake.zones, {"home.lab"})
        self.assertEqual(self.fake.records, [{"zone": "home.lab", "domain": "*.home.lab", "type": "A", "ipAddress": "192.168.1.10", "ttl": "300"}])
        self.assertIn(self.state.get("technitium", "token"), self.fake.tokens)

    def test_a_second_run_changes_nothing(self):
        self.run_step()
        self.run_step()
        self.assertEqual(self.fake.created_tokens, 1)                      # the token is reused
        self.assertEqual(self.fake.zones, {"home.lab"})
        self.assertEqual(len(self.fake.records), 1)                        # overwritten, not added again

    def test_a_token_that_no_longer_works_is_replaced(self):
        self.run_step()
        self.fake.tokens.clear()
        self.run_step()
        self.assertEqual(self.fake.created_tokens, 2)
        self.assertIn(self.state.get("technitium", "token"), self.fake.tokens)

    def test_a_changed_address_updates_the_wildcard(self):
        self.run_step()
        self.cfg.host_ip = "10.0.0.7"
        self.run_step()
        self.assertEqual([r["ipAddress"] for r in self.fake.records], ["10.0.0.7"])

    def test_a_password_that_was_not_set_from_ADMIN_PASSWORD_says_what_to_do(self):
        self.fake.password = "another-password-1"
        with self.assertRaises(StepError) as e:
            self.run_step()
        self.assertIn("ADMIN_PASSWORD", str(e.exception))
        self.assertIsNone(self.state.get("technitium", "token"))
