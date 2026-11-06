#!/usr/bin/env bash
# Writes .env for the homegraf stack: how names reach this host, the administrator and random secrets for every service.
#
#   ./setup.sh                    asks a few questions (the host's address, how names reach it, the administrator)
#   DNS_MODE=adguard ADMIN_EMAIL=me@example.org ./setup.sh --yes   no questions, for scripts (HOST_IP, DOMAIN, ... from the environment)
#
# Names: the services are reached as <name>.<DOMAIN> (git, komodo, secrets, proxy, homegraf), and the proxy routes by that name, so
# the names must reach this host. DNS_MODE says how (the default is technitium):
#   technitium Technitium DNS Server runs in this stack (compose profile "technitium"): the bootstrap makes the zone, the wildcard to this
#              host and an API token for the tools; everything else is forwarded (DNS_FORWARDERS). Point your router's DHCP at it.
#   adguard    AdGuard Home runs in this stack (compose profile "dns") and answers for the domain; point your router's DHCP at it.
#   external   you already have DNS (router, Pi-hole, your own AdGuard): create the one wildcard entry this script prints.
#   sslip      zero configuration: DOMAIN becomes <host address with dashes>.sslip.io, a public service that answers with the
#              address inside the name. Good for trying it out (needs internet DNS on the clients); no private CA.
#
# Optional services are compose profiles: OPTIONAL_SERVICES="renovate backup status runner" (or answer the question) writes them into
# COMPOSE_PROFILES. renovate: dependency updates as pull requests (brings the Actions runner along); runner: Forgejo Actions runner
# (docker-in-docker); backup: daily dumps of the databases; status: Uptime Kuma.
#
# Refuses to overwrite an existing .env (your secrets would be gone and the databases would no longer open).
# Environment variables named like the knobs in .env.example (PORT_*, BIND_ADDRESS, PROJECT_PREFIX, HOMEGRAF_IMAGE, TZ, ...) are
# copied into .env. The only secret ever printed is the administrator password, and only when it was generated.
set -euo pipefail
cd "$(dirname "$0")"

if [ -e .env ]; then
  echo ".env exists; remove it first if you really want to start over (that also means: docker compose down -v)." >&2
  exit 1
fi

yes_mode=false
[ "${1:-}" = "--yes" ] && yes_mode=true
interactive=true
{ [ -t 0 ] && ! $yes_mode; } || interactive=false

hex() { od -An -tx1 -N"$1" /dev/urandom | tr -d ' \n'; }
b64() { head -c "$1" /dev/urandom | base64 | tr -d '\n'; }
alnum() { LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c "$1" || true; }
die() { echo "$*" >&2; exit 1; }

# The address this host has in the LAN, as far as it can be told: the source address of the route to the internet.
detect_ip() {
  local found
  found=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i <= NF; i++) if ($i == "src") { print $(i + 1); exit }}' || true)
  [ -n "$found" ] || found=$(hostname -I 2>/dev/null | awk '{print $1}' || true)
  printf '%s' "$found"
}

# Does something already listen on this port, on the address we would bind or on all addresses? (loopback listeners such as
# systemd-resolved's 127.0.0.53 do not count: we bind only to the LAN address.) Prints busy, free or unknown.
port_state() {
  command -v ss >/dev/null 2>&1 || { echo unknown; return; }
  local listeners
  listeners=$(ss -H -lntu "sport = :$2" 2>/dev/null | awk '{print $5}')
  if printf '%s\n' "$listeners" | grep -Eq "^(\*|0\.0\.0\.0|\[::\]|::):$2\$|^$1:$2\$"; then echo busy; else echo free; fi
}

domain=${DOMAIN:-}
email=${ADMIN_EMAIL:-}
user=${ADMIN_USER:-}
password=${ADMIN_PASSWORD:-}
host_ip=${HOST_IP:-}
dns_mode=${DNS_MODE:-}
optional=${OPTIONAL_SERVICES:-}
generated_password=false

[ -n "$host_ip" ] || host_ip=$(detect_ip)

