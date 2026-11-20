"""Acceptance criteria (docs/acceptance.md) against the HTTP fakes."""
import datetime as dt
import sqlite3

import pytest

from fakes import FakeGithubMeta, session_for
from ingressctl.cli import _resolve_presets
from ingressctl.errors import AdapterError, CertificateError, ConflictError, IngressError, PendingIssuance
from ingressctl.public import render_public_config
from ingressctl.reconcile import adopt, apply_service, plan_service, recover


def apply(env, stack="omniroute", **kw):
    ctx = env.ctx()
    (spec,) = env.specs(stack)
    kw.setdefault("commit", env.commit_sha)
    return ctx, spec, apply_service(ctx, spec, **kw)


def set_public(env, value: str | None):
    """Set (or remove) the single-line proxy.public block in the Omniroute manifest.

    proxy.public requires a Let's Encrypt issuer, so for these tests the manifest switches to the public
    LE host. Removing the block leaves issuer and domain unchanged (no new certificate).
    """
    path = env.root / "stacks/ai/omniroute/ingress.yaml"
    text = path.read_text()
    if value is not None:
        text = (text.replace("domains: [Omni.Lab.Example.]", "domains: [omni.pub.example]")
                    .replace("certificate: {issuer_ref: homelab-private-ca, validity_days: 90,"
                             " renew_before_days: 30}",
                             "certificate: {issuer_ref: letsencrypt, renew_before_days: 30}"))
    lines = [line for line in text.splitlines() if "public:" not in line]
    if value is not None:
        i = next(i for i, line in enumerate(lines) if "force_https:" in line)
        lines.insert(i + 1, f"      public: {value}")
    path.write_text("\n".join(lines) + "\n")
    return env.commit()


def plan(env, stack="omniroute"):
    ctx = env.ctx()
    (spec,) = env.specs(stack)
    return ctx, spec, plan_service(ctx, spec, ctx.npm["main"].list_proxy_hosts(),
                                   {c["id"]: c for c in ctx.npm["main"].list_certificates()})


def apply_with_presets(env, meta, stack="omniroute", *, persist=True):
    ctx = env.ctx()
    (spec,) = env.specs(stack)
    ctx.le_dns_credentials = "token=le-secret"
    _resolve_presets(ctx, [spec], persist=persist, session=session_for(meta))
    return ctx, spec, apply_service(ctx, spec, commit=env.commit_sha)


def plan_with_presets(env, meta, *, persist, stack="omniroute"):
    ctx = env.ctx()
    (spec,) = env.specs(stack)
    _resolve_presets(ctx, [spec], persist=persist, session=session_for(meta))
    return ctx, spec, plan_service(ctx, spec, ctx.npm["main"].list_proxy_hosts(),
                                   {c["id"]: c for c in ctx.npm["main"].list_certificates()})


def test_new_service_gets_one_host_and_valid_cert(env):
    ctx, spec, res = apply(env)
    assert res == "applied"
    assert len(env.npm.hosts) == 1 and len(env.npm.certs) == 1 and env.inf.issued == 1
    host = next(iter(env.npm.hosts.values()))
    assert host["domain_names"] == ["omni.lab.example"] and host["forward_host"] == "10.0.0.20"
    assert host["ssl_forced"] is True and host["allow_websocket_upgrade"] is True
    row = ctx.state.get("ai-sandbox/omniroute")
    assert env.npm.presented["omni.lab.example"] == row["cert_fingerprint"]
    assert row["reconciled_commit"] == env.commit_sha and row["last_result"] == "ok"


def test_identical_second_apply_is_noop(env):
    apply(env)
    npm_before, inf_before = env.npm.mutations, env.inf.mutations
    _, _, res = apply(env)
    assert res == "noop"
    assert env.npm.mutations == npm_before and env.inf.mutations == inf_before


