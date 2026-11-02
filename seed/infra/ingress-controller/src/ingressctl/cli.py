"""Kommandozeile. Exit-Codes: 0 ok, 1 Fehler/Blocker, 2 Erneuerung fehlgeschlagen und Ablauf naht, 3 ausstehend."""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys

import yaml

from . import __version__, log
from .checks import Checker
from .errors import IngressError, ManifestError, PendingIssuance
from .infisical import InfisicalClient
from .inventory import load_inventory
from .lock import npm_lock
from .manifest import ServiceSpec, load_all
from .npm import NpmClient
from .reconcile import Context, _static_blockers, adopt, apply_service, plan_service, recover
from .state import State


def _secret(name: str) -> str:
    """Secret aus NAME_FILE (bevorzugt, z. B. Docker-Secret) oder NAME lesen."""
    path = os.environ.get(f"{name}_FILE")
    if path:
        return pathlib.Path(path).read_text().strip()
    value = os.environ.get(name)
    if not value:
        raise IngressError(f"{name} bzw. {name}_FILE ist nicht gesetzt")
    return value


def _api_verify():
    return os.environ.get("INGRESSCTL_API_CA_BUNDLE") or True


def _state_dir() -> pathlib.Path:
    return pathlib.Path(os.environ.get("INGRESSCTL_STATE_DIR", "/var/lib/ingressctl"))


def build_context(root: pathlib.Path | None, inv, specs: list[ServiceSpec]) -> Context:
    npm, issuers = {}, {}
    for s in specs:
        if s.npm_instance not in npm:
            cfg = inv.npm_instances[s.npm_instance]
            client = NpmClient(cfg.api_url, _secret("NPM_IDENTITY"), _secret("NPM_SECRET"), verify=_api_verify())
            client.login()
            npm[s.npm_instance] = client
        if s.issuer_ref not in issuers:
            cfg = inv.issuers[s.issuer_ref]
            client = InfisicalClient(cfg.api_url, _secret("INFISICAL_CLIENT_ID"), _secret("INFISICAL_CLIENT_SECRET"),
                                     verify=_api_verify())
            client.login()
            issuers[s.issuer_ref] = client
    repo = ",".join(inv.controller.get("allowed_repos") or []) or None
    return Context(root=root, inv=inv, state=State(_state_dir()), npm=npm, issuers=issuers, checker=Checker(),
                   source_repo=repo)


def _select(specs: list[ServiceSpec], stacks: list[str] | None) -> list[ServiceSpec]:
    if not stacks:
        return specs
    unknown = set(stacks) - {s.stack_id for s in specs}
    for u in sorted(unknown):
        log.event("select", "no-manifest", stack=u)  # Stack ohne ingress.yaml: nichts zu tun, nichts zu löschen
    return [s for s in specs if s.stack_id in stacks]


def cmd_validate(args) -> int:
    root = pathlib.Path(args.root).resolve()
    inv = load_inventory(root)
    try:
        specs = load_all(root, inv)
    except ManifestError as e:
        print(f"FEHLER\n{e}", file=sys.stderr)
        return 1
    ctx = Context(root=root, inv=inv, state=None, npm={}, issuers={}, checker=None)
    gaps = 0
    for s in specs:
        for b in _static_blockers(ctx, s):
            gaps += 1
            print(f"WARN  {s.resource_id}: {b}", file=sys.stderr)
    print(f"ingressctl validate: {len(specs)} Services, {gaps} offene Konfigurationslücken", file=sys.stderr)
    return 1 if (args.strict and gaps) else 0


def cmd_plan(args) -> int:
    root = pathlib.Path(args.root).resolve()
    inv = load_inventory(root)
    specs = _select(load_all(root, inv), args.stack)
    ctx = build_context(root, inv, specs)
    blocked = False
    cache: dict[str, tuple] = {}
    for s in specs:
        if s.npm_instance not in cache:
            npm = ctx.npm[s.npm_instance]
            cache[s.npm_instance] = (npm.list_proxy_hosts(), {c["id"]: c for c in npm.list_certificates()})
        p = plan_service(ctx, s, *cache[s.npm_instance])
        blocked |= bool(p.blockers)
        print(p.render())
    for row in ctx.state.all():
        if row["resource_id"] not in {s.resource_id for s in load_all(root, inv)}:
            print(f"{row['resource_id']}\n  i verwaltet, aber ohne Manifest: keine Aktion (Löschen nur über lifecycle: absent)")
    return 1 if blocked else 0


