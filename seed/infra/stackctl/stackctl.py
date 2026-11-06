#!/usr/bin/env python3
"""stackctl: Inventar validieren und betroffene Stacks einer Git-Änderung bestimmen.

Ausschließlich lesend; benötigt keine Credentials. Abhängigkeiten: Python >= 3.11, PyYAML, git.

  stackctl validate --root .
  stackctl affected --root . --head <sha> (--base <sha> | --deployed deployed.json)

`--deployed` ist eine JSON-Map {stack_id: zuletzt erfolgreich deployter Commit | null}. Dadurch wird je
Stack gegen dessen eigenen Stand verglichen, nicht nur gegen den letzten Push-Diff.
"""
from __future__ import annotations

import argparse
import functools
import json
import pathlib
import re
import subprocess
import sys

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "homelab"))
import stacklist  # noqa: E402

STACK_KEYS = {"id", "namespace", "path", "compose_files", "legacy", "komodo_stack", "server", "project_name",
              "status", "extra_files", "notes", "secrets", "job"}
STACK_REQUIRED = {"id", "namespace", "path", "compose_files", "status"}
STATUSES = {"unverified", "verified", "inactive", "unmanaged", "derived"}  # unmanaged: nicht über Komodo ausgerollt; derived: nur als Ordner mit compose.yaml + ingress.yaml
SECRET_SOURCES = {"infisical"}  # infra/infisical/stack_secrets.py
INGRESS_FILE = "ingress.yaml"
INGRESS_GLOBAL = ("inventory/ingress.yaml", "inventory/namespaces.yaml")


class ValidationError(Exception):
    pass


@functools.lru_cache(maxsize=None)
def _glob_regex(pattern: str) -> re.Pattern:
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def glob_match(path: str, patterns) -> bool:
    return any(_glob_regex(p).match(path) for p in patterns)


def git(root: pathlib.Path, *args: str, check: bool = True) -> str:
    res = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    if check and res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} fehlgeschlagen: {res.stderr.strip()}")
    return res.stdout


def load_yaml_text(text: str | None, default):
    if not text:
        return default
    return yaml.safe_load(text) or default


def read_at(root: pathlib.Path, commit: str | None, rel: str):
    """Datei-Inhalt zu einem Commit (None = Arbeitsverzeichnis). Fehlende Datei -> None."""
    if commit is None:
        p = root / rel
        return p.read_text() if p.exists() else None
    res = subprocess.run(["git", "-C", str(root), "show", f"{commit}:{rel}"], capture_output=True, text=True)
    return res.stdout if res.returncode == 0 else None


class Inventory:
    def __init__(self, root: pathlib.Path, commit: str | None = None):
        self.root = root
        self.commit = commit
        self.stacks = load_yaml_text(read_at(root, commit, "inventory/stacks.yaml"), {}).get("stacks") or []
        if commit is None:   # Ordner mit compose.yaml + ingress.yaml ohne Eintrag (im Arbeitsverzeichnis; ein Commit-Stand kennt sie nicht)
            self.stacks = self.stacks + stacklist.derived(root, self.stacks)
        self.servers = load_yaml_text(read_at(root, commit, "inventory/servers.yaml"), {}).get("servers") or {}
        self.namespaces = load_yaml_text(read_at(root, commit, "inventory/namespaces.yaml"), {}).get("namespaces") or {}
        self.paths = load_yaml_text(read_at(root, commit, "inventory/paths.yaml"), {})

    def by_id(self) -> dict:
        return {s["id"]: s for s in self.stacks if isinstance(s, dict) and "id" in s}