def test_upstream_change_updates_proxy_keeps_cert(env):
    ctx, _, _ = apply(env)
    cert_before = ctx.state.get("ai-sandbox/omniroute")["npm_cert_id"]
    text = (env.root / "stacks/ai/omniroute/ingress.yaml").read_text().replace("20128", "20200")
    (env.root / "stacks/ai/omniroute/ingress.yaml").write_text(text)
    env.commit()
    ctx, _, res = apply(env)
    assert res == "applied" and env.inf.issued == 1
    host = next(iter(env.npm.hosts.values()))
    assert host["forward_port"] == 20200 and host["certificate_id"] == cert_before


def test_expiring_cert_is_renewed_and_old_object_removed(env):
    ctx, _, _ = apply(env)
    old = ctx.state.get("ai-sandbox/omniroute")
    env.now += dt.timedelta(days=65)  # < 30 days left
    env.inf.not_before = env.now - dt.timedelta(minutes=1)
    ctx, _, res = apply(env, commit=old["reconciled_commit"])
    new = ctx.state.get("ai-sandbox/omniroute")
    assert res == "applied" and env.inf.issued == 2
    assert new["cert_fingerprint"] != old["cert_fingerprint"]
    assert env.npm.presented["omni.lab.example"] == new["cert_fingerprint"]
    assert old["npm_cert_id"] not in env.npm.certs  # cleaned up only after a successful check


def test_short_issuer_validity_does_not_loop(env):
    env.inf.override_days = 9  # the issuer grants a shorter validity than requested
    apply(env)
    env.now += dt.timedelta(days=2)
    _, _, res = apply(env)
    assert res == "noop" and env.inf.issued == 1
    env.now += dt.timedelta(days=5)  # now less than 1/3 of the real lifetime left
    env.inf.not_before = env.now - dt.timedelta(minutes=1)
    _, _, res = apply(env)
    assert res == "applied" and env.inf.issued == 2


def test_manual_host_with_same_domain_is_conflict(env):
    env.npm.add_manual_host(["omni.lab.example"])
    before = env.npm.mutations
    with pytest.raises(ConflictError, match="not assigned to ai-sandbox/omniroute"):
        apply(env)
    assert env.npm.mutations == before and env.inf.issued == 0


def test_explicit_adopt_snapshots_and_takes_over(env):
    hid = env.npm.add_manual_host(["omni.lab.example"])
    ctx = env.ctx()
    (spec,) = env.specs("omniroute")
    adopt(ctx, spec, hid)
    snap = ctx.state.db.execute("SELECT npm_host FROM snapshots").fetchone()[0]
    assert "# manual" in snap
    _, _, res = apply(env)
    host = env.npm.hosts[hid]
    assert res == "applied" and len(env.npm.hosts) == 1
    assert host["forward_port"] == 20128 and host["advanced_config"] == "# manual"  # unmanaged fields stay


def test_adopt_refuses_domain_mismatch(env):
    hid = env.npm.add_manual_host(["omni.lab.example", "other.lab.example"])
    ctx = env.ctx()
    (spec,) = env.specs("omniroute")
    with pytest.raises(ConflictError):
        adopt(ctx, spec, hid)


def test_concurrent_nas_and_ai_stay_separate(env):
    apply(env, "omniroute")
    apply(env, "seerr")
    ctx = env.ctx()
    a, b = ctx.state.get("ai-sandbox/omniroute"), ctx.state.get("nas-compose/seerr")
    assert a["npm_host_id"] != b["npm_host_id"] and a["npm_cert_id"] != b["npm_cert_id"]
    assert len(env.npm.hosts) == 2


def test_timeout_after_host_create_resumes_without_duplicate(env):
    env.npm.fail_after.add(("POST", "/api/nginx/proxy-hosts"))
    with pytest.raises(AdapterError) as e:
        apply(env)
    assert e.value.ambiguous
    _, _, res = apply(env)
    assert res == "applied"
    assert len(env.npm.hosts) == 1 and env.inf.issued == 1


def test_timeout_after_issuance_finds_certificate_instead_of_reissuing(env):
    env.inf.fail_after_issue = True
    with pytest.raises(AdapterError):
        apply(env)
    _, _, res = apply(env)
    assert res == "applied" and env.inf.issued == 1


def test_timeout_after_npm_cert_create_reuses_object(env):
    env.npm.fail_after.add(("POST", "/api/nginx/certificates"))
    with pytest.raises(AdapterError):
        apply(env)
    apply(env)
    assert len(env.npm.certs) == 1 and env.inf.issued == 1


