"""Netzprüfungen vom Management-Server aus: Upstream-Erreichbarkeit und präsentiertes HTTPS-Zertifikat."""
from __future__ import annotations

import http.client
import socket
import ssl
from dataclasses import dataclass

from cryptography import x509
from cryptography.hazmat.primitives import hashes


@dataclass
class TlsResult:
    ok: bool
    fingerprint: str | None
    status: int | None
    error: str | None = None


class Checker:
    def __init__(self, timeout: float = 10.0):
        self.timeout = timeout

    def upstream(self, scheme: str, host: str, port: int, path: str, accepted: tuple[int, ...]) -> tuple[bool, str]:
        """HTTP(S)-Anfrage direkt an den Upstream. TLS des Upstreams wird hier nicht geprüft (interne Backends
        nutzen oft selbstsignierte Zertifikate); maßgeblich ist die anschließende Prüfung über NPM."""
        try:
            if scheme == "https":
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                conn = http.client.HTTPSConnection(host, port, timeout=self.timeout, context=ctx)
            else:
                conn = http.client.HTTPConnection(host, port, timeout=self.timeout)
            conn.request("GET", path, headers={"User-Agent": "ingressctl"})
            status = conn.getresponse().status
            conn.close()
        except (OSError, http.client.HTTPException) as e:
            return False, f"Upstream {scheme}://{host}:{port} nicht erreichbar: {type(e).__name__}"
        if status not in accepted:
            return False, f"Upstream antwortet {status}, erwartet {list(accepted)}"
        return True, f"Upstream antwortet {status}"

    def https(self, address: str, port: int, domain: str, path: str, accepted: tuple[int, ...],
              ca_bundle: str) -> TlsResult:
        """Verbindet zu NPM mit SNI=domain, prüft Kette und Hostname gegen das CA-Bundle (kein --insecure)."""
        ctx = ssl.create_default_context(cafile=ca_bundle)
        try:
            with socket.create_connection((address, port), timeout=self.timeout) as raw:
                with ctx.wrap_socket(raw, server_hostname=domain) as tls:
                    der = tls.getpeercert(binary_form=True)
                    fp = x509.load_der_x509_certificate(der).fingerprint(hashes.SHA256()).hex()
                    conn = http.client.HTTPConnection(domain)
                    conn.sock = tls
                    conn.request("GET", path, headers={"Host": domain, "User-Agent": "ingressctl"})
                    status = conn.getresponse().status
        except ssl.SSLCertVerificationError as e:
            return TlsResult(False, None, None, f"TLS-Prüfung für {domain} fehlgeschlagen: {e.verify_message}")
        except (OSError, http.client.HTTPException) as e:
            return TlsResult(False, None, None, f"HTTPS {domain} via {address}:{port} fehlgeschlagen: {type(e).__name__}")
        if status not in accepted:
            return TlsResult(False, fp, status, f"HTTPS {domain} antwortet {status}, erwartet {list(accepted)}")
        return TlsResult(True, fp, status)
