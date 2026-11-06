#!/usr/bin/env bash
# A new app is a folder, end to end: the stack runs as the first host, a container with its own Docker daemon (docker:dind) plays a
# second host, and a push of stacks/<id>/compose.yaml + ingress.yaml (nothing else) must give each app its secret, its Komodo stack,
# its deploy on the right host, a proxy host and a certificate of the stack's CA. Once for an app on the first host, once on the second.
#
#   scripts/e2e-folder.sh      needs docker with compose (and the internet: both daemons pull images)
# The stack's ports are bound to the gateway of its network, so only containers and this machine reach them. The temporary data
# directory (root-owned files from the agent) stays in $TMPDIR.
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
cd "$here"
P=hgfold
step() { printf '\n== %s\n' "$*"; }
ok()   { printf '   ok  %s\n' "$*"; }
die()  { printf '   FAIL %s\n' "$*" >&2; exit 1; }

step "images"
docker build -q -f bootstrap/Dockerfile -t "$P-bootstrap:local" . >/dev/null
HOMEGRAF_IMAGE=${HOMEGRAF_IMAGE:-ghcr.io/belphegorprime/homegraf:main}
ok "bootstrap ($HOMEGRAF_IMAGE for homegraf)"

data=$(mktemp -d "${TMPDIR:-/tmp}/$P.XXXXXX")
work=$(mktemp -d "${TMPDIR:-/tmp}/$P-work.XXXXXX")
env_file=$(mktemp)
pid=
down() { [ -z "$pid" ] || kill "$pid" 2>/dev/null || true
         docker rm -f "$P-hostb" >/dev/null 2>&1 || true
         # the apps and ingressctl are Komodo stacks: their containers and networks are not in this project
         docker ps -aq --filter "label=com.docker.compose.project=$P" | xargs -r docker rm -f >/dev/null 2>&1 || true
         docker compose -p "$P" --env-file "$env_file" down -v >/dev/null 2>&1 || true
         docker ps -aq --filter "name=^fldone-" | xargs -r docker rm -f >/dev/null 2>&1 || true
         docker ps -aq --filter "name=^ingressctl-" | xargs -r docker rm -f >/dev/null 2>&1 || true
         docker volume rm "$P-ingressctl-state" "$P-ingressctl-credentials" >/dev/null 2>&1 || true
         docker network rm "$P-net" fldone_default >/dev/null 2>&1 || true
         rm -rf "$env_file" "$work" "$here/worker-1.periphery.yaml"; }
trap down EXIT

step "the first host"
[ -f .env ] && die "there is a .env here; this check would replace it, move it away first"
docker network create --label "com.docker.compose.project=$P" --label com.docker.compose.network=default "$P-net" >/dev/null
gw=$(docker network inspect "$P-net" -f '{{(index .IPAM.Config 0).Gateway}}')
HOMEGRAF_IMAGE=$HOMEGRAF_IMAGE BOOTSTRAP_IMAGE=$P-bootstrap:local PROJECT_PREFIX=$P BIND_ADDRESS=$gw PORT_HTTP=38180 PORT_HTTPS=38543 \
  PORT_NPM_ADMIN=38181 PORT_FORGEJO=33100 PORT_FORGEJO_SSH=32322 PORT_KOMODO=39220 PORT_INFISICAL=38179 PORT_HOMEGRAF=38197 PORT_DNS=35453 \
  PORT_DNS_SETUP=33400 PORT_DNS_ADMIN=38153 DNS_MODE=external DOMAIN=folder.test HOST_IP=$gw ADMIN_EMAIL=e2e@folder.test ./setup.sh --yes >/dev/null
mv .env "$env_file"
sed -i "s|^DATA_DIR=.*|DATA_DIR=$data|" "$env_file"
docker compose -p "$P" --env-file "$env_file" up -d --wait >/dev/null 2>&1
# `up --wait` does not wait for the one-shot certificates service: it is the one that makes the provisioner and the Reconcile stage
for _ in $(seq 1 180); do [ "$(docker inspect -f '{{.State.Status}}' "$P-certificates" 2>/dev/null)" = exited ] && break; sleep 5; done
[ "$(docker inspect -f '{{.State.ExitCode}}' "$P-certificates")" = 0 ] || die "the certificates step did not finish"
docker logs "$P-certificates" 2>&1 | grep -q "provisioner.env: committed" || die "the bootstrap did not commit infra/secrets/provisioner.env"
docker logs "$P-certificates" 2>&1 | grep -q "sets up new apps" || die "update-all has no Reconcile stage"
ok "stack up at $gw, provisioner.env committed, update-all reconciles"