def _run_apply(ctx: Context, specs: list[ServiceSpec], **kw) -> dict:
    results = {}
    for instance in sorted({s.npm_instance for s in specs}):
        with npm_lock(ctx.state.dir, instance):
            for s in [x for x in specs if x.npm_instance == instance]:
                try:
                    results[s.resource_id] = apply_service(ctx, s, **kw)
                except PendingIssuance as e:
                    results[s.resource_id] = "pending"
                    log.event("apply", "pending", service=s.resource_id, request_id=e.request_id)
                except IngressError as e:
                    results[s.resource_id] = f"error: {e}"
                    log.event("apply", "error", service=s.resource_id, error=str(e))
    return results


def _checkout_commit(root: pathlib.Path) -> str:
    """Commit des Checkouts; Komodo hat ihn per Repo-Pull auf den Server gebracht."""
    res = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True)
    if res.returncode != 0:
        raise IngressError(f"{root} ist kein Git-Checkout; --commit angeben")
    return res.stdout.strip()


def cmd_apply(args) -> int:
    root = pathlib.Path(args.root).resolve()
    if not args.all and not args.stack:
        raise IngressError("--stack oder --all angeben")
    args.commit = args.commit or _checkout_commit(root)
    inv = load_inventory(root)
    log.set_context(repo=",".join(inv.controller.get("allowed_repos") or []), commit=args.commit)
    specs = _select(load_all(root, inv), None if args.all else args.stack)
    if not specs:
        print(json.dumps({}))
        return 0
    ctx = build_context(root, inv, specs)
    res = _run_apply(ctx, specs, commit=args.commit, deployed_commit=args.deployed_commit,
                     allow_rollback=args.allow_rollback, skip_upstream_check=args.skip_upstream_check)
    print(json.dumps(res, indent=2, ensure_ascii=False))
    if any(v.startswith("error") for v in res.values()):
        return 1
    return 3 if "pending" in res.values() else 0


def cmd_renew(args) -> int:
    """Erneuert anhand der zuletzt erfolgreich angewendeten Manifeste aus dem State (nicht Branch-HEAD)."""
    root = pathlib.Path(args.root).resolve() if args.root else None
    inv = load_inventory(pathlib.Path(args.inventory_root).resolve())
    state = State(_state_dir())
    specs = [ServiceSpec.from_json(r["manifest"]) for r in state.all() if r.get("manifest")]
    specs = [s for s in specs if s.lifecycle == "present"]
    if not specs:
        return 0
    ctx = build_context(root, inv, specs)
    by_commit = {r["resource_id"]: r.get("reconciled_commit") for r in state.all()}
    results = {}
    for s in specs:
        results.update(_run_apply(ctx, [s], commit=by_commit.get(s.resource_id), allow_rollback=False))
    print(json.dumps(results, indent=2, ensure_ascii=False))
    failed = [k for k, v in results.items() if v.startswith("error")]
    if failed:
        # Fehlgeschlagene Erneuerung bei nahem Ablauf -> Exit 2 (Komodo-Alert über failure_alert der Procedure)
        return 2
    return 0


def cmd_status(args) -> int:
    state = State(_state_dir())
    rows = state.all(args.namespace)
    cols = ("resource_id", "stack_id", "npm_host_id", "npm_cert_id", "cert_not_after", "reconciled_commit", "last_result")
    if args.json:
        print(json.dumps([{c: r.get(c) for c in cols + ("last_error",)} for r in rows], indent=2))
        return 0
    for r in rows:
        print("  ".join(f"{c}={(r.get(c) or '-') if c != 'reconciled_commit' else (r.get(c) or '-')[:12]}" for c in cols))
        if r.get("last_error"):
            print(f"    last_error: {r['last_error']}")
    return 0


