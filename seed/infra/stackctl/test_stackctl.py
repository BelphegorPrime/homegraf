"""Tests für die Änderungserkennung (pytest infra/stackctl)."""
import json
import pathlib
import subprocess
import sys
import textwrap

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import stackctl  # noqa: E402

STACKS = """
stacks:
  - {id: nas/paperless, namespace: nas-compose, path: stacks/nas/paperless, compose_files: [compose.yaml], server: box-a, komodo_stack: paperless, project_name: paperless, status: verified}
  - {id: nas/jellyfin, namespace: nas-compose, path: stacks/nas/jellyfin, compose_files: [compose.yaml], server: box-a, komodo_stack: jellyfin, project_name: jellyfin, status: verified}
  - {id: nas/jellyfin--ct, namespace: nas-compose, path: stacks/nas/jellyfin, compose_files: [compose.ct.yaml], server: box-e, komodo_stack: box-e, project_name: jellyfin, status: verified}
  - {id: ai/searxng, namespace: ai-sandbox, path: stacks/ai/searxng, compose_files: [compose.yaml], server: box-c, komodo_stack: searxng, project_name: searxng, status: verified, extra_files: [shared/searxng.env]}
"""


class Repo:
    def __init__(self, root: pathlib.Path):
        self.root = root
        files = {
            "inventory/stacks.yaml": STACKS,
            "inventory/servers.yaml": "servers:\n  box-a: {address: 10.0.0.1, status: verified}\n"
                                      "  box-e: {address: 10.0.0.2, status: verified}\n"
                                      "  box-c: {address: 10.0.0.3, status: verified}\n",
            "inventory/namespaces.yaml": "namespaces:\n  nas-compose: {paths: [stacks/nas/]}\n  ai-sandbox: {paths: [stacks/ai/]}\n",
            "inventory/paths.yaml": textwrap.dedent("""
                no_deploy: ["docs/**", "**/*.md", "infra/openclaw/**", "inventory/paths.yaml"]
                controller: ["infra/ingress-controller/**"]
                shared:
                  "shared/common/**": [nas/paperless, ai/searxng]
            """),
            "stacks/nas/paperless/compose.yaml": "services: {a: {image: x}}\n",
            "stacks/nas/jellyfin/compose.yaml": "services: {a: {image: x}}\n",
            "stacks/nas/jellyfin/compose.ct.yaml": "services: {a: {image: x}}\n",
            "stacks/nas/jellyfin/config.xml": "<x/>\n",
            "stacks/ai/searxng/compose.yaml": "services: {a: {image: x}}\n",
            "shared/searxng.env": "A=1\n",
            "shared/common/x.conf": "x\n",
            "docs/README.md": "x\n",
        }
        for rel, content in files.items():
            self.write(rel, content)
        self.git("init", "-q", "-b", "main")
        self.base = self.commit()

    def write(self, rel, content):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), "-c", "user.email=t@t", "-c", "user.name=t", *args],
                              check=True, capture_output=True, text=True).stdout.strip()

    def commit(self):
        self.git("add", "-A")
        self.git("commit", "-qm", "c", "--allow-empty")
        return self.git("rev-parse", "HEAD")

    def affected(self, head, **kw):
        res = stackctl.affected(self.root, head, **kw)
        return {s["id"]: s for s in res["stacks"]}, res


@pytest.fixture
def repo(tmp_path):
    return Repo(tmp_path)


def test_validate_ok(repo):
    errors, warnings = stackctl.validate(repo.root, strict=True)
    assert errors == []


def test_validate_secrets(repo):
    stacks = (repo.root / "inventory/stacks.yaml").read_text()
    repo.write("inventory/stacks.yaml", stacks.replace("status: verified, extra_files", "status: verified, secrets: infisical, extra_files"))
    assert stackctl.validate(repo.root, strict=True)[0] == []
    repo.write("inventory/stacks.yaml", stacks.replace("status: verified, extra_files", "status: verified, secrets: sops, extra_files"))
    assert any("secrets muss" in e for e in stackctl.validate(repo.root)[0])
    repo.write("inventory/stacks.yaml", stacks.replace("komodo_stack: searxng,", "secrets: infisical,"))
    assert any("secrets braucht" in e for e in stackctl.validate(repo.root)[0])