step "the second host: a container with its own Docker"
docker run -d --privileged --name "$P-hostb" --network "$P-net" -e DOCKER_TLS_CERTDIR= docker:dind >/dev/null
for _ in $(seq 1 60); do docker exec "$P-hostb" docker info >/dev/null 2>&1 && break; sleep 2; done
addr=$(docker inspect "$P-hostb" -f "{{(index .NetworkSettings.Networks \"$P-net\").IPAddress}}")
log=$(mktemp)
COMPOSE="docker compose -p $P --env-file $env_file" scripts/add-host.sh worker-1 "$addr" >"$log" 2>&1 &
pid=$!
for _ in $(seq 1 90); do [ -s worker-1.periphery.yaml ] && break; sleep 1; done
[ -s worker-1.periphery.yaml ] || { cat "$log" >&2; die "add-host.sh wrote no compose file for the agent"; }
sleep 2
docker cp worker-1.periphery.yaml "$P-hostb:/worker-1.periphery.yaml"
docker exec "$P-hostb" docker compose -f /worker-1.periphery.yaml up -d >/dev/null 2>&1 || die "the agent did not start on the second host"
wait "$pid" || { cat "$log" >&2; die "add-host.sh failed"; }
pid=
grep -q "server worker-1: project stacks-worker-1" "$log" || die "add-host did not set up the secrets of the new server"
rm -f "$log" worker-1.periphery.yaml
ok "worker-1 registered at $addr, its Infisical project exists"

password=$(grep '^ADMIN_PASSWORD=' "$env_file" | cut -d= -f2-)
git clone -q "http://owner:${password}@$gw:33100/owner/infrastructure.git" "$work/repo"
cd "$work/repo"
git config user.name e2e; git config user.email e2e@folder.test

# one folder per app: compose.yaml and ingress.yaml, nothing else
app() {  # name, server, port
  mkdir -p "stacks/$1"
  cat > "stacks/$1/compose.yaml" <<EOF
services:
  $1:
    image: traefik/whoami:v1.11.0
    restart: unless-stopped
    ports:
      - $3:80
    environment:
      - TOKEN=\${TOKEN}
EOF
  cat > "stacks/$1/ingress.yaml" <<EOF
apiVersion: homelab.ingress/v1
namespace: infrastructure
services:
- id: $1
  stack: $1
  lifecycle: present
  proxy:
    domains: [$1.folder.test]
    upstream: {scheme: http, server: $2, port: $3}
    websockets: false
    force_https: false
  certificate: {issuer_ref: homelab-private-ca, validity_days: 90, renew_before_days: 30}
  healthcheck: {path: /, accepted_statuses: [200]}
EOF
}
server=$(sed -n 's/^  "\([^"]*\)": *{ *address: "'"$gw"'".*/\1/p' inventory/servers.yaml | head -1)
[ -n "$server" ] || die "inventory/servers.yaml does not list the first host ($gw)"
app fldone "$server" 18201
app fldtwo worker-1 18202
git add stacks && git commit -q -m "add fldone and fldtwo" && git push -q origin main
ok "pushed stacks/fldone ($server) and stacks/fldtwo (worker-1)"

step "the apps answer over https with a certificate of the stack's CA"
ca="$work/repo/inventory/homegraf-root-ca.pem"
answers() { curl -s --cacert "$ca" --resolve "$1.folder.test:38543:$gw" -o /dev/null -w '%{http_code}' "https://$1.folder.test:38543/" 2>/dev/null; }
for name in fldone fldtwo; do
  for _ in $(seq 1 90); do [ "$(answers $name)" = 200 ] && break; sleep 10; done
  [ "$(answers $name)" = 200 ] || die "https://$name.folder.test does not answer 200 (docker compose -p $P logs; Komodo has the update)"
  ok "https://$name.folder.test: 200, verified"
done

step "where they run"
docker exec "$P-hostb" docker ps --format '{{.Names}}' | grep -q '^fldtwo-' || die "fldtwo does not run on worker-1"
docker ps --format '{{.Names}}' | grep -q '^fldtwo-' && die "fldtwo runs on the first host too"
docker ps --format '{{.Names}}' | grep -q '^fldone-' || die "fldone does not run on the first host"
for c in "docker exec $P-hostb docker inspect fldtwo-fldtwo-1" "docker inspect fldone-fldone-1"; do
  $c -f '{{range .Config.Env}}{{println .}}{{end}}' | grep -q '^TOKEN=.\{40,\}' || die "an app got no generated secret ($c)"
done
ok "each app runs on its own host with a generated secret"
