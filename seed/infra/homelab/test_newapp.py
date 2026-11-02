"""Tests for the wizards and the secrets menu (pytest infra/homelab)."""
import pathlib
import shutil
import sys

import pytest
import yaml

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import newapp  # noqa: E402
import removeapp  # noqa: E402
import secrets_menu  # noqa: E402

COMPOSE = """
services:
  web:
    image: x:1
    ports:
      - 8080:80
      - "127.0.0.1:9090:9000/tcp"
      - 3000
      - ${PORT}:1
      - {published: 7000, target: 70}
    environment:
      A: ${DB_PASSWORD}
      B: ${API_KEY:?fehlt}
      C: ${OPT:-x}
      D: $PLAIN
      E: $${NOT_A_VAR}
      # F: ${COMMENTED}
"""


def test_compose_vars():
    required, optional = newapp.compose_vars(COMPOSE)
    assert required == {"DB_PASSWORD", "API_KEY", "PORT", "PLAIN"}
    assert optional == {"OPT"}


def test_compose_ports():
    assert newapp.compose_ports(COMPOSE) == [("web", 8080, 80), ("web", 9090, 9000), ("web", 7000, 70)]


def test_skeleton_is_valid_compose_with_secret_refs():
    text = newapp.skeleton("demo", "ghcr.io/x/demo:1.0", 80, 8123, ["DB_PASSWORD"], "/data")
    svc = yaml.safe_load(text)["services"]["demo"]
    assert svc["ports"] == ["8123:80"] and svc["environment"] == {"DB_PASSWORD": "${DB_PASSWORD}"}
    assert svc["volumes"] == ["/srv/docker/demo:/data"]
    assert newapp.compose_vars(text) == ({"DB_PASSWORD"}, set())
    assert "ports" not in yaml.safe_load(newapp.skeleton("demo", "x:1", None, None, [], None))["services"]["demo"]


def _app(**kw):
    app = newapp.App("demo", "worker-1", newapp.skeleton("demo", "x:1", 80, 8123, ["S"], None), True,
                     domain="demo.lab.example", upstream_port=8123, secrets={"S": "v"})
    for k, v in kw.items():
        setattr(app, k, v)
    return app


def test_inventory_block_and_manifest():
    entry = yaml.safe_load(newapp.inventory_block(_app()))[0]
    assert entry == {"id": "demo", "namespace": "infrastructure", "path": "stacks/demo", "compose_files": ["compose.yaml"],
                     "komodo_stack": "demo", "server": "worker-1", "project_name": "demo", "status": "verified",
                     "secrets": "infisical"}
    assert "secrets" not in yaml.safe_load(newapp.inventory_block(_app(secrets={})))[0]
    svc = yaml.safe_load(newapp.manifest(_app()))["services"][0]
    assert svc["proxy"]["domains"] == ["demo.lab.example"]
    assert svc["proxy"]["upstream"] == {"scheme": "http", "server": "worker-1", "port": 8123}
    assert svc["certificate"]["issuer_ref"] == "homelab-private-ca"


