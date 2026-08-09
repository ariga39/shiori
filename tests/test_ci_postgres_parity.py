from pathlib import Path

WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "ci.yml"


def test_ci_enables_and_verifies_pgvector_preload_before_database_setup():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    preload = workflow.index("name: Enable pgvector preload")
    prepare = workflow.index("name: Prepare isolated database")
    block = workflow[preload:prepare]

    assert "ALTER SYSTEM SET shared_preload_libraries = 'vector'" in block
    assert "docker ps --filter 'ancestor=pgvector/pgvector:pg17'" in block
    assert "docker restart \"${containers[0]}\"" in block
    assert "pg_isready --host 127.0.0.1 --port 5432" in block
    assert "SHOW shared_preload_libraries;" in block
    assert "grep -Eq '(^|[,[:space:]])vector([,[:space:]]|$)'" in block


def test_ci_preload_gate_is_fail_closed():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    preload = workflow.index("name: Enable pgvector preload")
    prepare = workflow.index("name: Prepare isolated database")
    block = workflow[preload:prepare]

    assert "exit 1" in block
    assert "if (( ${#containers[@]} != 1 )); then" in block
    assert "if [[ \"${ready}\" != true ]]; then" in block
    assert "if ! grep -Eq" in block
