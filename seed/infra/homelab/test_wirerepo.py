"""Tests for the `--wire` step of new-repo (pytest from the repository root).

The edit is checked on copies of ForgeHands compose files: the shipped template
(infra/homelab/templates/agents/compose.yaml) for the mapping form, and a fixture shaped like the operator's own stack
(environment list, single-quoted JSON) for the list form. Both are parsed with YAML and their values with JSON, so the
text edit has to leave a file compose and ForgeHands accept. The delivery runs against a local bare origin and a fake
Forgejo client, the CLI against a loopback HTTP recorder: no live server, no real credentials.
"""
import http.server
import json
import os
import pathlib
import subprocess
import sys
import threading

import pytest
import yaml

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import ops  # noqa: E402
import ui  # noqa: E402
import wirerepo  # noqa: E402

SEED = pathlib.Path(__file__).resolve().parents[2]
TEMPLATE = pathlib.Path(__file__).resolve().parent / "templates/agents/compose.yaml"
REPO = "owner/newrepo"
BRANCH = "new-repo/newrepo"
TOKEN = "operator-token"

# The operator's stack puts the environment in list form and single-quotes the policy (see the real counterpart: an
# env list `- ALLOWED_REPOS=a,b` and `- 'MERGE_POLICY={...}'`, because compose reads both as strings).
LIST_FORM = """\
services:
  forgehands:
    image: ghcr.io/belphegorprime/forgehands:v0.3.10
    environment:
      - OPENHANDS_API_URL=http://openhands:8000
      # Both stores the agents may work on; comma separated, no spaces.
      - 'MERGE_POLICY={"owner/repository":{"deny":["stacks/**","infra/**"]}}'
      - ALLOWED_SENDERS=operator,openhands
      - ALLOWED_REPOS=owner/repository,owner/other
      - PORT=8080
    ports:
      - 8790:8080
"""


def test_the_edit_keeps_the_shipped_template_and_adds_the_repository(tmp_path):
    text = TEMPLATE.read_text()
    edited, allowed, policy = wirerepo.edit(text, REPO)
    assert (allowed, policy) == (False, False)
    environment = yaml.safe_load(edited)["services"]["app"]["environment"]
    assert environment["ALLOWED_REPOS"] == f"owner/repository,{REPO}"      # a comma list, no spaces
    merge = json.loads(environment["MERGE_POLICY"])
    assert set(merge) == {"owner/repository", REPO}
    assert merge[REPO] == {"deny": list(wirerepo.DEFAULT_DENY)}
    assert merge["owner/repository"]["deny"][-2:] == ["infra/**", "stacks/**"]   # the existing entry is untouched
    comments = [line for line in text.splitlines() if line.lstrip().startswith("#")]
    assert comments and all(line in edited for line in comments)                  # comments survive
    assert edited != text


def test_the_edit_keeps_the_list_form_and_its_quoting():
    edited, allowed, policy = wirerepo.edit(LIST_FORM, REPO)
    assert (allowed, policy) == (False, False)
    environment = yaml.safe_load(edited)["services"]["forgehands"]["environment"]
    by_name = dict(item.split("=", 1) for item in environment)
    assert by_name["ALLOWED_REPOS"] == f"owner/repository,owner/other,{REPO}"
    assert json.loads(by_name["MERGE_POLICY"]) == {
        "owner/repository": {"deny": ["stacks/**", "infra/**"]}, REPO: {"deny": list(wirerepo.DEFAULT_DENY)}}
    assert "      - 'MERGE_POLICY={" in edited                     # still a single-quoted list item
    assert "      - ALLOWED_REPOS=owner/repository,owner/other," in edited
    assert "      # Both stores the agents may work on; comma separated, no spaces.\n" in edited
    assert "      - PORT=8080\n" in edited and edited.endswith("      - 8790:8080\n")


@pytest.mark.parametrize("text", [TEMPLATE.read_text(), LIST_FORM])
def test_a_second_edit_changes_nothing_and_reports_the_entry(text):
    once, _, _ = wirerepo.edit(text, REPO)
    twice, allowed, policy = wirerepo.edit(once, REPO)
    assert twice == once
    assert (allowed, policy) == (True, True)


def test_a_repository_already_in_the_list_is_not_added_twice():
    text = LIST_FORM.replace("ALLOWED_REPOS=owner/repository,owner/other", f"ALLOWED_REPOS={REPO},owner/other")
    edited, allowed, policy = wirerepo.edit(text, REPO)
    by_name = dict(item.split("=", 1) for item in yaml.safe_load(edited)["services"]["forgehands"]["environment"])
    assert by_name["ALLOWED_REPOS"] == f"{REPO},owner/other"
    assert allowed is True and policy is False


