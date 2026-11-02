"""HTTP-Fakes für NPM 2.16.0 und Infisical v0.167.2.

Bilden das aus dem Quellcode verifizierte Verhalten nach (docs/discovery.md), nicht die Annahmen von ingressctl:
- NPM: doppelte Domains -> 400; Upload überschreibt Dateien ohne Reload; nginx liest Zertifikate erst bei
  Create/Update/Enable eines aktiven Hosts; GET auf Zertifikate enthält meta.certificate_key im Klartext.
- Infisical: CSR wird von einer echten Test-CA signiert; optional ausstehend, kürzere Laufzeit, falsche SANs.
Fehlerinjektion: `fail_after` führt eine Mutation aus und wirft danach requests.Timeout (unklarer Ausgang).
"""
from __future__ import annotations

import datetime as dt
import email.parser
import email.policy
import itertools
import json
import re
import uuid
from urllib.parse import parse_qs, urlparse

import requests
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


class _Adapter(requests.adapters.BaseAdapter):
    def __init__(self, handler):
        super().__init__()
        self.handler = handler

    def send(self, request, **kw):
        url = urlparse(request.url)
        status, body = self.handler(request.method, url.path, parse_qs(url.query), request.headers, request.body)
        resp = requests.Response()
        resp.status_code = status
        resp._content = json.dumps(body).encode()
        resp.headers["Content-Type"] = "application/json"
        resp.url = request.url
        resp.request = request
        return resp

    def close(self):
        pass


def session_for(fake) -> requests.Session:
    s = requests.Session()
    s.mount("http://", _Adapter(fake.handle))
    s.mount("https://", _Adapter(fake.handle))
    return s


def _multipart(headers, body: bytes) -> dict[str, bytes]:
    msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
        b"Content-Type: " + headers["Content-Type"].encode() + b"\r\n\r\n" + body)
    return {part.get_param("name", header="content-disposition"): part.get_payload(decode=True)
            for part in msg.iter_parts()}


def _fp(pem: str) -> str:
    return x509.load_pem_x509_certificate(pem.encode()).fingerprint(hashes.SHA256()).hex()


