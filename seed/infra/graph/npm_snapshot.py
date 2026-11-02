#!/usr/bin/env python3
"""Snapshot the Nginx Proxy Manager hosts that the repo does not manage into inventory/npm-hosts.yaml.

ingressctl owns the *.lab.example hosts (stacks/*/ingress.yaml); the rest (the *.example.org hosts) were made
by hand in NPM. The map reads this file so those domains show up too. Read-only against NPM; the file holds domains,
upstreams and flags, never certificates, access lists or custom nginx config.

    npm_snapshot.py [--root DIR]      rewrite inventory/npm-hosts.yaml
    npm_snapshot.py --check           exit 1 if the file differs from NPM (needs NPM access)

Credentials: NPM_API_URL, NPM_IDENTITY, NPM_SECRET from the environment, else from infra/secrets/npm.env.
Needs ingressctl (pip install -e infra/ingress-controller) for the NPM client.
"""
from __future__ import annotations

import argparse
import datetime
import os
import pathlib
import re
import sys

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import build  # noqa: E402

HEADER = """# Snapshot of the NPM proxy hosts that ingressctl does not manage (made by hand in NPM).
# Written by infra/graph/npm_snapshot.py on {date}; rerun it after changing hosts in NPM. Read by infra/graph/build.py.
"""


def select_hosts(raw: list[dict], managed: set[str]) -> list[dict]:
    """Hosts with at least one domain the repo does not manage, reduced to what the map needs."""
    out = []
    for h in sorted(raw, key=lambda h: h["id"]):
        domains = sorted(d for d in h.get("domain_names") or [] if d not in managed)
        if not domains:
            continue
        out.append({
            "id": h["id"],
            "domains": domains,
            "upstream": {"scheme": h["forward_scheme"], "host": h["forward_host"], "port": h["forward_port"]},
            "enabled": bool(h.get("enabled", True)),
            "force_https": bool(h.get("ssl_forced")),
            "websockets": bool(h.get("allow_websocket_upgrade")),
        })
    return out


def render(hosts: list[dict], date: str) -> str:
    body = yaml.safe_dump({"hosts": hosts}, sort_keys=False, default_flow_style=None, width=140)
    return HEADER.format(date=date) + body


def _env_from_file(path: pathlib.Path) -> dict[str, str]:
    out = {}
    for line in path.read_text().splitlines():
        m = re.match(r"\s*(?:export\s+)?([A-Z_]+)=(.*)", line)
        if m:
            out[m.group(1)] = m.group(2).strip().strip("'\"")
    return out


def fetch(root: pathlib.Path) -> list[dict]:
    try:
        from ingressctl.npm import NpmClient
    except ImportError:
        raise SystemExit("ingressctl is not installed: pip install -e infra/ingress-controller")
    cfg = {k: os.environ[k] for k in ("NPM_API_URL", "NPM_IDENTITY", "NPM_SECRET") if os.environ.get(k)}
    envfile = root / "infra" / "secrets" / "npm.env"
    if len(cfg) < 3 and envfile.exists():
        cfg = {**_env_from_file(envfile), **cfg}
    missing = [k for k in ("NPM_API_URL", "NPM_IDENTITY", "NPM_SECRET") if not cfg.get(k)]
    if missing:
        raise SystemExit(f"missing {', '.join(missing)} (environment or infra/secrets/npm.env)")
    client = NpmClient(cfg["NPM_API_URL"], cfg["NPM_IDENTITY"], cfg["NPM_SECRET"])
    client.login()
    return client.list_proxy_hosts()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", type=pathlib.Path, default=pathlib.Path(__file__).resolve().parents[2])
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)
    hosts = select_hosts(fetch(args.root), build.managed_domains(args.root))
    target = args.root / "inventory" / "npm-hosts.yaml"
    current = (yaml.safe_load(target.read_text()) or {}).get("hosts") if target.exists() else None
    if args.check:
        print("npm-hosts.yaml is current" if current == hosts else "npm-hosts.yaml differs from NPM")
        return 0 if current == hosts else 1
    if current == hosts:
        print(f"npm-hosts.yaml already current ({len(hosts)} hosts)")
        return 0
    target.write_text(render(hosts, datetime.date.today().isoformat()))
    print(f"wrote {target.relative_to(args.root)}: {len(hosts)} hosts")
    return 0


if __name__ == "__main__":
    sys.exit(main())
