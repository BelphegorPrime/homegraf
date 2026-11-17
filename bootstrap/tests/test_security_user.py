"""The security reviewer's user: its creation, its read access, its idempotence, and the token that is lost or only configured."""
import unittest

from hgboot.factory_config import FactoryConfig
from hgboot.security_user import TOKEN, ensure_security_user
from hgboot.steps import StepError

from .helpers import config
from .test_agent_user import FakeAgentForgejo, Server, Store

REPO = "owner/repository"
OTHER = "owner/other"
REVIEWER = "security-reviewer"
CONFIGURED = "configured-value-the-fake-does-not-know"
ADMIN_USERS = "/api/v1/admin/users"


class SecurityUserTest(unittest.TestCase):
    def setUp(self):
        self.forgejo = FakeAgentForgejo()
        self.server = Server(self.forgejo)
        self.addCleanup(self.server.close)
        self.cfg = config(forgejo_url=self.server.url)
        self.store = Store()
        for repo in (REPO, OTHER):
            self.forgejo.repos[repo] = {"empty": True, "files": {}, "private": True, "collaborators": {}}

    def factory(self, **over):
        values = dict(enabled=True, repository=REPO, agent_login="homegraf-agent", human_login="human",
                      allowed_repos=(REPO, OTHER), security_reviewers=(REVIEWER,),
                      security_token="placeholder-not-a-real-token")
        values.update(over)
        return FactoryConfig(**values)

    def run_step(self, **over):
        return ensure_security_user(self.cfg, self.factory(**over), self.store)

    def calls(self, method=None, prefix=""):
        return self.server.calls(method, prefix)

    def test_creates_the_user_its_token_and_its_read_access(self):
        settings = self.run_step()
        self.assertEqual(settings.login, REVIEWER)
        self.assertIn(REVIEWER, self.forgejo.users)
        tokens = self.forgejo.user_tokens[REVIEWER]
        self.assertEqual(list(tokens.values()), [self.store.get(TOKEN)])
        self.assertTrue(next(iter(tokens)).startswith("security-"))
        body = self.calls("POST", ADMIN_USERS)[0].json
        self.assertEqual((body["visibility"], body["admin"], body["restricted"], body["must_change_password"],
                          body["send_notify"]), ("limited", False, False, False, False))
        created = self.calls("POST", f"/api/v1/users/{REVIEWER}/tokens")
        self.assertEqual(created[0].json["scopes"], ["read:repository", "write:issue"])

    def test_the_reviewer_collaborates_with_read_on_every_repository(self):
        self.run_step()
        for repo in (REPO, OTHER):
            self.assertEqual(self.forgejo.repos[repo]["collaborators"][REVIEWER], "read")
        self.assertEqual(len(self.calls("PUT", "/api/v1/repos/")), 2)
        permissions = {r.json["permission"] for r in self.calls("PUT", "/api/v1/repos/") if "/collaborators/" in r.path}
        self.assertEqual(permissions, {"read"})

    def test_a_second_run_changes_nothing(self):
        self.run_step()
        token = self.store.get(TOKEN)
        created = len(self.calls("POST", ADMIN_USERS))
        issued = len(self.calls("POST", f"/api/v1/users/{REVIEWER}/tokens"))
        self.run_step()
        self.assertEqual(len(self.calls("POST", ADMIN_USERS)), created)
        self.assertEqual(len(self.calls("POST", f"/api/v1/users/{REVIEWER}/tokens")), issued)
        self.assertEqual(self.calls("PATCH"), [])
        self.assertEqual(self.calls("DELETE"), [])
        self.assertEqual(self.store.get(TOKEN), token)

    def test_a_lost_secret_gets_a_new_token_and_the_old_one_is_deleted(self):
        self.run_step()
        first = self.store.get(TOKEN)
        self.store.data[TOKEN] = "lost-value"
        self.run_step()
        second = self.store.get(TOKEN)
        self.assertNotEqual(second, first)
        self.assertEqual(list(self.forgejo.user_tokens[REVIEWER].values()), [second])
        self.assertTrue(self.calls("PATCH", f"{ADMIN_USERS}/{REVIEWER}"))
        used = [r for r in self.calls("GET", "/api/v1/user") if r.headers.get("authorization") == f"token {second}"]
        self.assertTrue(used)
        self.assertEqual(self.forgejo.owner(used[0]), REVIEWER)

    def test_a_configured_token_the_fake_does_not_know_gets_a_created_one(self):
        self.run_step(security_token=CONFIGURED)
        self.assertNotEqual(self.store.get(TOKEN), CONFIGURED)
        self.assertEqual(self.store.get(TOKEN), next(iter(self.forgejo.user_tokens[REVIEWER].values())))
        self.assertEqual(len(self.calls("POST", f"/api/v1/users/{REVIEWER}/tokens")), 1)


if __name__ == "__main__":
    unittest.main()
