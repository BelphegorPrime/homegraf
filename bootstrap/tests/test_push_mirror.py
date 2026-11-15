"""The optional GitHub push mirror: guarded writes, drift-only repair, bounded sync checks and redaction."""
import contextlib
import io
import os
import unittest
from unittest import mock
from urllib.parse import parse_qs, unquote, urlsplit

from hgboot.push_mirror import STEP, PushMirror
from hgboot.steps import StepError
from .fakes import Server

FORGEJO_REPO = "marcel/homegraf"
GITHUB_REPO = "marcel/homegraf"
FORGEJO_TOKEN = "test-forgejo-token"
GITHUB_TOKEN = "test-github-token"
UPDATED = "2026-11-15T00:00:00Z"


def page(rows, query, parameter):
    number = int((query.get("page") or ["1"])[0])
    size = int((query.get(parameter) or ["100"])[0])
    return rows[(number - 1) * size:number * size]


class FakeMirror:
    """A real loopback service for the Forgejo mirror API and the GitHub branch API it drives."""

    def __init__(self, forgejo_branches, github_branches=None, mirrors=None, sync_error=None,
                 sync_branches=True, sync_main_sha=None, github_status=200, history=None,
                 default_branch="main", sync_last_update=UPDATED):
        self.forgejo_branches = dict(forgejo_branches)
        self.github_branches = dict(github_branches or {})
        self.mirrors = [dict(m) for m in (mirrors or [])]
        self.sync_error, self.sync_branches, self.sync_main_sha = sync_error, sync_branches, sync_main_sha
        self.github_status = github_status
        self.history = {sha: list(parents) for sha, parents in (history or {}).items()}
        self.default_branch = default_branch
        self.sync_last_update = sync_last_update
        self.syncs = 0

    def _reachable(self, sha):
        seen, stack = set(), [sha]
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            stack.extend(self.history.get(current, ()))
        return seen

    def _compare(self, branches, base, head):
        known = set().union(*(self._reachable(sha) for sha in branches.values())) if branches else set()
        if base not in known or head not in known:
            return None
        if base == head:
            return "identical"
        if base in self._reachable(head):
            return "ahead"
        if head in self._reachable(base):
            return "behind"
        return "diverged"

    def __call__(self, req):
        parts = urlsplit(req.path)
        query = parse_qs(parts.query)
        if parts.path == f"/api/v1/repos/{FORGEJO_REPO}" and req.method == "GET":
            if req.headers.get("authorization") != f"token {FORGEJO_TOKEN}":
                return 401, {"message": "no Forgejo token"}
            return 200, {"default_branch": self.default_branch}
        if parts.path == f"/api/v1/repos/{FORGEJO_REPO}/branches":
            if req.headers.get("authorization") != f"token {FORGEJO_TOKEN}":
                return 401, {"message": "no Forgejo token"}
            return 200, [{"name": n, "commit": {"id": s}}
                         for n, s in page(list(self.forgejo_branches.items()), query, "limit")]
        if parts.path.startswith(f"/api/v1/repos/{FORGEJO_REPO}/compare/") and req.method == "GET":
            if req.headers.get("authorization") != f"token {FORGEJO_TOKEN}":
                return 401, {"message": "no Forgejo token"}
            base, _, head = unquote(parts.path.rsplit("/", 1)[1]).partition("...")
            status = self._compare(self.forgejo_branches, base, head)
            if status is None:
                return 404, {"message": "The target couldn't be found."}
            commits = self._reachable(head) - self._reachable(base)
            return 200, {"total_commits": len(commits), "commits": [], "files": []}
        if parts.path.startswith(f"/repos/{GITHUB_REPO}/compare/") and req.method == "GET":
            if req.headers.get("authorization") != f"Bearer {GITHUB_TOKEN}":
                return 401, {"message": "Bad credentials test-github-token https://user:test-github-token@github.com/x"}
            base, _, head = unquote(parts.path.rsplit("/", 1)[1]).partition("...")
            status = self._compare(self.github_branches, base, head)
            if status is None:
                return 404, {"message": "Not Found"}
            return 200, {"status": status}
        if parts.path == f"/repos/{GITHUB_REPO}/branches":
            if req.headers.get("authorization") != f"Bearer {GITHUB_TOKEN}":
                return 401, {"message": "Bad credentials test-github-token https://user:test-github-token@github.com/x"}
            if self.github_status != 200:
                return self.github_status, {"message": f"failed for {GITHUB_TOKEN}"}
            return 200, [{"name": n, "commit": {"sha": s}}
                         for n, s in page(list(self.github_branches.items()), query, "per_page")]
        base = f"/api/v1/repos/{FORGEJO_REPO}/push_mirrors"
        if parts.path == base and req.method == "GET":
            if req.headers.get("authorization") != f"token {FORGEJO_TOKEN}":
                return 401, {"message": "no Forgejo token"}
            return 200, self.mirrors
        if parts.path == base and req.method == "POST":
            body = req.json
            mirror = {**body, "remote_name": "github", "last_update": None, "last_error": ""}
            self.mirrors.append(mirror)
            return 200, mirror
        if parts.path == f"{base}-sync" and req.method == "POST":
            self.syncs += 1
            if self.sync_error:
                for mirror in self.mirrors:
                    mirror["last_error"] = self.sync_error
                return 200, {}
            if self.sync_branches:
                self.github_branches = dict(self.forgejo_branches)
            if self.sync_main_sha:
                self.github_branches[self.default_branch] = self.sync_main_sha
            if self.sync_last_update:
                for mirror in self.mirrors:
                    mirror["last_update"] = self.sync_last_update
            return 200, {}
        if parts.path.startswith(base + "/") and req.method == "DELETE":
            name = parts.path[len(base) + 1:]
            before = len(self.mirrors)
            self.mirrors = [m for m in self.mirrors if m.get("remote_name") != name]
            if len(self.mirrors) == before:
                return 404, {"message": "no mirror"}
            return 204, None
        if parts.path.startswith(base + "/") and req.method == "PATCH":
            return 405, {"message": "method not allowed"}
        return 404, {"message": f"unexpected {req.method} {parts.path}"}