def test_wizard_files_pass_ingressctl(tmp_path, monkeypatch):
    """The wizard's manifest + inventory entry against ingressctl's real schema and a small inventory of its own."""
    real = pathlib.Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(real / "infra/ingress-controller/src"))
    pytest.importorskip("jsonschema")
    from ingressctl.inventory import load_inventory
    from ingressctl.manifest import parse_manifest
    inv = tmp_path / "inventory"
    inv.mkdir()
    (inv / "stacks.yaml").write_text("stacks:\n- {id: other, namespace: infrastructure, path: stacks/other, compose_files: [compose.yaml], status: verified}\n")
    (inv / "servers.yaml").write_text("servers:\n  worker-1: {address: 10.0.0.20, status: verified}\n  proxy-1: {address: 10.0.0.30, status: verified}\n")
    (inv / "namespaces.yaml").write_text("namespaces:\n  infrastructure: {paths: [stacks/]}\n")
    (inv / "ingress.yaml").write_text(
        "npm_instances:\n  main: {server: proxy-1, api_url: 'http://npm.test/api', tls_address: 10.0.0.30, tls_port: 443}\n"
        "issuers:\n  homelab-private-ca: {type: infisical, api_url: 'http://infisical.test', project_id: p, profile_id: f, application_id: a,\n"
        "    max_validity_days: 90, ca_bundle: ca.pem, allowed_domains: ['*.lab.example']}\n"
        "controller: {default_npm_instance: main, allowed_repos: [owner/infrastructure]}\n")
    monkeypatch.setattr(newapp, "ROOT", tmp_path)
    monkeypatch.setattr(newapp.ops, "ROOT", tmp_path)
    newapp.write_files(_app())
    specs = parse_manifest(tmp_path, tmp_path / "stacks/demo/ingress.yaml", load_inventory(tmp_path))
    assert [s.resource_id for s in specs] == ["infrastructure/demo"]


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / "inventory").mkdir()
    (tmp_path / "inventory/stacks.yaml").write_text(
        "stacks:\n- id: other\n  path: stacks/other\n  compose_files:\n  - compose.yaml\n  server: worker-1\n  status: verified\n")
    (tmp_path / "stacks/other").mkdir(parents=True)
    (tmp_path / "stacks/other/compose.yaml").write_text("services:\n  a:\n    ports: ['8123:80']\n")
    (tmp_path / "stacks/other/ingress.yaml").write_text(
        "services:\n- id: other\n  proxy:\n    domains: [other.lab.example]\n")
    monkeypatch.setattr(newapp, "ROOT", tmp_path)
    monkeypatch.setattr(newapp.ops, "ROOT", tmp_path)
    return tmp_path


def test_used_ports_and_known_domains(repo):
    assert newapp.used_host_ports("worker-1", repo) == {8123}
    assert newapp.used_host_ports("nas", repo) == set()
    assert newapp.known_domains(repo) == {"other.lab.example": "stacks/other/ingress.yaml"}


def test_write_files_is_idempotent(repo):
    app = _app()
    paths = newapp.write_files(app)
    assert paths == ["stacks/demo", "inventory/stacks.yaml"]
    assert (repo / "stacks/demo/ingress.yaml").is_file()
    inv = yaml.safe_load((repo / "inventory/stacks.yaml").read_text())["stacks"]
    assert [s["id"] for s in inv] == ["other", "demo"]
    files = ["stacks/demo/compose.yaml", "stacks/demo/ingress.yaml", "inventory/stacks.yaml"]
    before = {p: (repo / p).read_text() for p in files}
    app.compose_is_new = False
    newapp.write_files(app)
    assert {p: (repo / p).read_text() for p in files} == before
    assert app.entry["komodo_stack"] == "demo" and app.stack == "demo"


# --- secrets menu ---


class FakeFolder:
    def __init__(self, inf, stack):
        self.where, self.data = "stacks-x/prod/demo", inf

    def keys(self):
        return sorted(self.data)

    def get(self, key):
        return self.data[key]

    def set(self, key, value):
        res = "changed" if key in self.data else "created"
        self.data[key] = value
        return res

    def delete(self, key):
        del self.data[key]


def test_secrets_menu_flow(monkeypatch, capsys):
    data = {"A": "1"}
    answers = iter(["n", "NEW", "g", "16", "n",   # new, generate (16 bytes), do not show
                    "c 1", "t",                   # change A, type it (getpass)
                    "s 1",                        # show
                    "d 2", "y",                   # delete NEW
                    "c", "x",                     # missing number, unknown command
                    "q", "n"])                    # quit, no redeploy
    monkeypatch.setattr("builtins.input", lambda _="": next(answers))
    monkeypatch.setattr("getpass.getpass", lambda _="": "geheim")
    monkeypatch.setattr(secrets_menu.secret, "Folder", FakeFolder)
    monkeypatch.setattr(secrets_menu.secret, "compose_refs", lambda stack, key: key == "A")
    stack = {"id": "demo", "komodo_stack": "demo"}
    assert secrets_menu.stack_menu(data, stack) is False
    assert data == {"A": "geheim"}
    out = capsys.readouterr().out
    assert "A = geheim" in out and "NEW created" in out and "deleted" in out and "e.g. 'c 1'" in out


# --- Remove app ---


