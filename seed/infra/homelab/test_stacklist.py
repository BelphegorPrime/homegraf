import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import stacklist  # noqa: E402

INGRESS = """apiVersion: homelab.ingress/v1
namespace: infrastructure
services:
- id: app
  stack: app
  lifecycle: present
  proxy: {domains: [app.example.lan], upstream: {scheme: http, server: Local, port: 8080}}
"""


def repo(tmp_path, listed=""):
    (tmp_path / "inventory").mkdir()
    (tmp_path / "inventory/stacks.yaml").write_text(f"stacks:\n{listed}" if listed else "stacks: []\n")
    (tmp_path / "inventory/namespaces.yaml").write_text("namespaces:\n  infrastructure:\n    paths: [stacks/]\n")
    return tmp_path


def folder(root, name, compose=True, ingress=True):
    d = root / "stacks" / name
    d.mkdir(parents=True)
    if compose:
        (d / "compose.yaml").write_text("services: {a: {image: x}}\n")
    if ingress:
        (d / "ingress.yaml").write_text(INGRESS)


def test_a_folder_with_compose_and_ingress_is_a_stack(tmp_path):
    root = repo(tmp_path)
    folder(root, "app")
    [s] = stacklist.load(root)
    assert (s["id"], s["path"], s["server"], s["status"], s["secrets"]) == ("app", "stacks/app", "Local", "derived", "infisical")


def test_folders_missing_one_of_the_files_are_not_stacks(tmp_path):
    root = repo(tmp_path)
    folder(root, "only-compose", ingress=False)
    folder(root, "only-ingress", compose=False)
    assert stacklist.load(root) == []


def test_an_entry_in_stacks_yaml_wins_and_comes_first(tmp_path):
    root = repo(tmp_path, "- {id: app, namespace: infrastructure, path: stacks/app, compose_files: [compose.yaml], status: verified}\n")
    folder(root, "app")
    folder(root, "other")
    assert [(s["id"], s["status"]) for s in stacklist.load(root)] == [("app", "verified"), ("other", "derived")]
