"""Command line. Exit codes: 0 ok, 1 error/blocker, 2 renewal failed and expiry is near, 3 pending."""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys

import requests
import yaml

from . import __version__, log
from .checks import Checker
from .errors import IngressError, ManifestError, PendingIssuance
from .infisical import InfisicalClient
from .inventory import load_inventory
from .lock import npm_lock
from .manifest import ServiceSpec, load_all
from .npm import NpmClient
from .presets import resolve_presets
from .reconcile import Context, _static_blockers, adopt, apply_service, plan_service, recover
from .state import State


def _secret(name: str) -> str:
    """Read the secret from NAME_FILE (preferred, e.g. a Docker secret) or NAME."""
    path = os.environ.get(f"{name}_FILE")
    if path:
        return pathlib.Path(path).read_text().strip()
    value = os.environ.get(name)
    if not value:
        raise IngressError(f"{name} or {name}_FILE is not set")
    return value


def _api_verify():
    return os.environ.get("INGRESSCTL_API_CA_BUNDLE") or True


def _secret_optional(name: str) -> str | None:
    """Like _secret, but without an error: missing DNS credentials are reported as a blocker."""
    path = os.environ.get(f"{name}_FILE")
    if path:
        return pathlib.Path(path).read_text().strip()
    return os.environ.get(name) or None


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
        if s.issuer_ref not in issuers and inv.issuers[s.issuer_ref].type == "infisical":
            cfg = inv.issuers[s.issuer_ref]
            client = InfisicalClient(cfg.api_url, _secret("INFISICAL_CLIENT_ID"), _secret("INFISICAL_CLIENT_SECRET"),
                                     verify=_api_verify())
            client.login()
            issuers[s.issuer_ref] = client
    repo = ",".join(inv.controller.get("allowed_repos") or []) or None
    needs_dns = any(s.dns_challenge and inv.issuers[s.issuer_ref].type == "letsencrypt" for s in specs)
    return Context(root=root, inv=inv, state=State(_state_dir()), npm=npm, issuers=issuers, checker=Checker(),
                   le_dns_credentials=_secret_optional("LETSENCRYPT_DNS_CREDENTIALS") if needs_dns else None,
                   source_repo=repo)


def _resolve_presets(ctx: Context, specs: list[ServiceSpec], *, persist: bool, session=None) -> None:
    """Resolve presets for the selected manifests once per run (public endpoint, standard TLS verification)."""
    names = {name for s in specs if s.public is not None for name in s.public.allow_presets}
    if not names:
        return
    if session is None:
        session = requests.Session()
    res = resolve_presets(ctx.state, names, session, persist=persist)
    ctx.preset_cidrs = res.cidrs
    ctx.preset_errors = res.errors
    ctx.preset_notes = res.notes


def _select(specs: list[ServiceSpec], stacks: list[str] | None) -> list[ServiceSpec]:
    if not stacks:
        return specs
    unknown = set(stacks) - {s.stack_id for s in specs}
    for u in sorted(unknown):
        log.event("select", "no-manifest", stack=u)  # stack without an ingress.yaml: nothing to do, nothing to delete
    return [s for s in specs if s.stack_id in stacks]


def cmd_validate(args) -> int:
    root = pathlib.Path(args.root).resolve()
    inv = load_inventory(root)
    try:
        specs = load_all(root, inv)
    except ManifestError as e:
        print(f"ERROR\n{e}", file=sys.stderr)
        return 1
    ctx = Context(root=root, inv=inv, state=None, npm={}, issuers={}, checker=None)
    gaps = 0
    for s in specs:
        for b in _static_blockers(ctx, s):
            gaps += 1
            print(f"WARN  {s.resource_id}: {b}", file=sys.stderr)
    print(f"ingressctl validate: {len(specs)} services, {gaps} open configuration gaps", file=sys.stderr)
    return 1 if (args.strict and gaps) else 0


def cmd_plan(args) -> int:
    root = pathlib.Path(args.root).resolve()
    inv = load_inventory(root)
    specs = _select(load_all(root, inv), args.stack)
    ctx = build_context(root, inv, specs)
    _resolve_presets(ctx, specs, persist=False)
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
            print(f"{row['resource_id']}\n  i managed, but without a manifest: no action (deletion only via lifecycle: absent)")
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
    """Commit of the checkout; Komodo brought it to the server with a repo pull."""
    res = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True)
    if res.returncode != 0:
        raise IngressError(f"{root} is not a git checkout; pass --commit")
    return res.stdout.strip()