def test_remove_inventory_entry_keeps_neighbours():
    text = ("# header\nstacks:\n- id: a\n  server: x\n- id: demo\n  server: y\n  secrets: infisical\n"
            "# comment of the next entry\n- id: b\n  server: z\n")
    out = removeapp.remove_inventory_entry(text, "demo")
    assert out == "# header\nstacks:\n- id: a\n  server: x\n# comment of the next entry\n- id: b\n  server: z\n"
    assert removeapp.remove_inventory_entry(out, "demo") == out
    assert removeapp.remove_inventory_entry("stacks:\n- id: demo\n  server: y\n", "demo") == "stacks:\n"


def test_wizard_entry_round_trip(repo):
    before = (repo / "inventory/stacks.yaml").read_text()
    newapp.write_files(_app())
    after = removeapp.remove_inventory_entry((repo / "inventory/stacks.yaml").read_text(), "demo")
    assert after == before


def test_bind_mounts(tmp_path):
    (tmp_path / "compose.yaml").write_text(
        "services:\n  a:\n    volumes:\n      - /srv/docker/demo:/data\n      - ./conf:/conf:ro\n      - named:/x\n"
        "      - {type: bind, source: /mnt/media, target: /media}\n")
    assert removeapp.bind_mounts(tmp_path) == ["/srv/docker/demo", "/mnt/media"]
    assert removeapp.bind_mounts(tmp_path / "missing") == []


# --- agent mode ---


@pytest.fixture
def agent(repo, monkeypatch):
    (repo / "inventory/servers.yaml").write_text("servers:\n  worker-1: {address: 10.0.0.3, status: verified}\n")
    (repo / "inventory/ingress.yaml").write_text("issuers:\n  homelab-private-ca: {allowed_domains: ['*.lab.example']}\n")
    monkeypatch.setattr(newapp.ui, "AUTO", True)
    monkeypatch.setattr(newapp.ui, "ASSUME_YES", True)
    monkeypatch.setattr("builtins.input", lambda *_: pytest.fail("agent mode must not prompt"))
    return repo


def test_agent_gather_from_flags(agent, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "from-env")
    app = newapp.gather({"app_id": "demo", "server": "worker-1", "image": "x/demo:1.0", "container_port": 80,
                         "secret_vars": ["API_TOKEN", "DB_PASSWORD"], "mount": "",
                         "secrets": {"API_TOKEN": "env", "DB_PASSWORD": "generate:8"}})
    assert app.domain == "demo.lab.example" and app.upstream_port == 8000   # 8123 is taken by "other"
    assert app.force_https is True and app.websockets is False
    assert app.secrets["API_TOKEN"] == "from-env" and len(app.secrets["DB_PASSWORD"]) == 16


def test_agent_missing_answers_name_the_flag(agent):
    with pytest.raises(SystemExit, match="--image"):
        newapp.gather({"app_id": "demo", "server": "worker-1"})
    with pytest.raises(SystemExit, match="--server"):
        newapp.gather({"app_id": "demo"})
    with pytest.raises(SystemExit, match="--secret S=generate"):
        newapp.gather({"app_id": "demo", "server": "worker-1", "image": "x:1", "container_port": 80,
                       "secret_vars": ["S"], "mount": "", "routing": False})
    with pytest.raises(SystemExit, match="does not use"):
        newapp.gather({"app_id": "demo", "server": "worker-1", "image": "x:1", "secret_vars": [], "mount": "",
                       "container_port": 80, "routing": False, "secrets": {"NOPE": "generate"}})
    with pytest.raises(SystemExit, match="taken"):
        newapp.gather({"app_id": "demo", "server": "worker-1", "image": "x:1", "container_port": 80,
                       "host_port": 8123})


def test_agent_dry_run_stops_before_changes(agent, monkeypatch):
    monkeypatch.setattr(newapp.ui, "ASSUME_YES", False)
    with pytest.raises(newapp.ui.DryRun, match="pass --yes"):
        newapp.gather({"app_id": "demo", "server": "worker-1", "image": "x:1", "container_port": 80,
                       "secret_vars": [], "mount": ""})
    assert not (agent / "stacks/demo").exists()


def test_resolve_secret():
    assert len(newapp.resolve_secret("A", "generate")) == 64
    assert newapp.resolve_secret("A", "keep") is None
    with pytest.raises(SystemExit, match="not set"):
        newapp.resolve_secret("SURELY_NOT_SET_VAR", "env")
    with pytest.raises(SystemExit, match="use generate"):
        newapp.resolve_secret("A", "plain-value")


