"""The `new-repo` command against a loopback Forgejo: the plan changes nothing, --yes creates and reads back.

The Forgejo side (bootstrap/hgboot/new_repo.py) has its own fixture; this test drives the real command line in a
subprocess and speaks to the same fake over HTTP, so the boundary under test is Forgejo's API, not a stub of the
command. A seed-only checkout has no bootstrap and skips, like test_templates.py does.
"""
import json
import os
import pathlib
import subprocess
import sys

import pytest

SEED = pathlib.Path(__file__).resolve().parents[2]
BOOTSTRAP = SEED.parent / "bootstrap"
if not (BOOTSTRAP / "hgboot" / "new_repo.py").is_file():
    pytest.skip("the bootstrap is outside seed/ (scripts/check-seed.sh copies seed/ alone)", allow_module_level=True)
sys.path.insert(0, str(BOOTSTRAP))
from hgboot.factory_repos import AGENT_EVENTS, LABELS  # noqa: E402
from tests.fakes import Server  # noqa: E402
from tests.test_new_repo import FakeNewRepoForgejo  # noqa: E402

TOKEN = "operator-token"
SECRET = "webhook-secret-value"
ENDPOINT = "http://forgehands:8080/webhooks/forgejo"
OWNER = "owner"
NAME = "repository"
REPO = f"{OWNER}/{NAME}"
AGENT = "openhands"
ORIGIN = "ssh://git@git.lab.example/owner/infrastructure.git"
CI = ".forgejo/workflows/ci.yaml"
FILES = {"AGENTS.md", "CONTRIBUTING.md", "README.md", ".gitignore", CI}
CLEAR = ("FORGEJO_TOKEN", "FORGEJO_URL", "FORGEHANDS_FORGEJO_WEBHOOK_URL", "FORGEHANDS_FORGEJO_WEBHOOK_SECRET",
         "STATE_DIR", "HOMELAB_ROOT", "HGBOOT_QUIET")


@pytest.fixture
def forgejo():
    fake = FakeNewRepoForgejo()
    fake.users[AGENT] = {}
    fake.users["reviewer"] = {}
    server = Server(fake)
    yield fake, server
    server.close()


def environment(tmp_path, server, token=TOKEN):
    env = {k: v for k, v in os.environ.items() if k not in CLEAR}
    env.update(HOMELAB_ROOT=str(tmp_path / "repo"), PYTHONDONTWRITEBYTECODE="1", STATE_DIR=str(tmp_path / "state"))
    if server is not None:
        env.update(FORGEJO_URL=server.url, FORGEHANDS_FORGEJO_WEBHOOK_URL=ENDPOINT,
                   FORGEHANDS_FORGEJO_WEBHOOK_SECRET=SECRET)
    if token is not None:
        env["FORGEJO_TOKEN"] = token
    return env


def cli(env, *args):
    return subprocess.run([sys.executable, "-B", str(SEED / "homelab.py"), "new-repo", "--name", NAME,
                           "--profile", "python", *args], cwd=SEED, env=env, capture_output=True, text=True, timeout=60)


def command(tmp_path, server, *args, token=TOKEN, owner=True, origin=ORIGIN):
    """Run `homelab.py new-repo ...` against the loopback Forgejo, from a repository whose origin names the owner."""
    repo = tmp_path / "repo"
    if not repo.exists():
        repo.mkdir()
        if origin:
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", origin], check=True)
    return cli(environment(tmp_path, server, token), *(["--owner", OWNER] if owner else []), *args)


def test_the_plan_changes_nothing_and_names_what_would_be_created(forgejo, tmp_path):
    fake, server = forgejo
    got = command(tmp_path, server)
    assert got.returncode == 0, got.stderr
    for text in (REPO, "private", AGENT, "python", ENDPOINT, "ai:ready", *FILES):
        assert text in got.stdout, text
    assert "rerun with --yes" in got.stdout
    assert not server.requests                       # not a single request: the plan is local
    assert fake.repos == {}


def test_plan_json_is_for_the_app_and_makes_no_request(forgejo, tmp_path):
    fake, server = forgejo
    got = command(tmp_path, server, "--json")
    assert got.returncode == 0, got.stderr
    plan = json.loads(got.stdout)
    assert plan["applied"] is False
    assert plan["repository"] == REPO and plan["visibility"] == "private"
    assert plan["agent_login"] == AGENT and plan["profile"] == "python"
    assert set(plan["files"]) == FILES
    assert set(plan["labels"]) == set(LABELS)
    assert plan["hook"] == {"url": ENDPOINT, "events": list(AGENT_EVENTS)}
    assert not server.requests


def test_yes_creates_the_repository_with_agent_access_labels_hook_and_first_commit(forgejo, tmp_path):
    fake, server = forgejo
    got = command(tmp_path, server, "--yes")
    assert got.returncode == 0, got.stderr + got.stdout
    assert "read back" in got.stdout
    repo = fake.repos[REPO]
    assert not repo["empty"]
    assert repo["private"] is True and repo["default_branch"] == "main"
    assert repo["collaborators"] == {AGENT: "write"}
    assert {label["name"] for label in repo["labels"]} == set(LABELS)
    assert len(repo["hooks"]) == 1
    hook = repo["hooks"][0]
    assert hook["type"] == "forgejo" and hook["active"] is True
    assert set(AGENT_EVENTS) <= set(hook["events"])
    assert hook["config"]["url"] == ENDPOINT and hook["config"]["secret"] == SECRET
    assert set(repo["files"]) == FILES
    assert b"# repository" in repo["files"]["README.md"]
    assert b"python -m pytest -q" in repo["files"]["AGENTS.md"]
    assert TOKEN not in got.stdout + got.stderr


