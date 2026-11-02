class IngressError(Exception):
    """Basisfehler; Meldungen dürfen keine Secrets enthalten."""


class ManifestError(IngressError):
    pass


class ConfigError(IngressError):
    pass


class ConflictError(IngressError):
    pass


class AdapterError(IngressError):
    def __init__(self, system: str, message: str, status: int | None = None, ambiguous: bool = False):
        super().__init__(f"{system}: {message}" + (f" (HTTP {status})" if status else ""))
        self.system = system
        self.status = status
        # ambiguous: Mutation evtl. ausgeführt (Timeout/Verbindungsabbruch) -> vor Wiederholung Live-Abgleich
        self.ambiguous = ambiguous


class CertificateError(IngressError):
    pass


class PendingIssuance(IngressError):
    def __init__(self, request_id: str):
        super().__init__(f"Infisical-Zertifikatsanfrage {request_id} ist noch ausstehend")
        self.request_id = request_id