def cmd_apply(args) -> int:
    root = pathlib.Path(args.root).resolve()
    if not args.all and not args.stack:
        raise IngressError("--stack or --all is required")
    args.commit = args.commit or _checkout_commit(root)
    inv = load_inventory(root)
    log.set_context(repo=",".join(inv.controller.get("allowed_repos") or []), commit=args.commit)
    specs = _select(load_all(root, inv), None if args.all else args.stack)
    if not specs:
        print(json.dumps({}))
        return 0
    ctx = build_context(root, inv, specs)
    _resolve_presets(ctx, specs, persist=True)
    res = _run_apply(ctx, specs, commit=args.commit, deployed_commit=args.deployed_commit,
                     allow_rollback=args.allow_rollback, skip_upstream_check=args.skip_upstream_check)
    print(json.dumps(res, indent=2, ensure_ascii=False))
    if any(v.startswith("error") for v in res.values()):
        return 1
    return 3 if "pending" in res.values() else 0


def cmd_renew(args) -> int:
    """Renews from the manifests last applied successfully in the state (not the branch HEAD)."""
    root = pathlib.Path(args.root).resolve() if args.root else None
    inv = load_inventory(pathlib.Path(args.inventory_root).resolve())
    state = State(_state_dir())
    specs = [ServiceSpec.from_json(r["manifest"]) for r in state.all() if r.get("manifest")]
    specs = [s for s in specs if s.lifecycle == "present"]
    if not specs:
        return 0
    ctx = build_context(root, inv, specs)
    _resolve_presets(ctx, specs, persist=True)
    by_commit = {r["resource_id"]: r.get("reconciled_commit") for r in state.all()}
    results = {}
    for s in specs:
        results.update(_run_apply(ctx, [s], commit=by_commit.get(s.resource_id), allow_rollback=False))
    print(json.dumps(results, indent=2, ensure_ascii=False))
    failed = [k for k, v in results.items() if v.startswith("error")]
    if failed:
        # renewal failed and expiry is near -> exit 2 (Komodo alert via the procedure's failure_alert)
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
    """Drop the state entry of a resource whose NPM host no longer exists (e.g. deleted by hand)."""
    root = pathlib.Path(args.root).resolve()
    inv = load_inventory(root)
    state = State(_state_dir())
    row = state.get(args.resource)
    if row is None:
        raise IngressError(f"{args.resource} is not in the state")
    if any(s.resource_id == args.resource for s in load_all(root, inv)):
        raise IngressError(f"{args.resource} is still declared in a manifest; remove the manifest first")
    npm_cfg = inv.npm_instances[row["npm_instance"]]
    client = NpmClient(npm_cfg.api_url, _secret("NPM_IDENTITY"), _secret("NPM_SECRET"), verify=_api_verify())
    client.login()
    for label, exists in (("host", client.get_proxy_host(row["npm_host_id"]) if row["npm_host_id"] else None),
                          ("certificate", client.get_certificate(row["npm_cert_id"]) if row["npm_cert_id"] else None)):
        if exists:
            raise IngressError(f"NPM {label} of {args.resource} still exists; remove it there first")
    with npm_lock(state.dir, row["npm_instance"]):
        state.forget(args.resource)
    log.event("forget", "ok", service=args.resource)
    return 0


