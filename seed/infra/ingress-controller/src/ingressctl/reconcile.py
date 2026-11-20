"""Plan and apply: reconcile ServiceSpecs with NPM and Infisical.

Principles:
- Only resources with an unambiguous ownership record in the state are changed; foreign hosts with the same
  domain abort before any mutation.
- After every external side effect the progress is saved in `operations`. A re-run first reconciles the live
  state (marker in the NPM certificate name, Infisical metadata) instead of blindly creating anew.
- New certificate: issue -> validate -> upload as a new NPM object -> switch the host -> verify HTTPS;
  on failure back to the previous certificate. Old objects are deleted only afterwards and only without references.
- Missing manifests are never a signal to delete.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import subprocess
import time
from dataclasses import dataclass, field

from . import certs, log
from .checks import Checker
from .errors import AdapterError, CertificateError, ConflictError, IngressError, PendingIssuance
from .infisical import InfisicalClient
from .inventory import Inventory
from .manifest import ServiceSpec
from .npm import CERT_NAME_PREFIX, MANAGED_HOST_FIELDS, NpmClient, host_matches_domains
from .public import MANAGED_MARKER, is_managed_config, remove_managed_config, render_public_config
from .state import State

OP_ISSUE = "issue"
OP_HOST = "host"
INFISICAL_OP_KEY = "ingressctl-op"


@dataclass
class Context:
    root: pathlib.Path | None
    inv: Inventory
    state: State
    npm: dict[str, NpmClient]
    issuers: dict[str, InfisicalClient]
    checker: Checker
    clock: callable = lambda: dt.datetime.now(dt.timezone.utc)
    source_repo: str | None = None
    # DNS-01 with Let's Encrypt: the provider's credentials, never logged
    le_dns_credentials: str | None = None
    # NPM answers PUT/POST before nginx has finished the reload -> retry the check
    verify_attempts: int = 6
    verify_delay: float = 2.0
    preset_cidrs: dict = field(default_factory=dict)
    preset_errors: dict = field(default_factory=dict)
    preset_notes: list = field(default_factory=list)

    def trust_pem(self, issuer_ref: str) -> str:
        path = self.inv.issuers[issuer_ref].ca_bundle
        return pathlib.Path(path).read_text()


@dataclass
class ServicePlan:
    spec: ServiceSpec
    create_host: bool = False
    host_changes: dict = field(default_factory=dict)   # field -> (live, desired)
    cert_reasons: list[str] = field(default_factory=list)
    drift: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    resume_host_id: int | None = None
    renew_cert: bool = False
    renew_not_after: dt.datetime | None = None

    @property
    def noop(self) -> bool:
        return not (self.create_host or self.host_changes or self.cert_reasons or self.renew_cert)

    def render(self) -> str:
        s = self.spec
        lines = [f"{s.resource_id} ({', '.join(s.domains)} -> {s.upstream_scheme}://{s.upstream_host}:{s.upstream_port})"]
        lines += [f"  BLOCKER {b}" for b in self.blockers]
        lines += [f"  DRIFT   {d}" for d in self.drift]
        if self.create_host:
            lines.append("  + Create proxy host")
        for k, (old, new) in self.host_changes.items():
            if k == "advanced_config":
                lines.append("  ~ advanced_config:")
                lines += [f"      - {l}" for l in (str(old or "").splitlines() or [""])]
                lines += [f"      + {l}" for l in (str(new or "").splitlines() or [""])]
            else:
                lines.append(f"  ~ {k}: {old!r} -> {new!r}")
        for r in self.cert_reasons:
            lines.append(f"  * Issue certificate: {r}")
        if self.renew_cert:
            until = f"{self.renew_not_after:%Y-%m-%d}" if self.renew_not_after else "?"
            lines.append(f"  * Renew Let's Encrypt certificate (expires on {until})")
        lines += [f"  i {n}" for n in self.notes]
        if self.noop and not self.blockers:
            lines.append("  = no change")
        return "\n".join(lines)


def desired_host(spec: ServiceSpec, cert_id: int | None, preset_cidrs: dict | None = None) -> dict:
    host = {
        "domain_names": list(spec.domains),
        "forward_scheme": spec.upstream_scheme,
        "forward_host": spec.upstream_host,
        "forward_port": spec.upstream_port,
        "certificate_id": cert_id or 0,
        "ssl_forced": bool(spec.force_https and cert_id),
        "allow_websocket_upgrade": spec.websockets,
        "enabled": True,
    }
    if spec.public is not None:
        host["advanced_config"] = render_public_config(spec.public, preset_cidrs)
    return host


def _norm(field_name: str, value):
    if field_name == "domain_names":
        return sorted(d.lower() for d in value or [])
    if field_name == "forward_host" and isinstance(value, str):
        return value.lower()
    if field_name == "certificate_id":
        return int(value or 0)
    return value


def _static_blockers(ctx: Context, spec: ServiceSpec) -> list[str]:
    b = []
    npm = ctx.inv.npm_instances.get(spec.npm_instance)
    if npm is None or not npm.api_url or not npm.tls_address:
        b.append(f"NPM instance '{spec.npm_instance}' is not fully configured in inventory/ingress.yaml")
    missing = ctx.inv.issuers[spec.issuer_ref].missing()
    if missing:
        b.append(f"Issuer '{spec.issuer_ref}' is incomplete: {', '.join(missing)}")
    if spec.dns_challenge and ctx.npm and not ctx.le_dns_credentials:
        b.append("LETSENCRYPT_DNS_CREDENTIALS is missing")
    if spec.upstream_host is None:
        b.append(f"Upstream server '{spec.upstream_server}' has no confirmed address in inventory/servers.yaml")
    return b


def _parse_expires(value) -> dt.datetime | None:
    """NPM returns expires_on as 'YYYY-MM-DD HH:MM:SS' (older versions with T/Z); no zone = UTC."""
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00").replace(" ", "T", 1)
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _plan_le_certificate(ctx: Context, spec: ServiceSpec, st: dict | None, live_cert: dict | None,
                         p: ServicePlan) -> None:
    """Let's Encrypt: no PEM over the API -> no fingerprint comparison; NPM renews itself, tracked here."""
    cert_id = st.get("npm_cert_id") if st else None
    if not cert_id:
        p.cert_reasons.append("no certificate of our own yet")
        return
    if live_cert is None:
        p.cert_reasons.append(f"NPM certificate {cert_id} is missing")
        return
    if st.get("policy_hash") != spec.policy_hash():
        p.cert_reasons.append("Domains, issuer or challenge changed")
        return
    expires = _parse_expires(live_cert.get("expires_on"))
    if expires is not None and expires - ctx.clock() < dt.timedelta(days=spec.renew_before_days):
        p.renew_cert = True
        p.renew_not_after = expires


