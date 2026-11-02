"""Laden, Schema-Prüfung, Normalisierung und globale Konfliktprüfung der ingress.yaml-Manifeste."""
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

MANIFEST_NAME = "ingress.yaml"
EXTERNAL_DIR = "external"      # external/ingress.yaml: Dienste außerhalb des Repos
EXTERNAL_STACK = "external"
_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")


@dataclass(frozen=True)
class ServiceSpec:
    resource_id: str          # <namespace>/<service-id>, stabile Besitzkennung
    namespace: str
    service_id: str
    stack_id: str
    manifest_path: str
    lifecycle: str
    npm_instance: str
    domains: tuple[str, ...]  # normalisiert, sortiert
    upstream_scheme: str
    upstream_host: str | None  # aufgelöst; None = Server ohne bestätigte Adresse
    upstream_server: str | None
    upstream_port: int
    websockets: bool
    force_https: bool
    issuer_ref: str
    validity_days: int
    renew_before_days: int
    health_path: str
    health_statuses: tuple[int, ...]

    def policy_hash(self) -> str:
        """Ändert sich nur bei Werten, die eine Neuausstellung erfordern (nicht beim Upstream)."""
        data = {"domains": list(self.domains), "issuer": self.issuer_ref, "validity_days": self.validity_days}
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16]

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "ServiceSpec":
        d = json.loads(text)
        d["domains"] = tuple(d["domains"])
        d["health_statuses"] = tuple(d["health_statuses"])
        return cls(**d)


def normalize_domain(raw: str) -> str:
    d = raw.strip().rstrip(".").lower()
    if "*" in d:
        raise ManifestError(f"Wildcard-Domain '{raw}' wird im ersten Umfang nicht unterstützt")
    try:
        d = d.encode("idna").decode("ascii")
    except UnicodeError as e:
        raise ManifestError(f"ungültiger Hostname '{raw}': {e}") from None
    labels = d.split(".")
    if len(d) > 253 or len(labels) < 2 or not all(_LABEL.match(lbl) for lbl in labels):
        raise ManifestError(f"ungültiger Hostname '{raw}'")
    if labels[-1].isdigit():
        raise ManifestError(f"'{raw}' ist keine Domain (IP-Adressen sind nicht zulässig)")
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
        raise ManifestError(f"{rel}: kein gültiges YAML: {str(e).splitlines()[0]}") from None
    errors = sorted(jsonschema.Draft202012Validator(_schema()).iter_errors(doc), key=lambda e: list(e.absolute_path))
    if errors:
        msgs = [f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}" for e in errors[:10]]
        raise ManifestError(f"{rel}: Schemafehler: " + "; ".join(msgs))

    rel_dir = path.parent.relative_to(root).as_posix()
    ns = doc["namespace"]
    expected_ns = inv.namespace_for_path(rel_dir)
    if ns != expected_ns:
        raise ManifestError(f"{rel}: namespace '{ns}' passt nicht zum Verzeichnis (erwartet '{expected_ns}')")

    specs, ids = [], set()
    for svc in doc["services"]:
        rid = f"{ns}/{svc['id']}"
        if svc["id"] in ids:
            raise ManifestError(f"{rel}: Service-ID '{svc['id']}' doppelt")
        ids.add(svc["id"])
        if rel_dir == EXTERNAL_DIR:
            # Dienste ohne Stack im Repo (VMs, Appliances, NAS apps): nur Ingress, kein Deployment
            if svc["stack"] != EXTERNAL_STACK:
                raise ManifestError(f"{rel}: {rid}: in {EXTERNAL_DIR}/ muss stack '{EXTERNAL_STACK}' sein")
        else:
            stack = inv.stacks.get(svc["stack"])
            if stack is None:
                raise ManifestError(f"{rel}: {rid}: Stack '{svc['stack']}' fehlt im Inventar")
            if stack["path"].rstrip("/") != rel_dir:
                raise ManifestError(f"{rel}: {rid}: Stack '{svc['stack']}' gehört zu '{stack['path']}', nicht zu '{rel_dir}'")
            if stack["namespace"] != ns:
                raise ManifestError(f"{rel}: {rid}: Stack-Namespace '{stack['namespace']}' != '{ns}'")

        proxy, up, cert, hc = svc["proxy"], svc["proxy"]["upstream"], svc["certificate"], svc["healthcheck"]
        domains = [normalize_domain(d) for d in proxy["domains"]]
        if len(set(domains)) != len(domains):
            raise ManifestError(f"{rel}: {rid}: doppelte Domains nach Normalisierung")

        npm_instance = svc.get("npm_instance") or inv.controller.get("default_npm_instance", "main")
        if npm_instance not in inv.npm_instances:
            raise ManifestError(f"{rel}: {rid}: unbekannte NPM-Instanz '{npm_instance}'")
        issuer = inv.issuers.get(cert["issuer_ref"])
        if issuer is None:
            raise ManifestError(f"{rel}: {rid}: issuer_ref '{cert['issuer_ref']}' ist nicht in inventory/ingress.yaml freigegeben")
        outside = [d for d in domains if not issuer.permits(d)]
        if outside:
            raise ManifestError(f"{rel}: {rid}: {outside} liegen außerhalb von {list(issuer.allowed_domains)} "
                                f"(Issuer '{issuer.name}')")
        validity = cert.get("validity_days", 90)
        renew_before = cert.get("renew_before_days", 30)
        if validity > issuer.max_validity_days:
            raise ManifestError(f"{rel}: {rid}: validity_days {validity} > CA-Policy {issuer.max_validity_days}")
        if renew_before >= validity:
            raise ManifestError(f"{rel}: {rid}: renew_before_days muss kleiner als validity_days sein")

        server = up.get("server")
        if server is not None and server not in inv.servers:
            raise ManifestError(f"{rel}: {rid}: Upstream-Server '{server}' fehlt in inventory/servers.yaml")
        host = up.get("host") or (inv.server_address(server) if server else None)

        specs.append(ServiceSpec(
            resource_id=rid, namespace=ns, service_id=svc["id"], stack_id=svc["stack"], manifest_path=rel,
            lifecycle=svc["lifecycle"], npm_instance=npm_instance, domains=tuple(sorted(domains)),
            upstream_scheme=up["scheme"], upstream_host=host.lower() if host else None, upstream_server=server,
            upstream_port=up["port"], websockets=proxy.get("websockets", False),
            force_https=proxy.get("force_https", True), issuer_ref=cert["issuer_ref"], validity_days=validity,
            renew_before_days=renew_before, health_path=hc["path"], health_statuses=tuple(sorted(hc["accepted_statuses"])),
        ))
    return specs


def load_all(root: pathlib.Path, inv: Inventory) -> list[ServiceSpec]:
    """Alle Manifeste im Repo laden und global prüfen (Domains eindeutig pro NPM-Instanz)."""
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
            errors.append(f"{s.resource_id}: doppelt definiert in {seen_ids[s.resource_id]} und {s.manifest_path}")
        seen_ids[s.resource_id] = s.manifest_path
        if s.lifecycle != "present":
            continue
        for d in s.domains:
            key = (s.npm_instance, d)
            if key in owners and owners[key] != s.resource_id:
                errors.append(f"Domain '{d}' wird von {owners[key]} und {s.resource_id} beansprucht")
            owners.setdefault(key, s.resource_id)
    if errors:
        raise ManifestError("\n".join(errors))
    return specs
