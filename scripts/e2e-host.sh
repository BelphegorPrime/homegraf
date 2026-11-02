#!/usr/bin/env bash
# A second host, end to end: the stack runs as the first host; a container with its own Docker daemon (docker:dind) plays the second.
# scripts/add-host.sh registers it, an app is created on it through homegraf's API, merged, answers, and is removed again.
#
#   scripts/e2e-host.sh        needs docker with compose (and the internet: the second daemon pulls its images)
# The stack's ports are bound to the gateway of its network, so only containers and this machine reach them (the second host
# clones from there). The temporary data directory (root-owned files from the agent) stays in $TMPDIR.
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
cd "$here"
P=hgh2e
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
pid=
down() { [ -z "$pid" ] || kill "$pid" 2>/dev/null || true
         docker rm -f "$P-hostb" >/dev/null 2>&1 || true
         # what `compose run` left (add-host waits for ten minutes for an agent that never came)
         docker ps -aq --filter "label=com.docker.compose.project=$P" | xargs -r docker rm -f >/dev/null 2>&1 || true
         docker compose -p "$P" --env-file "$env_file" down -v >/dev/null 2>&1 || true
         docker rm -f ingressctl-ingressctl-1 >/dev/null 2>&1 || true
         docker volume rm "$P-ingressctl-state" "$P-ingressctl-credentials" >/dev/null 2>&1 || true
         docker network rm "$P-net" >/dev/null 2>&1 || true
         rm -f "$env_file" "$here/worker-1.periphery.yaml"; }
trap down EXIT

step "the first host"
[ -f .env ] && die "there is a .env here; this check would replace it, move it away first"
# the network is made here (as compose would) to know its gateway: the address the second host reaches the first at
docker network create --label "com.docker.compose.project=$P" --label com.docker.compose.network=default "$P-net" >/dev/null
gw=$(docker network inspect "$P-net" -f '{{(index .IPAM.Config 0).Gateway}}')
HOMEGRAF_IMAGE=$HOMEGRAF_IMAGE BOOTSTRAP_IMAGE=$P-bootstrap:local PROJECT_PREFIX=$P BIND_ADDRESS=$gw PORT_HTTP=28180 PORT_HTTPS=28543 \
  PORT_NPM_ADMIN=28181 PORT_FORGEJO=23100 PORT_FORGEJO_SSH=22322 PORT_KOMODO=29220 PORT_INFISICAL=28179 PORT_HOMEGRAF=28197 PORT_DNS=25453 \
  PORT_DNS_SETUP=23400 PORT_DNS_ADMIN=28153 DNS_MODE=sslip HOST_IP=$gw ./setup.sh --yes >/dev/null
mv .env "$env_file"
sed -i "s|^DATA_DIR=.*|DATA_DIR=$data|" "$env_file"
docker compose -p "$P" --env-file "$env_file" up -d --wait >/dev/null
ok "every service healthy, bootstrap finished (first host at $gw)"

step "the second host: a container with its own Docker"
docker run -d --privileged --name "$P-hostb" --network "$P-net" -e DOCKER_TLS_CERTDIR= docker:dind >/dev/null
for _ in $(seq 1 60); do docker exec "$P-hostb" docker info >/dev/null 2>&1 && break; sleep 2; done
docker exec "$P-hostb" docker compose version >/dev/null 2>&1 || die "the second host has no docker compose"
addr=$(docker inspect "$P-hostb" -f "{{(index .NetworkSettings.Networks \"$P-net\").IPAddress}}")
ok "ready at $addr"

step "add-host.sh"
log=$(mktemp)
COMPOSE="docker compose -p $P --env-file $env_file" scripts/add-host.sh worker-1 "$addr" >"$log" 2>&1 &
pid=$!
for _ in $(seq 1 60); do [ -s worker-1.periphery.yaml ] && break; sleep 1; done
[ -s worker-1.periphery.yaml ] || { cat "$log" >&2; die "add-host.sh wrote no compose file for the agent"; }
[ "$(stat -c %a worker-1.periphery.yaml)" = 600 ] || die "the agent's compose file (it holds the passkey) is not private"
grep -q "^Written: worker-1.periphery.yaml" "$log" || sleep 2
docker cp worker-1.periphery.yaml "$P-hostb:/worker-1.periphery.yaml"
docker exec "$P-hostb" docker compose -f /worker-1.periphery.yaml up -d >/dev/null 2>&1 || die "the agent did not start on the second host"
wait "$pid" || { cat "$log" >&2; die "add-host.sh failed"; }
rm -f "$log" worker-1.periphery.yaml
ok "the host is registered: server, clone, update-all, inventory/servers.yaml"

step "an app on the second host"
password=$(grep '^ADMIN_PASSWORD=' "$env_file" | cut -d= -f2-)
HG_URL=http://$gw:28197 FORGEJO_URL=http://$gw:23100 HG_PASSWORD=$password HG_USER=$(grep '^ADMIN_EMAIL=' "$env_file" | cut -d= -f2-) \
  HOST_NAME=worker-1 HOST_ADDRESS=$addr HOST_DOCKER=$P-hostb python3 scripts/e2e_host.py
ok "created on the second host, merged, running there, removed"
