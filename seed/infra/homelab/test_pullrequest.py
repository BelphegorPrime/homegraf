"""Tests for the pull-request mode of new-app (pytest infra/homelab): git flow against a local bare origin,
Forgejo client against a local HTTP server."""
import http.server
import json
import pathlib
import subprocess
import sys
import threading

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import forgejo  # noqa: E402
import newapp  # noqa: E402
import ops  # noqa: E402
import ui  # noqa: E402


def sh(cwd, *args):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def clone(tmp_path, monkeypatch):
    origin, work = tmp_path / "origin.git", tmp_path / "work"
    sh(tmp_path, "git", "init", "-q", "--bare", "-b", "main", str(origin))
    sh(tmp_path, "git", "clone", "-q", str(origin), str(work))
    for k, v in (("user.name", "t"), ("user.email", "t@example.org")):
        sh(work, "git", "config", k, v)
    (work / "inventory").mkdir()
    (work / "inventory/stacks.yaml").write_text("stacks: []\n")
    sh(work, "git", "add", "-A")
    sh(work, "git", "commit", "-q", "-m", "init")
    sh(work, "git", "push", "-q", "origin", "main")
    monkeypatch.setattr(ops, "ROOT", work)
    monkeypatch.setattr(ui, "AUTO", True)
    monkeypatch.setattr(ui, "ASSUME_YES", True)
    return work


class FakeForgejo:
    def __init__(self):
        self.pulls = []

    def git_env(self, remote_url):
        return {}

    def find_pull(self, branch, base="main"):
        return next((p for p in self.pulls if p["branch"] == branch), None)

    def create_pull(self, branch, title, body, base="main"):
        pull = {"number": len(self.pulls) + 1, "html_url": f"https://git.example/pulls/{len(self.pulls) + 1}",
                "branch": branch, "title": title, "body": body}
        self.pulls.append(pull)
        return pull