if $interactive; then
  read -r -p "Address of this host in your LAN [${host_ip:-none detected}]: " answer
  host_ip=${answer:-$host_ip}
  if [ -z "$dns_mode" ]; then
    echo "How should names like git.<domain> reach this host?"
    echo "  1) technitium  Technitium DNS Server runs in this stack: zone, wildcard to this host and an API token for the tools (you point your router at it)"
    echo "  2) adguard     AdGuard Home runs in this stack and answers for your domain (you point your router at it)"
    echo "  3) external    you already have DNS and add one wildcard entry"
    echo "  4) sslip       zero configuration, for trying it out (names like git.${host_ip//./-}.sslip.io)"
    read -r -p "Choice [1]: " answer
    case "${answer:-1}" in 1) dns_mode=technitium ;; 2) dns_mode=adguard ;; 3) dns_mode=external ;; 4) dns_mode=sslip ;; *) die "choose 1, 2, 3 or 4" ;; esac
  fi
  if [ "$dns_mode" != sslip ] && [ -z "$domain" ]; then
    read -r -p "Domain, as <name>.<domain> [homegraf.lan]: " domain
  fi
  [ -n "$email" ] || read -r -p "Administrator e-mail: " email
  if [ -z "$password" ]; then
    read -r -s -p "Administrator password (14 characters or more with a letter and a digit; Enter generates one): " password
    echo
  fi
fi

dns_mode=${dns_mode:-technitium}
case "$dns_mode" in technitium | adguard | external | sslip) ;; *) die "DNS_MODE is technitium, adguard, external or sslip, not '$dns_mode'" ;; esac
[ -n "$host_ip" ] || die "the address of this host in your LAN is needed: set HOST_IP (the wildcard and the proxy hosts use it)"
printf '%s' "$host_ip" | grep -Eq '^[0-9]{1,3}(\.[0-9]{1,3}){3}$' || die "HOST_IP is an IPv4 address like 192.168.1.10, not '$host_ip'"
if [ "$dns_mode" = sslip ]; then
  domain=${host_ip//./-}.sslip.io
fi
domain=${domain:-homegraf.lan}
[ -n "$email" ] || email="admin@$domain"
# The e-mail address is the sign-in name everywhere it can be (ADMIN_USER overrides it).
[ -n "$user" ] || user=$email
if [ -z "$password" ]; then
  until printf '%s' "$password" | grep -q '[A-Za-z]' && printf '%s' "$password" | grep -q '[0-9]'; do password=$(alnum 24); done
  generated_password=true
fi
# One password for every service: Infisical's policy is the strictest (14 characters, a letter, a digit or a special character).
[ "${#password}" -ge 14 ] || die "the password needs at least 14 characters"
printf '%s' "$password" | grep -q '[A-Za-z]' && printf '%s' "$password" | grep -q '[0-9!@#%^&*(),.?:{}|<>]' || die "the password needs a letter and a digit (or special character)"
# Forgejo takes the e-mail address to sign in too, but its user name (the owner of the repository) must be a plain name:
# no "@", and not "admin" nor a few other reserved ones. It is called owner then.
forgejo_user=$user
case "$user" in admin|*@*) forgejo_user=owner ;; esac
case "$password$domain$email$user" in
  *[\"\'\\\$\`\ ]*) die "no spaces, quotes, backslashes, \$ or backticks in the domain, e-mail, user or password (they go into .env and URLs)" ;;
esac

# The DNS server answers on the LAN address only, so that systemd-resolved (127.0.0.53:53) and a router on the same host do not collide.
dns_port=${PORT_DNS:-53}
if [ "$dns_mode" = adguard ] || [ "$dns_mode" = technitium ]; then
  dns_name=AdGuard; [ "$dns_mode" = technitium ] && dns_name=Technitium
  state=$(port_state "$host_ip" "$dns_port")
  if [ "$state" = busy ]; then
    echo "Port $dns_port on $host_ip is already in use (see: ss -lntup 'sport = :$dns_port')." >&2
    echo "Clients can only use a DNS server on port 53, so free it, or set DNS_MODE=external and use the DNS that holds it." >&2
    echo "If it is dnsmasq or similar, stop it; systemd-resolved alone is no problem, it listens on 127.0.0.53 only." >&2
    $yes_mode && exit 1
    read -r -p "Continue anyway with port $dns_port ($dns_name will then not start until it is free)? [y/N] " answer
    case "$answer" in y | Y) ;; *) exit 1 ;; esac
  fi
fi

if $interactive && [ -z "$optional" ]; then
  echo
  echo "Optional services (compose profiles), several separated by spaces, Enter for none:"
  echo "  renovate  dependency updates for your repository as pull requests (needs and starts the Actions runner)"
  echo "  runner    a Forgejo Actions runner (jobs run in their own docker-in-docker daemon, which needs --privileged)"
  echo "  backup    a dump of every database once a day, seven days kept, in data/backups"
  echo "  status    Uptime Kuma at status.$domain"
  read -r -p "Optional services: " optional