def test_homelab_cli_presets():
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
    import homelab
    args = homelab.parse_args(["new-app", "--app-id", "demo", "--server", "s", "--secret", "A=generate",
                               "--secret-var", "A", "--no-routing", "--no-force-https", "--statuses", "200,302"])
    p = homelab.preset(args)
    assert p["secrets"] == {"A": "generate"} and p["secret_vars"] == ["A"] and "secret_var" not in p
    assert homelab.preset(homelab.parse_args(["new-app", "--app-id", "demo"]))["secret_vars"] is None and p["routing"] is False and p["force_https"] is False
    assert p["statuses"] == [200, 302] and p["websockets"] is None and not args.yes
    with pytest.raises(SystemExit):
        homelab.parse_args(["new-app", "--app-id", "demo", "--secret", "no-equals"])


# --- status / logs / deploy ---


class FakeKomodo:
    def __init__(self, state="running"):
        self.state, self.deployed = state, False

    def read(self, typ, params=None):
        return {
            "ListServers": [{"id": "s1", "name": "host-1"}],
            "ListStacks": [{"name": "paperless", "id": "p1", "info": {"state": self.state, "server_id": "s1"}}],
            "GetStack": {"info": {"deployed_services": [{"container_name": "paperless"}, {"container_name": "paperless-db"}]}},
            "ListDockerContainers": [{"name": "paperless", "state": "running", "status": "Up"},
                                     {"name": "paperless-db-1", "state": "running", "status": "Up"},
                                     {"name": "other", "state": "running", "status": "Up"}],
            "ListUpdates": {"updates": []},
            "GetStackLog": {"stdout": "\x1b[32mline\x1b[0m\n", "stderr": ""},
        }[typ]

    def execute_and_poll(self, typ, params, timeout=0):
        self.deployed = True
        return {"success": True, "logs": [{"stage": "Compose Up", "success": True}], "id": "u1"}


def test_stackops(capsys, monkeypatch):
    import stackops
    assert stackops.status(FakeKomodo(), ["paperless"]) == 0
    out = capsys.readouterr().out
    assert "paperless-db-1" in out and "other" not in out
    assert stackops.status(FakeKomodo("unhealthy"), ["paperless"]) == 1
    with pytest.raises(SystemExit, match="unknown"):
        stackops.status(FakeKomodo(), ["paperless", "nope"])
    capsys.readouterr()
    stackops.logs(FakeKomodo(), "paperless", 10, [], [])
    assert capsys.readouterr().out == "line\n"
    k = FakeKomodo()
    monkeypatch.setattr(stackops.ui, "AUTO", True)
    monkeypatch.setattr(stackops.ui, "ASSUME_YES", False)
    with pytest.raises(stackops.ui.DryRun):
        stackops.deploy(k, "paperless")
    monkeypatch.setattr(stackops.ui, "ASSUME_YES", True)
    assert stackops.deploy(k, "paperless") == 0 and k.deployed


# --- building blocks ---

import addons  # noqa: E402


def test_addon_catalog_blocks_are_sound():
    blocks = addons.catalog()
    assert {"postgres", "mariadb", "redis", "valkey", "mongo", "meilisearch", "adminer", "rustfs", "mongoku", "redisinsight"} <= set(blocks)
    for b in blocks.values():
        service = yaml.safe_load(b.services)[b.id]            # the service is named like the block
        assert ":" in service["image"] and not service["image"].endswith(":latest")
        assert "healthcheck" in service or b.vpn            # gluetun's image brings its own healthcheck
        assert service["restart"] == "unless-stopped"
        assert all(v.startswith("/srv/docker/__APP__/") or (b.vpn and v == "../gluetun/wg0.conf")   # data lives under /srv/docker
                   for v in (x.split(":")[0] for x in service.get("volumes", [])))
        required, optional = newapp.compose_vars(b.services)
        provided = {s for r in b.requires_any for s in blocks[r].secrets}   # a tool uses the password of its database
        assert required - provided == set(b.secrets) and not optional      # exactly the passwords the block declares
        rendered = b.render("demo", 8123 if b.needs_port else None, b.requires_any[0] if b.requires_any else "", "10.0.0.1", (9000, 80))
        assert not any(m in rendered for m in ("__APP__", "__PORT__", "__DB__", "__HOST__", "__HOST_PORT__", "__CONTAINER_PORT__"))
        assert (b.needs_port or b.needs_app_port) == ("ports" in service)