def test_pull_request_flow_leaves_main_alone(clone):
    fg = FakeForgejo()
    (clone / "stacks/demo").mkdir(parents=True)  # untracked files come along to the branch
    (clone / "stacks/demo/compose.yaml").write_text("services: {}\n")
    back = ops.start_branch("new-app/demo")
    assert back == "main" and ops.git("rev-parse", "--abbrev-ref", "HEAD") == "new-app/demo"
    url = ops.open_pull_request(["stacks/demo"], "feat(demo): add", "feat(demo): add", "body", fg, back)
    assert url == "https://git.example/pulls/1"
    assert ops.git("rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert not (clone / "stacks/demo").exists()  # the change lives on the branch only
    assert sh(clone, "git", "ls-tree", "-r", "--name-only", "origin/new-app/demo", "stacks") == "stacks/demo/compose.yaml"
    assert sh(clone, "git", "rev-parse", "origin/main") == sh(clone, "git", "rev-parse", "main")


def test_pull_request_flow_resumes(clone):
    fg = FakeForgejo()
    (clone / "f.txt").write_text("x")
    back = ops.start_branch("new-app/demo")
    ops.open_pull_request(["f.txt"], "m", "t", "b", fg, back)
    # second run from main: the branch is continued from origin, the open pull request is found, nothing is duplicated
    back = ops.start_branch("new-app/demo")
    assert (clone / "f.txt").read_text() == "x"
    ops.open_pull_request(["f.txt"], "m", "t", "b", fg, back)
    assert len(fg.pulls) == 1
    assert len(sh(clone, "git", "log", "--oneline", "origin/new-app/demo").splitlines()) == 2


def test_start_branch_refuses_dirty_tracked_files(clone):
    (clone / "inventory/stacks.yaml").write_text("stacks: [x]\n")
    with pytest.raises(SystemExit, match="uncommitted changes"):
        ops.start_branch("new-app/demo")


def test_pull_request_without_change_is_an_error(clone):
    back = ops.start_branch("new-app/demo")
    with pytest.raises(SystemExit, match="nothing to open"):
        ops.open_pull_request(["inventory"], "m", "t", "b", FakeForgejo(), back)


def test_pr_body_names_secrets_but_no_values():
    app = newapp.App("demo", "worker-1", "", True, domain="demo.lab.example", upstream_port=8123,
                     secrets={"API_KEY": "hunter2", "B": None})
    body = newapp.pr_body(app)
    assert "API_KEY, B" in body and "hunter2" not in body
    assert "remove-app --app-id demo --server worker-1 --yes" in body       # an unmerged app is not in the inventory


@pytest.mark.parametrize("url,expected", [
    ("ssh://git@git.example.org/owner/infrastructure.git", ("ssh", "git.example.org", "owner/infrastructure")),
    ("git@git.example:owner/infrastructure.git", ("ssh", "git.example", "owner/infrastructure")),
    ("https://git.example:3000/owner/infra", ("https", "git.example", "owner/infra")),
    ("https://user:x@git.example/owner/infra.git", ("https", "git.example", "owner/infra")),
])
def test_parse_remote(url, expected):
    assert forgejo.parse_remote(url) == expected


def test_token_is_required(monkeypatch):
    monkeypatch.delenv("FORGEJO_TOKEN", raising=False)
    with pytest.raises(SystemExit, match="FORGEJO_TOKEN"):
        forgejo.Forgejo.from_env("ssh://git@git.example/a/b.git")


class _Handler(http.server.BaseHTTPRequestHandler):
    seen = []

    def _send(self, data):
        raw = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self.seen.append(("GET", self.path, self.headers["Authorization"], None))
        if self.path == "/api/v1/user":
            return self._send({"login": "owner"})
        self._send([{"number": 3, "html_url": "u3", "head": {"ref": "new-app/a"}, "base": {"ref": "main"}},
                    {"number": 4, "html_url": "u4", "head": {"ref": "other"}, "base": {"ref": "main"}}])

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.seen.append(("POST", self.path, self.headers["Authorization"], body))
        self._send({"number": 9, "html_url": "u9"})

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _Handler.seen = []
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_forgejo_client(server):
    fg = forgejo.Forgejo(server, "owner/infrastructure", "tok")
    assert fg.find_pull("new-app/a")["number"] == 3
    assert fg.find_pull("new-app/missing") is None
    assert fg.create_pull("new-app/b", "title", "body")["html_url"] == "u9"
    method, path, auth, body = _Handler.seen[-1]
    assert (method, path, auth) == ("POST", "/api/v1/repos/owner/infrastructure/pulls", "token tok")
    assert body == {"head": "new-app/b", "base": "main", "title": "title", "body": "body"}


def test_git_env_for_https_remote_only(server):
    fg = forgejo.Forgejo(server, "owner/infrastructure", "tok")
    assert fg.git_env("ssh://git@git.example/owner/infrastructure.git") == {}
    env = fg.git_env("https://git.example/owner/infrastructure.git")
    assert env["GIT_CONFIG_KEY_0"] == "http.extraHeader"
    import base64
    assert env["GIT_CONFIG_VALUE_0"] == "Authorization: Basic " + base64.b64encode(b"owner:tok").decode()


def test_git_network_commands_get_the_credentials_and_others_do_not(clone, monkeypatch):
    seen = []
    real = subprocess.run

    def spy(cmd, **kw):
        seen.append((cmd[3], (kw.get("env") or {}).get("GIT_CONFIG_VALUE_0")))
        return real(cmd, **kw)

    monkeypatch.setattr(ops.subprocess, "run", spy)
    monkeypatch.setattr(ops, "NET_ENV", {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.extraHeader", "GIT_CONFIG_VALUE_0": "Authorization: Basic x"})
    ops.git("fetch", "-q", "origin")
    ops.git("rev-parse", "HEAD")
    ops.git("push", "-q", "origin", "main")
    assert seen == [("fetch", "Authorization: Basic x"), ("rev-parse", None), ("push", "Authorization: Basic x")]


def test_start_branch_fetches_with_the_credentials(clone, monkeypatch):
    fetches = []
    real = subprocess.run
    monkeypatch.setattr(ops, "NET_ENV", {"GIT_CONFIG_VALUE_0": "marker"})
    monkeypatch.setattr(ops.subprocess, "run", lambda cmd, **kw: (fetches.append(kw["env"]["GIT_CONFIG_VALUE_0"]) if cmd[3] == "fetch" else None, real(cmd, **kw))[1])
    ops.start_branch("new-app/demo")
    assert fetches == ["marker"]