def validate(root: pathlib.Path, strict: bool = False) -> tuple[list[str], list[str]]:
    inv = Inventory(root)
    errors, warnings = [], []
    seen = set()
    for i, s in enumerate(inv.stacks):
        where = f"inventory/stacks.yaml[{i}]"
        if not isinstance(s, dict):
            errors.append(f"{where}: Eintrag ist kein Objekt")
            continue
        where = f"{where} ({s.get('id')})"
        for k in sorted(set(s) - STACK_KEYS):
            errors.append(f"{where}: unbekanntes Feld '{k}'")
        for k in sorted(STACK_REQUIRED - set(s)):
            errors.append(f"{where}: Pflichtfeld '{k}' fehlt")
        sid = s.get("id")
        if sid in seen:
            errors.append(f"{where}: doppelte Stack-ID")
        seen.add(sid)
        ns = s.get("namespace")
        path = str(s.get("path", ""))
        if ns not in inv.namespaces:
            errors.append(f"{where}: unbekannter Namespace '{ns}'")
        elif not any((path + "/").startswith(p) for p in inv.namespaces[ns].get("paths", [])):
            errors.append(f"{where}: Pfad '{path}' liegt nicht im Namespace '{ns}'")
        if not (root / path).is_dir():
            errors.append(f"{where}: Verzeichnis '{path}' fehlt")
        for f in s.get("compose_files") or []:
            cf = root / path / f
            if not cf.is_file():
                errors.append(f"{where}: Compose-Datei '{path}/{f}' fehlt")
                continue
            try:
                doc = yaml.safe_load(cf.read_text())
                if not isinstance(doc, dict) or not isinstance(doc.get("services"), dict):
                    errors.append(f"{where}: '{path}/{f}' enthält keine services")
            except yaml.YAMLError as e:
                errors.append(f"{where}: '{path}/{f}' ist kein gültiges YAML: {str(e).splitlines()[0]}")
        for f in s.get("extra_files") or []:
            if not (root / f).exists():
                errors.append(f"{where}: extra_file '{f}' fehlt")
        if s.get("server") is not None and s["server"] not in inv.servers:
            errors.append(f"{where}: unbekannter Server '{s['server']}'")
        if s.get("status") not in STATUSES:
            errors.append(f"{where}: status muss einer von {sorted(STATUSES)} sein")
        if "secrets" in s:
            if s["secrets"] not in SECRET_SOURCES:
                errors.append(f"{where}: secrets muss einer von {sorted(SECRET_SOURCES)} sein")
            elif s.get("komodo_stack") is None or s.get("server") is None:
                errors.append(f"{where}: secrets braucht komodo_stack und server")
        if "job" in s:  # einmaliger Lauf: die UI liest den Exit-Code der Container über Komodo
            if s["job"] is not True:
                errors.append(f"{where}: job muss true sein (oder fehlen)")
            elif s.get("komodo_stack") is None:
                errors.append(f"{where}: job braucht komodo_stack")
        missing = [k for k in ("komodo_stack", "server", "project_name") if s.get(k) is None]
        if missing and s.get("status") in ("unverified", "verified"):
            (errors if strict else warnings).append(f"{where}: noch unbestätigt: {', '.join(missing)}")
    for pattern, targets in (inv.paths.get("shared") or {}).items():
        for t in targets or []:
            if t not in seen:
                errors.append(f"inventory/paths.yaml shared['{pattern}']: unbekannte Stack-ID '{t}'")
    return errors, warnings


def _changed_files(root: pathlib.Path, base: str, head: str) -> list[str]:
    out = git(root, "diff", "--name-status", "-M", base, head)
    files = []
    for line in out.splitlines():
        parts = line.split("\t")
        files.extend(parts[1:])  # bei Umbenennung alter und neuer Pfad
    return sorted(set(files))


def _commit_exists(root: pathlib.Path, sha: str) -> bool:
    return subprocess.run(["git", "-C", str(root), "cat-file", "-e", f"{sha}^{{commit}}"],
                          capture_output=True).returncode == 0


def _is_ancestor(root: pathlib.Path, a: str, b: str) -> bool:
    return subprocess.run(["git", "-C", str(root), "merge-base", "--is-ancestor", a, b],
                          capture_output=True).returncode == 0


def _stack_hit(stack: dict, siblings: list[dict], path: str) -> str | None:
    """'app', 'ingress' oder None für eine geänderte Datei relativ zu einem Stack."""
    base = stack["path"].rstrip("/") + "/"
    if path in (stack.get("extra_files") or []):
        return "app"
    if not path.startswith(base):
        return None
    rel = path[len(base):]
    if rel == INGRESS_FILE:
        return "ingress"
    other_compose = {f for s in siblings if s is not stack for f in s.get("compose_files") or []}
    if rel in other_compose and rel not in (stack.get("compose_files") or []):
        return None  # Compose-Datei einer anderen Variante im selben Verzeichnis
    return "app"