def test_skeleton_with_blocks_is_a_connected_compose_file():
    blocks = [addons.catalog()[n] for n in ("postgres", "redis")]
    text = newapp.skeleton("demo", "x:1", 80, 8123, ["API_KEY"], "/data", blocks)
    services = yaml.safe_load(text)["services"]
    assert list(services) == ["demo", "postgres", "redis"]
    assert services["demo"]["depends_on"] == {"postgres": {"condition": "service_healthy"}, "redis": {"condition": "service_healthy"}}
    assert services["demo"]["environment"]["POSTGRES_URL"] == "postgresql://app:${POSTGRES_PASSWORD}@postgres:5432/app"
    assert services["demo"]["environment"]["REDIS_URL"] == "redis://redis:6379"
    assert services["postgres"]["volumes"] == ["/srv/docker/demo/postgres:/var/lib/postgresql"]
    assert newapp.compose_vars(text) == ({"API_KEY", "POSTGRES_PASSWORD"}, set())
    assert newapp.compose_ports(text) == [("demo", 8123, 80)]       # blocks publish nothing: routing is unaffected


def test_skeleton_without_blocks_is_unchanged():
    assert yaml.safe_load(newapp.skeleton("demo", "x:1", None, None, [], None))["services"]["demo"] == \
        {"image": "x:1", "container_name": "demo", "restart": "unless-stopped"}


def test_a_block_cannot_share_the_apps_name():
    with pytest.raises(SystemExit, match="service name is taken"):
        newapp.skeleton("redis", "x:1", None, None, [], None, [addons.catalog()["redis"]])


def test_choose_addons(monkeypatch):
    monkeypatch.setattr(newapp.ui, "AUTO", True)
    assert [b.id for b in newapp._choose_addons({"addon": ["redis", "postgres", "redis"]})] == ["redis", "postgres"]
    assert newapp._choose_addons({}) == []                             # agent mode: none unless asked for
    with pytest.raises(SystemExit, match="not one of"):
        newapp._choose_addons({"addon": ["oracle"]})


def _servers(repo):
    (repo / "inventory/servers.yaml").write_text("servers:\n  worker-1: {address: 10.0.0.1, status: verified}\n")


def test_compose_preview(repo, capsys, monkeypatch):
    monkeypatch.setattr(newapp.ui, "AUTO", True)
    _servers(repo)
    got = newapp.compose_preview({"app_id": "demo", "server": "worker-1", "image": "x:1", "container_port": 80,
                                  "addon": ["postgres"], "secret_vars": ["API_KEY"]})
    assert list(yaml.safe_load(got["compose"])["services"]) == ["demo", "postgres"]
    assert got["existing"] is False and got["secrets"] == ["API_KEY", "POSTGRES_PASSWORD"]
    assert got["ports"] == [{"service": "demo", "host": 8000, "container": 80}]
    assert capsys.readouterr().out == ""                                # only the result is the answer
    other = newapp.compose_preview({"app_id": "other"})
    assert other["compose"] == (repo / "stacks/other/compose.yaml").read_text() and other["existing"] is True
    assert not (repo / "stacks/demo").exists()                          # nothing written
    with pytest.raises(SystemExit, match="--server"):
        newapp.compose_preview({"app_id": "demo", "server": "nope", "image": "x:1"})
    with pytest.raises(SystemExit, match="--app-id"):
        newapp.compose_preview({"app_id": "Bad_ID"})