class MirrorTest(unittest.TestCase):
    def mirror(self, fake, **kwargs):
        self.fake = fake
        self.srv = Server(fake)
        self.addCleanup(self.srv.close)
        options = {"forgejo_url": self.srv.url, "forgejo_repo": FORGEJO_REPO, "forgejo_token": FORGEJO_TOKEN,
                   "github_repo": GITHUB_REPO, "github_token": GITHUB_TOKEN, "github_url": self.srv.url,
                   "github_api": self.srv.url, "sleep": lambda s: None, "attempts": 3}
        options.update(kwargs)
        return PushMirror(**options)

    def writes(self):
        return [r for r in self.srv.requests if r.method in ("POST", "PUT", "PATCH", "DELETE")]

    def correct_mirror(self):
        return {"remote_name": "github", "remote_address": f"{self.srv.url}/{GITHUB_REPO}.git",
                "sync_on_commit": True, "interval": "8h0m0s", "last_update": UPDATED, "last_error": ""}

    def test_a_new_mirror_is_created_then_synced_and_verified_from_branch_data(self):
        fake = FakeMirror({"main": "1" * 40, "release": "2" * 40})
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"HGBOOT_QUIET": ""}), contextlib.redirect_stdout(out):
            result = self.mirror(fake).sync()
        self.assertEqual(fake.github_branches, fake.forgejo_branches)
        self.assertEqual((result.created, result.repaired, result.synced), (True, False, True))
        self.assertEqual(result.branches, ("main", "release"))
        self.assertEqual((result.default_branch, result.default_sha), ("main", "1" * 40))
        creates = [r for r in self.writes() if r.path.endswith("/push_mirrors")]
        self.assertEqual(len(creates), 1)
        body = creates[0].json
        self.assertEqual((body["remote_address"], body["remote_username"], body["remote_password"]),
                         (f"{self.srv.url}/{GITHUB_REPO}.git", "git", GITHUB_TOKEN))
        self.assertEqual((body["sync_on_commit"], body["interval"]), (True, "8h0m0s"))
        self.assertNotIn(GITHUB_TOKEN, body["remote_address"])
        self.assertNotIn(GITHUB_TOKEN, out.getvalue())
        self.assertIn("synced", out.getvalue())
        self.assertEqual(fake.syncs, 1)

    def test_a_non_main_default_branch_is_read_from_forgejo_and_verified(self):
        fake = FakeMirror({"trunk": "1" * 40}, {"trunk": "1" * 40}, default_branch="trunk")
        mirror = self.mirror(fake)
        fake.mirrors = [self.correct_mirror()]
        result = mirror.sync()
        metadata = [r for r in self.srv.requests
                    if urlsplit(r.path).path == f"/api/v1/repos/{FORGEJO_REPO}"]
        self.assertEqual(len(metadata), 1)
        self.assertEqual(metadata[0].headers["authorization"], f"token {FORGEJO_TOKEN}")
        self.assertEqual((result.default_branch, result.default_sha), ("trunk", "1" * 40))
        self.assertEqual(fake.syncs, 0)

    def test_a_sync_that_leaves_a_different_default_branch_sha_is_a_failure(self):
        fake = FakeMirror({"trunk": "1" * 40}, {"trunk": "1" * 40}, default_branch="trunk",
                          sync_main_sha="9" * 40)
        mirror = self.mirror(fake)
        fake.mirrors = [{**self.correct_mirror(), "last_update": None}]
        with self.assertRaisesRegex(StepError, "trunk differs"):
            mirror.sync()

    def test_every_github_branch_page_is_compared_and_an_extra_branch_blocks_all_writes(self):
        forgejo = {"main": "1" * 40, **{f"feature-{i:03d}": f"{i:040d}" for i in range(204)}}
        github = {**forgejo, "z-stale": "9" * 40}
        fake = FakeMirror(forgejo, github)
        with self.assertRaisesRegex(StepError, "z-stale"):
            self.mirror(fake).sync()
        self.assertEqual(len(self.srv.calls("GET", f"/repos/{GITHUB_REPO}/branches")), 3)
        self.assertEqual(self.writes(), [])

    def test_a_branch_missing_on_github_is_created_by_the_sync(self):
        fake = FakeMirror({"main": "1" * 40, "release": "2" * 40}, {"main": "1" * 40})
        result = self.mirror(fake).sync()
        self.assertTrue(result.synced)
        self.assertEqual(fake.github_branches, fake.forgejo_branches)

    def test_a_diverged_shared_branch_blocks_every_write(self):
        common, forgejo, github = "3" * 40, "1" * 40, "2" * 40
        fake = FakeMirror({"main": forgejo, "archive": github}, {"main": github, "archive": github},
                          history={forgejo: [common], github: [common]})
        with self.assertRaisesRegex(StepError, "main"):
            self.mirror(fake).sync()
        self.assertEqual(self.writes(), [])
        self.assertEqual(len(self.srv.calls("GET", f"/repos/{GITHUB_REPO}/compare/")), 1)
        self.assertEqual(len(self.srv.calls("GET", f"/api/v1/repos/{FORGEJO_REPO}/compare/")), 2)

    def test_github_being_ahead_of_a_shared_branch_blocks_every_write(self):
        forgejo, github = "1" * 40, "2" * 40
        fake = FakeMirror({"main": forgejo}, {"main": github}, history={github: [forgejo]})
        with self.assertRaisesRegex(StepError, "main"):
            self.mirror(fake).sync()
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.srv.calls("GET", f"/api/v1/repos/{FORGEJO_REPO}/compare/"), [])

    def test_a_lagging_shared_branch_passes_when_github_does_not_know_the_commit(self):
        github, forgejo = "2" * 40, "1" * 40
        fake = FakeMirror({"main": forgejo}, {"main": github}, history={forgejo: [github]})
        result = self.mirror(fake).sync()
        compares = self.srv.calls("GET", f"/repos/{GITHUB_REPO}/compare/")
        self.assertEqual(urlsplit(compares[0].path).path, f"/repos/{GITHUB_REPO}/compare/{github}...{forgejo}")
        forgejo_compares = self.srv.calls("GET", f"/api/v1/repos/{FORGEJO_REPO}/compare/")
        self.assertEqual([urlsplit(r.path).path for r in forgejo_compares],
                         [f"/api/v1/repos/{FORGEJO_REPO}/compare/{github}...{forgejo}",
                          f"/api/v1/repos/{FORGEJO_REPO}/compare/{forgejo}...{github}"])
        self.assertEqual(fake.github_branches, fake.forgejo_branches)
        self.assertEqual(fake.syncs, 1)
        self.assertTrue(result.synced)

    def test_a_missing_token_is_an_explicit_failure_before_any_request(self):
        fake = FakeMirror({"main": "1" * 40})
        self.srv = Server(fake)
        self.addCleanup(self.srv.close)
        with self.assertRaisesRegex(StepError, "GitHub token"):
            PushMirror(forgejo_url=self.srv.url, forgejo_repo=FORGEJO_REPO, forgejo_token=FORGEJO_TOKEN,
                       github_repo=GITHUB_REPO, github_token="  ", github_url=self.srv.url,
                       github_api=self.srv.url, sleep=lambda s: None)
        self.assertEqual(self.srv.requests, [])

    def test_a_converged_mirror_is_neither_rewritten_nor_synced_on_rerun(self):
        fake = FakeMirror({"main": "1" * 40}, {"main": "1" * 40})
        mirror = self.mirror(fake)
        fake.mirrors = [self.correct_mirror()]
        first = mirror.sync()
        second = mirror.sync()
        self.assertEqual((first.created, first.repaired, first.synced), (False, False, False))
        self.assertEqual((second.created, second.repaired, second.synced), (False, False, False))
        self.assertEqual(self.writes(), [])
        self.assertEqual(fake.syncs, 0)
        self.assertEqual(second.branches, ("main",))
        self.assertEqual((second.default_branch, second.default_sha), ("main", "1" * 40))

    def test_a_mirror_never_synced_with_the_zero_time_or_epoch_is_synced(self):
        for stamp in (None, "", "0001-01-01T00:00:00Z", "1969-12-31T16:00:00-08:00",
                      "1970-01-01T00:00:00Z", "1970-01-01T08:00:00+08:00"):
            with self.subTest(last_update=stamp):
                fake = FakeMirror({"main": "1" * 40}, {"main": "1" * 40})
                mirror = self.mirror(fake)
                fake.mirrors = [{**self.correct_mirror(), "last_update": stamp}]
                result = mirror.sync()
                self.assertTrue(result.synced)
                self.assertEqual(fake.syncs, 1)
                self.assertEqual([urlsplit(r.path).path for r in self.writes()],
                                 [f"/api/v1/repos/{FORGEJO_REPO}/push_mirrors-sync"])
                started = fake.syncs
                second = mirror.sync()
                self.assertFalse(second.synced)
                self.assertEqual(fake.syncs, started)

    def test_a_first_sync_that_never_reports_completion_is_a_bounded_failure(self):
        fake = FakeMirror({"main": "1" * 40}, {"main": "1" * 40}, sync_last_update=None)
        mirror = self.mirror(fake)
        fake.mirrors = [{**self.correct_mirror(), "last_update": "1969-12-31T16:00:00-08:00"}]
        with self.assertRaisesRegex(StepError, "did not report a finished sync"):
            mirror.sync()
        self.assertEqual(fake.syncs, 1)
        reads = [r for r in self.srv.calls("GET", f"/api/v1/repos/{FORGEJO_REPO}/push_mirrors")
                 if urlsplit(r.path).path == f"/api/v1/repos/{FORGEJO_REPO}/push_mirrors"]
        self.assertEqual(len(reads), 1 + 3)

    def test_drift_is_repaired_by_delete_and_create_and_the_next_run_is_converged(self):
        fake = FakeMirror({"main": "1" * 40}, {"main": "1" * 40})
        mirror = self.mirror(fake)
        fake.mirrors = [{**self.correct_mirror(), "sync_on_commit": False, "interval": "1h0m0s"}]
        first = mirror.sync()
        writes = self.writes()
        self.assertEqual([r.method for r in writes], ["DELETE", "POST", "POST"])
        self.assertEqual(urlsplit(writes[0].path).path, f"/api/v1/repos/{FORGEJO_REPO}/push_mirrors/github")
        self.assertEqual(urlsplit(writes[1].path).path, f"/api/v1/repos/{FORGEJO_REPO}/push_mirrors")
        body = writes[1].json
        self.assertEqual((body["sync_on_commit"], body["interval"]), (True, "8h0m0s"))
        self.assertEqual(body["remote_password"], GITHUB_TOKEN)
        self.assertEqual((first.created, first.repaired, first.synced), (False, True, True))
        self.assertEqual(fake.syncs, 1)
        started = len(self.writes())
        second = mirror.sync()
        self.assertEqual((second.created, second.repaired, second.synced), (False, False, False))
        self.assertEqual(len(self.writes()), started)

    def test_a_failed_sync_is_explicit_and_hides_the_token_and_url_credentials(self):
        fake = FakeMirror({"main": "1" * 40}, {},
                          sync_error=f"remote: Authorization https://x:{GITHUB_TOKEN}@github.com/o/r.git {GITHUB_TOKEN}")
        with self.assertRaises(StepError) as caught:
            self.mirror(fake).sync()
        message = str(caught.exception)
        self.assertIn("failed", message)
        self.assertNotIn(GITHUB_TOKEN, message)
        self.assertNotIn("x:", message)
        self.assertIn("***@", message)
        self.assertEqual(fake.syncs, 1)

    def test_a_sync_that_leaves_a_different_main_sha_is_a_failure(self):
        fake = FakeMirror({"main": "1" * 40}, {"main": "1" * 40}, sync_main_sha="9" * 40)
        mirror = self.mirror(fake)
        fake.mirrors = [{**self.correct_mirror(), "last_update": None}]
        with self.assertRaisesRegex(StepError, "main differs"):
            mirror.sync()

    def test_a_sync_that_leaves_a_missing_branch_is_a_failure(self):
        fake = FakeMirror({"main": "1" * 40, "release": "2" * 40}, {"main": "1" * 40}, sync_branches=False)
        mirror = self.mirror(fake)
        fake.mirrors = [{**self.correct_mirror(), "last_update": None}]
        with self.assertRaisesRegex(StepError, "missing release"):
            mirror.sync()

    def test_github_api_errors_are_explicit_and_redacted(self):
        fake = FakeMirror({"main": "1" * 40}, {"main": "1" * 40}, github_status=500)
        with self.assertRaises(StepError) as caught:
            self.mirror(fake).sync()
        message = str(caught.exception)
        self.assertIn("500", message)
        self.assertNotIn(GITHUB_TOKEN, message)
        self.assertEqual(self.writes(), [])

    def test_an_authenticated_request_does_not_follow_a_redirect_to_another_origin(self):
        reached = Server(lambda req: (200, {"message": "reached"}))
        self.addCleanup(reached.close)
        elsewhere = reached.url.replace("127.0.0.1", "localhost")
        github = Server(lambda req: (302, {"message": "moved"}, {"Location": elsewhere + req.path}))
        self.addCleanup(github.close)
        forgejo = Server(FakeMirror({"main": "1" * 40}))
        self.addCleanup(forgejo.close)
        self.srv = forgejo
        mirror = PushMirror(forgejo_url=forgejo.url, forgejo_repo=FORGEJO_REPO, forgejo_token=FORGEJO_TOKEN,
                            github_repo=GITHUB_REPO, github_token=GITHUB_TOKEN, github_url=github.url,
                            github_api=github.url, sleep=lambda s: None, attempts=1)
        with self.assertRaises(StepError) as caught:
            mirror.sync()
        self.assertIn("302", str(caught.exception))
        self.assertEqual(reached.requests, [])
        self.assertEqual(github.requests[0].headers["authorization"], f"Bearer {GITHUB_TOKEN}")

    def test_a_commit_forgejo_does_not_know_is_an_explicit_and_redacted_failure(self):
        fake = FakeMirror({"main": "1" * 40}, {"main": "2" * 40})
        with self.assertRaises(StepError) as caught:
            self.mirror(fake).sync()
        message = str(caught.exception)
        self.assertIn("Forgejo does not know", message)
        self.assertIn("main", message)
        self.assertNotIn(GITHUB_TOKEN, message)
        self.assertEqual(self.writes(), [])

    def test_a_mirror_cannot_point_at_a_different_host_than_the_github_target(self):
        fake = FakeMirror({"main": "1" * 40}, {"main": "1" * 40})
        self.srv = Server(fake)
        self.addCleanup(self.srv.close)
        with self.assertRaisesRegex(StepError, "not the host"):
            PushMirror(forgejo_url=self.srv.url, forgejo_repo=FORGEJO_REPO, forgejo_token=FORGEJO_TOKEN,
                       github_repo=GITHUB_REPO, github_token=GITHUB_TOKEN, github_url="https://github.com",
                       github_api=self.srv.url, sleep=lambda s: None)
        self.assertEqual(self.srv.requests, [])

    def test_branches_are_requested_from_both_services_in_pages(self):
        fake = FakeMirror({"main": "1" * 40})
        self.mirror(fake).sync()
        forgejo = self.srv.calls("GET", f"/api/v1/repos/{FORGEJO_REPO}/branches")[0]
        github = self.srv.calls("GET", f"/repos/{GITHUB_REPO}/branches")[0]
        self.assertEqual(parse_qs(urlsplit(forgejo.path).query), {"limit": ["100"], "page": ["1"]})
        self.assertEqual(parse_qs(urlsplit(github.path).query), {"per_page": ["100"], "page": ["1"]})
        self.assertEqual(forgejo.headers["authorization"], f"token {FORGEJO_TOKEN}")
        self.assertEqual(github.headers["authorization"], f"Bearer {GITHUB_TOKEN}")
        self.assertEqual(STEP, "push-mirror")
