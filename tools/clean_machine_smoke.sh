#!/usr/bin/env bash
set -euo pipefail

# This harness deliberately constructs every path and provider setting under a
# fresh temporary HOME.  It is a CI/clean-machine proof, not a production
# bootstrap and never reads host OpenClaw/Hermes credentials.

usage() {
  echo "usage: $0 --cli PATH --python PATH --dsn DSN --database-name NAME --workdir PATH" >&2
  exit 2
}

cli=
python_bin=
dsn=
database_name=
workdir=
while (($#)); do
  case "$1" in
    --cli) cli="$2"; shift 2 ;;
    --python) python_bin="$2"; shift 2 ;;
    --dsn) dsn="$2"; shift 2 ;;
    --database-name) database_name="$2"; shift 2 ;;
    --workdir) workdir="$2"; shift 2 ;;
    *) usage ;;
  esac
done
[[ -x "${cli}" && -x "${python_bin}" && -n "${dsn}" && -n "${database_name}" && -n "${workdir}" ]] || usage
marker="${SHIYI_TEST_DATABASE_MARKER:-}"

for variable in $(compgen -v | grep '^SHIYI_' || true); do
  unset "${variable}"
done
unset PGHOST PGPORT PGDATABASE PGUSER PGSERVICE PGPASSFILE PGOPTIONS

mkdir -p "${workdir}/home" "${workdir}/sessions" "${workdir}/discord" "${workdir}/exports"
export HOME="${workdir}/home"
export XDG_CONFIG_HOME="${workdir}/home/.config"
export XDG_CACHE_HOME="${workdir}/home/.cache"
export SHIYI_DATABASE_DSN="${dsn}"
export SHIYI_TEST_DATABASE_DSN="${dsn}"
export SHIYI_TEST_DATABASE_NAME="${database_name}"
export SHIYI_TEST_DATABASE_MARKER="${marker:?marker must be supplied by the isolated test invocation}"
# The harness is deliberately self-contained; never inherit a host user's
# ambient PostgreSQL password into an isolated smoke run.
export PGPASSWORD="shiyi-ci-only"

if [[ ! "${database_name}" =~ ^shiyi_test[A-Za-z0-9_-]*$ ]]; then
  echo "refusing a non-isolated database name" >&2
  exit 1
fi
actual_database="$(psql "${dsn}" --set ON_ERROR_STOP=1 --tuples-only --no-align --command 'SELECT current_database();')"
if [[ "${actual_database}" != "${database_name}" ]]; then
  echo "connected database does not match the declared isolated name" >&2
  exit 1
fi
if [[ ! "${SHIYI_TEST_DATABASE_MARKER}" =~ ^ci-[A-Za-z0-9_-]+$ ]]; then
  echo "refusing malformed isolated database marker" >&2
  exit 1
fi

# Exercise the documented private key/value credential path through the
# installed wheel. Keep the file under the fresh harness home and create it
# before any command can read it; no host credential file is consulted.
credential_file="${workdir}/home/pg-credentials"
umask 077
cat > "${credential_file}" <<EOF
host=127.0.0.1
port=5432
dbname=${database_name}
user=shiyi_ci
password=${PGPASSWORD}
EOF
chmod 600 "${credential_file}"
unset SHIYI_DATABASE_DSN
export SHIYI_PG_CRED="${credential_file}"

psql "${dsn}" --set ON_ERROR_STOP=1 \
  --command "CREATE TABLE IF NOT EXISTS shiyi_test_guard (marker text PRIMARY KEY); INSERT INTO shiyi_test_guard(marker) VALUES ('${SHIYI_TEST_DATABASE_MARKER}') ON CONFLICT (marker) DO NOTHING;"
guard_count="$(psql "${dsn}" --set ON_ERROR_STOP=1 --tuples-only --no-align --command 'SELECT count(*) FROM shiyi_test_guard;')"
guard_marker="$(psql "${dsn}" --set ON_ERROR_STOP=1 --tuples-only --no-align --command 'SELECT marker FROM shiyi_test_guard ORDER BY marker LIMIT 1;')"
if [[ "${guard_count}" != 1 || "${guard_marker}" != "${SHIYI_TEST_DATABASE_MARKER}" ]]; then
  echo "isolated database guard mismatch" >&2
  exit 1
fi

