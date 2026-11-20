"""Let's Encrypt via NPM: plan/apply/renew/adopt (slice 4), using FakeNpm and FakeChecker."""
import datetime as dt
import json

import pytest
import yaml

from fakes import session_for
from ingressctl.errors import AdapterError, ConflictError, IngressError
from ingressctl.inventory import load_inventory
from ingressctl.manifest import load_all
from ingressctl.npm import NpmClient
from ingressctl.reconcile import adopt, apply_service, plan_service

LE_MANIFEST = """\
apiVersion: homelab.ingress/v1
namespace: ai-sandbox
services:
  - id: omniroute
    stack: omniroute
    lifecycle: present
    proxy:
      domains: [app.pub.example]
      upstream: {scheme: http, server: worker, port: 20128}
      websockets: true
      force_https: true
%s    certificate: {%s}
    healthcheck: {path: /, accepted_statuses: [200, 302, 401]}
"""


def use_le(env, cert="issuer_ref: letsencrypt", proxy_extra=""):
    env.write("stacks/ai/omniroute/ingress.yaml", LE_MANIFEST % (proxy_extra, cert))
    env.commit()
    return env.specs("omniroute")[0]


def set_issuer(env, **fields):
    path = env.root / "inventory/ingress.yaml"
    doc = yaml.safe_load(path.read_text())
    doc["issuers"]["letsencrypt"].update(fields)
    path.write_text(yaml.safe_dump(doc))


def apply(env, **kw):
    ctx = env.ctx()
    (spec,) = env.specs("omniroute")
    kw.setdefault("commit", env.commit_sha)
    return ctx, spec, apply_service(ctx, spec, **kw)


def plan(env):
    ctx = env.ctx()
    (spec,) = env.specs("omniroute")
    return ctx, spec, plan_service(ctx, spec, ctx.npm["main"].list_proxy_hosts(),
                                   {c["id"]: c for c in ctx.npm["main"].list_certificates()})


def test_new_le_service_gets_one_host_and_one_le_certificate(env):
    use_le(env)
    ctx, spec, res = apply(env)
    assert res == "applied"
    assert len(env.npm.hosts) == 1 and len(env.npm.certs) == 1
    assert env.npm.le_issued == [["app.pub.example"]]
    cert = next(iter(env.npm.certs.values()))
    assert cert["provider"] == "letsencrypt"
    host = next(iter(env.npm.hosts.values()))
    assert host["domain_names"] == ["app.pub.example"]
    assert host["certificate_id"] == cert["id"] and host["ssl_forced"] is True
    row = ctx.state.get("ai-sandbox/omniroute")
    assert row["npm_cert_id"] == cert["id"]
    assert row["infisical_cert_id"] is None
    assert row["cert_issuer"] == "Fake LE R3"
    assert row["cert_not_after"].startswith("20") and "T" in row["cert_not_after"]
    assert row["cert_fingerprint"] == env.npm.presented["app.pub.example"]
    assert row["policy_hash"] == spec.policy_hash()
    assert ctx.checker.ca_bundles and set(ctx.checker.ca_bundles) == {None}  # System-Trust-Store


def test_plan_for_new_le_service_is_read_only(env):
    use_le(env)
    ctx, spec, p = plan(env)
    text = p.render()
    assert "+ Create proxy host" in text
    assert "* Issue certificate: no certificate of our own yet" in text
    assert not p.blockers and not p.renew_cert
    assert env.npm.le_issued == [] and env.npm.hosts == {} and env.npm.certs == {}


def test_second_apply_is_noop(env):
    use_le(env)
    apply(env)
    host_before = dict(next(iter(env.npm.hosts.values())))
    certs_before = dict(env.npm.certs)
    mutations = env.npm.mutations
    _, _, res = apply(env)
    assert res == "noop"
    assert env.npm.mutations == mutations
    assert env.npm.hosts[host_before["id"]] == host_before and env.npm.certs == certs_before
    assert len(env.npm.le_issued) == 1


def test_http01_precheck_failure_blocks_before_any_mutation(env):
    use_le(env)
    env.npm.http01_ok = False
    with pytest.raises(IngressError) as e:
        apply(env)
    msg = str(e.value)
    assert "app.pub.example" in msg and "no-host" in msg
    assert "make port 80 reachable from the internet or set certificate.dns_challenge: true" in msg
    assert env.npm.hosts == {} and env.npm.certs == {}
    assert env.npm.le_issued == [] and env.npm.le_requests == []


