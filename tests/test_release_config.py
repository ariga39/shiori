from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
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


def test_manifest_contains_runtime_release_references() -> None:
    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")

    assert "include schema.sql" in manifest
    assert "include tools/legacy_schema_upgrade_smoke.sh" in manifest
    assert "include tools/verify_pgvector_image.sh" in manifest
    assert "recursive-include docs *.md" in manifest
    assert "include THIRD_PARTY_NOTICES.md" in manifest
