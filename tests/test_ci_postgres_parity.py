from pathlib import Path

WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "ci.yml"


def test_ci_enables_and_verifies_pgvector_preload_before_database_setup():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    preload = workflow.index("name: Enable pgvector preload")
    prepare = workflow.index("name: Prepare isolated database")
    block = workflow[preload:prepare]

    assert "ALTER SYSTEM SET shared_preload_libraries = 'vector'" in block
    assert 'service_container="${{ job.services.postgres.id }}"' in block
    assert 'tools/verify_pgvector_image.sh "${expected_image}" "${service_container}"' in block
    assert "sha256:7ae6051efd0e60444282c27c7e141af07f322ce033300e727a49c3dd11075e38" in block
    assert 'docker restart "${service_container}"' in block
    assert "pg_isready --host 127.0.0.1 --port 5432" in block
    assert "SHOW shared_preload_libraries;" in block
    assert "grep -Eq '(^|[,[:space:]])vector([,[:space:]]|$)'" in block


def test_ci_preload_gate_is_fail_closed():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    preload = workflow.index("name: Enable pgvector preload")
    prepare = workflow.index("name: Prepare isolated database")
    block = workflow[preload:prepare]

    assert "exit 1" in block
    assert "if [[ ! \"${service_container}\" =~ ^[0-9a-f]{12,64}$ ]]; then" in block
    assert 'tools/verify_pgvector_image.sh "${expected_image}" "${service_container}"' in block
    assert "if [[ \"${ready}\" != true ]]; then" in block
    assert "if ! grep -Eq" in block


def test_ci_verifies_pg_client_server_major_parity():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "Verify pg_dump/pg_restore client matches server major version" in workflow
    assert "Install pinned PostgreSQL 17 client (PGDG)" in workflow
    assert "postgresql-client-17" in workflow
    preload = workflow.index("Verify pg_dump/pg_restore client matches server major version")
    prepare = workflow.index("name: Prepare isolated database")
    block = workflow[preload:prepare]
    assert "for tool in pg_dump pg_restore" in block
    assert '"${tool}" --version' in block
    assert "server_version_num" in block
    assert 'client_major}' in block and 'server_major}' in block
    assert "exit 1" in block
    # The pinned install must verify both client tools against major 17.
    install_idx = workflow.index("Install pinned PostgreSQL 17 client (PGDG)")
    verify_idx = workflow.index("Verify pg_dump/pg_restore client matches server major version")
    install_block = workflow[install_idx:verify_idx]
    assert "postgresql-client-17" in install_block
    assert "pg_dump --version | grep -q ' 17\\.'" in install_block
    assert "pg_restore --version | grep -q ' 17\\.'" in install_block