def test_validate_job(repo):
    stacks = (repo.root / "inventory/stacks.yaml").read_text()
    repo.write("inventory/stacks.yaml", stacks.replace("status: verified, extra_files", "status: verified, job: true, extra_files"))
    assert stackctl.validate(repo.root, strict=True)[0] == []
    repo.write("inventory/stacks.yaml", stacks.replace("status: verified, extra_files", "status: verified, job: yes please, extra_files"))
    assert any("job muss" in e for e in stackctl.validate(repo.root)[0])
    repo.write("inventory/stacks.yaml", stacks.replace("komodo_stack: searxng,", "job: true,"))
    assert any("job braucht" in e for e in stackctl.validate(repo.root)[0])


def test_only_changed_stack_is_affected(repo):
    repo.write("stacks/nas/paperless/compose.yaml", "services: {a: {image: y}}\n")
    stacks, res = repo.affected(repo.commit(), base=repo.base)
    assert set(stacks) == {"nas/paperless"} and stacks["nas/paperless"]["app"]
    assert not res["unclassified"]


def test_variant_compose_only_affects_its_variant(repo):
    repo.write("stacks/nas/jellyfin/compose.ct.yaml", "services: {a: {image: z}}\n")
    stacks, _ = repo.affected(repo.commit(), base=repo.base)
    assert set(stacks) == {"nas/jellyfin--ct"}


def test_shared_file_in_stack_dir_affects_all_variants(repo):
    repo.write("stacks/nas/jellyfin/config.xml", "<y/>\n")
    stacks, _ = repo.affected(repo.commit(), base=repo.base)
    assert set(stacks) == {"nas/jellyfin", "nas/jellyfin--ct"}


def test_shared_config_detects_all_consumers(repo):
    repo.write("shared/common/x.conf", "y\n")
    stacks, _ = repo.affected(repo.commit(), base=repo.base)
    assert set(stacks) == {"nas/paperless", "ai/searxng"}
    repo.write("shared/searxng.env", "A=2\n")
    stacks, _ = repo.affected(repo.commit(), base=repo.base)
    assert "ai/searxng" in stacks


def test_docs_only_no_deploy(repo):
    repo.write("docs/README.md", "changed\n")
    repo.write("stacks/nas/paperless/NOTES.md", "x\n")
    stacks, res = repo.affected(repo.commit(), base=repo.base)
    assert stacks == {} and not res["controller"] and not res["unclassified"]


@pytest.mark.parametrize("path", [
    "infra/openclaw/organization/teams.yaml",
    "infra/openclaw/organization/docs/README.md",
    "infra/openclaw/organization/skills/example/SKILL.md",
])
def test_openclaw_only_no_deploy(repo, path):
    repo.write(path, "updated\n")
    stacks, res = repo.affected(repo.commit(), base=repo.base)
    assert stacks == {} and not res["unclassified"]

def test_docker_compose_change_affects_stack(repo):
    repo.write("stacks/nas/paperless/compose.yaml", "services: {a: {image: changed}}\n")
    stacks, res = repo.affected(repo.commit(), base=repo.base)
    assert set(stacks) == {"nas/paperless"} and stacks["nas/paperless"]["app"]
    assert not res["unclassified"]

def test_mixed_docker_and_openclaw_change_keeps_docker_hit(repo):
    repo.write("stacks/nas/paperless/compose.yaml", "services: {a: {image: changed}}\n")
    repo.write("infra/openclaw/organization/teams.yaml", "updated\n")
    stacks, res = repo.affected(repo.commit(), base=repo.base)
    assert set(stacks) == {"nas/paperless"} and stacks["nas/paperless"]["app"]
    assert not res["unclassified"]

def test_paths_inventory_change_is_no_deploy(repo):
    text = (repo.root / "inventory/paths.yaml").read_text()
    repo.write("inventory/paths.yaml", text + "# updated\n")
    stacks, res = repo.affected(repo.commit(), base=repo.base)
    assert stacks == {} and not res["unclassified"]

def test_unknown_path_remains_unclassified(repo):
    repo.write("mystery/new-file.dat", "updated\n")
    stacks, res = repo.affected(repo.commit(), base=repo.base)
    assert stacks == {} and res["unclassified"] == ["mystery/new-file.dat"]

