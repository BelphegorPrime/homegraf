"""What homegraf is given, and what the user is told."""
from __future__ import annotations


def homegraf_env(cfg, state) -> dict[str, str]:
    return {
        "KOMODO_URL": cfg.komodo_url,
        "KOMODO_API_KEY": state.get("komodo", "api_key"),
        "KOMODO_API_SECRET": state.get("komodo", "api_secret"),
        "FORGEJO_URL": cfg.forgejo_url,
        "FORGEJO_TOKEN": state.get("forgejo", "token"),
        "HOMEGRAF_CLONE_URL": cfg.clone_url,
        "INFISICAL_URL": cfg.infisical_url,
        "INFISICAL_BOOTSTRAP_CLIENT_ID": state.get("infisical", "client_id"),
        "INFISICAL_BOOTSTRAP_CLIENT_SECRET": state.get("infisical", "client_secret"),
        # Sign-in through this stack's Forgejo; its administrator is the first homegraf administrator.
        "OIDC_ISSUER": cfg.git_public_url,
        "OIDC_CLIENT_ID": state.get("forgejo", "oauth_client_id"),
        "OIDC_CLIENT_SECRET": state.get("forgejo", "oauth_client_secret"),
        "HOMEGRAF_URL": cfg.homegraf_callback_url.removesuffix("/api/auth/callback"),
        "OIDC_LABEL": "Forgejo",
        "OIDC_ADMINS": cfg.forgejo_user,
    }


def summary(cfg, state, notes: list[str]) -> str:
    p, ip, d = cfg.ports, cfg.host_ip, cfg.domain
    lines = ["", "=" * 72, "homegraf stack: ready", "=" * 72, "", "Open (by name, once the names reach this host; by port right away):"]
    ports = {"git": p["FORGEJO"], "komodo": p["KOMODO"], "secrets": p["INFISICAL"], "proxy": p["NPM_ADMIN"], "homegraf": p["HOMEGRAF"],
             "status": p["KUMA"]}
    for sub, _, _, label in cfg.services:
        lines.append(f"  {label:22} http://{sub}.{d}    or http://{ip}:{ports[sub]}")
    lines += ["", "Sign in:",
              f"  homegraf, Komodo{', AdGuard Home' if cfg.dns_mode == 'adguard' else ''}   user {cfg.admin_user}, password ADMIN_PASSWORD from .env",
              f"  homegraf                         also the button \"Forgejo\": the Forgejo administrator {cfg.forgejo_user} is its first administrator",
              f"  Forgejo                          {cfg.admin_email} or the user name {cfg.forgejo_user} (it owns the repository), same password",
              f"  Nginx Proxy Manager              {cfg.admin_email}, same password",
              *([f"  Technitium DNS                   http://{ip}:{p['DNS_ADMIN']}, user admin (not the e-mail address), same password; the bootstrap made an API token for the tools"]
                if cfg.dns_mode == "technitium" else []),
              f"  Infisical                        {cfg.admin_email}, " + ("a generated password: docker compose run --rm bootstrap show infisical-password"
                                                                         if state.get("infisical", "admin_password") else "same password"),
              "", f"Your repository: http://git.{d}/{cfg.repository}  or  http://{ip}:{p['FORGEJO']}/{cfg.repository}",
              "homegraf reads it (Komodo keeps a clone current) and opens its pull requests there.",
              "homegraf has the Komodo key, the Forgejo token and the Infisical identity it needs (no token to type anywhere)."]
    if notes:
        lines += ["", "Still to do by hand:"] + [f"  - {n}" for n in notes]
    lines.append("")
    return "\n".join(lines)


def next_steps(cfg) -> list[str]:
    notes = []
    if cfg.dns_mode == "external":
        notes.append(f"names: create the wildcard entry *.{cfg.domain} -> {cfg.host_ip} in your DNS")
    elif cfg.dns_mode in ("technitium", "adguard"):
        notes.append(f"names: point your router's DHCP (or each device) at {cfg.host_ip} as DNS server; Android wants a second DNS server")
    else:
        notes.append("names: nothing, sslip.io answers (your devices need internet DNS)")
    notes.append("certificates: the service certificates sets up the private CA next (docker compose logs certificates); it says what to do with the root certificate")
    return notes


def certificates_summary(cfg, ok: bool) -> str:
    if not ok:
        return "\ncertificates: not set up (see the warning above); the proxy hosts stay plain HTTP\n"
    url = f"http://git.{cfg.domain}/{cfg.repository}/raw/branch/main/inventory/homegraf-root-ca.pem"
    return "\n".join(["", "=" * 72, "certificates: every proxy host of the stack has one from your own CA", "=" * 72, "",
                      "Browsers trust them once the root certificate is installed on your devices:", f"  {url}",
                      "Then set force_https: true in external/ingress.yaml (and in the apps' ingress.yaml) so http turns into https.", ""])
