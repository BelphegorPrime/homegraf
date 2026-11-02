#!/usr/bin/env python3
"""Reads the network from a local UniFi OS controller (UDM SE) with a read-only account and prints a snapshot as JSON.

  UNIFI_URL=https://10.0.1.1 UNIFI_USER=... UNIFI_PASSWORD=... python3 infra/graph/unifi.py [--root .]

Read only: it logs in, reads devices, networks, WLANs and clients, and logs out. Of the clients it keeps counts per
device and, for the machines the repository knows by address, where they are plugged in (switch and port, or access
point). No client names, no MAC addresses of other clients, no WLAN passwords. The controller's certificate is
self-signed: it is not verified unless UNIFI_VERIFY=true, and UNIFI_FINGERPRINT=<sha256 hex> pins it instead.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import http.cookiejar
import json
import os
import pathlib
import socket
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

import yaml

SITE = "default"


class UnifiError(Exception):
    pass


def known_addresses(root: pathlib.Path) -> set[str]:
    """Addresses of the machines of the repository: Komodo servers and the hand-kept machines of network.yaml."""
    out: set[str] = set()
    servers = root / "inventory/servers.yaml"
    if servers.exists():
        out |= {c["address"] for c in (yaml.safe_load(servers.read_text()) or {}).get("servers", {}).values() if c and c.get("address")}
    network = root / "inventory/network.yaml"
    if network.exists():
        out |= {n["address"] for n in (yaml.safe_load(network.read_text()) or {}).get("nodes", []) if n.get("address")}
    return out


class Controller:
    """A session on the controller: login, GET under /proxy/network/api/s/<site>/, logout."""

    def __init__(self, base: str, user: str, password: str, verify: bool = False, fingerprint: str = "", timeout: int = 20):
        self.base, self.user, self.password, self.timeout = base.rstrip("/"), user, password, timeout
        ctx = ssl.create_default_context() if verify else ssl._create_unverified_context()  # noqa: S323 - self-signed, see the module doc
        self.fingerprint = fingerprint.lower().replace(":", "")
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
                                                  urllib.request.HTTPSHandler(context=ctx))

    def _pin(self) -> None:
        if not self.fingerprint:
            return
        host = urllib.parse.urlparse(self.base)
        pem = ssl.get_server_certificate((host.hostname, host.port or 443), timeout=self.timeout)
        seen = hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()
        if seen != self.fingerprint:
            raise UnifiError(f"the certificate of {self.base} is not the pinned one (sha256 {seen[:12]}…)")

    def _open(self, req: urllib.request.Request):
        try:
            return self.opener.open(req, timeout=self.timeout)
        except urllib.error.HTTPError as e:
            raise UnifiError(f"{req.get_method()} {urllib.parse.urlparse(req.full_url).path}: HTTP {e.code}") from None
        except (urllib.error.URLError, OSError, socket.timeout) as e:
            raise UnifiError(f"{self.base} not reachable: {getattr(e, 'reason', e)}") from None

    def login(self) -> None:
        self._pin()
        body = json.dumps({"username": self.user, "password": self.password, "remember": False}).encode()
        self._open(urllib.request.Request(f"{self.base}/api/auth/login", data=body, headers={"Content-Type": "application/json"}))

    def get(self, path: str) -> list[dict]:
        with self._open(urllib.request.Request(f"{self.base}/proxy/network/api/s/{SITE}/{path}")) as r:
            return json.load(r).get("data", [])

    def logout(self) -> None:
        try:
            self._open(urllib.request.Request(f"{self.base}/api/auth/logout", data=b"", method="POST"))
        except UnifiError:
            pass


def snapshot_from(devices: list[dict], networks: list[dict], wlans: list[dict], clients: list[dict], known: set[str]) -> dict:
    """The part of the controller's answers the map uses (see the module doc for what is left out)."""
    names = {d["mac"]: d.get("name") or d["mac"] for d in devices}
    net_names = {n.get("_id"): n.get("name") for n in networks}
    out_devices = []
    for d in devices:
        up = d.get("uplink") or {}
        ports = {p.get("port_idx"): {"name": p.get("name"), "speed": p.get("speed")} for p in d.get("port_table") or [] if p.get("up")}
        out_devices.append({
            "mac": d["mac"], "name": names[d["mac"]], "model": d.get("model"), "type": d.get("type"), "ip": d.get("ip"),
            "state": d.get("state"), "version": d.get("version"),
            "uplink": {"mac": up.get("uplink_mac"), "remotePort": up.get("uplink_remote_port"), "port": up.get("port_idx"), "type": up.get("type"),
                       "speed": up.get("speed")} if up else None,
            "ports": ports,
        })
    per_device: dict[str, dict[str, int]] = {}
    for c in clients:
        mac = c.get("sw_mac") if c.get("is_wired") else c.get("ap_mac")
        if mac:
            per_device.setdefault(mac, {"wired": 0, "wireless": 0})["wired" if c.get("is_wired") else "wireless"] += 1
    seen = [{"ip": c["ip"], "wired": bool(c.get("is_wired")), "switch": c.get("sw_mac"), "port": c.get("sw_port"), "ap": c.get("ap_mac"),
             "essid": c.get("essid"), "network": c.get("network") or net_names.get(c.get("network_id")), "vlan": c.get("vlan")}
            for c in clients if c.get("ip") in known]
    return {
        "fetched": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "devices": out_devices,
        "networks": [{"name": n.get("name"), "purpose": n.get("purpose"), "vlan": n.get("vlan") if n.get("vlan_enabled") else None,
                      "subnet": n.get("ip_subnet"), "dhcp": n.get("dhcpd_enabled"), "enabled": n.get("enabled")} for n in networks],
        "wlans": [{"name": w.get("name"), "enabled": w.get("enabled"), "security": w.get("security"),
                   "network": net_names.get(w.get("networkconf_id"))} for w in wlans],
        "clients": {"total": len(clients), "wired": sum(1 for c in clients if c.get("is_wired")), "perDevice": per_device},
        "known": seen,
    }


def fetch(controller: Controller, known: set[str]) -> dict:
    controller.login()
    try:
        return snapshot_from(controller.get("stat/device"), controller.get("rest/networkconf"), controller.get("rest/wlanconf"),
                             controller.get("stat/sta"), known)
    finally:
        controller.logout()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", type=pathlib.Path, default=pathlib.Path(__file__).resolve().parents[2], help="repository, for the machines' addresses")
    args = ap.parse_args(argv)
    try:
        url, user, password = (os.environ[k] for k in ("UNIFI_URL", "UNIFI_USER", "UNIFI_PASSWORD"))
    except KeyError as e:
        print(f"error: {e.args[0]} is not set", file=sys.stderr)
        return 2
    controller = Controller(url, user, password, os.environ.get("UNIFI_VERIFY", "").lower() == "true", os.environ.get("UNIFI_FINGERPRINT", ""))
    try:
        snapshot = fetch(controller, known_addresses(args.root))
    except UnifiError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    json.dump(snapshot, sys.stdout, indent=1, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
