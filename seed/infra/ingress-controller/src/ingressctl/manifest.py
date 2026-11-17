"""Loading, schema validation, normalization and global conflict checks for ingress.yaml manifests."""
from __future__ import annotations

import hashlib
import json
import pathlib
import re
from dataclasses import asdict, dataclass
from importlib import resources

import jsonschema
import yaml

from .errors import ManifestError
from .inventory import Inventory
from .public import PublicSpec, parse_public

MANIFEST_NAME = "ingress.yaml"
EXTERNAL_DIR = "external"      # external/ingress.yaml: services outside the repository
EXTERNAL_STACK = "external"
_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")


@dataclass(frozen=True)
class ServiceSpec:
    resource_id: str          # <namespace>/<service-id>, stable ownership identifier
    namespace: str
    service_id: str
    stack_id: str
    manifest_path: str
    lifecycle: str
    npm_instance: str
    domains: tuple[str, ...]  # normalized, sorted
    upstream_scheme: str
    upstream_host: str | None  # resolved; None = server without a confirmed address
    upstream_server: str | None
    upstream_port: int
    websockets: bool
    force_https: bool
    issuer_ref: str
    validity_days: int
    renew_before_days: int
    health_path: str
    health_statuses: tuple[int, ...]
    public: PublicSpec | None = None
    dns_challenge: bool = False

    def policy_hash(self) -> str:
        """Changes only for values that require a reissue (not for the upstream)."""
        data = {"domains": list(self.domains), "issuer": self.issuer_ref, "validity_days": self.validity_days}
        if self.dns_challenge:
            data["dns_challenge"] = True
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16]

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "ServiceSpec":
        d = json.loads(text)
        d.setdefault("public", None)
        d.setdefault("dns_challenge", False)
        d["domains"] = tuple(d["domains"])
        d["health_statuses"] = tuple(d["health_statuses"])
        if d["public"] is not None:
            p = d["public"]
            d["public"] = PublicSpec(paths=tuple(p["paths"]), methods=tuple(p["methods"]),
                                     allow_cidrs=tuple(p["allow_cidrs"]),
                                     allow_presets=tuple(p.get("allow_presets", ())))
        return cls(**d)


def normalize_domain(raw: str) -> str:
    d = raw.strip().rstrip(".").lower()
    if "*" in d:
        raise ManifestError(f"wildcard domain '{raw}' is not supported in the initial scope")
    try:
        d = d.encode("idna").decode("ascii")
    except UnicodeError as e:
        raise ManifestError(f"invalid hostname '{raw}': {e}") from None
    labels = d.split(".")
    if len(d) > 253 or len(labels) < 2 or not all(_LABEL.match(lbl) for lbl in labels):
        raise ManifestError(f"invalid hostname '{raw}'")
    if labels[-1].isdigit():
        raise ManifestError(f"'{raw}' is not a domain (IP addresses are not allowed)")
    return d


def _schema() -> dict:
    return json.loads(resources.files("ingressctl").joinpath("schema/ingress.v1.schema.json").read_text())


def find_manifests(root: pathlib.Path, inv: Inventory) -> list[pathlib.Path]:
    found = []
    for cfg in inv.namespaces.values():
        for p in cfg.get("paths", []):
            base = root / p
            if base.is_dir():
                found.extend(sorted(base.rglob(MANIFEST_NAME)))
    return sorted(set(found))


