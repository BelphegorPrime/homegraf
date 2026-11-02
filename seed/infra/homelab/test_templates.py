"""Tests for the templates of new-app (pytest infra/homelab)."""
import dataclasses
import pathlib
import re
import sys

import pytest
import yaml

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import addons  # noqa: E402
import newapp  # noqa: E402
import templates  # noqa: E402
from test_newapp import agent, repo  # noqa: E402,F401  (the fixtures: a small repository, and one for agent mode)


def test_the_catalog_is_sound():
    found = templates.catalog()
    assert {"nginx", "httpd", "haproxy", "mealie", "paperless", "immich"} <= set(found)
    assert {found[n].category for n in ("nginx", "httpd", "haproxy")} == {"web server"}
    blocks = addons.catalog()
    for t in found.values():
        service = yaml.safe_load(t.compose)["services"]["app"]
        assert ":" in service["image"] and not service["image"].endswith(":latest"), t.id
        d = newapp.template_defaults(t)
        assert d["image"] == service["image"] and d["containerPort"], t.id
        assert t.category in templates.CATEGORIES and t.description and t.label, t.id
        assert set(t.addons) <= set(blocks), f"{t.id}: unknown building block"
        for v in d["volumes"]:
            if v["kind"] == "repo":
                assert templates.referenced(t, [v["source"]]), f"{t.id}: nothing for {v['source']}"
        for name, text in t.sidecars.items():
            image = yaml.safe_load(text)[name]["image"]
            assert ":" in image.split("/")[-1] and not image.endswith(":latest"), f"{t.id}/{name}"
        targets = {v["target"] for v in d["volumes"]}
        assert set(t.host_paths) <= targets, f"{t.id}: host_paths names a path without volume"
        text = t.compose + "".join(t.files.values())
        assert not re.search(r"192\.168\.|\.codes\b|/mnt/|/home/", text), f"{t.id}: names this homelab"


def test_every_template_renders_a_valid_compose_file_with_its_secrets_generated(repo, monkeypatch):
    monkeypatch.setattr(newapp.ui, "AUTO", True)
    (repo / "inventory/servers.yaml").write_text("servers:\n  worker-1: {address: 10.0.0.1, status: verified}\n")
    for t in templates.catalog().values():
        got = newapp.compose_preview({"app_id": "demo", "server": "worker-1", "template": t.id})
        doc = yaml.safe_load(got["compose"])
        names = set(doc["services"])
        assert "demo" in names and set(t.sidecars) <= names and set(t.addons) <= names, t.id
        assert set(got["secrets"]) >= t.secrets(), t.id
        assert "__" not in got["compose"], f"{t.id}: placeholder left in the compose file"
        for service in doc["services"].values():
            for dep in (service.get("depends_on") or {}):
                assert dep in names, f"{t.id}: depends on {dep}"


def test_placeholders_are_filled_from_the_domain(repo, monkeypatch):
    monkeypatch.setattr(newapp.ui, "AUTO", True)
    (repo / "inventory/servers.yaml").write_text("servers:\n  worker-1: {address: 10.0.0.1, status: verified}\n")
    got = newapp.compose_preview({"app_id": "demo", "server": "worker-1", "template": "mealie", "domain": "meals.lab.example"})
    assert "BASE_URL: https://meals.lab.example" in got["compose"]
    plain = newapp.compose_preview({"app_id": "demo", "server": "worker-1", "template": "mealie", "routing": False, "host_port": 9100})
    assert "BASE_URL: http://10.0.0.1:9100" in plain["compose"]


