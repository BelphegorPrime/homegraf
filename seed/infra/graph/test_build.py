"""Tests for the graph builder (pytest infra/graph)."""
import pathlib
import sys
import textwrap

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import build  # noqa: E402

FILES = {
    "inventory/homegraf.yaml": textwrap.dedent("""
        platform:
          npm: {label: Nginx Proxy Manager, stack: proxy, kind: proxy}
          komodo: {label: Komodo, stack: other, kind: deploy}
          infisical: {label: Infisical, stack: app, kind: secrets}
          ca: {label: Homelab CA, stack: app, kind: certificates}
    """),
    "inventory/servers.yaml": "servers:\n  nas: {address: 10.0.0.1}\n  box: {address: 10.0.0.2}\n",
    "inventory/stacks.yaml": textwrap.dedent("""
        stacks:
          - {id: app, path: stacks/app, compose_files: [compose.yaml], server: nas, komodo_stack: app, status: verified, secrets: infisical}
          - {id: other, path: stacks/other, compose_files: [compose.yaml], server: box, komodo_stack: other, status: verified, job: true}
          - {id: old, path: stacks/old, compose_files: [compose.yaml], server: box, status: inactive}
    """),
    "inventory/network.yaml": textwrap.dedent("""
        nodes:
          - {id: router, type: router, label: Router}
          - {id: "machine:hv", type: machine, label: hv, address: 10.0.0.77, kind: LXC}
        links: []
        default_uplink: router
        annotations:
          service:npm-hand: {label: Hand Made, kind: demo}
          machine:nowhere: {label: x}
    """),
    "stacks/app/compose.yaml": textwrap.dedent("""
        services:
          web:
            image: app:1
            ports: ["127.0.0.1:8080:80/tcp"]
            depends_on: [db]
            environment: ["UPSTREAM=http://10.0.0.2:9000/api"]
          db:
            image: postgres:17
    """),
    "stacks/app/ingress.yaml": textwrap.dedent("""
        services:
          - id: app
            stack: app
            proxy: {domains: [app.lab.example, www.lab.example], upstream: {scheme: http, server: nas, port: 8080}}
    """),
    "stacks/other/compose.yaml": "services:\n  api:\n    image: other:1\n    ports: ['9000:3000']\n",
    "stacks/old/compose.yaml": "services: {x: {image: x}}\n",
    "inventory/npm-hosts.yaml": textwrap.dedent("""
        hosts:
          - {id: 1, domains: [app.example.org], upstream: {scheme: http, host: 10.0.0.1, port: 8080}, enabled: true}
          - {id: 2, domains: [hand.example.org], upstream: {scheme: http, host: 10.0.0.77, port: 81}, enabled: true}
          - {id: 3, domains: [off.example.org], upstream: {scheme: http, host: 10.0.0.1, port: 8080}, enabled: false}
    """),
    "external/ingress.yaml": textwrap.dedent("""
        services:
          - id: appliance
            stack: external
            proxy: {domains: [nas.lab.example], upstream: {scheme: https, host: 10.0.0.2, port: 443}}
          - id: vm
            stack: external
            proxy: {domains: [vm.lab.example], upstream: {scheme: http, host: 10.0.0.99, port: 80}}
    """),
}


@pytest.fixture
def graph(tmp_path):
    for rel, content in FILES.items():
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content)
    return build.build_graph(tmp_path)


def edges(graph, type_):
    return {(e["source"], e["target"]) for e in graph["edges"] if e["type"] == type_}


def test_inactive_stacks_are_left_out(graph):
    assert "stack:old" not in {n["id"] for n in graph["nodes"]}


def test_hosting_chain(graph):
    assert ("machine:nas", "stack:app") in edges(graph, "hosts")
    assert ("stack:app", "container:app/web") in edges(graph, "contains")
    assert ("container:app/web", "container:app/db") in edges(graph, "depends-on")


def test_domains_route_to_the_container_publishing_the_port(graph):
    proxies = [e for e in graph["edges"] if e["type"] == "proxies" and e["target"] == "container:app/web"]
    assert proxies and proxies[0]["domains"] == ["app.example.org", "app.lab.example", "www.lab.example"]
    assert ("domain:app.lab.example", "infra:npm") in edges(graph, "terminated-by")
    stack = next(n for n in graph["nodes"] if n["id"] == "stack:app")
    assert stack["urls"] == ["https://app.example.org", "https://app.lab.example", "https://www.lab.example"]


