import hashlib
import json
import textwrap

import pytest
import yaml

from ingressctl.errors import ConfigError, ManifestError
from ingressctl.inventory import load_inventory
from ingressctl.manifest import ServiceSpec, load_all, normalize_domain

LE_MANIFEST = """
apiVersion: homelab.ingress/v1
namespace: ai-sandbox
services:
  - id: omniroute
    stack: omniroute
    lifecycle: present
    proxy:
      domains: [app.pub.example]
      upstream: {scheme: http, server: worker, port: 20128}
%s    certificate: {%s}
    healthcheck: {path: /, accepted_statuses: [200]}
"""


def _le_manifest(env, cert="issuer_ref: letsencrypt", proxy_extra=""):
    env.write("stacks/ai/omniroute/ingress.yaml", LE_MANIFEST % (proxy_extra, cert))


def _set_le_issuer(env, **fields):
    p = env.root / "inventory/ingress.yaml"
    doc = yaml.safe_load(p.read_text())
    doc["issuers"]["letsencrypt"].update(fields)
    p.write_text(yaml.safe_dump(doc))


def _policy_hash(domains, issuer, validity):
    data = {"domains": domains, "issuer": issuer, "validity_days": validity}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16]


def test_domains_are_normalized(env):
    (spec,) = env.specs("omniroute")
    assert spec.domains == ("omni.lab.example",)
    assert spec.resource_id == "ai-sandbox/omniroute"
    assert spec.upstream_host == "10.0.0.20"  # from inventory/servers.yaml, not duplicated in the manifest


@pytest.mark.parametrize("raw", ["*.lab.example", "bad_name.lab.example", "localhost", "-x.lab.example", "10.0.0.1"])
def test_invalid_domains_rejected(raw):
    with pytest.raises(ManifestError):
        normalize_domain(raw)


def test_idn_is_punycoded():
    assert normalize_domain("café.lab.example") == "xn--caf-dma.lab.example"


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
    with pytest.raises(ManifestError, match="does not match the directory"):
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
    with pytest.raises(ManifestError, match="is not allowed in inventory/ingress.yaml"):
        env.specs()


def test_validity_above_ca_policy_rejected(env):
    text = (env.root / "stacks/ai/omniroute/ingress.yaml").read_text().replace("validity_days: 90", "validity_days: 200")
    (env.root / "stacks/ai/omniroute/ingress.yaml").write_text(text)
    with pytest.raises(ManifestError, match="CA policy"):
        env.specs()


def test_policy_hash_ignores_upstream(env):
    (a,) = env.specs("omniroute")
    text = (env.root / "stacks/ai/omniroute/ingress.yaml").read_text().replace("20128", "20129")
    (env.root / "stacks/ai/omniroute/ingress.yaml").write_text(text)
    (b,) = env.specs("omniroute")
    assert a.policy_hash() == b.policy_hash() and a.upstream_port != b.upstream_port


def test_public_zone_is_rejected_for_private_ca(env):
    """*.example.org stays with Let's Encrypt in NPM; the private CA is only for *.lab.example."""
    text = (env.root / "stacks/nas/seerr/ingress.yaml").read_text().replace("seerr.lab.example", "seerr.example.org")
    (env.root / "stacks/nas/seerr/ingress.yaml").write_text(text)
    with pytest.raises(ManifestError, match="are outside"):
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


def test_existing_manifest_has_no_public_spec(env):
    (spec,) = env.specs("omniroute")
    assert spec.public is None


def test_public_block_is_validated_and_round_trips(env):
    _set_le_issuer(env, dns_provider="cloudflare")
    _le_manifest(env, cert="issuer_ref: letsencrypt, dns_challenge: true",
                 proxy_extra="      public: "
                             '{paths: [/hook/abc, "/files/*"], methods: [post], allow_cidrs: [10.0.0.0/8]}\n')
    (spec,) = env.specs("omniroute")
    assert spec.public is not None
    assert spec.public.paths == ("/files/*", "/hook/abc")
    assert spec.public.methods == ("POST",)
    assert spec.public.allow_cidrs == ("10.0.0.0/8",)
    assert spec.public.allow_presets == ()
    assert ServiceSpec.from_json(spec.to_json()) == spec


def test_public_block_accepts_a_preset(env):
    _le_manifest(env, proxy_extra="      public: {paths: [/hook/abc], allow_cidrs: {preset: github-hooks}}\n")
    (spec,) = env.specs("omniroute")
    assert spec.public is not None
    assert spec.public.allow_cidrs == ()
    assert spec.public.allow_presets == ("github-hooks",)
    assert ServiceSpec.from_json(spec.to_json()) == spec


