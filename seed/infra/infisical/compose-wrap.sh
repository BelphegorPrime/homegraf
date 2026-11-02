#!/bin/sh
# Komodo compose_cmd_wrapper: startet einen Compose-Befehl mit den Secrets eines Stacks aus Infisical als
# Umgebungsvariablen (für ${VAR} in compose.yaml). Die Werte landen nicht auf der Platte.
# Läuft in Periphery (docker CLI + Socket, Arbeitsverzeichnis = Run-Verzeichnis des Stacks).
# Eingerichtet von infra/infisical/stack_secrets.py, Wrapper im Stack etwa:
#
#   INFISICAL_API_URL=[[INFISICAL_API_URL]] ... sh <klon>/infra/infisical/compose-wrap.sh /<stack-id> -- [[COMPOSE_COMMAND]]
#
# Umgebung: INFISICAL_API_URL, INFISICAL_PROJECT_ID, INFISICAL_UNIVERSAL_AUTH_CLIENT_ID,
#           INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET (Komodo-Variablen, Identity des Servers)
set -eu

# renovate: datasource=docker depName=infisical/cli
CLI_VERSION=0.44.11
CLI_DIR="${INFISICAL_CLI_DIR:-/tmp/infisical-cli}"
ENVIRONMENT="${INFISICAL_ENVIRONMENT:-prod}"

die() { echo "compose-wrap: $*" >&2; exit 1; }

# Kind von "infisical run" bei "docker compose … config": Komodo speichert diese Ausgabe im Update-Log und als
# merged_config. Jede Variable, die infisical run neu in die Umgebung gebracht hat, bekommt den Platzhalter
# <infisical:KEY>; so steht er genau dort, wo Compose ${KEY} auflöst. Textersetzung in der Ausgabe träfe auch
# Container- und Servicenamen, wenn ein Wert zufällig so heißt.
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

[ $# -ge 3 ] && [ "$2" = "--" ] || die "Aufruf: compose-wrap.sh <secret-pfad> -- <befehl...>"
SECRET_PATH="$1"
shift 2
for v in INFISICAL_API_URL INFISICAL_PROJECT_ID INFISICAL_UNIVERSAL_AUTH_CLIENT_ID INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET; do
  eval "[ -n \"\${$v:-}\" ]" || die "$v fehlt (Komodo-Variable angelegt? infra/infisical/stack_secrets.py)"
done

# Statische Binary aus dem offiziellen Image, einmal pro Version und Periphery-Container.
# Nicht im Periphery-Image: der Core-Updater erneuert Periphery mit --no-deps, ein Init-Container liefe nie mit.
BIN="$CLI_DIR/$CLI_VERSION/infisical"
if [ ! -x "$BIN" ]; then
  mkdir -p "$CLI_DIR/$CLI_VERSION"
  cid="$(docker create --quiet "infisical/cli:$CLI_VERSION")" || die "Image infisical/cli:$CLI_VERSION nicht verfügbar"
  rc=0
  docker cp "$cid:/bin/infisical" "$BIN.$$" >/dev/null || rc=$?
  docker rm "$cid" >/dev/null || true
  [ $rc -eq 0 ] || die "CLI nicht aus infisical/cli:$CLI_VERSION kopierbar"
  chmod 755 "$BIN.$$" && mv -f "$BIN.$$" "$BIN"  # atomar: parallele Deploys auf demselben Server
fi

# Ohne Token würde "infisical run" auf einen interaktiven Login warten: Login muss vorher gelingen.
INFISICAL_TOKEN="$("$BIN" login --method=universal-auth --silent --plain </dev/null)" || die "Login bei $INFISICAL_API_URL fehlgeschlagen"
[ -n "$INFISICAL_TOKEN" ] || die "Login bei $INFISICAL_API_URL lieferte kein Token"
export INFISICAL_TOKEN
unset INFISICAL_UNIVERSAL_AUTH_CLIENT_ID INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET
for last in "$@"; do :; done
if [ "$last" = config ]; then
  CW_ENV_BEFORE="$(env | sed -n 's/^\([A-Za-z_][A-Za-z0-9_]*\)=.*/\1/p' | tr '\n' ' ')"
  export CW_ENV_BEFORE
  set -- sh "$0" --mask-config "$@"
fi
exec "$BIN" run --projectId="$INFISICAL_PROJECT_ID" --env="$ENVIRONMENT" --path="$SECRET_PATH" --silent -- "$@"