def test_ingress_only_change_does_not_redeploy_app(repo):
    repo.write("stacks/nas/paperless/ingress.yaml", "x: 1\n")
    stacks, _ = repo.affected(repo.commit(), base=repo.base)
    assert stacks["nas/paperless"]["ingress"] and not stacks["nas/paperless"]["app"]


def test_controller_change_is_flagged_without_app_redeploy(repo):
    repo.write("infra/ingress-controller/x.py", "x\n")
    stacks, res = repo.affected(repo.commit(), base=repo.base)
    assert res["controller"] and stacks == {}


def test_previously_failed_stack_still_detected(repo):
    repo.write("stacks/nas/paperless/compose.yaml", "services: {a: {image: y}}\n")
    c1 = repo.commit()  # paperless-Deploy scheitert, ai/searxng etc. sind auf c1
    repo.write("stacks/ai/searxng/compose.yaml", "services: {a: {image: y}}\n")
    c2 = repo.commit()
    deployed = {"nas/paperless": repo.base, "nas/jellyfin": c1, "nas/jellyfin--ct": c1, "ai/searxng": c1}
    stacks, _ = repo.affected(c2, deployed=deployed)
    assert set(stacks) == {"nas/paperless", "ai/searxng"}


def test_unknown_baseline_is_conservative(repo):
    head = repo.commit()
    stacks, _ = repo.affected(head, deployed={"nas/paperless": None})
    assert stacks["nas/paperless"]["app"]  # andere Stacks fehlen in der Map -> ebenfalls konservativ
    assert set(stacks) == {"nas/paperless", "nas/jellyfin", "nas/jellyfin--ct", "ai/searxng"}


def test_rename_detects_old_and_new_path(repo):
    repo.git("mv", "stacks/nas/jellyfin/config.xml", "stacks/nas/paperless/config.xml")
    stacks, _ = repo.affected(repo.commit(), base=repo.base)
    assert {"nas/jellyfin", "nas/jellyfin--ct", "nas/paperless"} <= set(stacks)


def test_unclassified_path_is_reported(repo):
    repo.write("random/file.txt", "x\n")
    _, res = repo.affected(repo.commit(), base=repo.base)
    assert res["unclassified"] == ["random/file.txt"]


def test_inventory_server_change_marks_ingress_only(repo):
    text = (repo.root / "inventory/servers.yaml").read_text().replace("10.0.0.3", "10.0.0.33")
    repo.write("inventory/servers.yaml", text)
    stacks, _ = repo.affected(repo.commit(), base=repo.base)
    assert set(stacks) == {"ai/searxng"} and stacks["ai/searxng"]["ingress"] and not stacks["ai/searxng"]["app"]


def test_non_ancestor_baseline_requires_confirmation(repo):
    repo.git("checkout", "-q", "-b", "side")
    repo.write("stacks/nas/paperless/compose.yaml", "services: {a: {image: side}}\n")
    side = repo.commit()
    repo.git("checkout", "-q", "main")
    head = repo.commit()
    stacks, _ = repo.affected(head, deployed={"nas/paperless": side, "nas/jellyfin": head,
                                             "nas/jellyfin--ct": head, "ai/searxng": head})
    assert stacks["nas/paperless"]["requires_confirmation"]


def test_cli_exit_code_for_unclassified(repo, capsys):
    repo.write("random/file.txt", "x\n")
    head = repo.commit()
    assert stackctl.main(["affected", "--root", str(repo.root), "--head", head, "--base", repo.base]) == 3
    assert json.loads(capsys.readouterr().out)["unclassified"] == ["random/file.txt"]


def test_global_ingress_config_triggers_ingress_for_manifest_stacks(repo):
    repo.write("stacks/nas/paperless/ingress.yaml", "x: 1\n")
    base = repo.commit()
    repo.write("inventory/ingress.yaml", "npm_instances: {}\n")
    stacks, res = repo.affected(repo.commit(), base=base)
    assert set(stacks) == {"nas/paperless"} and stacks["nas/paperless"]["ingress"] and not stacks["nas/paperless"]["app"]
    assert not res["unclassified"]
