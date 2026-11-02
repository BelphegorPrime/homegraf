#!/usr/bin/env python3
"""ingressctl im Komodo-Stack auf dem Management-Server ausführen (RunStackService) und Ausgabe zeigen.

  . infra/komodo/komodo.env
  python infra/komodo/run_ingressctl.py plan --root /repo --stack omniroute
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import komodo_api as k  # noqa: E402


def run(argv: list[str], echo: bool = True) -> tuple[int, str]:
    """(Exit-Code, Ausgabe) eines ingressctl-Laufs im Stack ingressctl."""
    u = k.execute_and_poll("RunStackService", {"stack": "ingressctl", "service": "ingressctl", "command": argv,
                                               "no_deps": True, "no_tty": True}, timeout=1800)
    out = ""
    for lg in u.get("logs") or []:
        if lg.get("stdout"):
            if echo:
                print(lg["stdout"])
            out += lg["stdout"]
        if lg.get("stderr"):
            if echo:
                print(lg["stderr"], file=sys.stderr)
            out += lg["stderr"]
    # Komodo meldet den Lauf auch bei Exit-Code != 0 als erfolgreich -> Ergebnis aus der Ausgabe ableiten
    failed = '"result": "error"' in out or "BLOCKER" in out or "FEHLER" in out
    return (0 if u.get("success") and not failed else 1), out


def main(argv: list[str]) -> int:
    return run(argv)[0]


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
