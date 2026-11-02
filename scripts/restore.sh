#!/usr/bin/env bash
# Puts one database of this installation back from a dump of the `backup` profile (data/backups), the way back of that profile.
#
#   scripts/restore.sh list                       the dumps there are, newest last
#   scripts/restore.sh forgejo [FILE] [--yes]     Forgejo's PostgreSQL: accounts, repositories' metadata, issues, pull requests
#   scripts/restore.sh infisical [FILE] [--yes]   Infisical's PostgreSQL: the secrets and the certificate authority
#   scripts/restore.sh komodo [FILE] [--yes]      Komodo's MongoDB: servers, stacks, procedures
#   FILE is a path, or a name in the backups directory; without it the newest dump of that kind is used.
#   ENV_FILE=path scripts/restore.sh ...          another env file than .env
#
# What it does: checks the dump (gzip), makes a copy of the database as it is now (pre-restore-<kind>-<time>, in the same folder,
# so a mistake can be undone), stops what uses the database, replaces the database with the dump, and starts what it stopped.
# What a dump does not hold stays as it is: the git data of the repositories (forgejo-data), Komodo's keys and the proxy's data.
# Infisical encrypts its secrets with INFISICAL_ENCRYPTION_KEY from your .env: keep that file together with the dumps, a dump
# restored under another key cannot be read.
set -euo pipefail
cd "$(dirname "$0")/.."
env_file=${ENV_FILE:-.env}
usage() { sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-1}"; }

yes=0 kind="" file=""
for arg in "$@"; do
  case "$arg" in
    --yes) yes=1 ;;
    -h | --help) usage 0 ;;
    list | forgejo | infisical | komodo) [ -z "$kind" ] && kind=$arg || file=$arg ;;
    *) [ -z "$file" ] && [ -n "$kind" ] && file=$arg || usage ;;
  esac
done
[ -n "$kind" ] || usage
[ -f "$env_file" ] || { echo "no $env_file here: nothing to restore into" >&2; exit 1; }
data_dir=$(grep '^DATA_DIR=' "$env_file" | cut -d= -f2-)
[ -n "$data_dir" ] || { echo "DATA_DIR is not set in $env_file" >&2; exit 1; }
backups="$data_dir/backups"
dc() { docker compose --env-file "$env_file" --profile '*' "$@"; }

# the dump files of one kind, oldest first (the names carry a UTC time stamp)
dumps() {
  case "$1" in
    forgejo | infisical) find "$backups" -maxdepth 1 -name "$1-*.gz" 2>/dev/null | sort ;;
    komodo) find "$backups" -maxdepth 1 -name 'komodo-*.archive.gz' 2>/dev/null | sort ;;
  esac
}

if [ "$kind" = list ]; then
  for k in forgejo infisical komodo; do
    echo "$k:"
    dumps "$k" | while read -r f; do printf '  %s  %s\n' "$(basename "$f")" "$(du -h "$f" | cut -f1)"; done
  done
  exit 0
fi

if [ -z "$file" ]; then
  file=$(dumps "$kind" | tail -1)
  [ -n "$file" ] || { echo "no $kind dump in $backups (is the profile backup on?)" >&2; exit 1; }
elif [ ! -f "$file" ] && [ -f "$backups/$file" ]; then
  file="$backups/$file"
fi
[ -f "$file" ] || { echo "no such dump: $file" >&2; exit 1; }
gzip -t "$file" || { echo "$file is not a complete gzip file: not restoring from it" >&2; exit 1; }
echo "restoring $kind from $file ($(date -r "$file" '+%Y-%m-%d %H:%M'))"

if [ "$yes" != 1 ]; then
  read -r -p "This replaces the $kind database with that dump; changes made since are lost (a copy of the current state is kept). Continue? [y/N] " answer
  case "$answer" in y | Y) ;; *) exit 1 ;; esac
fi

case "$kind" in
  forgejo)   db_service=forgejo-db   stops="forgejo runner" ;;
  infisical) db_service=infisical-db stops="infisical" ;;
  komodo)    db_service=komodo-mongo stops="komodo-core" ;;
esac

# what runs now and uses the database: only that is started again afterwards
running=$(dc ps --status running --services 2>/dev/null || true)
again=""
for s in $stops; do grep -qx "$s" <<<"$running" && again="$again $s"; done

echo "== the database"
dc up -d --wait "$db_service" >/dev/null

stamp=$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$backups"
case "$kind" in
  forgejo | infisical)
    user=$kind
    dump_now() { dc exec -T "$db_service" pg_dump -U "$user" "$user"; }
    restore() {
      dc exec -T "$db_service" psql -U "$user" -d postgres -v ON_ERROR_STOP=1 -q \
        -c "DROP DATABASE IF EXISTS $user WITH (FORCE)" -c "CREATE DATABASE $user OWNER $user"
      gzip -dc "$file" | dc exec -T "$db_service" psql -U "$user" -d "$user" -v ON_ERROR_STOP=1 -q -o /dev/null
    } ;;
  komodo)
    dump_now() { dc exec -T "$db_service" sh -c 'mongodump --quiet --uri "mongodb://komodo:$MONGO_INITDB_ROOT_PASSWORD@localhost:27017/?authSource=admin" --archive --gzip'; }
    restore() {
      dc exec -T "$db_service" sh -c 'mongorestore --quiet --uri "mongodb://komodo:$MONGO_INITDB_ROOT_PASSWORD@localhost:27017/?authSource=admin" --archive --gzip --drop --nsExclude "admin.*" --nsExclude "config.*"' < "$file"
    } ;;
esac

echo "== a copy of the current state"
safety="$backups/pre-restore-$kind-$stamp.$([ "$kind" = komodo ] && echo archive.gz || echo gz)"
copied=0
if [ "$kind" = komodo ]; then
  if dump_now > "$safety.part" 2>/dev/null; then copied=1; fi
elif dump_now 2>/dev/null | gzip > "$safety.part"; then copied=1    # pipefail: a failed dump is a failed copy, not an empty file
fi
if [ "$copied" = 1 ] && [ -s "$safety.part" ]; then
  mv "$safety.part" "$safety"; echo "   $safety"
else
  rm -f "$safety.part"; echo "   the current database could not be dumped: going on without a copy"
fi

echo "== stopping what uses it"
for s in $again; do dc stop "$s" >/dev/null; echo "   stopped $s"; done

echo "== restoring"
restore

if [ "$kind" = infisical ]; then
  # its queue and cache live in Redis and describe the old state
  dc exec -T infisical-redis redis-cli FLUSHALL >/dev/null 2>&1 || true
fi

echo "== starting again"
for s in $again; do dc start "$s" >/dev/null; echo "   started $s"; done
echo "Done: $kind is back to $(basename "$file"). The state before is kept as $(basename "$safety") (in $backups, removed with the other old dumps)."
