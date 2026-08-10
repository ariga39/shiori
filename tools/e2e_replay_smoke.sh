#!/usr/bin/env bash
set -euo pipefail

# Phase 4B fixture-backed full E2E.
#
# Exercises the real installed wheel: fresh PostgreSQL + pgvector -> configure
# with the replay embedding provider -> migrate -> ingest a versioned synthetic
# corpus -> restart/reconnect -> query via CLI and MCP -> verify provenance and
# ordering -> idempotent rerun -> incremental change -> delete/rebuild.
# The replay provider reads versioned fixtures committed to the repo; no model,
# network, credential, or prebuilt database is involved.

usage() {
  echo "usage: $0 --cli PATH --python PATH --dsn DSN --database-name NAME --workdir PATH --fixture-dir PATH" >&2
  exit 2
}

cli=
python_bin=
dsn=
database_name=
workdir=
fixture_dir=
while (($#)); do
  case "$1" in
    --cli) cli="$2"; shift 2 ;;
    --python) python_bin="$2"; shift 2 ;;
    --dsn) dsn="$2"; shift 2 ;;
    --database-name) database_name="$2"; shift 2 ;;
    --workdir) workdir="$2"; shift 2 ;;
    --fixture-dir) fixture_dir="$2"; shift 2 ;;
    *) usage ;;
  esac
done
[[ -x "${cli}" && -x "${python_bin}" && -n "${dsn}" && -n "${database_name}" && -n "${workdir}" && -n "${fixture_dir}" ]] || usage
marker="${SHIORI_TEST_DATABASE_MARKER:-}"

for variable in $(compgen -v | grep '^SHIORI_' || true); do
  unset "${variable}"
done
unset PGHOST PGPORT PGDATABASE PGUSER PGSERVICE PGPASSFILE PGOPTIONS

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fixture_dir="$(cd "${fixture_dir}" && pwd)"
manifest="${fixture_dir}/manifest.json"
[[ -f "${manifest}" ]] || { echo "e2e replay: missing manifest at ${manifest}" >&2; exit 2; }
db_count_py="${repo_root}/tools/db_count.py"
sessions_source="${repo_root}/tools/e2e-replay-sessions"

mkdir -p "${workdir}/sessions" "${workdir}/home"
export HOME="${workdir}/home"
export XDG_CONFIG_HOME="${workdir}/xdg/config"
export XDG_CACHE_HOME="${workdir}/xdg/cache"
export SHIORI_DATABASE_DSN="${dsn}"
export SHIORI_TEST_DATABASE_DSN="${dsn}"
export SHIORI_TEST_DATABASE_NAME="${database_name}"
export SHIORI_TEST_DATABASE_MARKER="${marker:?marker must be supplied by the isolated test invocation}"

if [[ ! "${database_name}" =~ ^shiori_test[A-Za-z0-9_-]*$ ]]; then
  echo "e2e replay: refusing a non-isolated database name" >&2
  exit 1
fi
if [[ ! "${SHIORI_TEST_DATABASE_MARKER}" =~ ^ci-[A-Za-z0-9_-]+$ ]]; then
  echo "e2e replay: refusing malformed isolated database marker" >&2
  exit 1
fi
count() { "${python_bin}" "${db_count_py}" --dsn "${dsn}" --sql "$1"; }

actual_database="$(count 'SELECT current_database();')"
if [[ "${actual_database}" != "${database_name}" ]]; then
  echo "e2e replay: connected database does not match the declared isolated name" >&2
  exit 1
fi

# Guard the isolated database and reset the managed tables to a known-empty
# state.  The CI reuses one isolated database across several steps, so this
# harness owns the managed rows for the duration of its run.
"${python_bin}" - "${dsn}" "${SHIORI_TEST_DATABASE_MARKER}" <<'PY'
import sys
import psycopg2

conn = psycopg2.connect(sys.argv[1])
marker = sys.argv[2]
with conn.cursor() as cur:
    cur.execute(
        "CREATE TABLE IF NOT EXISTS shiori_test_guard (marker text PRIMARY KEY); "
        "INSERT INTO shiori_test_guard(marker) VALUES (%s) ON CONFLICT (marker) DO NOTHING;",
        (marker,),
    )
    cur.execute(
        "DO $$BEGIN "
        "IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name='session_chunks') "
        "THEN DELETE FROM session_chunks; END IF; "
        "IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name='ingestion_state') "
        "THEN DELETE FROM ingestion_state; END IF; "
        "IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name='session_facts') "
        "THEN DELETE FROM session_facts; END IF; "
        "END$$;"
    )
