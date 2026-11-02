"""Tests for the options of the app's service: volumes, networks, limits, GPU, environment (pytest infra/homelab)."""
import pathlib
import sys

import pytest
import yaml

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import newapp  # noqa: E402
import serviceopts as so  # noqa: E402

ALL = {"volume": ["data::/data", "data:media:/media", "bind:/mnt/tank/Series:/series:ro", "named:cache:/cache",
                  "tmpfs::/tmp:size=512m", "repo:./config:/config:ro"],
       "network": ["internal"], "external_network": ["protonmail_default"], "cpus": "1.5", "memory": "2g", "gpu": "nvidia",
       "env": ["TZ=Europe/Berlin", "DEBUG=true", "URL=http://x:8080/a", "EMPTY=", "N=007"]}


@pytest.mark.parametrize("spec,volume", [
    ("data::/data", so.Volume("data", "", "/data")),
    ("data:media/photos:/media:ro", so.Volume("data", "media/photos", "/media", True)),
    ("bind:/mnt/tank/media:/media:ro", so.Volume("bind", "/mnt/tank/media", "/media", True)),
    ("named:cache:/cache", so.Volume("named", "cache", "/cache")),
    ("tmpfs::/tmp:size=8g", so.Volume("tmpfs", "", "/tmp", False, "8g")),
    ("tmpfs::/run", so.Volume("tmpfs", "", "/run")),
    ("repo:./config/app.ini:/etc/app.ini:ro", so.Volume("repo", "./config/app.ini", "/etc/app.ini", True)),
    ("repo:../shared/x:/x", so.Volume("repo", "../shared/x", "/x")),
])
def test_parse_volume(spec, volume):
    assert so.parse_volume(spec) == volume
    assert so.parse_volume(volume.spec()) == volume          # the flag grammar round-trips


@pytest.mark.parametrize("spec", [
    "disk::/x", "data::data", "data:/abs:/x", "bind:rel:/x", "bind::/x", "named:bad name:/x", "repo:config:/x", "repo:/etc:/x",
    "tmpfs:src:/x", "tmpfs::/x:ro", "data::/x:size=1g", "tmpfs::/x:size=big", "data::/x:rw", "data::/a b", "data", "data:a",
])
def test_parse_volume_rejects(spec):
    with pytest.raises(SystemExit):
        so.parse_volume(spec)


def test_options_from_checks_the_whole_set():
    assert so.options_from({}) == so.ServiceOptions()
    with pytest.raises(SystemExit, match="same target"):
        so.options_from({"volume": ["data::/x", "named:v:/x"]})
    with pytest.raises(SystemExit, match="listed twice"):
        so.options_from({"network": ["a"], "external_network": ["a"]})
    with pytest.raises(SystemExit, match="not 'default'"):
        so.options_from({"network": ["default"]})
    for bad in ({"cpus": "0"}, {"cpus": "lots"}, {"memory": "2"}, {"memory": "0g"}, {"gpu": "amd"}):
        with pytest.raises(SystemExit):
            so.options_from(bad)
    with pytest.raises(SystemExit, match="NAME=value"):
        so.options_from({"env": ["1BAD=x"]})


def test_the_generated_service_has_everything_in_the_idiom_of_the_other_stacks():
    text = newapp.skeleton("demo", "x:1", 80, 8123, ["API_KEY"], None, [], {}, None, so.options_from(ALL))
    doc = yaml.safe_load(text)
    svc = doc["services"]["demo"]
    assert svc["volumes"] == ["/srv/docker/demo:/data", "/srv/docker/demo/media:/media", "/mnt/tank/Series:/series:ro", "cache:/cache",
                              "./config:/config:ro"]
    assert svc["tmpfs"] == ["/tmp:size=512m"]
    assert svc["networks"] == ["default", "internal", "protonmail_default"]
    assert (svc["cpus"], svc["mem_limit"]) == ("1.5", "2g")
    assert svc["deploy"]["resources"]["reservations"]["devices"] == [{"driver": "nvidia", "count": "all", "capabilities": ["gpu"]}]
    assert svc["environment"] == {"API_KEY": "${API_KEY}", "TZ": "Europe/Berlin", "DEBUG": "true", "URL": "http://x:8080/a", "EMPTY": "", "N": "007"}
    assert doc["networks"] == {"internal": {}, "protonmail_default": {"external": True}}
    assert doc["volumes"] == {"cache": {}}


