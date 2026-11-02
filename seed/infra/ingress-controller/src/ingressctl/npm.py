"""Adapter für die Nginx-Proxy-Manager-API (geprüft gegen jc21/nginx-proxy-manager v2.16.0).

Vertrag (siehe docs/discovery.md, Quelle: backend/ des Tags v2.16.0):
- POST /api/tokens {identity, secret} -> {token, expires}; 2FA-Konten liefern stattdessen eine Challenge.
- Proxy Hosts: GET/POST /api/nginx/proxy-hosts, GET/PUT /api/nginx/proxy-hosts/{id} (PUT partiell).
  Create/Update laden nginx neu; schlägt `nginx -t` fehl, antwortet die API trotzdem 2xx mit
  meta.nginx_online=false -> hier als Fehler behandelt.
- Doppelte Domains (auch fremde/inaktive Hosts) -> 400 "... is already in use".
- Custom-Zertifikate: POST /api/nginx/certificates {provider:"other", nice_name}, dann
  POST /api/nginx/certificates/{id}/upload (multipart certificate, certificate_key, intermediate_certificate).
  Upload lädt nginx NICHT neu; erst ein PUT auf einen aktiven Host tut das.
- GET auf Zertifikate liefert meta.certificate_key im Klartext -> wird hier sofort verworfen.
"""
from __future__ import annotations

import requests

from . import certs
from .errors import AdapterError
from .http import request

SYSTEM = "NPM"
MANAGED_HOST_FIELDS = ("domain_names", "forward_scheme", "forward_host", "forward_port", "certificate_id",
                       "ssl_forced", "allow_websocket_upgrade", "enabled")
CERT_NAME_PREFIX = "ingressctl:"


def sanitize_certificate(raw: dict) -> dict:
    """Nur unkritische Felder übernehmen; Fingerprint aus dem öffentlichen Zertifikat berechnen."""
    meta = raw.get("meta") or {}
    info = None
    if isinstance(meta.get("certificate"), str) and "BEGIN CERTIFICATE" in meta["certificate"]:
        try:
            info = certs.info_from_pem(meta["certificate"])
        except Exception:  # defektes PEM im NPM -> wie fehlendes Zertifikat behandeln
            info = None
    return {
        "id": raw.get("id"),
        "provider": raw.get("provider"),
        "nice_name": raw.get("nice_name"),
        "domain_names": raw.get("domain_names") or [],
        "expires_on": raw.get("expires_on"),
        "info": info,
        "fingerprint": info.fingerprint if info else None,
    }


class NpmClient:
    def __init__(self, api_url: str, identity: str, secret: str, verify: str | bool = True,
                 session: requests.Session | None = None):
        self.base = api_url.rstrip("/")
        self.identity = identity
        self._secret = secret
        self.session = session or requests.Session()
        self.session.verify = verify
        self.mutations = 0

    def _req(self, method: str, path: str, **kw) -> requests.Response:
        mutating = method not in ("GET", "HEAD")
        if mutating:
            self.mutations += 1
        return request(self.session, SYSTEM, method, self.base + path, mutating=mutating, **kw)

    def login(self) -> None:
        resp = request(self.session, SYSTEM, "POST", self.base + "/tokens", mutating=False,
                       json={"identity": self.identity, "secret": self._secret})
        body = resp.json()
        if body.get("requires_2fa"):
            raise AdapterError(SYSTEM, "technischer Benutzer hat 2FA aktiv; für ingressctl nicht unterstützt")
        token = body.get("token")
        if not token:
            raise AdapterError(SYSTEM, "Token-Antwort ohne token")
        self.session.headers["Authorization"] = f"Bearer {token}"

    # --- Proxy Hosts ------------------------------------------------------------------------------
    def list_proxy_hosts(self) -> list[dict]:
        return self._req("GET", "/nginx/proxy-hosts").json()

    def get_proxy_host(self, host_id: int) -> dict | None:
        try:
            return self._req("GET", f"/nginx/proxy-hosts/{host_id}").json()
        except AdapterError as e:
            if e.status == 404:
                return None
            raise

    def create_proxy_host(self, fields: dict) -> dict:
        host = self._req("POST", "/nginx/proxy-hosts", json=fields).json()
        self._check_nginx(host)
        return host

    def update_proxy_host(self, host_id: int, fields: dict) -> dict:
        host = self._req("PUT", f"/nginx/proxy-hosts/{host_id}", json=fields).json()
        self._check_nginx(host)
        return host

    def delete_proxy_host(self, host_id: int) -> None:
        self._req("DELETE", f"/nginx/proxy-hosts/{host_id}")

    @staticmethod
    def _check_nginx(host: dict) -> None:
        meta = host.get("meta") or {}
        if meta.get("nginx_online") is False:
            err = str(meta.get("nginx_err") or "")[:300]
            raise AdapterError(SYSTEM, f"nginx-Konfiguration für Host {host.get('id')} ungültig: {err}")

    # --- Zertifikate ------------------------------------------------------------------------------
    def list_certificates(self) -> list[dict]:
        return [sanitize_certificate(c) for c in self._req("GET", "/nginx/certificates").json()]

    def get_certificate(self, cert_id: int) -> dict | None:
        try:
            return sanitize_certificate(self._req("GET", f"/nginx/certificates/{cert_id}").json())
        except AdapterError as e:
            if e.status == 404:
                return None
            raise

    def certificate_users(self, cert_id: int) -> int:
        raw = self._req("GET", f"/nginx/certificates/{cert_id}",
                        params={"expand": "proxy_hosts,redirection_hosts,dead_hosts,streams"}).json()
        return sum(len(raw.get(k) or []) for k in ("proxy_hosts", "redirection_hosts", "dead_hosts", "streams"))

    def create_custom_certificate(self, nice_name: str) -> dict:
        raw = self._req("POST", "/nginx/certificates", json={"provider": "other", "nice_name": nice_name}).json()
        return sanitize_certificate(raw)

    def upload_certificate(self, cert_id: int, cert_pem: str, key_pem: bytes, chain_pem: str | None) -> None:
        files = {"certificate": ("cert.pem", cert_pem.encode()), "certificate_key": ("key.pem", key_pem)}
        if chain_pem:
            files["intermediate_certificate"] = ("chain.pem", chain_pem.encode())
        self._req("POST", f"/nginx/certificates/{cert_id}/upload", files=files)  # Antwort enthält Schlüssel: verwerfen

    def delete_certificate(self, cert_id: int) -> None:
        self._req("DELETE", f"/nginx/certificates/{cert_id}")


def host_matches_domains(host: dict, domains) -> set[str]:
    live = {d.lower() for d in host.get("domain_names") or []}
    return live & set(domains)
