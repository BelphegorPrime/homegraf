#!/bin/sh
# Komodo compose_cmd_wrapper: starts a compose command with the secrets of a stack from Infisical as
# environment variables (for ${VAR} in compose.yaml). The values never reach the disk.
# Runs in Periphery (docker CLI + socket, working directory = the stack's run directory).
# Set up by infra/infisical/stack_secrets.py; the wrapper in the stack looks like:
#
#   INFISICAL_API_URL=[[INFISICAL_API_URL]] ... sh <clone>/infra/infisical/compose-wrap.sh /<stack-id> -- [[COMPOSE_COMMAND]]
#
# Environment: INFISICAL_API_URL, INFISICAL_PROJECT_ID, INFISICAL_UNIVERSAL_AUTH_CLIENT_ID,
#              INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET (Komodo variables, the server's identity)
set -eu

# renovate: datasource=docker depName=infisical/cli
CLI_VERSION=0.44.11
CLI_DIR="${INFISICAL_CLI_DIR:-/tmp/infisical-cli}"
ENVIRONMENT="${INFISICAL_ENVIRONMENT:-prod}"

die() { echo "compose-wrap: $*" >&2; exit 1; }

# Child of "infisical run" for "docker compose … config": Komodo stores this output in the update log and as
# merged_config. Every variable that infisical run brought newly into the environment gets the placeholder
# <infisical:KEY>; that way it sits exactly where compose resolves ${KEY}. Replacing text in the output would also
# hit container and service names if a value happened to be named like that.
if [ "${1:-}" = "--mask-config" ]; then
  shift
  for k in $(env | sed -n 's/^\([A-Za-z_][A-Za-z0-9_]*\)=.*/\1/p'); do
    case " ${CW_ENV_BEFORE:-} " in *" $k "*) continue ;; esac
    case "$k" in INFISICAL_*|CW_ENV_BEFORE) continue ;; esac
    export "$k=<infisical:$k>"
  done
  unset CW_ENV_BEFORE
  exec "$@"
fi

[ $# -ge 3 ] && [ "$2" = "--" ] || die "usage: compose-wrap.sh <secret-path> -- <command...>"
SECRET_PATH="$1"
shift 2
for v in INFISICAL_API_URL INFISICAL_PROJECT_ID INFISICAL_UNIVERSAL_AUTH_CLIENT_ID INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET; do
  eval "[ -n \"\${$v:-}\" ]" || die "$v is missing (Komodo variable created? infra/infisical/stack_secrets.py)"
done

# Static binary from the official image, once per version and Periphery container.
# Not inside the Periphery image: the core updater renews Periphery with --no-deps, an init container would never run with it.
BIN="$CLI_DIR/$CLI_VERSION/infisical"
if [ ! -x "$BIN" ]; then
  mkdir -p "$CLI_DIR/$CLI_VERSION"
  cid="$(docker create --quiet "infisical/cli:$CLI_VERSION")" || die "image infisical/cli:$CLI_VERSION not available"
  rc=0
  docker cp "$cid:/bin/infisical" "$BIN.$$" >/dev/null || rc=$?
  docker rm "$cid" >/dev/null || true
  [ $rc -eq 0 ] || die "cannot copy the CLI out of infisical/cli:$CLI_VERSION"
  chmod 755 "$BIN.$$" && mv -f "$BIN.$$" "$BIN"  # atomic: parallel deploys on the same server
fi

# Without a token "infisical run" would wait for an interactive login: the login has to succeed first.
INFISICAL_TOKEN="$("$BIN" login --method=universal-auth --silent --plain </dev/null)" || die "login at $INFISICAL_API_URL failed"
[ -n "$INFISICAL_TOKEN" ] || die "login at $INFISICAL_API_URL returned no token"
export INFISICAL_TOKEN
unset INFISICAL_UNIVERSAL_AUTH_CLIENT_ID INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET
for last in "$@"; do :; done
if [ "$last" = config ]; then
  CW_ENV_BEFORE="$(env | sed -n 's/^\([A-Za-z_][A-Za-z0-9_]*\)=.*/\1/p' | tr '\n' ' ')"
  export CW_ENV_BEFORE
  set -- sh "$0" --mask-config "$@"
fi
exec "$BIN" run --projectId="$INFISICAL_PROJECT_ID" --env="$ENVIRONMENT" --path="$SECRET_PATH" --silent -- "$@"
