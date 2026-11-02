"""Strukturierte, redigierte JSON-Logs (eine Zeile pro Ereignis auf stderr)."""
from __future__ import annotations

import datetime as dt
import json
import re
import sys

_PEM = re.compile(r"-----BEGIN [A-Z ]+-----.*?-----END [A-Z ]+-----", re.S)
_BEARER = re.compile(r"(?i)bearer\s+[a-z0-9._~+/=-]+")
_SECRET_KEYS = re.compile(r"(?i)(secret|password|token|private_?key|certificate_key|authorization)")

_context: dict = {}


def set_context(**kw) -> None:
    _context.update({k: v for k, v in kw.items() if v is not None})


def redact(value):
    if isinstance(value, str):
        return _BEARER.sub("Bearer <redacted>", _PEM.sub("<pem redacted>", value))
    if isinstance(value, dict):
        return {k: ("<redacted>" if _SECRET_KEYS.search(k) else redact(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value


def event(operation: str, result: str, **fields) -> None:
    rec = {"ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), **_context,
           "operation": operation, "result": result, **fields}
    print(json.dumps(redact(rec), ensure_ascii=False, default=str), file=sys.stderr, flush=True)
