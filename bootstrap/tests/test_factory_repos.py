import contextlib
import copy
import io
import tempfile
import unittest
import urllib.parse

from hgboot.factory_repos import LABELS, RepositoryReconciler, reconcile
from hgboot.state import State
from hgboot.steps import StepError

from .fakes import Req, Server

TOKEN = "agent-token"
SECRET = "hook-secret-value"
ENDPOINT = "http://forgehands:8080/webhooks/forgejo"
REPO = "owner/repo"
KOMODO_HOOK = "http://komodo-core:9120/listener/github/procedure/update-all/main"
# Forgejo stores `issues` as the whole issue group and returns the events sorted.
EXPANDED_EVENTS = ("action_run_failure", "action_run_success", "issue_assign", "issue_comment", "issue_label",
                   "issue_milestone", "issues", "pull_request_label")


class FakeFactoryForgejo:
    """The Forgejo endpoints the reconciler uses, with pagination and masked secrets like the real API."""

    def __init__(self, login="agent"):
        self.login = login
        self.repos = {REPO: {"mirror": False, "original_url": ""}}
        self.labels = {REPO: [{"id": 1, "name": "deploy", "color": "ededed", "description": "unrelated"}]}
        self.hooks = {REPO: [{"id": 90, "type": "forgejo", "active": True, "events": ["push"], "branch_filter": "main",
                              "config": {"url": KOMODO_HOOK, "content_type": "json", "secret": "komodo-secret"}}]}
        self.next_id = 100
        self.page_cap = 2
        self.failures: dict[tuple[str, str], tuple[int, dict]] = {}
        self.masked = "********"

    def fail_once(self, method, path, status=500, message="boom"):
        self.failures[(method, path)] = (status, {"message": message})

    def _view_hook(self, hook):
        view = copy.deepcopy(hook)
        view["events"] = self.effective_events(view.get("events") or [])
        if "secret" in view.get("config", {}):
            view["config"]["secret"] = self.masked
        return view

    @staticmethod
    def effective_events(events):
        expanded = set()
        for event in events:
            if event == "issues":
                expanded.update(("issue_assign", "issue_comment", "issue_label", "issue_milestone", "issues"))
            else:
                expanded.add(event)
        return sorted(expanded)

    def __call__(self, req):
        p, _, query = req.path.partition("?")
        params = {k: v[0] for k, v in urllib.parse.parse_qs(query).items()}
        failure = self.failures.pop((req.method, p), None)
        if failure:
            return failure
        if not self.authed(req):
            return 401, {"message": "unauthorized"}
        if p == "/api/v1/user":
            return 200, {"login": self.login}
        parts = p.strip("/").split("/")
        if parts[:3] != ["api", "v1", "repos"] or len(parts) < 5:
            return 404, {"message": f"unexpected {req.method} {p}"}
        repo = f"{parts[3]}/{parts[4]}"
        if repo not in self.repos:
            return 404, {"message": "not found"}
        rest = parts[5:]
        if not rest:
            return 200, {"full_name": repo, **self.repos[repo]}
        if rest == ["labels"] or rest == ["hooks"]:
            items = self.labels[repo] if rest == ["labels"] else self.hooks[repo]
            if req.method == "GET":
                return 200, self.page(items, params)
            if req.method == "POST":
                return self.create(rest[0], repo, req)
            return 405, {"message": "method not allowed"}
        if len(rest) == 2 and rest[0] in ("labels", "hooks"):
            collection = self.labels[repo] if rest[0] == "labels" else self.hooks[repo]
            item = next((x for x in collection if str(x["id"]) == rest[1]), None)
            if item is None:
                return 404, {"message": "not found"}
            if req.method == "GET":
                return 200, self._view_hook(item) if rest[0] == "hooks" else copy.deepcopy(item)
            if req.method == "DELETE":
                collection.remove(item)
                return 204, None
            if req.method == "PATCH":
                if rest[0] == "hooks":
                    return 405, {"message": "hooks do not accept PATCH in this test"}
                item.update({k: req.json[k] for k in ("color", "description") if k in req.json})
                return 200, copy.deepcopy(item)
        return 404, {"message": f"unexpected {req.method} {p}"}

    def page(self, items, params):
        page = int(params.get("page", "1"))
        limit = min(int(params.get("limit", "50")), self.page_cap)
        start = (page - 1) * limit
        batch = items[start:start + limit]
        if items == self.hooks[REPO]:
            return [self._view_hook(h) for h in batch]
        return copy.deepcopy(batch)

    def create(self, kind, repo, req):
        self.next_id += 1
        body = req.json
        if kind == "labels":
            label = {"id": self.next_id, "name": body["name"], "color": body["color"], "description": body.get("description") or ""}
            self.labels[repo].append(label)
            return 201, copy.deepcopy(label)
        hook = {"id": self.next_id, "type": body["type"], "active": body["active"],
                "events": self.effective_events(body["events"]), "branch_filter": "", "config": dict(body["config"])}
        self.hooks[repo].append(hook)
        return 201, self._view_hook(hook)

    def authed(self, req):
        return req.headers.get("authorization") == f"token {TOKEN}"


class RepositoryReconcilerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fake = FakeFactoryForgejo()
        self.srv = Server(self.fake)
        self.addCleanup(self.srv.close)
        from .helpers import config
        self.cfg = config(forgejo_url=self.srv.url, state_dir=self.tmp.name)
        self.state = State(f"{self.tmp.name}/state.json")

    def reconciler(self, **kw):
        params = dict(allowed_repos=[REPO], endpoint=ENDPOINT, secret=SECRET, token=TOKEN)
        params.update(kw)
        return RepositoryReconciler(self.cfg, self.state, **params)

    def writes(self):
        return [(r.method, r.path) for r in self.srv.requests if r.method in ("POST", "PUT", "PATCH", "DELETE")]

    def test_fresh_reconciliation_creates_six_labels_and_the_full_event_hook(self):
        self.reconciler().run()
        labels = {label["name"]: label for label in self.fake.labels[REPO]}
        self.assertEqual(set(LABELS) - {"ai:blocked"}, set(labels) - {"deploy"})
        for name, wanted in LABELS.items():
            self.assertEqual((labels[name]["color"], labels[name]["description"]), (wanted["color"], wanted["description"]))
        self.assertEqual(self.fake.labels[REPO][0]["name"], "deploy")
        hooks = [h for h in self.fake.hooks[REPO] if h["config"]["url"] == ENDPOINT]
        self.assertEqual(len(hooks), 1)
        hook = hooks[0]
        self.assertEqual((hook["type"], hook["active"], hook["events"]), ("forgejo", True, list(EXPANDED_EVENTS)))
        self.assertEqual((hook["config"]["url"], hook["config"]["content_type"], hook["config"]["secret"]),
                         (ENDPOINT, "json", SECRET))
        self.assertTrue(any(h["config"]["url"] == KOMODO_HOOK for h in self.fake.hooks[REPO]))
        reads = [r.path for r in self.srv.requests if r.method == "GET"]
        self.assertFalse(any(p.startswith(("/api/v1/user/repos", "/api/v1/repos/search")) for p in reads))

    def test_a_second_run_with_a_correct_hook_creates_nothing_and_deletes_nothing(self):
        self.reconciler().run()
        self.assertEqual(len(self.fake.labels[REPO]), 8)
        self.srv.requests.clear()
        self.reconciler().run()
        self.assertEqual(self.writes(), [])
        self.assertEqual(len(self.fake.labels[REPO]), 8)
        self.assertEqual(len(self.fake.hooks[REPO]), 2)

    def test_local_drift_is_corrected_with_one_patch_and_drifted_events_recreate_the_hook(self):
        self.reconciler().run()
        label = next(x for x in self.fake.labels[REPO] if x["name"] == "ai:ready")
        label.update({"color": "000000", "description": "old"})
        hook = next(h for h in self.fake.hooks[REPO] if h["config"]["url"] == ENDPOINT)
        hook["events"] = ["issues"]
        self.srv.requests.clear()
        self.reconciler().run()
        self.assertEqual([w for w in self.writes() if "/labels/" in w[1]], [("PATCH", f"/api/v1/repos/{REPO}/labels/{label['id']}")])
        self.assertFalse(any(method == "PATCH" and "/hooks" in path for method, path in self.writes()))
        self.assertTrue(any(method == "DELETE" and "/hooks/" in path for method, path in self.writes()))
        self.assertEqual((label["color"], label["description"]), (LABELS["ai:ready"]["color"], LABELS["ai:ready"]["description"]))
        hooks = [h for h in self.fake.hooks[REPO] if h["config"]["url"] == ENDPOINT]
        self.assertEqual([h["events"] for h in hooks], [list(EXPANDED_EVENTS)])

    def test_duplicate_owned_hooks_are_removed_and_only_one_is_recreated(self):
        self.reconciler().run()
        owned = next(h for h in self.fake.hooks[REPO] if h["config"]["url"] == ENDPOINT)
        self.fake.hooks[REPO].append({**copy.deepcopy(owned), "id": 777})
        self.assertEqual(len([h for h in self.fake.hooks[REPO] if h["config"]["url"] == ENDPOINT]), 2)
        self.reconciler().run()
        self.assertEqual(len([h for h in self.fake.hooks[REPO] if h["config"]["url"] == ENDPOINT]), 1)
        self.assertTrue(any(h["config"]["url"] == KOMODO_HOOK for h in self.fake.hooks[REPO]))

    def test_a_rotated_secret_is_detected_without_reading_it_back_and_a_failed_replacement_retries(self):
        self.reconciler().run()
        rotated = self.reconciler(secret="new-secret-value")
        self.fake.fail_once("POST", f"/api/v1/repos/{REPO}/hooks", message="temporary failure")
        with self.assertRaisesRegex(StepError, "temporary failure"):
            rotated.run()
        self.assertEqual([], [h for h in self.fake.hooks[REPO] if h["config"]["url"] == ENDPOINT])
        rotated.run()
        hooks = [h for h in self.fake.hooks[REPO] if h["config"]["url"] == ENDPOINT]
        self.assertEqual([(h["config"]["secret"], h["events"]) for h in hooks],
                         [("new-secret-value", list(EXPANDED_EVENTS))])
        self.srv.requests.clear()
        rotated.run()
        self.assertEqual(self.writes(), [])

    def test_failures_do_not_leak_the_secret_or_the_token(self):
        self.fake.fail_once("POST", f"/api/v1/repos/{REPO}/hooks", status=422,
                            message=f"bad config secret={SECRET} token={TOKEN}")
        with self.assertRaises(StepError) as caught:
            self.reconciler().run()
        text = str(caught.exception)
        self.assertIn("422", text)
        self.assertNotIn(SECRET, text)
        self.assertNotIn(TOKEN, text)

    def test_a_pull_mirror_is_rejected_before_any_write(self):
        self.fake.repos[REPO] = {"mirror": True, "original_url": "https://github.com/somewhere/else"}
        with self.assertRaisesRegex(StepError, "pull mirror is read-only"):
            self.reconciler().run()
        self.assertEqual(self.writes(), [])

    def test_a_migrated_primary_repository_with_an_original_url_is_accepted(self):
        self.fake.repos[REPO] = {"mirror": False, "original_url": "https://github.com/owner/repo"}
        with contextlib.redirect_stdout(io.StringIO()):
            self.reconciler().run()
        self.assertEqual(len([h for h in self.fake.hooks[REPO] if h["config"]["url"] == ENDPOINT]), 1)
        self.assertIn("ai:close", {label["name"] for label in self.fake.labels[REPO]})

    def test_the_read_back_of_the_expanded_issue_events_keeps_the_hook(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.reconciler().run()
        hook = next(h for h in self.fake.hooks[REPO] if h["config"]["url"] == ENDPOINT)
        self.assertEqual(hook["events"], list(EXPANDED_EVENTS))
        self.assertFalse(any(method == "DELETE" for method, _ in self.writes()))

    def test_a_hook_with_a_missing_required_event_is_replaced(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.reconciler().run()
        hook = next(h for h in self.fake.hooks[REPO] if h["config"]["url"] == ENDPOINT)
        hook["events"].remove("action_run_success")
        self.srv.requests.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            self.reconciler().run()
        self.assertIn(("DELETE", f"/api/v1/repos/{REPO}/hooks/{hook['id']}"), self.writes())
        hooks = [h for h in self.fake.hooks[REPO] if h["config"]["url"] == ENDPOINT]
        self.assertEqual([h["events"] for h in hooks], [list(EXPANDED_EVENTS)])

    def test_pagination_finds_every_label_and_hook(self):
        for i in range(5):
            self.fake.labels[REPO].append({"id": 200 + i, "name": f"keep-{i}", "color": "cccccc", "description": ""})
        with contextlib.redirect_stdout(io.StringIO()):
            self.reconciler().run()
        names = {label["name"] for label in self.fake.labels[REPO]}
        self.assertTrue(set(LABELS) <= names)
        self.assertTrue({f"keep-{i}" for i in range(5)} <= names)
        self.srv.requests.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            self.reconciler().run()
        self.assertEqual(self.writes(), [])

    def test_only_explicit_repositories_are_accepted(self):
        for bad in ([], "owner/*", "owner", "https://git.example/owner/repo", "owner/../repo"):
            with self.assertRaises(ValueError):
                self.reconciler(allowed_repos=bad if isinstance(bad, list) else [bad])
        self.assertEqual(self.srv.requests, [])

    def test_the_token_may_come_from_the_state_without_an_administrator_warning(self):
        self.fake.login = "owner"
        self.state.set("forgejo", "token", TOKEN)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
            reconcile(self.cfg, self.state, [REPO], ENDPOINT, SECRET)
        self.assertNotIn("administrator", stderr.getvalue())
        self.assertNotIn(TOKEN, stderr.getvalue())
        self.assertEqual(len([h for h in self.fake.hooks[REPO] if h["config"]["url"] == ENDPOINT]), 1)


if __name__ == "__main__":
    unittest.main()
