"""The bootstrap's settings, all from the environment the compose file hands in."""
from __future__ import annotations

from dataclasses import dataclass, field

# Forgejo refuses these as user names ("admin" is the one people try).
FORGEJO_RESERVED = {"admin", "api", "assets", "attachments", "avatars", "explore", "favicon.ico", "ghost", "issues", "login",
                    "metrics", "milestones", "new", "notifications", "org", "pulls", "raw", "repo", "robots.txt", "settings",
                    "stars", "swagger.v1.json", "user", "v2"}
FORGEJO_FALLBACK_USER = "owner"
REPOSITORY = "infrastructure"
SERVICES = [("git", "forgejo", 3000, "Forgejo"), ("komodo", "komodo-core", 9120, "Komodo"), ("secrets", "infisical", 8080, "Infisical"),
            ("proxy", "proxy", 81, "Nginx Proxy Manager"), ("homegraf", "homegraf", 8080, "homegraf")]
# Services of the optional profiles, with a proxy host once the profile is on: (profile, name, container, port, label)
OPTIONAL_SERVICES = [("status", "status", "kuma", 3001, "Uptime Kuma")]


class ConfigError(Exception):
    pass


@dataclass
class Config:
    admin_user: str
    admin_email: str
    admin_password: str
    domain: str
    host_ip: str
    dns_mode: str
    data_dir: str
    webhook_secret: str
    state_dir: str = "/state"
    seed_dir: str = "/seed"
    forgejo_url: str = "http://forgejo:3000"
    komodo_url: str = "http://komodo-core:9120"
    npm_url: str = "http://proxy:81"
    npm_proxy_url: str = "http://proxy:80"
    infisical_url: str = "http://infisical:8080"
    adguard_setup_url: str = "http://dns:3000"
    adguard_url: str = "http://dns:80"
    ports: dict = field(default_factory=dict)
    bind_address: str = "0.0.0.0"
    profiles: tuple = ()                    # COMPOSE_PROFILES: which optional services are started
    network: str = "homegraf-net"          # the compose network every service of the stack is in (COMPOSE_NETWORK)
    credentials_volume: str = "homegraf-ingressctl-credentials"
    state_volume: str = "homegraf-ingressctl-state"
    credentials_dir: str = "/credentials"  # where the bootstrap mounts that volume
    komodo_passkey: str = ""          # KOMODO_PASSKEY: what an agent on another host needs to accept this stack's Komodo
    forgejo_admin: str = ""            # FORGEJO_ADMIN_USER, which setup.sh writes; the same name the forgejo-admin service creates

    @property
    def services(self) -> list[tuple[str, str, int, str]]:
        """The proxy hosts of this installation: the services of the stack and those of the profiles that are on."""
        return SERVICES + self.optional_services

    @property
    def optional_services(self) -> list[tuple[str, str, int, str]]:
        return [(sub, host, port, label) for profile, sub, host, port, label in OPTIONAL_SERVICES if profile in self.profiles]

    @property
    def forgejo_user(self) -> str:
        if self.forgejo_admin:
            return self.forgejo_admin
        usable = self.admin_user.lower() not in FORGEJO_RESERVED and "@" not in self.admin_user
        return self.admin_user if usable else FORGEJO_FALLBACK_USER

    @property
    def git_public_url(self) -> str:
        """Forgejo as a browser reaches it (its ROOT_URL): the issuer of the single sign-on."""
        return f"http://git.{self.domain}"

    @property
    def homegraf_callback_url(self) -> str:
        return f"http://homegraf.{self.domain}/api/auth/callback"

    @property
    def repository(self) -> str:
        return f"{self.forgejo_user}/{REPOSITORY}"

    @property
    def clone_url(self) -> str:
        return f"{self.forgejo_url}/{self.repository}.git"

    @property
    def webhook_url(self) -> str:
        return f"{self.komodo_url}/listener/github/procedure/update-all/main"

    @property
    def repos_dir(self) -> str:
        return f"{self.data_dir}/periphery/repos"

    @classmethod
    def from_env(cls, env) -> "Config":
        def need(name: str) -> str:
            value = (env.get(name) or "").strip()
            if not value:
                raise ConfigError(f"{name} is not set (run ./setup.sh, it writes .env)")
            return value

        cfg = cls(admin_user=env.get("ADMIN_USER") or "admin", admin_email=need("ADMIN_EMAIL"), admin_password=need("ADMIN_PASSWORD"),
                  domain=need("DOMAIN"), host_ip=need("HOST_IP"), dns_mode=env.get("DNS_MODE") or "external", data_dir=need("DATA_DIR"),
                  webhook_secret=need("KOMODO_WEBHOOK_SECRET"), state_dir=env.get("STATE_DIR") or "/state",
                  seed_dir=env.get("SEED_DIR") or "/seed", bind_address=env.get("BIND_ADDRESS") or "0.0.0.0",
                  forgejo_admin=(env.get("FORGEJO_ADMIN_USER") or "").strip(), komodo_passkey=(env.get("KOMODO_PASSKEY") or "").strip(),
                  profiles=tuple(p for p in (env.get("COMPOSE_PROFILES") or "").replace(" ", "").split(",") if p), network=env.get("COMPOSE_NETWORK") or "homegraf-net",
                  credentials_volume=env.get("CREDENTIALS_VOLUME") or "homegraf-ingressctl-credentials",
                  state_volume=env.get("STATE_VOLUME") or "homegraf-ingressctl-state", credentials_dir=env.get("CREDENTIALS_DIR") or "/credentials")
        for attr, name in (("forgejo_url", "FORGEJO_URL"), ("komodo_url", "KOMODO_URL"), ("npm_url", "NPM_URL"), ("npm_proxy_url", "NPM_PROXY_URL"),
                           ("infisical_url", "INFISICAL_URL"), ("adguard_setup_url", "ADGUARD_SETUP_URL"), ("adguard_url", "ADGUARD_URL")):
            if env.get(name):
                setattr(cfg, attr, env[name].rstrip("/"))
        cfg.ports = {k: env.get(f"PORT_{k}") or d for k, d in (("HTTP", "80"), ("NPM_ADMIN", "81"), ("FORGEJO", "3000"), ("KOMODO", "9120"),
                                                                 ("INFISICAL", "8079"), ("HOMEGRAF", "8097"), ("DNS_ADMIN", "8053"), ("KUMA", "3001"))}
        if cfg.dns_mode not in ("sslip", "adguard", "external"):
            raise ConfigError(f"DNS_MODE must be sslip, adguard or external, not {cfg.dns_mode!r}")
        return cfg
