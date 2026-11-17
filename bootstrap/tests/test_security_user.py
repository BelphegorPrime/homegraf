"""The security reviewer's user: its creation, its read access, its refusals, the inactive configurations, and what may never
be logged."""
import contextlib
import io
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout

from hgboot.factory_config import FactoryConfig
from hgboot.security_user import STEP, TOKEN, ensure_security_user
from hgboot.steps import StepError

from .helpers import config
from .test_agent_user import FakeAgentForgejo, Server, Store

REPO = "owner/repository"
OTHER = "owner/other"
REVIEWER = "security-reviewer"
CONFIGURED = "configured-value-the-fake-does-not-know"
ADMIN_USERS = "/api/v1/admin/users"


@contextlib.contextmanager
def loud():
    """The log lines of the step are visible: the test package silences them for every other module."""
    previous = os.environ.pop("HGBOOT_QUIET", None)
    try:
        yield
    finally:
        if previous is not None:
            os.environ["HGBOOT_QUIET"] = previous


class WideTokenForgejo(FakeAgentForgejo):
    """A Forgejo whose token answers the administrator list, whatever scopes it was created with."""

    def __call__(self, req):
        if (req.path.split("?")[0] == ADMIN_USERS and req.method == "GET"
                and req.headers.get("authorization", "").startswith("token ")):
            return 200, [{"login": "owner"}]
        return super().__call__(req)


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

    def run_loud(self, **over):
        """The step with its log lines visible: the result, the stdout and the stderr."""
        out, err = io.StringIO(), io.StringIO()
        with loud(), redirect_stdout(out), redirect_stderr(err):
            result = self.run_step(**over)
        return result, out.getvalue(), err.getvalue()

    def refused(self, login):
        """The message of a refused login, and the check that neither an account nor a token was made."""
        with self.assertRaises(StepError) as caught:
            self.run_step(security_reviewers=(login,))
        self.assertEqual(self.calls("POST", ADMIN_USERS), [])
        self.assertEqual(self.calls("POST", "/api/v1/users/"), [])
        return str(caught.exception)

    def inactive(self, **over):
        """A configuration that switches the step off: no result, no request, and one line that says so."""
        result, out, err = self.run_loud(**over)
        self.assertIsNone(result)
        self.assertEqual(self.server.requests, [])
        self.assertEqual(err, "")
        self.assertEqual(len(out.strip().splitlines()), 1)
        self.assertIn(f"[{STEP}]", out)
        self.assertIn("no security user", out)

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

    def test_the_agent_login_is_refused(self):
        self.assertIn("agent user", self.refused("homegraf-agent"))

    def test_the_human_login_is_refused(self):
        self.assertIn("human", self.refused("human"))

    def test_the_admin_login_is_refused(self):
        self.assertIn("human", self.refused(self.cfg.forgejo_user))

    def test_another_case_of_the_human_login_is_refused(self):
        self.assertIn("human", self.refused("Human"))

    def test_an_existing_administrator_is_refused(self):
        self.forgejo.users[REVIEWER] = "an-old-password"
        self.forgejo.admins.add(REVIEWER)
        with self.assertRaises(StepError) as caught:
            self.run_step()
        self.assertIn("administrator", str(caught.exception))
        self.assertEqual(self.calls("POST", ADMIN_USERS), [])
        self.assertEqual(self.calls("POST", f"/api/v1/users/{REVIEWER}/tokens"), [])
        self.assertEqual(self.store.data, {})

    def test_no_reviewer_configured_does_nothing(self):
        self.inactive(security_reviewers=())

    def test_two_reviewers_do_nothing(self):
        self.inactive(security_reviewers=("a-reviewer", "b-reviewer"))

    def test_an_empty_security_token_does_nothing(self):
        self.inactive(security_token="")

    def test_the_token_the_password_and_the_cookie_stay_out_of_the_output(self):
        settings, out, _ = self.run_loud(security_token=CONFIGURED)
        password = self.forgejo.users[REVIEWER]
        token = self.store.get(TOKEN)
        cookie = next(iter(self.forgejo.sessions))
        for text in (out, repr(settings), str(settings)):
            for secret in (token, password, cookie, CONFIGURED, "web-session-"):
                self.assertNotIn(secret, text)

    def test_a_failing_notification_form_is_an_error_without_a_secret(self):
        self.forgejo.fail_form = True
        out, err = io.StringIO(), io.StringIO()
        with loud(), redirect_stdout(out), redirect_stderr(err):
            with self.assertRaises(StepError) as caught:
                self.run_step(security_token=CONFIGURED)
        password = self.forgejo.users[REVIEWER]
        cookie = next(iter(self.forgejo.sessions))
        message = str(caught.exception)
        self.assertIn("mail notifications", message)
        for text in (message, out.getvalue(), err.getvalue()):
            for secret in (password, cookie, CONFIGURED, "web-session-"):
                self.assertNotIn(secret, text)
        self.assertEqual(self.store.data, {})

    def test_a_token_that_reaches_the_administrator_endpoints_is_an_error(self):
        forgejo = WideTokenForgejo()
        server = Server(forgejo)
        self.addCleanup(server.close)
        for repo in (REPO, OTHER):
            forgejo.repos[repo] = {"empty": True, "files": {}, "private": True, "collaborators": {}}
        with self.assertRaises(StepError) as caught:
            ensure_security_user(config(forgejo_url=server.url), self.factory(), self.store)
        self.assertIn("more rights", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
