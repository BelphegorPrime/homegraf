import base64
import contextlib
import copy
import hashlib
import io
import os
import tempfile
import unittest
from urllib.parse import parse_qs, unquote

from hgboot.factory_repos import LABELS
from hgboot.new_repo import NewRepository
from hgboot.state import State
from hgboot.steps import StepError

from .fakes import Server

TOKEN = "operator-token"
SECRET = "webhook-secret-value"
ENDPOINT = "http://forgehands:8080/webhooks/forgejo"
OWNER = "owner"
AGENT = "agent"
ORG = "lab-team"
NAME = "repository"
REPO = f"{OWNER}/{NAME}"
BRANCH = "main"
# issues and pull_request are stored as their whole groups.
EXPANDED_EVENTS = ("action_run_failure", "action_run_success", "issue_assign", "issue_comment", "issue_label",
                   "issue_milestone", "issues", "pull_request", "pull_request_assign", "pull_request_comment",
                   "pull_request_label", "pull_request_milestone", "pull_request_review_approved",
                   "pull_request_review_comment", "pull_request_review_rejected", "pull_request_review_request",
                   "pull_request_sync", "push")


class FakeNewRepoForgejo:
    """The Forgejo endpoints of the repository step, with pagination, masked secrets and the answers of the real API."""

    def __init__(self):
        self.users = {OWNER: {}, AGENT: {}}
        self.orgs = {ORG: {}}
        self.tokens = {TOKEN: OWNER}
        self.repos: dict[str, dict] = {}
        self.multi_file = True
        self.page_cap = 2
        self.masked = "********"
        self.failures: dict[tuple[str, str], tuple[int, dict]] = {}
        self.next_id = 100
        self.creates: list[str] = []
        self.commits: list[tuple[str, list[str]]] = []

    def add_repo(self, owner, name, *, empty=True, files=None, default_branch=BRANCH, private=True, description="",
                 collaborators=None):
        full = f"{owner}/{name}"
        self.repos[full] = {"empty": empty, "default_branch": default_branch, "private": private,
                            "description": description, "files": dict(files or {}),
                            "branches": set() if empty else {default_branch},
                            "collaborators": dict(collaborators or {}), "labels": [], "hooks": []}
        return full

    def fail_once(self, method, path, status=500, message="boom"):
        self.failures[(method, path)] = (status, {"message": message})

    def login(self, req):
        auth = req.headers.get("authorization", "")
        return self.tokens.get(auth[6:]) if auth.startswith("token ") else None

    def authed(self, req):
        return self.login(req) is not None

    def view(self, full_name):
        repo = self.repos[full_name]
        owner, name = full_name.split("/", 1)
        return {"full_name": full_name, "name": name, "owner": {"login": owner}, "empty": repo["empty"],
                "default_branch": repo["default_branch"], "private": repo["private"],
                "description": repo["description"], "mirror": False, "original_url": ""}

    @staticmethod
    def effective_events(events):
        expanded = set()
        for event in events:
            if event == "issues":
                expanded.update(("issue_assign", "issue_comment", "issue_label", "issue_milestone", "issues"))
            elif event == "pull_request":
                expanded.update(("pull_request", "pull_request_assign", "pull_request_comment", "pull_request_label",
                                 "pull_request_milestone", "pull_request_review_approved",
                                 "pull_request_review_comment", "pull_request_review_rejected",
                                 "pull_request_review_request", "pull_request_sync"))
            else:
                expanded.add(event)
        return sorted(expanded)

    def hook_view(self, hook):
        view = copy.deepcopy(hook)
        view["events"] = self.effective_events(view.get("events") or [])
        if isinstance(view.get("config"), dict) and "secret" in view["config"]:
            view["config"]["secret"] = self.masked
        return view

    def page(self, items, params):
        page = int(params.get("page", "1"))
        limit = min(int(params.get("limit", "50")), self.page_cap)
        start = (page - 1) * limit
        return items[start:start + limit]

    def __call__(self, req):
        p, _, query = req.path.partition("?")
        params = {k: v[0] for k, v in parse_qs(query).items()}
        failure = self.failures.pop((req.method, p), None)
        if failure:
            return failure
        if p == "/swagger.v1.json":
            operation = {"get": {}} if not self.multi_file else {"get": {}, "post": {}}
            return 200, {"swagger": "2.0", "paths": {"/repos/{owner}/{repo}/contents": operation}}
        if not self.authed(req):
            return 401, {"message": "unauthorized"}
        parts = p.split("/")
        if p == "/api/v1/user":
            return 200, {"login": self.login(req)}
        if len(parts) == 5 and parts[1:4] == ["api", "v1", "users"]:
            return (200, {"login": parts[4]}) if parts[4] in self.users else (404, {"message": "not found"})
        if len(parts) == 5 and parts[1:4] == ["api", "v1", "orgs"]:
            return (200, {"username": parts[4]}) if parts[4] in self.orgs else (404, {"message": "not found"})
        if p == "/api/v1/user/repos" and req.method == "POST":
            return self.create_repo(self.login(req), req)
        if len(parts) == 6 and parts[1:4] == ["api", "v1", "orgs"] and parts[5] == "repos" and req.method == "POST":
            return self.create_repo(parts[4], req) if parts[4] in self.orgs else (404, {"message": "not found"})
        if len(parts) < 6 or parts[1:4] != ["api", "v1", "repos"]:
            return 404, {"message": f"unexpected {req.method} {p}"}
        full = f"{unquote(parts[4])}/{unquote(parts[5])}"
        if full not in self.repos:
            return 404, {"message": "not found"}
        repo, rest = self.repos[full], parts[6:]
        if not rest:
            if req.method == "GET":
                return 200, self.view(full)
            if req.method == "PATCH":
                return self.edit_repo(repo, req)
            return 405, {"message": "method not allowed"}
        if rest[0] == "contents":
            return self.contents(repo, rest[1:], params, req)
        if rest[0] == "collaborators" and len(rest) == 2:
            login = unquote(rest[1])
            if login not in self.users:
                return 404, {"message": "not found"}
            if req.method == "PUT":
                repo["collaborators"][login] = req.json["permission"]
                return 204, None
            if req.method == "GET":
                return (204, None) if login in repo["collaborators"] else (404, {"message": "not found"})
            return 405, {"message": "method not allowed"}
        if rest[0] == "collaborators" and len(rest) == 3 and rest[2] == "permission" and req.method == "GET":
            login = unquote(rest[1])
            if login not in repo["collaborators"]:
                return 404, {"message": "not found"}
            return 200, {"permission": repo["collaborators"][login], "role_name": repo["collaborators"][login],
                         "user": {"login": login}}
        if rest[0] == "labels":
            return self.labels(repo, rest[1:], params, req)
        if rest[0] == "hooks":
            return self.hooks(repo, rest[1:], params, req)
        return 404, {"message": f"unexpected {req.method} {p}"}

    def create_repo(self, owner, req):
        body = req.json
        full = f"{owner}/{body['name']}"
        if full in self.repos:
            return 409, {"message": "The repository with the same name already exists."}
        self.add_repo(owner, body["name"], default_branch=body.get("default_branch") or "main",
                      private=bool(body.get("private")), description=body.get("description") or "")
        self.creates.append(full)
        return 201, self.view(full)

    def edit_repo(self, repo, req):
        body = req.json
        if "default_branch" in body:
            branch = body["default_branch"]
            if not repo["empty"] and branch not in repo["branches"]:
                return 422, {"message": "default branch does not exist"}
            repo["default_branch"] = branch
        for key in ("private", "description"):
            if key in body:
                repo[key] = body[key]
        return 200, self.view_body(repo)

    def view_body(self, repo):
        for full, candidate in self.repos.items():
            if candidate is repo:
                return self.view(full)
        raise AssertionError("unknown repository")

    def contents(self, repo, rest, params, req):
        if not rest:
            if req.method == "GET":
                return 200, [{"name": path.split("/")[-1], "path": path, "type": "file",
                              "sha": hashlib.sha1(data).hexdigest()} for path, data in sorted(repo["files"].items())]
            if req.method == "POST":
                return self.commit_many(repo, req)
            return 405, {"message": "method not allowed"}
        path = "/".join(unquote(part) for part in rest)
        if req.method == "GET":
            ref = params.get("ref") or repo["default_branch"]
            if repo["empty"]:
                return 200, []
            if ref not in repo["branches"] or path not in repo["files"]:
                return 404, {"message": "not found"}
            data = repo["files"][path]
            encoded = base64.b64encode(data).decode("ascii")
            return 200, {"type": "file", "name": path.rsplit("/", 1)[-1], "path": path,
                         "sha": hashlib.sha1(data).hexdigest(), "size": len(data), "encoding": "base64",
                         "content": "\n".join(encoded[i:i + 60] for i in range(0, len(encoded), 60))}
        if req.method == "POST":
            return self.commit_one(repo, path, req)
        return 405, {"message": "method not allowed"}

    def commit_many(self, repo, req):
        body = req.json
        branch = body.get("branch") or repo["default_branch"]
        if not repo["empty"] and branch not in repo["branches"]:
            return 404, {"message": "branch not found"}
        for operation in body.get("files") or []:
            if operation.get("operation") != "create":
                return 422, {"message": "this fixture only creates files"}
            if operation["path"] in repo["files"]:
                return 422, {"message": f"{operation['path']} already exists"}
        for operation in body.get("files") or []:
            repo["files"][operation["path"]] = base64.b64decode(operation["content"])
            repo["branches"].add(branch)
            repo["empty"] = False
        self.commits.append(("multi", [operation["path"] for operation in body["files"]]))
        return 201, {"commit": {"sha": "abc"}}

    def commit_one(self, repo, path, req):
        body = req.json
        branch = body.get("branch") or repo["default_branch"]
        if not repo["empty"] and branch not in repo["branches"]:
            return 404, {"message": "branch not found"}
        if path in repo["files"]:
            return 422, {"message": f"{path} already exists"}
        repo["files"][path] = base64.b64decode(body["content"])
        repo["branches"].add(branch)
        repo["empty"] = False
        self.commits.append(("single", [path]))
        return 201, {"commit": {"sha": "def"}}

    def labels(self, repo, rest, params, req):
        if not rest:
            if req.method == "GET":
                return 200, copy.deepcopy(self.page(repo["labels"], params))
            if req.method == "POST":
                self.next_id += 1
                label = {"id": self.next_id, "name": req.json["name"], "color": req.json["color"],
                         "description": req.json.get("description") or ""}
                repo["labels"].append(label)
                return 201, copy.deepcopy(label)
            return 405, {"message": "method not allowed"}
        label = next((x for x in repo["labels"] if str(x["id"]) == rest[0]), None)
        if label is None:
            return 404, {"message": "not found"}
        if req.method == "GET":
            return 200, copy.deepcopy(label)
        if req.method == "PATCH":
            label.update({key: req.json[key] for key in ("color", "description") if key in req.json})
            return 200, copy.deepcopy(label)
        return 405, {"message": "method not allowed"}

    def hooks(self, repo, rest, params, req):
        if not rest:
            if req.method == "GET":
                return 200, [self.hook_view(hook) for hook in self.page(repo["hooks"], params)]
            if req.method == "POST":
                self.next_id += 1
                body = req.json
                hook = {"id": self.next_id, "type": body["type"], "active": body["active"],
                        "events": self.effective_events(body["events"]), "branch_filter": body.get("branch_filter") or "",
                        "config": dict(body["config"])}
                repo["hooks"].append(hook)
                return 201, self.hook_view(hook)
            return 405, {"message": "method not allowed"}
        hook = next((x for x in repo["hooks"] if str(x["id"]) == rest[0]), None)
        if hook is None:
            return 404, {"message": "not found"}
        if req.method == "GET":
            return 200, self.hook_view(hook)
        if req.method == "DELETE":
            repo["hooks"].remove(hook)
            return 204, None
        return 405, {"message": "method not allowed"}


class NewRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fake = FakeNewRepoForgejo()
        self.srv = Server(self.fake)
        self.addCleanup(self.srv.close)
        from .helpers import config
        self.cfg = config(forgejo_url=self.srv.url, state_dir=self.tmp.name)
        self.state = State(f"{self.tmp.name}/state.json")
        self.files = {"AGENTS.md": "# Agent guide\n", "README.md": "# repository\n",
                      ".forgejo/workflows/ci.yaml": "name: ci\n"}

    def create(self, **over):
        params = dict(token=TOKEN, owner=OWNER, name=NAME, description="A repository for the agents", visibility="private",
                      default_branch=BRANCH, agent_login=AGENT, files=self.files, forgehands_endpoint=ENDPOINT,
                      webhook_secret=SECRET)
        params.update(over)
        return NewRepository(self.cfg, self.state, **params).run()

    def writes(self):
        return [(r.method, r.path) for r in self.srv.requests if r.method in ("POST", "PUT", "PATCH", "DELETE")]

    def test_a_new_repository_is_created_wired_and_read_back(self):
        result = self.create()
        repo = self.fake.repos[REPO]
        self.assertEqual({path: content.encode("utf-8") for path, content in self.files.items()}, repo["files"])
        self.assertEqual((repo["private"], repo["description"], repo["default_branch"]),
                         (True, "A repository for the agents", BRANCH))
        self.assertEqual(repo["branches"], {BRANCH})
        self.assertEqual(repo["collaborators"], {AGENT: "write"})
        self.assertTrue(set(LABELS) <= {label["name"] for label in repo["labels"]})
        self.assertEqual(len(repo["hooks"]), 1)
        self.assertEqual(repo["hooks"][0]["config"], {"url": ENDPOINT, "content_type": "json", "secret": SECRET})
        self.assertEqual(repo["hooks"][0]["events"], list(EXPANDED_EVENTS))
        self.assertEqual(self.fake.commits, [("multi", sorted(self.files))])
        self.assertEqual(result.repository_status, "created")
        self.assertEqual(result.collaborator_status, "added")
        self.assertEqual(result.commit, "created")
        self.assertEqual(result.files, tuple(sorted(self.files)))
        self.assertEqual(result.labels_created, tuple(LABELS))
        self.assertEqual(result.labels_updated, ())
        self.assertEqual(result.hook_status, "created")
        self.assertEqual(result.hook_events, EXPANDED_EVENTS)
        self.assertEqual(result.default_branch, BRANCH)

    def test_a_second_run_changes_nothing(self):
        first = self.create()
        self.assertEqual(first.repository_status, "created")
        label_count, hook_count = len(self.fake.repos[REPO]["labels"]), len(self.fake.repos[REPO]["hooks"])
        self.srv.requests.clear()
        second = self.create()
        self.assertEqual(self.writes(), [])
        self.assertEqual(second.repository_status, "already there")
        self.assertEqual(second.commit, "no change")
        self.assertEqual(second.collaborator_status, "already there")
        self.assertEqual(second.labels_created, ())
        self.assertEqual(second.labels_updated, ())
        self.assertEqual(second.hook_status, "already there")
        self.assertEqual(len(self.fake.repos[REPO]["labels"]), label_count)
        self.assertEqual(len(self.fake.repos[REPO]["hooks"]), hook_count)
        self.assertEqual(len(self.fake.commits), 1)

    def test_a_non_empty_existing_repository_is_refused(self):
        self.fake.add_repo(OWNER, NAME, empty=False, files={"NOTES.md": b"someone else's notes\n"})
        with self.assertRaisesRegex(StepError, "not empty; refusing to touch"):
            self.create()
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.fake.repos[REPO]["collaborators"], {})

    def test_a_non_empty_repository_that_already_holds_every_file_is_not_touched(self):
        self.fake.add_repo(OWNER, NAME, empty=False,
                           files={path: content.encode("utf-8") for path, content in self.files.items()})
        result = self.create()
        self.assertEqual(result.repository_status, "already there")
        self.assertEqual(result.commit, "no change")
        self.assertEqual(self.fake.commits, [])
        self.assertTrue(set(LABELS) <= {label["name"] for label in self.fake.repos[REPO]["labels"]})

    def test_a_different_file_in_an_existing_repository_is_never_overwritten(self):
        self.fake.add_repo(OWNER, NAME, empty=False, files={"README.md": b"someone else's readme\n"})
        with self.assertRaisesRegex(StepError, "refusing to overwrite README.md"):
            self.create()
        self.assertEqual(self.fake.repos[REPO]["files"]["README.md"], b"someone else's readme\n")
        self.assertEqual(self.writes(), [])

    def test_an_existing_empty_repository_is_reused_and_its_default_branch_is_set(self):
        self.fake.add_repo(OWNER, NAME, empty=True, default_branch="trunk")
        result = self.create()
        self.assertEqual(self.fake.creates, [])
        self.assertEqual(result.repository_status, "reused")
        self.assertEqual(self.fake.repos[REPO]["default_branch"], BRANCH)
        self.assertEqual(self.fake.commits, [("multi", sorted(self.files))])
        self.assertIn(("PATCH", f"/api/v1/repos/{REPO}"), self.writes())

    def test_an_organisation_owner_creates_the_repository_in_the_organisation(self):
        result = self.create(owner=ORG)
        self.assertEqual(self.fake.creates, [f"{ORG}/{NAME}"])
        self.assertTrue(any(method == "POST" and path == f"/api/v1/orgs/{ORG}/repos" for method, path in self.writes()))
        self.assertFalse(any(method == "POST" and path == "/api/v1/user/repos" for method, path in self.writes()))
        self.assertEqual(result.repository, f"{ORG}/{NAME}")
        self.assertEqual(result.repository_status, "created")

    def test_public_visibility_is_explicit(self):
        self.create(visibility="public")
        self.assertFalse(self.fake.repos[REPO]["private"])

    def test_an_existing_read_permission_is_raised_to_write(self):
        self.fake.add_repo(OWNER, NAME, empty=True, collaborators={AGENT: "read"})
        result = self.create()
        self.assertEqual(result.collaborator_status, "updated")
        self.assertEqual(self.fake.repos[REPO]["collaborators"][AGENT], "write")

    def test_a_failing_step_is_explicit_and_hides_the_token_and_the_secret(self):
        self.fake.fail_once("POST", f"/api/v1/repos/{REPO}/contents", status=422,
                            message=f"bad commit token={TOKEN} secret={SECRET}")
        with self.assertRaises(StepError) as caught:
            self.create()
        text = str(caught.exception)
        self.assertIn("committing the files", text)
        self.assertIn("422", text)
        self.assertIn("bad commit", text)
        self.assertNotIn(TOKEN, text)
        self.assertNotIn(SECRET, text)

    def test_an_owner_that_is_neither_the_token_user_nor_an_organisation_is_explicit(self):
        self.fake.users["someone"] = {}
        with self.assertRaisesRegex(StepError, "another user"):
            self.create(owner="someone")
        with self.assertRaisesRegex(StepError, "no user or organisation"):
            self.create(owner="ghost")

    def test_the_token_and_the_secret_never_reach_a_log_line_the_state_or_the_result(self):
        previous = os.environ.pop("HGBOOT_QUIET", None)
        stdout, stderr = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                result = self.create()
        finally:
            if previous is not None:
                os.environ["HGBOOT_QUIET"] = previous
        self.assertIn("[new-repo]", stdout.getvalue())
        for text in (stdout.getvalue(), stderr.getvalue(), repr(result), self.state.path.read_text()):
            self.assertNotIn(TOKEN, text)
            self.assertNotIn(SECRET, text)

    def test_the_per_file_fallback_is_used_when_the_swagger_lacks_the_multi_file_post(self):
        self.fake.multi_file = False
        result = self.create()
        self.assertEqual(result.commit, "created")
        self.assertEqual(self.fake.commits, [("single", [path]) for path in sorted(self.files)])
        self.assertEqual({path: content.encode("utf-8") for path, content in self.files.items()},
                         self.fake.repos[REPO]["files"])
        self.assertTrue(all(method == "POST" and path.startswith(f"/api/v1/repos/{REPO}/contents/")
                            for method, path in self.writes() if "/contents" in path))

    def test_invalid_inputs_are_refused_before_any_request(self):
        cases = [dict(token=None), dict(token=""), dict(owner="owner/other"), dict(name=".."), dict(visibility="internal"),
                 dict(default_branch="bad branch"), dict(agent_login="the agent"), dict(files={}),
                 dict(files={"../escape": "x"}), dict(files={"/absolute": "x"}), dict(files={"a": 3}),
                 dict(forgehands_endpoint="ftp://forgehands/hook"), dict(webhook_secret=""), dict(description=None)]
        for over in cases:
            with self.subTest(over=over), self.assertRaises(ValueError):
                self.create(**over)
        self.assertEqual(self.srv.requests, [])


if __name__ == "__main__":
    unittest.main()