def parse_manifest(root: pathlib.Path, path: pathlib.Path, inv: Inventory) -> list[ServiceSpec]:
    rel = path.relative_to(root).as_posix()
    try:
        doc = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        raise ManifestError(f"{rel}: invalid YAML: {str(e).splitlines()[0]}") from None
    errors = sorted(jsonschema.Draft202012Validator(_schema()).iter_errors(doc), key=lambda e: list(e.absolute_path))
    if errors:
        msgs = [f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}" for e in errors[:10]]
        raise ManifestError(f"{rel}: schema error: " + "; ".join(msgs))

    rel_dir = path.parent.relative_to(root).as_posix()
    ns = doc["namespace"]
    expected_ns = inv.namespace_for_path(rel_dir)
    if ns != expected_ns:
        raise ManifestError(f"{rel}: namespace '{ns}' does not match the directory (expected '{expected_ns}')")

    specs, ids = [], set()
    for svc in doc["services"]:
        rid = f"{ns}/{svc['id']}"
        if svc["id"] in ids:
            raise ManifestError(f"{rel}: service ID '{svc['id']}' is duplicated")
        ids.add(svc["id"])
        if rel_dir == EXTERNAL_DIR:
            # services without a stack in the repo (VMs, appliances, NAS apps): ingress only, no deployment
            if svc["stack"] != EXTERNAL_STACK:
                raise ManifestError(f"{rel}: {rid}: in {EXTERNAL_DIR}/ stack '{EXTERNAL_STACK}' is required")
        else:
            stack = inv.stacks.get(svc["stack"])
            if stack is None:
                raise ManifestError(f"{rel}: {rid}: stack '{svc['stack']}' is missing from the inventory")
            if stack["path"].rstrip("/") != rel_dir:
                raise ManifestError(f"{rel}: {rid}: stack '{svc['stack']}' belongs to '{stack['path']}', not '{rel_dir}'")
            if stack["namespace"] != ns:
                raise ManifestError(f"{rel}: {rid}: stack namespace '{stack['namespace']}' != '{ns}'")

        proxy, up, cert, hc = svc["proxy"], svc["proxy"]["upstream"], svc["certificate"], svc["healthcheck"]
        try:
            public = parse_public(proxy["public"]) if "public" in proxy else None
        except ManifestError as e:
            raise ManifestError(f"{rel}: {rid}: {e}") from None
        domains = [normalize_domain(d) for d in proxy["domains"]]
        if len(set(domains)) != len(domains):
            raise ManifestError(f"{rel}: {rid}: duplicate domains after normalization")

        npm_instance = svc.get("npm_instance") or inv.controller.get("default_npm_instance", "main")
        if npm_instance not in inv.npm_instances:
            raise ManifestError(f"{rel}: {rid}: unknown NPM instance '{npm_instance}'")
        issuer = inv.issuers.get(cert["issuer_ref"])
        if issuer is None:
            raise ManifestError(f"{rel}: {rid}: issuer_ref '{cert['issuer_ref']}' is not allowed in inventory/ingress.yaml")
        is_le = issuer.type == "letsencrypt"
        if public is not None and not is_le:
            raise ManifestError(f"{rel}: {rid}: proxy.public requires a Let's Encrypt issuer "
                                f"(issuer '{issuer.name}')")
        if "dns_challenge" in cert and not is_le:
            raise ManifestError(f"{rel}: {rid}: certificate.dns_challenge is only allowed with a "
                                f"Let's Encrypt issuer (issuer '{issuer.name}')")
        outside = [d for d in domains if not issuer.permits(d)]
        if outside:
            raise ManifestError(f"{rel}: {rid}: {outside} are outside {list(issuer.allowed_domains)} "
                                f"(issuer '{issuer.name}')")
        if is_le:
            if "validity_days" in cert:
                raise ManifestError(f"{rel}: {rid}: validity_days is not allowed with Let's Encrypt; "
                                    "Let's Encrypt fixes the lifetime (90 days)")
            validity = issuer.max_validity_days
        else:
            validity = cert.get("validity_days", 90)
        renew_before = cert.get("renew_before_days", 30)
        if validity > issuer.max_validity_days:
            raise ManifestError(f"{rel}: {rid}: validity_days {validity} > CA policy {issuer.max_validity_days}")
        if renew_before >= validity:
            raise ManifestError(f"{rel}: {rid}: renew_before_days must be smaller than validity_days")
        dns_challenge = bool(cert.get("dns_challenge", issuer.dns_challenge)) if is_le else False
        if dns_challenge and not issuer.dns_provider:
            raise ManifestError(f"{rel}: {rid}: dns_challenge requires dns_provider on issuer '{issuer.name}'")
        if is_le and public is not None and public.allow_cidrs and not dns_challenge:
            raise ManifestError(f"{rel}: {rid}: proxy.public.allow_cidrs is incompatible with HTTP-01 from "
                                "Let's Encrypt (the validation would be rejected with 403); set dns_challenge: true")

        server = up.get("server")
        if server is not None and server not in inv.servers:
            raise ManifestError(f"{rel}: {rid}: upstream server '{server}' is missing from inventory/servers.yaml")
        host = up.get("host") or (inv.server_address(server) if server else None)

        specs.append(ServiceSpec(
            resource_id=rid, namespace=ns, service_id=svc["id"], stack_id=svc["stack"], manifest_path=rel,
            lifecycle=svc["lifecycle"], npm_instance=npm_instance, domains=tuple(sorted(domains)),
            upstream_scheme=up["scheme"], upstream_host=host.lower() if host else None, upstream_server=server,
            upstream_port=up["port"], websockets=proxy.get("websockets", False),
            force_https=proxy.get("force_https", True), issuer_ref=cert["issuer_ref"], validity_days=validity,
            renew_before_days=renew_before, health_path=hc["path"], health_statuses=tuple(sorted(hc["accepted_statuses"])),
            public=public, dns_challenge=dns_challenge,
        ))
    return specs


def load_all(root: pathlib.Path, inv: Inventory) -> list[ServiceSpec]:
    """Load all manifests in the repo and validate them globally (domains unique per NPM instance)."""
    specs, errors = [], []
    for path in find_manifests(root, inv):
        try:
            specs.extend(parse_manifest(root, path, inv))
        except ManifestError as e:
            errors.append(str(e))
    owners: dict[tuple[str, str], str] = {}
    seen_ids: dict[str, str] = {}
    for s in specs:
        if s.resource_id in seen_ids:
            errors.append(f"{s.resource_id}: defined twice in {seen_ids[s.resource_id]} and {s.manifest_path}")
        seen_ids[s.resource_id] = s.manifest_path
        if s.lifecycle != "present":
            continue
        for d in s.domains:
            key = (s.npm_instance, d)
            if key in owners and owners[key] != s.resource_id:
                errors.append(f"domain '{d}' is claimed by {owners[key]} and {s.resource_id}")
            owners.setdefault(key, s.resource_id)
    if errors:
        raise ManifestError("\n".join(errors))
    return specs
