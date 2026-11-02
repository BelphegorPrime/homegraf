#!/usr/bin/env python3
"""Build the homelab graph (machines, stacks, containers, domains, platform services) from the repo.

Reads inventory/*.yaml, stacks/*/compose*.y*ml, stacks/*/ingress.yaml and external/ingress.yaml; the hand-kept
layer (ISP, router, switches, manual links) comes from inventory/network.yaml. Needs PyYAML only and no credentials.

    build.py [--root DIR] [-o graph.json]   write the graph (stdout without -o)
    build.py --check                        build, verify every edge points at a node, exit 1 otherwise

Output: {"generated", "nodes": [{id, type, label, ...}], "edges": [{id, source, target, type, ...}], "warnings"}.
Node ids are "<type>:<name>" (machine:nas, stack:paperless, container:paperless/webserver, domain:x.lab.example);
the hand-kept nodes of network.yaml keep the ids they are given.
Edges run along the request or ownership direction: machine -hosts-> stack -contains-> container.
"""
from __future__ import annotations

import argparse
import datetime
import ipaddress
import json
import pathlib
import re
import sys

import yaml

# The platform services around the stacks (proxy, deployer, forge, secrets, certificate authority, ...) are what this installation
# says they are: inventory/homegraf.yaml `platform:` (id -> label, the stack that runs it, kind). Without them the map just has none;
# the ids npm, komodo, infisical and ca get the usual edges (domain -> proxy, stack -> deployer, stack -> secrets) when present.


def _platform(root: pathlib.Path) -> dict[str, tuple[str, str, str]]:
    path = root / "inventory" / "homegraf.yaml"
    cfg = (yaml.safe_load(path.read_text()) or {}) if path.is_file() else {}
    return {k: ((v or {}).get("label") or k, (v or {}).get("stack") or "", (v or {}).get("kind") or "service")
            for k, v in (cfg.get("platform") or {}).items()}


EDGE_TYPES = (
    "link", "hosts", "contains", "proxies", "terminated-by", "depends-on", "deployed-by", "uses-secrets",
    "runs", "certificates-from",
)


def _read(path: pathlib.Path):
    return yaml.safe_load(path.read_text()) or {}


def ingress_files(root: pathlib.Path) -> list[pathlib.Path]:
    return sorted((root / "stacks").glob("*/ingress.yaml")) + [root / "external" / "ingress.yaml"]


def managed_domains(root: pathlib.Path) -> set[str]:
    """Domains the repo's ingress manifests manage (everything else in NPM was set up by hand)."""
    return {d for f in ingress_files(root) if f.exists()
            for svc in _read(f).get("services") or [] for d in (svc.get("proxy") or {}).get("domains") or []}


def _ports(spec) -> list[dict]:
    """Published ports of a compose service: [{host, container}] (long and short syntax, IPs and protocols ignored)."""
    out = []
    for p in spec.get("ports") or []:
        if isinstance(p, dict):
            if p.get("published") and p.get("target"):
                out.append({"host": int(p["published"]), "container": int(p["target"])})
            continue
        parts = re.sub(r"/\w+$", "", str(p)).split(":")
        if len(parts) >= 2 and parts[-1].isdigit() and parts[-2].isdigit():
            out.append({"host": int(parts[-2]), "container": int(parts[-1])})
    return out


def _labels(spec) -> dict[str, str]:
    raw = spec.get("labels") or {}
    if isinstance(raw, list):
        raw = dict(str(item).split("=", 1) for item in raw if "=" in str(item))
    return {str(k): str(v) for k, v in raw.items()}


def _environment(spec) -> dict[str, str]:
    raw = spec.get("environment") or {}
    if isinstance(raw, list):
        raw = dict((str(item).split("=", 1) + [""])[:2] for item in raw)
    return {str(k): "" if v is None else str(v) for k, v in raw.items()}


def _depends_on(spec) -> list[str]:
    raw = spec.get("depends_on") or []
    return list(raw) if isinstance(raw, (list, dict)) else []


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _network(cidr) -> str | None:
    """10.0.1.1/24 (the gateway's address) -> 10.0.1.0/24."""
    try:
        return str(ipaddress.ip_network(cidr, strict=False))
    except (ValueError, TypeError):
        return cidr or None


def _speed(mbit) -> str:
    return {100: "100M", 1000: "1G", 2500: "2.5G", 5000: "5G", 10000: "10G"}.get(mbit, f"{mbit}M" if mbit else "")