def _iso_expires(value) -> str | None:
    parsed = _parse_expires(value)
    return parsed.isoformat() if parsed else (str(value) if value else None)


def plan_service(ctx: Context, spec: ServiceSpec, live_hosts: list[dict], live_certs: dict[int, dict]) -> ServicePlan:
    p = ServicePlan(spec)
    st = ctx.state.get(spec.resource_id)
    if spec.lifecycle == "absent":
        if st and st.get("npm_host_id"):
            p.blockers.append("lifecycle: absent is not implemented yet (stage G); no change")
        else:
            p.notes.append("lifecycle: absent and no managed resource")
        return p
    p.blockers += _static_blockers(ctx, spec)

    unresolved = [name for name in (spec.public.allow_presets if spec.public else ())
                  if name in ctx.preset_errors]
    for name in unresolved:
        p.blockers.append(f"preset '{name}' unavailable and no saved value "
                          f"({ctx.preset_errors[name]}); fail closed")
    if spec.public is not None:
        for note in ctx.preset_notes:
            if any(note.startswith(f"preset {name}:") for name in spec.public.allow_presets):
                p.notes.append(note)

    owned_id = st.get("npm_host_id") if st else None
    owned = next((h for h in live_hosts if h.get("id") == owned_id), None) if owned_id else None
    if owned_id and owned is None:
        p.drift.append(f"managed NPM host {owned_id} no longer exists")
    p.create_host = owned is None

    owned_elsewhere = {r["npm_host_id"] for r in ctx.state.all()
                       if r.get("npm_host_id") and r["resource_id"] != spec.resource_id}
    if p.create_host and ctx.state.open_op(spec.resource_id, OP_HOST):
        # An earlier run aborted after POST /proxy-hosts. NPM does not allow duplicate domains; a host
        # that is not assigned and carries exactly these domains therefore comes from that attempt.
        p.resume_host_id = next((h["id"] for h in live_hosts if h.get("id") not in owned_elsewhere
                                 and sorted(d.lower() for d in h.get("domain_names") or []) == list(spec.domains)), None)
        if p.resume_host_id:
            p.notes.append(f"resuming an interrupted host creation (NPM host {p.resume_host_id})")

    for h in live_hosts:
        if h.get("id") in (owned_id, p.resume_host_id):
            continue
        clash = host_matches_domains(h, spec.domains)
        if clash:
            p.blockers.append(f"Domain(s) {sorted(clash)} belong to NPM host {h.get('id')}, which is not "
                              f"assigned to {spec.resource_id} (adoption only via 'ingressctl adopt')")
    for other in ctx.state.all():
        if other["resource_id"] == spec.resource_id or other["npm_instance"] != spec.npm_instance:
            continue
        clash = set(json.loads(other["domains"] or "[]")) & set(spec.domains)
        if clash and other.get("npm_host_id"):
            p.blockers.append(f"Domain(s) {sorted(clash)} are already assigned to {other['resource_id']} in the state")

    cert_id = st.get("npm_cert_id") if st else None
    live_cert = live_certs.get(cert_id) if cert_id else None
    if ctx.inv.issuers[spec.issuer_ref].type == "letsencrypt":
        _plan_le_certificate(ctx, spec, st, live_cert, p)
    elif not cert_id:
        p.cert_reasons.append("no certificate of our own yet")
    elif live_cert is None or live_cert.get("info") is None:
        p.cert_reasons.append(f"NPM certificate {cert_id} is missing or unreadable")
    else:
        info = live_cert["info"]
        if st.get("cert_fingerprint") and info.fingerprint != st["cert_fingerprint"]:
            p.cert_reasons.append("NPM certificate differs from the last deployed one")
        if st.get("policy_hash") != spec.policy_hash():
            p.cert_reasons.append("Domains, issuer or lifetime changed")
        if certs.needs_renewal(info, spec.renew_before_days, ctx.clock()):
            p.cert_reasons.append(f"expires on {info.not_after:%Y-%m-%d} (lead time "
                                  f"{certs.renewal_lead(info, spec.renew_before_days).days} days)")

    if owned is not None and not unresolved:
        want = desired_host(spec, cert_id if live_cert else owned.get("certificate_id"), ctx.preset_cidrs)
        for k in MANAGED_HOST_FIELDS:
            if k == "certificate_id" and p.cert_reasons:
                continue  # set with the new certificate anyway
            if _norm(k, owned.get(k)) != _norm(k, want[k]):
                p.host_changes[k] = (owned.get(k), want[k])
        current_advanced = owned.get("advanced_config") or ""
        last_written = st.get("public_config") if st else None
        if spec.public is not None:
            wanted_advanced = want["advanced_config"]
            if current_advanced != wanted_advanced:
                if not is_managed_config(current_advanced):
                    p.blockers.append(
                        f"NPM host {owned.get('id')}: advanced_config contains foreign configuration "
                        f"(no '{MANAGED_MARKER}' marker); adoption only by hand")
                else:
                    p.host_changes["advanced_config"] = (current_advanced, wanted_advanced)
        elif current_advanced:
            remaining = remove_managed_config(current_advanced, last_written)
            if remaining != current_advanced:
                p.host_changes["advanced_config"] = (current_advanced, remaining)
        if st and st.get("manifest") == spec.to_json() and p.host_changes:
            # Only a live config that differs from the last written block is manual work;
            # changed preset networks are a normal update.
            manual_advanced = current_advanced != (last_written or "")
            drifted = [k for k in p.host_changes if k != "advanced_config" or manual_advanced]
            p.drift += [f"changed manually in NPM: {k}" for k in drifted]
            if (set(p.host_changes) == {"advanced_config"} and not manual_advanced
                    and spec.public is not None and spec.public.allow_presets):
                p.notes.append("advanced_config: Preset-Netze aktualisiert")
    return p


