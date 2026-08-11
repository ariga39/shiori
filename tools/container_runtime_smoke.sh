#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "usage: $0 --project PROJECT [--skip-build]" >&2
  exit 2
}

project=""
skip_build=false
while (($#)); do
  case "$1" in
    --project)
      (($# >= 2)) || usage
      project="$2"
      shift 2
      ;;
    --skip-build)
      skip_build=true
      shift
      ;;
    *)
      usage
      ;;
  esac
done

[[ "${project}" =~ ^[a-z0-9][a-z0-9_-]{0,50}$ ]] || {
  echo "refusing an invalid compose project name" >&2
  exit 1
}

: "${POSTGRES_DB:?missing POSTGRES_DB}"
: "${POSTGRES_USER:?missing POSTGRES_USER}"
: "${POSTGRES_PASSWORD:?missing POSTGRES_PASSWORD}"
: "${SHIORI_PG_PORT:?missing SHIORI_PG_PORT}"

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
compose_file="${repo_root}/deploy/docker-compose.yml"
image_ref="shiori-pgvector:local"

compose=(docker compose --file "${compose_file}" --project-name "${project}")
# Configuration, identity preflight, and image build are non-destructive with
# respect to compose resources. Do them before installing the EXIT trap so a
# failure here cannot run down --volumes against a pre-existing project.
"${compose[@]}" config --quiet
existing_containers="$(docker ps -aq --filter "label=com.docker.compose.project=${project}")"
existing_networks="$(docker network ls -q --filter "label=com.docker.compose.project=${project}")"
existing_volumes="$(docker volume ls -q --filter "label=com.docker.compose.project=${project}")"
if [[ -n "${existing_containers}" || -n "${existing_networks}" || -n "${existing_volumes}" ]]; then
  echo "refusing to reuse existing resources for the compose project" >&2
  exit 1
fi
if [[ "${skip_build}" != true ]]; then
  "${compose[@]}" build --pull session-memory-pg
fi

image_id="$(docker image inspect "${image_ref}" --format '{{.Id}}')"
[[ "${image_id}" =~ ^sha256:[0-9a-f]{64}$ ]] || {
  echo "built compose image did not expose an immutable image id" >&2
  exit 1
}

# The preflight established that this project had no labeled resources. From
# this point onward, any project resources are owned by this smoke invocation,
# including resources partially created by a failing up command.
created=false
started=false
cleaned=false
cleanup() {
  status=$?
  set +e
  if [[ "${cleaned}" != true && ( "${created}" == true || "${started}" == true ) ]]; then
    "${compose[@]}" down --volumes --remove-orphans >/dev/null 2>&1
    cleaned=true
  fi
  containers="$(docker ps -aq --filter "label=com.docker.compose.project=${project}" 2>/dev/null)"
  networks="$(docker network ls -q --filter "label=com.docker.compose.project=${project}" 2>/dev/null)"
  volumes="$(docker volume ls -q --filter "label=com.docker.compose.project=${project}" 2>/dev/null)"
  if [[ -n "${containers}" || -n "${networks}" || -n "${volumes}" ]]; then
    status=1
  fi
  exit "${status}"
}
trap cleanup EXIT

created=true
"${compose[@]}" up --detach --force-recreate --no-deps session-memory-pg >/dev/null
started=true
container_id="$("${compose[@]}" ps --quiet session-memory-pg | tr -d '\r\n')"
[[ "${container_id}" =~ ^[0-9a-f]{12,64}$ ]] || {
  echo "compose did not start exactly one database container" >&2
  exit 1
}
project_volumes=()
while IFS= read -r line; do
  project_volumes+=("${line}")
done < <(docker volume ls -q --filter "label=com.docker.compose.project=${project}")
if (( ${#project_volumes[@]} != 1 )); then
  echo "compose did not create exactly one project-scoped data volume" >&2
  exit 1
fi
volume_scope="$(docker volume inspect --format '{{ index .Labels "com.shiori.scope" }}' "${project_volumes[0]}")"
[[ "${volume_scope}" == project-owned ]] || {
  echo "data volume is missing the project-owned label" >&2
  exit 1
}

configured_user="$(docker inspect --format '{{.Config.User}}' "${container_id}")"
entrypoint="$(docker inspect --format '{{json .Config.Entrypoint}}' "${container_id}")"
command="$(docker inspect --format '{{json .Config.Cmd}}' "${container_id}")"
[[ "${configured_user}" == postgres ]] || {
  echo "database container is not configured for the postgres user" >&2
  exit 1
}
[[ "${entrypoint}" != "" && "${entrypoint}" != null ]] || {
  echo "database container has no inherited entrypoint" >&2
  exit 1
}
[[ "${command}" == *shared_preload_libraries=vector* ]] || {
  echo "database container CMD does not request vector preload" >&2
  exit 1
}

ready=false
for _ in {1..60}; do
  if "${compose[@]}" exec --no-TTY session-memory-pg \
    pg_isready --username "${POSTGRES_USER}" --dbname "${POSTGRES_DB}" >/dev/null 2>&1; then
    ready=true
    break
  fi
  sleep 1
done
[[ "${ready}" == true ]] || {
  echo "database container did not become ready" >&2
  exit 1
}

uid="$("${compose[@]}" exec --no-TTY session-memory-pg id -u | tr -d '\r\n')"
[[ "${uid}" =~ ^[1-9][0-9]*$ ]] || {
  echo "database process is running as root" >&2
  exit 1
}

psql_exec() {
  "${compose[@]}" exec --no-TTY --env PGPASSWORD="${POSTGRES_PASSWORD}" \
    session-memory-pg psql --no-psqlrc --set ON_ERROR_STOP=1 \
    --username "${POSTGRES_USER}" --dbname "${POSTGRES_DB}" "$@"
}

preload="$(psql_exec --tuples-only --no-align --command 'SHOW shared_preload_libraries;')"
grep -Eq '(^|[,[:space:]])vector([,[:space:]]|$)' <<<"${preload}" || {
  echo "vector preload is not active" >&2
  exit 1
}

psql_exec --command 'CREATE EXTENSION IF NOT EXISTS vector; CREATE TABLE shiori_container_smoke (id integer PRIMARY KEY, embedding vector(2)); INSERT INTO shiori_container_smoke VALUES (1, $$[1,2]$$);' >/dev/null
count="$(psql_exec --tuples-only --no-align --command 'SELECT count(*) FROM shiori_container_smoke;')"
[[ "${count}" == 1 ]] || {
  echo "vector write smoke did not persist one row" >&2
  exit 1
}

"${compose[@]}" restart session-memory-pg >/dev/null
# Restart readiness must not trust `pg_isready` alone: pg_isready can report
# success while the old postmaster is still shutting down, and the very next
# psql then fails with `database system is shutting down`. Wait for the NEW
# postmaster generation to be stably readable AND writable: a transactional
# probe must succeed twice consecutively (fail-closed with a hard timeout).
ready=false
for _ in {1..60}; do
  if "${compose[@]}" exec --no-TTY --env PGPASSWORD="${POSTGRES_PASSWORD}" \
      session-memory-pg psql --no-psqlrc --set ON_ERROR_STOP=1 \
      --username "${POSTGRES_USER}" --dbname "${POSTGRES_DB}" \
      --command 'BEGIN; SELECT 1; COMMIT;' >/dev/null 2>&1; then
    # One success can still be a shutting-down old postmaster's last breath;
    # require a second consecutive success to confirm the new generation is
    # durably accepting reads and writes.
    if "${compose[@]}" exec --no-TTY --env PGPASSWORD="${POSTGRES_PASSWORD}" \
        session-memory-pg psql --no-psqlrc --set ON_ERROR_STOP=1 \
        --username "${POSTGRES_USER}" --dbname "${POSTGRES_DB}" \
        --command 'BEGIN; SELECT 1; COMMIT;' >/dev/null 2>&1; then
      ready=true
      break
    fi
  fi
  sleep 1
done
[[ "${ready}" == true ]] || {
  echo "database container did not become read/write ready after restart" >&2
  exit 1
}

uid_after_restart="$("${compose[@]}" exec --no-TTY session-memory-pg id -u | tr -d '\r\n')"
[[ "${uid_after_restart}" =~ ^[1-9][0-9]*$ ]] || {
  echo "restarted database process is running as root" >&2
  exit 1
}
count_after_restart="$(psql_exec --tuples-only --no-align --command 'SELECT count(*) FROM shiori_container_smoke;')"
[[ "${count_after_restart}" == 1 ]] || {
  echo "database row was not retained across restart" >&2
  exit 1
}

echo "container runtime smoke passed"
