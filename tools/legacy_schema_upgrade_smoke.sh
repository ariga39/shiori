#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "usage: $0 --cli PATH --admin-dsn CONNINFO [--schema PATH]" >&2
  exit 2
}

cli=""
admin_dsn=""
schema_path="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/schema.sql"
while (($#)); do
  case "$1" in
    --cli)
      (($# >= 2)) || usage
      cli="$2"
      shift 2
      ;;
    --admin-dsn)
      (($# >= 2)) || usage
      admin_dsn="$2"
      shift 2
      ;;
    --schema)
      (($# >= 2)) || usage
      schema_path="$2"
      shift 2
      ;;
    *)
      usage
      ;;
  esac
done

[[ -x "$cli" ]] || { echo "legacy upgrade smoke: CLI is not executable" >&2; exit 2; }
[[ -n "$admin_dsn" ]] || { echo "legacy upgrade smoke: admin DSN is required" >&2; exit 2; }
[[ -f "$schema_path" ]] || { echo "legacy upgrade smoke: schema file is missing" >&2; exit 2; }

run_id="${GITHUB_RUN_ID:-0}"
run_attempt="${GITHUB_RUN_ATTEMPT:-0}"
database="shiyi_legacy_${run_id}_${run_attempt}_${RANDOM}"
marker="legacy-ci-${run_id}-${run_attempt}-${RANDOM}"
if [[ ! "$database" =~ ^shiyi_legacy_[0-9]+_[0-9]+_[0-9]+$ ||
      ! "$marker" =~ ^legacy-ci-[0-9]+-[0-9]+-[0-9]+$ ]]; then
  echo "legacy upgrade smoke: generated identity is malformed" >&2
  exit 2
fi

admin_base="${admin_dsn%/*}"
if [[ "$admin_base" == "$admin_dsn" || -z "$admin_base" ]]; then
  echo "legacy upgrade smoke: admin DSN must contain a database component" >&2
  exit 2
fi
target_dsn="${admin_base}/${database}"
tmp_dir="$(mktemp -d)"

cleanup() {
  local status=$?
  set +e
  local current=""
  local actual_marker=""
  current="$(psql "$target_dsn" --no-psqlrc --set ON_ERROR_STOP=1 --tuples-only --no-align \
    --command 'SELECT current_database();' 2>/dev/null)"
  actual_marker="$(psql "$target_dsn" --no-psqlrc --set ON_ERROR_STOP=1 --tuples-only --no-align \
    --command "SELECT marker FROM shiyi_legacy_test_guard WHERE marker = '${marker}' LIMIT 1;" \
    2>/dev/null)"
  if [[ "$current" == "$database" && "$actual_marker" == "$marker" ]]; then
    psql "$admin_dsn" --no-psqlrc --set ON_ERROR_STOP=1 \
      --command "DROP DATABASE IF EXISTS \"${database}\";" >/dev/null 2>&1
  else
    echo "legacy upgrade smoke: cleanup identity check failed; database retained" >&2
    status=1
  fi
  rm -rf "$tmp_dir"
  exit "$status"
}
trap cleanup EXIT

psql "$admin_dsn" --no-psqlrc --set ON_ERROR_STOP=1 \
  --command "CREATE DATABASE \"${database}\";"
psql "$target_dsn" --no-psqlrc --set ON_ERROR_STOP=1 --command \
  "CREATE TABLE shiyi_legacy_test_guard (marker text PRIMARY KEY); INSERT INTO shiyi_legacy_test_guard(marker) VALUES ('${marker}');"
psql "$target_dsn" --no-psqlrc --set ON_ERROR_STOP=1 --file "$schema_path"
psql "$target_dsn" --no-psqlrc --set ON_ERROR_STOP=1 --command \
  "INSERT INTO session_chunks (session_id, source_type, content, embedding_model) VALUES ('legacy-upgrade-smoke', 'main_user', 'synthetic legacy row', 'voyage-4-large');"

SHIYI_DATABASE_DSN="$target_dsn" "$cli" db migrate > "$tmp_dir/migrate.json"
SHIYI_DATABASE_DSN="$target_dsn" "$cli" db health > "$tmp_dir/health.json"
grep -q '"version": 1' "$tmp_dir/migrate.json"
grep -q '"state": "current"' "$tmp_dir/health.json"

actual_database="$(psql "$target_dsn" --no-psqlrc --set ON_ERROR_STOP=1 --tuples-only --no-align \
  --command 'SELECT current_database();')"
actual_marker="$(psql "$target_dsn" --no-psqlrc --set ON_ERROR_STOP=1 --tuples-only --no-align \
  --command "SELECT marker FROM shiyi_legacy_test_guard WHERE marker = '${marker}' LIMIT 1;")"
version="$(psql "$target_dsn" --no-psqlrc --set ON_ERROR_STOP=1 --tuples-only --no-align \
  --command 'SELECT max(version) FROM shiyi_schema_migrations;')"
legacy_row="$(psql "$target_dsn" --no-psqlrc --set ON_ERROR_STOP=1 --tuples-only --no-align \
  --command "SELECT count(*) FROM session_chunks WHERE session_id = 'legacy-upgrade-smoke' AND content = 'synthetic legacy row';")"
[[ "$actual_database" == "$database" && "$actual_marker" == "$marker" ]]
[[ "$version" == "1" && "$legacy_row" == "1" ]]

echo "legacy schema upgrade smoke passed"
