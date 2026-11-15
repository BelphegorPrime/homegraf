"""Reine Funktionen für den `public:`-Block: Pfade, Methoden und Quellnetze validieren und als nginx-Direktiven rendern."""
from __future__ import annotations

import ipaddress
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .errors import ManifestError

ACME_CHALLENGE = "/.well-known/acme-challenge/"
MANAGED_MARKER = "# managed by ingressctl"
_ACME_METHOD = "-acme-"
_COMMENT = f"{MANAGED_MARKER}: public paths only"
_FIELDS = frozenset({"paths", "methods", "allow_cidrs"})
_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})
_DEFAULT_METHODS = ("GET", "HEAD")
_PATH_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._~-/")

# Muss den Schlüsseln von presets.PRESETS entsprechen (geprüft in slice (c)).
KNOWN_PRESETS = frozenset({"github-hooks"})


@dataclass(frozen=True)
class PublicSpec:
    paths: tuple[str, ...]        # sortiert, eindeutig
    methods: tuple[str, ...]      # sortiert, Großbuchstaben
    allow_cidrs: tuple[str, ...]  # sortiert, kanonisch
    allow_presets: tuple[str, ...] = ()  # sortiert, eindeutig


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
    cidrs, presets = _parse_cidrs(raw["allow_cidrs"]) if "allow_cidrs" in raw else ((), ())
    return PublicSpec(
        paths=_parse_paths(raw["paths"]),
        methods=_parse_methods(raw["methods"]) if "methods" in raw else _DEFAULT_METHODS,
        allow_cidrs=cidrs,
        allow_presets=presets,
    )


def render_public_config(spec: PublicSpec, preset_cidrs: Mapping[str, Sequence[str]] | None = None) -> str:
    alternatives = [_path_regex(path) for path in sorted(spec.paths)]
    alternatives.append(_ACME_REGEX)
    uri_regex = "^(" + "|".join(alternatives) + ")$"
    method_regex = "^(" + "|".join(sorted(spec.methods) + [_ACME_METHOD]) + ")$"
    acme_regex = "^" + _ACME_REGEX + "$"
    # nginx lehnt ein `if` in einem `if` ab ("if directive is not allowed here"), daher
    # setzt die Methode eine Variable, die der ACME-Pfad vorher umschreibt.
    lines = [
        _COMMENT,
        f'if ($uri !~ "{uri_regex}") {{ return 404; }}',
        "set $ingressctl_method $request_method;",
        f'if ($uri ~ "{acme_regex}") {{ set $ingressctl_method "{_ACME_METHOD}"; }}',
        f'if ($ingressctl_method !~ "{method_regex}") {{ return 405; }}',
    ]
    if spec.allow_cidrs or spec.allow_presets:
        networks = set(spec.allow_cidrs) | _resolved_preset_cidrs(spec.allow_presets, preset_cidrs or {})
        lines.extend(f"allow {cidr};" for cidr in sorted(networks))
        lines.append("deny all;")
    return "\n".join(lines)


def _resolved_preset_cidrs(names: tuple[str, ...], preset_cidrs: Mapping[str, Sequence[str]]) -> set[str]:
    """Netze der Presets, erneut geprüft: nichts Ausgelöstes darf ungeprüft in die nginx-Konfiguration."""
    networks = set()
    for name in names:
        if name not in preset_cidrs:
            raise _error("allow_cidrs", f"preset '{name}' ist nicht aufgelöst")
        for cidr in preset_cidrs[name]:
            if not isinstance(cidr, str):
                raise _error("allow_cidrs", f"Preset '{name}': {cidr!r} ist kein IPv4-/IPv6-Netz")
            try:
                networks.add(str(ipaddress.ip_network(cidr, strict=True)))
            except ValueError as exc:
                raise _error("allow_cidrs", f"Preset '{name}': '{cidr}' ist kein IPv4-/IPv6-Netz: {exc}") from None
    return networks


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


def _parse_cidrs(value) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Statische Netze und Preset-Namen; ein einzelnes `{preset: <name>}` steht für eine Liste mit einem Eintrag."""
    items = [value] if isinstance(value, dict) else value
    if not isinstance(items, (list, tuple)):
        raise _error("allow_cidrs", "muss eine Liste sein")
    cidrs, presets = set(), set()
    for item in items:
        if isinstance(item, dict):
            presets.add(_parse_preset(item))
            continue
        if not isinstance(item, str):
            raise _error("allow_cidrs", f"{item!r} ist kein String")
        try:
            network = ipaddress.ip_network(item, strict=True)
        except ValueError as exc:
            raise _error("allow_cidrs", f"'{item}' ist kein IPv4-/IPv6-Netz: {exc}") from None
        cidrs.add(str(network))
    return tuple(sorted(cidrs)), tuple(sorted(presets))


def _parse_preset(item: dict) -> str:
    if set(item) != {"preset"}:
        raise _error("allow_cidrs", "Preset-Eintrag muss genau das Feld 'preset' haben")
    name = item["preset"]
    if not isinstance(name, str):
        raise _error("allow_cidrs", f"{name!r} ist kein Preset-Name")
    if name not in KNOWN_PRESETS:
        known = ", ".join(sorted(KNOWN_PRESETS))
        raise _error("allow_cidrs", f"unbekanntes Preset '{name}' (bekannt: {known})")
    return name


_ACME_REGEX = ACME_CHALLENGE.replace(".", r"\.") + ".*"


def _path_regex(path: str) -> str:
    if path.endswith("/*"):
        return path[:-1].replace(".", r"\.") + ".*"
    return path.replace(".", r"\.")
