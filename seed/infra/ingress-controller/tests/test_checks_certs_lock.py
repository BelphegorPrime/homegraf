import datetime as dt
import http.server
import ssl
import threading
import time

import pytest
from cryptography.hazmat.primitives import serialization

from fakes import FakeCa
from ingressctl import certs
from ingressctl.checks import Checker
from ingressctl.errors import CertificateError, IngressError
from ingressctl.lock import npm_lock

NOW = dt.datetime.now(dt.timezone.utc)


def _issue(ca, domains, days=90, eku=True):
    key_pem = certs.generate_key_pem()
    key = serialization.load_pem_private_key(key_pem, None)
    return key_pem, ca.sign(key.public_key(), list(domains), days, eku=eku)


def test_validate_issued_accepts_good_certificate():
    ca = FakeCa()
    key, pem = _issue(ca, ["a.lab.example", "b.lab.example"])
    info = certs.validate_issued(pem, ca.pem, key, ("a.lab.example", "b.lab.example"), ca.pem, NOW)
    assert info.sans == ("a.lab.example", "b.lab.example")


def test_validate_rejects_wrong_key():
    ca = FakeCa()
    _, pem = _issue(ca, ["a.lab.example"])
    with pytest.raises(CertificateError, match="Schlüssel"):
        certs.validate_issued(pem, ca.pem, certs.generate_key_pem(), ("a.lab.example",), ca.pem, NOW)


def test_validate_rejects_untrusted_chain():
    ca, other = FakeCa(), FakeCa("Other Root")
    key, pem = _issue(ca, ["a.lab.example"])
    with pytest.raises(CertificateError, match="Kette"):
        certs.validate_issued(pem, ca.pem, key, ("a.lab.example",), other.pem, NOW)


def test_validate_rejects_missing_san():
    ca = FakeCa()
    key, pem = _issue(ca, ["a.lab.example"])
    with pytest.raises(CertificateError, match="SANs"):
        certs.validate_issued(pem, ca.pem, key, ("a.lab.example", "b.lab.example"), ca.pem, NOW)


def test_renewal_lead_is_capped_by_lifetime():
    ca = FakeCa()
    _, pem = _issue(ca, ["a.lab.example"], days=9)
    info = certs.info_from_pem(pem)
    assert certs.renewal_lead(info, 30) == info.lifetime / 3
    assert not certs.needs_renewal(info, 30, NOW)
    assert certs.needs_renewal(info, 30, NOW + dt.timedelta(days=7))


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(401 if self.headers["Host"] == "auth.lab.example" else 200)
        self.end_headers()

    def log_message(self, *a):
        pass


@pytest.fixture
def tls_server(tmp_path):
    ca = FakeCa()
    key, pem = _issue(ca, ["svc.lab.example", "auth.lab.example"])
    (tmp_path / "c.pem").write_text(pem)
    (tmp_path / "k.pem").write_bytes(key)
    (tmp_path / "ca.pem").write_text(ca.pem)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(tmp_path / "c.pem", tmp_path / "k.pem")
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1], str(tmp_path / "ca.pem"), certs.info_from_pem(pem).fingerprint, tmp_path
    srv.shutdown()


def test_https_check_verifies_chain_sni_and_status(tls_server):
    port, bundle, fp, _ = tls_server
    res = Checker(timeout=5).https("127.0.0.1", port, "svc.lab.example", "/", (200,), bundle)
    assert res.ok and res.fingerprint == fp
    res = Checker(timeout=5).https("127.0.0.1", port, "auth.lab.example", "/", (200, 401), bundle)
    assert res.ok and res.status == 401  # fehlende Authentifizierung ist kein Ausfall, wenn erlaubt


def test_https_check_fails_for_wrong_hostname_or_ca(tls_server):
    port, bundle, _, tmp = tls_server
    res = Checker(timeout=5).https("127.0.0.1", port, "other.lab.example", "/", (200,), bundle)
    assert not res.ok and "TLS-Prüfung" in res.error
    (tmp / "other.pem").write_text(FakeCa("x").pem)
    res = Checker(timeout=5).https("127.0.0.1", port, "svc.lab.example", "/", (200,), str(tmp / "other.pem"))
    assert not res.ok


def test_upstream_check_reports_unreachable():
    ok, msg = Checker(timeout=1).upstream("http", "127.0.0.1", 1, "/", (200,))
    assert not ok and "nicht erreichbar" in msg


def test_lock_is_exclusive(tmp_path):
    order = []

    def worker(name, hold):
        with npm_lock(tmp_path, "main", timeout=10):
            order.append(f"{name}-in")
            time.sleep(hold)
            order.append(f"{name}-out")

    t1 = threading.Thread(target=worker, args=("a", 0.5))
    t1.start()
    time.sleep(0.1)
    t2 = threading.Thread(target=worker, args=("b", 0))
    t2.start()
    t1.join(), t2.join()
    assert order == ["a-in", "a-out", "b-in", "b-out"]


def test_lock_timeout(tmp_path):
    with npm_lock(tmp_path, "main"):
        result = []
        t = threading.Thread(target=lambda: result.append(_try(tmp_path)))
        t.start()
        t.join()
    assert result == ["timeout"]


def _try(path):
    try:
        with npm_lock(path, "main", timeout=0.6):
            return "acquired"
    except IngressError:
        return "timeout"
