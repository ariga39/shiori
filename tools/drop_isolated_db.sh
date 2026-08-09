#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${SHIYI_TEST_DATABASE_NAME:-}" ]]; then
  exit 0
fi

: "${SHIYI_TEST_DATABASE_DSN:?missing SHIYI_TEST_DATABASE_DSN}"
: "${SHIYI_TEST_DATABASE_MARKER:?missing SHIYI_TEST_DATABASE_MARKER}"
: "${GITHUB_RUN_ID:?missing GITHUB_RUN_ID}"
: "${GITHUB_RUN_ATTEMPT:?missing GITHUB_RUN_ATTEMPT}"

database="${SHIYI_TEST_DATABASE_NAME}"
if [[ ! "${database}" =~ ^shiyi_test_${GITHUB_RUN_ID}_${GITHUB_RUN_ATTEMPT}_[0-9]+$ ]]; then
  echo "teardown refused: database is outside this job namespace" >&2
  exit 1
fi

current_database="$(
  psql "${SHIYI_TEST_DATABASE_DSN}" \
    --no-psqlrc --set ON_ERROR_STOP=1 --tuples-only --no-align \
    --command 'SELECT current_database();'
)"
actual_marker="$(
  psql "${SHIYI_TEST_DATABASE_DSN}" \
    --no-psqlrc --set ON_ERROR_STOP=1 \
    --variable marker="${SHIYI_TEST_DATABASE_MARKER}" \
    --tuples-only --no-align \
    --command "SELECT marker FROM shiyi_test_guard WHERE marker = :'marker' LIMIT 1;"
)"

if [[ "${current_database}" != "${database}" || "${actual_marker}" != "${SHIYI_TEST_DATABASE_MARKER}" ]]; then
  echo "teardown refused: database identity or marker mismatch" >&2
  exit 1
fi

dropdb --if-exists --host 127.0.0.1 --port 5432 --username shiyi_ci "${database}"
