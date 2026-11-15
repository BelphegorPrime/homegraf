"""Let's-Encrypt-Vertrag von NpmClient gegen FakeNpm (NPM 2.16.0)."""
import pytest

from fakes import LE_META_KEYS, FakeNpm, session_for
from ingressctl.errors import AdapterError
from ingressctl.npm import NpmClient

# meta-Schlüssel aus backend/schema/components/certificate-object.json; der Fake muss sie akzeptieren.
NPM_SCHEMA_META_KEYS = {
    "certificate": "PEM",
    "certificate_key": "PEM",
    "dns_challenge": False,
    "dns_provider_credentials": "dummy-credentials",
    "dns_provider": "cloudflare",
    "letsencrypt_certificate": {},
    "propagation_seconds": 30,
    "key_type": "ecdsa",
}


def client(fake):
    npm = NpmClient("http://npm.test/api", "bot@test", "pw", session=session_for(fake))
    npm.login()
    return npm


def test_schema_key_list_matches_the_fake():
    assert set(NPM_SCHEMA_META_KEYS) == LE_META_KEYS


def test_create_le_http01_overwrites_nice_name_and_hides_key_material():
    fake = FakeNpm()
    npm = client(fake)
    out = npm.create_letsencrypt_certificate(["b.example.org", "a.example.org"], dns_challenge=False)

    assert out["provider"] == "letsencrypt"
    assert out["domain_names"] == ["a.example.org", "b.example.org"]
    assert out["nice_name"] == "a.example.org, b.example.org"  # NPM überschreibt nice_name
    assert out["expires_on"]
    assert out["le_issuer"] == "Fake LE R3"
    assert "PRIVATE KEY" not in repr(out)
    assert "certificate_key" not in out
    assert fake.le_requests[-1]["meta"] == {"dns_challenge": False, "key_type": "ecdsa"}
    assert fake.le_issued == [["a.example.org", "b.example.org"]]
    assert out["id"] in fake.cert_files  # nach dem Reload kann nginx das Zertifikat präsentieren
    # Die Zeile speichert nur expires_on; das certbot-Ergebnis hängt NPM an die Create-Antwort.
    assert fake.certs[out["id"]]["meta"] == {"dns_challenge": False}
    assert "letsencrypt_certificate" not in fake.certs[out["id"]]["meta"]
    assert "certificate" not in fake.certs[out["id"]]["meta"]
    listed = npm.list_certificates()
    assert [c["le_issuer"] for c in listed] == [None]
    assert "PRIVATE KEY" not in repr(listed)


def test_create_le_dns01_passes_provider_and_never_returns_credentials():
    fake = FakeNpm()
    npm = client(fake)
    out = npm.create_letsencrypt_certificate(["x.example.org"], dns_challenge=True,
                                             dns_provider="cloudflare", dns_credentials="token=s3cret",
                                             propagation_seconds=120)

    assert fake.le_requests[-1]["meta"] == {
        "dns_challenge": True,
        "key_type": "ecdsa",
        "dns_provider": "cloudflare",
        "dns_provider_credentials": "token=s3cret",
        "propagation_seconds": 120,
    }
    assert "s3cret" not in repr(out)
    assert "dns_provider_credentials" not in out
    assert out["le_issuer"] == "Fake LE R3"


def test_le_calls_wait_for_certbot(monkeypatch):
    fake = FakeNpm()
    npm = client(fake)
    from ingressctl import npm as npm_mod
    real_request = npm_mod.request
    timeouts = []

    def spy(*args, **kw):
        timeouts.append(kw.get("timeout"))
        return real_request(*args, **kw)

    monkeypatch.setattr(npm_mod, "request", spy)
    out = npm.create_letsencrypt_certificate(["a.example.org"], dns_challenge=False)
    npm.renew_certificate(out["id"])
    assert timeouts == [(5, 900), (5, 900)]


def test_missing_user_email_is_adapter_error():
    fake = FakeNpm()
    fake.user_email = ""
    npm = client(fake)
    with pytest.raises(AdapterError, match="valid email address must be set"):
        npm.create_letsencrypt_certificate(["a.example.org"], dns_challenge=False)
    assert fake.le_issued == []