def test_compose_file_is_used_as_it_is_and_checked(repo, tmp_path, monkeypatch):
    monkeypatch.setattr(newapp.ui, "AUTO", True)
    _servers(repo)
    f = tmp_path / "c.yaml"
    f.write_text("services:\n  web:\n    image: x:1\n    ports: ['9000:80', '9001:81']\n    environment:\n      K: ${MY_KEY}\n")
    got = newapp.compose_preview({"app_id": "demo", "server": "worker-1", "compose_file": str(f)})
    assert got["compose"] == f.read_text() and got["secrets"] == ["MY_KEY"]
    assert [x["host"] for x in got["ports"]] == [9000, 9001]
    f.write_text("services:\n  web:\n    image: x:1\n    ports: ['8123:80']\n")      # taken by stacks/other
    with pytest.raises(SystemExit, match="already used"):
        newapp.compose_preview({"app_id": "demo", "server": "worker-1", "compose_file": str(f)})
    for bad, msg in (("services: [", "not valid YAML"), ("foo: 1", "services"), ("services: {}", "services"),
                     ("services:\n  a: x", "services")):
        f.write_text(bad)
        with pytest.raises(SystemExit, match=msg):
            newapp.compose_preview({"app_id": "demo", "server": "worker-1", "compose_file": str(f)})
    with pytest.raises(SystemExit, match="not a file"):
        newapp.compose_preview({"app_id": "demo", "server": "worker-1", "compose_file": str(tmp_path / "missing")})


def test_form_values_read_back_what_the_generator_was_given():
    blocks = [addons.catalog()[n] for n in ("postgres", "valkey")]
    text = newapp.skeleton("demo", "ghcr.io/x/demo:1.2", 80, 8123, ["API_KEY"], "/data", blocks)
    got = newapp.form_values(text, "demo")
    assert {k: got[k] for k in ("image", "containerPort", "hostPort", "ports", "mount", "secretVars", "addons")} == {
        "image": "ghcr.io/x/demo:1.2", "containerPort": 80, "hostPort": 8123, "ports": [[8123, 80]], "mount": "/data",
        "secretVars": ["API_KEY"], "addons": ["postgres", "valkey"]}
    assert got["env"] == {} and got["networks"] == [] and got["gpu"] == ""            # the blocks' variables are not the app's own
    edited = text.replace("8123:80", "9000:8080").replace("  valkey:\n    image", "  valkey:\n    image: other:1\n    x")
    got = newapp.form_values(edited, "demo")
    assert (got["hostPort"], got["containerPort"]) == (9000, 8080)
    assert "valkey" not in got["addons"]                      # another image under that name is not the block


def test_form_values_of_a_foreign_compose_file():
    text = "services:\n  web:\n    image: x:1\n    environment:\n      K: ${MY_KEY}\n"
    got = newapp.form_values(text, "demo")
    assert {k: got[k] for k in ("image", "containerPort", "hostPort", "ports", "mount", "addons")} == {
        "image": "x:1", "containerPort": None, "hostPort": None, "ports": [], "mount": None, "addons": []}
    assert got["secretVars"] == [] and got["env"] == {"K": "${MY_KEY}"}               # not NAME: ${NAME}: plain environment
    assert newapp.form_values("services: {}", "demo")["image"] is None


# --- tools next to the app (UI, S3) ---


def _all(*names):
    return [addons.catalog()[n] for n in names]


def test_a_tool_block_is_neither_waited_for_nor_used_by_the_app():
    blocks = _all("postgres", "adminer")
    text = newapp.skeleton("demo", "x:1", 80, 8123, [], None, blocks, {"adminer": 8001})
    services = yaml.safe_load(text)["services"]
    assert list(services["demo"]["depends_on"]) == ["postgres"] and "ADMINER" not in str(services["demo"]["environment"])
    assert services["adminer"]["ports"] == ["8001:8080"]
    assert services["adminer"]["environment"] == {"ADMINER_DEFAULT_SERVER": "postgres"}
    assert services["adminer"]["depends_on"] == {"postgres": {"condition": "service_healthy"}}
    assert newapp.compose_ports(text) == [("demo", 8123, 80), ("adminer", 8001, 8080)]
    assert newapp.block_services(text) == {"postgres", "adminer"}


def test_adminer_opens_the_first_database_it_finds():
    blocks = _all("mariadb", "postgres", "adminer")
    assert newapp.db_of(addons.catalog()["adminer"], blocks) == "mariadb"
    services = yaml.safe_load(newapp.skeleton("demo", "x:1", None, None, [], None, blocks, {"adminer": 8001}))["services"]
    assert services["adminer"]["environment"]["ADMINER_DEFAULT_SERVER"] == "mariadb"


def test_a_block_that_needs_a_port_gets_one_or_fails():
    with pytest.raises(ValueError, match="needs a host port"):
        newapp.skeleton("demo", "x:1", None, None, [], None, _all("postgres", "adminer"))


