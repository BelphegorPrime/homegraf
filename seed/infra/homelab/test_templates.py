"""Tests for the templates of new-app (pytest infra/homelab)."""
import pathlib
import sys

import pytest
import yaml

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import newapp  # noqa: E402
import templates  # noqa: E402
from test_newapp import repo  # noqa: E402,F401  (the fixture: a small repository)


def test_the_catalog_is_sound():
    found = templates.catalog()
    assert {"nginx", "httpd", "haproxy"} <= set(found)
    for t in found.values():
        service = yaml.safe_load(t.compose)["services"]["app"]
        assert ":" in service["image"] and not service["image"].endswith(":latest")
        d = newapp.template_defaults(t)
        assert d["image"] == service["image"] and d["containerPort"] == 80
        repo = [v["source"] for v in d["volumes"] if v["kind"] == "repo"]
        assert repo and templates.referenced(t, repo)                 # every template ships what its volumes mount
        for v in d["volumes"]:
            if v["kind"] == "repo":
                assert templates.referenced(t, [v["source"]]), f"{t.id}: nothing for {v['source']}"


def test_referenced_matches_files_and_folders():
    t = templates.catalog()["nginx"]
    assert templates.referenced(t, ["./default.conf"]) == ["default.conf"]
    assert templates.referenced(t, ["./html"]) == ["html/index.html"]
    assert templates.referenced(t, ["./html/"]) == ["html/index.html"]
    assert templates.referenced(t, ["./other", "/abs/html", "html"]) == []         # not repo paths, or not the template's


def test_a_template_gives_the_defaults_only_when_no_image_is_given():
    t = templates.catalog()["nginx"]
    got = newapp.with_template_defaults({"template": "nginx"}, t)
    assert got["image"].startswith("nginx:") and got["container_port"] == 80
    assert got["volume"] == ["repo:./default.conf:/etc/nginx/conf.d/default.conf:ro", "repo:./html:/usr/share/nginx/html:ro"]
    mine = {"image": "x:1", "volume": []}
    assert newapp.with_template_defaults(mine, t) is mine                        # a form always gives the image: no refill


def test_template_files_are_what_the_compose_file_mounts_and_edits_win(tmp_path):
    t = templates.catalog()["nginx"]
    compose = newapp.skeleton("demo", "nginx:1", 80, 8000, [], None, [], {}, None,
                              newapp.serviceopts.options_from({"volume": ["repo:./default.conf:/etc/nginx/conf.d/default.conf:ro"]}))
    assert list(newapp.template_files(compose, "demo", t, None)) == ["default.conf"]       # html/ is not mounted: not written
    (tmp_path / "default.conf").write_text("server { listen 81; }\n")
    assert newapp.template_files(compose, "demo", t, str(tmp_path)) == {"default.conf": "server { listen 81; }\n"}
    assert newapp.template_files(compose, "demo", None, None) == {}


def test_unknown_template_is_an_error():
    with pytest.raises(SystemExit, match="not one of"):
        newapp.get_template({"template": "tomcat"})
    assert newapp.get_template({}) is None


def test_write_files_copies_the_template_files_and_never_overwrites(repo):
    app = newapp.App("demo", "worker-1", "services: {}\n", True, template="nginx",
                     files={"default.conf": "from template\n", "html/index.html": "<p>hi</p>\n"})
    (repo / "stacks/demo").mkdir()
    (repo / "stacks/demo/default.conf").write_text("mine\n")
    newapp.write_files(app)
    assert (repo / "stacks/demo/default.conf").read_text() == "mine\n"
    assert (repo / "stacks/demo/html/index.html").read_text() == "<p>hi</p>\n"


def test_preview_returns_the_files_and_the_edited_text(repo, tmp_path, monkeypatch):
    monkeypatch.setattr(newapp.ui, "AUTO", True)
    (repo / "inventory/servers.yaml").write_text("servers:\n  worker-1: {address: 10.0.0.1, status: verified}\n")
    got = newapp.compose_preview({"app_id": "demo", "server": "worker-1", "template": "haproxy"})
    assert got["form"]["image"].startswith("haproxy:") and list(got["files"]) == ["haproxy.cfg"]
    assert "haproxy is running" in got["files"]["haproxy.cfg"]
    (tmp_path / "haproxy.cfg").write_text("edited\n")
    again = newapp.compose_preview({"app_id": "demo", "server": "worker-1", "template": "haproxy", "files_dir": str(tmp_path)})
    assert again["files"] == {"haproxy.cfg": "edited\n"}
    plain = newapp.compose_preview({"app_id": "demo", "server": "worker-1", "image": "x:1", "container_port": 80})
    assert plain["files"] == {}                                                 # no template, no files
    assert not (repo / "stacks/demo").exists()                                  # a preview writes nothing
