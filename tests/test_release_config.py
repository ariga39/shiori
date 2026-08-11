from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
AUDIT = ROOT / "tools" / "release_audit.py"
DOCKERFILE = ROOT / "Dockerfile"
COMPOSE = ROOT / "deploy" / "docker-compose.yml"
RUN_SCRIPT = ROOT / "deploy" / "run.sh"
RUNTIME_SMOKE = ROOT / "tools" / "container_runtime_smoke.sh"
CLEAN_SMOKE = ROOT / "tools" / "clean_machine_smoke.sh"
PGVECTOR_DIGEST = "sha256:7ae6051efd0e60444282c27c7e141af07f322ce033300e727a49c3dd11075e38"


def test_ci_actions_and_container_are_pinned() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    uses = re.findall(r"^\s+uses:\s+([^\s#]+)", workflow, flags=re.MULTILINE)

    assert uses
    assert all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", value) for value in uses)
    assert workflow.count(PGVECTOR_DIGEST) >= 2
    assert "schema.sql" not in workflow
    assert "pip-audit" in workflow
    assert "release_audit.py" in workflow
    assert "clean_machine_smoke.sh" in workflow
    assert "legacy_schema_upgrade_smoke.sh" in workflow
    assert "trivy-action" in workflow
    assert 'service_container="${{ job.services.postgres.id }}"' in workflow
    assert "tools/verify_pgvector_image.sh \"${expected_image}\" \"${service_container}\"" in workflow
    assert f'expected_image="pgvector/pgvector@{PGVECTOR_DIGEST}"' in workflow
    assert "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02" in workflow
    assert "raw_logs_uploaded\":false" in workflow
    assert "retention-days: 1" in workflow
    assert workflow.count("fetch-depth: 0") == 2

    audit = AUDIT.read_text(encoding="utf-8")
    assert '"rev-list", "--objects", "--all"' in audit
    assert '"rev-list", "--all"' in audit
    assert '"for-each-ref", "--format=%(refname)"' in audit
    assert 'source="commit_metadata"' in audit
    assert '"rev-parse", "--is-shallow-repository"' in audit

    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    assert "RUN rm -f /usr/local/bin/gosu" in dockerfile
    assert "USER postgres" in dockerfile
    compose = COMPOSE.read_text(encoding="utf-8")
    assert "build:" in compose
    assert "dockerfile: Dockerfile" in compose
    assert "image: shiori-pgvector:local" in compose
    assert "command:" not in compose
    assert "session-memory-pgdata:/var/lib/postgresql/data" in compose
    assert "com.shiori.scope: project-owned" in compose
    assert "SHIORI_PG_DATA_DIR" not in compose
    workflow_container = workflow[workflow.index("  container_scan:") :]
    assert "docker build" not in workflow_container
    assert "build --pull session-memory-pg" in workflow_container
    assert "tools/container_runtime_smoke.sh --project" in workflow_container
    assert "docker compose --file deploy/docker-compose.yml" in workflow_container
    assert "docker image inspect shiori-pgvector:local" in workflow_container
    assert "image-ref: shiori-pgvector:local" in workflow_container
    assert 'project="shiori-ci-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"' in workflow_container
    assert 'echo "SHIORI_CONTAINER_PROJECT=${project}" >> "${GITHUB_ENV}"' in workflow_container
    assert "SHIORI_PG_DATA_DIR" not in workflow_container
    assert "SHIORI_CONTAINER_ROOT" not in workflow_container
    assert "runner.temp" not in workflow_container
    assert "down --volumes --remove-orphans" in workflow_container
    run_script = RUN_SCRIPT.read_text(encoding="utf-8")
    assert "SHIORI_COMPOSE_PROJECT" in run_script
    assert "COMPOSE_PROJECT_NAME" in run_script
    assert "SHIORI_PG_DATA_DIR" not in run_script
    runtime_smoke = RUNTIME_SMOKE.read_text(encoding="utf-8")
    assert "--volumes --remove-orphans" in runtime_smoke
    assert "label=com.docker.compose.project" in runtime_smoke
    assert "com.shiori.scope" in runtime_smoke
    assert "SHIORI_PG_DATA_DIR" not in runtime_smoke
    assert runtime_smoke.index("existing_containers=") < runtime_smoke.index("trap cleanup EXIT")
    assert "created=false" in runtime_smoke
    assert "started=false" in runtime_smoke
    assert '"${created}" == true' in runtime_smoke
    assert "CREATE EXTENSION IF NOT EXISTS vector" in runtime_smoke
    assert "SELECT count(*) FROM shiori_container_smoke" in runtime_smoke
    assert "shared_preload_libraries=vector" in runtime_smoke
    assert "id -u" in runtime_smoke
    clean_smoke = CLEAN_SMOKE.read_text(encoding="utf-8")
    assert 'export SHIORI_PG_CRED="${credential_file}"' in clean_smoke
    assert 'unset SHIORI_DATABASE_DSN' in clean_smoke
    assert 'chmod 600 "${credential_file}"' in clean_smoke


def test_manifest_contains_runtime_release_references() -> None:
    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")

    assert "recursive-include shiori *.sql" in manifest
    assert "include tools/legacy_schema_upgrade_smoke.sh" in manifest
    assert "include tools/verify_pgvector_image.sh" in manifest
    assert "include tools/container_runtime_smoke.sh" in manifest
    assert "include tools/e2e_replay_smoke.sh" in manifest
    assert "include tools/db_count.py" in manifest
    assert "recursive-include tools/e2e-replay-sessions *.jsonl" in manifest
    assert "recursive-include tests/fixtures/replay *.jsonl *.json" in manifest
    assert "recursive-include docs *.md" in manifest
    assert "include THIRD_PARTY_NOTICES.md" in manifest


def test_schema_sql_ships_as_package_data() -> None:
    """schema.sql must ship inside the wheel so a fresh-DB migrate on an
    installed package resolves it (regression for the pre-existing wheel gap)."""
    import tomllib

    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    package_data = pyproject["tool"]["setuptools"]["package-data"]
    assert "schema.sql" in package_data.get("shiori", [])
    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    assert "recursive-include shiori *.sql" in manifest
    assert (ROOT / "shiori" / "schema.sql").is_file()


def test_schema_sql_resolvable_from_package() -> None:
    import shiori.schema_migrations as schema_migrations

    schema_path = schema_migrations._schema_sql_path()
    assert schema_path.is_file(), f"schema.sql not resolvable from package: {schema_path}"
    assert schema_path.name == "schema.sql"
    text = schema_path.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS session_chunks" in text
