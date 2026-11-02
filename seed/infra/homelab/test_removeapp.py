"""Tests for remove-app: which apps are removable, and the two halves of a removal with a pull request
(pytest infra/homelab): git against a local bare origin, Komodo and Forgejo faked."""
import pathlib
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))
for sub in ("komodo", "infisical"):
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / sub))
import forgejo  # noqa: E402
import ops  # noqa: E402
import removeapp  # noqa: E402
import ui  # noqa: E402

INVENTORY = """stacks:
- id: demo
  path: stacks/demo
  komodo_stack: demo
  server: worker-1
  status: verified
  secrets: infisical
- id: other
  path: stacks/other
  komodo_stack: other
  server: worker-1
  status: verified
- id: homegraf
  path: stacks/homegraf
  komodo_stack: homegraf
  server: host-1
  status: verified
- id: git--komodo-backup
  path: stacks/git
  komodo_stack: komodo-backup
  server: nas
  status: verified
- id: git--nas
  path: stacks/git
  status: unmanaged
"""


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
    (work / "inventory/stacks.yaml").write_text(INVENTORY)
    (work / "inventory/homegraf.yaml").write_text("platform_stacks: [infisical, reverseProxy]\nplatform_prefixes: [\"git--\"]\n")
    (work / "stacks/demo").mkdir(parents=True)
    (work / "stacks/demo/compose.yaml").write_text("services:\n  demo:\n    image: x\n    volumes:\n      - /srv/docker/demo:/data\n")
    (work / "stacks/demo/ingress.yaml").write_text("services:\n- proxy:\n    domains: [demo.lab.example]\n")
    (work / "stacks/other").mkdir()
    (work / "stacks/other/compose.yaml").write_text("services: {}\n")
    sh(work, "git", "add", "-A")
    sh(work, "git", "commit", "-q", "-m", "init")
    sh(work, "git", "push", "-q", "origin", "main")
    for mod in (ops, removeapp):
        monkeypatch.setattr(mod, "ROOT", work)
    monkeypatch.setattr(ops, "NET_ENV", {})
    monkeypatch.setattr(ui, "AUTO", True)
    monkeypatch.setattr(ui, "ASSUME_YES", True)
    return work


class FakeForgejo:
    pulls: list = []

    def git_env(self, remote_url):
        return {}

    def find_pull(self, branch, base="main"):
        return next((p for p in self.pulls if p["branch"] == branch), None)

    def create_pull(self, branch, title, body, base="main"):
        pull = {"number": len(self.pulls) + 1, "html_url": f"https://git.example/pulls/{len(self.pulls) + 1}", "branch": branch, "body": body}
        self.pulls.append(pull)
        return pull


class FakeKomodo:
    def __init__(self, stacks=("demo",)):
        self.calls = []
        self.stacks = list(stacks)

    def read(self, name, body=None):
        assert name == "ListStacks"
        return [{"name": s} for s in self.stacks]

    def execute_and_poll(self, name, body, timeout=0):
        self.calls.append((name, body))
        return {"success": True}

    def write(self, name, body):
        self.calls.append((name, body))


@pytest.fixture
def fakes(monkeypatch):
    FakeForgejo.pulls = []
    monkeypatch.setattr(forgejo.Forgejo, "from_env", classmethod(lambda cls, url: FakeForgejo()))
    import rebuild
    monkeypatch.setattr(rebuild, "remove", lambda stack: True)
    monkeypatch.setenv("FORGEJO_TOKEN", "t")


def test_removable_lists_apps_with_what_removing_touches_and_never_the_platform(clone):
    apps = {a["id"]: a for a in removeapp.removable()}
    assert sorted(apps) == ["demo", "other"]       # homegraf, git--*: platform; git--nas: no Komodo stack anyway
    assert apps["demo"] == {"id": "demo", "server": "worker-1", "komodoStack": "demo", "secrets": True,
                            "domains": ["demo.lab.example"], "dataPaths": ["/srv/docker/demo"]}
    assert apps["other"]["secrets"] is False


@pytest.mark.parametrize("app_id", ["homegraf", "infisical", "reverseProxy", "git--komodo-backup", "git--nas"])
def test_the_platform_is_not_removed(clone, app_id):
    with pytest.raises(SystemExit, match="part of the platform"):
        removeapp.pick({"app_id": app_id})