@pytest.mark.parametrize("name", ["ALLOWED_REPOS", "MERGE_POLICY"])
def test_a_value_from_the_environment_is_refused_instead_of_edited(name):
    value = "${FORGEHANDS_ALLOWED_REPOS:-}" if name == "ALLOWED_REPOS" else "${FORGEHANDS_MERGE_POLICY:-}"
    text = LIST_FORM.replace(f"- {name}=owner/repository,owner/other" if name == "ALLOWED_REPOS" else
                             "- 'MERGE_POLICY={\"owner/repository\":{\"deny\":[\"stacks/**\",\"infra/**\"]}}'",
                             f"- {name}={value}")
    with pytest.raises(ValueError, match="compose variable"):
        wirerepo.edit(text, REPO)


def test_a_missing_key_is_named():
    with pytest.raises(ValueError, match="MERGE_POLICY not found"):
        wirerepo.edit(LIST_FORM.replace("      - 'MERGE_POLICY=", "      - LABEL_ROUTES="), REPO)


def _root(tmp_path, *stacks):
    for name, text in stacks:
        folder = tmp_path / "stacks" / name
        folder.mkdir(parents=True)
        (folder / "compose.yaml").write_text(text)
    return tmp_path


def test_the_one_stack_that_runs_forgehands_is_found(tmp_path):
    root = _root(tmp_path, ("web", "services:\n  app:\n    image: nginx:1.27\n"), ("agents", LIST_FORM))
    assert wirerepo.locate(root) == root / "stacks/agents/compose.yaml"


def test_no_forgehands_stack_is_an_error_that_names_what_was_found(tmp_path):
    root = _root(tmp_path, ("web", "services:\n  app:\n    image: nginx:1.27\n"),
                 ("db", "services:\n  db:\n    image: postgres:16\n"))
    with pytest.raises(SystemExit, match=r"none of the 2 stacks runs ForgeHands.*: db, web"):
        wirerepo.locate(root)


def test_several_forgehands_stacks_are_an_error_that_names_them(tmp_path):
    root = _root(tmp_path, ("one", LIST_FORM), ("two", LIST_FORM))
    with pytest.raises(SystemExit, match=r"several stacks run ForgeHands.*stacks/one/compose.yaml, stacks/two/compose.yaml"):
        wirerepo.locate(root)


def test_a_repository_without_stacks_says_so(tmp_path):
    with pytest.raises(SystemExit, match="no stacks/<id>/compose.yaml"):
        wirerepo.locate(tmp_path)


def test_the_plan_names_the_compose_file_and_its_edit(tmp_path):
    root = _root(tmp_path, ("agents", LIST_FORM))
    wiring = wirerepo.plan(REPO, root)
    assert wiring.path == root / "stacks/agents/compose.yaml" and wiring.changed and wiring.stack == "agents"
    assert (root / "stacks/agents/compose.yaml").read_text() == LIST_FORM   # plan only reads


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


def sh(cwd, *args):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def clone(tmp_path, monkeypatch):
    origin, work = tmp_path / "origin.git", tmp_path / "work"
    sh(tmp_path, "git", "init", "-q", "--bare", "-b", "main", str(origin))
    sh(tmp_path, "git", "clone", "-q", str(origin), str(work))
    for k, v in (("user.name", "t"), ("user.email", "t@example.org")):
        sh(work, "git", "config", k, v)
    (work / "stacks/agents").mkdir(parents=True)
    (work / "stacks/agents/compose.yaml").write_text(LIST_FORM)
    sh(work, "git", "add", "-A")
    sh(work, "git", "commit", "-q", "-m", "init")
    sh(work, "git", "push", "-q", "origin", "main")
    monkeypatch.setattr(ops, "ROOT", work)
    monkeypatch.setattr(ui, "AUTO", True)
    monkeypatch.setattr(ui, "ASSUME_YES", True)
    return work


def test_the_wiring_goes_to_a_branch_and_a_pull_request(clone):
    fake = FakeForgejo()
    url = wirerepo.deliver(REPO, fake)
    assert url == "https://git.example/pulls/1"
    assert ops.git("rev-parse", "--abbrev-ref", "HEAD") == "main"                 # main stays checked out
    assert REPO not in ops.git("show", "main:stacks/agents/compose.yaml")         # and unchanged
    branch_text = ops.git("show", f"origin/{BRANCH}:stacks/agents/compose.yaml")
    assert REPO in branch_text
    assert len(fake.pulls) == 1
    pull = fake.pulls[0]
    assert pull["branch"] == BRANCH and pull["title"].startswith("feat(new-repo): wire ")
    assert wirerepo.DEFAULT_DENY[0] in pull["body"] and "never merged automatically" in pull["body"]
    assert "stacks/agents/compose.yaml" in pull["body"]


