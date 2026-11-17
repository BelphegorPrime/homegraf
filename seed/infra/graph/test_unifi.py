"""Tests for the UniFi snapshot and its place in the graph (pytest infra/graph)."""
import base64
import hashlib
import http.server
import json
import pathlib
import sys
import threading

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import build  # noqa: E402
import unifi  # noqa: E402

GATEWAY, CORE, FLEX, AP = "aa:00", "bb:00", "cc:00", "dd:00"
DEVICES = [
    {"mac": GATEWAY, "name": "UDM SE", "model": "UDMPROSE", "type": "udm", "ip": "10.0.0.100", "state": 1, "version": "5.1",
     "uplink": {"uplink_mac": "ff:ff", "port_idx": 9, "type": "wire"},
     "port_table": [{"port_idx": 1, "name": "Port 1", "up": True, "speed": 1000}, {"port_idx": 11, "name": "SFP+ 2", "up": True, "speed": 10000},
                    {"port_idx": 2, "name": "Port 2", "up": False}]},
    {"mac": CORE, "name": "USW Core", "model": "US48", "type": "usw", "ip": "10.0.0.150", "state": 1,
     "uplink": {"uplink_mac": GATEWAY, "uplink_remote_port": 11, "port_idx": 51, "type": "wire"},
     "port_table": [{"port_idx": 4, "name": "Port 4", "up": True, "speed": 1000}, {"port_idx": 46, "name": "Port 46", "up": True, "speed": 2500}]},
    {"mac": FLEX, "name": "Flex", "type": "usw", "state": 1, "uplink": {"uplink_mac": GATEWAY, "uplink_remote_port": 1, "port_idx": 9, "type": "wire"}},
    {"mac": AP, "name": "Attic AP", "model": "U6", "type": "uap", "ip": "10.0.0.37", "state": 1,
     "uplink": {"uplink_mac": GATEWAY, "uplink_remote_port": 2, "port_idx": 1, "type": "wire"}},
]
NETWORKS = [
    {"_id": "n1", "name": "Provider", "purpose": "wan", "enabled": True},
    {"_id": "n0", "name": "Spare WAN", "purpose": "wan"},
    {"_id": "n2", "name": "Default", "purpose": "corporate", "ip_subnet": "10.0.0.1/24", "enabled": True},
    {"_id": "n3", "name": "IoT", "purpose": "corporate", "ip_subnet": "10.0.10.1/24", "vlan_enabled": True, "vlan": 10, "enabled": True},
]
WLANS = [{"name": "Home", "enabled": True, "security": "wpapsk", "networkconf_id": "n2", "x_passphrase": "SECRET-WIFI"},
         {"name": "Off", "enabled": False, "networkconf_id": "n3"}]
CLIENTS = [
    {"ip": "10.0.0.45", "mac": "11:11", "is_wired": True, "sw_mac": CORE, "sw_port": 46, "network": "Default", "hostname": "workhorse"},
    {"ip": "10.0.10.100", "mac": "22:22", "is_wired": True, "sw_mac": CORE, "sw_port": 4, "network": "IoT", "vlan": 10},
    {"ip": "10.0.0.99", "mac": "33:33", "is_wired": False, "ap_mac": AP, "essid": "Home", "hostname": "someones-phone"},
    {"ip": "10.0.0.9", "mac": "44:44", "is_wired": False, "ap_mac": AP, "essid": "Home", "hostname": "tv"},
]
KNOWN = {"10.0.0.45", "10.0.10.100", "10.0.0.123"}


def snapshot():
    return unifi.snapshot_from(DEVICES, NETWORKS, WLANS, CLIENTS, KNOWN)


def test_the_snapshot_keeps_what_the_map_needs_and_nothing_private():
    s = snapshot()
    assert [d["name"] for d in s["devices"]] == ["UDM SE", "USW Core", "Flex", "Attic AP"]
    assert s["devices"][1]["uplink"] == {"mac": GATEWAY, "remotePort": 11, "port": 51, "type": "wire", "speed": None}
    assert s["devices"][0]["ports"] == {1: {"name": "Port 1", "speed": 1000}, 11: {"name": "SFP+ 2", "speed": 10000}}   # ports that are up
    assert s["clients"] == {"total": 4, "wired": 2, "perDevice": {CORE: {"wired": 2, "wireless": 0}, AP: {"wired": 0, "wireless": 2}}}
    assert [k["ip"] for k in s["known"]] == ["10.0.0.45", "10.0.10.100"]                                   # only the machines we know
    text = json.dumps(s)
    for private in ("SECRET-WIFI", "someones-phone", "workhorse", "11:11", "33:33"):
        assert private not in text
    assert [w["name"] for w in s["wlans"]] == ["Home", "Off"] and s["wlans"][0]["network"] == "Default"
    assert s["networks"][3]["vlan"] == 10 and s["networks"][2]["vlan"] is None


