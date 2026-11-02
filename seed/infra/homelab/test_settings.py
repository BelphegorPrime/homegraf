"""Tests for the per-installation settings (pytest infra/homelab)."""
import pathlib
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import settings  # noqa: E402


def write(root, name, text):
    (root / "inventory").mkdir(exist_ok=True)
    (root / "inventory" / name).write_text(text)


def test_without_any_file_the_defaults_are_generic(tmp_path):
    assert settings.domain(tmp_path) == "homegraf.lan"
    assert settings.repository(tmp_path) is None
    assert settings.infisical_url(tmp_path) is None
    assert settings.platform(tmp_path) == {}
    assert settings.is_platform("homegraf", tmp_path) and not settings.is_platform("paperless", tmp_path)   # only itself


def test_the_domain_follows_the_setting_then_the_ca_policy(tmp_path):
    write(tmp_path, "ingress.yaml", "issuers:\n  ca:\n    allowed_domains: ['*.lab.example']\n")
    assert settings.domain(tmp_path) == "lab.example"
    write(tmp_path, "homegraf.yaml", "domain: '*.home.example'\n")
    assert settings.domain(tmp_path) == "home.example"            # a leading wildcard is tolerated


def test_a_name_must_match_the_policy_when_there_is_one_else_the_domain(tmp_path):
    assert settings.matches_policy("a.homegraf.lan", tmp_path) and not settings.matches_policy("a.other.lan", tmp_path)
    write(tmp_path, "ingress.yaml", "issuers:\n  ca:\n    allowed_domains: ['*.lab.example', 'x.example']\n")
    assert settings.matches_policy("app.lab.example", tmp_path) and settings.matches_policy("x.example", tmp_path)
    assert not settings.matches_policy("app.other.example", tmp_path)


def test_the_repository_is_the_setting_or_read_from_origin(tmp_path):
    write(tmp_path, "homegraf.yaml", "repository: me/infra\n")
    assert settings.repository(tmp_path) == "me/infra"
    other = tmp_path / "clone"
    other.mkdir()
    subprocess.run(["git", "init", "-q", str(other)], check=True)
    for url, want in (("ssh://git@forge.example:2222/me/lab.git", "me/lab"), ("git@forge.example:me/lab.git", "me/lab"),
                      ("https://forge.example/me/lab", "me/lab"), ("https://forge.example:3000/org/sub/lab.git", "org/sub/lab")):
        subprocess.run(["git", "-C", str(other), "remote", "remove", "origin"], capture_output=True)
        subprocess.run(["git", "-C", str(other), "remote", "add", "origin", url], check=True)
        assert settings.repository(other) == want, url


def test_platform_stacks_by_name_and_prefix_and_the_services_of_the_map(tmp_path):
    write(tmp_path, "homegraf.yaml", "platform_stacks: [infisical]\nplatform_prefixes: ['git--']\nplatform:\n  npm: {label: NPM, stack: proxy, kind: proxy}\n  bare: {}\n")
    assert [settings.is_platform(s, tmp_path) for s in ("infisical", "git--x", "homegraf", "paperless")] == [True, True, True, False]
    assert settings.platform(tmp_path) == {"npm": ("NPM", "proxy", "proxy"), "bare": ("bare", "", "service")}


def test_the_repos_dir_is_a_setting_without_a_default(tmp_path):
    assert settings.repos_dir(tmp_path) is None
    write(tmp_path, "homegraf.yaml", "repos_dir: /srv/data/periphery/repos\n")
    assert settings.repos_dir(tmp_path) == "/srv/data/periphery/repos"
