"""Minimal Komodo API client (v2.3.x) for the tools in infra/komodo and infra/ci.

Access via KOMODO_URL, KOMODO_API_KEY, KOMODO_API_SECRET (see infra/komodo/komodo.env).
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import time
import urllib.error
import urllib.request


def _env_file() -> pathlib.Path:
    """komodo.env of the repository the tools work on (HOMELAB_ROOT), else next to this file."""
    root = os.environ.get("HOMELAB_ROOT")
    return pathlib.Path(root) / "infra/komodo/komodo.env" if root else pathlib.Path(__file__).with_name("komodo.env")


def load_env(path: pathlib.Path | None = None) -> None:
    """Take the KOMODO_* variables from komodo.env unless they are already set.
    Without the file (a new installation, a container) they come from the process environment."""
    path = path or _env_file()
    if not path.is_file():
        return
    for key, value in re.findall(r"^(?:export\s+)?(KOMODO_\w+)=(\S*)", path.read_text(), re.M):
        os.environ.setdefault(key, value.strip("'\""))


def _req(path: str, body: dict, timeout: int = 60):
    return urllib.request.Request(
        os.environ["KOMODO_URL"].rstrip("/") + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Api-Key": os.environ["KOMODO_API_KEY"],
                 "X-Api-Secret": os.environ["KOMODO_API_SECRET"]}, method="POST")


def call(kind: str, typ: str, params: dict):
    """kind: read | write | execute"""
    try:
        with urllib.request.urlopen(_req(f"/{kind}/{typ}", params), timeout=60) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Komodo {kind}/{typ}: HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:500]}") from None


def read(typ: str, params: dict | None = None):
    return call("read", typ, params or {})


def write(typ: str, params: dict):
    return call("write", typ, params)


def execute_and_poll(typ: str, params: dict, timeout: float = 1800) -> dict:
    update = call("execute", typ, params)
    uid = update.get("id") or (update.get("_id") or {}).get("$oid")
    if not uid:  # like the official TS client: without an ID (e.g. DeployStackIfChanged with no change) there is nothing to poll
        return update
    deadline = time.monotonic() + timeout
    while update.get("status") != "Complete":
        if time.monotonic() > deadline:
            raise TimeoutError(f"Komodo update {uid} did not complete")
        time.sleep(2)
        update = read("GetUpdate", {"id": uid})
    return update