class FakeNpm:
    def __init__(self):
        self.hosts: dict[int, dict] = {}
        self.certs: dict[int, dict] = {}
        self.cert_files: dict[int, str] = {}       # /data/custom_ssl/npm-<id>/fullchain.pem (Leaf)
        self.presented: dict[str, str] = {}        # Domain -> Fingerprint, Stand des letzten nginx-Reloads
        self.ids = itertools.count(1)
        self.mutations = 0
        self.reachable = True
        self.fail_after: set[tuple[str, str]] = set()

    def add_manual_host(self, domains, forward_host="10.0.0.9", port=80):
        hid = next(self.ids)
        self.hosts[hid] = {"id": hid, "domain_names": domains, "forward_scheme": "http", "forward_host": forward_host,
                           "forward_port": port, "certificate_id": 0, "ssl_forced": False,
                           "allow_websocket_upgrade": False, "enabled": True, "hsts_enabled": False,
                           "advanced_config": "# manual", "meta": {"nginx_online": True}}
        return hid

    def _reload(self):
        self.presented = {}
        for h in self.hosts.values():
            if h["enabled"] and h["certificate_id"] and h["certificate_id"] in self.cert_files:
                for d in h["domain_names"]:
                    self.presented[d] = _fp(self.cert_files[h["certificate_id"]])

    def _taken(self, domains, exclude=None):
        for h in self.hosts.values():
            if h["id"] != exclude and {d.lower() for d in h["domain_names"]} & {d.lower() for d in domains}:
                return next(iter({d.lower() for d in h["domain_names"]} & {d.lower() for d in domains}))
        return None

    def handle(self, method, path, query, headers, body):
        if not self.reachable:
            raise requests.ConnectionError("connection refused")
        key = (method, re.sub(r"/\d+", "/{id}", path))
        status, out = self._route(method, path, query, headers, body)
        if method != "GET":
            self.mutations += 1
        if key in self.fail_after:
            self.fail_after.discard(key)
            raise requests.Timeout("read timeout")
        return status, out

    def _route(self, method, path, query, headers, body):
        p = path.removeprefix("/api")
        if p == "/tokens" and method == "POST":
            self.mutations -= 1  # Login zählt nicht als Mutation
            return 200, {"token": "jwt-test", "expires": "2099-01-01T00:00:00Z"}
        if headers.get("Authorization") != "Bearer jwt-test":
            return 401, {"error": {"code": 401, "message": "Unauthorized"}}
        if p == "/nginx/proxy-hosts":
            if method == "GET":
                return 200, list(self.hosts.values())
            data = json.loads(body)
            clash = self._taken(data["domain_names"])
            if clash:
                return 400, {"error": {"code": 400, "message": f"{clash} is already in use"}}
            hid = next(self.ids)
            host = {"id": hid, "hsts_enabled": False, "advanced_config": "", "enabled": True,
                    "meta": {"nginx_online": True}, **data}
            self.hosts[hid] = host
            self._reload()
            return 201, host
        m = re.fullmatch(r"/nginx/proxy-hosts/(\d+)", p)
        if m:
            hid = int(m.group(1))
            if hid not in self.hosts:
                return 404, {"error": {"code": 404, "message": "Not Found"}}
            if method == "GET":
                return 200, self.hosts[hid]
            if method == "DELETE":
                del self.hosts[hid]
                self._reload()
                return 200, True
            data = json.loads(body)
            if "domain_names" in data and self._taken(data["domain_names"], exclude=hid):
                return 400, {"error": {"code": 400, "message": "already in use"}}
            self.hosts[hid].update(data)
            if self.hosts[hid]["enabled"]:
                self._reload()
            return 200, self.hosts[hid]
        if p == "/nginx/certificates":
            if method == "GET":
                return 200, list(self.certs.values())
            data = json.loads(body)
            cid = next(self.ids)
            self.certs[cid] = {"id": cid, "provider": data["provider"], "nice_name": data["nice_name"],
                               "domain_names": [], "expires_on": "now", "meta": {}}
            return 201, self.certs[cid]
        m = re.fullmatch(r"/nginx/certificates/(\d+)(/upload)?", p)
        if m:
            cid = int(m.group(1))
            if cid not in self.certs:
                return 404, {"error": {"code": 404, "message": "Not Found"}}
            if m.group(2):
                parts = _multipart(headers, body)
                cert = parts["certificate"].decode()
                self.cert_files[cid] = cert  # kein Reload!
                self.certs[cid]["meta"] = {"certificate": cert, "certificate_key": parts["certificate_key"].decode()}
                return 200, {"certificate": cert, "certificate_key": parts["certificate_key"].decode()}
            if method == "DELETE":
                del self.certs[cid]
                return 200, True
            out = dict(self.certs[cid])
            if "expand" in query:
                out["proxy_hosts"] = [h for h in self.hosts.values() if h["certificate_id"] == cid]
                out["redirection_hosts"] = out["dead_hosts"] = out["streams"] = []
            return 200, out
        return 404, {"error": {"code": 404, "message": f"no route {method} {p}"}}


class FakeCa:
    def __init__(self, cn="Test Homelab Root"):
        self.key = ec.generate_private_key(ec.SECP256R1())
        now = dt.datetime.now(dt.timezone.utc)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
        self.cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                     .public_key(self.key.public_key()).serial_number(x509.random_serial_number())
                     .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=3650))
                     .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                     .add_extension(x509.KeyUsage(digital_signature=False, content_commitment=False, key_encipherment=False,
                                                  data_encipherment=False, key_agreement=False, key_cert_sign=True,
                                                  crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
                     .add_extension(x509.SubjectKeyIdentifier.from_public_key(self.key.public_key()), critical=False)
                     .sign(self.key, hashes.SHA256()))
        self.pem = self.cert.public_bytes(serialization.Encoding.PEM).decode()

    def sign(self, public_key, sans, days, not_before=None, eku=True):
        nb = not_before or dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)
        b = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, sans[0])]))
             .issuer_name(self.cert.subject).public_key(public_key).serial_number(x509.random_serial_number())
             .not_valid_before(nb).not_valid_after(nb + dt.timedelta(days=days))
             .add_extension(x509.SubjectAlternativeName([x509.DNSName(s) for s in sans]), critical=False)
             # wie die echten Infisical-Zertifikate: Python >= 3.13 prüft mit VERIFY_X509_STRICT (verlangt AKI)
             .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(self.key.public_key()), critical=False)
             .add_extension(x509.SubjectKeyIdentifier.from_public_key(public_key), critical=False))
        if eku:
            b = b.add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        return b.sign(self.key, hashes.SHA256()).public_bytes(serialization.Encoding.PEM).decode()


