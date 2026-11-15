"""The Forgejo user of the agents: creation, token, collaborations, rotation, and what may never be logged."""
import io
import unittest
from contextlib import redirect_stderr, redirect_stdout

from hgboot.agent_user import LOGIN, TOKEN, TOKEN_PREFIX, TOKEN_SCOPES, ensure_agent_user
from hgboot.steps import StepError

from .fakes import FakeForgejo, Server
from .helpers import config

REPO = "owner/infrastructure"
OTHER = "owner/other"


class FakeAgentForgejo(FakeForgejo):
    """The fake of the neighbouring tests, plus what only this step uses: who a token belongs to, is_admin, the refusal of the admin
    endpoints, and the reset of a password through the admin API."""

    def __init__(self):
        super().__init__()
        self.admins: set[str] = set()

    def owner(self, req) -> str | None:
        auth = req.headers.get("authorization", "")
        if not auth.startswith("token "):
            return None
        value = auth[6:]
        for user, tokens in self.user_tokens.items():
            if value in tokens.values():
                return user
        return self.admin if value in self.tokens.values() else None

    def __call__(self, req):
        p = req.path.split("?")[0]
        owner = self.owner(req)
        if p == "/api/v1/user" and req.method == "GET" and owner:
            return 200, {"login": owner}
        if p == "/api/v1/admin/users" and req.method == "GET":
            if owner:
                return (200, [{"login": u} for u in self.users]) if owner in self.admins else (403, {"message": "forbidden"})
            return 200, [{"login": u} for u in self.users]
        if p.startswith("/api/v1/admin/users/") and req.method == "PATCH":
            self.users[p.rsplit("/", 1)[1]] = req.json["password"]
            return 200, {"login": p.rsplit("/", 1)[1]}
        if p.startswith("/api/v1/users/") and len(p.split("/")) == 5 and req.method == "GET":
            user = p.split("/")[4]
            if user in self.users or user == self.admin:
                return 200, {"login": user, "is_admin": user in self.admins}
            return 404, {"message": "not found"}
        return super().__call__(req)


class Store:
    """The secret store as the step sees it: get(name) and set(name, value)."""

    def __init__(self, data=None):
        self.data = dict(data or {})

    def get(self, name):
        return self.data.get(name)

    def set(self, name, value):
        self.data[name] = value