def affected(root: pathlib.Path, head: str, base: str | None = None, deployed: dict | None = None) -> dict:
    head = git(root, "rev-parse", "--verify", f"{head}^{{commit}}").strip()
    inv_head = Inventory(root, head)
    stacks = inv_head.stacks
    rules = inv_head.paths
    by_path: dict[str, list[dict]] = {}
    for s in stacks:
        by_path.setdefault(s["path"], []).append(s)

    result = {"head": head, "stacks": [], "controller": False, "unclassified": [], "no_deploy": []}
    classified_any: dict[str, bool] = {}
    diff_cache: dict[str, list[str]] = {}

    for s in stacks:
        sid = s["id"]
        b = base if deployed is None else deployed.get(sid)
        entry = {"id": sid, "app": False, "ingress": False, "baseline": b, "reasons": []}
        if b is None or not _commit_exists(root, b):
            entry.update(app=True, ingress=True)
            entry["reasons"].append("kein bekannter Deploy-Stand" if b is None else f"Baseline {b} nicht im Checkout")
            entry["requires_confirmation"] = b is not None
            result["stacks"].append(entry)
            continue
        if not _is_ancestor(root, b, head):
            entry["requires_confirmation"] = True
            entry["reasons"].append("Baseline ist kein Vorfahre von head (Rollback/Force-Push?)")
        if b not in diff_cache:
            diff_cache[b] = _changed_files(root, b, head)
        files = diff_cache[b]

        inv_base = Inventory(root, b)
        old = inv_base.by_id().get(sid)
        if old != s:
            entry["app"] = True
            entry["reasons"].append("Inventareintrag neu oder geändert")
        if any(f in files for f in INGRESS_GLOBAL) and read_at(root, head, f"{s['path']}/{INGRESS_FILE}") is not None:
            entry["ingress"] = True
            entry["reasons"].append("globale Ingress-Konfiguration geändert")
        srv = s.get("server")
        if srv and inv_base.servers.get(srv) != inv_head.servers.get(srv):
            entry["ingress"] = True
            entry["reasons"].append(f"Servereintrag '{srv}' geändert")

        for f in files:
            hit = _stack_hit(s, by_path.get(s["path"], []), f)
            if hit is None:
                for pattern, targets in (rules.get("shared") or {}).items():
                    if sid in (targets or []) and glob_match(f, [pattern]):
                        hit = "app"
            if hit is None:
                continue
            if hit == "app" and glob_match(f, rules.get("no_deploy") or []):
                continue
            entry[hit] = True
            entry["reasons"].append(f"{hit}: {f}")
        if entry["app"] or entry["ingress"] or entry.get("requires_confirmation"):
            result["stacks"].append(entry)

    all_files = sorted({f for fl in diff_cache.values() for f in fl})
    stack_paths = [s["path"].rstrip("/") + "/" for s in stacks]
    extra = {f for s in stacks for f in s.get("extra_files") or []}
    for f in all_files:
        if glob_match(f, rules.get("no_deploy") or []):
            result["no_deploy"].append(f)
        elif glob_match(f, rules.get("controller") or []):
            result["controller"] = True
        elif f == "inventory/paths.yaml":
            result["no_deploy"].append(f)  # Klassifikationsregeln selbst ändern keine Deploy-Ziele
        elif f in ("inventory/stacks.yaml", "inventory/servers.yaml", *INGRESS_GLOBAL):
            pass  # pro Eintrag oben ausgewertet
        elif any(f.startswith(p) for p in stack_paths) or f in extra:
            pass
        elif any(glob_match(f, [p]) for p in (rules.get("shared") or {})):
            pass
        else:
            result["unclassified"].append(f)
    return result


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="stackctl")
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("validate")
    v.add_argument("--root", default=".")
    v.add_argument("--strict", action="store_true", help="unbestätigte Felder als Fehler werten")
    a = sub.add_parser("affected")
    a.add_argument("--root", default=".")
    a.add_argument("--head", required=True)
    g = a.add_mutually_exclusive_group(required=True)
    g.add_argument("--base")
    g.add_argument("--deployed", help="JSON-Datei {stack_id: sha|null}")
    args = ap.parse_args(argv)
    root = pathlib.Path(args.root).resolve()

    if args.cmd == "validate":
        errors, warnings = validate(root, strict=args.strict)
        for w in warnings:
            print(f"WARN  {w}", file=sys.stderr)
        for e in errors:
            print(f"ERROR {e}", file=sys.stderr)
        print(f"stackctl validate: {len(errors)} Fehler, {len(warnings)} Warnungen", file=sys.stderr)
        return 1 if errors else 0

    deployed = json.loads(pathlib.Path(args.deployed).read_text()) if args.deployed else None
    res = affected(root, args.head, base=args.base, deployed=deployed)
    print(json.dumps(res, indent=2, ensure_ascii=False))
    return 3 if res["unclassified"] else 0


if __name__ == "__main__":
    sys.exit(main())