def test_known_addresses_come_from_servers_and_hand_kept_machines(tmp_path):
    (tmp_path / "inventory").mkdir()
    (tmp_path / "inventory/servers.yaml").write_text("servers:\n  a: {address: 10.0.0.1}\n  b: {}\n")
    (tmp_path / "inventory/network.yaml").write_text("nodes:\n  - {id: 'machine:pve', type: machine, address: 10.0.0.42}\n  - {id: router, type: router}\n")
    assert unifi.known_addresses(tmp_path) == {"10.0.0.1", "10.0.0.42"}
    assert unifi.known_addresses(tmp_path / "nowhere") == set()


class FakeController(http.server.BaseHTTPRequestHandler):
    calls: list = []
    password = "pw"

    def _json(self, code, data, cookie=False):
        raw = json.dumps(data).encode()
        self.send_response(code)
        if cookie:
            self.send_header("Set-Cookie", "TOKEN=abc; Path=/")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        self.calls.append(("POST", self.path))
        if self.path == "/api/auth/login":
            ok = body.get("username") == "reader" and body.get("password") == self.password
            return self._json(200 if ok else 401, {}, cookie=ok)
        return self._json(200, {})

    def do_GET(self):
        self.calls.append(("GET", self.path))
        if "TOKEN=abc" not in (self.headers.get("Cookie") or ""):
            return self._json(401, {})
        data = {"stat/device": DEVICES, "rest/networkconf": NETWORKS, "rest/wlanconf": WLANS, "stat/sta": CLIENTS}[self.path.rsplit("/s/default/", 1)[1]]
        return self._json(200, {"meta": {"rc": "ok"}, "data": data})

    def log_message(self, *args):
        pass


@pytest.fixture
def controller():
    srv = http.server.HTTPServer(("127.0.0.1", 0), FakeController)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    FakeController.calls = []
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_fetch_logs_in_reads_and_logs_out(controller):
    s = unifi.fetch(unifi.Controller(controller, "reader", "pw"), KNOWN)
    assert len(s["devices"]) == 4 and len(s["known"]) == 2
    calls = FakeController.calls
    assert calls[0] == ("POST", "/api/auth/login") and calls[-1] == ("POST", "/api/auth/logout")
    assert {m for m, _ in calls[1:-1]} == {"GET"}                                     # between them it only reads


def test_wrong_password_and_unreachable_controller_are_errors_not_tracebacks(controller):
    with pytest.raises(unifi.UnifiError, match="HTTP 401"):
        unifi.fetch(unifi.Controller(controller, "reader", "wrong"), KNOWN)
    with pytest.raises(unifi.UnifiError, match="not reachable"):
        unifi.fetch(unifi.Controller("http://127.0.0.1:9", "reader", "pw", timeout=2), KNOWN)


def test_a_pinned_certificate_must_match(monkeypatch):
    der = b"not really a certificate"
    pem = "-----BEGIN CERTIFICATE-----\n" + base64.encodebytes(der).decode() + "-----END CERTIFICATE-----\n"
    monkeypatch.setattr(unifi.ssl, "get_server_certificate", lambda addr, timeout=None: pem)
    good = hashlib.sha256(der).hexdigest()
    unifi.Controller("https://udm.test", "u", "p", fingerprint=good.upper())._pin()
    unifi.Controller("https://udm.test", "u", "p", fingerprint=":".join(good[i:i + 2] for i in range(0, len(good), 2)))._pin()
    with pytest.raises(unifi.UnifiError, match="not the pinned one"):
        unifi.Controller("https://udm.test", "u", "p", fingerprint="00" * 32)._pin()
    unifi.Controller("https://udm.test", "u", "p")._pin()                              # nothing pinned: nothing checked


# --- the graph ---

ROOT_FILES = {
    "inventory/servers.yaml": "servers:\n  nas: {address: 10.0.0.101}\n  hw: {address: 10.0.0.45}\n  lonely: {address: 10.0.0.250}\n",
    "inventory/stacks.yaml": "stacks: []\n",
    "inventory/network.yaml": """
nodes:
  - {id: isp, type: isp, label: ISP, placeholder: true}
  - {id: router, type: router, label: Router, placeholder: true}
  - {id: switch, type: switch, label: Switch, placeholder: true}
  - {id: "machine:pve", type: machine, label: pve, address: 10.0.0.46}
  - {id: "machine:ha", type: machine, label: ha, address: 10.0.10.100, vlan: 10.0.10.x}
links:
  - {source: isp, target: router, label: WAN}
  - {source: router, target: switch, label: LAN}
  - {source: "machine:pve", target: "machine:hw", type: hosts}
default_uplink: switch
machine_uplinks: {hw: "machine:pve", ha: router}
""",
}


