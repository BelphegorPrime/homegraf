#!/usr/bin/env bash
# Takes this installation down completely, including what Komodo created outside the compose project: the stopped container and the
# volumes of ingressctl. `docker compose down -v` alone leaves them, and a new installation would then inherit the hosts and
# certificates of the old one. Your data directory (DATA_DIR in .env: clones, backups) is NOT removed; delete it yourself if you want
# a clean start.
#
#   scripts/reset.sh              asks first
#   scripts/reset.sh --yes        no question
#   ENV_FILE=path scripts/reset.sh    another env file than .env (COMPOSE_PROJECT_NAME is honoured as usual)
set -euo pipefail
cd "$(dirname "$0")/.."
env_file=${ENV_FILE:-.env}
[ -f "$env_file" ] || { echo "no $env_file here: nothing to take down" >&2; exit 1; }
if [ "${1:-}" != "--yes" ]; then
  read -r -p "This removes every container and volume of this installation (databases, repositories, secrets). Continue? [y/N] " answer
  case "$answer" in y | Y) ;; *) exit 1 ;; esac
fi
data_dir=$(grep '^DATA_DIR=' "$env_file" | cut -d= -f2-)
prefix=$(grep '^PROJECT_PREFIX=' "$env_file" | cut -d= -f2- || true); prefix=${prefix:-homegraf}

# the containers Komodo started for ingressctl: their compose working directory is under this installation's data directory
if [ -n "$data_dir" ]; then
  docker ps -aq --filter label=com.docker.compose.project=ingressctl | while read -r id; do
    case "$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}' "$id")" in
      "$data_dir"/*) docker rm -f "$id" >/dev/null && echo "removed container $id (ingressctl)" ;;
    esac
  done
fi
docker compose --env-file "$env_file" --profile '*' down -v --remove-orphans
for v in "$prefix-ingressctl-state" "$prefix-ingressctl-credentials"; do
  docker volume rm "$v" >/dev/null 2>&1 && echo "removed volume $v" || true
done
echo "Down. $data_dir is left as it is."
