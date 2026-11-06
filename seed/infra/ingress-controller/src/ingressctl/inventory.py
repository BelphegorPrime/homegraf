"""Lesen von inventory/*.yaml (gemeinsam mit infra/stackctl gepflegt)."""
from __future__ import annotations

import fnmatch
import pathlib
from dataclasses import dataclass, field

import yaml

from .errors import ConfigError


@dataclass
class NpmInstance:
    name: str
    server: str | None
    api_url: str | None
    tls_address: str | None
    tls_port: int = 443


@dataclass
class Issuer:
    name: str
    type: str
    api_url: str | None
    project_id: str | None
    profile_id: str | None
    application_id: str | None
    max_validity_days: int
    ca_bundle: str | None
    allowed_domains: tuple[str, ...] = ()

    def permits(self, domain: str) -> bool:
        """Glob wie in der Infisical-Policy (`*` = beliebige Zeichen); leere Liste = keine Einschränkung."""
        return not self.allowed_domains or any(fnmatch.fnmatchcase(domain, p) for p in self.allowed_domains)

    def missing(self) -> list[str]:
        return [k for k in ("api_url", "project_id", "profile_id", "application_id", "ca_bundle") if not getattr(self, k)]


@dataclass
class Inventory:
    root: pathlib.Path
    stacks: dict[str, dict]
    servers: dict[str, dict]
    namespaces: dict[str, dict]
    npm_instances: dict[str, NpmInstance]
    issuers: dict[str, Issuer]
    controller: dict = field(default_factory=dict)

    def server_address(self, name: str) -> str | None:
        return (self.servers.get(name) or {}).get("address")

    def namespace_for_path(self, rel_dir: str) -> str | None:
        rel = rel_dir.rstrip("/") + "/"
        for ns, cfg in self.namespaces.items():
            if any(rel.startswith(p) for p in cfg.get("paths", [])):
                return ns
        return None


def _load(path: pathlib.Path) -> dict:
    if not path.exists():
        raise ConfigError(f"{path} fehlt")
    return yaml.safe_load(path.read_text()) or {}


COMPOSE_NAMES = ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml")


def derive_stacks(root: pathlib.Path, namespaces: dict[str, dict], known: dict[str, dict]) -> dict[str, dict]:
    """Stacks that exist only as a folder: stacks/<id>/ with a compose file and an ingress.yaml.

    Nothing is written to inventory/stacks.yaml; an entry there always wins. The server is the upstream server
    of the manifest's first service, the namespace comes from inventory/namespaces.yaml by path. A folder whose
    manifest is unreadable or has no upstream server is skipped here; manifest validation then reports it.
    """
    derived: dict[str, dict] = {}
    taken_paths = {(s.get("path") or "").rstrip("/") for s in known.values()}
    for ns, cfg in namespaces.items():
        for prefix in (cfg or {}).get("paths") or []:
            base = root / prefix
            if not base.is_dir():
                continue
            for manifest in sorted(base.rglob("ingress.yaml")):
                folder = manifest.parent
                rel = folder.relative_to(root).as_posix()
                files = [n for n in COMPOSE_NAMES if (folder / n).is_file()]
                if not files or rel in taken_paths or folder.name in known or folder.name in derived:
                    continue
                try:
                    services = (yaml.safe_load(manifest.read_text()) or {}).get("services") or []
                    server = services[0]["proxy"]["upstream"]["server"]
                except (OSError, yaml.YAMLError, LookupError, TypeError, AttributeError):
                    continue
                derived[folder.name] = {
                    "id": folder.name, "namespace": ns, "path": rel, "compose_files": files,
                    "komodo_stack": folder.name, "server": server, "status": "derived", "secrets": "infisical",
                }
    return derived


def load_inventory(root: pathlib.Path) -> Inventory:
    inv = root / "inventory"
    stacks = {s["id"]: s for s in (_load(inv / "stacks.yaml").get("stacks") or [])}
    servers = _load(inv / "servers.yaml").get("servers") or {}
    namespaces = _load(inv / "namespaces.yaml").get("namespaces") or {}
    stacks.update(derive_stacks(root, namespaces, stacks))
    ing = _load(inv / "ingress.yaml")
    npm = {
        name: NpmInstance(name=name, server=c.get("server"), api_url=c.get("api_url"),
                          tls_address=c.get("tls_address"), tls_port=int(c.get("tls_port", 443)))
        for name, c in (ing.get("npm_instances") or {}).items()
    }
    issuers = {
        name: Issuer(name=name, type=c.get("type", ""), api_url=c.get("api_url"), project_id=c.get("project_id"),
                     profile_id=c.get("profile_id"), application_id=c.get("application_id"),
                     max_validity_days=int(c.get("max_validity_days", 90)), ca_bundle=c.get("ca_bundle"),
                     allowed_domains=tuple(d.lower() for d in c.get("allowed_domains") or ()))
        for name, c in (ing.get("issuers") or {}).items()
    }
    for i in issuers.values():
        if i.type != "infisical":
            raise ConfigError(f"Issuer '{i.name}': nur type 'infisical' wird unterstützt")
    return Inventory(root=root, stacks=stacks, servers=servers, namespaces=namespaces, npm_instances=npm,
                     issuers=issuers, controller=ing.get("controller") or {})