def test_from_json_accepts_public_without_allow_presets(env):
    """State rows written before this slice carry no allow_presets."""
    _set_le_issuer(env, dns_provider="cloudflare")
    _le_manifest(env, cert="issuer_ref: letsencrypt, dns_challenge: true",
                 proxy_extra="      public: {paths: [/a], allow_cidrs: [10.0.0.0/8]}\n")
    (spec,) = env.specs("omniroute")
    data = json.loads(spec.to_json())
    del data["public"]["allow_presets"]
    loaded = ServiceSpec.from_json(json.dumps(data))
    assert loaded == spec
    assert loaded.public.allow_presets == ()


def test_invalid_public_block_is_rejected(env):
    _le_manifest(env, proxy_extra="      public: {paths: [/]}\n")
    with pytest.raises(ManifestError, match="proxy.public.paths"):
        env.specs("omniroute")


def test_public_block_unknown_field_is_rejected(env):
    _le_manifest(env, proxy_extra="      public: {paths: [/hook], typo: 1}\n")
    with pytest.raises(ManifestError, match="typo"):
        env.specs("omniroute")


def test_stack_without_inventory_entry_is_derived_from_its_folder(env):
    repo = env.root
    """A folder with compose.yaml and ingress.yaml needs no line in inventory/stacks.yaml."""
    d = repo / "stacks/ai/newapp"
    d.mkdir(parents=True)
    (d / "compose.yaml").write_text("services: {a: {image: x}}\n")
    (d / "ingress.yaml").write_text(textwrap.dedent("""
        apiVersion: homelab.ingress/v1
        namespace: ai-sandbox
        services:
          - id: newapp
            stack: newapp
            lifecycle: present
            proxy:
              domains: [newapp.lab.example]
              upstream: {scheme: http, server: worker, port: 8080}
            certificate: {issuer_ref: homelab-private-ca, validity_days: 90, renew_before_days: 30}
            healthcheck: {path: /, accepted_statuses: [200]}
    """))
    inv = load_inventory(repo)
    assert inv.stacks["newapp"]["server"] == "worker" and inv.stacks["newapp"]["path"] == "stacks/ai/newapp"
    assert "newapp" in {s.stack_id for s in load_all(repo, inv)}


def test_letsencrypt_issuer_defaults_and_validity(env):
    _set_le_issuer(env, max_validity_days=365, propagation_seconds=45)
    issuer = load_inventory(env.root).issuers["letsencrypt"]
    assert issuer.type == "letsencrypt"
    assert issuer.max_validity_days == 90
    assert issuer.email == "admin@lab.example"
    assert issuer.dns_challenge is False and issuer.dns_provider is None
    assert issuer.propagation_seconds == 45
    assert issuer.missing() == []
    assert issuer.permits("app.pub.example") and not issuer.permits("app.lab.example")


def test_letsencrypt_missing_requires_email_and_allowed_domains(env):
    _set_le_issuer(env, email=None, allowed_domains=[])
    issuer = load_inventory(env.root).issuers["letsencrypt"]
    assert issuer.missing() == ["email", "allowed_domains"]
    assert load_inventory(env.root).issuers["homelab-private-ca"].missing() == []


def test_letsencrypt_empty_allowed_domains_permits_nothing(env):
    _set_le_issuer(env, allowed_domains=[])
    issuer = load_inventory(env.root).issuers["letsencrypt"]
    assert not issuer.permits("app.pub.example")
    assert not issuer.permits("anything.example")
    _le_manifest(env)
    with pytest.raises(ManifestError, match="are outside"):
        env.specs("omniroute")


def test_letsencrypt_manifest_is_accepted(env):
    _le_manifest(env)
    (spec,) = env.specs("omniroute")
    assert spec.issuer_ref == "letsencrypt"
    assert spec.validity_days == 90
    assert spec.renew_before_days == 30
    assert spec.dns_challenge is False
    assert spec.policy_hash() == _policy_hash(["app.pub.example"], "letsencrypt", 90)


def test_letsencrypt_domain_outside_allowed_domains_rejected(env):
    _le_manifest(env)
    env.write("stacks/ai/omniroute/ingress.yaml",
              (env.root / "stacks/ai/omniroute/ingress.yaml").read_text().replace("app.pub.example", "app.lab.example"))
    with pytest.raises(ManifestError, match="are outside"):
        env.specs("omniroute")


def test_letsencrypt_validity_days_rejected(env):
    _le_manifest(env, cert="issuer_ref: letsencrypt, validity_days: 90")
    with pytest.raises(ManifestError, match="fixes the lifetime \\(90 days\\)"):
        env.specs("omniroute")


