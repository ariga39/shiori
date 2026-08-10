#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "usage: $0 EXPECTED_IMAGE SERVICE_CONTAINER_ID" >&2
  exit 2
}

[[ $# == 2 ]] || usage
expected_image="$1"
service_container="$2"

if [[ ! "${expected_image}" =~ ^pgvector/pgvector@sha256:[0-9a-f]{64}$ ]]; then
  echo "refusing an unpinned pgvector image reference" >&2
  exit 1
fi
if [[ ! "${service_container}" =~ ^[0-9a-f]{12,64}$ ]]; then
  echo "refusing an invalid service container id" >&2
  exit 1
fi

containers=()
while IFS= read -r line; do
  containers+=("${line}")
done < <(docker ps --no-trunc --filter "id=${service_container}" --format '{{.ID}}')
if (( ${#containers[@]} != 1 )); then
  echo "expected exactly one service container for the supplied job service id" >&2
  exit 1
fi

image_id="$(docker inspect --format '{{.Image}}' "${service_container}")"
if [[ ! "${image_id}" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  echo "service container did not expose a valid image id" >&2
  exit 1
fi

if ! docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "${image_id}" \
  | grep -Fxq "${expected_image}"; then
  echo "service image RepoDigests do not contain the pinned image" >&2
  exit 1
fi

printf '%s\n' "${service_container}"
