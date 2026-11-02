"""Tests for stack operations and the Komodo stack config (pytest infra/homelab)."""
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import newapp  # noqa: E402,F401  (puts infra/komodo on the path)
import create_stack  # noqa: E402
import stackops  # noqa: E402


class FakeKomodo:
    def __init__(self, names):
        self.names = names

    def read(self, typ, params=None):
        if typ == "ListStacks":
            return [{"name": n} for n in self.names if n in params["query"]["names"]]
        return {"stdout": "line\n", "stderr": ""}


def test_logs_of_an_unknown_stack_is_an_error_not_a_traceback():
    with pytest.raises(SystemExit, match="unknown Komodo stack git--komodo-backup"):
        stackops.logs(FakeKomodo(["komodo-backup"]), "git--komodo-backup", 10, [], [])
    assert stackops.logs(FakeKomodo(["komodo-backup"]), "komodo-backup", 10, [], []) == 0


def test_builds_image_only_for_services_with_build(tmp_path):
    (tmp_path / "stacks/a").mkdir(parents=True)
    (tmp_path / "stacks/a/compose.yaml").write_text("services:\n  web:\n    build: ./app\n  db:\n    image: postgres:18\n")
    (tmp_path / "stacks/a/other.yaml").write_text("services:\n  web:\n    image: x:1\n")
    entry = {"path": "stacks/a", "compose_files": ["compose.yaml"]}
    assert create_stack.builds_image(entry, tmp_path) is True
    assert create_stack.builds_image({**entry, "compose_files": ["other.yaml"]}, tmp_path) is False
    assert create_stack.builds_image({**entry, "compose_files": ["missing.yaml"]}, tmp_path) is False
    assert create_stack.builds_image({"path": "stacks/a"}, tmp_path) is False


# --- the Build stage of update-all ---

import rebuild  # noqa: E402

STAGES = [
    {"name": "Stage 1", "enabled": True, "executions": [{"execution": {"type": "BatchPullRepo", "params": {"pattern": "*"}}, "enabled": True}]},
    {"name": "Stage 2", "enabled": True, "executions": [{"execution": {"type": "BatchDeployStackIfChanged", "params": {"pattern": "*"}}, "enabled": True}]},
    {"name": "Ingress", "enabled": True, "executions": [{"execution": {"type": "RunProcedure", "params": {"procedure": "x"}}, "enabled": True}]},
]


def _names(stages):
    return [s["name"] for s in stages]


def test_build_stage_comes_right_after_the_deploy_stage():
    out = rebuild.with_stack(STAGES, "homegraf")
    assert _names(out) == ["Stage 1", "Stage 2", "Build", "Ingress"]
    assert out[2]["executions"] == [{"execution": {"type": "DeployStack", "params": {"stack": "homegraf"}}, "enabled": True}]
    assert _names(STAGES) == ["Stage 1", "Stage 2", "Ingress"]            # the input is not changed


def test_adding_twice_changes_nothing_and_a_second_stack_joins_the_stage():
    once = rebuild.with_stack(STAGES, "homegraf")
    assert rebuild.with_stack(once, "homegraf") == once
    twice = rebuild.with_stack(once, "other")
    assert [e["execution"]["params"]["stack"] for e in twice[2]["executions"]] == ["homegraf", "other"]


def test_removing_the_last_stack_removes_the_stage_again():
    both = rebuild.with_stack(rebuild.with_stack(STAGES, "homegraf"), "other")
    assert [e["execution"]["params"]["stack"] for e in rebuild.without_stack(both, "homegraf")[2]["executions"]] == ["other"]
    assert rebuild.without_stack(rebuild.without_stack(both, "homegraf"), "other") == STAGES
    assert rebuild.without_stack(STAGES, "homegraf") == STAGES            # nothing to remove


def test_a_procedure_without_the_deploy_stage_gets_the_stage_at_the_end():
    odd = [STAGES[0], STAGES[2]]
    assert _names(rebuild.with_stack(odd, "homegraf")) == ["Stage 1", "Ingress", "Build"]


class FakeProcedures:
    """Komodo as seen by rebuild: it saves a stack by id, whatever name the caller used."""

    def __init__(self, stages):
        self.stages, self.writes = stages, []

    def read(self, typ, params=None):
        if typ == "ListStacks":
            return [{"name": "homegraf", "id": "abc123"}, {"name": "other", "id": "def456"}]
        assert typ == "GetProcedure"
        return {"_id": {"$oid": "p1"}, "config": {"stages": self.stages}}

    def write(self, typ, params):
        self.writes.append((typ, params))
        by_name = {"homegraf": "abc123", "other": "def456"}
        self.stages = [dict(s, executions=[
            {**e, "execution": {**e["execution"], "params": {**e["execution"]["params"],
             "stack": by_name.get(e["execution"]["params"].get("stack"), e["execution"]["params"].get("stack"))}}}
            if e["execution"]["type"] == "DeployStack" else e for e in s["executions"]]) for s in params["config"]["stages"]]


def test_add_and_remove_write_only_when_something_changes(monkeypatch):
    fake = FakeProcedures(STAGES)
    monkeypatch.setattr(rebuild, "k", fake)
    assert rebuild.add("homegraf") is True and rebuild.add("homegraf") is False
    assert rebuild.stacks() == ["homegraf"]                                 # listed by name, stored by id
    assert fake.stages[2]["executions"][0]["execution"]["params"]["stack"] == "abc123"
    assert len(fake.writes) == 1 and fake.writes[0][1]["id"] == "p1"
    assert rebuild.add("other") is True and rebuild.stacks() == ["homegraf", "other"]
    assert rebuild.remove("homegraf") is True and rebuild.remove("homegraf") is False   # found by name although stored by id
    assert rebuild.stacks() == ["other"]
    assert rebuild.remove("other") is True and rebuild.stacks() == [] and fake.stages == STAGES


def test_a_stack_is_known_by_name_and_by_id():
    stages = rebuild.with_stack(STAGES, "homegraf", {"homegraf", "abc123"})
    stages[2]["executions"][0]["execution"]["params"]["stack"] = "abc123"
    assert rebuild.with_stack(stages, "homegraf", {"homegraf", "abc123"}) == stages          # no second entry
    assert rebuild.without_stack(stages, "homegraf", {"homegraf", "abc123"}) == STAGES


def test_managed_lists_komodo_stacks_without_the_platform(tmp_path, monkeypatch):
    (tmp_path / "inventory").mkdir()
    (tmp_path / "inventory/stacks.yaml").write_text("""stacks:
- {id: paperless, server: host-1, komodo_stack: paperless, secrets: infisical}
- {id: aws, server: aws, komodo_stack: aws-floci}
- {id: infisical, server: host-1, komodo_stack: infisical}
- {id: reverseProxy, server: pi, komodo_stack: reverse-proxy, secrets: infisical}
- {id: homegraf, server: host-1, komodo_stack: homegraf, secrets: infisical}
- {id: git--komodo-backup, server: nas, komodo_stack: komodo-backup}
- {id: git--nas, status: unmanaged}
- {id: unmanaged-thing, status: unmanaged}
""")
    (tmp_path / "inventory/homegraf.yaml").write_text("platform_stacks: [infisical, reverseProxy]\nplatform_prefixes: [\"git--\"]\n")
    monkeypatch.setattr(stackops.ops, "ROOT", tmp_path)
    assert stackops.managed() == [
        {"id": "aws", "server": "aws", "komodoStack": "aws-floci", "secrets": False},
        {"id": "paperless", "server": "host-1", "komodoStack": "paperless", "secrets": True},
    ]