def test_env_values_survive_yaml_as_the_strings_they_are():
    env = {"A": "true", "B": "007", "C": "1e3", "D": "no", "E": "a: b", "F": "x # y", "G": "", "H": "it's", "I": 'say "hi"', "J": "${REF}", "K": "~"}
    text = "services:\n  demo:\n    image: x:1\n    environment:\n" + "\n".join(so.env_lines(env)) + "\n"
    assert yaml.safe_load(text)["services"]["demo"]["environment"] == env


def test_gpu_dri_is_a_device_not_a_reservation():
    svc = yaml.safe_load(newapp.skeleton("demo", "x:1", None, None, [], None, [], {}, None, so.options_from({"gpu": "dri"})))["services"]["demo"]
    assert svc["devices"] == ["/dev/dri:/dev/dri"] and "deploy" not in svc


def test_the_legacy_mount_stays_the_first_volume():
    svc = yaml.safe_load(newapp.skeleton("demo", "x:1", None, None, [], "/data", [], {}, None, so.options_from({"volume": ["tmpfs::/tmp"]})))["services"]["demo"]
    assert svc["volumes"] == ["/srv/docker/demo:/data"] and svc["tmpfs"] == ["/tmp"]


def test_options_and_the_vpn_do_not_mix_when_the_app_cannot_join_networks():
    import addons
    with pytest.raises(SystemExit, match="cannot join other networks"):
        newapp.skeleton("demo", "x:1", 80, 8123, [], None, [addons.catalog()["gluetun"]], {}, None, so.options_from({"network": ["n"]}))
    text = newapp.skeleton("demo", "x:1", 80, 8123, [], None, [addons.catalog()["gluetun"]], {}, None, so.options_from({"memory": "1g", "env": ["A=1"]}))
    assert yaml.safe_load(text)["services"]["demo"]["mem_limit"] == "1g"


def test_what_the_generator_writes_the_form_reads_back():
    text = newapp.skeleton("demo", "x:1", 80, 8123, ["API_KEY"], None, [], {}, None, so.options_from(ALL))
    got = newapp.form_values(text, "demo")
    rows = [so.parse_volume(s) for s in ALL["volume"] if not s.startswith("tmpfs")] + [so.parse_volume("tmpfs::/tmp:size=512m")]
    assert got["volumes"] == [{"kind": v.kind, "source": v.source, "target": v.target, "readOnly": v.read_only, "size": v.size} for v in rows]
    assert got["networks"] == [{"name": "internal", "external": False}, {"name": "protonmail_default", "external": True}]
    assert (got["cpus"], got["memory"], got["gpu"]) == ("1.5", "2g", "nvidia")
    assert got["env"] == {"TZ": "Europe/Berlin", "DEBUG": "true", "URL": "http://x:8080/a", "EMPTY": "", "N": "007"}
    assert got["secretVars"] == ["API_KEY"] and got["mount"] == "/data"


def test_reading_back_a_hand_written_service():
    text = """services:
  demo:
    image: x:1
    environment:
      - A=1
      - B=two words
    volumes:
      - type: bind
        source: /mnt/x
        target: /x
        read_only: true
      - cache:/cache
      - /etc/localtime:/etc/localtime:ro
    networks:
      shared: {}
    cpus: 2
    mem_limit: 512m
    devices:
      - /dev/dri/renderD128:/dev/dri/renderD128
networks:
  shared:
    external: true
"""
    got = newapp.form_values(text, "demo")
    assert got["env"] == {"A": "1", "B": "two words"}
    assert [(v["kind"], v["source"], v["target"], v["readOnly"]) for v in got["volumes"]] == [
        ("bind", "/mnt/x", "/x", True), ("named", "cache", "/cache", False), ("bind", "/etc/localtime", "/etc/localtime", True)]
    assert got["networks"] == [{"name": "shared", "external": True}]
    assert (got["cpus"], got["memory"], got["gpu"]) == ("2", "512m", "dri")
