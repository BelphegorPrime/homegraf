"""Single-Writer-Sperre pro NPM-Instanz (flock auf dem gemeinsamen Statusvolume)."""
from __future__ import annotations

import contextlib
import fcntl
import os
import pathlib
import time

from .errors import IngressError


@contextlib.contextmanager
def npm_lock(state_dir: pathlib.Path, instance: str, timeout: float = 600.0):
    lock_dir = state_dir / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_dir / f"npm-{instance}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise IngressError(f"Sperre für NPM-Instanz '{instance}' nicht innerhalb {timeout:.0f}s erhalten")
                time.sleep(0.5)
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