config="${workdir}/config.toml"
printf '%s\n' \
  '[shiyi]' \
  "sessions_dir = \"${workdir}/sessions\"" \
  "hermes_db = \"${workdir}/hermes.db\"" \
  "discord_archive_dir = \"${workdir}/discord\"" \
  'embedding_provider = "fake"' \
  'allow_fake_embeddings = true' \
  'environment = "test"' \
  'voyage_model = "shiyi-fake-v1"' \
  'embed_dim = 1024' \
  > "${config}"

printf '%s\n' \
  '{"type":"message","message":{"role":"user","content":"Synthetic clean-machine smoke decision"},"timestamp":"2026-01-01T00:00:00Z"}' \
  '{"type":"message","message":{"role":"assistant","content":"Synthetic result remains local and searchable"},"timestamp":"2026-01-01T00:01:00Z"}' \
  > "${workdir}/sessions/synthetic.jsonl"

printf '%s\n' \
  '{"id":"m1","type":0,"timestamp":"2026-01-01T00:00:00+00:00","author":{"username":"synthetic"},"content":"Synthetic Discord smoke message"}' \
  > "${workdir}/discord/channel.jsonl"

"${python_bin}" - "${workdir}/hermes.db" <<'PY'
import sqlite3
import sys

path = sys.argv[1]
connection = sqlite3.connect(path)
connection.executescript(
    """
    CREATE TABLE sessions (
      id TEXT PRIMARY KEY, source TEXT, title TEXT, chat_id TEXT,
      message_count INTEGER, started_at REAL, last_activity_at REAL,
      rewind_count INTEGER DEFAULT 0
    );
    CREATE TABLE messages (
      id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,
      timestamp REAL, active INTEGER DEFAULT 1, compacted INTEGER DEFAULT 0
    );
    INSERT INTO sessions VALUES ('hermes-smoke', 'tui', 'Synthetic', 'local', 2, 1767225600, 1767225660, 0);
    INSERT INTO messages VALUES (1, 'hermes-smoke', 'user', 'Synthetic Hermes smoke message', 1767225600, 1, 0);
    INSERT INTO messages VALUES (2, 'hermes-smoke', 'assistant', 'Synthetic Hermes result', 1767225660, 1, 0);
    """
)
connection.commit()
connection.close()
PY

run() {
  "${cli}" --config "${config}" "$@"
}

run db migrate >/dev/null
run db health >/dev/null
run ingest --source sessions --dry-run >/dev/null
run ingest --source hermes --dry-run >/dev/null
run ingest --source discord --dry-run >/dev/null
run ingest --source sessions >/dev/null
run ingest --source hermes >/dev/null
run ingest --source discord >/dev/null
run query "synthetic clean-machine smoke" --limit 3 >/dev/null
run privacy providers >/dev/null
run privacy retention-check --scope sessions >/dev/null
run privacy export --scope sessions --dest "${workdir}/exports/sessions.json" >/dev/null
run privacy export --scope sessions --dest "${workdir}/exports/sessions.json" --yes >/dev/null
run privacy delete --scope discord >/dev/null
run privacy delete --scope discord --yes >/dev/null
run privacy delete --scope discord --yes >/dev/null

backup="${workdir}/exports/shiyi.dump"
run db backup "${backup}" >/dev/null
restore_name="shiyi_restore_ci_${GITHUB_RUN_ID:-local}_${GITHUB_RUN_ATTEMPT:-1}"
restore_json="${workdir}/restore.json"
run db restore "${backup}" --target "${restore_name}" > "${restore_json}"
restored_db="$(${python_bin} - "${restore_json}" <<'PY'
import json
import sys
from urllib.parse import urlsplit

with open(sys.argv[1], encoding="utf-8") as stream:
    payload = json.load(stream)
name = urlsplit(payload["staging_dsn"]).path.lstrip("/")
if not name.startswith("shiyi_restore_ci_"):
    raise SystemExit("unexpected restore database name")
print(name)
PY
)"
dropdb --if-exists --host 127.0.0.1 --port 5432 --username shiyi_ci --no-password -- "${restored_db}"

"${python_bin}" "${PWD}/tools/mcp_stdio_smoke.py" --cli "${cli}" --config "${config}" >/dev/null
echo "clean-machine smoke ok: migrate health ingest query privacy backup restore MCP"
