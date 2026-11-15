"""A small HTTP client on the standard library: JSON in and out, basic auth, and waiting for a service to answer."""
from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable


@dataclass
class Response:
    status: int
    body: bytes

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def json(self):
        return json.loads(self.body) if self.body else None


class Unreachable(Exception):
    """The service did not answer at all (not even with an error status)."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Keeps a redirect as the answer instead of following it to another origin with the credentials attached."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_NO_REDIRECT = urllib.request.build_opener(_NoRedirect)


def request(method: str, url: str, *, body=None, headers: dict | None = None, basic: tuple[str, str] | None = None,
            timeout: float = 30, redirect: bool = True) -> Response:
    """redirect: follow a 3xx answer; False keeps it as the response, so no header reaches another origin."""
    h = {"Accept": "application/json", **(headers or {})}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h.setdefault("Content-Type", "application/json")
    if basic:
        h["Authorization"] = "Basic " + base64.b64encode(f"{basic[0]}:{basic[1]}".encode()).decode()
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    open_ = urllib.request.urlopen if redirect else _NO_REDIRECT.open
    try:
        with open_(req, timeout=timeout) as resp:
            return Response(resp.status, resp.read())
    except urllib.error.HTTPError as e:
        with e:   # an error answer holds the connection until it is closed (Python 3.15 warns about it)
            return Response(e.code, e.read())
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