def test_tool_blocks_need_their_database(monkeypatch):
    monkeypatch.setattr(newapp.ui, "AUTO", True)
    with pytest.raises(SystemExit, match="needs one of postgres, mariadb"):
        newapp._choose_addons({"addon": ["adminer", "redis"]})
    assert [b.id for b in newapp._choose_addons({"addon": ["adminer", "mariadb"]})] == ["adminer", "mariadb"]


def test_s3_block_gives_the_app_endpoint_and_keys():
    text = newapp.skeleton("demo", "x:1", None, None, [], None, _all("rustfs"), {"rustfs": 8002})
    services = yaml.safe_load(text)["services"]
    assert services["demo"]["environment"] == {"S3_ENDPOINT": "http://rustfs:9000", "S3_ACCESS_KEY": "${RUSTFS_ACCESS_KEY}",
                                                "S3_SECRET_KEY": "${RUSTFS_SECRET_KEY}"}
    assert services["rustfs"]["user"] == "0:0" and services["rustfs"]["ports"] == ["8002:9001"]   # the image's uid cannot write a root-owned bind mount
    assert newapp.compose_vars(text) == ({"RUSTFS_ACCESS_KEY", "RUSTFS_SECRET_KEY"}, set())


def test_preview_keeps_tool_ports_out_of_the_routing_candidates(repo, monkeypatch):
    monkeypatch.setattr(newapp.ui, "AUTO", True)
    _servers(repo)
    got = newapp.compose_preview({"app_id": "demo", "server": "worker-1", "image": "x:1", "container_port": 80,
                                  "addon": ["postgres", "adminer", "rustfs"]})
    assert [(p["service"], p["host"]) for p in got["ports"]] == [("demo", 8000)]
    assert [(p["service"], p["container"]) for p in got["blockPorts"]] == [("adminer", 8080), ("rustfs", 9001)]
    assert len({p["host"] for p in got["ports"] + got["blockPorts"]}) == 3          # three different free ports
    assert 8123 not in {p["host"] for p in got["blockPorts"]}                        # nothing taken on the server
    assert got["form"]["addons"] == ["adminer", "postgres", "rustfs"]


def test_mongoku_is_connected_to_mongo_and_knows_where_it_is_opened():
    blocks = _all("mongo", "mongoku")
    text = newapp.skeleton("demo", "x:1", None, None, [], None, blocks, {"mongoku": 8003}, "10.0.0.50")
    mk = yaml.safe_load(text)["services"]["mongoku"]
    assert mk["environment"] == {"MONGOKU_DEFAULT_HOST": "mongodb://app:${MONGO_PASSWORD}@mongo:27017",
                                 "MONGOKU_SERVER_ORIGIN": "http://10.0.0.50:8003"}
    assert mk["ports"] == ["8003:3100"] and mk["depends_on"] == {"mongo": {"condition": "service_healthy"}}
    with pytest.raises(ValueError, match="server's address"):
        newapp.skeleton("demo", "x:1", None, None, [], None, blocks, {"mongoku": 8003})
    assert newapp.compose_vars(text) == ({"MONGO_PASSWORD"}, set())


def test_redisinsight_follows_whichever_cache_is_chosen(monkeypatch):
    monkeypatch.setattr(newapp.ui, "AUTO", True)
    for cache in ("redis", "valkey"):
        blocks = newapp._choose_addons({"addon": [cache, "redisinsight"]})
        ri = yaml.safe_load(newapp.skeleton("demo", "x:1", None, None, [], None, blocks, {"redisinsight": 8004}))["services"]["redisinsight"]
        assert ri["depends_on"] == {cache: {"condition": "service_healthy"}} and ri["ports"] == ["8004:5540"]
    with pytest.raises(SystemExit, match="needs one of redis, valkey"):
        newapp._choose_addons({"addon": ["redisinsight", "postgres"]})


def test_server_address(repo):
    _servers(repo)
    assert newapp.server_address("worker-1", repo) == "10.0.0.1" and newapp.server_address("nope", repo) is None


def test_a_tool_says_where_its_page_starts():
    assert addons.catalog()["rustfs"].url_path == "/rustfs/console/"        # the console answers 403 at /
    assert addons.catalog()["adminer"].url_path == "/"
    assert addons.catalog()["rustfs"].info()["urlPath"] == "/rustfs/console/"