def test_dns01_skips_precheck_and_passes_provider(env, monkeypatch, capsys):
    set_issuer(env, dns_provider="cloudflare", propagation_seconds=120)
    use_le(env, cert="issuer_ref: letsencrypt, dns_challenge: true")
    ctx = env.ctx()
    ctx.le_dns_credentials = "token=le-secret"

    def no_http01(domains):
        raise AssertionError("DNS-01 must not call the HTTP-01 check")

    monkeypatch.setattr(ctx.npm["main"], "test_http_challenge", no_http01)
    (spec,) = env.specs("omniroute")
    assert apply_service(ctx, spec, commit=env.commit_sha) == "applied"
    assert env.npm.le_requests[-1]["meta"] == {
        "dns_challenge": True, "key_type": "ecdsa", "dns_provider": "cloudflare",
        "dns_provider_credentials": "token=le-secret", "propagation_seconds": 120,
    }
    assert "token=le-secret" not in capsys.readouterr().err
    assert "token=le-secret" not in json.dumps(ctx.state.get("ai-sandbox/omniroute"))


def test_dns01_without_credentials_is_a_blocker(env):
    set_issuer(env, dns_provider="cloudflare")
    use_le(env, cert="issuer_ref: letsencrypt, dns_challenge: true")
    _, _, p = plan(env)
    assert "LETSENCRYPT_DNS_CREDENTIALS is missing" in p.blockers
    with pytest.raises(ConflictError, match="LETSENCRYPT_DNS_CREDENTIALS"):
        apply(env)
    assert env.npm.le_issued == [] and env.npm.hosts == {} and env.npm.certs == {}


def test_validate_context_reports_no_dns_credential_blocker(env):
    """`ingressctl validate` has no NPM clients and must not require credentials offline."""
    from ingressctl.reconcile import Context, _static_blockers
    set_issuer(env, dns_provider="cloudflare")
    use_le(env, cert="issuer_ref: letsencrypt, dns_challenge: true")
    inv = load_inventory(env.root)
    (spec,) = [s for s in load_all(env.root, inv) if s.stack_id == "omniroute"]
    ctx = Context(root=env.root, inv=inv, state=None, npm={}, issuers={}, checker=None)
    assert not [b for b in _static_blockers(ctx, spec) if "LETSENCRYPT_DNS_CREDENTIALS" in b]


def test_build_context_needs_no_infisical_for_le_and_reads_dns_credentials(env, monkeypatch):
    from ingressctl import cli
    set_issuer(env, dns_provider="cloudflare")
    use_le(env, cert="issuer_ref: letsencrypt, dns_challenge: true")
    for name in ("INFISICAL_CLIENT_ID", "INFISICAL_CLIENT_SECRET",
                 "INFISICAL_CLIENT_ID_FILE", "INFISICAL_CLIENT_SECRET_FILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NPM_IDENTITY", "x")
    monkeypatch.setenv("NPM_SECRET", "x")
    monkeypatch.setenv("INGRESSCTL_STATE_DIR", str(env.state_dir))
    monkeypatch.setenv("LETSENCRYPT_DNS_CREDENTIALS", "token=le-secret")
    monkeypatch.setattr(cli, "NpmClient", lambda *a, **k: env.ctx().npm["main"])
    inv = load_inventory(env.root)
    specs = [s for s in load_all(env.root, inv) if s.stack_id == "omniroute"]
    ctx = cli.build_context(env.root, inv, specs)
    assert specs[0].issuer_ref not in ctx.issuers  # no Infisical client for Let's Encrypt
    assert ctx.le_dns_credentials == "token=le-secret"
    use_le(env)  # the secret is not read without dns_challenge
    specs = [s for s in load_all(env.root, inv) if s.stack_id == "omniroute"]
    assert cli.build_context(env.root, inv, specs).le_dns_credentials is None


def test_timeout_after_le_post_resumes_without_second_issuance(env):
    use_le(env)
    env.npm.fail_after.add(("POST", "/api/nginx/certificates"))
    with pytest.raises(AdapterError) as e:
        apply(env)
    assert e.value.ambiguous
    assert len(env.npm.certs) == 1 and len(env.npm.le_issued) == 1
    ctx, _, res = apply(env)
    assert res == "applied"
    assert len(env.npm.certs) == 1 and len(env.npm.le_issued) == 1
    assert ctx.state.get("ai-sandbox/omniroute")["npm_cert_id"] == next(iter(env.npm.certs))