def test_pending_issuance_is_resumed(env):
    env.inf.pending_next = True
    with pytest.raises(PendingIssuance):
        apply(env)
    with pytest.raises(PendingIssuance):
        apply(env)
    env.inf.release_pending()
    _, _, res = apply(env)
    assert res == "applied" and env.inf.issued == 1


def test_bad_certificate_is_not_switched(env):
    ctx, _, _ = apply(env)
    old_fp = ctx.state.get("ai-sandbox/omniroute")["cert_fingerprint"]
    env.now += dt.timedelta(days=65)
    env.inf.not_before = env.now - dt.timedelta(minutes=1)
    env.inf.wrong_san = True
    with pytest.raises(CertificateError):
        apply(env)
    assert env.npm.presented["omni.lab.example"] == old_fp
    assert len(env.npm.certs) == 1


def test_failed_verification_rolls_back_to_old_cert(env, monkeypatch):
    ctx, _, _ = apply(env)
    old = ctx.state.get("ai-sandbox/omniroute")
    env.now += dt.timedelta(days=65)
    env.inf.not_before = env.now - dt.timedelta(minutes=1)
    from ingressctl.checks import TlsResult
    monkeypatch.setattr(env.checker, "https", lambda *a, **k: TlsResult(False, None, None, "TLS broken"))
    with pytest.raises(IngressError, match="switched back to previous certificate"):
        apply(env)
    host = env.npm.hosts[old["npm_host_id"]]
    assert host["certificate_id"] == old["npm_cert_id"]
    assert env.npm.presented["omni.lab.example"] == old["cert_fingerprint"]


def test_unreachable_npm_gives_clear_error_and_deletes_nothing(env):
    apply(env)
    hosts, certs = dict(env.npm.hosts), dict(env.npm.certs)
    env.npm.reachable = False
    with pytest.raises(AdapterError, match="NPM: unreachable"):
        apply(env)
    env.npm.reachable = True
    assert env.npm.hosts == hosts and env.npm.certs == certs


def test_unreachable_upstream_blocks_before_mutation(env):
    env.checker.upstream_ok = False
    with pytest.raises(IngressError, match="upstream unreachable"):
        apply(env)
    assert env.npm.mutations == 0 and env.inf.issued == 0


def test_late_webhook_does_not_roll_back(env):
    old_commit = env.commit_sha
    text = (env.root / "stacks/ai/omniroute/ingress.yaml").read_text().replace("20128", "20300")
    (env.root / "stacks/ai/omniroute/ingress.yaml").write_text(text)
    new_commit = env.commit()
    apply(env, commit=new_commit)
    (env.root / "stacks/ai/omniroute/ingress.yaml").write_text(text.replace("20300", "20128"))  # checkout of A
    _, _, res = apply(env, commit=old_commit)
    assert res == "skipped"
    assert next(iter(env.npm.hosts.values()))["forward_port"] == 20300


def test_deployed_commit_mismatch_aborts(env):
    with pytest.raises(IngressError, match="Komodo deployed"):
        apply(env, deployed_commit="0" * 40)
    assert env.npm.mutations == 0


def test_removed_manifest_deletes_nothing(env):
    apply(env)
    (env.root / "stacks/ai/omniroute/ingress.yaml").unlink()
    env.commit()
    assert env.specs("omniroute") == []
    apply(env, "seerr")
    assert len(env.npm.hosts) == 2


def test_partial_success_is_recorded_per_service(env):
    apply(env, "omniroute")
    env.checker.upstream_ok = False
    with pytest.raises(IngressError):
        apply(env, "seerr")
    ctx = env.ctx()
    assert ctx.state.get("ai-sandbox/omniroute")["reconciled_commit"] == env.commit_sha
    assert ctx.state.get("nas-compose/seerr")["reconciled_commit"] is None
    assert ctx.state.get("nas-compose/seerr")["last_result"] == "error"


