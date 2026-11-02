"""Shared bits of the steps: the error type, logging, and the placeholders of the seed."""
from __future__ import annotations

import os
import sys


class StepError(Exception):
    """A step that cannot continue; the message says what to do."""


def log(step: str, message: str) -> None:
    if not os.environ.get("HGBOOT_QUIET"):
        print(f"[{step}] {message}", flush=True)


def warn(step: str, message: str) -> None:
    if not os.environ.get("HGBOOT_QUIET"):
        print(f"[{step}] WARNING: {message}", file=sys.stderr, flush=True)