def test_a_template_brings_its_blocks_sidecars_and_the_keys_the_form_does_not_model(repo, monkeypatch):
    monkeypatch.setattr(newapp.ui, "AUTO", True)
    (repo / "inventory/servers.yaml").write_text("servers:\n  worker-1: {address: 10.0.0.1, status: verified}\n")
    doc = yaml.safe_load(newapp.compose_preview({"app_id": "docs", "server": "worker-1", "template": "paperless"})["compose"])
    app = doc["services"]["docs"]
    assert set(doc["services"]) == {"docs", "gotenberg", "tika", "postgres", "redis"}
    assert app["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert app["depends_on"]["tika"]["condition"] == "service_started"                 # no healthcheck to wait for
    assert "POSTGRES_URL" not in app["environment"] and app["environment"]["PAPERLESS_DBHOST"] == "postgres"
    assert doc["services"]["gotenberg"]["restart"] == "unless-stopped"
    extras = yaml.safe_load(newapp.compose_preview({"app_id": "music", "server": "worker-1", "template": "navidrome"})["compose"])
    assert extras["services"]["music"]["user"] == "0:0"
    sparky = yaml.safe_load(newapp.compose_preview({"app_id": "fit", "server": "worker-1", "template": "sparkyfitness"})["compose"])
    assert sparky["services"]["fit"]["depends_on"]["server"]["condition"] == "service_healthy"
    assert sparky["services"]["server"]["volumes"][0].startswith("/srv/docker/fit/")


def test_the_form_can_still_leave_out_a_block_the_template_suggests(repo, monkeypatch):
    monkeypatch.setattr(newapp.ui, "AUTO", True)
    (repo / "inventory/servers.yaml").write_text("servers:\n  worker-1: {address: 10.0.0.1, status: verified}\n")
    got = newapp.compose_preview({"app_id": "meals", "server": "worker-1", "template": "mealie", "addon": []})
    assert set(yaml.safe_load(got["compose"])["services"]) == {"meals"}


def test_routing_defaults_come_from_the_template_unless_given():
    t = templates.catalog()["immich"]
    got = newapp.routing_defaults({}, t)
    assert got["websockets"] is True and got["health_path"] == "/api/server/ping" and got["statuses"] == [200]
    mine = newapp.routing_defaults({"websockets": False, "health_path": "/x", "statuses": [204]}, t)
    assert mine["websockets"] is False and mine["health_path"] == "/x" and mine["statuses"] == [204]


def test_a_stand_in_folder_is_reported_until_it_is_changed():
    t = templates.catalog()["navidrome"]
    compose = newapp.skeleton("music", t_image := "x:1", 4533, 4533, [], None, [], {}, None,
                              newapp.serviceopts.options_from({"volume": ["bind:/srv/media/music:/music:ro"]}), t)
    assert newapp.unchosen_paths(compose, "music", t) == ["/music"]
    chosen = compose.replace("/srv/media/music", "/tank/songs")
    assert newapp.unchosen_paths(chosen, "music", t) == []


def test_split_services_keeps_each_text_as_written():
    text = "x-template: {}\nservices:\n  app:\n    image: a:1\n  # about b\n  b:\n    image: b:1\n    environment:\n      K: v\nvolumes: {}\n"
    assert templates.service_blocks(text) == {"app": "  app:\n    image: a:1\n  # about b\n", "b": "  b:\n    image: b:1\n    environment:\n      K: v\n"}


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



# --- several templates in one compose file ---


def _several(repo, monkeypatch, ids, **extra):
    monkeypatch.setattr(newapp.ui, "AUTO", True)
    (repo / "inventory/servers.yaml").write_text("servers:\n  worker-1: {address: 10.0.0.1, status: verified}\n")
    return newapp.compose_preview({"app_id": "demo", "server": "worker-1", "template": ids, **extra})


def _text_values(doc):
    for name, svc in doc["services"].items():
        for k, v in (svc.get("environment") or {}).items():
            yield name, k, str(v)


def test_the_first_template_is_the_app_and_the_others_are_services_of_their_own(repo, monkeypatch):
    got = _several(repo, monkeypatch, ["mealie", "linkwarden", "paperless"])
    doc = yaml.safe_load(got["compose"])
    assert {"demo", "postgres", "demo-linkwarden", "demo-linkwarden-postgres", "demo-linkwarden-meilisearch",
            "demo-paperless", "demo-paperless-postgres", "demo-paperless-redis", "demo-paperless-gotenberg", "demo-paperless-tika"} == set(doc["services"])
    assert doc["services"]["demo-linkwarden"]["depends_on"].keys() == {"demo-linkwarden-postgres", "demo-linkwarden-meilisearch"}
    env = doc["services"]["demo-paperless"]["environment"]
    assert env["PAPERLESS_REDIS"] == "redis://demo-paperless-redis:6379" and env["PAPERLESS_DBHOST"] == "demo-paperless-postgres"
    assert env["PAPERLESS_TIKA_ENDPOINT"] == "http://demo-paperless-tika:9998"
    assert doc["services"]["demo"]["environment"]["POSTGRES_SERVER"] == "postgres"        # the first one is untouched
    assert "@demo-linkwarden-postgres:5432/app" in doc["services"]["demo-linkwarden"]["environment"]["DATABASE_URL"]


def test_building_blocks_folders_ports_and_secrets_are_each_templates_own(repo, monkeypatch):
    got = _several(repo, monkeypatch, ["mealie", "linkwarden", "paperless"])
    doc = yaml.safe_load(got["compose"])
    assert set(got["secrets"]) >= {"POSTGRES_PASSWORD", "LINKWARDEN_POSTGRES_PASSWORD", "PAPERLESS_POSTGRES_PASSWORD", "LINKWARDEN_MEILI_MASTER_KEY"}
    ports = [(h, c) for s, h, c in newapp.compose_ports(got["compose"])]
    assert len({h for h, _ in ports}) == len(ports)                                       # no host port twice
    folders = [v.split(":")[0] for s in doc["services"].values() for v in s.get("volumes") or [] if v.startswith("/srv/docker/")]
    assert len(set(folders)) == len(folders) and "/srv/docker/demo-paperless/postgres" in folders
    assert "demo-paperless" in {p["service"] for p in got["ports"]}


def test_every_template_fits_next_to_another_and_all_of_them_into_one_file(repo, monkeypatch):
    everything = list(templates.catalog())
    for ids in [["nginx", t] for t in everything if t != "nginx"] + [["mealie", t] for t in everything if t not in ("mealie", "romm")] + [everything]:
        got = _several(repo, monkeypatch, ids)
        doc = yaml.safe_load(got["compose"])
        names = set(doc["services"])
        what = "+".join(ids[:3])
        assert {"demo", *(f"demo-{t}" for t in ids[1:])} <= names, what
        for name, svc in doc["services"].items():
            for dep in svc.get("depends_on") or {}:
                assert dep in names, f"{what}: {name} waits for {dep}"
            if name != "demo":    # the first template is as it always was; the others point at their own, prefixed services
                for _, k, v in _text_values({"services": {name: svc}}):
                    host_like = re.search(r"HOST|SERVER|REDIS|ENDPOINT|URL", k)
                    assert not (host_like and re.fullmatch(r"postgres|mariadb|redis|valkey|meilisearch|database|server|tika|gotenberg|machine-learning", v)), f"{what}: {name}.{k} names a service without prefix"
        used = {h for _, h, _ in newapp.compose_ports(got["compose"])}
        assert len(used) == len(newapp.compose_ports(got["compose"])), what
        folders = [v.split(":")[0] for s in doc["services"].values() for v in s.get("volumes") or [] if v.startswith("/srv/docker/")]
        assert len(set(folders)) == len(folders), f"{what}: a folder is used twice"
        assert set(got["secrets"]) >= newapp.compose_vars(got["compose"])[0], what


def test_a_further_templates_note_names_its_own_folders_address_and_secrets(repo, monkeypatch):
    got = _several(repo, monkeypatch, ["mealie", "paperless"])
    note = {n["template"]: n["text"] for n in got["notes"]}["paperless"]
    assert "http://10.0.0.1:8000" in note and "/srv/docker/demo-paperless/consume" in note and "secrets get demo PAPERLESS_ADMIN_PASSWORD" in note
    assert "__" not in note
    assert _several(repo, monkeypatch, ["mealie"])["notes"] == []


def test_stand_in_folders_are_reported_per_service(repo, monkeypatch):
    got = _several(repo, monkeypatch, ["mealie", "navidrome", "jellyfin"])
    assert {(s["service"], s["path"]) for s in got["standIn"]} == {("demo-navidrome", "/music"), ("demo-jellyfin", "/media/Movies"), ("demo-jellyfin", "/media/Series")}


def test_files_of_several_templates_are_all_written_and_a_shared_name_is_refused(repo, monkeypatch):
    got = _several(repo, monkeypatch, ["nginx", "httpd", "searxng"])
    assert {"default.conf", "html/index.html", "httpd.conf", "htdocs/index.html", "searxng/settings.yml"} <= set(got["files"])
    twin = dataclasses.replace(templates.catalog()["nginx"], id="nginx2")
    compose = ("services:\n  demo:\n    image: nginx:1\n    volumes:\n      - ./default.conf:/a:ro\n"
               "  demo-nginx2:\n    image: nginx:1\n    volumes:\n      - ./default.conf:/a:ro\n")
    with pytest.raises(SystemExit, match="both bring the file default.conf"):
        newapp.template_files(compose, "demo", [templates.catalog()["nginx"], twin], None)


def test_the_same_template_twice_counts_once_and_an_unknown_one_is_an_error():
    assert [t.id for t in newapp.chosen_templates({"template": ["nginx", "nginx", "httpd"]})] == ["nginx", "httpd"]
    assert [t.id for t in newapp.chosen_templates({"template": "nginx"})] == ["nginx"]
    with pytest.raises(SystemExit, match="not one of"):
        newapp.chosen_templates({"template": ["nginx", "tomcat"]})


def test_gather_generates_the_secrets_of_every_template_and_keeps_one_route(agent):
    app = newapp.gather({"app_id": "demo", "server": "worker-1", "template": ["mealie", "paperless"], "web_port": 9000})
    assert app.upstream_port == 9000 and app.template == "mealie, paperless"
    assert set(app.secrets) == {"POSTGRES_PASSWORD", "PAPERLESS_POSTGRES_PASSWORD", "PAPERLESS_ADMIN_PASSWORD", "PAPERLESS_SECRET_KEY"} and all(app.secrets.values())
    assert "Mealie: " in app.first_run and "Paperless-ngx: " in app.first_run and "demo-paperless/consume" in app.first_run