def test_preview_gives_the_path_of_each_tool_port(repo, monkeypatch):
    monkeypatch.setattr(newapp.ui, "AUTO", True)
    _servers(repo)
    got = newapp.compose_preview({"app_id": "demo", "server": "worker-1", "image": "x:1", "addon": ["postgres", "adminer", "rustfs"]})
    assert {p["service"]: p["path"] for p in got["blockPorts"]} == {"adminer": "/", "rustfs": "/rustfs/console/"}


# --- VPN (gluetun) ---


def test_vpn_moves_the_apps_port_to_gluetun_and_waits_for_it():
    text = newapp.skeleton("demo", "x:1", 80, 8123, ["API_KEY"], "/data", _all("gluetun"))
    services = yaml.safe_load(text)["services"]
    demo, vpn = services["demo"], services["gluetun"]
    assert demo["network_mode"] == "service:gluetun" and "ports" not in demo
    assert demo["depends_on"] == {"gluetun": {"condition": "service_healthy"}}
    assert vpn["ports"] == ["8123:80"] and vpn["environment"]["FIREWALL_INPUT_PORTS"] == "80"
    assert vpn["volumes"] == ["../gluetun/wg0.conf:/gluetun/wireguard/wg0.conf:ro"]
    assert vpn["cap_add"] == ["NET_ADMIN"] and vpn["devices"] == ["/dev/net/tun:/dev/net/tun"]
    assert newapp.compose_ports(text) == [("gluetun", 8123, 80)]
    assert newapp.tool_services(text) == set()                     # the VPN's port is the app's: it can be the domain
    assert newapp.block_services(text) == {"gluetun"}


def test_vpn_needs_the_apps_port_and_stands_alone(monkeypatch):
    with pytest.raises(SystemExit, match="needs the app's port"):
        newapp.skeleton("demo", "x:1", None, None, [], None, _all("gluetun"))
    monkeypatch.setattr(newapp.ui, "AUTO", True)
    with pytest.raises(SystemExit, match="cannot be combined with other blocks"):
        newapp._choose_addons({"addon": ["gluetun", "postgres"]})
    assert [b.id for b in newapp._choose_addons({"addon": ["gluetun"]})] == ["gluetun"]


def test_form_values_find_the_port_behind_the_vpn():
    text = newapp.skeleton("demo", "x:1", 80, 8123, [], "/data", _all("gluetun"))
    got = newapp.form_values(text, "demo")
    assert (got["hostPort"], got["containerPort"], got["ports"], got["addons"]) == (8123, 80, [[8123, 80]], ["gluetun"])


def test_vpn_config_must_be_in_the_repo_and_committed(tmp_path):
    import subprocess
    assert "not in the repo" in newapp.vpn_config_problem(tmp_path)
    (tmp_path / "stacks/gluetun").mkdir(parents=True)
    (tmp_path / "stacks/gluetun/wg0.conf").write_text("x")
    assert newapp.vpn_config_problem(tmp_path) is None             # no git checkout: the file counts
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    assert "not committed yet" in newapp.vpn_config_problem(tmp_path)
    subprocess.run(["git", "-C", str(tmp_path), "add", "-f", "stacks/gluetun/wg0.conf"], check=True)
    assert newapp.vpn_config_problem(tmp_path) is None


def test_the_vpn_block_checks_the_config_before_it_generates(repo, monkeypatch):
    monkeypatch.setattr(newapp.ui, "AUTO", True)
    _servers(repo)
    monkeypatch.setattr(newapp, "vpn_config_problem", lambda root=None: "stacks/gluetun/wg0.conf is not committed yet")
    with pytest.raises(SystemExit, match="not committed yet"):
        newapp.compose_preview({"app_id": "demo", "server": "worker-1", "image": "x:1", "container_port": 80, "addon": ["gluetun"]})
    monkeypatch.setattr(newapp, "vpn_config_problem", lambda root=None: None)
    got = newapp.compose_preview({"app_id": "demo", "server": "worker-1", "image": "x:1", "container_port": 80, "addon": ["gluetun"]})
    assert [(p["service"], p["host"]) for p in got["ports"]] == [("gluetun", 8000)] and got["blockPorts"] == []
    assert got["form"]["addons"] == ["gluetun"]