# --- Apply ----------------------------------------------------------------------------------------

def _git_is_ancestor(root: pathlib.Path, a: str, b: str) -> bool:
    return subprocess.run(["git", "-C", str(root), "merge-base", "--is-ancestor", a, b],
                          capture_output=True).returncode == 0


def check_commit_order(ctx: Context, spec: ServiceSpec, commit: str | None, allow_rollback: bool) -> str | None:
    """None = continue, otherwise the reason to skip (late/older run)."""
    st = ctx.state.get(spec.resource_id)
    last = st.get("reconciled_commit") if st else None
    if not commit or not last or commit == last or ctx.root is None:
        return None
    if _git_is_ancestor(ctx.root, last, commit):
        return None
    if allow_rollback:
        log.event("apply", "rollback-requested", service=spec.resource_id, from_commit=last, to_commit=commit)
        return None
    if _git_is_ancestor(ctx.root, commit, last):
        return f"Commit {commit[:12]} is older than the already applied {last[:12]} (late event)"
    return f"Commit {commit[:12]} is not a descendant of {last[:12]}; only with --allow-rollback"


def ensure_certificate(ctx: Context, spec: ServiceSpec) -> dict:
    issuer = ctx.inv.issuers[spec.issuer_ref]
    if issuer.type == "letsencrypt":
        return _ensure_le_certificate(ctx, spec)
    inf = ctx.issuers[spec.issuer_ref]
    npm = ctx.npm[spec.npm_instance]
    state = ctx.state

    op = state.open_op(spec.resource_id, OP_ISSUE)
    if op and op["data"].get("policy") != spec.policy_hash():
        state.close_op(op, "abandoned")
        op = None
    if op and state.load_pending_key(op["op_id"]) is None:
        state.close_op(op, "abandoned")  # key lost: the issuance is unusable
        op = None
    if op is None:
        op = state.start_op(spec.resource_id, OP_ISSUE, {"policy": spec.policy_hash(), "domains": list(spec.domains)})
        state.save_pending_key(op["op_id"], certs.generate_key_pem())
        state.step(op, "key_generated")
    else:
        log.event("issue", "resume", service=spec.resource_id, op=op["op_id"], step=op["step"])
    key_pem = state.load_pending_key(op["op_id"])

    bundle = None
    if op["data"].get("infisical_cert_id"):
        bundle = inf.get_certificate(op["data"]["infisical_cert_id"])
    elif op["data"].get("request_id"):
        bundle = inf.get_request(op["data"]["request_id"])
        if bundle is None:
            raise PendingIssuance(op["data"]["request_id"])
    else:
        if op["step"] == "requesting":  # the previous run aborted during the request
            ids = inf.find_by_metadata(issuer.project_id, issuer.application_id, INFISICAL_OP_KEY, op["op_id"])
            if ids:
                bundle = inf.get_certificate(ids[0])
        if bundle is None:
            state.step(op, "requesting")
            try:
                bundle = inf.issue_from_csr(
                    profile_id=issuer.profile_id, application_id=issuer.application_id,
                    csr_pem=certs.make_csr_pem(key_pem, spec.domains), ttl_days=spec.validity_days,
                    metadata={INFISICAL_OP_KEY: op["op_id"], "ingressctl-resource": spec.resource_id})
            except PendingIssuance as p:
                state.step(op, "pending", request_id=p.request_id)
                raise
    state.step(op, "issued", infisical_cert_id=bundle["certificate_id"])

    try:
        info = certs.validate_issued(bundle["certificate"], bundle.get("certificate_chain"), key_pem, spec.domains,
                                     ctx.trust_pem(spec.issuer_ref), ctx.clock())
    except CertificateError:
        state.close_op(op, "invalid-certificate")
        raise
    if info.lifetime < dt.timedelta(days=spec.validity_days) - dt.timedelta(days=1):
        log.event("issue", "shorter-validity", service=spec.resource_id, requested_days=spec.validity_days,
                  actual_days=info.lifetime.days)

    nice_name = f"{CERT_NAME_PREFIX}{spec.resource_id}:{op['op_id']}"
    npm_cert_id = op["data"].get("npm_cert_id")
    if npm_cert_id and npm.get_certificate(npm_cert_id) is None:
        npm_cert_id = None
    if not npm_cert_id:  # maybe created by an aborted run: find it again via the unique marker
        npm_cert_id = next((c["id"] for c in npm.list_certificates() if c.get("nice_name") == nice_name), None)
    if not npm_cert_id:
        state.step(op, "npm_creating")
        npm_cert_id = npm.create_custom_certificate(nice_name)["id"]
    state.step(op, "npm_created", npm_cert_id=npm_cert_id)
    chain = _chain_without_leaf(bundle.get("certificate_chain"), info.fingerprint)
    npm.upload_certificate(npm_cert_id, bundle["certificate"], key_pem, chain)
    state.step(op, "uploaded")
    log.event("issue", "ok", service=spec.resource_id, npm_cert_id=npm_cert_id, not_after=info.not_after)
    return {"op": op, "npm_cert_id": npm_cert_id, "info": info, "infisical_cert_id": bundle["certificate_id"]}


