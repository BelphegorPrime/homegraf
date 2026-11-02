"""Terminal building blocks for the interactive tools (homelab.py, secret.py): menus, questions, hidden input.

Agent mode (AUTO): no prompts. Questions take their preset or default, or fail naming the flag to pass;
confirm() answers yes only with --yes (ASSUME_YES), otherwise the run stops as a dry run.
"""
from __future__ import annotations

import getpass
import secrets
from typing import Callable

AUTO = False
ASSUME_YES = False


class DryRun(SystemExit):
    """Raised in agent mode at the first outward step when --yes is missing."""


def _need(what: str, flag: str) -> SystemExit:
    return SystemExit(f"agent mode: {what} needs {flag}" if flag else f"agent mode: cannot ask '{what}'")


def choose(title: str, options: list[str], flag: str = "") -> int | None:
    """Numbered menu; None on q or empty input."""
    if AUTO:
        raise _need(title, flag)
    print(f"\n{title}")
    for i, o in enumerate(options, 1):
        print(f"  {i:2}) {o}")
    while True:
        a = input(f"Choice [1-{len(options)}, q = back]: ").strip().lower()
        if a in ("", "q"):
            return None
        if a.isdigit() and 1 <= int(a) <= len(options):
            return int(a) - 1
        print("  invalid input")


def ask(question: str, default: str = "", check: Callable[[str], str | None] | None = None,
        example: str = "", flag: str = "") -> str:
    """Free text with default; check returns an error message or None."""
    check = check or (lambda a: None if a else "input required")
    if AUTO:
        if check(default) is None:
            print(f"{question}: {default}")
            return default
        raise _need(question, flag)
    hint = f" (e.g. {example})" if example else ""
    while True:
        a = input(f"{question}{hint}{f' [{default}]' if default else ''}: ").strip() or default
        err = check(a)
        if err is None:
            return a
        print(f"  {err}")


def yes(question: str, default: bool = False) -> bool:
    """An option (not a confirmation); agent mode takes the default."""
    if AUTO:
        return default
    a = input(f"{question} {'[Y/n]' if default else '[y/N]'} ").strip().lower()
    return default if a == "" else a in ("y", "yes", "j", "ja")


def confirm(question: str) -> bool:
    """Confirmation of an outward step (Komodo, Infisical, git push). Agent mode: yes only with --yes."""
    if AUTO:
        if not ASSUME_YES:
            raise DryRun(f"dry run: stopped before '{question}' (nothing changed from here on); pass --yes to apply")
        print(f"{question} -> yes (--yes)")
        return True
    return yes(question, True)


def hidden(label: str) -> str:
    """Hidden input, typed twice."""
    if AUTO:
        raise _need(label, "")
    value = getpass.getpass(f"{label}: ")
    if getpass.getpass("Repeat: ") != value:
        raise SystemExit("values do not match")
    return value


def secret_value(key: str, allow_skip: bool = False) -> tuple[str | None, bool]:
    """Type a value hidden or generate one. (value, generated); (None, False) = skipped."""
    if AUTO:
        raise _need(f"value for {key}", f"--secret {key}=generate|generate:BYTES|env|keep")
    options = "[t]ype it, [g]enerate a random one" + (", [k]eep the existing one / set later" if allow_skip else "")
    while True:
        a = input(f"{key}: {options}? ").strip().lower()
        if a == "g":
            n = input("  Length in bytes, as hex = twice as many characters (e.g. 16 -> 32 chars) [32]: ").strip()
            return secrets.token_hex(int(n) if n.isdigit() and int(n) > 0 else 32), True
        if a == "t":
            value = hidden(f"  Value for {key}")
            if value:
                return value, False
            print("  empty value")
        elif a == "k" and allow_skip:
            return None, False


def step(title: str, explain: str = "") -> None:
    print(f"\n── {title} " + "─" * max(0, 60 - len(title)))
    if explain:
        print(f"   {explain}")


def ok(msg: str) -> None:
    print(f"  ✓ {msg}")


def warn(msg: str) -> None:
    print(f"  ! {msg}")