def test_first_half_destroys_the_stack_and_opens_a_pull_request_that_removes_the_files(clone, fakes):
    komodo = FakeKomodo()
    assert removeapp.run(lambda: None, komodo, {"app_id": "demo", "pr": True}) is True
    assert [c[0] for c in komodo.calls] == ["DestroyStack", "DeleteStack"]
    assert ops.git("rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert (clone / "stacks/demo").exists()                        # main is untouched until the merge
    assert sh(clone, "git", "ls-tree", "-r", "--name-only", "origin/remove-app/demo", "stacks") == "stacks/other/compose.yaml"
    assert "- id: demo" not in sh(clone, "git", "show", "origin/remove-app/demo:inventory/stacks.yaml")
    assert "- id: other" in sh(clone, "git", "show", "origin/remove-app/demo:inventory/stacks.yaml")
    [pull] = FakeForgejo.pulls
    assert pull["branch"] == "remove-app/demo"
    assert "--server worker-1 --finish --yes" in pull["body"] and "demo.lab.example" in pull["body"] and "/srv/docker/demo" in pull["body"]


def test_first_half_without_yes_changes_nothing(clone, fakes, monkeypatch):
    monkeypatch.setattr(ui, "ASSUME_YES", False)
    komodo = FakeKomodo()
    with pytest.raises(ui.DryRun):
        removeapp.run(lambda: None, komodo, {"app_id": "demo", "pr": True})
    assert komodo.calls == [] and FakeForgejo.pulls == []
    assert not sh(clone, "git", "branch", "--list", "remove-app/demo")


def test_first_half_resumes_when_the_stack_is_already_gone(clone, fakes):
    komodo = FakeKomodo(stacks=())
    assert removeapp.run(lambda: None, komodo, {"app_id": "demo", "pr": True}) is True
    assert komodo.calls == [] and len(FakeForgejo.pulls) == 1


def test_second_half_refuses_while_main_still_has_the_app(clone, fakes):
    removeapp.run(lambda: None, FakeKomodo(), {"app_id": "demo", "pr": True})
    with pytest.raises(SystemExit, match="merge the pull request first"):
        removeapp.run(lambda: None, FakeKomodo(stacks=()), {"app_id": "demo", "finish": True})


def test_second_half_removes_routing_and_secrets_once_the_pull_request_is_merged(clone, fakes, monkeypatch):
    removeapp.run(lambda: None, FakeKomodo(), {"app_id": "demo", "pr": True})
    sh(clone, "git", "push", "-q", "origin", "origin/remove-app/demo:main")      # the merge
    calls = []
    monkeypatch.setattr(ops, "wait_for_update_all", lambda server, sha, komodo, pushed_now=True: calls.append(("wait", sha, pushed_now)))
    monkeypatch.setattr(ops, "ingress_cfg", lambda: {"controller": {"management_server": "host-1"}})
    monkeypatch.setattr(removeapp, "remove_routing", lambda app_id: calls.append(("routing", app_id)) or True)
    monkeypatch.setattr(removeapp, "drop_secrets", lambda infisical, entry: calls.append(("secrets", entry["id"], entry["server"])))
    komodo = FakeKomodo(stacks=())
    assert removeapp.run(lambda: None, komodo, {"app_id": "demo", "server": "worker-1", "finish": True}) is True
    assert komodo.calls == []                                        # nothing is destroyed twice
    merge = sh(clone, "git", "rev-parse", "origin/main")
    assert calls == [("wait", merge, False), ("routing", "demo"), ("secrets", "demo", "worker-1")]


def test_the_two_halves_are_not_combined(clone):
    with pytest.raises(SystemExit, match="two halves"):
        removeapp.run(lambda: None, FakeKomodo(), {"app_id": "demo", "pr": True, "finish": True})


def test_routing_waits_while_update_all_holds_ingressctl(monkeypatch):
    import run_ingressctl
    answers = iter([(1, "ERROR: Resource is busy"), (1, "ERROR: Resource is busy"), (0, "removed")])
    calls = []
    monkeypatch.setattr(run_ingressctl, "run", lambda argv, echo=True: calls.append(argv) or next(answers))
    monkeypatch.setattr(removeapp.time, "sleep", lambda s: calls.append(("sleep", s)))
    assert removeapp.remove_routing("demo") is True
    assert [c for c in calls if c[0] == "sleep"] == [("sleep", removeapp.BUSY_WAIT)] * 2
    assert len([c for c in calls if c[0] != "sleep"]) == 3


def test_routing_gives_up_after_the_retries_and_reports_the_failure(monkeypatch):
    import run_ingressctl
    monkeypatch.setattr(run_ingressctl, "run", lambda argv, echo=True: (1, "ERROR: Resource is busy"))
    monkeypatch.setattr(removeapp.time, "sleep", lambda s: None)
    assert removeapp.remove_routing("demo") is False


def test_routing_does_not_retry_other_errors(monkeypatch):
    import run_ingressctl
    calls = []
    monkeypatch.setattr(run_ingressctl, "run", lambda argv, echo=True: calls.append(argv) or (1, "BLOCKER: something else"))
    monkeypatch.setattr(removeapp.time, "sleep", lambda s: pytest.fail("must not wait"))
    assert removeapp.remove_routing("demo") is False and len(calls) == 1


def test_routing_is_skipped_when_the_installation_has_no_ingressctl_stack(monkeypatch):
    import run_ingressctl
    def run(argv, echo=True):
        raise RuntimeError('Komodo execute/RunStackService: HTTP 500: {"error":"Did not find any Stack matching ingressctl","trace":[]}')
    monkeypatch.setattr(run_ingressctl, "run", run)
    assert removeapp.remove_routing("demo") is True


def test_routing_reports_other_failures_to_run_ingressctl_without_a_traceback(monkeypatch):
    import run_ingressctl
    def run(argv, echo=True):
        raise RuntimeError("Komodo execute/RunStackService: HTTP 502: bad gateway")
    monkeypatch.setattr(run_ingressctl, "run", run)
    assert removeapp.remove_routing("demo") is False
