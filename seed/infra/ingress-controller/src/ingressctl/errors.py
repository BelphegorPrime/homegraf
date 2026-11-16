class IngressError(Exception):
    """Base error; messages must not contain secrets."""


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
        # ambiguous: mutation may have run (timeout/connection drop) -> reconcile live state before retrying
        self.ambiguous = ambiguous


class PresetError(IngressError):
    pass


class CertificateError(IngressError):
    pass


class PendingIssuance(IngressError):
    def __init__(self, request_id: str):
        super().__init__(f"Infisical certificate request {request_id} is still pending")
        self.request_id = request_id
