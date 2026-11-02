"""Tests für secret.py mit einem Fake für Infisical v0.167.2 (pytest infra/infisical)."""
import io
import json
import pathlib
import sys
import urllib.parse

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import secret  # noqa: E402

INVENTORY = """
stacks:
  - {id: paperless, path: stacks/paperless, compose_files: [compose.yaml], server: host-1, komodo_stack: paperless, secrets: infisical}
  - {id: plain, path: stacks/plain, compose_files: [compose.yaml], server: host-1, komodo_stack: plain}
"""
COMPOSE = "services:\n  db:\n    environment:\n      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}\n      OTHER: $OTHER_KEY\n"


class FakeInfisical:
    def __init__(self):
        self.secrets = {"POSTGRES_PASSWORD": "alt", "UNUSED": "x"}
        self.calls = []

    def call(self, method, path, body=None, auth=True):
        url = urllib.parse.urlparse(path)
        q = dict(urllib.parse.parse_qsl(url.query))
        self.calls.append((method, url.path))
        if url.path == "/api/v1/projects":
            return {"projects": [{"id": "p1", "slug": "stacks-host-1"}]}
        if url.path == "/api/v2/folders":
            return {"folders": [{"name": "paperless"}]}
        if url.path.startswith("/api/v2/folders/"):
            assert method == "DELETE" and body == {"projectId": "p1", "environment": "prod", "path": "/", "forceDelete": True}
            self.secrets.clear()
            return {}
        if url.path == "/api/v4/secrets":
            assert q["secretPath"] == "/paperless" and q["environment"] == "prod"
            return {"secrets": [{"secretKey": k} for k in self.secrets]}
        key = url.path.rsplit("/", 1)[1]
        if method == "GET":
            return {"secret": {"secretValue": self.secrets[key]}}
        assert body["projectId"] == "p1" and body["secretPath"] == "/paperless"
        if method == "POST":
            assert key not in self.secrets
            self.secrets[key] = body["secretValue"]
        elif method == "PATCH":
            assert key in self.secrets
            self.secrets[key] = body["secretValue"]
        elif method == "DELETE":
            del self.secrets[key]
        return {}


@pytest.fixture
def inf(tmp_path, monkeypatch):
    (tmp_path / "inventory").mkdir()
    (tmp_path / "inventory/stacks.yaml").write_text(INVENTORY)
    for d in ("paperless", "plain"):
        (tmp_path / "stacks" / d).mkdir(parents=True)
        (tmp_path / "stacks" / d / "compose.yaml").write_text(COMPOSE)
    monkeypatch.setattr(secret, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    return FakeInfisical()


def test_list_marks_unreferenced_keys(inf, capsys):
    secret.run(secret.parse_args(["list", "paperless"]), inf)
    out = capsys.readouterr().out.splitlines()
    assert out == ["POSTGRES_PASSWORD", "UNUSED   (not used in any compose file)"]


def test_list_json_says_which_keys_a_compose_file_uses(inf, capsys):
    secret.run(secret.parse_args(["list", "paperless", "--json"]), inf)
    assert json.loads(capsys.readouterr().out) == [{"key": "POSTGRES_PASSWORD", "used": True}, {"key": "UNUSED", "used": False}]


def test_get(inf, capsys):
    secret.run(secret.parse_args(["get", "paperless", "POSTGRES_PASSWORD"]), inf)
    assert capsys.readouterr().out.strip() == "alt"


def test_set_updates_from_stdin(inf, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("neu wert\n"))
    secret.run(secret.parse_args(["set", "paperless", "POSTGRES_PASSWORD"]), inf)
    assert inf.secrets["POSTGRES_PASSWORD"] == "neu wert"
    out = capsys.readouterr().out
    assert "stacks-host-1/prod/paperless/POSTGRES_PASSWORD changed" in out and "neu wert" not in out


def test_set_generate_creates_and_hints(inf, capsys):
    secret.run(secret.parse_args(["set", "paperless", "NEW_KEY", "--generate", "16"]), inf)
    assert len(inf.secrets["NEW_KEY"]) == 32
    out = capsys.readouterr().out
    assert "created" in out and inf.secrets["NEW_KEY"] not in out and "uses ${NEW_KEY}" in out


def test_set_generate_show(inf, capsys):
    secret.run(secret.parse_args(["set", "paperless", "OTHER_KEY", "--generate", "--show"]), inf)
    out = capsys.readouterr().out
    assert inf.secrets["OTHER_KEY"] in out and len(inf.secrets["OTHER_KEY"]) == 64
    assert "Note" not in out  # $OTHER_KEY without braces counts as a reference


def test_set_rejects_empty_value(inf):
    with pytest.raises(SystemExit, match="empty value"):
        secret.run(secret.parse_args(["set", "paperless", "POSTGRES_PASSWORD"]), inf)


def test_delete_refuses_referenced_key(inf):
    with pytest.raises(SystemExit, match="still uses \\$\\{POSTGRES_PASSWORD\\}"):
        secret.run(secret.parse_args(["delete", "paperless", "POSTGRES_PASSWORD", "--yes"]), inf)
    assert "POSTGRES_PASSWORD" in inf.secrets


def test_delete_unreferenced_with_yes(inf):
    secret.run(secret.parse_args(["delete", "paperless", "UNUSED", "--yes"]), inf)
    assert "UNUSED" not in inf.secrets


def test_delete_asks_without_yes(inf, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: "n")
    assert secret.run(secret.parse_args(["delete", "paperless", "UNUSED"]), inf) == 1
    assert "UNUSED" in inf.secrets


def test_stack_without_infisical_and_bad_key(inf):
    with pytest.raises(SystemExit, match="has no `secrets: infisical`"):
        secret.run(secret.parse_args(["list", "plain"]), inf)
    with pytest.raises(SystemExit, match="invalid key"):
        secret.run(secret.parse_args(["get", "paperless", "../x"]), inf)
    assert inf.calls == []


def test_drop_deletes_non_empty_folder_with_force(inf):
    folder = secret.Folder(inf, secret.find_stack("paperless"))
    folder.drop()
    assert inf.calls[-1] == ("DELETE", "/api/v2/folders/paperless") and inf.secrets == {} and not folder.exists
