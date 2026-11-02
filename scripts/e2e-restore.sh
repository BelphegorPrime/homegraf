#!/usr/bin/env bash
# Proves that scripts/restore.sh puts the databases back: starts the three databases, Forgejo and Infisical (they run on their data)
# and the backup profile (own project name, a fresh data directory), writes a marker, lets the backup services dump it, changes the
# data, restores each database from its dump and looks at the marker. Also: Forgejo and Infisical are stopped for it, started again
# and healthy on the restored data; a damaged dump is refused and changes nothing; the state before a restore is kept.
#
#   scripts/e2e-restore.sh        needs docker with compose; takes a few minutes (the stack's own images)
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
cd "$here"
P=hgrst
step() { printf '\n== %s\n' "$*"; }
ok()   { printf '   ok  %s\n' "$*"; }
die()  { printf '   FAIL %s\n' "$*" >&2; exit 1; }

[ -f .env ] && die "there is a .env here; this check would replace it, move it away first"
data=$(mktemp -d "${TMPDIR:-/tmp}/$P.XXXXXX")
env_file=$(mktemp)
export COMPOSE_PROJECT_NAME=$P ENV_FILE=$env_file
dc() { docker compose --env-file "$env_file" --profile '*' "$@"; }
# the dumps are written by containers (root): remove them with a container too, so nothing root-owned stays in $TMPDIR
cleanup() {
  dc down -v >/dev/null 2>&1 || true
  docker run --rm -v "$data:/d" --entrypoint rm postgres:18 -rf /d/backups >/dev/null 2>&1 || true
  rm -rf "$data" "$env_file" 2>/dev/null || true
}
trap cleanup EXIT

step "setup"
OPTIONAL_SERVICES="backup" HOMEGRAF_IMAGE=unused BOOTSTRAP_IMAGE=unused PROJECT_PREFIX=$P BIND_ADDRESS=127.0.0.1 PORT_HTTP=28080 PORT_HTTPS=28443 \
  PORT_NPM_ADMIN=28081 PORT_FORGEJO=23000 PORT_FORGEJO_SSH=22222 PORT_KOMODO=29120 PORT_INFISICAL=28079 PORT_HOMEGRAF=28097 PORT_DNS=25353 \
  PORT_DNS_SETUP=23300 PORT_DNS_ADMIN=28053 DNS_MODE=sslip HOST_IP=127.0.0.1 ./setup.sh --yes >/dev/null
mv .env "$env_file"
sed -i "s|^DATA_DIR=.*|DATA_DIR=$data|" "$env_file"
grep -q '^COMPOSE_PROFILES=.*backup' "$env_file" || die "setup.sh did not turn the backup profile on"
mkdir -p "$data/backups"
ok "own project $P, data in $data"

step "the services and their databases, with a marker"
dc up -d --wait forgejo infisical komodo-mongo >/dev/null
started() { docker inspect -f '{{.State.StartedAt}}' "$P-$1"; }
forgejo_started=$(started forgejo); infisical_started=$(started infisical)
sql() { dc exec -T "$1-db" psql -U "$1" -d "$1" -tA -v ON_ERROR_STOP=1 -c "$2"; }
mongo() { dc exec -T komodo-mongo sh -c "mongosh --quiet \"mongodb://komodo:\$MONGO_INITDB_ROOT_PASSWORD@localhost:27017/komodo?authSource=admin\" --eval '$1'"; }
for k in forgejo infisical; do sql "$k" "CREATE TABLE marker(v text); INSERT INTO marker VALUES ('before')" >/dev/null; done
mongo 'db.marker.insertOne({v: "before"})' >/dev/null
ok "marker 'before' in all three"

step "the backup services dump it"
dc up -d backup backup-komodo >/dev/null
for _ in $(seq 1 60); do
  [ -n "$(ls "$data"/backups/forgejo-*.gz 2>/dev/null)" ] && [ -n "$(ls "$data"/backups/infisical-*.gz 2>/dev/null)" ] \
    && [ -n "$(ls "$data"/backups/komodo-*.archive.gz 2>/dev/null)" ] && break
  sleep 2
done
dc stop backup backup-komodo >/dev/null     # no further dumps: the ones with 'before' are the newest
listing=$(scripts/restore.sh list)
case "$listing" in *forgejo-2*) ;; *) die "restore.sh list shows no dump" ;; esac
ok "$(ls "$data/backups" | wc -l) dumps"

step "the data changes"
for k in forgejo infisical; do sql "$k" "DELETE FROM marker; INSERT INTO marker VALUES ('after')" >/dev/null; done
mongo 'db.marker.deleteMany({}); db.marker.insertOne({v: "after"})' >/dev/null
[ "$(sql forgejo 'SELECT v FROM marker')" = after ] || die "the change did not happen"
ok "marker 'after'"

step "a damaged dump is refused and changes nothing"
damaged="$data/backups/forgejo-29990101T000000Z.gz"
head -c 40 "$(ls "$data"/backups/forgejo-2*.gz | head -1)" > "$damaged"
if scripts/restore.sh forgejo --yes >/dev/null 2>&1; then die "restored from a damaged dump"; fi
[ "$(sql forgejo 'SELECT v FROM marker')" = after ] || die "the database changed although the dump was damaged"
rm -f "$damaged"
if scripts/restore.sh forgejo no-such-dump.gz --yes >/dev/null 2>&1; then die "accepted a dump that does not exist"; fi
ok "refused, database untouched"

step "restore"
for k in forgejo infisical; do
  out=$(scripts/restore.sh "$k" --yes) || { printf '%s\n' "$out" >&2; die "restore.sh $k failed"; }
  case "$out" in *"stopped $k"*"started $k"*) ;; *) die "$k: the service was not stopped and started again" ;; esac
  [ "$(sql "$k" 'SELECT v FROM marker')" = before ] || die "$k: the marker is not 'before' after the restore"
  ls "$data"/backups/pre-restore-"$k"-*.gz >/dev/null 2>&1 || die "$k: no copy of the state before the restore"
  ok "$k is back, the state before is kept"
done
dc up -d --wait forgejo infisical >/dev/null || die "Forgejo or Infisical is not healthy on the restored data"
[ "$(started forgejo)" != "$forgejo_started" ] && [ "$(started infisical)" != "$infisical_started" ] || die "the services were not restarted"
ok "Forgejo and Infisical were restarted and are healthy on the restored data"
scripts/restore.sh komodo --yes >/dev/null || die "restore.sh komodo failed"
[ "$(mongo 'db.marker.find().toArray().map(d => d.v).join(",")' | tail -1)" = before ] || die "komodo: the marker is not 'before' after the restore"
ls "$data"/backups/pre-restore-komodo-*.archive.gz >/dev/null 2>&1 || die "komodo: no copy of the state before the restore"
ok "komodo is back, the state before is kept"

step "the copy before a restore restores too (an undo)"
scripts/restore.sh forgejo "$(basename "$(ls "$data"/backups/pre-restore-forgejo-*.gz | head -1)")" --yes >/dev/null || die "restoring the copy failed"
[ "$(sql forgejo 'SELECT v FROM marker')" = after ] || die "the copy did not bring 'after' back"
ok "undone"

printf '\nALL GOOD\n'
