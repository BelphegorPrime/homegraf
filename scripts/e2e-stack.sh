#!/usr/bin/env bash
# The whole life of an installation on this machine, as an end-to-end check of the stack: builds the images of this checkout,
# starts the stack from nothing (own project name, high ports, loopback only, a fresh data directory), creates an app from homegraf's
# API, merges its pull request, sees the app answer, removes it in both halves, and takes everything down again.
#
#   scripts/e2e-stack.sh                   needs docker with compose; uses HOMEGRAF_IMAGE (default: builds ../ is not assumed, see below)
#   HOMEGRAF_IMAGE=ghcr.io/belphegorprime/homegraf:main scripts/e2e-stack.sh      use a published homegraf image instead of building app/
# The temporary data directory (root-owned files from the Komodo agent) stays in $TMPDIR; delete it with sudo when you like.
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
cd "$here"
P=hge2e
step() { printf '\n== %s\n' "$*"; }
ok()   { printf '   ok  %s\n' "$*"; }
die()  { printf '   FAIL %s\n' "$*" >&2; exit 1; }

step "images"
docker build -q -f bootstrap/Dockerfile -t "$P-bootstrap:local" . >/dev/null
HOMEGRAF_IMAGE=${HOMEGRAF_IMAGE:-}
if [ -z "$HOMEGRAF_IMAGE" ]; then docker build -q -t "$P-homegraf:local" app >/dev/null; HOMEGRAF_IMAGE=$P-homegraf:local; fi
ok "bootstrap and homegraf ($HOMEGRAF_IMAGE)"

data=$(mktemp -d "${TMPDIR:-/tmp}/$P.XXXXXX")
env_file=$(mktemp)
# ingressctl is a Komodo stack, so its leftovers are not in this project: the exited container and the volume of its state
down() { docker compose -p "$P" --env-file "$env_file" down -v >/dev/null 2>&1 || true; docker rm -f ingressctl-ingressctl-1 >/dev/null 2>&1 || true
         docker volume rm "$P-ingressctl-state" >/dev/null 2>&1 || true; rm -f "$env_file"; }
trap down EXIT

step "setup and start from nothing"
host_ip=$(hostname -I | awk '{print $1}')
# setup.sh writes .env into this directory; keep a possible existing one out of the way
[ -f .env ] && die "there is a .env here; this check would replace it, move it away first"
HOMEGRAF_IMAGE=$HOMEGRAF_IMAGE BOOTSTRAP_IMAGE=$P-bootstrap:local PROJECT_PREFIX=$P BIND_ADDRESS=127.0.0.1 PORT_HTTP=28080 PORT_HTTPS=28443 \
  PORT_NPM_ADMIN=28081 PORT_FORGEJO=23000 PORT_FORGEJO_SSH=22222 PORT_KOMODO=29120 PORT_INFISICAL=28079 PORT_HOMEGRAF=28097 PORT_DNS=25353 \
  PORT_DNS_SETUP=23300 PORT_DNS_ADMIN=28053 DNS_MODE=sslip HOST_IP=$host_ip ./setup.sh --yes >/dev/null
mv .env "$env_file"
sed -i "s|^DATA_DIR=.*|DATA_DIR=$data|" "$env_file"
password=$(grep '^ADMIN_PASSWORD=' "$env_file" | cut -d= -f2-)
docker compose -p "$P" --env-file "$env_file" up -d --wait >/dev/null
ok "every service healthy, bootstrap finished"