def test_letsencrypt_renew_before_must_stay_below_90(env):
    _le_manifest(env, cert="issuer_ref: letsencrypt, renew_before_days: 89")
    (spec,) = env.specs("omniroute")
    assert spec.renew_before_days == 89
    _le_manifest(env, cert="issuer_ref: letsencrypt, renew_before_days: 90")
    with pytest.raises(ManifestError, match="must be smaller than"):
        env.specs("omniroute")


def test_dns_challenge_rejected_for_private_ca(env):
    text = (env.root / "stacks/nas/seerr/ingress.yaml").read_text().replace(
        "certificate: {issuer_ref: homelab-private-ca}",
        "certificate: {issuer_ref: homelab-private-ca, dns_challenge: true}")
    (env.root / "stacks/nas/seerr/ingress.yaml").write_text(text)
    with pytest.raises(ManifestError, match="only allowed with a Let's Encrypt issuer"):
        env.specs("seerr")


def test_dns_challenge_requires_dns_provider(env):
    _le_manifest(env, cert="issuer_ref: letsencrypt, dns_challenge: true")
    with pytest.raises(ManifestError, match="dns_provider"):
        env.specs("omniroute")


def test_dns_challenge_with_dns_provider_is_accepted(env):
    _set_le_issuer(env, dns_provider="cloudflare")
    _le_manifest(env, cert="issuer_ref: letsencrypt, dns_challenge: true")
    (spec,) = env.specs("omniroute")
    assert spec.dns_challenge is True
    assert spec.policy_hash() != _policy_hash(["app.pub.example"], "letsencrypt", 90)


def test_dns_challenge_defaults_from_issuer_and_manifest_wins(env):
    _set_le_issuer(env, dns_challenge=True, dns_provider="cloudflare")
    _le_manifest(env)
    (default,) = env.specs("omniroute")
    assert default.dns_challenge is True
    _le_manifest(env, cert="issuer_ref: letsencrypt, dns_challenge: false")
    (explicit,) = env.specs("omniroute")
    assert explicit.dns_challenge is False
    assert explicit.policy_hash() == _policy_hash(["app.pub.example"], "letsencrypt", 90)


def test_letsencrypt_public_allow_cidrs_without_dns_challenge_rejected(env):
    _le_manifest(env, proxy_extra="      public: {paths: [/hook], allow_cidrs: [10.0.0.0/8]}\n")
    with pytest.raises(ManifestError, match="allow_cidrs"):
        env.specs("omniroute")


def test_letsencrypt_public_allow_cidrs_with_dns_challenge_accepted(env):
    _set_le_issuer(env, dns_provider="cloudflare")
    _le_manifest(env, cert="issuer_ref: letsencrypt, dns_challenge: true",
                 proxy_extra="      public: {paths: [/hook], allow_cidrs: [10.0.0.0/8]}\n")
    (spec,) = env.specs("omniroute")
    assert spec.dns_challenge is True and spec.public.allow_cidrs == ("10.0.0.0/8",)


def test_public_block_requires_letsencrypt_issuer(env):
    text = (env.root / "stacks/ai/omniroute/ingress.yaml").read_text().replace(
        "      force_https: true\n",
        "      force_https: true\n      public: {paths: [/hook], allow_cidrs: [10.0.0.0/8]}\n")
    (env.root / "stacks/ai/omniroute/ingress.yaml").write_text(text)
    with pytest.raises(ManifestError, match="proxy.public requires a Let's Encrypt issuer"):
        env.specs("omniroute")


def test_unknown_issuer_type_is_config_error(env):
    _set_le_issuer(env, type="acme")
    with pytest.raises(ConfigError, match="infisical' or 'letsencrypt"):
        load_inventory(env.root)


def test_negative_propagation_seconds_is_config_error(env):
    _set_le_issuer(env, propagation_seconds=-1)
    with pytest.raises(ConfigError, match="propagation_seconds"):
        load_inventory(env.root)


def test_dns_challenge_must_be_boolean(env):
    _le_manifest(env, cert="issuer_ref: letsencrypt, dns_challenge: 'true'")
    with pytest.raises(ManifestError, match="dns_challenge"):
        env.specs("omniroute")


def test_from_json_without_dns_challenge_defaults_false(env):
    (spec,) = env.specs("omniroute")
    d = json.loads(spec.to_json())
    d.pop("dns_challenge")
    assert ServiceSpec.from_json(json.dumps(d)) == spec
    assert ServiceSpec.from_json(json.dumps(d)).dns_challenge is False


def test_policy_hash_of_existing_omniroute_spec_is_unchanged(env):
    """Hash from main before this change: existing specs must not see a reissue."""
    (spec,) = env.specs("omniroute")
    assert spec.dns_challenge is False
    assert spec.policy_hash() == "1409215e7ea35e14"
    assert spec.policy_hash() == _policy_hash(["omni.lab.example"], "homelab-private-ca", 90)
