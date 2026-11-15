"""Reine Funktionen für den `public:`-Block: Pfade, Methoden und Quellnetze validieren und als nginx-Direktiven rendern."""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass

from .errors import ManifestError

ACME_CHALLENGE = "/.well-known/acme-challenge/"
MANAGED_MARKER = "# managed by ingressctl"
_COMMENT = f"{MANAGED_MARKER}: public paths only"
_FIELDS = frozenset({"paths", "methods", "allow_cidrs"})
_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})
_DEFAULT_METHODS = ("GET", "HEAD")
_PATH_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._~-/")


@dataclass(frozen=True)
class PublicSpec:
    paths: tuple[str, ...]        # sortiert, eindeutig
    methods: tuple[str, ...]      # sortiert, Großbuchstaben
    allow_cidrs: tuple[str, ...]  # sortiert, kanonisch


def parse_public(raw: dict) -> PublicSpec:
    if not isinstance(raw, dict):
        raise ManifestError("proxy.public muss ein Objekt sein")
    for key in sorted(raw, key=str):
        if key not in _FIELDS:
            if key == "preset":
                raise ManifestError("proxy.public.preset wird noch nicht unterstützt")
            raise ManifestError(f"proxy.public: unbekanntes Feld '{key}'")

    if "paths" not in raw:
        raise _error("paths", "fehlt")
    return PublicSpec(
        paths=_parse_paths(raw["paths"]),
        methods=_parse_methods(raw["methods"]) if "methods" in raw else _DEFAULT_METHODS,
        allow_cidrs=_parse_cidrs(raw["allow_cidrs"]) if "allow_cidrs" in raw else (),
    )


def render_public_config(spec: PublicSpec) -> str:
    alternatives = [_path_regex(path) for path in sorted(spec.paths)]
    alternatives.append(_ACME_REGEX)
    uri_regex = "^(" + "|".join(alternatives) + ")$"
    method_regex = "^(" + "|".join(sorted(spec.methods)) + ")$"
    acme_regex = "^" + _ACME_REGEX + "$"
    lines = [
        _COMMENT,
        f'if ($uri !~ "{uri_regex}") {{ return 404; }}',
        # Das ACME-Pfadpräfix bleibt von der Methodenprüfung ausgenommen.
        f'if ($uri !~ "{acme_regex}") {{ if ($request_method !~ "{method_regex}") {{ return 405; }} }}',
    ]
    if spec.allow_cidrs:
        lines.extend(f"allow {cidr};" for cidr in sorted(spec.allow_cidrs))
        lines.append("deny all;")
    return "\n".join(lines)


def is_managed_config(text: str | None) -> bool:
    """Leer oder eine von ingressctl geschriebene advanced_config."""
    return not (text or "").strip() or text.lstrip().startswith(MANAGED_MARKER)


def remove_managed_config(current: str | None, written: str | None) -> str:
    """Entfernt genau den zuletzt geschriebenen Block; von Hand Ergänztes bleibt stehen."""
    current = current or ""
    if not written:
        return current
    if current.strip() == written.strip():
        return ""
    if current.startswith(written):
        return current[len(written):].lstrip("\r\n")
    return current


def _error(field: str, message: str) -> ManifestError:
    return ManifestError(f"proxy.public.{field}: {message}")


def _parse_paths(value) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise _error("paths", "muss eine Liste sein")
    if not value:
        raise _error("paths", "muss mindestens einen Pfad enthalten")
    return tuple(sorted({_parse_path(item) for item in value}))


def _parse_path(value) -> str:
    if not isinstance(value, str):
        raise _error("paths", f"{value!r} ist kein String")
    if not value:
        raise _error("paths", "leerer Pfad")
    if len(value) > 200:
        raise _error("paths", "Pfad ist länger als 200 Zeichen")
    if not value.startswith("/"):
        raise _error("paths", f"'{value}' beginnt nicht mit '/'")
    if value == "/":
        raise _error("paths", "'/' würde alle Pfade freigeben")
    if "//" in value:
        raise _error("paths", f"'{value}' enthält '//'")
    if value.endswith("/*"):
        body = value[:-1]
        if body == "/":
            raise _error("paths", "'/*' würde alle Pfade freigeben")
    else:
        body = value
    if "*" in body:
        raise _error("paths", f"'{value}': '*' ist nur am Ende als '/*' erlaubt")
    invalid = sorted({ch for ch in body if ch not in _PATH_CHARS})
    if invalid:
        raise _error("paths", f"'{value}' enthält unerlaubte Zeichen: {', '.join(map(repr, invalid))}")
    if any(segment in (".", "..") for segment in body.split("/")):
        raise _error("paths", f"'{value}' enthält '.' oder '..' als Segment")
    return value


def _parse_methods(value) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise _error("methods", "muss eine Liste sein")
    if not value:
        raise _error("methods", "darf nicht leer sein")
    methods = set()
    for item in value:
        if not isinstance(item, str):
            raise _error("methods", f"{item!r} ist kein String")
        method = item.upper()
        if method not in _METHODS:
            raise _error("methods", f"'{item}' ist nicht erlaubt")
        methods.add(method)
    return tuple(sorted(methods))


def _parse_cidrs(value) -> tuple[str, ...]:
    if isinstance(value, dict) and "preset" in value:
        raise _error("allow_cidrs", "preset wird noch nicht unterstützt")
    if not isinstance(value, (list, tuple)):
        raise _error("allow_cidrs", "muss eine Liste sein")
    cidrs = set()
    for item in value:
        if not isinstance(item, str):
            raise _error("allow_cidrs", f"{item!r} ist kein String")
        try:
            network = ipaddress.ip_network(item, strict=True)
        except ValueError as exc:
            raise _error("allow_cidrs", f"'{item}' ist kein IPv4-/IPv6-Netz: {exc}") from None
        cidrs.add(str(network))
    return tuple(sorted(cidrs))


_ACME_REGEX = ACME_CHALLENGE.replace(".", r"\.") + ".*"


def _path_regex(path: str) -> str:
    if path.endswith("/*"):
        return path[:-1].replace(".", r"\.") + ".*"
    return path.replace(".", r"\.")