def _le_resume_candidate(ctx: Context, spec: ServiceSpec) -> dict | None:
    """Certificate of an aborted LE POST: NPM overwrites nice_name, so search by domains and usage.

    Only after `npm_requesting`, and never when a state row already claims it or a host uses it.
    """
    npm = ctx.npm[spec.npm_instance]
    claimed = {r.get("npm_cert_id") for r in ctx.state.all() if r.get("npm_cert_id")}
    for cert in npm.list_certificates():
        if cert.get("provider") != "letsencrypt":
            continue
        if sorted(cert.get("domain_names") or []) != list(spec.domains):
            continue
        if cert.get("id") in claimed or npm.certificate_users(cert["id"]) != 0:
            continue
        return cert
    return None


def _ensure_le_certificate(ctx: Context, spec: ServiceSpec) -> dict:
    """NPM-managed Let's Encrypt: one POST issues, no PEM in the response -> info stays None."""
    issuer = ctx.inv.issuers[spec.issuer_ref]
    npm = ctx.npm[spec.npm_instance]
    state = ctx.state
    op = state.open_op(spec.resource_id, OP_ISSUE)
    if op and op["data"].get("policy") != spec.policy_hash():
        state.close_op(op, "abandoned")
        op = None
    if op is None:
        op = state.start_op(spec.resource_id, OP_ISSUE, {"policy": spec.policy_hash(), "domains": list(spec.domains)})
    else:
        log.event("issue", "resume", service=spec.resource_id, op=op["op_id"], step=op["step"])
    cert_id = op["data"].get("npm_cert_id")
    if cert_id and npm.get_certificate(cert_id) is None:
        cert_id = None
    expires_on, le_issuer = op["data"].get("expires_on"), op["data"].get("le_issuer")
    if not cert_id and op["step"] == "npm_requesting":
        candidate = _le_resume_candidate(ctx, spec)
        if candidate is not None:
            cert_id = candidate["id"]
            expires_on, le_issuer = candidate.get("expires_on"), candidate.get("le_issuer")
            log.event("issue", "resume-certificate", service=spec.resource_id, npm_cert_id=cert_id)
    if not cert_id:
        if not spec.dns_challenge:
            results = npm.test_http_challenge(list(spec.domains))
            blocked = {d: results.get(d, "no response") for d in spec.domains if results.get(d) != "ok"}
            if blocked:
                state.close_op(op, "blocked")  # nothing created: the next run starts a fresh operation
                detail = ", ".join(f"{d}: {r}" for d, r in sorted(blocked.items()))
                raise IngressError(f"HTTP-01 check for {', '.join(spec.domains)} failed ({detail}); "
                                   "make port 80 reachable from the internet or set "
                                   "certificate.dns_challenge: true")
        state.step(op, "npm_requesting")
        created = npm.create_letsencrypt_certificate(
            list(spec.domains), dns_challenge=spec.dns_challenge,
            dns_provider=issuer.dns_provider if spec.dns_challenge else None,
            dns_credentials=ctx.le_dns_credentials if spec.dns_challenge else None,
            propagation_seconds=issuer.propagation_seconds if spec.dns_challenge else None)
        cert_id, expires_on, le_issuer = created["id"], created.get("expires_on"), created.get("le_issuer")
    state.step(op, "npm_created", npm_cert_id=cert_id, expires_on=expires_on, le_issuer=le_issuer)
    log.event("issue", "ok", service=spec.resource_id, npm_cert_id=cert_id, not_after=expires_on)
    return {"op": op, "npm_cert_id": cert_id, "info": None, "infisical_cert_id": None}


