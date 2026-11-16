"""Generate keys/CSRs, validate issued certificates, decide on renewal."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from .errors import CertificateError

_PEM_CERT_END = "-----END CERTIFICATE-----"


@dataclass(frozen=True)
class CertInfo:
    fingerprint: str          # SHA-256 of the leaf certificate (hex)
    sans: tuple[str, ...]
    not_before: dt.datetime
    not_after: dt.datetime
    issuer: str
    serial: str

    @property
    def lifetime(self) -> dt.timedelta:
        return self.not_after - self.not_before


def generate_key_pem() -> bytes:
    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


def make_csr_pem(key_pem: bytes, domains: tuple[str, ...]) -> str:
    key = serialization.load_pem_private_key(key_pem, password=None)
    csr = (x509.CertificateSigningRequestBuilder()
           .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, domains[0])]))
           .add_extension(x509.SubjectAlternativeName([x509.DNSName(d) for d in domains]), critical=False)
           .sign(key, hashes.SHA256()))
    return csr.public_bytes(serialization.Encoding.PEM).decode()


def split_pem_chain(pem: str | None) -> list[x509.Certificate]:
    if not pem:
        return []
    blocks = [b + _PEM_CERT_END for b in pem.split(_PEM_CERT_END) if "BEGIN CERTIFICATE" in b]
    return [x509.load_pem_x509_certificate(b.strip().encode()) for b in blocks]


def cert_info(cert: x509.Certificate) -> CertInfo:
    try:
        sans = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
    except x509.ExtensionNotFound:
        sans = []
    return CertInfo(
        fingerprint=cert.fingerprint(hashes.SHA256()).hex(),
        sans=tuple(sorted(s.lower() for s in sans)),
        not_before=cert.not_valid_before_utc,
        not_after=cert.not_valid_after_utc,
        issuer=cert.issuer.rfc4514_string(),
        serial=format(cert.serial_number, "x"),
    )


def info_from_pem(pem: str) -> CertInfo:
    certs = split_pem_chain(pem)
    if not certs:
        raise CertificateError("no certificate in PEM")
    return cert_info(certs[0])


def public_key_id(obj) -> str:
    """Fingerprint of the public key (for a certificate, CSR or private key)."""
    pub = obj.public_key()
    der = pub.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    h = hashes.Hash(hashes.SHA256())
    h.update(der)
    return h.finalize().hex()


def key_id_from_pem(key_pem: bytes) -> str:
    return public_key_id(serialization.load_pem_private_key(key_pem, password=None))


def validate_issued(cert_pem: str, chain_pem: str | None, key_pem: bytes, domains: tuple[str, ...],
                    trust_pem: str, now: dt.datetime) -> CertInfo:
    """Validate key match, SANs, serverAuth, validity and chain up to a trust anchor.

    Raises CertificateError; NPM must then not be switched over to this certificate.
    """
    leaf_list = split_pem_chain(cert_pem)
    if len(leaf_list) != 1:
        raise CertificateError("expected exactly one leaf certificate")
    leaf = leaf_list[0]
    info = cert_info(leaf)
    if public_key_id(leaf) != key_id_from_pem(key_pem):
        raise CertificateError("certificate does not match the private key")
    if info.sans != tuple(sorted(domains)):
        raise CertificateError(f"SANs {list(info.sans)} do not match {list(domains)}")
    try:
        eku = leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        if ExtendedKeyUsageOID.SERVER_AUTH not in eku:
            raise CertificateError("ExtendedKeyUsage does not contain serverAuth")
    except x509.ExtensionNotFound:
        pass  # without an EKU extension serverAuth is not restricted
    if not (info.not_before <= now < info.not_after):
        raise CertificateError("certificate is not valid (yet)")

    anchors = split_pem_chain(trust_pem)
    if not anchors:
        raise CertificateError("no trust anchor (CA bundle is empty)")
    anchor_fps = {a.fingerprint(hashes.SHA256()) for a in anchors}
    intermediates = [c for c in split_pem_chain(chain_pem) if c.fingerprint(hashes.SHA256()) not in anchor_fps]
    current = leaf
    for _ in range(len(intermediates) + 1):
        for a in anchors:
            if _issued_by(current, a):
                return info
        nxt = next((c for c in intermediates if _issued_by(current, c)), None)
        if nxt is None:
            break
        intermediates.remove(nxt)
        current = nxt
    raise CertificateError("chain does not end at a trusted CA certificate")


def _issued_by(cert: x509.Certificate, issuer: x509.Certificate) -> bool:
    try:
        cert.verify_directly_issued_by(issuer)
        return True
    except Exception:  # InvalidSignature, name mismatch, unsupported key type
        return False


def renewal_lead(info: CertInfo, renew_before_days: int) -> dt.timedelta:
    """Lead time, capped at one third of the actual lifetime.

    If the issuer issues shorter certificates than requested, a freshly issued certificate thus does not
    trigger an immediate renewal (no endless loop).
    """
    return min(dt.timedelta(days=renew_before_days), info.lifetime / 3)


def needs_renewal(info: CertInfo, renew_before_days: int, now: dt.datetime) -> bool:
    return info.not_after - now <= renewal_lead(info, renew_before_days)
