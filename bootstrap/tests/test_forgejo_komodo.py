import base64
import unittest

from hgboot.forgejo import CHUNK, Forgejo
from hgboot.komodo import Komodo, update_all_stages
from hgboot.state import State
from hgboot.steps import StepError

from .fakes import FakeForgejo, FakeKomodo, Server
from .helpers import PASSWORD, config, no_wait

FILES = [("README.md", b"# hi\n"), ("inventory/stacks.yaml", b"stacks: []\n")]


class ForgejoTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeForgejo(admin="owner", password=PASSWORD)
        self.srv = Server(self.fake)
        self.addCleanup(self.srv.close)
        self.cfg = config(forgejo_url=self.srv.url)
        self.state = State(f"{self.cfg.state_dir}/state.json")

    def forgejo(self):
        return Forgejo(self.cfg, self.state, wait=no_wait)

    def test_creates_the_token_the_repository_its_content_and_the_webhook(self):
        self.forgejo().run(FILES)
        self.assertEqual(self.state.get("forgejo", "token"), "tok-1")
        self.assertEqual(self.fake.repos["owner/infrastructure"]["private"], True)
        self.assertEqual(self.fake.commits, [["README.md", "inventory/stacks.yaml"]])
        self.assertEqual(base64.b64decode(self.fake.repos["owner/infrastructure"]["files"]["README.md"]), b"# hi\n")
        self.assertEqual(len(self.fake.hooks), 1)
        hook = self.fake.hooks[0]
        self.assertEqual((hook["type"], hook["events"], hook["config"]["url"], hook["config"]["secret"]),
                         ("forgejo", ["push"], "http://komodo-core:9120/listener/github/procedure/update-all/main", "hook-secret"))
        tok = next(r for r in self.srv.calls("POST", "/api/v1/users/owner/tokens"))
        self.assertEqual(sorted(tok.json["scopes"]), ["read:user", "write:issue", "write:repository"])

    def test_a_second_run_changes_nothing(self):
        self.forgejo().run(FILES)
        self.srv.requests.clear()
        self.forgejo().run(FILES)
        self.assertEqual([(r.method, r.path) for r in self.srv.requests if r.method in ("POST", "DELETE", "PUT")], [])

    def test_creates_the_single_sign_on_application_and_keeps_its_secret(self):
        self.forgejo().run(FILES)
        (app,) = self.fake.oauth_apps
        self.assertEqual((app["name"], app["redirect_uris"], app["confidential_client"]),
                         ("homegraf", ["http://homegraf.home.lab/api/auth/callback"], True))
        self.assertEqual((self.state.get("forgejo", "oauth_client_id"), self.state.get("forgejo", "oauth_client_secret")), ("cid-1", "csec-1"))

    def test_an_application_whose_secret_is_lost_or_whose_callback_changed_is_replaced(self):
        self.forgejo().run(FILES)
        self.state.set("forgejo", "oauth_client_secret", "")
        self.forgejo().run(FILES)
        self.assertEqual([a["client_id"] for a in self.fake.oauth_apps], ["cid-2"])
        self.cfg.domain = "other.lab"
        self.forgejo().run(FILES)
        self.assertEqual([(a["client_id"], a["redirect_uris"]) for a in self.fake.oauth_apps], [("cid-3", ["http://homegraf.other.lab/api/auth/callback"])])

    def test_a_token_whose_value_is_lost_is_replaced(self):
        self.forgejo().run(FILES)
        self.state.set("forgejo", "token", "gone")
        self.forgejo().run(FILES)
        self.assertEqual(self.state.get("forgejo", "token"), "tok-2")
        self.assertEqual(list(self.fake.tokens), ["homegraf"])

    def test_an_existing_repository_with_content_is_not_seeded(self):
        self.fake.repos["owner/infrastructure"] = {"empty": False, "files": {}, "private": True}
        self.forgejo().run(FILES)
        self.assertEqual(self.fake.commits, [])

    def test_a_big_seed_goes_in_several_commits(self):
        files = [(f"f{i}.txt", b"x") for i in range(CHUNK + 5)]
        self.forgejo().run(files)
        self.assertEqual([len(c) for c in self.fake.commits], [CHUNK, 5])

    def test_the_wrong_administrator_password_says_what_to_check(self):
        self.cfg.admin_password = "something-else-entirely"
        with self.assertRaisesRegex(StepError, "forgejo-admin"):
            self.forgejo().run(FILES)

    def test_a_reserved_name_is_mapped(self):
        self.assertEqual(self.cfg.forgejo_user, "owner")


class KomodoTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeKomodo(password=PASSWORD)
        self.srv = Server(self.fake)
        self.addCleanup(self.srv.close)
        self.cfg = config(komodo_url=self.srv.url, forgejo_url="http://forgejo:3000")
        self.state = State(f"{self.cfg.state_dir}/state.json")

    def komodo(self):
        return Komodo(self.cfg, self.state, wait=no_wait, sleep=lambda s: None)

    def test_key_git_account_repository_clone_and_procedure(self):
        self.assertEqual(self.komodo().run("forgejo-token"), "Local")
        self.assertTrue(self.state.get("komodo", "api_key").startswith("K_"))
        self.assertEqual([(a["domain"], a["username"], a["https"], a["token"]) for a in self.fake.accounts], [("forgejo:3000", "owner", False, "forgejo-token")])
        repo = self.fake.repos["infrastructure"]
        self.assertEqual(repo["config"], {"server_id": "s1", "git_provider": "forgejo:3000", "git_https": False, "git_account": "owner",
                                          "repo": "owner/infrastructure", "branch": "main"})
        self.assertEqual(repo["info"]["latest_hash"], "25de55c")
        self.assertEqual(self.fake.procedures["update-all"]["stages"], update_all_stages())
        self.assertTrue(self.fake.procedures["update-all"]["webhook_enabled"])

    def test_the_procedure_has_the_shape_the_tools_look_for(self):
        stages = update_all_stages()
        self.assertEqual([s["name"] for s in stages], ["Pull", "Deploy"])
        self.assertEqual(stages[1]["executions"][0]["execution"]["type"], "BatchDeployStackIfChanged")

    def test_a_second_run_creates_and_changes_nothing(self):
        self.komodo().run("forgejo-token")
        self.srv.requests.clear()
        self.komodo().run("forgejo-token")
        writes = [r.path for r in self.srv.requests if r.path.startswith(("/write", "/execute", "/auth/manage"))]
        self.assertEqual(writes, [])

    def test_a_lost_api_key_secret_is_replaced_not_duplicated(self):
        self.komodo().run("forgejo-token")
        self.state.set("komodo", "api_secret", "lost")
        self.komodo().run("forgejo-token")
        self.assertEqual([k["name"] for k in self.fake.keys], ["homegraf"])
        self.assertEqual(self.state.get("komodo", "api_secret"), self.fake.keys[0]["secret"])

    def test_a_new_forgejo_token_is_passed_on_and_a_wrong_repository_resource_is_corrected(self):
        self.komodo().run("token-1")
        self.fake.repos["infrastructure"]["config"]["branch"] = "other"
        self.komodo().run("token-2")
        self.assertEqual(self.fake.accounts[0]["token"], "token-2")
        self.assertEqual(self.fake.repos["infrastructure"]["config"]["branch"], "main")

    def test_an_existing_update_all_is_left_alone(self):
        self.fake.procedures["update-all"] = {"stages": ["mine"]}
        self.komodo().run("t")
        self.assertEqual(self.fake.procedures["update-all"], {"stages": ["mine"]})

    def test_a_failing_clone_shows_why(self):
        self.fake.clone_ok = False
        with self.assertRaisesRegex(StepError, "cloning the repository failed at Clone Repo: fatal: could not read Username"):
            self.komodo().run("t")

    def test_the_wrong_password_and_an_agent_that_is_not_ok_are_explained(self):
        self.cfg.admin_password = "not-the-one-at-all"
        with self.assertRaisesRegex(StepError, "very first start only"):
            self.komodo().run("t")
        self.cfg.admin_password = PASSWORD
        self.fake.server_state = "NotOk"
        with self.assertRaisesRegex(StepError, "komodo-periphery"):
            self.komodo().run("t")


if __name__ == "__main__":
    unittest.main()
