#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${SHIORI_TEST_DATABASE_NAME:-}" ]]; then
  exit 0
fi

: "${SHIORI_TEST_DATABASE_DSN:?missing SHIORI_TEST_DATABASE_DSN}"
: "${SHIORI_TEST_DATABASE_MARKER:?missing SHIORI_TEST_DATABASE_MARKER}"
: "${GITHUB_RUN_ID:?missing GITHUB_RUN_ID}"
: "${GITHUB_RUN_ATTEMPT:?missing GITHUB_RUN_ATTEMPT}"

database="${SHIORI_TEST_DATABASE_NAME}"
marker="${SHIORI_TEST_DATABASE_MARKER}"
if [[ ! "${database}" =~ ^shiori_test_${GITHUB_RUN_ID}_${GITHUB_RUN_ATTEMPT}_[0-9]+$ ]]; then
  echo "teardown refused: database is outside this job namespace" >&2
  exit 1
fi
if [[ ! "${marker}" =~ ^ci-[0-9]+-[0-9]+-[0-9]+$ ]]; then
  echo "teardown refused: marker format is invalid" >&2
  exit 1
fi

current_database="$(
  psql "${SHIORI_TEST_DATABASE_DSN}" \
    --no-psqlrc --set ON_ERROR_STOP=1 --tuples-only --no-align \
    --command 'SELECT current_database();'
)"
actual_marker="$(
  psql "${SHIORI_TEST_DATABASE_DSN}" \
    --no-psqlrc --set ON_ERROR_STOP=1 \
    --tuples-only --no-align \
    --command "SELECT marker FROM shiori_test_guard WHERE marker = '${marker}' LIMIT 1;"
)"

if [[ "${current_database}" != "${database}" || "${actual_marker}" != "${marker}" ]]; then
  echo "teardown refused: database identity or marker mismatch" >&2
  exit 1
fi

dropdb --if-exists --host 127.0.0.1 --port 5432 --username shiori_ci "${database}"
echo "isolated database teardown verified"