class FakeInfisical:
    def __init__(self, ca: FakeCa):
        self.ca = ca
        self.certs: dict[str, dict] = {}
        self.requests: dict[str, dict] = {}
        self.mutations = 0
        self.issued = 0
        self.pending_next = False
        self.override_days: int | None = None
        self.wrong_san = False
        self.not_before: dt.datetime | None = None
        self.fail_after_issue = False

    def handle(self, method, path, query, headers, body):
        if path == "/api/v1/auth/universal-auth/login":
            return 200, {"accessToken": "inf-token", "expiresIn": 3600, "tokenType": "Bearer"}
        if headers.get("Authorization") != "Bearer inf-token":
            return 401, {"message": "unauthorized"}
        if path == "/api/v1/cert-manager/certificates" and method == "POST":
            self.mutations += 1
            data = json.loads(body)
            assert set(data) == {"profileId", "applicationId", "csr", "attributes", "metadata"}
            assert re.fullmatch(r"\d+d", data["attributes"]["ttl"])
            csr = x509.load_pem_x509_csr(data["csr"].encode())
            sans = csr.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
            if self.wrong_san:
                sans = ["evil.lab.example"]
            days = self.override_days or int(data["attributes"]["ttl"][:-1])
            pem = self.ca.sign(csr.public_key(), sans, days, not_before=self.not_before)
            cid = str(uuid.uuid4())
            self.issued += 1
            self.certs[cid] = {"certificate": pem, "certificateChain": self.ca.pem, "metadata": data["metadata"]}
            rid = str(uuid.uuid4())
            if self.pending_next:
                self.pending_next = False
                self.requests[rid] = {"status": "pending", "certificateId": None, "_cid": cid}
                return 200, {"certificate": None, "certificateRequestId": rid}
            self.requests[rid] = {"status": "issued", "certificateId": cid}
            if self.fail_after_issue:
                self.fail_after_issue = False
                raise requests.Timeout("read timeout")
            return 200, {"certificate": {"certificate": pem, "certificateChain": self.ca.pem,
                                         "issuingCaCertificate": self.ca.pem, "privateKey": None,
                                         "serialNumber": "01", "certificateId": cid},
                         "certificateRequestId": rid}
        m = re.fullmatch(r"/api/v1/cert-manager/certificates/certificate-requests/([^/]+)", path)
        if m:
            r = self.requests[m.group(1)]
            return 200, {"status": r["status"], "certificate": None, "certificateId": r["certificateId"]}
        m = re.fullmatch(r"/api/v1/cert-manager/certificates/([^/]+)/certificate", path)
        if m:
            c = self.certs[m.group(1)]
            return 200, {"certificate": c["certificate"], "certificateChain": c["certificateChain"], "serialNumber": "01"}
        m = re.fullmatch(r"/api/v1/projects/([^/]+)/certificates/search", path)
        if m:
            want = json.loads(body)["metadata"][0]
            hits = [{"id": cid} for cid, c in self.certs.items() if want in c["metadata"]]
            return 200, {"certificates": hits, "totalCount": len(hits)}
        return 404, {"message": f"no route {method} {path}"}

    def release_pending(self):
        for r in self.requests.values():
            if r["status"] == "pending":
                r.update(status="issued", certificateId=r.pop("_cid"))


class FakeChecker:
    """Simuliert die Netzsicht des Management-Servers auf Upstream und NPM:443."""

    def __init__(self, npm: FakeNpm):
        self.npm = npm
        self.upstream_ok = True

    def upstream(self, scheme, host, port, path, accepted):
        return (self.upstream_ok, "ok" if self.upstream_ok else "Upstream nicht erreichbar")

    def https(self, address, port, domain, path, accepted, ca_bundle):
        from ingressctl.checks import TlsResult
        fp = self.npm.presented.get(domain)
        if fp is None:
            return TlsResult(False, None, None, f"{domain}: kein Zertifikat konfiguriert")
        return TlsResult(True, fp, 200)