step "the e-mail address signs in everywhere"
email=$(grep '^ADMIN_EMAIL=' "$env_file" | cut -d= -f2-)
[ "$(grep '^ADMIN_USER=' "$env_file" | cut -d= -f2-)" = "$email" ] || die "ADMIN_USER is not the e-mail address"
owner=$(grep '^FORGEJO_ADMIN_USER=' "$env_file" | cut -d= -f2-)
code() { curl -s -o /dev/null -w '%{http_code}' "$@" || true; }
json() { printf '{"%s":"%s","%s":"%s"}' "$1" "$email" "$2" "$password"; }
[ "$(code -u "$email:$password" http://127.0.0.1:28097/api/users)" = 200 ] || die "homegraf refuses $email"
[ "$(code -u "x$email:$password" http://127.0.0.1:28097/api/users)" = 401 ] || die "homegraf accepts a wrong name"
ok "homegraf"
[ "$(code -H 'content-type: application/json' -d "$(json username password)" http://127.0.0.1:29120/auth/login/LoginLocalUser)" = 200 ] || die "Komodo refuses $email"
ok "Komodo"
[ "$(curl -fsS -u "$email:$password" http://127.0.0.1:23000/api/v1/user | python3 -c 'import sys,json;print(json.load(sys.stdin)["login"])')" = "$owner" ] || die "Forgejo refuses $email"
ok "Forgejo (the user $owner, signed in by e-mail)"
[ "$(code -H 'content-type: application/json' -d "$(json identity secret)" http://127.0.0.1:28081/api/tokens)" = 200 ] || die "Nginx Proxy Manager refuses $email"
ok "Nginx Proxy Manager"

step "single sign-on through the stack's own Forgejo"
# played in the stack's network, where git.<domain> and homegraf.<domain> answer on port 80 as they do for a browser
docker run --rm --network "$P-net" -e DOMAIN="$(grep '^DOMAIN=' "$env_file" | cut -d= -f2-)" -e FORGEJO_USER="$owner" -e ADMIN_EMAIL="$email" \
  -e ADMIN_PASSWORD="$password" -v "$here/scripts/e2e_sso.py:/e2e_sso.py:ro" --entrypoint python "$P-bootstrap:local" -I /e2e_sso.py || die "single sign-on"
ok "the administrator of Forgejo became the first administrator of homegraf"

step "certificates from your own CA"
# the service certificates starts after homegraf is healthy, so `up --wait` can return while it still works: wait for its end
[ "$(timeout 900 docker wait "$P-certificates")" = 0 ] || { docker logs "$P-certificates" 2>&1 | tail -5; die "the service certificates did not finish"; }
ok "the service certificates finished"
domain=$(grep '^DOMAIN=' "$env_file" | cut -d= -f2-); owner=$(grep '^FORGEJO_ADMIN_USER=' "$env_file" | cut -d= -f2-)
root_ca=$(mktemp)
curl -fsS -u "$owner:$password" "http://127.0.0.1:23000/api/v1/repos/$owner/infrastructure/raw/inventory/homegraf-root-ca.pem" -o "$root_ca"
grep -q 'BEGIN CERTIFICATE' "$root_ca" || die "the repository has no root certificate: the bootstrap did not set up the CA"
for sub in git komodo secrets proxy homegraf; do
  # --cacert is the verification: curl fails (exit 60) when the proxy does not present a certificate of this CA for the name;
  # the proxy reloads a moment after a change, so a few tries
  code=000
  for _ in $(seq 1 20); do
    code=$(curl -s -o /dev/null -w '%{http_code}' --cacert "$root_ca" --resolve "$sub.$domain:28443:127.0.0.1" "https://$sub.$domain:28443/" || true)
    [ "$code" != 000 ] && break
    sleep 3
  done
  case "$code" in 2*|3*|401) ok "$sub.$domain: https $code, certificate verified against the root CA";; *) die "$sub.$domain: no valid certificate from the CA (https answered $code)";; esac
done

export HG_URL=http://127.0.0.1:28097 FORGEJO_URL=http://127.0.0.1:23000 HG_PASSWORD=$password HG_USER=$email
step "the lifecycle through homegraf's API"
DOMAIN=$domain ROOT_CA=$root_ca HTTPS_PORT=28443 python3 scripts/e2e_lifecycle.py
rm -f "$root_ca"
ok "created, merged, running, removed, finished"
