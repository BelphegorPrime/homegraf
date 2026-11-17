"""Adapter for Infisical Certificate Management (verified against infisical/infisical v0.167.2).

Contract (see docs/discovery.md, source: backend/src of tag v0.167.2):
- POST /api/v1/auth/universal-auth/login {clientId, clientSecret} -> {accessToken, expiresIn}
- POST /api/v1/cert-manager/certificates {profileId, applicationId, csr, attributes:{ttl}, metadata}
  -> {certificate: {certificate, certificateChain, issuingCaCertificate, serialNumber, certificateId} | null,
      certificateRequestId}; certificate == null means pending.
- GET /api/v1/cert-manager/certificates/certificate-requests/{id} (status of pending requests)
- POST /api/v1/projects/{projectId}/certificates/search {metadata:[{key,value}], status, applicationId, limit}
- GET /api/v1/cert-manager/certificates/{id}/certificate (PEM without key)
We use the CSR path exclusively: the private key never leaves the management server.
"""
from __future__ import annotations

import requests

from .errors import AdapterError, PendingIssuance
from .http import request

SYSTEM = "Infisical"


class InfisicalClient:
    def __init__(self, api_url: str, client_id: str, client_secret: str, verify: str | bool = True,
                 session: requests.Session | None = None):
        self.base = api_url.rstrip("/")
        self.client_id = client_id
        self._secret = client_secret
        self.session = session or requests.Session()
        self.session.verify = verify
        self.mutations = 0

    def _req(self, method: str, path: str, *, mutating: bool | None = None, **kw) -> requests.Response:
        mutating = method not in ("GET", "HEAD") if mutating is None else mutating
        if mutating:
            self.mutations += 1
        return request(self.session, SYSTEM, method, self.base + path, mutating=mutating, **kw)

    def login(self) -> None:
        body = self._req("POST", "/api/v1/auth/universal-auth/login", mutating=False,
                         json={"clientId": self.client_id, "clientSecret": self._secret}).json()
        token = body.get("accessToken")
        if not token:
            raise AdapterError(SYSTEM, "login response without accessToken")
        self.session.headers["Authorization"] = f"Bearer {token}"

    def issue_from_csr(self, *, profile_id: str, application_id: str, csr_pem: str, ttl_days: int,
                       metadata: dict[str, str]) -> dict:
        body = self._req("POST", "/api/v1/cert-manager/certificates", json={
            "profileId": profile_id,
            "applicationId": application_id,
            "csr": csr_pem,
            "attributes": {"ttl": f"{ttl_days}d"},
            "metadata": [{"key": k, "value": v} for k, v in metadata.items()],
        }).json()
        cert = body.get("certificate")
        if not cert:
            raise PendingIssuance(str(body.get("certificateRequestId")))
        return _bundle(cert)

    def get_request(self, request_id: str) -> dict | None:
        """Bundle if the pending request has been issued meanwhile, otherwise None."""
        body = self._req("GET", f"/api/v1/cert-manager/certificates/certificate-requests/{request_id}").json()
        cert_id = body.get("certificateId") or (body.get("certificate") or {}).get("id")
        status = str(body.get("status", "")).lower()
        if status in ("rejected", "failed"):
            raise AdapterError(SYSTEM, f"certificate request {request_id} rejected ({status})")
        if not cert_id:
            return None
        return self.get_certificate(cert_id)

    def get_certificate(self, cert_id: str) -> dict:
        body = self._req("GET", f"/api/v1/cert-manager/certificates/{cert_id}/certificate").json()
        body.setdefault("certificateId", cert_id)
        return _bundle(body)

    def find_by_metadata(self, project_id: str, application_id: str, key: str, value: str) -> list[str]:
        """IDs of active certificates with metadata key=value (to find them again after a timeout)."""
        body = self._req("POST", f"/api/v1/projects/{project_id}/certificates/search", mutating=False, json={
            "metadata": [{"key": key, "value": value}], "status": "active", "applicationId": application_id,
            "limit": 100,
        }).json()
        return [c["id"] for c in body.get("certificates") or [] if c.get("id")]


def _bundle(cert: dict) -> dict:
    """Take only certificate data; a privateKey field that may be included is discarded."""
    return {
        "certificate": cert.get("certificate"),
        "certificate_chain": cert.get("certificateChain"),
        "issuing_ca": cert.get("issuingCaCertificate"),
        "serial": cert.get("serialNumber"),
        "certificate_id": cert.get("certificateId") or cert.get("id"),
    }