class AgentUserTest(unittest.TestCase):
    def setUp(self):
        self.forgejo = FakeAgentForgejo()
        self.server = Server(self.forgejo)
        self.addCleanup(self.server.close)
        self.cfg = config(forgejo_url=self.server.url)
        self.store = Store()
        for repo in (REPO, OTHER):
            self.forgejo.repos[repo] = {"empty": True, "files": {}, "private": True, "collaborators": {}}

    def run_step(self, **kw):
        kw.setdefault("repositories", (REPO, OTHER))
        return ensure_agent_user(self.cfg, self.store, **kw)

    def calls(self, method=None, prefix=""):
        return self.server.calls(method, prefix)

    def test_creates_the_user_its_token_the_collaborations_and_the_senders(self):
        settings = self.run_step()
        self.assertIn(LOGIN, self.forgejo.users)
        self.assertEqual(list(self.forgejo.user_tokens[LOGIN].values()), [self.store.get(TOKEN)])
        self.assertEqual(self.forgejo.repos[REPO]["collaborators"], {LOGIN: "write"})
        self.assertEqual(self.forgejo.repos[OTHER]["collaborators"], {LOGIN: "write"})
        self.assertEqual(settings.login, LOGIN)
        self.assertEqual(settings.allowed_senders, f"owner, {LOGIN}")
        self.assertEqual(self.store.get(TOKEN), next(iter(self.forgejo.user_tokens[LOGIN].values())))

    def test_the_token_carries_only_the_three_scopes_and_the_admin_endpoints_refuse_it(self):
        self.run_step()
        created = self.calls("POST", f"/api/v1/users/{LOGIN}/tokens")
        self.assertEqual(created[0].json["scopes"], list(TOKEN_SCOPES))
        check = [r for r in self.calls("GET", "/api/v1/admin/users") if r.headers.get("authorization", "").startswith("token ")]
        self.assertEqual(len(check), 1)                        # the step verified it, it did not trust the scopes it asked for
        self.assertEqual(self.forgejo.owner(check[0]), LOGIN)

    def test_the_user_is_created_without_a_mail_a_password_change_or_admin_rights(self):
        self.run_step()
        body = self.calls("POST", "/api/v1/admin/users")[0].json
        self.assertEqual((body["must_change_password"], body["send_notify"], body["admin"], body["restricted"]), (False, False, False, False))
        self.assertEqual(body["email"], f"{LOGIN}@noreply.home.lab")

    def test_a_second_run_changes_nothing(self):
        self.run_step()
        tokens = dict(self.forgejo.user_tokens[LOGIN])
        self.run_step()
        self.assertEqual(self.forgejo.user_tokens[LOGIN], tokens)
        self.assertEqual(self.forgejo.repos[REPO]["collaborators"], {LOGIN: "write"})
        self.assertEqual(self.calls("PATCH"), [])

    def test_a_lost_secret_gets_a_new_token_and_the_old_one_is_deleted(self):
        self.run_step()
        old = self.store.get(TOKEN)
        old_name = next(n for n, v in self.forgejo.user_tokens[LOGIN].items() if v == old)
        self.store.data.clear()
        self.run_step()
        self.assertNotEqual(self.store.get(TOKEN), old)
        self.assertEqual(list(self.forgejo.user_tokens[LOGIN].values()), [self.store.get(TOKEN)])
        self.assertIn(old_name, [r.path.rsplit("/", 1)[1] for r in self.calls("DELETE", f"/api/v1/users/{LOGIN}/tokens/")])

    def test_an_existing_user_is_reused(self):
        self.forgejo.users[LOGIN] = "an-old-password"
        self.run_step()
        self.assertEqual(self.calls("POST", "/api/v1/admin/users"), [])
        self.assertTrue(self.store.get(TOKEN))

    def test_a_missing_repository_is_an_error_that_names_it(self):
        with self.assertRaises(StepError) as caught:
            self.run_step(repositories=(REPO, "owner/absent"))
        self.assertIn("owner/absent", str(caught.exception))

    def test_an_admin_user_is_refused(self):
        self.forgejo.users[LOGIN] = "an-old-password"
        self.forgejo.admins.add(LOGIN)
        with self.assertRaises(StepError) as caught:
            self.run_step()
        self.assertIn("administrator", str(caught.exception))
        self.assertEqual((self.store.data, self.forgejo.user_tokens.get(LOGIN)), ({}, None))

    def test_rotation_makes_a_new_token_and_deletes_the_earlier_one(self):
        self.run_step()
        old = self.store.get(TOKEN)
        self.run_step(rotate=True)
        self.assertNotEqual(self.store.get(TOKEN), old)
        self.assertEqual(list(self.forgejo.user_tokens[LOGIN].values()), [self.store.get(TOKEN)])
        self.assertTrue(all(name.startswith(TOKEN_PREFIX) for name in self.forgejo.user_tokens[LOGIN]))

    def test_a_token_that_belongs_to_someone_else_is_replaced(self):
        self.forgejo.users[LOGIN] = "an-old-password"
        self.store.set(TOKEN, "not-a-token")
        self.run_step()
        self.assertEqual(self.store.get(TOKEN), next(iter(self.forgejo.user_tokens[LOGIN].values())))

    def test_the_token_and_the_password_stay_out_of_the_logs_the_errors_and_the_result(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            settings = self.run_step()
        password = self.calls("POST", "/api/v1/admin/users")[0].json["password"]
        token = self.store.get(TOKEN)
        texts = [out.getvalue(), err.getvalue(), repr(settings), str(settings)]
        for text in texts:
            self.assertNotIn(token, text)
            self.assertNotIn(password, text)


if __name__ == "__main__":
    unittest.main()
