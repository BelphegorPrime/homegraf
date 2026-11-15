"""The Forgejo user of the agents: creation, token, collaborations, rotation, its mail notifications, and what may never be logged."""
import io
import json
import threading
import unittest
import urllib.parse
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from hgboot.agent_user import LOGIN, TOKEN, TOKEN_PREFIX, TOKEN_SCOPES, ensure_agent_user
from hgboot.steps import StepError

from .fakes import FakeForgejo, Req
from .helpers import config

REPO = "owner/infrastructure"
OTHER = "owner/other"
ACCOUNT = "/user/settings/account"
CSRF = "csrf-token-of-the-form"
COOKIE = "i_like_gitea"


class Server:
    """A fake HTTP server whose answer may carry headers: handler(req) -> (status, payload[, headers]). Account and sign-in
    pages need `Set-Cookie` and `Location`, which the shared fake in fakes.py does not send. Every request is kept in .requests."""
    def __init__(self, handler):
        self.requests: list[Req] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def _handle(self):
                length = int(self.headers.get("content-length") or 0)
                req = Req(self.command, self.path, {k.lower(): v for k, v in self.headers.items()}, self.rfile.read(length))
                outer.requests.append(req)
                answer = handler(req)
                status, payload = answer[0], answer[1]
                extra = answer[2] if len(answer) > 2 else {}
                body = b"" if payload is None else (payload if isinstance(payload, bytes) else json.dumps(payload).encode())
                self.send_response(status)
                if not any(k.lower() == "content-type" for k in extra):
                    self.send_header("content-type", "application/json")
                for name, value in extra.items():
                    self.send_header(name, value)
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _handle

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def calls(self, method=None, prefix=""):
        return [r for r in self.requests if (method is None or r.method == method) and r.path.startswith(prefix)]


