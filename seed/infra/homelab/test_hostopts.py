"""Tests for force-https and hosts (pytest infra/homelab): git against a local bare origin, Forgejo faked."""
import pathlib
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))
for sub in ("komodo", "infisical"):
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / sub))
import forgejo  # noqa: E402
import hostopts  # noqa: E402
import ops  # noqa: E402
import ui  # noqa: E402

EXTERNAL = """# Ingress for services without a stack. This comment stays.
apiVersion: homelab.ingress/v1
services:
- id: nas-ui
  stack: external
  proxy:
    domains: [nas.lab.example]
    websockets: true
    force_https: false
- id: media                    # own container
  stack: external
  proxy:
    domains: [media.lab.example, media2.lab.example]
    upstream: {scheme: http, host: 10.0.0.6, port: 8021}
    force_https: false
- id: vm
  stack: external
  proxy:
    domains: [vm.lab.example]
    upstream: {scheme: https, host: 10.0.0.4, port: 8006}
    force_https: true
"""
DEMO = """services:
- id: demo
  stack: demo
  proxy:
    domains: [demo.lab.example]
    websockets: false
    force_https: true
"""
NO_LINE = "services:\n- id: bare\n  stack: bare\n  proxy:\n    domains: [bare.lab.example]\n"


def sh(cwd, *args):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def clone(tmp_path, monkeypatch):
    origin, work = tmp_path / "origin.git", tmp_path / "work"
    sh(tmp_path, "git", "init", "-q", "--bare", "-b", "main", str(origin))
    sh(tmp_path, "git", "clone", "-q", str(origin), str(work))
    for k, v in (("user.name", "t"), ("user.email", "t@example.org")):
        sh(work, "git", "config", k, v)
    (work / "external").mkdir()
    (work / "external/ingress.yaml").write_text(EXTERNAL)
    (work / "stacks/demo").mkdir(parents=True)
    (work / "stacks/demo/ingress.yaml").write_text(DEMO)
    (work / "stacks/bare").mkdir()
    (work / "stacks/bare/ingress.yaml").write_text(NO_LINE)
    (work / "stacks/plain").mkdir()
    (work / "stacks/plain/compose.yaml").write_text("services: {}\n")
    sh(work, "git", "add", "-A")
    sh(work, "git", "commit", "-q", "-m", "init")
    sh(work, "git", "push", "-q", "origin", "main")
    for mod in (ops, hostopts):
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


@pytest.fixture
def fakes(monkeypatch):
    FakeForgejo.pulls = []
    monkeypatch.setattr(forgejo.Forgejo, "from_env", classmethod(lambda cls, url: FakeForgejo()))
    monkeypatch.setenv("FORGEJO_TOKEN", "t")


def test_hosts_lists_every_managed_service_with_its_setting(clone):
    rows = {h["id"]: h for h in hostopts.hosts()}
    assert sorted(rows) == ["bare", "demo", "media", "nas-ui", "vm"]
    assert rows["media"] == {"id": "media", "stack": "external", "file": "external/ingress.yaml",
                             "domains": ["media.lab.example", "media2.lab.example"], "forceHttps": False}
    assert rows["demo"]["file"] == "stacks/demo/ingress.yaml" and rows["demo"]["forceHttps"] is True
    assert rows["bare"]["forceHttps"] is True        # without the line the default (on) applies


def test_set_force_https_changes_one_service_and_keeps_the_rest_of_the_text():
    out = hostopts.set_force_https(EXTERNAL, "media", True)
    assert out == EXTERNAL.replace("10.0.0.6, port: 8021}\n    force_https: false", "10.0.0.6, port: 8021}\n    force_https: true")
    assert "# own container" in out and "This comment stays." in out
    assert hostopts.set_force_https(EXTERNAL, "vm", False).count("force_https: false") == 3


def test_set_force_https_handles_the_inline_form_and_refuses_what_it_cannot_change():
    inline = "services:\n- id: a\n  proxy:\n    domains: [a.x]\n    websockets: true, force_https: false}\n- id: b\n  proxy: {domains: [b.x], force_https: true}\n"
    assert "force_https: true}" in hostopts.set_force_https(inline, "a", True)
    assert hostopts.set_force_https(inline, "b", False).splitlines()[-1].endswith("force_https: false}")
    with pytest.raises(SystemExit, match="not found"):
        hostopts.set_force_https(EXTERNAL, "nope", True)
    with pytest.raises(SystemExit, match="no force_https line"):
        hostopts.set_force_https(NO_LINE, "bare", False)


def test_an_unknown_domain_is_refused(clone):
    with pytest.raises(SystemExit, match="not a host ingressctl manages"):
        hostopts.run({"domain": "nope.lab.example", "state": "on"})


def test_nothing_to_do_when_the_setting_is_already_right(clone, fakes):
    assert hostopts.run({"domain": "vm.lab.example", "state": "on", "pr": True}) is True
    assert FakeForgejo.pulls == [] and not sh(clone, "git", "branch", "--list", "force-https/*")


def test_pull_request_changes_only_that_service_on_a_branch(clone, fakes):
    assert hostopts.run({"domain": "media2.lab.example", "state": "on", "pr": True}) is True
    assert ops.git("rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert (clone / "external/ingress.yaml").read_text() == EXTERNAL            # main is untouched until the merge
    changed = sh(clone, "git", "show", "origin/force-https/media-on:external/ingress.yaml")
    assert changed == hostopts.set_force_https(EXTERNAL, "media", True).strip()      # sh() strips the last newline
    assert sh(clone, "git", "diff", "--name-only", "origin/main", "origin/force-https/media-on") == "external/ingress.yaml"
    [pull] = FakeForgejo.pulls
    assert pull["branch"] == "force-https/media-on"
    assert "media.lab.example, media2.lab.example" in pull["body"] and "root certificate" in pull["body"]


def test_without_yes_nothing_changes(clone, fakes, monkeypatch):
    monkeypatch.setattr(ui, "ASSUME_YES", False)
    with pytest.raises(ui.DryRun):
        hostopts.run({"domain": "media.lab.example", "state": "on", "pr": True})
    assert FakeForgejo.pulls == [] and not sh(clone, "git", "branch", "--list", "force-https/*")
    assert (clone / "external/ingress.yaml").read_text() == EXTERNAL


def test_without_pr_it_pushes_main(clone, fakes):
    assert hostopts.run({"domain": "demo.lab.example", "state": "off"}) is True
    sh(clone, "git", "fetch", "-q", "origin")
    assert "force_https: false" in sh(clone, "git", "show", "origin/main:stacks/demo/ingress.yaml")
    assert sh(clone, "git", "log", "-1", "--format=%s", "origin/main") == "chore(demo): force https off for demo.lab.example"