def test_manual_drift_is_reported_and_restored(env):
    ctx, spec, _ = apply(env)
    hid = ctx.state.get("ai-sandbox/omniroute")["npm_host_id"]
    env.npm.hosts[hid]["forward_port"] = 9999
    p = plan_service(ctx, spec, ctx.npm["main"].list_proxy_hosts(),
                     {c["id"]: c for c in ctx.npm["main"].list_certificates()})
    assert any("forward_port" in d for d in p.drift)
    apply(env)
    assert env.npm.hosts[hid]["forward_port"] == 20128


def test_state_restore_requires_marked_certificate(env, tmp_path):
    ctx, spec, _ = apply(env)
    row = ctx.state.get("ai-sandbox/omniroute")
    env.state_dir = tmp_path / "restored"
    ctx2 = env.ctx()
    foreign = env.npm.add_manual_host(["x.lab.example"])
    with pytest.raises(ConflictError):
        recover(ctx2, spec, foreign, None)
    recover(ctx2, spec, row["npm_host_id"], row["npm_cert_id"])
    assert ctx2.state.get("ai-sandbox/omniroute")["cert_fingerprint"] == row["cert_fingerprint"]
    env.npm.certs[999] = {"id": 999, "provider": "other", "nice_name": "manual", "domain_names": [], "meta": {}}
    with pytest.raises(ConflictError, match="marker"):
        recover(ctx2, spec, row["npm_host_id"], 999)


def test_no_private_keys_in_state_or_logs(env, capsys):
    ctx, _, _ = apply(env)
    err = capsys.readouterr().err
    assert "PRIVATE KEY" not in err and "jwt-test" not in err and "inf-token" not in err
    dump = "\n".join(sqlite3.connect(ctx.state.dir / "state.sqlite3").iterdump())
    assert "PRIVATE KEY" not in dump
    assert list((ctx.state.dir / "pending").iterdir()) == []  # pending key removed after completion


def test_npm_certificate_reads_drop_private_key(env):
    ctx, _, _ = apply(env)
    for c in ctx.npm["main"].list_certificates():
        assert "meta" not in c and "certificate_key" not in str(c)


