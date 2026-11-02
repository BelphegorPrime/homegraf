"""Plan und Apply: Abgleich von ServiceSpecs mit NPM und Infisical.

Grundsätze:
- Nur Ressourcen mit eindeutiger Besitzzuordnung im State werden verändert; fremde Hosts mit gleicher
  Domain führen zum Abbruch vor jeder Mutation.
- Nach jedem externen Seiteneffekt wird der Fortschritt in `operations` gesichert. Ein Wiederanlauf gleicht
  zuerst den Live-Zustand ab (Marker im NPM-Zertifikatsnamen, Infisical-Metadatum), statt blind neu anzulegen.
- Neues Zertifikat: ausstellen -> prüfen -> als neues NPM-Objekt hochladen -> Host umschalten -> HTTPS prüfen;
  bei Fehlschlag zurück auf das alte Zertifikat. Alte Objekte werden erst danach und nur ohne Referenzen gelöscht.
- Fehlende Manifeste sind nie ein Löschsignal.
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
    # NPM antwortet auf PUT/POST, bevor nginx den Reload abgeschlossen hat -> Prüfung mehrfach versuchen
    verify_attempts: int = 6
    verify_delay: float = 2.0

    def trust_pem(self, issuer_ref: str) -> str:
        path = self.inv.issuers[issuer_ref].ca_bundle
        return pathlib.Path(path).read_text()


@dataclass
class ServicePlan:
    spec: ServiceSpec
    create_host: bool = False
    host_changes: dict = field(default_factory=dict)   # Feld -> (live, soll)
    cert_reasons: list[str] = field(default_factory=list)
    drift: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    resume_host_id: int | None = None

    @property
    def noop(self) -> bool:
        return not (self.create_host or self.host_changes or self.cert_reasons)

    def render(self) -> str:
        s = self.spec
        lines = [f"{s.resource_id} ({', '.join(s.domains)} -> {s.upstream_scheme}://{s.upstream_host}:{s.upstream_port})"]
        lines += [f"  BLOCKER {b}" for b in self.blockers]
        lines += [f"  DRIFT   {d}" for d in self.drift]
        if self.create_host:
            lines.append("  + Proxy Host anlegen")
        for k, (old, new) in self.host_changes.items():
            lines.append(f"  ~ {k}: {old!r} -> {new!r}")
        for r in self.cert_reasons:
            lines.append(f"  * Zertifikat ausstellen: {r}")
        lines += [f"  i {n}" for n in self.notes]
        if self.noop and not self.blockers:
            lines.append("  = keine Änderung")
        return "\n".join(lines)


def desired_host(spec: ServiceSpec, cert_id: int | None) -> dict:
    return {
        "domain_names": list(spec.domains),
        "forward_scheme": spec.upstream_scheme,
        "forward_host": spec.upstream_host,
        "forward_port": spec.upstream_port,
        "certificate_id": cert_id or 0,
        "ssl_forced": bool(spec.force_https and cert_id),
        "allow_websocket_upgrade": spec.websockets,
        "enabled": True,
    }


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
        b.append(f"NPM-Instanz '{spec.npm_instance}' ist in inventory/ingress.yaml nicht vollständig konfiguriert")
    missing = ctx.inv.issuers[spec.issuer_ref].missing()
    if missing:
        b.append(f"Issuer '{spec.issuer_ref}' unvollständig: {', '.join(missing)}")
    if spec.upstream_host is None:
        b.append(f"Upstream-Server '{spec.upstream_server}' hat keine bestätigte Adresse in inventory/servers.yaml")
    return b


def plan_service(ctx: Context, spec: ServiceSpec, live_hosts: list[dict], live_certs: dict[int, dict]) -> ServicePlan:
    p = ServicePlan(spec)
    st = ctx.state.get(spec.resource_id)
    if spec.lifecycle == "absent":
        if st and st.get("npm_host_id"):
            p.blockers.append("lifecycle: absent ist noch nicht implementiert (Etappe G); keine Änderung")
        else:
            p.notes.append("lifecycle: absent und keine verwaltete Ressource")
        return p
    p.blockers += _static_blockers(ctx, spec)

    owned_id = st.get("npm_host_id") if st else None
    owned = next((h for h in live_hosts if h.get("id") == owned_id), None) if owned_id else None
    if owned_id and owned is None:
        p.drift.append(f"verwalteter NPM-Host {owned_id} existiert nicht mehr")
    p.create_host = owned is None

    owned_elsewhere = {r["npm_host_id"] for r in ctx.state.all()
                       if r.get("npm_host_id") and r["resource_id"] != spec.resource_id}
    if p.create_host and ctx.state.open_op(spec.resource_id, OP_HOST):
        # Ein früherer Lauf brach nach POST /proxy-hosts ab. NPM lässt keine doppelten Domains zu; ein nicht
        # zugeordneter Host mit exakt diesen Domains stammt daher aus diesem Versuch und wird fortgeführt.
        p.resume_host_id = next((h["id"] for h in live_hosts if h.get("id") not in owned_elsewhere
                                 and sorted(d.lower() for d in h.get("domain_names") or []) == list(spec.domains)), None)
        if p.resume_host_id:
            p.notes.append(f"setzt unterbrochenes Anlegen fort (NPM-Host {p.resume_host_id})")

    for h in live_hosts:
        if h.get("id") in (owned_id, p.resume_host_id):
            continue
        clash = host_matches_domains(h, spec.domains)
        if clash:
            p.blockers.append(f"Domain(s) {sorted(clash)} gehören zu NPM-Host {h.get('id')}, der nicht "
                              f"{spec.resource_id} zugeordnet ist (Übernahme nur via 'ingressctl adopt')")
    for other in ctx.state.all():
        if other["resource_id"] == spec.resource_id or other["npm_instance"] != spec.npm_instance:
            continue
        clash = set(json.loads(other["domains"] or "[]")) & set(spec.domains)
        if clash and other.get("npm_host_id"):
            p.blockers.append(f"Domain(s) {sorted(clash)} sind im State an {other['resource_id']} vergeben")

    cert_id = st.get("npm_cert_id") if st else None
    live_cert = live_certs.get(cert_id) if cert_id else None
    if not cert_id:
        p.cert_reasons.append("kein eigenes Zertifikat vorhanden")
    elif live_cert is None or live_cert.get("info") is None:
        p.cert_reasons.append(f"NPM-Zertifikat {cert_id} fehlt oder ist unlesbar")
    else:
        info = live_cert["info"]
        if st.get("cert_fingerprint") and info.fingerprint != st["cert_fingerprint"]:
            p.cert_reasons.append("NPM-Zertifikat weicht vom zuletzt ausgerollten ab")
        if st.get("policy_hash") != spec.policy_hash():
            p.cert_reasons.append("Domains, Issuer oder Laufzeit geändert")
        if certs.needs_renewal(info, spec.renew_before_days, ctx.clock()):
            p.cert_reasons.append(f"läuft am {info.not_after:%Y-%m-%d} ab (Vorlauf "
                                  f"{certs.renewal_lead(info, spec.renew_before_days).days} Tage)")

    if owned is not None:
        want = desired_host(spec, cert_id if live_cert else owned.get("certificate_id"))
        for k in MANAGED_HOST_FIELDS:
            if k == "certificate_id" and p.cert_reasons:
                continue  # wird mit dem neuen Zertifikat ohnehin gesetzt
            if _norm(k, owned.get(k)) != _norm(k, want[k]):
                p.host_changes[k] = (owned.get(k), want[k])
        if st and st.get("manifest") == spec.to_json() and p.host_changes:
            p.drift += [f"in NPM manuell geändert: {k}" for k in p.host_changes]
    return p


# --- Apply ----------------------------------------------------------------------------------------

def _git_is_ancestor(root: pathlib.Path, a: str, b: str) -> bool:
    return subprocess.run(["git", "-C", str(root), "merge-base", "--is-ancestor", a, b],
                          capture_output=True).returncode == 0


def check_commit_order(ctx: Context, spec: ServiceSpec, commit: str | None, allow_rollback: bool) -> str | None:
    """None = fortfahren, sonst Grund zum Überspringen (verspäteter/älterer Lauf)."""
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
        return f"Commit {commit[:12]} ist älter als bereits angewendeter {last[:12]} (verspätetes Ereignis)"
    return f"Commit {commit[:12]} ist kein Nachfolger von {last[:12]}; nur mit --allow-rollback"


def ensure_certificate(ctx: Context, spec: ServiceSpec) -> dict:
    issuer = ctx.inv.issuers[spec.issuer_ref]
    inf = ctx.issuers[spec.issuer_ref]
    npm = ctx.npm[spec.npm_instance]
    state = ctx.state

    op = state.open_op(spec.resource_id, OP_ISSUE)
    if op and op["data"].get("policy") != spec.policy_hash():
        state.close_op(op, "abandoned")
        op = None
    if op and state.load_pending_key(op["op_id"]) is None:
        state.close_op(op, "abandoned")  # Schlüssel verloren: Ausstellung ist unbrauchbar
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
        if op["step"] == "requesting":  # vorheriger Lauf brach während der Anfrage ab
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
    if not npm_cert_id:  # evtl. von einem abgebrochenen Lauf angelegt: über den eindeutigen Marker wiederfinden
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


def _chain_without_leaf(chain_pem: str | None, leaf_fp: str) -> str | None:
    parts = [c for c in certs.split_pem_chain(chain_pem) if certs.cert_info(c).fingerprint != leaf_fp]
    if not parts:
        return None
    from cryptography.hazmat.primitives import serialization
    return "".join(c.public_bytes(serialization.Encoding.PEM).decode() for c in parts)


def apply_service(ctx: Context, spec: ServiceSpec, *, commit: str | None, deployed_commit: str | None = None,
                  allow_rollback: bool = False, skip_upstream_check: bool = False) -> str:
    """Muss unter npm_lock(spec.npm_instance) laufen. Liefert 'noop', 'applied' oder 'skipped'."""
    log.set_context(service=spec.resource_id, commit=commit)
    state, npm = ctx.state, ctx.npm[spec.npm_instance]
    if deployed_commit and commit and deployed_commit != commit:
        raise IngressError(f"Komodo hat {deployed_commit[:12]} ausgerollt, Manifest stammt aus {commit[:12]}")
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
    base = dict(namespace=spec.namespace, service_id=spec.service_id, stack_id=spec.stack_id,
                npm_instance=spec.npm_instance, domains=spec.domains, target_commit=commit,
                source_repo=ctx.source_repo)
    if deployed_commit:
        base["deployed_commit"] = deployed_commit
    state.upsert(spec.resource_id, **base)  # Domain-Reservierung unter der Sperre

    if plan.noop:
        state.upsert(spec.resource_id, reconciled_commit=commit, manifest=spec.to_json(), last_result="noop",
                     last_error=None)
        log.event("apply", "noop")
        return "noop"

    if not skip_upstream_check:
        ok, msg = ctx.checker.upstream(spec.upstream_scheme, spec.upstream_host, spec.upstream_port,
                                       spec.health_path, spec.health_statuses)
        if not ok:
            state.upsert(spec.resource_id, last_result="error", last_error=msg)
            raise IngressError(msg)

    old_cert_id = st.get("npm_cert_id") if st else None
    new_cert = ensure_certificate(ctx, spec) if plan.cert_reasons else None
    cert_id = new_cert["npm_cert_id"] if new_cert else old_cert_id
    expected_fp = new_cert["info"].fingerprint if new_cert else st.get("cert_fingerprint")
    want = desired_host(spec, cert_id)

    host_existed = not plan.create_host
    if plan.create_host:
        hop = state.open_op(spec.resource_id, OP_HOST) or state.start_op(spec.resource_id, OP_HOST)
        if plan.resume_host_id:
            host = npm.update_proxy_host(plan.resume_host_id, {k: want[k] for k in MANAGED_HOST_FIELDS})
        else:
            state.step(hop, "creating")
            host = npm.create_proxy_host(want)
        state.upsert(spec.resource_id, npm_host_id=host["id"])
        state.close_op(hop, "done")
        log.event("host", "created", npm_host_id=host["id"])
    else:
        host_id = st["npm_host_id"]
        changes = {k: want[k] for k in plan.host_changes}
        if new_cert:
            changes["certificate_id"] = cert_id
            changes["ssl_forced"] = want["ssl_forced"]
        npm.update_proxy_host(host_id, changes)
        log.event("host", "updated", npm_host_id=host_id, fields=sorted(changes))

    npm_cfg = ctx.inv.npm_instances[spec.npm_instance]
    for attempt in range(1, ctx.verify_attempts + 1):
        failures = []
        for domain in spec.domains:
            res = ctx.checker.https(npm_cfg.tls_address, npm_cfg.tls_port, domain, spec.health_path,
                                    spec.health_statuses, ctx.inv.issuers[spec.issuer_ref].ca_bundle)
            if not res.ok:
                failures.append(res.error)
            elif expected_fp and res.fingerprint != expected_fp:
                failures.append(f"{domain}: NPM präsentiert nicht das erwartete Zertifikat")
        if not failures:
            break
        if attempt < ctx.verify_attempts:
            log.event("verify", "retry", attempt=attempt, error="; ".join(failures))
            time.sleep(ctx.verify_delay)
    if failures:
        msg = "; ".join(failures)
        if new_cert and host_existed and old_cert_id:
            npm.update_proxy_host(st["npm_host_id"], {"certificate_id": old_cert_id, "ssl_forced": want["ssl_forced"]})
            msg += f" -> Host zurück auf vorheriges Zertifikat {old_cert_id}"
            # Neues Objekt bleibt im State der offenen Operation referenziert und wird beim nächsten Lauf wiederverwendet.
        state.upsert(spec.resource_id, last_result="error", last_error=msg[:1000])
        log.event("verify", "failed", error=msg)
        raise IngressError(msg)

    fields = dict(manifest=spec.to_json(), reconciled_commit=commit, last_result="ok", last_error=None)
    if new_cert:
        info = new_cert["info"]
        fields.update(npm_cert_id=cert_id, infisical_cert_id=new_cert["infisical_cert_id"],
                      cert_fingerprint=info.fingerprint, cert_sans=info.sans, cert_not_after=info.not_after.isoformat(),
                      cert_issuer=info.issuer, policy_hash=spec.policy_hash())
        state.close_op(new_cert["op"], "done")
    state.upsert(spec.resource_id, **fields)
    log.event("apply", "ok", not_after=(new_cert["info"].not_after if new_cert else st.get("cert_not_after")))

    if new_cert and old_cert_id and old_cert_id != cert_id:
        try:
            if npm.certificate_users(old_cert_id) == 0:
                npm.delete_certificate(old_cert_id)
                log.event("cleanup", "ok", npm_cert_id=old_cert_id)
            else:
                log.event("cleanup", "kept", npm_cert_id=old_cert_id, reason="noch referenziert")
        except AdapterError as e:
            log.event("cleanup", "failed", npm_cert_id=old_cert_id, error=str(e))
    return "applied"


# --- Übernahme und Recovery -----------------------------------------------------------------------

def adopt(ctx: Context, spec: ServiceSpec, npm_host_id: int) -> None:
    """Explizite Übernahme eines bestehenden NPM-Hosts (nur bei exakt gleichen Domains)."""
    npm = ctx.npm[spec.npm_instance]
    host = npm.get_proxy_host(npm_host_id)
    if host is None:
        raise IngressError(f"NPM-Host {npm_host_id} existiert nicht")
    if sorted(d.lower() for d in host.get("domain_names") or []) != list(spec.domains):
        raise ConflictError(f"Host {npm_host_id} hat Domains {host.get('domain_names')}, Manifest {list(spec.domains)}")
    for r in ctx.state.all():
        if r.get("npm_host_id") == npm_host_id and r["resource_id"] != spec.resource_id:
            raise ConflictError(f"Host {npm_host_id} ist bereits {r['resource_id']} zugeordnet")
    st = ctx.state.get(spec.resource_id)
    if st and st.get("npm_host_id") and st["npm_host_id"] != npm_host_id:
        raise ConflictError(f"{spec.resource_id} besitzt bereits Host {st['npm_host_id']}")
    ctx.state.snapshot(spec.resource_id, "adopt", host)
    # Das bisherige Zertifikat gehört uns nicht: npm_cert_id bleibt leer -> nächster apply stellt ein eigenes aus.
    ctx.state.upsert(spec.resource_id, namespace=spec.namespace, service_id=spec.service_id, stack_id=spec.stack_id,
                     npm_instance=spec.npm_instance, domains=spec.domains, npm_host_id=npm_host_id,
                     last_result="adopted", last_error=None)
    log.event("adopt", "ok", service=spec.resource_id, npm_host_id=npm_host_id)


def recover(ctx: Context, spec: ServiceSpec, npm_host_id: int, npm_cert_id: int | None) -> None:
    """State-Wiederherstellung über explizites Mapping; prüft Domains und Zertifikatsmarker."""
    npm = ctx.npm[spec.npm_instance]
    adopt(ctx, spec, npm_host_id)
    if npm_cert_id is not None:
        cert = npm.get_certificate(npm_cert_id)
        if cert is None or not str(cert.get("nice_name", "")).startswith(f"{CERT_NAME_PREFIX}{spec.resource_id}:"):
            raise ConflictError(f"NPM-Zertifikat {npm_cert_id} trägt nicht den Marker von {spec.resource_id}")
        info = cert["info"]
        ctx.state.upsert(spec.resource_id, npm_cert_id=npm_cert_id, cert_fingerprint=info.fingerprint if info else None,
                         cert_sans=info.sans if info else None,
                         cert_not_after=info.not_after.isoformat() if info else None, last_result="recovered")