def test_http01_failure_is_adapter_error():
    fake = FakeNpm()
    fake.http01_ok = False
    npm = client(fake)
    with pytest.raises(AdapterError, match="Certbot failed to authenticate"):
        npm.create_letsencrypt_certificate(["a.example.org"], dns_challenge=False)
    assert fake.le_issued == []


def test_dns01_does_not_need_http01_reachability():
    fake = FakeNpm()
    fake.http01_ok = False
    npm = client(fake)
    out = npm.create_letsencrypt_certificate(["a.example.org"], dns_challenge=True,
                                             dns_provider="cloudflare", dns_credentials="token=x")
    assert out["provider"] == "letsencrypt" and fake.le_issued == [["a.example.org"]]


def test_renew_le_reissues_and_reloads():
    fake = FakeNpm()
    npm = client(fake)
    out = npm.create_letsencrypt_certificate(["a.example.org"], dns_challenge=False)
    first = fake.cert_files[out["id"]]
    first_expiry = fake.certs[out["id"]]["expires_on"]
    renewed = npm.renew_certificate(out["id"])
    assert fake.cert_files[out["id"]] != first
    assert renewed["expires_on"] and fake.certs[out["id"]]["expires_on"] == renewed["expires_on"]
    assert renewed["expires_on"] >= first_expiry
    assert renewed["le_issuer"] is None  # Renew liefert nur expires_on, kein certbot-Ergebnis
    assert "letsencrypt_certificate" not in fake.certs[out["id"]]["meta"]


def test_get_sanitizes_le_without_stored_metadata():
    fake = FakeNpm()
    npm = client(fake)
    out = npm.create_letsencrypt_certificate(["a.example.org"], dns_challenge=True,
                                             dns_provider="cloudflare", dns_credentials="token=s3cret")
    stored = npm.get_certificate(out["id"])
    assert stored["provider"] == "letsencrypt" and stored["expires_on"]
    assert stored["le_issuer"] is None
    assert "s3cret" not in repr(stored) and "dns_provider_credentials" not in stored


def test_renew_rejects_custom_certificate():
    fake = FakeNpm()
    npm = client(fake)
    custom = npm.create_custom_certificate("ingressctl:ns:svc")
    with pytest.raises(AdapterError, match="Only Let'sEncrypt certificates can be renewed"):
        npm.renew_certificate(custom["id"])


def test_http_challenge_is_not_a_mutation():
    fake = FakeNpm()
    npm = client(fake)
    before = npm.mutations
    assert npm.test_http_challenge(["a.example.org", "b.example.org"]) == {
        "a.example.org": "ok", "b.example.org": "ok"}
    assert npm.mutations == before


def test_http_challenge_reports_unreachable_host_without_mutation():
    fake = FakeNpm()
    fake.http01_ok = False
    npm = client(fake)
    before = npm.mutations
    assert npm.test_http_challenge(["a.example.org"]) == {"a.example.org": "no-host"}
    assert npm.mutations == before


@pytest.mark.parametrize("key", sorted(NPM_SCHEMA_META_KEYS))
def test_fake_accepts_every_schema_meta_key(key):
    fake = FakeNpm()
    npm = client(fake)
    resp = npm.session.post("http://npm.test/api/nginx/certificates", json={
        "provider": "letsencrypt", "domain_names": ["a.example.org"],
        "meta": {key: NPM_SCHEMA_META_KEYS[key]}})
    assert resp.status_code == 201, resp.text


def test_fake_rejects_meta_key_outside_the_schema():
    fake = FakeNpm()
    npm = client(fake)
    resp = npm.session.post("http://npm.test/api/nginx/certificates", json={
        "provider": "letsencrypt", "domain_names": ["a.example.org"],
        "meta": {"dns_challenge": False, "intermediate_certificate": "PEM"}})
    assert resp.status_code == 400
    assert fake.le_issued == []