def test_verification_retries_until_nginx_reloaded(env, monkeypatch):
    """NPM answers before the nginx reload has finished; the first check may fail."""
    real = env.checker.https
    calls = []

    def flaky(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            from ingressctl.checks import TlsResult
            return TlsResult(False, None, None, "SSLError")
        return real(*a, **k)

    monkeypatch.setattr(env.checker, "https", flaky)
    _, _, res = apply(env)
    assert res == "applied" and len(calls) == 2 and env.inf.issued == 1


def test_failed_first_apply_resumes_without_reissue(env, monkeypatch):
    from ingressctl.checks import TlsResult
    monkeypatch.setattr(env.checker, "https", lambda *a, **k: TlsResult(False, None, None, "SSLError"))
    with pytest.raises(IngressError):
        apply(env)
    monkeypatch.undo()
    _, _, res = apply(env)
    assert res == "applied" and env.inf.issued == 1 and len(env.npm.certs) == 1


def test_cli_apply_all_uses_checkout_commit(env, monkeypatch, capsys):
    """Komodo procedure ingress-apply: `apply --all` without --commit uses the HEAD of the checkout."""
    import json as _json
    from ingressctl import cli
    ctx = env.ctx()
    monkeypatch.setattr(cli, "build_context", lambda root, inv, specs: ctx)
    monkeypatch.setenv("INGRESSCTL_STATE_DIR", str(env.state_dir))
    assert cli.main(["apply", "--root", str(env.root), "--all"]) == 0
    out = _json.loads(capsys.readouterr().out)
    assert set(out) == {"ai-sandbox/omniroute", "nas-compose/seerr"}
    assert ctx.state.get("ai-sandbox/omniroute")["reconciled_commit"] == env.commit_sha


def test_remove_deletes_host_and_cert_after_manifest_is_gone(env, monkeypatch):
    from ingressctl import cli
    ctx = env.ctx()
    monkeypatch.setattr(cli, "NpmClient", lambda *a, **k: ctx.npm["main"])
    monkeypatch.setenv("INGRESSCTL_STATE_DIR", str(env.state_dir))
    monkeypatch.setenv("NPM_IDENTITY", "x")
    monkeypatch.setenv("NPM_SECRET", "x")
    apply(env)
    rid = "ai-sandbox/omniroute"
    args = type("A", (), {"root": str(env.root), "resource": rid})()
    with pytest.raises(IngressError, match="still declared in a manifest"):
        cli.cmd_remove(args)
    (env.root / "stacks/ai/omniroute/ingress.yaml").unlink()
    env.commit()
    row = ctx.state.get(rid)
    env.npm.hosts[row["npm_host_id"]]["domain_names"].append("fremd.lab.example")  # Host wurde manuell erweitert
    with pytest.raises(IngressError, match="nothing deleted"):
        cli.cmd_remove(args)
    env.npm.hosts[row["npm_host_id"]]["domain_names"].remove("fremd.lab.example")
    assert cli.cmd_remove(args) == 0
    assert row["npm_host_id"] not in env.npm.hosts and row["npm_cert_id"] not in env.npm.certs
    assert ctx.state.get(rid) is None
    with pytest.raises(IngressError, match="is not in the state"):
        cli.cmd_remove(args)


def test_forget_requires_gone_host_and_no_manifest(env, monkeypatch):
    from ingressctl import cli
    ctx = env.ctx()
    monkeypatch.setattr(cli, "NpmClient", lambda *a, **k: ctx.npm["main"])
    monkeypatch.setenv("INGRESSCTL_STATE_DIR", str(env.state_dir))
    monkeypatch.setenv("NPM_IDENTITY", "x")
    monkeypatch.setenv("NPM_SECRET", "x")
    apply(env)
    rid = "ai-sandbox/omniroute"
    with pytest.raises(IngressError, match="still declared in a manifest"):
        cli.cmd_forget(type("A", (), {"root": str(env.root), "resource": rid})())
    (env.root / "stacks/ai/omniroute/ingress.yaml").unlink()
    env.commit()
    with pytest.raises(IngressError, match="still exists"):
        cli.cmd_forget(type("A", (), {"root": str(env.root), "resource": rid})())
    row = ctx.state.get(rid)
    del env.npm.hosts[row["npm_host_id"]]
    del env.npm.certs[row["npm_cert_id"]]
    assert cli.cmd_forget(type("A", (), {"root": str(env.root), "resource": rid})()) == 0
    assert ctx.state.get(rid) is None


# --- proxy.public: advanced_config ------------------------------------------------------------

def test_public_block_writes_managed_advanced_config(env):
    set_public(env, '{paths: [/hook/abc, "/files/*"], methods: [post]}')
    ctx, spec, res = apply(env)
    assert res == "applied"
    host = next(iter(env.npm.hosts.values()))
    assert host["advanced_config"] == render_public_config(spec.public)
    assert ctx.state.get("ai-sandbox/omniroute")["public_config"] == host["advanced_config"]


def test_public_second_apply_is_noop(env):
    set_public(env, "{paths: [/hook/abc]}")
    apply(env)
    before = env.npm.mutations
    _, _, res = apply(env)
    assert res == "noop" and env.npm.mutations == before


def test_changed_public_paths_update_advanced_config_without_new_cert(env):
    set_public(env, "{paths: [/hook/abc]}")
    ctx, _, _ = apply(env)
    row = ctx.state.get("ai-sandbox/omniroute")
    set_public(env, '{paths: [/hook/other, "/files/*"], methods: [post]}')
    ctx, spec, res = apply(env)
    host = env.npm.hosts[row["npm_host_id"]]
    assert res == "applied" and len(env.npm.le_issued) == 1
    assert host["advanced_config"] == render_public_config(spec.public)
    assert host["certificate_id"] == row["npm_cert_id"]


def test_hand_edit_of_managed_advanced_config_is_drift_and_restored(env):
    set_public(env, "{paths: [/hook/abc]}")
    ctx, _, _ = apply(env)
    hid = ctx.state.get("ai-sandbox/omniroute")["npm_host_id"]
    good = env.npm.hosts[hid]["advanced_config"]
    env.npm.hosts[hid]["advanced_config"] = good.replace("/hook/abc", "/hook/evil")
    _, _, p = plan(env)
    assert any("advanced_config" in d for d in p.drift)
    apply(env)
    assert env.npm.hosts[hid]["advanced_config"] == good


def test_hand_made_advanced_config_is_conflict_naming_host(env):
    ctx, _, _ = apply(env)  # still without public:
    hid = ctx.state.get("ai-sandbox/omniroute")["npm_host_id"]
    env.npm.hosts[hid]["advanced_config"] = "# manual\nadd_header X-Test 1;"
    set_public(env, "{paths: [/hook/abc]}")
    before = env.npm.mutations
    with pytest.raises(ConflictError, match=rf"NPM host {hid}.*advanced_config"):
        apply(env)
    assert env.npm.mutations == before
    assert env.npm.hosts[hid]["advanced_config"].startswith("# manual")


def test_removing_public_block_clears_only_managed_config(env):
    set_public(env, "{paths: [/hook/abc]}")
    ctx, _, _ = apply(env)
    hid = ctx.state.get("ai-sandbox/omniroute")["npm_host_id"]
    assert env.npm.hosts[hid]["advanced_config"]
    set_public(env, None)
    ctx, spec, res = apply(env)
    assert res == "applied" and spec.public is None
    assert env.npm.hosts[hid]["advanced_config"] == ""
    assert ctx.state.get("ai-sandbox/omniroute")["public_config"] is None


def test_removing_public_block_keeps_hand_added_lines(env):
    set_public(env, "{paths: [/hook/abc]}")
    ctx, _, _ = apply(env)
    hid = ctx.state.get("ai-sandbox/omniroute")["npm_host_id"]
    env.npm.hosts[hid]["advanced_config"] += "\nadd_header X-Manual 1;"
    set_public(env, None)
    apply(env)
    assert env.npm.hosts[hid]["advanced_config"] == "add_header X-Manual 1;"


def test_manifest_without_public_leaves_advanced_config_untouched(env):
    apply(env)
    host = next(iter(env.npm.hosts.values()))
    assert host["advanced_config"] == ""
    before = env.npm.mutations
    _, _, res = apply(env)
    assert res == "noop" and env.npm.mutations == before


def test_plan_shows_advanced_config_diff(env):
    set_public(env, "{paths: [/hook/abc]}")
    apply(env)
    set_public(env, "{paths: [/hook/other]}")
    _, _, p = plan(env)
    text = p.render()
    assert "advanced_config" in text and "/hook/abc" in text and "/hook/other" in text


# --- proxy.public: resolved IP presets -------------------------------------------------------

def test_preset_resolves_into_host_and_state(env):
    meta = FakeGithubMeta()
    set_public(env, "{paths: [/hook], allow_cidrs: [{preset: github-hooks}]}")

    ctx, spec, res = apply_with_presets(env, meta)

    assert res == "applied"
    advanced = next(iter(env.npm.hosts.values()))["advanced_config"]
    for cidr in meta.hooks:
        assert f"allow {cidr};" in advanced
    assert advanced.rstrip().endswith("deny all;")
    assert ctx.state.get_preset("github-hooks")["cidrs"] == tuple(sorted(meta.hooks))


def test_second_apply_with_same_preset_list_is_noop(env):
    meta = FakeGithubMeta()
    set_public(env, "{paths: [/hook], allow_cidrs: [{preset: github-hooks}]}")
    apply_with_presets(env, meta)
    before = env.npm.mutations

    _, _, res = apply_with_presets(env, meta)

    assert res == "noop"
    assert env.npm.mutations == before


def test_changed_preset_list_plans_without_writing_then_applies_as_update(env):
    meta = FakeGithubMeta()
    set_public(env, "{paths: [/hook], allow_cidrs: [{preset: github-hooks}]}")
    ctx, _, res = apply_with_presets(env, meta)
    assert res == "applied"
    hid = ctx.state.get("ai-sandbox/omniroute")["npm_host_id"]
    old_config = env.npm.hosts[hid]["advanced_config"]
    new_hooks = ["140.82.112.0/20", "192.30.252.0/22"]
    meta.hooks = list(new_hooks)

    plan_ctx, _, p = plan_with_presets(env, meta, persist=False)

    assert "advanced_config" in p.host_changes
    assert any("Preset-Netze aktualisiert" in note for note in p.notes)
    assert not any("manuell" in d for d in p.drift)
    assert plan_ctx.state.get_preset("github-hooks")["cidrs"] == tuple(sorted(FakeGithubMeta.DEFAULT_HOOKS))
    assert env.npm.hosts[hid]["advanced_config"] == old_config

    apply_ctx, spec, p2 = plan_with_presets(env, meta, persist=True)
    assert not any("advanced_config" in d for d in p2.drift)
    assert apply_service(apply_ctx, spec, commit=env.commit_sha) == "applied"
    updated = env.npm.hosts[hid]["advanced_config"]
    for cidr in new_hooks:
        assert f"allow {cidr};" in updated
    assert "2606:50c0::/32" not in updated
    assert apply_ctx.state.get_preset("github-hooks")["cidrs"] == tuple(sorted(new_hooks))


def test_failed_fetch_uses_stored_list_and_notes(env):
    meta = FakeGithubMeta()
    set_public(env, "{paths: [/hook], allow_cidrs: [{preset: github-hooks}]}")
    apply_with_presets(env, meta)
    before = env.npm.mutations
    meta.fail = True

    ctx, spec, p = plan_with_presets(env, meta, persist=True)

    assert any("fetch failed" in note for note in p.notes)
    assert not p.host_changes
    assert apply_service(ctx, spec, commit=env.commit_sha) == "noop"
    assert env.npm.mutations == before


def test_failed_fetch_without_stored_list_fails_closed(env):
    meta = FakeGithubMeta(fail=True)
    set_public(env, "{paths: [/hook], allow_cidrs: [{preset: github-hooks}]}")

    ctx, spec, p = plan_with_presets(env, meta, persist=True)

    assert any("preset 'github-hooks' unavailable and no saved value" in blocker for blocker in p.blockers)
    assert any("fail closed" in blocker for blocker in p.blockers)
    with pytest.raises(ConflictError, match="fail closed"):
        apply_service(ctx, spec, commit=env.commit_sha)
    assert env.npm.hosts == {}
    assert ctx.state.get_preset("github-hooks") is None


def test_static_and_preset_networks_render_together(env):
    meta = FakeGithubMeta()
    inventory = env.root / "inventory/ingress.yaml"
    inventory.write_text(inventory.read_text().replace("dns_challenge: false", "dns_challenge: true")
                         .replace("dns_provider: null", "dns_provider: cloudflare"))
    set_public(env, "{paths: [/hook], allow_cidrs: [{preset: github-hooks}, 203.0.113.0/24]}")

    apply_with_presets(env, meta)

    advanced = next(iter(env.npm.hosts.values()))["advanced_config"]
    for cidr in [*meta.hooks, "203.0.113.0/24"]:
        assert f"allow {cidr};" in advanced
    assert advanced.rstrip().endswith("deny all;")


def test_manifest_without_presets_makes_no_preset_request(env):
    meta = FakeGithubMeta()
    ctx = env.ctx()

    _resolve_presets(ctx, env.specs(), persist=True, session=session_for(meta))

    assert meta.requests == []
    assert ctx.preset_cidrs == {}


def test_hand_edit_of_managed_advanced_config_with_presets_is_drift(env):
    meta = FakeGithubMeta()
    set_public(env, "{paths: [/hook], allow_cidrs: [{preset: github-hooks}]}")
    ctx, _, _ = apply_with_presets(env, meta)
    hid = ctx.state.get("ai-sandbox/omniroute")["npm_host_id"]
    good = env.npm.hosts[hid]["advanced_config"]
    env.npm.hosts[hid]["advanced_config"] = good.replace(meta.hooks[0], "198.51.100.0/24")

    _, _, p = plan_with_presets(env, meta, persist=False)

    assert any("advanced_config" in d for d in p.drift)
    apply_with_presets(env, meta)
    assert env.npm.hosts[hid]["advanced_config"] == good