def cmd_remove(args) -> int:
    """Delete the NPM host and certificate of a managed service that no manifest declares anymore; then the state.

    Part of stage G (lifecycle: absent) as an explicit command: only what belongs to this resource according to the
    state is deleted (the host ID with exactly the domains from the state, the certificate without further users)."""
    root = pathlib.Path(args.root).resolve()
    inv = load_inventory(root)
    state = State(_state_dir())
    row = state.get(args.resource)
    if row is None:
        raise IngressError(f"{args.resource} is not in the state")
    if any(s.resource_id == args.resource for s in load_all(root, inv)):
        raise IngressError(f"{args.resource} is still declared in a manifest; remove the manifest first")
    npm_cfg = inv.npm_instances[row["npm_instance"]]
    client = NpmClient(npm_cfg.api_url, _secret("NPM_IDENTITY"), _secret("NPM_SECRET"), verify=_api_verify())
    client.login()
    domains = set(json.loads(row["domains"]))
    with npm_lock(state.dir, row["npm_instance"]):
        host = client.get_proxy_host(row["npm_host_id"]) if row["npm_host_id"] else None
        if host:
            live = {d.lower() for d in host.get("domain_names") or []}
            if live != domains:
                raise IngressError(f"NPM host {host['id']} has {sorted(live)}, the state {sorted(domains)}; nothing deleted")
            client.delete_proxy_host(host["id"])
            log.event("remove", "host-deleted", service=args.resource, npm_host_id=host["id"])
        cert = client.get_certificate(row["npm_cert_id"]) if row["npm_cert_id"] else None
        if cert:
            users = client.certificate_users(cert["id"])
            if users:
                raise IngressError(f"NPM certificate {cert['id']} is still used by {users} host(s); nothing deleted")
            client.delete_certificate(cert["id"])
            log.event("remove", "cert-deleted", service=args.resource, npm_cert_id=cert["id"])
        state.forget(args.resource)
    log.event("remove", "ok", service=args.resource)
    return 0


def _spec_by_resource(root: pathlib.Path, inv, resource: str) -> ServiceSpec:
    spec = next((s for s in load_all(root, inv) if s.resource_id == resource), None)
    if spec is None:
        raise IngressError(f"{resource} is not declared in any manifest")
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

    p = sub.add_parser("validate", help="check manifests offline (no credentials)")
    p.add_argument("--root", default=".")
    p.add_argument("--strict", action="store_true", help="treat configuration gaps as errors")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("plan", help="read the live state and show changes (without mutating)")
    p.add_argument("--root", default=".")
    p.add_argument("--commit")
    p.add_argument("--stack", action="append")
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("apply", help="apply the manifests of the selected stacks")
    p.add_argument("--root", default=".")
    p.add_argument("--commit", help="commit of the manifests (default: HEAD of the checkout below --root)")
    p.add_argument("--stack", action="append")
    p.add_argument("--all", action="store_true", help="all manifests in the repo (Komodo procedure ingress-apply)")
    p.add_argument("--deployed-commit", help="commit Komodo actually rolled out (must match --commit)")
    p.add_argument("--allow-rollback", action="store_true")
    p.add_argument("--skip-upstream-check", action="store_true")
    p.set_defaults(func=cmd_apply)

    p = sub.add_parser("renew", help="check the certificates of all rolled-out services and renew them in time")
    p.add_argument("--all-deployed", action="store_true", required=True)
    p.add_argument("--inventory-root", default=".", help="checkout with inventory/ (addresses of the NPM/issuer APIs)")
    p.add_argument("--root", help="checkout for the commit sequence check (optional)")
    p.set_defaults(func=cmd_renew)

    p = sub.add_parser("status", help="show the operating status")
    p.add_argument("--namespace")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("adopt", help="explicitly adopt an existing NPM host (a snapshot is stored)")
    p.add_argument("--root", default=".")
    p.add_argument("--resource", required=True, help="<namespace>/<service-id>")
    p.add_argument("--npm-host-id", type=int, required=True)
    p.set_defaults(func=cmd_adopt)

    p = sub.add_parser("forget", help="drop a state entry whose NPM host no longer exists")
    p.add_argument("--root", default=".")
    p.add_argument("--resource", required=True, help="<namespace>/<service-id>")
    p.set_defaults(func=cmd_forget)

    p = sub.add_parser("remove", help="delete the NPM host + certificate of a service that is no longer declared")
    p.add_argument("--root", default=".")
    p.add_argument("--resource", required=True, help="<namespace>/<service-id>")
    p.set_defaults(func=cmd_remove)

    p = sub.add_parser("recover", help="restore the state after a loss from an explicit mapping")
    p.add_argument("--root", default=".")
    p.add_argument("--mapping", required=True, help="YAML list [{resource, npm_host_id, npm_cert_id}]")
    p.set_defaults(func=cmd_recover)

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except ManifestError as e:
        print(f"ERROR\n{e}", file=sys.stderr)
        return 1
    except IngressError as e:
        log.event(args.cmd, "error", error=str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