def test_a_rerun_with_the_same_state_creates_nothing_again(forgejo, tmp_path):
    fake, server = forgejo
    first = command(tmp_path, server, "--yes")
    assert first.returncode == 0, first.stderr
    commits, hook = len(fake.commits), fake.repos[REPO]["hooks"][0]["id"]
    again = command(tmp_path, server, "--yes")
    assert again.returncode == 0, again.stderr
    assert len(fake.commits) == commits              # no second commit
    assert fake.repos[REPO]["hooks"][0]["id"] == hook  # the stored fingerprint keeps the hook
    assert "already there" in again.stdout and "no change" in again.stdout


def test_result_json_is_for_the_app(forgejo, tmp_path):
    fake, server = forgejo
    got = command(tmp_path, server, "--yes", "--json")
    assert got.returncode == 0, got.stderr
    result = json.loads(got.stdout)
    assert result["applied"] is True
    assert result["repository"] == REPO and result["repository_status"] == "created"
    assert result["collaborator"] == AGENT and result["collaborator_status"] == "added"
    assert set(result["labels_created"]) == set(LABELS) and result["labels_updated"] == []
    assert result["hook_status"] == "created" and set(result["hook_events"]) >= set(AGENT_EVENTS)
    assert result["commit"] == "created" and set(result["files"]) == FILES


def test_public_profile_and_custom_branch_and_agent(forgejo, tmp_path):
    fake, server = forgejo
    got = command(tmp_path, server, "--yes", "--public", "--branch", "develop", "--agent-login", "reviewer",
                  "--description", "a small service")
    assert got.returncode == 0, got.stderr
    repo = fake.repos[REPO]
    assert repo["private"] is False and repo["default_branch"] == "develop"
    assert repo["description"] == "a small service"
    assert repo["collaborators"] == {"reviewer": "write"}


def test_a_missing_token_fails_and_names_the_variable(forgejo, tmp_path):
    fake, server = forgejo
    got = command(tmp_path, server, "--yes", token=None)
    assert got.returncode == 1
    assert "FORGEJO_TOKEN" in got.stderr
    assert not server.requests
    assert TOKEN not in got.stderr


def test_shipped_profiles_are_the_only_ones(forgejo, tmp_path):
    fake, server = forgejo
    got = subprocess.run([sys.executable, "-B", str(SEED / "homelab.py"), "new-repo", "--name", NAME,
                          "--profile", "ruby", "--owner", OWNER],
                         cwd=SEED, env=environment(tmp_path, server), capture_output=True, text=True, timeout=60)
    assert got.returncode == 1
    assert "unknown profile 'ruby'" in got.stderr
    for profile in ("generic", "node", "python"):
        assert profile in got.stderr
    assert not server.requests


def test_an_existing_non_empty_repository_is_refused(forgejo, tmp_path):
    fake, server = forgejo
    fake.add_repo(OWNER, NAME, empty=False, files={"README.md": b"# another history\n"})
    got = command(tmp_path, server, "--yes")
    assert got.returncode == 1
    assert "refusing to touch its history" in got.stderr or "refusing to overwrite" in got.stderr
    assert fake.repos[REPO]["files"] == {"README.md": b"# another history\n"}


def test_the_owner_comes_from_origin_when_the_flag_is_missing(forgejo, tmp_path):
    fake, server = forgejo
    got = command(tmp_path, server, "--yes", owner=False)
    assert got.returncode == 0, got.stderr
    assert fake.creates == [REPO]


def test_without_origin_and_without_owner_the_command_says_what_to_pass(forgejo, tmp_path):
    fake, server = forgejo
    got = command(tmp_path, server, owner=False, origin=None)
    assert got.returncode == 1
    assert "--owner" in got.stderr and "origin" in got.stderr
    assert not server.requests


@pytest.mark.parametrize("name", ["FORGEHANDS_FORGEJO_WEBHOOK_URL", "FORGEHANDS_FORGEJO_WEBHOOK_SECRET"])
def test_a_missing_webhook_setting_fails_and_names_the_variable(forgejo, tmp_path, name):
    fake, server = forgejo
    env = environment(tmp_path, server)
    env.pop(name)
    got = subprocess.run([sys.executable, "-B", str(SEED / "homelab.py"), "new-repo", "--name", NAME,
                          "--profile", "python", "--owner", OWNER],
                         cwd=SEED, env=env, capture_output=True, text=True, timeout=60)
    assert got.returncode == 1
    assert name in got.stderr
    assert not server.requests


def test_a_failing_step_is_reported_without_the_token(forgejo, tmp_path):
    fake, server = forgejo
    fake.fail_once("POST", "/api/v1/user/repos", 500, f"boom {TOKEN}")
    got = command(tmp_path, server, "--yes")
    assert got.returncode == 1
    assert "Forgejo answered 500" in got.stderr
    assert TOKEN not in got.stdout + got.stderr and "[redacted]" in got.stderr