def _chain_without_leaf(chain_pem: str | None, leaf_fp: str) -> str | None:
    parts = [c for c in certs.split_pem_chain(chain_pem) if certs.cert_info(c).fingerprint != leaf_fp]
    if not parts:
        return None
    from cryptography.hazmat.primitives import serialization
    return "".join(c.public_bytes(serialization.Encoding.PEM).decode() for c in parts)


def apply_service(ctx: Context, spec: ServiceSpec, *, commit: str | None, deployed_commit: str | None = None,
                  allow_rollback: bool = False, skip_upstream_check: bool = False) -> str:
    """Must hold npm_lock(spec.npm_instance). Returns 'noop', 'applied' or 'skipped'."""
    log.set_context(service=spec.resource_id, commit=commit)
    state, npm = ctx.state, ctx.npm[spec.npm_instance]
    if deployed_commit and commit and deployed_commit != commit:
        raise IngressError(f"Komodo deployed {deployed_commit[:12]}, but the manifest is from {commit[:12]}")
    reason = check_commit_order(ctx, spec, commit, allow_rollback)
    if reason:
        log.event("apply", "skipped", reason=reason)
        return "skipped"

    live_hosts = npm.list_proxy_hosts()
    live_certs = {c["id"]: c for c in npm.list_certificates()}
    plan = plan_service(ctx, spec, live_hosts, live_certs)
    if plan.blockers:
        state_row = state.get(spec.resource_id)
        if state_row:
            state.upsert(spec.resource_id, last_result="blocked", last_error="; ".join(plan.blockers)[:1000])
        raise ConflictError("; ".join(plan.blockers))

    st = state.get(spec.resource_id)
    public_config = render_public_config(spec.public, ctx.preset_cidrs) if spec.public is not None else None
    base = dict(namespace=spec.namespace, service_id=spec.service_id, stack_id=spec.stack_id,
                npm_instance=spec.npm_instance, domains=spec.domains, target_commit=commit,
                source_repo=ctx.source_repo)
    if deployed_commit:
        base["deployed_commit"] = deployed_commit
    state.upsert(spec.resource_id, **base)  # domain reservation while holding the lock

    if plan.noop:
        state.upsert(spec.resource_id, reconciled_commit=commit, manifest=spec.to_json(), last_result="noop",
                     last_error=None, public_config=public_config)
        log.event("apply", "noop")
        return "noop"

    if not skip_upstream_check:
        ok, msg = ctx.checker.upstream(spec.upstream_scheme, spec.upstream_host, spec.upstream_port,
                                       spec.health_path, spec.health_statuses)
        if not ok:
            state.upsert(spec.resource_id, last_result="error", last_error=msg)
            raise IngressError(msg)

    old_cert_id = st.get("npm_cert_id") if st else None
    issuer = ctx.inv.issuers[spec.issuer_ref]
    is_le = issuer.type == "letsencrypt"
    new_cert = ensure_certificate(ctx, spec) if plan.cert_reasons else None
    cert_id = new_cert["npm_cert_id"] if new_cert else old_cert_id
    renewed = npm.renew_certificate(cert_id) if (is_le and plan.renew_cert and not plan.cert_reasons) else None
    if is_le:
        # The API returns no PEM: no expected fingerprint; the comparison uses what the check sees.
        expected_fp = None
    else:
        expected_fp = new_cert["info"].fingerprint if new_cert else st.get("cert_fingerprint")
    want = desired_host(spec, cert_id, ctx.preset_cidrs)

    host_existed = not plan.create_host
    if plan.create_host:
        hop = state.open_op(spec.resource_id, OP_HOST) or state.start_op(spec.resource_id, OP_HOST)
        if plan.resume_host_id:
            resume = {k: want[k] for k in MANAGED_HOST_FIELDS}
            if "advanced_config" in want:
                resume["advanced_config"] = want["advanced_config"]
            host = npm.update_proxy_host(plan.resume_host_id, resume)
        else:
            state.step(hop, "creating")
            host = npm.create_proxy_host(want)
        state.upsert(spec.resource_id, npm_host_id=host["id"])
        state.close_op(hop, "done")
        log.event("host", "created", npm_host_id=host["id"])
    else:
        host_id = st["npm_host_id"]
        changes = {k: want[k] for k in plan.host_changes if k != "advanced_config"}
        if "advanced_config" in plan.host_changes:
            changes["advanced_config"] = plan.host_changes["advanced_config"][1]
        if new_cert:
            changes["certificate_id"] = cert_id
            changes["ssl_forced"] = want["ssl_forced"]
        if changes:
            npm.update_proxy_host(host_id, changes)
            log.event("host", "updated", npm_host_id=host_id, fields=sorted(changes))

    npm_cfg = ctx.inv.npm_instances[spec.npm_instance]
    # Let's Encrypt: system trust store, unless the issuer provides its own bundle.
    ca_bundle = issuer.ca_bundle if (issuer.ca_bundle or not is_le) else None
    observed_fp = None
    for attempt in range(1, ctx.verify_attempts + 1):
        failures = []
        for domain in spec.domains:
            res = ctx.checker.https(npm_cfg.tls_address, npm_cfg.tls_port, domain, spec.health_path,
                                    spec.health_statuses, ca_bundle)
            if not res.ok:
                failures.append(res.error)
            elif expected_fp and res.fingerprint != expected_fp:
                failures.append(f"{domain}: NPM does not present the expected certificate")
            else:
                observed_fp = res.fingerprint
        if not failures:
            break
        if attempt < ctx.verify_attempts:
            log.event("verify", "retry", attempt=attempt, error="; ".join(failures))
            time.sleep(ctx.verify_delay)
    if failures:
        msg = "; ".join(failures)
        if new_cert and host_existed and old_cert_id:
            npm.update_proxy_host(st["npm_host_id"], {"certificate_id": old_cert_id, "ssl_forced": want["ssl_forced"]})
            msg += f" -> host switched back to previous certificate {old_cert_id}"
            # The new object stays referenced in the open operation and is reused on the next run.
        state.upsert(spec.resource_id, last_result="error", last_error=msg[:1000])
        log.event("verify", "failed", error=msg)
        raise IngressError(msg)

    fields = dict(manifest=spec.to_json(), reconciled_commit=commit, last_result="ok", last_error=None,
                  public_config=public_config)
    if new_cert:
        if is_le:
            data = new_cert["op"]["data"]
            fields.update(npm_cert_id=cert_id, infisical_cert_id=None, cert_issuer=data.get("le_issuer"),
                          cert_fingerprint=observed_fp, cert_not_after=_iso_expires(data.get("expires_on")),
                          policy_hash=spec.policy_hash())
        else:
            info = new_cert["info"]
            fields.update(npm_cert_id=cert_id, infisical_cert_id=new_cert["infisical_cert_id"],
                          cert_fingerprint=info.fingerprint, cert_sans=info.sans,
                          cert_not_after=info.not_after.isoformat(), cert_issuer=info.issuer,
                          policy_hash=spec.policy_hash())
        state.close_op(new_cert["op"], "done")
    elif renewed is not None:
        fields.update(npm_cert_id=cert_id, infisical_cert_id=None,
                      cert_issuer=renewed.get("le_issuer") or (st.get("cert_issuer") if st else None),
                      cert_fingerprint=observed_fp, cert_not_after=_iso_expires(renewed.get("expires_on")),
                      policy_hash=spec.policy_hash())
    state.upsert(spec.resource_id, **fields)
    if new_cert and new_cert["info"]:
        not_after = new_cert["info"].not_after
    elif new_cert:
        not_after = new_cert["op"]["data"].get("expires_on")
    elif renewed is not None:
        not_after = renewed.get("expires_on")
    else:
        not_after = st.get("cert_not_after") if st else None
    log.event("apply", "ok", not_after=not_after)

    if new_cert and old_cert_id and old_cert_id != cert_id:
        try:
            if npm.certificate_users(old_cert_id) == 0:
                npm.delete_certificate(old_cert_id)
                log.event("cleanup", "ok", npm_cert_id=old_cert_id)
            else:
                log.event("cleanup", "kept", npm_cert_id=old_cert_id, reason="still referenced")
        except AdapterError as e:
            log.event("cleanup", "failed", npm_cert_id=old_cert_id, error=str(e))
    return "applied"


