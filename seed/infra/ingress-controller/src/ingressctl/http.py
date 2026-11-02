"""Gemeinsame HTTP-Hilfen für die Adapter: Timeouts, Fehlerabbildung ohne Response-Bodies."""
from __future__ import annotations

import requests

from .errors import AdapterError

TIMEOUT = (5, 30)


def request(session: requests.Session, system: str, method: str, url: str, *, mutating: bool, **kw) -> requests.Response:
    kw.setdefault("timeout", TIMEOUT)
    try:
        resp = session.request(method, url, **kw)
    except requests.Timeout:
        raise AdapterError(system, f"Timeout bei {method} {_path(url)}", ambiguous=mutating) from None
    except requests.ConnectionError as e:
        # Bei Mutationen ist unklar, ob der Server die Anfrage verarbeitet hat.
        raise AdapterError(system, f"nicht erreichbar ({type(e).__name__}) bei {method} {_path(url)}",
                           ambiguous=mutating) from None
    if resp.status_code >= 400:
        raise AdapterError(system, f"{method} {_path(url)}: {_error_message(resp)}", status=resp.status_code)
    return resp


def _path(url: str) -> str:
    return "/" + url.split("://", 1)[-1].split("/", 1)[-1].split("?", 1)[0]


def _error_message(resp: requests.Response) -> str:
    """Nur die strukturierte Fehlermeldung übernehmen, nie den rohen Body (kann Secrets enthalten)."""
    try:
        body = resp.json()
    except ValueError:
        return resp.reason or "Fehler"
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and isinstance(err.get("message"), str):
            return err["message"][:300]
        if isinstance(body.get("message"), str):
            return body["message"][:300]
    return resp.reason or "Fehler"
