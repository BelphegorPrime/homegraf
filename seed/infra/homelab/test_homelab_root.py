"""The tools run from one place against the repository in another (HOMELAB_ROOT): what the homegraf image does with /repo."""
import json
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

SEED = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture
def tools_and_repo(tmp_path):
    code = tmp_path / "code"
    code.mkdir()
    shutil.copy(SEED / "homelab.py", code / "homelab.py")
    shutil.copytree(SEED / "infra", code / "infra", ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "tests", "test_*.py"))
    data = tmp_path / "data"
    (data / "inventory").mkdir(parents=True)
    (data / "inventory/stacks.yaml").write_text(
        "stacks:\n- id: other\n  path: stacks/other\n  compose_files:\n  - compose.yaml\n  komodo_stack: other\n  server: worker-1\n  status: verified\n")
    (data / "inventory/servers.yaml").write_text("servers:\n  worker-1: {address: 10.0.0.1, status: verified}\n")
    (data / "inventory/homegraf.yaml").write_text("domain: lab.example\nrepository: owner/infrastructure\n")
    (data / "stacks/other").mkdir(parents=True)
    (data / "stacks/other/compose.yaml").write_text("services:\n  a:\n    image: a:1\n    ports: ['8123:80']\n")
    return code, data


def run(code, data, *args):
    env = {k: v for k, v in os.environ.items() if k != "HOMELAB_ROOT"} | {"HOMELAB_ROOT": str(data), "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run([sys.executable, "-I", str(code / "homelab.py"), *args], cwd=code, env=env, capture_output=True, text=True, timeout=60)


def test_the_stacks_come_from_the_repository_not_from_the_tools(tools_and_repo):
    code, data = tools_and_repo
    got = run(code, data, "stacks", "--json")
    assert got.returncode == 0, got.stderr
    assert [s["id"] for s in json.loads(got.stdout)] == ["other"]


def test_templates_come_with_the_tools_and_the_domain_with_the_repository(tools_and_repo):
    code, data = tools_and_repo
    got = run(code, data, "new-app", "--app-id", "meals", "--server", "worker-1", "--template", "mealie", "--print-compose")
    assert got.returncode == 0, got.stderr
    assert "BASE_URL: https://meals.lab.example" in json.loads(got.stdout)["compose"]
    assert not (code / "stacks").exists()          # nothing was written next to the tools


def test_without_the_variable_the_repository_is_the_tools_own_folder(tools_and_repo):
    code, data = tools_and_repo
    shutil.copytree(data / "inventory", code / "inventory")
    shutil.copytree(data / "stacks", code / "stacks")
    env = {k: v for k, v in os.environ.items() if k != "HOMELAB_ROOT"}
    got = subprocess.run([sys.executable, "-I", str(code / "homelab.py"), "stacks", "--json"], cwd=code, env=env, capture_output=True, text=True, timeout=60)
    assert got.returncode == 0, got.stderr
    assert [s["id"] for s in json.loads(got.stdout)] == ["other"]
