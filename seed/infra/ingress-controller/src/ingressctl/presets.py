"""IP-Allowlist-Presets: abrufen, streng prüfen und den letzten guten Stand im State halten."""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field

import requests

from . import http, log
from .errors import AdapterError, PresetError
from .state import State

MIN_PREFIX = {4: 8, 6: 16}
MAX_ENTRIES = 1000


@dataclass(frozen=True)
class Preset:
    name: str
    url: str
    key: str


PRESETS: dict[str, Preset] = {
    "github-hooks": Preset("github-hooks", "https://api.github.com/meta", "hooks"),
}


def fetch_preset(session: requests.Session, name: str) -> tuple[str, ...]:
    preset = PRESETS.get(name)
    if preset is None:
        raise PresetError(f"unbekanntes Preset '{name}'")
    try:
        resp = http.request(session, "github", "GET", preset.url, mutating=False,
                            headers={"Accept": "application/vnd.github+json", "User-Agent": "ingressctl"},
                            allow_redirects=False)
    except AdapterError as exc:
        raise PresetError(str(exc)) from None
    if resp.status_code != 200:
        raise PresetError(f"preset {name}: unerwarteter HTTP-Status {resp.status_code}")
    try:
        body = resp.json()
    except ValueError:
        raise PresetError(f"preset {name}: Antwort ist kein JSON") from None
    if not isinstance(body, dict):
        raise PresetError(f"preset {name}: Antwort ist kein JSON-Objekt")
    if preset.key not in body:
        raise PresetError(f"preset {name}: Schlüssel '{preset.key}' fehlt")
    entries = body[preset.key]
    if not isinstance(entries, list):
        raise PresetError(f"preset {name}: '{preset.key}' ist keine Liste")
    if not entries:
        raise PresetError(f"preset {name}: '{preset.key}' ist leer")
    if len(entries) > MAX_ENTRIES:
        raise PresetError(f"preset {name}: mehr als {MAX_ENTRIES} Einträge")
    networks = set()
    for entry in entries:
        if not isinstance(entry, str):
            raise PresetError(f"preset {name}: Eintrag {entry!r} ist kein String")
        try:
            network = ipaddress.ip_network(entry, strict=True)
        except ValueError:
            raise PresetError(f"preset {name}: '{entry}' ist kein IPv4-/IPv6-Netz") from None
        if not network.is_global:
            raise PresetError(f"preset {name}: '{entry}' ist kein globales Netz")
        if network.prefixlen < MIN_PREFIX[network.version]:
            raise PresetError(f"preset {name}: '{entry}' ist zu weit gefasst")
        networks.add(str(network))
    return tuple(sorted(networks))


@dataclass
class PresetResolution:
    cidrs: dict[str, tuple[str, ...]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)


def resolve_presets(state: State, names, session: requests.Session, *, persist: bool) -> PresetResolution:
    res = PresetResolution()
    for name in sorted(set(names)):
        try:
            fetched = fetch_preset(session, name)
        except PresetError as exc:
            stored = state.get_preset(name)
            if stored is None:
                res.errors[name] = str(exc)
                continue
            res.cidrs[name] = tuple(stored["cidrs"])
            res.notes.append(f"preset {name}: Abruf fehlgeschlagen ({exc}); "
                             f"nutze letzten guten Stand vom {stored['fetched_at']}")
            log.event("preset", "stale", preset=name, error=str(exc))
            continue
        stored = state.get_preset(name)
        previous = tuple(stored["cidrs"]) if stored is not None else ()
        if stored is not None and previous != fetched:
            added = sorted(set(fetched) - set(previous))
            removed = sorted(set(previous) - set(fetched))
            res.notes.append(f"preset {name}: Liste geändert (+{','.join(added)}, -{','.join(removed)})")
        if persist and (stored is None or previous != fetched):
            state.save_preset(name, fetched, PRESETS[name].url)
        res.cidrs[name] = fetched
    return res