# --- Adoption and recovery ------------------------------------------------------------------------

def adopt(ctx: Context, spec: ServiceSpec, npm_host_id: int) -> None:
    """Explicit adoption of an existing NPM host (only with exactly matching domains)."""
    npm = ctx.npm[spec.npm_instance]
    host = npm.get_proxy_host(npm_host_id)
    if host is None:
        raise IngressError(f"NPM host {npm_host_id} does not exist")
    if sorted(d.lower() for d in host.get("domain_names") or []) != list(spec.domains):
        raise ConflictError(f"Host {npm_host_id} has domains {host.get('domain_names')}, manifest {list(spec.domains)}")
    for r in ctx.state.all():
        if r.get("npm_host_id") == npm_host_id and r["resource_id"] != spec.resource_id:
            raise ConflictError(f"Host {npm_host_id} is already assigned to {r['resource_id']}")
    st = ctx.state.get(spec.resource_id)
    if st and st.get("npm_host_id") and st["npm_host_id"] != npm_host_id:
        raise ConflictError(f"{spec.resource_id} already owns host {st['npm_host_id']}")
    ctx.state.snapshot(spec.resource_id, "adopt", host)
    # The existing certificate is not ours: npm_cert_id stays empty -> the next apply issues our own.
    ctx.state.upsert(spec.resource_id, namespace=spec.namespace, service_id=spec.service_id, stack_id=spec.stack_id,
                     npm_instance=spec.npm_instance, domains=spec.domains, npm_host_id=npm_host_id,
                     last_result="adopted", last_error=None)
    log.event("adopt", "ok", service=spec.resource_id, npm_host_id=npm_host_id)


def recover(ctx: Context, spec: ServiceSpec, npm_host_id: int, npm_cert_id: int | None) -> None:
    """State recovery via an explicit mapping; checks domains and the certificate marker."""
    npm = ctx.npm[spec.npm_instance]
    adopt(ctx, spec, npm_host_id)
    if npm_cert_id is not None:
        cert = npm.get_certificate(npm_cert_id)
        if cert is None or not str(cert.get("nice_name", "")).startswith(f"{CERT_NAME_PREFIX}{spec.resource_id}:"):
            raise ConflictError(f"NPM certificate {npm_cert_id} does not carry the marker of {spec.resource_id}")
        info = cert["info"]
        ctx.state.upsert(spec.resource_id, npm_cert_id=npm_cert_id, cert_fingerprint=info.fingerprint if info else None,
                         cert_sans=info.sans if info else None,
                         cert_not_after=info.not_after.isoformat() if info else None, last_result="recovered")