class FakeAgentForgejo(FakeForgejo):
    """The fake of the neighbouring tests, plus what only this step uses: who a token belongs to, is_admin, the refusal of the admin
    endpoints, the reset of a password through the admin API, and the web pages that turn the mail notifications off."""

    def __init__(self):
        super().__init__()
        self.admins: set[str] = set()
        self.notifications: dict[str, str] = {}       # login -> enabled, onmention or disabled
        self.sessions: dict[str, str] = {}            # cookie value -> login
        self.session_n = 0
        self.fail_form = False                        # the account form answers with a validation error, the preference stays
        self.csrf = ""                                # when set, the pages carry a csrf token, as Forgejo before 17 did

    def owner(self, req) -> str | None:
        auth = req.headers.get("authorization", "")
        if not auth.startswith("token "):
            return None
        value = auth[6:]
        for user, tokens in self.user_tokens.items():
            if value in tokens.values():
                return user
        return self.admin if value in self.tokens.values() else None

    def _form(self, req) -> dict:
        return {k: v[0] for k, v in urllib.parse.parse_qs(req.body.decode()).items()}

    def _session(self, req) -> str | None:
        for part in req.headers.get("cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE:
                return self.sessions.get(value)
        return None

    def _cross_origin(self, req) -> bool:
        """What Forgejo 17 refuses before a form is read: a POST whose Origin or Referer names another host."""
        origin = req.headers.get("origin") or req.headers.get("referer")
        return bool(origin) and urllib.parse.urlsplit(origin).netloc != req.headers.get("host", "")

    def _account_page(self, login: str) -> bytes:
        selected = self.notifications.get(login, "enabled")
        items = "".join(f'<div data-value="{value}" class="{"active selected " if value == selected else ""}item">{value}</div>'
                        for value in ("enabled", "andyourown", "onmention", "disabled"))
        # Forgejo 17 renders no csrf token: a hidden input holds the preference and the selected state is on a div.
        csrf = f'<input name="_csrf" type="hidden" value="{self.csrf}">' if self.csrf else ""
        return (f'<html><body><form action="{ACCOUNT}/email" class="ui form" method="post">'
                f'{csrf}<input name="_method" type="hidden" value="NOTIFICATION">'
                f'<div class="ui selection dropdown"><input name="preference" type="hidden" value="{selected}">'
                f'<div class="menu">{items}</div></div></form></body></html>').encode()

    def _login_page(self) -> bytes:
        csrf = f'<input name="_csrf" type="hidden" value="{self.csrf}">' if self.csrf else ""
        return ('<html><body><form class="ui form" action="/user/login" method="post">'
                f'{csrf}<input name="user_name"><input name="password" type="password"></form></body></html>').encode()

    def __call__(self, req):
        p = req.path.split("?")[0]
        if p == "/user/login" and req.method == "GET":
            return 200, self._login_page(), {"content-type": "text/html", "Set-Cookie": f"{COOKIE}=anonymous; Path=/; HttpOnly"}
        if p == "/user/login" and req.method == "POST":
            form = self._form(req)
            if (self.csrf and form.get("_csrf") != self.csrf) or self.users.get(form.get("user_name")) != form.get("password"):
                return 200, self._login_page(), {"content-type": "text/html"}
            self.session_n += 1
            sid = f"web-session-{self.session_n}"
            self.sessions[sid] = form["user_name"]
            return 303, b"", {"Location": "/", "Set-Cookie": f"{COOKIE}={sid}; Path=/; HttpOnly", "content-type": "text/html"}
        if p == "/" and req.method == "GET":
            return 200, b"<html><body>dashboard</body></html>", {"content-type": "text/html"}
        if p == ACCOUNT and req.method == "GET":
            login = self._session(req)
            if not login:
                return 303, b"", {"Location": "/user/login", "content-type": "text/html"}
            return 200, self._account_page(login), {"content-type": "text/html"}
        if p == ACCOUNT + "/email" and req.method == "POST":
            if self._cross_origin(req):
                return 403, b"cross-origin request", {"content-type": "text/plain"}
            login = self._session(req)
            if not login:
                return 303, b"", {"Location": "/user/login", "content-type": "text/html"}
            form = self._form(req)
            if self.fail_form or (self.csrf and form.get("_csrf") != self.csrf) or form.get("_method") != "NOTIFICATION":
                return 422, {"message": "invalid form"}, {"content-type": "text/html"}
            self.notifications[login] = form.get("preference", "")
            return 303, b"", {"Location": ACCOUNT, "content-type": "text/html"}
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
        self.assertEqual(self.forgejo.notifications[LOGIN], "disabled")

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
        self.assertEqual(body["visibility"], "public")          # as on the reference installation: the human can mention it and see it

    def test_the_mail_notifications_are_turned_off_on_creation(self):
        self.run_step()
        self.assertEqual(self.forgejo.notifications[LOGIN], "disabled")
        login = self.calls("POST", "/user/login")[0]
        self.assertNotIn("_csrf", login.body.decode())       # Forgejo 17 has no token in the form
        self.assertEqual(login.headers.get("origin"), self.server.url)
        form = self.calls("POST", f"{ACCOUNT}/email")[0]
        self.assertEqual(form.headers.get("origin"), self.server.url)
        self.assertTrue(form.headers.get("referer", "").startswith(self.server.url + ACCOUNT))
        body = form.body.decode()
        self.assertIn("_method=NOTIFICATION", body)
        self.assertIn("preference=disabled", body)

    def test_a_form_that_carries_a_csrf_token_gets_it_back(self):
        self.forgejo.csrf = CSRF                             # Forgejo before 17; the current one carries no token
        self.run_step()
        for path in ("/user/login", f"{ACCOUNT}/email"):
            self.assertIn(f"_csrf={CSRF}", self.calls("POST", path)[0].body.decode())

    def test_an_existing_user_with_mail_on_gets_it_turned_off(self):
        self.forgejo.users[LOGIN] = "an-old-password"
        self.forgejo.notifications[LOGIN] = "enabled"
        self.run_step()
        self.assertEqual(self.forgejo.notifications[LOGIN], "disabled")

    def test_a_second_run_changes_nothing(self):
        self.run_step()
        tokens = dict(self.forgejo.user_tokens[LOGIN])
        self.run_step()
        self.assertEqual(self.forgejo.user_tokens[LOGIN], tokens)
        self.assertEqual(self.forgejo.repos[REPO]["collaborators"], {LOGIN: "write"})
        self.assertEqual(self.calls("PATCH"), [])
        self.assertEqual(len(self.calls("POST", f"{ACCOUNT}/email")), 1)   # the account form is not posted again

    def test_a_failing_notification_form_is_a_step_error_without_a_secret(self):
        self.forgejo.fail_form = True
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            with self.assertRaises(StepError) as caught:
                self.run_step()
        password = self.calls("POST", "/api/v1/admin/users")[0].json["password"]
        cookie = next(iter(self.forgejo.sessions))
        message = str(caught.exception)
        self.assertIn("mail notifications", message)
        for text in (message, out.getvalue(), err.getvalue()):
            self.assertNotIn(password, text)
            self.assertNotIn(cookie, text)
            self.assertNotIn(CSRF, text)

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
        cookie = next(iter(self.forgejo.sessions))
        texts = [out.getvalue(), err.getvalue(), repr(settings), str(settings)]
        for text in texts:
            self.assertNotIn(token, text)
            self.assertNotIn(password, text)
            self.assertNotIn(cookie, text)
            self.assertNotIn(CSRF, text)


if __name__ == "__main__":
    unittest.main()