@pytest.fixture
def root(tmp_path):
    for name, text in ROOT_FILES.items():
        f = tmp_path / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    return tmp_path


def graph(root, snap=None):
    g = build.build_graph(root, snap)
    return {n["id"]: n for n in g["nodes"]}, {(e["source"], e["target"]): e for e in g["edges"] if e["type"] == "link"}, g


def snap_for_graph():
    clients = CLIENTS + [{"ip": "10.0.0.46", "is_wired": True, "sw_mac": CORE, "sw_port": 46, "network": "Default"},
                         {"ip": "10.0.0.101", "is_wired": True, "sw_mac": CORE, "sw_port": 4, "network": "Default"}]
    return unifi.snapshot_from(DEVICES, NETWORKS, WLANS, clients, {"10.0.0.45", "10.0.0.46", "10.0.0.101", "10.0.10.100"})


def test_placeholders_stay_without_a_snapshot(root):
    nodes, links, g = graph(root)
    assert {"isp", "router", "switch"} <= set(nodes) and ("switch", "machine:nas") in links and "sources" not in g


def test_the_snapshot_replaces_the_placeholders_with_the_real_devices(root):
    nodes, links, g = graph(root, snap_for_graph())
    assert not ({"isp", "router", "switch"} & set(nodes))
    assert {i for i in nodes if i.split(":")[0] in ("wan", "router", "switch", "ap")} == {
        "wan:provider", "router:udm-se", "switch:usw-core", "switch:flex", "ap:attic-ap"}              # the disabled WAN is not there
    assert nodes["router:udm-se"]["kind"] == "Gateway" and nodes["ap:attic-ap"]["kind"] == "Access point" and nodes["ap:attic-ap"]["type"] == "device"
    assert nodes["ap:attic-ap"]["clients"] == "2 wireless" and nodes["switch:usw-core"]["clients"] == "4 wired"
    assert links[("wan:provider", "router:udm-se")]["label"] == "WAN"
    assert links[("router:udm-se", "switch:usw-core")]["label"] == "SFP+ 2 · 10G"
    assert links[("router:udm-se", "ap:attic-ap")]["label"] == "port 2"                                  # port not up in the table: its number
    assert links[("switch:usw-core", "machine:nas")]["label"] == "Port 4 · 1G"
    assert links[("switch:usw-core", "machine:pve")]["label"] == "Port 46 · 2.5G"
    assert g["sources"]["unifi"]["fetched"]
    assert [w for w in g["warnings"] if "dangling" in w or "unknown" in w] == []


def test_a_device_carries_the_controllers_state_for_the_health_of_the_map(root):
    snap = snap_for_graph()
    next(d for d in snap["devices"] if d["name"] == "Attic AP")["state"] = 0
    nodes, _, _ = graph(root, snap)
    assert (nodes["router:udm-se"]["online"], nodes["router:udm-se"]["unifiState"]) == (True, 1)
    assert (nodes["ap:attic-ap"]["online"], nodes["ap:attic-ap"]["unifiState"]) == (False, 0)      # state 0 is not dropped as empty


def test_a_guest_of_a_hand_kept_host_hangs_on_that_host(root):
    nodes, links, _ = graph(root, snap_for_graph())
    assert ("machine:pve", "machine:hw") in links and ("switch:usw-core", "machine:hw") not in links   # the LXC is behind pve, not on port 46


def test_machines_get_their_network_and_the_gateway_knows_the_vlans(root):
    nodes, links, _ = graph(root, snap_for_graph())
    assert (nodes["machine:ha"]["network"], nodes["machine:ha"]["vlan"], nodes["machine:ha"]["subnet"]) == ("IoT", 10, "10.0.10.0/24")
    assert ("switch:usw-core", "machine:ha") in links                                                    # the override pointed at a placeholder
    assert nodes["router:udm-se"]["networks"] == "Default 10.0.0.0/24 · IoT (VLAN 10) 10.0.10.0/24"
    assert nodes["router:udm-se"]["wlans"] == "Home (Default)"


def test_a_machine_the_controller_does_not_see_has_no_uplink(root):
    nodes, links, _ = graph(root, snap_for_graph())
    assert not [k for k in links if k[1] == "machine:lonely"]                                            # no default uplink once the real network is known
