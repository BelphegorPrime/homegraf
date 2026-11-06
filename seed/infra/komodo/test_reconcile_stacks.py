import pathlib
import sys
import types

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import reconcile_stacks as rs  # noqa: E402

ENTRY = {"id": "mlflow", "namespace": "infrastructure", "path": "stacks/mlflow", "compose_files": ["compose.yaml"],
         "komodo_stack": "mlflow", "server": "homework-ct", "status": "derived", "secrets": "infisical"}
RUN_DIR = "/data/komodo/repos/infrastructure-hw/stacks/mlflow"
VARIABLES = ["INFISICAL_API_URL", "INFISICAL_PROJECT_ID_HOMEWORK_CT", "INFISICAL_CLIENT_ID_HOMEWORK_CT",
             "INFISICAL_CLIENT_SECRET_HOMEWORK_CT"]


class Komodo:
    def __init__(self, variables=None):
        self.stacks = {}
        self.names = VARIABLES if variables is None else variables
        self.calls = []

    def read(self, typ, params=None):
        if typ == "ListStacks":
            return [{"name": n} for n in self.stacks]
        if typ == "ListVariables":
            return [{"name": n} for n in self.names]
        if typ == "GetStack":
            return {"config": self.stacks[params["stack"]]}

    def write(self, typ, params):
        self.calls.append((typ, params))
        if typ == "UpdateStack":
            self.stacks[params["id"]].update(params["config"])

    def execute_and_poll(self, typ, params, timeout=0):
        self.calls.append((typ, params))
        return {"success": True}


class Inf:
    def __init__(self):
        self.folder, self.have, self.made = False, set(), {}

    def project(self, slug):
        return "pid"

    def has_folder(self, *a):
        return self.folder

    def make_folder(self, *a):
        self.folder = True

    def keys(self, *a):
        return set(self.have)

    def make_secret(self, project, env, path, key, value):
        self.made[key] = value


def fixtures(tmp_path, komodo, build=False):
    d = tmp_path / "stacks/mlflow"
    d.mkdir(parents=True)
    (d / "compose.yaml").write_text(
        "services: {a: {image: x, environment: [P=${POSTGRES_PASSWORD}, Q=${OPT:-d}, R=$${ESC}]}}\n")

    def create_stack(entry, config):
        komodo.stacks[entry["komodo_stack"]] = dict(config)
        return []
    create = types.SimpleNamespace(stack_config=lambda e: {"run_directory": RUN_DIR}, create=create_stack,
                                   builds_image=lambda e, r: build)
    rebuild = types.SimpleNamespace(add=lambda s: True, stacks=lambda: [])
    return create, rebuild


def run(tmp_path, komodo, inf, apply=True, build=False):
    create, rebuild = fixtures(tmp_path, komodo, build) if not (tmp_path / "stacks/mlflow").exists() else (
        types.SimpleNamespace(stack_config=lambda e: {"run_directory": RUN_DIR},
                              create=lambda e, c: komodo.stacks.__setitem__(e["komodo_stack"], dict(c)) or [],
                              builds_image=lambda e, r: build),
        types.SimpleNamespace(add=lambda s: True, stacks=lambda: []))
    return rs.setup_stack(ENTRY, komodo, inf, create, rebuild, apply, tmp_path, say=lambda m: None)


def test_required_vars_skips_defaults_and_escapes(tmp_path):
    fixtures(tmp_path, Komodo())
    assert rs.required_vars(tmp_path, ENTRY) == ["POSTGRES_PASSWORD"]


def test_new_stack_gets_secrets_stack_wrapper_and_deploy(tmp_path):
    komodo, inf = Komodo(), Inf()
    assert run(tmp_path, komodo, inf, build=True) == "secrets, stack, wrapper, rebuild, deploy"
    assert len(inf.made["POSTGRES_PASSWORD"]) == 48
    assert "compose-wrap.sh /mlflow" in komodo.stacks["mlflow"]["compose_cmd_wrapper"]
    assert komodo.calls[-1][0] == "DeployStack"


def test_second_run_does_nothing(tmp_path):
    komodo, inf = Komodo(), Inf()
    run(tmp_path, komodo, inf)
    inf.have = set(inf.made)
    komodo.calls.clear()
    assert run(tmp_path, komodo, inf) == ""
    assert komodo.calls == []


def test_dry_run_changes_nothing(tmp_path):
    komodo, inf = Komodo(), Inf()
    assert run(tmp_path, komodo, inf, apply=False) == "secrets, stack, wrapper"
    assert not inf.made and not komodo.stacks and not komodo.calls


def test_missing_komodo_variables_fail_with_a_hint(tmp_path):
    with pytest.raises(RuntimeError, match="stack_secrets.py --apply"):
        run(tmp_path, Komodo(variables=[]), Inf())
