#!/usr/bin/env bash
# Start the local PostgreSQL + pgvector service using explicitly supplied
# credentials. The credential file is never printed and is never discovered
# from a home-directory default.

set -euo pipefail
cd "$(dirname "$0")/.."

if [[ -z "${POSTGRES_DB:-}" || -z "${POSTGRES_USER:-}" || -z "${POSTGRES_PASSWORD:-}" ]]; then
  if [[ -z "${SHIYI_PG_CRED:-}" || ! -f "$SHIYI_PG_CRED" ]]; then
    echo "error: set POSTGRES_DB/POSTGRES_USER/POSTGRES_PASSWORD or an explicit SHIYI_PG_CRED file" >&2
    exit 1
  fi

  get() { awk -F= -v key="$1" '$1 == key {sub(/^[^=]*=/, ""); print; exit}' "$SHIYI_PG_CRED" | tr -d '\r'; }
  export POSTGRES_DB="$(get dbname)"
  export POSTGRES_USER="$(get user)"
  export POSTGRES_PASSWORD="$(get password)"
fi

if [[ -z "$POSTGRES_DB" || -z "$POSTGRES_USER" || -z "$POSTGRES_PASSWORD" ]]; then
  echo "error: credentials file missing dbname/user/password keys" >&2
  exit 1
fi

if [[ -n "${SHIYI_COMPOSE_PROJECT:-}" ]]; then
  if [[ ! "${SHIYI_COMPOSE_PROJECT}" =~ ^[a-z0-9][a-z0-9_-]{0,50}$ ]]; then
    echo "error: SHIYI_COMPOSE_PROJECT has an invalid project name" >&2
    exit 1
  fi
  export COMPOSE_PROJECT_NAME="${SHIYI_COMPOSE_PROJECT}"
fi

exec docker compose -f deploy/docker-compose.yml "$@"