def test_a_rerun_finds_the_open_pull_request_and_adds_no_commit(clone):
    fake = FakeForgejo()
    url = wirerepo.deliver(REPO, fake)
    commits = sh(clone, "git", "log", "--oneline", f"origin/{BRANCH}").splitlines()
    again = wirerepo.deliver(REPO, fake)
    assert again == url and len(fake.pulls) == 1
    assert sh(clone, "git", "log", "--oneline", f"origin/{BRANCH}").splitlines() == commits
    assert ops.git("rev-parse", "--abbrev-ref", "HEAD") == "main"


def test_a_repository_already_wired_in_main_opens_nothing(clone):
    wiring = wirerepo.plan(REPO)
    wiring.path.write_text(wiring.edited)
    sh(clone, "git", "add", "-A")
    sh(clone, "git", "commit", "-q", "-m", "wired")
    sh(clone, "git", "push", "-q", "origin", "main")
    fake = FakeForgejo()
    assert wirerepo.deliver(REPO, fake) is None
    assert fake.pulls == []
    assert ops.git("rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert sh(clone, "git", "branch", "--list", BRANCH) == ""    # no empty branch was pushed


class Recorder(http.server.BaseHTTPRequestHandler):
    seen = []

    def _record(self):
        Recorder.seen.append((self.command, self.path))
        self.send_response(500)
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_GET = do_POST = do_PATCH = _record

    def log_message(self, *args):
        pass


@pytest.fixture
def forgejo_server():
    server = http.server.HTTPServer(("127.0.0.1", 0), Recorder)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    Recorder.seen = []
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


CLEAR = ("FORGEJO_TOKEN", "FORGEJO_URL", "FORGEHANDS_FORGEJO_WEBHOOK_URL", "FORGEHANDS_FORGEJO_WEBHOOK_SECRET",
         "STATE_DIR", "HOMELAB_ROOT", "HGBOOT_QUIET")


def cli(root, server, *args, token=TOKEN):
    env = {k: v for k, v in os.environ.items() if k not in CLEAR}
    env.update(HOMELAB_ROOT=str(root), STATE_DIR=str(root.parent / "state"), PYTHONDONTWRITEBYTECODE="1",
               FORGEJO_URL=server, FORGEHANDS_FORGEJO_WEBHOOK_URL="http://forgehands:8080/webhooks/forgejo",
               FORGEHANDS_FORGEJO_WEBHOOK_SECRET="webhook-secret-value")
    if token is not None:
        env["FORGEJO_TOKEN"] = token
    return subprocess.run([sys.executable, "-B", str(SEED / "homelab.py"), "new-repo", "--name", "newrepo",
                           "--profile", "python", "--owner", "owner", "--wire", *args],
                          cwd=SEED, env=env, capture_output=True, text=True, timeout=60)


@pytest.mark.skipif(not (SEED.parent / "bootstrap/hgboot/new_repo.py").is_file(),
                    reason="the bootstrap is outside seed/")
def test_the_plan_with_wire_changes_nothing_and_makes_no_request(tmp_path, forgejo_server):
    root = _root(tmp_path, ("agents", LIST_FORM))
    got = cli(root, forgejo_server)
    assert got.returncode == 0, got.stderr
    assert "stacks/agents/compose.yaml" in got.stdout and REPO in got.stdout
    assert "rerun with --yes" in got.stdout
    assert (root / "stacks/agents/compose.yaml").read_text() == LIST_FORM
    assert Recorder.seen == []


@pytest.mark.skipif(not (SEED.parent / "bootstrap/hgboot/new_repo.py").is_file(),
                    reason="the bootstrap is outside seed/")
def test_wire_without_the_token_fails_and_names_the_variable(tmp_path, forgejo_server):
    root = _root(tmp_path, ("agents", LIST_FORM))
    got = cli(root, forgejo_server, "--yes", token=None)
    assert got.returncode == 1
    assert "FORGEJO_TOKEN" in got.stderr
    assert TOKEN not in got.stderr
    assert Recorder.seen == []
    assert (root / "stacks/agents/compose.yaml").read_text() == LIST_FORM


@pytest.mark.skipif(not (SEED.parent / "bootstrap/hgboot/new_repo.py").is_file(),
                    reason="the bootstrap is outside seed/")
def test_wire_without_a_forgehands_stack_fails_with_exit_1(tmp_path, forgejo_server):
    root = _root(tmp_path, ("web", "services:\n  app:\n    image: nginx:1.27\n"))
    got = cli(root, forgejo_server, "--yes")
    assert got.returncode == 1
    assert "none of the 1 stacks runs ForgeHands" in got.stderr and "web" in got.stderr
    assert Recorder.seen == []
