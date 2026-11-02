import pytest

from ingressctl.errors import ManifestError
from ingressctl.manifest import normalize_domain


def test_domains_are_normalized(env):
    (spec,) = env.specs("omniroute")
    assert spec.domains == ("omni.lab.example",)
    assert spec.resource_id == "ai-sandbox/omniroute"
    assert spec.upstream_host == "10.0.0.20"  # aus inventory/servers.yaml, nicht im Manifest dupliziert


@pytest.mark.parametrize("raw", ["*.lab.example", "bad_name.lab.example", "localhost", "-x.lab.example", "10.0.0.1"])
def test_invalid_domains_rejected(raw):
    with pytest.raises(ManifestError):
        normalize_domain(raw)


def test_idn_is_punycoded():
    assert normalize_domain("bücher.lab.example") == "xn--bcher-kva.lab.example"


def test_unknown_field_is_error(env):
    env.write("stacks/nas/seerr/ingress.yaml", """
        apiVersion: homelab.ingress/v1
        namespace: nas-compose
        services:
          - id: seerr
            stack: seerr
            lifecycle: present
            proxy: {domains: [seerr.lab.example], upstream: {scheme: http, server: unas, port: 8089}, typo: 1}
            certificate: {issuer_ref: homelab-private-ca}
            healthcheck: {path: /, accepted_statuses: [200]}
    """)
    with pytest.raises(ManifestError, match="typo"):
        env.specs()


def test_namespace_must_match_directory(env):
    text = (env.root / "stacks/nas/seerr/ingress.yaml").read_text().replace("nas-compose", "ai-sandbox")
    (env.root / "stacks/nas/seerr/ingress.yaml").write_text(text)
    with pytest.raises(ManifestError, match="passt nicht zum Verzeichnis"):
        env.specs()


def test_same_domain_in_two_manifests_fails_before_mutation(env):
    text = (env.root / "stacks/nas/seerr/ingress.yaml").read_text().replace("seerr.lab.example", "omni.lab.example")
    (env.root / "stacks/nas/seerr/ingress.yaml").write_text(text)
    with pytest.raises(ManifestError, match="omni.lab.example"):
        env.specs()
    assert env.npm.mutations == 0


def test_unregistered_issuer_rejected(env):
    text = (env.root / "stacks/nas/seerr/ingress.yaml").read_text().replace("homelab-private-ca", "random-ca")
    (env.root / "stacks/nas/seerr/ingress.yaml").write_text(text)
    with pytest.raises(ManifestError, match="nicht in inventory/ingress.yaml freigegeben"):
        env.specs()


def test_validity_above_ca_policy_rejected(env):
    text = (env.root / "stacks/ai/omniroute/ingress.yaml").read_text().replace("validity_days: 90", "validity_days: 200")
    (env.root / "stacks/ai/omniroute/ingress.yaml").write_text(text)
    with pytest.raises(ManifestError, match="CA-Policy"):
        env.specs()


def test_policy_hash_ignores_upstream(env):
    (a,) = env.specs("omniroute")
    text = (env.root / "stacks/ai/omniroute/ingress.yaml").read_text().replace("20128", "20129")
    (env.root / "stacks/ai/omniroute/ingress.yaml").write_text(text)
    (b,) = env.specs("omniroute")
    assert a.policy_hash() == b.policy_hash() and a.upstream_port != b.upstream_port


def test_public_zone_is_rejected_for_private_ca(env):
    """*.example.org bleibt bei Let's Encrypt in NPM; die private CA ist nur für *.lab.example."""
    text = (env.root / "stacks/nas/seerr/ingress.yaml").read_text().replace("seerr.lab.example", "seerr.example.org")
    (env.root / "stacks/nas/seerr/ingress.yaml").write_text(text)
    with pytest.raises(ManifestError, match="außerhalb"):
        env.specs()


def test_external_manifest_without_stack(env):
    (env.root / "external").mkdir()
    env.write("external/ingress.yaml", """
        apiVersion: homelab.ingress/v1
        namespace: nas-compose
        services:
          - id: pve
            stack: external
            lifecycle: present
            proxy: {domains: [pve.lab.example], upstream: {scheme: https, host: 10.0.0.42, port: 8006}}
            certificate: {issuer_ref: homelab-private-ca}
            healthcheck: {path: /, accepted_statuses: [200]}
    """)
    ns = env.root / "inventory/namespaces.yaml"
    ns.write_text(ns.read_text().replace("paths: [stacks/nas/]", "paths: [stacks/nas/, external/]"))
    spec = next(s for s in env.specs() if s.service_id == "pve")
    assert spec.stack_id == "external" and spec.upstream_host == "10.0.0.42"


def test_external_manifest_requires_external_stack(env):
    (env.root / "external").mkdir()
    env.write("external/ingress.yaml", """
        apiVersion: homelab.ingress/v1
        namespace: nas-compose
        services:
          - id: pve
            stack: seerr
            lifecycle: present
            proxy: {domains: [pve.lab.example], upstream: {scheme: https, host: 10.0.0.42, port: 8006}}
            certificate: {issuer_ref: homelab-private-ca}
            healthcheck: {path: /, accepted_statuses: [200]}
    """)
    ns = env.root / "inventory/namespaces.yaml"
    ns.write_text(ns.read_text().replace("paths: [stacks/nas/]", "paths: [stacks/nas/, external/]"))
    with pytest.raises(ManifestError, match="stack 'external'"):
        env.specs()