def cmd_forget(args) -> int:
    """State-Eintrag einer Ressource entfernen, deren NPM-Host nicht mehr existiert (z. B. manuell gelöscht)."""
    root = pathlib.Path(args.root).resolve()
    inv = load_inventory(root)
    state = State(_state_dir())
    row = state.get(args.resource)
    if row is None:
        raise IngressError(f"{args.resource} ist nicht im State")
    if any(s.resource_id == args.resource for s in load_all(root, inv)):
        raise IngressError(f"{args.resource} ist noch in einem Manifest deklariert; erst das Manifest entfernen")
    npm_cfg = inv.npm_instances[row["npm_instance"]]
    client = NpmClient(npm_cfg.api_url, _secret("NPM_IDENTITY"), _secret("NPM_SECRET"), verify=_api_verify())
    client.login()
    for label, exists in (("Host", client.get_proxy_host(row["npm_host_id"]) if row["npm_host_id"] else None),
                          ("Zertifikat", client.get_certificate(row["npm_cert_id"]) if row["npm_cert_id"] else None)):
        if exists:
            raise IngressError(f"NPM-{label} von {args.resource} existiert noch; erst dort entfernen")
    with npm_lock(state.dir, row["npm_instance"]):
        state.forget(args.resource)
    log.event("forget", "ok", service=args.resource)
    return 0


def cmd_remove(args) -> int:
    """NPM-Host und Zertifikat eines verwalteten Service löschen, den kein Manifest mehr deklariert; dann den State.

    Teil von Etappe G (lifecycle: absent) als expliziter Befehl: gelöscht wird nur, was laut State zu dieser
    Ressource gehört (Host-ID mit exakt den Domains aus dem State, Zertifikat ohne weitere Nutzer)."""
    root = pathlib.Path(args.root).resolve()
    inv = load_inventory(root)
    state = State(_state_dir())
    row = state.get(args.resource)
    if row is None:
        raise IngressError(f"{args.resource} ist nicht im State")
    if any(s.resource_id == args.resource for s in load_all(root, inv)):
        raise IngressError(f"{args.resource} ist noch in einem Manifest deklariert; erst das Manifest entfernen")
    npm_cfg = inv.npm_instances[row["npm_instance"]]
    client = NpmClient(npm_cfg.api_url, _secret("NPM_IDENTITY"), _secret("NPM_SECRET"), verify=_api_verify())
    client.login()
    domains = set(json.loads(row["domains"]))
    with npm_lock(state.dir, row["npm_instance"]):
        host = client.get_proxy_host(row["npm_host_id"]) if row["npm_host_id"] else None
        if host:
            live = {d.lower() for d in host.get("domain_names") or []}
            if live != domains:
                raise IngressError(f"NPM-Host {host['id']} hat {sorted(live)}, der State {sorted(domains)}; nichts gelöscht")
            client.delete_proxy_host(host["id"])
            log.event("remove", "host-deleted", service=args.resource, npm_host_id=host["id"])
        cert = client.get_certificate(row["npm_cert_id"]) if row["npm_cert_id"] else None
        if cert:
            users = client.certificate_users(cert["id"])
            if users:
                raise IngressError(f"NPM-Zertifikat {cert['id']} wird noch von {users} Host(s) genutzt; nicht gelöscht")
            client.delete_certificate(cert["id"])
            log.event("remove", "cert-deleted", service=args.resource, npm_cert_id=cert["id"])
        state.forget(args.resource)
    log.event("remove", "ok", service=args.resource)
    return 0


def _spec_by_resource(root: pathlib.Path, inv, resource: str) -> ServiceSpec:
    spec = next((s for s in load_all(root, inv) if s.resource_id == resource), None)
    if spec is None:
        raise IngressError(f"{resource} ist in keinem Manifest deklariert")
    return spec