def test_external_services_get_a_node_on_their_machine(graph):
    assert ("machine:box", "service:appliance") in edges(graph, "hosts")      # host matched by address
    assert ("machine:10.0.0.99", "service:vm") in edges(graph, "hosts")       # unknown host -> external machine


def test_manual_npm_hosts_join_the_stack_that_answers(graph):
    proxy = next(e for e in graph["edges"] if e["type"] == "proxies" and e["target"] == "container:app/web")
    assert "app.example.org" in proxy["domains"]
    stack = next(n for n in graph["nodes"] if n["id"] == "stack:app")
    assert "https://app.example.org" in stack["urls"] and stack["manualUrls"] == ["https://app.example.org"]
    manual = next(n for n in graph["nodes"] if n["id"] == "domain:app.example.org")
    assert manual["manual"] is True
    assert "manual" not in next(n for n in graph["nodes"] if n["id"] == "domain:app.lab.example")


def test_manual_host_without_a_stack_becomes_a_service(graph):
    svc = next(n for n in graph["nodes"] if n["id"] == "service:npm-hand")
    assert svc["label"] == "Hand Made" and svc["urls"] == ["https://hand.example.org"]
    assert ("machine:hv", "service:npm-hand") in edges(graph, "hosts")       # address of a machine defined in network.yaml
    assert "machine:10.0.0.77" not in {n["id"] for n in graph["nodes"]}


def test_annotations_rename_nodes_and_unknown_ids_warn(graph):
    svc = next(n for n in graph["nodes"] if n["id"] == "service:npm-hand")
    assert svc["label"] == "Hand Made" and svc["kind"] == "demo"
    assert "annotation for unknown node machine:nowhere" in graph["warnings"]


def test_disabled_manual_host_has_no_route_and_no_url(graph):
    off = next(n for n in graph["nodes"] if n["id"] == "domain:off.example.org")
    assert off["disabled"] is True and "target" not in off
    assert not [e for e in graph["edges"] if e["type"] == "proxies" and "off.example.org" in e.get("domains", [])]
    stack = next(n for n in graph["nodes"] if n["id"] == "stack:app")
    assert "https://off.example.org" not in stack["urls"]


def test_platform_and_secrets_edges(graph):
    assert ("stack:app", "infra:komodo") in edges(graph, "deployed-by")
    assert ("stack:app", "infra:infisical") in edges(graph, "uses-secrets")
    assert ("stack:other", "infra:infisical") not in edges(graph, "uses-secrets")


def test_a_one_shot_stack_is_marked_as_a_job(graph):
    nodes = {n["id"]: n for n in graph["nodes"]}
    assert nodes["stack:other"]["job"] is True
    assert "job" not in nodes["stack:app"]


def test_dependency_inferred_from_environment(graph):
    dep = next(e for e in graph["edges"] if e["type"] == "depends-on" and e["source"] == "stack:app")
    assert dep["target"] == "stack:other" and dep["inferred"] and dep["via"] == ["UPSTREAM"]


def test_network_layer(graph):
    assert ("router", "machine:nas") in edges(graph, "link")
    assert not [n for n in graph["nodes"] if n["id"] == "machine:10.0.0.99" and ("router", n["id"]) in edges(graph, "link")]


def test_every_edge_points_at_a_node(graph):
    ids = {n["id"] for n in graph["nodes"]}
    assert all(e["source"] in ids and e["target"] in ids for e in graph["edges"])


def test_real_repo_builds_without_dangling_edges():
    root = pathlib.Path(__file__).resolve().parents[2]
    assert build.main(["--root", str(root), "--check"]) == 0


def test_without_platform_settings_the_map_has_no_platform_and_no_dangling_edges(tmp_path):
    files = {k: v for k, v in FILES.items() if k != "inventory/homegraf.yaml"}
    for rel, content in files.items():
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content)
    g = build.build_graph(tmp_path)
    assert not [n for n in g["nodes"] if n["type"] == "infra"]
    assert not [e for e in g["edges"] if e["type"] in ("deployed-by", "uses-secrets", "terminated-by", "proxies", "certificates-from")]
    assert [w for w in g["warnings"] if "dangling" in w] == []
