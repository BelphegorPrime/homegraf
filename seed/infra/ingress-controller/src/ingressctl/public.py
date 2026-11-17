"""Pure functions for the `public:` block: validate paths, methods and source networks and render them as nginx directives."""
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

# Must match the keys of presets.PRESETS (checked in slice (c)).
KNOWN_PRESETS = frozenset({"github-hooks"})


@dataclass(frozen=True)
class PublicSpec:
    paths: tuple[str, ...]        # sorted, unique
    methods: tuple[str, ...]      # sorted, uppercase
    allow_cidrs: tuple[str, ...]  # sorted, canonical
    allow_presets: tuple[str, ...] = ()  # sorted, unique


def parse_public(raw: dict) -> PublicSpec:
    if not isinstance(raw, dict):
        raise ManifestError("proxy.public must be an object")
    for key in sorted(raw, key=str):
        if key not in _FIELDS:
            if key == "preset":
                raise ManifestError("proxy.public.preset is not supported yet")
            raise ManifestError(f"proxy.public: unknown field '{key}'")

    if "paths" not in raw:
        raise _error("paths", "is missing")
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
    # nginx rejects an `if` inside an `if` ("if directive is not allowed here"), so
    # the method sets a variable that the ACME path rewrites beforehand.
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
    """Networks of the presets, re-checked: nothing fetched may enter the nginx config unvalidated."""
    networks = set()
    for name in names:
        if name not in preset_cidrs:
            raise _error("allow_cidrs", f"preset '{name}' is not resolved")
        for cidr in preset_cidrs[name]:
            if not isinstance(cidr, str):
                raise _error("allow_cidrs", f"preset '{name}': {cidr!r} is not an IPv4/IPv6 network")
            try:
                networks.add(str(ipaddress.ip_network(cidr, strict=True)))
            except ValueError as exc:
                raise _error("allow_cidrs", f"preset '{name}': '{cidr}' is not an IPv4/IPv6 network: {exc}") from None
    return networks


def is_managed_config(text: str | None) -> bool:
    """Empty or an advanced_config written by ingressctl."""
    return not (text or "").strip() or text.lstrip().startswith(MANAGED_MARKER)


def remove_managed_config(current: str | None, written: str | None) -> str:
    """Removes exactly the last written block; manually added content remains."""
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
        raise _error("paths", "must be a list")
    if not value:
        raise _error("paths", "must contain at least one path")
    return tuple(sorted({_parse_path(item) for item in value}))


def _parse_path(value) -> str:
    if not isinstance(value, str):
        raise _error("paths", f"{value!r} is not a string")
    if not value:
        raise _error("paths", "empty path")
    if len(value) > 200:
        raise _error("paths", "path is longer than 200 characters")
    if not value.startswith("/"):
        raise _error("paths", f"'{value}' does not start with '/'")
    if value == "/":
        raise _error("paths", "'/' would expose all paths")
    if "//" in value:
        raise _error("paths", f"'{value}' contains '//'")
    if value.endswith("/*"):
        body = value[:-1]
        if body == "/":
            raise _error("paths", "'/*' would expose all paths")
    else:
        body = value
    if "*" in body:
        raise _error("paths", f"'{value}': '*' is only allowed at the end as '/*'")
    invalid = sorted({ch for ch in body if ch not in _PATH_CHARS})
    if invalid:
        raise _error("paths", f"'{value}' contains invalid characters: {', '.join(map(repr, invalid))}")
    if any(segment in (".", "..") for segment in body.split("/")):
        raise _error("paths", f"'{value}' contains '.' or '..' as a segment")
    return value


def _parse_methods(value) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise _error("methods", "must be a list")
    if not value:
        raise _error("methods", "must not be empty")
    methods = set()
    for item in value:
        if not isinstance(item, str):
            raise _error("methods", f"{item!r} is not a string")
        method = item.upper()
        if method not in _METHODS:
            raise _error("methods", f"'{item}' is not allowed")
        methods.add(method)
    return tuple(sorted(methods))


def _parse_cidrs(value) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Static networks and preset names; a single `{preset: <name>}` stands for a list with one entry."""
    items = [value] if isinstance(value, dict) else value
    if not isinstance(items, (list, tuple)):
        raise _error("allow_cidrs", "must be a list")
    cidrs, presets = set(), set()
    for item in items:
        if isinstance(item, dict):
            presets.add(_parse_preset(item))
            continue
        if not isinstance(item, str):
            raise _error("allow_cidrs", f"{item!r} is not a string")
        try:
            network = ipaddress.ip_network(item, strict=True)
        except ValueError as exc:
            raise _error("allow_cidrs", f"'{item}' is not an IPv4/IPv6 network: {exc}") from None
        cidrs.add(str(network))
    return tuple(sorted(cidrs)), tuple(sorted(presets))


def _parse_preset(item: dict) -> str:
    if set(item) != {"preset"}:
        raise _error("allow_cidrs", "preset entry must have exactly the field 'preset'")
    name = item["preset"]
    if not isinstance(name, str):
        raise _error("allow_cidrs", f"{name!r} is not a preset name")
    if name not in KNOWN_PRESETS:
        known = ", ".join(sorted(KNOWN_PRESETS))
        raise _error("allow_cidrs", f"unknown preset '{name}' (known: {known})")
    return name


_ACME_REGEX = ACME_CHALLENGE.replace(".", r"\.") + ".*"


def _path_regex(path: str) -> str:
    if path.endswith("/*"):
        return path[:-1].replace(".", r"\.") + ".*"
    return path.replace(".", r"\.")
