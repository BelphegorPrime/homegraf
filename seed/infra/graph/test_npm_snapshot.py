"""Tests for the NPM snapshot (pytest infra/graph); NPM itself is not contacted."""
import pathlib
import sys

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import npm_snapshot  # noqa: E402

RAW = [
    {"id": 7, "domain_names": ["b.example.org", "a.example.org"], "forward_scheme": "http", "forward_host": "10.0.0.1",
     "forward_port": 80, "enabled": True, "ssl_forced": True, "allow_websocket_upgrade": False,
     "advanced_config": "secret-token", "certificate_id": 3, "meta": {"x": 1}},
    {"id": 2, "domain_names": ["managed.lab.example"], "forward_scheme": "http", "forward_host": "10.0.0.2", "forward_port": 81, "enabled": True},
    {"id": 3, "domain_names": ["managed.lab.example", "extra.lab.example"], "forward_scheme": "https", "forward_host": "10.0.0.3",
     "forward_port": 443, "enabled": False},
]


def test_only_unmanaged_domains_are_kept_and_sorted():
    hosts = npm_snapshot.select_hosts(RAW, {"managed.lab.example"})
    assert [h["id"] for h in hosts] == [3, 7]
    assert hosts[0]["domains"] == ["extra.lab.example"]        # the managed domain of a mixed host is dropped
    assert hosts[1]["domains"] == ["a.example.org", "b.example.org"]


def test_snapshot_holds_no_nginx_config_certificates_or_meta():
    text = npm_snapshot.render(npm_snapshot.select_hosts(RAW, set()), "2026-01-01")
    assert "secret-token" not in text and "certificate" not in text and "meta" not in text
    assert yaml.safe_load(text)["hosts"][-1]["upstream"] == {"scheme": "http", "host": "10.0.0.1", "port": 80}


def test_env_file_parser_reads_export_lines(tmp_path):
    f = tmp_path / "npm.env"
    f.write_text("# comment\nexport NPM_API_URL=http://x/api\nexport NPM_IDENTITY='me'\nNPM_SECRET=\"s\"\n")
    assert npm_snapshot._env_from_file(f) == {"NPM_API_URL": "http://x/api", "NPM_IDENTITY": "me", "NPM_SECRET": "s"}