def cmd_adopt(args) -> int:
    root = pathlib.Path(args.root).resolve()
    inv = load_inventory(root)
    spec = _spec_by_resource(root, inv, args.resource)
    ctx = build_context(root, inv, [spec])
    with npm_lock(ctx.state.dir, spec.npm_instance):
        adopt(ctx, spec, args.npm_host_id)
    return 0


def cmd_recover(args) -> int:
    root = pathlib.Path(args.root).resolve()
    inv = load_inventory(root)
    mapping = yaml.safe_load(pathlib.Path(args.mapping).read_text()) or []
    specs = [_spec_by_resource(root, inv, m["resource"]) for m in mapping]
    ctx = build_context(root, inv, specs)
    for m, spec in zip(mapping, specs):
        with npm_lock(ctx.state.dir, spec.npm_instance):
            recover(ctx, spec, int(m["npm_host_id"]), m.get("npm_cert_id"))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="ingressctl", description=f"ingressctl {__version__}")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("validate", help="Manifeste offline prüfen (keine Credentials)")
    p.add_argument("--root", default=".")
    p.add_argument("--strict", action="store_true", help="Konfigurationslücken als Fehler werten")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("plan", help="Live-Zustand lesen und Änderungen anzeigen (ohne Mutation)")
    p.add_argument("--root", default=".")
    p.add_argument("--commit")
    p.add_argument("--stack", action="append")
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("apply", help="Manifeste ausgewählter Stacks anwenden")
    p.add_argument("--root", default=".")
    p.add_argument("--commit", help="Commit des Manifests (Standard: HEAD des Checkouts unter --root)")
    p.add_argument("--stack", action="append")
    p.add_argument("--all", action="store_true", help="alle Manifeste im Repo (Komodo-Procedure ingress-apply)")
    p.add_argument("--deployed-commit", help="von Komodo tatsächlich ausgerollter Commit (muss --commit entsprechen)")
    p.add_argument("--allow-rollback", action="store_true")
    p.add_argument("--skip-upstream-check", action="store_true")
    p.set_defaults(func=cmd_apply)

    p = sub.add_parser("renew", help="Zertifikate aller ausgerollten Services prüfen und rechtzeitig erneuern")
    p.add_argument("--all-deployed", action="store_true", required=True)
    p.add_argument("--inventory-root", default=".", help="Checkout mit inventory/ (Adressen der NPM-/Issuer-APIs)")
    p.add_argument("--root", help="Checkout für Commit-Reihenfolgeprüfung (optional)")
    p.set_defaults(func=cmd_renew)

    p = sub.add_parser("status", help="Betriebsstatus anzeigen")
    p.add_argument("--namespace")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("adopt", help="bestehenden NPM-Host explizit übernehmen (Snapshot wird gespeichert)")
    p.add_argument("--root", default=".")
    p.add_argument("--resource", required=True, help="<namespace>/<service-id>")
    p.add_argument("--npm-host-id", type=int, required=True)
    p.set_defaults(func=cmd_adopt)

    p = sub.add_parser("forget", help="State-Eintrag entfernen, dessen NPM-Host nicht mehr existiert")
    p.add_argument("--root", default=".")
    p.add_argument("--resource", required=True, help="<namespace>/<service-id>")
    p.set_defaults(func=cmd_forget)

    p = sub.add_parser("remove", help="NPM-Host + Zertifikat eines nicht mehr deklarierten Service löschen")
    p.add_argument("--root", default=".")
    p.add_argument("--resource", required=True, help="<namespace>/<service-id>")
    p.set_defaults(func=cmd_remove)

    p = sub.add_parser("recover", help="State nach Verlust über explizites Mapping wiederherstellen")
    p.add_argument("--root", default=".")
    p.add_argument("--mapping", required=True, help="YAML-Liste [{resource, npm_host_id, npm_cert_id}]")
    p.set_defaults(func=cmd_recover)

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except ManifestError as e:
        print(f"FEHLER\n{e}", file=sys.stderr)
        return 1
    except IngressError as e:
        log.event(args.cmd, "error", error=str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