def test_foreign_unused_le_certificate_is_not_adopted(env):
    use_le(env)
    other = NpmClient("http://npm.test/api", "bot@test", "pw", session=session_for(env.npm))
    other.login()
    foreign = other.create_letsencrypt_certificate(["app.pub.example"], dns_challenge=False)
    ctx, _, res = apply(env)
    assert res == "applied"
    row = ctx.state.get("ai-sandbox/omniroute")
    assert row["npm_cert_id"] not in (None, foreign["id"])
    assert foreign["id"] in env.npm.certs
    assert len(env.npm.le_issued) == 2  # das fremde plus das eigene von ingressctl


def test_expiring_le_certificate_is_renewed_in_place(env):
    use_le(env)
    ctx, _, _ = apply(env)
    row = ctx.state.get("ai-sandbox/omniroute")
    env.now += dt.timedelta(days=65)  # < 30 Tage Restlaufzeit
    _, _, p = plan(env)
    assert p.renew_cert and not p.cert_reasons
    until = row["cert_not_after"][:10]
    assert f"  * Renew Let's Encrypt certificate (expires on {until})" in p.render()
    host_before = dict(env.npm.hosts[row["npm_host_id"]])
    ctx, _, res = apply(env)
    assert res == "applied"
    assert env.npm.hosts[row["npm_host_id"]] == host_before  # host remains unchanged
    assert len(env.npm.certs) == 1 and len(env.npm.le_issued) == 1
    updated = ctx.state.get("ai-sandbox/omniroute")
    assert updated["npm_cert_id"] == row["npm_cert_id"]
    assert updated["cert_not_after"] >= row["cert_not_after"]
    assert updated["cert_fingerprint"] == env.npm.presented["app.pub.example"]
    assert updated["cert_fingerprint"] != row["cert_fingerprint"]


def test_adopt_switches_to_own_le_certificate_and_keeps_the_hand_made_one(env):
    use_le(env)
    env.npm.certs[999] = {"id": 999, "provider": "other", "nice_name": "handgemacht",
                          "domain_names": ["app.pub.example"], "expires_on": "2099-01-01 00:00:00",
                          "meta": {}}
    hid = env.npm.add_manual_host(["app.pub.example"])
    env.npm.hosts[hid]["certificate_id"] = 999
    ctx = env.ctx()
    (spec,) = env.specs("omniroute")
    adopt(ctx, spec, hid)
    assert ctx.state.get("ai-sandbox/omniroute")["npm_cert_id"] is None
    ctx, _, res = apply(env)
    assert res == "applied"
    row = ctx.state.get("ai-sandbox/omniroute")
    assert env.npm.hosts[hid]["certificate_id"] == row["npm_cert_id"] != 999
    assert 999 in env.npm.certs  # manually created certificate is never deleted
    assert len(env.npm.le_issued) == 1


def test_hand_made_host_with_le_domain_is_a_conflict(env):
    use_le(env)
    env.npm.add_manual_host(["app.pub.example"])
    with pytest.raises(ConflictError, match="adoption only via 'ingressctl adopt'"):
        apply(env)
    assert env.npm.le_issued == [] and env.npm.certs == {}


def test_renew_all_deployed_works_for_le_service(env, monkeypatch):
    from ingressctl import cli
    use_le(env)
    ctx = env.ctx()
    monkeypatch.setattr(cli, "build_context", lambda root, inv, specs: ctx)
    monkeypatch.setenv("INGRESSCTL_STATE_DIR", str(env.state_dir))
    assert cli.main(["apply", "--root", str(env.root), "--stack", "omniroute"]) == 0
    assert env.npm.le_issued == [["app.pub.example"]]
    old_fp = env.npm.presented["app.pub.example"]
    env.now += dt.timedelta(days=65)
    assert cli.main(["renew", "--all-deployed", "--inventory-root", str(env.root),
                     "--root", str(env.root)]) == 0
    assert env.npm.presented["app.pub.example"] != old_fp
    assert len(env.npm.certs) == 1 and len(env.npm.le_issued) == 1