fi
profiles=""
[ "$dns_mode" != adguard ] || profiles="dns"
[ "$dns_mode" != technitium ] || profiles="technitium"
for o in $optional; do
  case "$o" in renovate | runner | backup | status) ;; *) die "unknown optional service '$o' (renovate, runner, backup, status)" ;; esac
  case ",$profiles," in *,"$o",*) ;; *) profiles="${profiles:+$profiles,}$o" ;; esac
done

umask 077
mkdir -p data/periphery data/backups
{
  echo "# Written by setup.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ). Secrets: keep this file private, do not commit it."
  echo "HOST_IP=$host_ip"
  echo "DNS_MODE=$dns_mode"
  echo "DOMAIN=$domain"
  echo "ADMIN_USER=$user"
  echo "ADMIN_EMAIL=$email"
  echo "ADMIN_PASSWORD=$password"
  echo "FORGEJO_ADMIN_USER=$forgejo_user"
  echo "DATA_DIR=$PWD/data"
  [ -z "$profiles" ] || echo "COMPOSE_PROFILES=$profiles"
  [ "$dns_mode" != adguard ] && [ "$dns_mode" != technitium ] || echo "DNS_BIND=$host_ip"
  echo
  echo "FORGEJO_DB_PASSWORD=$(alnum 32)"
  echo "FORGEJO_SECRET_KEY=$(hex 32)"
  echo "KOMODO_DB_PASSWORD=$(alnum 32)"
  echo "KOMODO_PASSKEY=$(alnum 40)"
  echo "KOMODO_JWT_SECRET=$(alnum 40)"
  echo "KOMODO_WEBHOOK_SECRET=$(alnum 40)"
  echo "INFISICAL_DB_PASSWORD=$(alnum 32)"
  echo "INFISICAL_ENCRYPTION_KEY=$(hex 16)"
  echo "INFISICAL_AUTH_SECRET=$(b64 32)"
  echo "RUNNER_SECRET=$(hex 20)"
  echo
  for knob in TZ BIND_ADDRESS PROJECT_PREFIX HOMEGRAF_IMAGE PORT_HTTP PORT_HTTPS PORT_NPM_ADMIN PORT_FORGEJO PORT_FORGEJO_SSH \
              PORT_KOMODO PORT_INFISICAL PORT_HOMEGRAF PORT_DNS PORT_DNS_SETUP PORT_DNS_ADMIN DNS_FORWARDERS REPO_DIR BOOTSTRAP_IMAGE \
              UNIFI_URL UNIFI_USER UNIFI_PASSWORD UNIFI_FINGERPRINT; do
    [ -z "${!knob:-}" ] || echo "$knob=${!knob}"
  done
} > .env

echo "Wrote .env (mode 600). Names: $dns_mode, domain $domain, host $host_ip."
if $generated_password; then
  echo "Administrator: $user / $password   (note it now; it is the sign-in of homegraf, Komodo and the rest, and stays in .env)"
fi
case "$dns_mode" in
  sslip)
    echo "Names like git.$domain reach $host_ip through the public sslip.io service: nothing to set up (the clients need internet DNS)."
    ;;
  technitium)
    echo "DNS: Technitium DNS Server starts with the stack and the bootstrap sets it up: the zone $domain, the record  *.$domain -> $host_ip"
    echo "     and an API token for the tools. Then point your router's DHCP (or each device) at $host_ip."
    echo "     Its console: http://$host_ip:${PORT_DNS_ADMIN:-8053}, user admin (not the e-mail address), the administrator password."
    echo "     Android's private DNS wants two DNS servers: give it a second address that also reaches Technitium."
    ;;
  adguard)
    echo "DNS: AdGuard Home starts with the stack and the bootstrap sets it up, including the rewrite  *.$domain -> $host_ip."
    echo "     Then point your router's DHCP (or each device) at $host_ip."
    echo "     Android's private DNS wants two DNS servers: give it a second address that also reaches AdGuard."
    ;;
  external)
    echo "DNS: create this one wildcard entry in your DNS (router, Pi-hole, AdGuard, dnsmasq: address=/$domain/$host_ip):"
    echo "     *.$domain  ->  $host_ip"
    ;;
esac
echo "Next: docker compose up -d --wait     then see docs/stack.md"
