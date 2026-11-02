import unittest

from hgboot.adguard import AdGuard
from hgboot.infisical import Infisical, generate_password, meets_policy
from hgboot.npm import Npm
from hgboot.state import State
from hgboot.steps import StepError

from .fakes import FakeAdGuard, FakeInfisical, FakeNpm, Server
from .helpers import PASSWORD, config, no_wait


class NpmTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeNpm()
        self.api, self.proxy = Server(self.fake), Server(self.fake.proxy)
        self.addCleanup(self.api.close)
        self.addCleanup(self.proxy.close)
        self.cfg = config(npm_url=self.api.url, npm_proxy_url=self.proxy.url)
        self.state = State(f"{self.cfg.state_dir}/state.json")

    def npm(self):
        return Npm(self.cfg, self.state, wait=no_wait, sleep=lambda s: None)

    def test_creates_the_administrator_through_setup_and_a_host_per_service(self):
        self.npm().run()
        self.assertEqual(self.fake.users, {"admin@example.org": PASSWORD})
        self.assertEqual([(h["domain_names"][0], h["forward_host"], h["forward_port"]) for h in self.fake.hosts], [
            ("git.home.lab", "forgejo", 3000), ("komodo.home.lab", "komodo-core", 9120), ("secrets.home.lab", "infisical", 8080),
            ("proxy.home.lab", "proxy", 81), ("homegraf.home.lab", "homegraf", 8080)])
        self.assertTrue(all(h["allow_websocket_upgrade"] and h["forward_scheme"] == "http" and h["certificate_id"] == 0 for h in self.fake.hosts))

    def test_a_second_run_changes_nothing_and_an_existing_account_is_used(self):
        self.npm().run()
        self.api.requests.clear()
        self.npm().run()
        self.assertEqual([r.method for r in self.api.requests if r.method in ("POST", "PUT") and r.path != "/api/tokens"], [])

    def test_a_host_pointing_elsewhere_is_corrected(self):
        self.npm().run()
        self.fake.hosts[0]["forward_port"] = 1234
        self.npm().run()
        self.assertEqual(self.fake.hosts[0]["forward_port"], 3000)

    def test_an_npm_set_up_before_with_other_credentials_is_explained(self):
        self.fake.users = {"someone@else.org": "x"}
        with self.assertRaisesRegex(StepError, "set up before with other credentials"):
            self.npm().run()

    def test_a_host_the_proxy_does_not_know_yet_only_warns(self):
        self.fake.unknown_hosts_for = 999
        self.npm().run()                              # no exception: a warning on stderr


class InfisicalTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeInfisical()
        self.srv = Server(self.fake)
        self.addCleanup(self.srv.close)
        self.cfg = config(infisical_url=self.srv.url)
        self.state = State(f"{self.cfg.state_dir}/state.json")

    def infisical(self):
        return Infisical(self.cfg, self.state, wait=no_wait, password_factory=lambda: "Generated-Passw0rd-xyz")

    def test_bootstraps_the_instance_and_creates_the_identity_for_homegraf(self):
        self.cfg.admin_password = "a-long-enough-pass-1"
        self.infisical().run()
        self.assertEqual(self.fake.admin_password, "a-long-enough-pass-1")
        self.assertEqual(self.fake.identities["id1"], {"name": "homegraf", "organizationId": "org1", "role": "admin"})
        self.assertEqual((self.state.get("infisical", "client_id"), self.state.get("infisical", "client_secret")), ("client-1", "secret-1"))
        self.assertIsNone(self.state.get("infisical", "admin_password"))

    def test_a_password_that_does_not_meet_the_policy_is_replaced_by_a_generated_one_that_is_kept(self):
        self.cfg.admin_password = "short-pass-12"                     # 13 characters
        self.infisical().run()
        self.assertEqual(self.fake.admin_password, "Generated-Passw0rd-xyz")
        self.assertEqual(self.state.get("infisical", "admin_password"), "Generated-Passw0rd-xyz")

    def test_a_second_run_creates_nothing(self):
        self.infisical().run()
        self.srv.requests.clear()
        self.infisical().run()
        self.assertEqual([r.path for r in self.srv.requests if r.method == "POST" and "login" not in r.path], [])

    def test_an_instance_set_up_by_hand_is_explained(self):
        self.fake.initialized = True
        with self.assertRaisesRegex(StepError, "INFISICAL_CLIENT_ID"):
            self.infisical().run()

    def test_the_policy(self):
        self.assertTrue(meets_policy("abcdefghijklm1"))
        for bad in ("abcdefghijkl1", "abcdefghijklmn", "12345678901234", "aaaaabcdefghij1", "abcdefghijk\\lm1"):
            self.assertFalse(meets_policy(bad), bad)
        self.assertTrue(all(meets_policy(generate_password()) for _ in range(50)))


class AdGuardTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeAdGuard(password=PASSWORD)
        self.srv = Server(self.fake)
        self.addCleanup(self.srv.close)
        self.cfg = config(adguard_url=self.srv.url, adguard_setup_url=self.srv.url, dns_mode="adguard")
        self.state = State(f"{self.cfg.state_dir}/state.json")

    def test_first_run_setup_and_the_wildcard_rewrite(self):
        AdGuard(self.cfg, self.state, wait=no_wait).run()
        self.assertTrue(self.fake.configured)
        self.assertEqual(self.fake.rewrites, [{"domain": "*.home.lab", "answer": "192.168.1.10"}])
        configure = self.srv.calls("POST", "/control/install/configure")[0].json
        self.assertEqual((configure["username"], configure["web"]["port"], configure["dns"]["port"]), ("admin", 80, 53))

    def test_a_second_run_changes_nothing_and_a_wrong_address_is_replaced(self):
        AdGuard(self.cfg, self.state, wait=no_wait).run()
        self.srv.requests.clear()
        AdGuard(self.cfg, self.state, wait=no_wait).run()
        self.assertEqual([r.path for r in self.srv.requests if r.method == "POST"], [])
        self.fake.rewrites = [{"domain": "*.home.lab", "answer": "10.9.9.9"}]
        AdGuard(self.cfg, self.state, wait=no_wait).run()
        self.assertEqual(self.fake.rewrites, [{"domain": "*.home.lab", "answer": "192.168.1.10"}])


if __name__ == "__main__":
    unittest.main()