class Builder:
    def __init__(self, root: pathlib.Path, unifi: dict | None = None):
        self.unifi = unifi
        self.root = root
        self.nodes: dict[str, dict] = {}
        self.edges: dict[tuple, dict] = {}
        self.warnings: list[str] = []

    def node(self, node_id: str, type_: str, label: str, **attrs) -> dict:
        n = self.nodes.setdefault(node_id, {"id": node_id, "type": type_, "label": label})
        n.update({k: v for k, v in attrs.items() if v not in (None, [], {})})
        return n

    def edge(self, source: str, target: str, type_: str, **attrs) -> None:
        key = (source, target, type_)
        e = self.edges.setdefault(key, {"id": f"{type_}:{source}>{target}", "source": source, "target": target, "type": type_})
        e.update({k: v for k, v in attrs.items() if v not in (None, [], {})})

    # -- sources ---------------------------------------------------------------------------------------------

    def load(self) -> None:
        inv = self.root / "inventory"
        self.servers = _read(inv / "servers.yaml").get("servers") or {}
        self.stacks = [s for s in _read(inv / "stacks.yaml").get("stacks") or [] if s.get("status") != "inactive"]
        core = _read(inv / "core-services.yaml") if (inv / "core-services.yaml").exists() else {}
        self.file_server = {t["file"]: t["server"] for t in core.get("targets") or []}
        self.network = _read(inv / "network.yaml") if (inv / "network.yaml").exists() else {}
        self.platform_services = _platform(self.root)

    def machines(self) -> None:
        for name, cfg in self.servers.items():
            if (cfg or {}).get("status") == "inactive":
                continue
            self.node(f"machine:{name}", "machine", name, address=(cfg or {}).get("address"), komodo=True)

    def stack_server(self, s: dict) -> str | None:
        if s.get("server"):
            return s["server"]
        for f in s.get("compose_files") or []:
            if f"{s['path']}/{f}" in self.file_server:
                return self.file_server[f"{s['path']}/{f}"]
        return None

    def stack_nodes(self) -> None:
        self.containers: list[dict] = []   # {id, stack, server, ports, env}
        for s in self.stacks:
            sid, server = s["id"], self.stack_server(s)
            self.node(f"stack:{sid}", "stack", s.get("komodo_stack") or sid, server=server, path=s["path"],
                      status=s.get("status"), secrets=s.get("secrets"), job=s.get("job"))
            if server and f"machine:{server}" in self.nodes:
                self.edge(f"machine:{server}", f"stack:{sid}", "hosts")
            if s.get("status") == "verified" and "komodo" in self.platform_services:
                self.edge(f"stack:{sid}", "infra:komodo", "deployed-by")
            if s.get("secrets") == "infisical" and "infisical" in self.platform_services:
                self.edge(f"stack:{sid}", "infra:infisical", "uses-secrets")
            images = []
            for f in s.get("compose_files") or []:
                path = self.root / s["path"] / f
                try:
                    services = (yaml.safe_load(path.read_text()) or {}).get("services") or {}
                except (OSError, yaml.YAMLError) as e:
                    self.warnings.append(f"{s['path']}/{f}: {str(e).splitlines()[0]}")
                    continue
                for svc, spec in services.items():
                    spec = spec or {}
                    cid = f"container:{sid}/{svc}"
                    ports = _ports(spec)
                    labels = _labels(spec)
                    images.append(spec.get("image"))
                    self.node(cid, "container", spec.get("container_name") or svc, stack=sid, server=server,
                              image=spec.get("image"), ports=ports, service=svc)
                    self.edge(f"stack:{sid}", cid, "contains")
                    for dep in _depends_on(spec):
                        self.edge(cid, f"container:{sid}/{dep}", "depends-on")
                    if labels.get("homepage.group"):
                        self.nodes[f"stack:{sid}"].setdefault("category", labels["homepage.group"])
                    self.containers.append({"id": cid, "stack": sid, "server": server, "ports": ports,
                                            "env": _environment(spec)})
            self.nodes[f"stack:{sid}"]["images"] = [i for i in images if i]

    def platform(self) -> None:
        for key, (label, stack, kind) in self.platform_services.items():
            self.node(f"infra:{key}", "infra", label, kind=kind)
            if f"stack:{stack}" in self.nodes:
                self.edge(f"infra:{key}", f"stack:{stack}", "runs")
        if {"npm", "ca"} <= set(self.platform_services):
            self.edge("infra:npm", "infra:ca", "certificates-from")

    def ingress(self) -> None:
        files = ingress_files(self.root)
        # Any machine with an address counts, not only Komodo servers: the Proxmox host and the LXCs are in network.yaml.
        addr_server = {n["address"]: n["id"].split(":", 1)[1] for n in self.nodes.values()
                       if n["type"] == "machine" and n.get("address")}
        services = [(svc, False) for path in files if path.exists() for svc in _read(path).get("services") or []]
        services += [(svc, True) for svc in self._manual_hosts()]
        for svc, manual in services:
            proxy = svc.get("proxy") or {}
            up = proxy.get("upstream") or {}
            server = up.get("server") or addr_server.get(up.get("host"))
            port = up.get("port")
            domains = proxy.get("domains") or []
            enabled = svc.get("enabled", True)
            # A disabled host answers nothing, so it gets its domain node but no route.
            target = self._upstream_target(svc, server, up, port) if enabled else None
            for d in domains:
                self.node(f"domain:{d}", "domain", d, url=f"https://{d}", service=svc["id"],
                          scheme=up.get("scheme"), health=(svc.get("healthcheck") or {}).get("path"),
                          manual=manual or None, disabled=None if enabled else True)
                if "npm" in self.platform_services:
                    self.edge(f"domain:{d}", "infra:npm", "terminated-by")
            if target and "npm" in self.platform_services:
                known = self.edges.get(("infra:npm", target, "proxies"), {}).get("domains", [])
                self.edge("infra:npm", target, "proxies", domains=sorted({*known, *domains}), port=port)
                stack = self.nodes[target].get("stack") or (target.split(":", 1)[1] if target.startswith("stack:") else None)
                owner = self.nodes.get(f"stack:{stack}") if stack else self.nodes[target]
                for d in domains:
                    self.nodes[f"domain:{d}"]["target"] = target
                    owner["urls"] = sorted({*owner.get("urls", []), f"https://{d}"})
                    if manual:
                        owner["manualUrls"] = sorted({*owner.get("manualUrls", []), f"https://{d}"})

    def _manual_hosts(self) -> list[dict]:
        """inventory/npm-hosts.yaml (infra/graph/npm_snapshot.py): NPM hosts made by hand, shaped like manifest services."""
        path = self.root / "inventory" / "npm-hosts.yaml"
        if not path.exists():
            return []
        out = []
        for h in _read(path).get("hosts") or []:
            first = h["domains"][0].split(".")[0]
            out.append({"id": f"npm-{first}", "label": first, "stack": None, "enabled": h.get("enabled", True),
                        "proxy": {"domains": h["domains"], "upstream": h["upstream"]}})
        return out

    def _upstream_target(self, svc: dict, server: str | None, up: dict, port) -> str | None:
        for c in self.containers:
            if c["server"] == server and any(p["host"] == port for p in c["ports"]):
                return c["id"]
        stack = svc.get("stack")
        if stack and f"stack:{stack}" in self.nodes:
            return f"stack:{stack}"
        # Appliance or VM without a stack in this repo: a service node on its machine.
        node_id = f"service:{svc['id']}"
        self.node(node_id, "service", svc.get("label", svc["id"]), server=server, host=up.get("host"), port=port)
        if server and f"machine:{server}" in self.nodes:
            self.edge(f"machine:{server}", node_id, "hosts")
        elif up.get("host"):
            ext = f"machine:{up['host']}"
            self.node(ext, "machine", up["host"], address=up["host"], external=True)
            self.edge(ext, node_id, "hosts")
        return node_id

    def inferred_dependencies(self) -> None:
        """Stack -> stack when an environment value names another stack's domain or <server address>:<host port>."""
        domains = {n["label"]: n["target"] for n in self.nodes.values() if n["type"] == "domain" and n.get("target")}
        endpoints: dict[str, list[str]] = {}
        for c in self.containers:
            addr = (self.servers.get(c["server"]) or {}).get("address") if c["server"] else None
            for p in c["ports"]:
                for host in filter(None, (addr, c["server"])):
                    endpoints.setdefault(f"{host}:{p['host']}", []).append(c["stack"])
        for c in self.containers:
            for key, value in c["env"].items():
                found = {self.nodes[t].get("stack") or t.split(":", 1)[1] for d, t in domains.items() if d in value}
                found |= {s for ep, stacks in endpoints.items() if re.search(re.escape(ep) + r"(?!\d)", value) for s in stacks}
                for other in found - {c["stack"]}:
                    if f"stack:{other}" in self.nodes:
                        e = self.edges.setdefault((f"stack:{c['stack']}", f"stack:{other}", "depends-on"), {
                            "id": f"depends-on:stack:{c['stack']}>stack:{other}", "source": f"stack:{c['stack']}",
                            "target": f"stack:{other}", "type": "depends-on", "inferred": True, "via": []})
                        if key not in e.get("via", []):
                            e.setdefault("via", []).append(key)

    def network_nodes(self) -> None:
        """Nodes of inventory/network.yaml: ISP, router, switches, and machines that are not Komodo servers (Proxmox, LXCs)."""
        for n in self.network.get("nodes") or []:
            extra = {k: v for k, v in n.items() if k not in ("id", "type", "label")}
            self.node(n["id"], n.get("type", "device"), n.get("label", n["id"]), **extra)

    def unifi_layer(self) -> dict[str, str]:
        """Replaces the placeholders (ISP, router, switch) by what the UniFi controller reports: the internet connections,
        the gateway, switches and access points with the port each hangs on, and the machines with the port they are
        plugged into. Returns the machines' node ids by the device they hang on (for the uplinks that are not hand-kept)."""
        snap = self.unifi or {}
        gone = {i for i, n in self.nodes.items() if n.get("placeholder") and n["type"] in ("isp", "router", "switch")}
        for i in gone:
            del self.nodes[i]
        self.removed = gone
        devices = snap.get("devices") or []
        by_mac = {d["mac"]: d for d in devices}
        ids: dict[str, str] = {}
        networks = {n["name"]: n for n in snap.get("networks") or [] if n.get("name")}
        vlans = " · ".join(n["name"] + (" (VLAN %s)" % n["vlan"] if n.get("vlan") else "") + " " + _network(n["subnet"])
                           for n in networks.values() if n.get("subnet") and n.get("purpose") in ("corporate", "guest"))
        wlans = ", ".join(w["name"] + (f" ({w['network']})" if w.get("network") else "") for w in snap.get("wlans") or [] if w.get("enabled"))
        per = (snap.get("clients") or {}).get("perDevice") or {}
        for d in devices:
            kind = {"udm": "router", "ugw": "router", "usw": "switch", "uap": "device"}.get(d.get("type"), "device")
            ids[d["mac"]] = f"{'ap' if d.get('type') == 'uap' else kind}:{_slug(d['name'])}"
            c = per.get(d["mac"]) or {}
            clients = ", ".join(f"{c[k]} {k}" for k in ("wired", "wireless") if c.get(k))
            extra = {"networks": vlans, "wlans": wlans} if kind == "router" else {}
            self.node(ids[d["mac"]], kind, d["name"], kind={"udm": "Gateway", "ugw": "Gateway", "usw": "Switch", "uap": "Access point"}.get(d.get("type"), d.get("type")),
                      model=d.get("model"), address=d.get("ip"), firmware=d.get("version"), clients=clients or None,
                      online=d.get("state") == 1, unifiState=d.get("state"), **extra)
        gateway = next((ids[d["mac"]] for d in devices if d.get("type") in ("udm", "ugw")), None)
        if gateway:
            for n in networks.values():
                if n.get("purpose") == "wan" and n.get("enabled"):
                    self.node(f"wan:{_slug(n['name'])}", "isp", n["name"], kind="Internet connection")
                    self.edge(f"wan:{_slug(n['name'])}", gateway, "link", label="WAN")

        def port_label(parent: dict | None, number) -> str | None:
            if number is None:
                return None
            ports = (parent or {}).get("ports") or {}
            info = ports.get(str(number)) or ports.get(number) or {}    # JSON has string keys
            return " · ".join(x for x in (info.get("name") or f"port {number}", _speed(info.get("speed"))) if x)

        for d in devices:
            up = d.get("uplink") or {}
            parent = by_mac.get(up.get("mac"))
            if parent:
                self.edge(ids[parent["mac"]], ids[d["mac"]], "link", label=port_label(parent, up.get("remotePort")))
        networks_by_name = networks
        hanging: dict[str, str] = {}
        addr = {n["address"]: i for i, n in self.nodes.items() if n["type"] == "machine" and n.get("address")}
        for c in snap.get("known") or []:
            node_id = addr.get(c["ip"])
            if not node_id:
                continue
            net = networks_by_name.get(c.get("network") or "") or {}
            self.nodes[node_id].update({k: v for k, v in {"network": c.get("network"), "vlan": c.get("vlan") or net.get("vlan"),
                                                           "subnet": _network(net.get("subnet"))}.items() if v})
            behind = (self.network.get("machine_uplinks") or {}).get(node_id.split(":", 1)[1])
            if behind and behind in self.nodes:   # a guest of a hand-kept host (an LXC on the Proxmox host): it hangs on that host
                continue
            parent = by_mac.get(c.get("switch") if c.get("wired") else c.get("ap"))
            if parent:
                label = port_label(parent, c.get("port")) if c.get("wired") else (c.get("essid") or "wifi")
                hanging[node_id] = ids[parent["mac"]]
                self.edge(ids[parent["mac"]], node_id, "link", label=label)
        return hanging

    def network_layer(self) -> None:
        """Hand-kept links between node ids, uplinks of machines and annotations of existing nodes."""
        net = self.network
        uplink = net.get("default_uplink")
        overrides = net.get("machine_uplinks") or {}
        hanging: dict[str, str] = {}
        if self.unifi:
            hanging = self.unifi_layer()
            uplink = None   # a machine the controller does not know has no known uplink; the placeholders are gone
        for node_id, n in list(self.nodes.items()):
            if n["type"] == "machine" and not n.get("external") and node_id not in hanging:
                up = overrides.get(node_id.split(":", 1)[1], uplink)
                if up and up not in getattr(self, "removed", set()):
                    self.edge(up, node_id, "link")
        for link in net.get("links") or []:
            if link["source"] in getattr(self, "removed", set()) or link["target"] in getattr(self, "removed", set()):
                continue
            self.edge(link["source"], link["target"], link.get("type", "link"), label=link.get("label"))
        # Names and facts for nodes the other sources only know by address; the key is the node id.
        for node_id, attrs in (net.get("annotations") or {}).items():
            if node_id in self.nodes:
                self.nodes[node_id].update(attrs)
            else:
                self.warnings.append(f"annotation for unknown node {node_id}")

    # -- output ----------------------------------------------------------------------------------------------

    def dangling(self) -> list[str]:
        return [f"{e['type']}: {e['source']} -> {e['target']}" for e in self.edges.values()
                if e["source"] not in self.nodes or e["target"] not in self.nodes]

    def build(self) -> dict:
        self.load()
        self.machines()
        self.network_nodes()
        self.stack_nodes()
        self.platform()
        self.ingress()
        self.inferred_dependencies()
        self.network_layer()
        for d in self.dangling():
            self.warnings.append(f"dangling edge {d}")
        # Edges to a missing node would break the viewer; drop them (they are reported above).
        edges = [e for e in self.edges.values() if e["source"] in self.nodes and e["target"] in self.nodes]
        return {
            "generated": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "nodes": sorted(self.nodes.values(), key=lambda n: n["id"]),
            "edges": sorted(edges, key=lambda e: e["id"]),
            "warnings": self.warnings,
            **({"sources": {"unifi": {"fetched": self.unifi.get("fetched")}}} if self.unifi else {}),
        }


def build_graph(root: pathlib.Path, unifi: dict | None = None) -> dict:
    return Builder(root, unifi).build()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", type=pathlib.Path, default=pathlib.Path(__file__).resolve().parents[2])
    ap.add_argument("-o", "--output", type=pathlib.Path)
    ap.add_argument("--check", action="store_true", help="exit 1 if an edge points at a missing node")
    ap.add_argument("--unifi", type=pathlib.Path, help="snapshot of the UniFi controller (unifi.py): replaces the network placeholders")
    args = ap.parse_args(argv)
    graph = build_graph(args.root, json.loads(args.unifi.read_text()) if args.unifi else None)
    dangling = [w for w in graph["warnings"] if w.startswith("dangling edge")]
    if args.check:
        for w in graph["warnings"]:
            print(f"warning: {w}", file=sys.stderr)
        print(f"{len(graph['nodes'])} nodes, {len(graph['edges'])} edges")
        return 1 if dangling else 0
    text = json.dumps(graph, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        tmp = args.output.with_suffix(args.output.suffix + ".tmp")
        tmp.write_text(text)
        tmp.replace(args.output)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
