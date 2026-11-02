#!/usr/bin/env bash
# Adds another Docker host to the stack: its Komodo agent (Periphery) is registered here, the host gets its own clone of your
# repository, and its address is listed in inventory/servers.yaml. Apps are placed on it by choosing the server NAME.
#
#   ./scripts/add-host.sh NAME ADDRESS      NAME: what the host is called (lower case, digits, hyphens); ADDRESS: its IP address or
#                                           host name, as this host reaches it
#
# It writes NAME.periphery.yaml (the compose file of the agent, with the passkey of this stack: mode 600) and says what to do with it
# on the new host: copy it there and start it. This host then waits up to ten minutes for the agent to answer on port 8120.
# Requirements: the new host has Docker with compose and the same path for DATA_DIR as here (the file says which), this host reaches
# its port 8120, and the new host reaches Forgejo at HOST_IP:PORT_FORGEJO (so BIND_ADDRESS is not 127.0.0.1).
# Run it from the directory of the stack. COMPOSE="docker compose -p NAME --env-file FILE" if you start the stack that way.
set -euo pipefail
cd "$(dirname "$0")/.."
[ $# -eq 2 ] || { sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2; }
name=$1 address=$2
compose=${COMPOSE:-docker compose}
file="$name.periphery.yaml"

( umask 077; $compose run --rm -T bootstrap agent-compose "$name" > "$file.part" ) || { rm -f "$file.part"; exit 1; }
mv "$file.part" "$file"
chmod 600 "$file"
cat <<MSG
Written: $file (it holds the passkey of this stack; keep it private)

On the new host ($address), with the same DATA_DIR path as here:
  scp $file $address:    (or any other way), then there:  docker compose -f $file up -d
Port 8120 of that host has to be reachable from here, from this host only if you can.

Waiting for the agent to answer (up to ten minutes) ...
MSG
exec $compose run --rm -T bootstrap add-host "$name" "$address"