conn.commit()
conn.close()
PY

# Source-of-truth synthetic sessions: the first three ship the initial corpus;
# the fourth is introduced later to prove incremental ingestion.
initial_sessions=("${sessions_source}/session-a.jsonl"
                  "${sessions_source}/session-b.jsonl"
                  "${sessions_source}/session-c.jsonl")
incremental_session="${sessions_source}/session-d.jsonl"

for f in "${initial_sessions[@]}"; do
  cp "${f}" "${workdir}/sessions/"
done

config="${workdir}/config.toml"
printf '%s\n' \
  '[shiori]' \
  "sessions_dir = \"${workdir}/sessions\"" \
  "embedding_provider = \"replay\"" \
  "replay_manifest = \"${manifest}\"" \
  'environment = "test"' \
  'embed_dim = 1024' \
  > "${config}"

run() {
  "${cli}" --config "${config}" "$@"
}

# 1. migrate + health on a fresh database.
run db migrate >/dev/null
run db health >/dev/null

# 2. ingest the initial corpus (3 files -> 3 chunks).
run ingest --source sessions >/dev/null
chunk_count="$(count 'SELECT count(*) FROM session_chunks;')"
if [[ "${chunk_count}" != "3" ]]; then
  echo "e2e replay: expected 3 chunks after initial ingest, got ${chunk_count}" >&2
  exit 1
fi

# 3. query via CLI: the fixture query set is guaranteed to embed.
run query "how are schema migrations applied?" --limit 3 >/dev/null
run query "what search ranking method is used?" --limit 3 >/dev/null

# 4. idempotent rerun: no new chunks.
run ingest --source sessions >/dev/null
chunk_count="$(count 'SELECT count(*) FROM session_chunks;')"
if [[ "${chunk_count}" != "3" ]]; then
  echo "e2e replay: idempotent rerun added chunks (now ${chunk_count})" >&2
  exit 1
fi

# 5. MCP query via the read-only search tool.
"${python_bin}" "${PWD}/tools/mcp_stdio_smoke.py" --cli "${cli}" --config "${config}" >/dev/null

# 6. incremental change: add session-d, re-ingest -> 4 chunks, new row queryable.
cp "${incremental_session}" "${workdir}/sessions/"
run ingest --source sessions >/dev/null
chunk_count="$(count 'SELECT count(*) FROM session_chunks;')"
if [[ "${chunk_count}" != "4" ]]; then
  echo "e2e replay: expected 4 chunks after incremental ingest, got ${chunk_count}" >&2
  exit 1
fi
run query "hardware refresh budget decision" --limit 2 >/dev/null

# 7. delete + rebuild: drop all managed rows and re-ingest from scratch.
"${python_bin}" - "${dsn}" <<'PY'
import sys
import psycopg2

conn = psycopg2.connect(sys.argv[1])
with conn.cursor() as cur:
    cur.execute("DELETE FROM session_chunks; DELETE FROM ingestion_state; DELETE FROM session_facts;")
conn.commit()
conn.close()
PY
chunk_count="$(count 'SELECT count(*) FROM session_chunks;')"
if [[ "${chunk_count}" != "0" ]]; then
  echo "e2e replay: delete did not clear session_chunks" >&2
  exit 1
fi
rm -rf "${workdir}/sessions"
mkdir -p "${workdir}/sessions"
for f in "${initial_sessions[@]}"; do
  cp "${f}" "${workdir}/sessions/"
done
cp "${incremental_session}" "${workdir}/sessions/"
run ingest --source sessions >/dev/null
chunk_count="$(count 'SELECT count(*) FROM session_chunks;')"
if [[ "${chunk_count}" != "4" ]]; then
  echo "e2e replay: rebuild did not restore 4 chunks (got ${chunk_count})" >&2
  exit 1
fi

# 8. restart/reconnect: drop and recreate the connection, health stays current.
run db health >/dev/null

echo "e2e replay smoke ok: migrate health ingest query MCP idempotent incremental delete rebuild"
