"""Tests for ingressctl.presets with a local fake instead of the network."""
from __future__ import annotations

import pytest

from fakes import session_for
from ingressctl.errors import PresetError
from ingressctl.presets import PRESETS, fetch_preset, resolve_presets
from ingressctl.state import State

GOOD = ["2606:50c0::/32", "140.82.112.0/20", "2a0a:a440::/29", "140.82.112.0/20"]
GOOD_SORTED = ("140.82.112.0/20", "2606:50c0::/32", "2a0a:a440::/29")


class FakeGithubMeta:
    def __init__(self, body=None, status=200):
        self.body = body if body is not None else {"hooks": list(GOOD)}
        self.status = status
        self.requests = []

    def handle(self, method, path, query, headers, body):
        self.requests.append({"method": method, "path": path, "headers": headers})
        return self.status, self.body


def _fetch(fake):
    return fetch_preset(session_for(fake), "github-hooks")


def test_valid_list_is_canonical_sorted_and_deduplicated():
    fake = FakeGithubMeta()

    assert _fetch(fake) == GOOD_SORTED
    request = fake.requests[0]
    assert request["method"] == "GET"
    assert request["path"] == "/meta"
    assert request["headers"]["Accept"] == "application/vnd.github+json"
    assert request["headers"]["User-Agent"] == "ingressctl"
    assert "Authorization" not in request["headers"]


@pytest.mark.parametrize("body", [
    [{"hooks": list(GOOD)}],
    {},
    {"hooks": []},
    {"hooks": [str(i) for i in range(1001)]},
    {"hooks": [1]},
    {"hooks": ["not-a-network"]},
    {"hooks": ["192.30.252.1/22"]},
    {"hooks": ["10.0.0.0/8"]},
    {"hooks": ["0.0.0.0/0"]},
    {"hooks": ["::/0"]},
    {"hooks": ["2000::/3"]},
    {"hooks": ["140.82.112.0/20", "10.0.0.0/8"]},
])
def test_invalid_list_is_rejected_as_a_whole(body):
    with pytest.raises(PresetError):
        _fetch(FakeGithubMeta(body))


@pytest.mark.parametrize("status", [302, 403, 500])
def test_non_200_status_is_a_preset_error(status):
    with pytest.raises(PresetError) as excinfo:
        _fetch(FakeGithubMeta(status=status))
    assert str(status) in str(excinfo.value)


def test_unknown_preset_name_is_rejected():
    with pytest.raises(PresetError):
        fetch_preset(session_for(FakeGithubMeta()), "does-not-exist")


def test_resolve_persists_new_list(tmp_path):
    state = State(tmp_path)
    fake = FakeGithubMeta()

    res = resolve_presets(state, ["github-hooks"], session_for(fake), persist=True)

    assert res.cidrs == {"github-hooks": GOOD_SORTED}
    assert res.errors == {} and res.notes == []
    stored = state.get_preset("github-hooks")
    assert stored["cidrs"] == GOOD_SORTED
    assert stored["source"] == PRESETS["github-hooks"].url
    assert stored["fetched_at"]


def test_resolve_without_persist_does_not_write(tmp_path):
    state = State(tmp_path)

    res = resolve_presets(state, ["github-hooks"], session_for(FakeGithubMeta()), persist=False)

    assert res.cidrs == {"github-hooks": GOOD_SORTED}
    assert state.get_preset("github-hooks") is None


def test_resolve_reports_changed_list(tmp_path):
    state = State(tmp_path)
    state.save_preset("github-hooks", ("140.82.112.0/20", "192.30.252.0/22"), PRESETS["github-hooks"].url)

    res = resolve_presets(state, ["github-hooks"], session_for(FakeGithubMeta()), persist=True)

    assert res.notes == ["preset github-hooks: list changed (+2606:50c0::/32,2a0a:a440::/29, -192.30.252.0/22)"]
    assert state.get_preset("github-hooks")["cidrs"] == GOOD_SORTED


def test_resolve_falls_back_to_last_good_list(tmp_path):
    state = State(tmp_path)
    state.save_preset("github-hooks", ("140.82.112.0/20",), PRESETS["github-hooks"].url)
    before = state.get_preset("github-hooks")

    res = resolve_presets(state, ["github-hooks"], session_for(FakeGithubMeta(status=500)), persist=True)

    assert res.cidrs == {"github-hooks": ("140.82.112.0/20",)}
    assert res.errors == {}
    assert len(res.notes) == 1
    assert "fetch failed" in res.notes[0]
    assert f"using last good copy from {before['fetched_at']}" in res.notes[0]
    assert state.get_preset("github-hooks") == before


def test_resolve_fails_closed_without_stored_list(tmp_path):
    state = State(tmp_path)

    res = resolve_presets(state, ["github-hooks"], session_for(FakeGithubMeta(status=500)), persist=True)

    assert "github-hooks" in res.errors
    assert "github-hooks" not in res.cidrs
    assert state.get_preset("github-hooks") is None


def test_reopened_state_has_presets_table(tmp_path):
    state = State(tmp_path)
    state.save_preset("github-hooks", ("140.82.112.0/20",), PRESETS["github-hooks"].url)
    state.db.close()

    reopened = State(tmp_path)

    assert reopened.get_preset("github-hooks")["cidrs"] == ("140.82.112.0/20",)
    tables = {r["name"] for r in reopened.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "presets" in tables
