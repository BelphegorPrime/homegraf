"""A small HTTP client on the standard library: JSON in and out, basic auth, and waiting for a service to answer."""
from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class Response:
    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def json(self):
        return json.loads(self.body) if self.body else None

    def header(self, name: str) -> str:
        """The header of that name (lowercased); repeated headers are on lines of their own."""
        return self.headers.get(name.lower(), "")


class Unreachable(Exception):
    """The service did not answer at all (not even with an error status)."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Keeps a redirect as the answer: a sign-in sets its cookie there, and following it would lose that cookie."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_NO_REDIRECT = urllib.request.build_opener(_NoRedirect)


def _headers(message) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, value in message.items():
        key = name.lower()
        out[key] = f"{out[key]}\n{value}" if key in out else value
    return out


def request(method: str, url: str, *, body=None, headers: dict | None = None, basic: tuple[str, str] | None = None,
            timeout: float = 30, redirect: bool = True) -> Response:
    """body: a dict is sent as JSON; bytes or str are sent as they are (the caller sets its Content-Type). redirect: follow a 3xx."""
    h = {"Accept": "application/json", **(headers or {})}
    data = None
    if body is not None:
        if isinstance(body, (bytes, bytearray, str)):
            data = body.encode() if isinstance(body, str) else bytes(body)
        else:
            data = json.dumps(body).encode()
            h.setdefault("Content-Type", "application/json")
    if basic:
        h["Authorization"] = "Basic " + base64.b64encode(f"{basic[0]}:{basic[1]}".encode()).decode()
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    open_ = urllib.request.urlopen if redirect else _NO_REDIRECT.open
    try:
        with open_(req, timeout=timeout) as resp:
            return Response(resp.status, resp.read(), _headers(resp.headers))
    except urllib.error.HTTPError as e:
        with e:   # an error answer holds the connection until it is closed (Python 3.15 warns about it)
            return Response(e.code, e.read(), _headers(e.headers))
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
        raise Unreachable(f"{method} {url}: {getattr(e, 'reason', e)}") from None


def wait_for(check: Callable[[], bool], what: str, *, timeout: float = 180, interval: float = 2,
             sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> None:
    """Calls check until it is true; an unreachable service counts as not yet. Raises with `what` after the timeout."""
    deadline = clock() + timeout
    while True:
        try:
            if check():
                return
        except Unreachable:
            pass
        if clock() >= deadline:
            raise TimeoutError(f"{what} did not become ready within {int(timeout)} s")
        sleep(interval)
