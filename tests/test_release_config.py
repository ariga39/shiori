from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


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
    assert "include CONTRIBUTING.md" in manifest
    assert "recursive-include architecture *.md" in manifest
    assert "recursive-include maintainers *.md" in manifest
    assert "src/content/docs" not in manifest
    assert "include THIRD_PARTY_NOTICES.md" in manifest

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "`maintainers/RELEASE_CHECKLIST.md`" in readme
    assert "src/content/docs/RELEASE_CHECKLIST.md" not in readme


def test_repository_docs_reference_the_contained_site_project() -> None:
    """Contributor, maintainer, and reference docs must use the split layout."""
    contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    checklist = (ROOT / "maintainers" / "RELEASE_CHECKLIST.md").read_text(encoding="utf-8")
    configuration = (ROOT / "docs" / "CONFIGURATION.md").read_text(encoding="utf-8")
    configuration_zh = (ROOT / "docs" / "zh-cn" / "CONFIGURATION.md").read_text(encoding="utf-8")
    cli_reference = (ROOT / "docs" / "cli-mcp-reference.md").read_text(encoding="utf-8")
    cli_reference_zh = (ROOT / "docs" / "zh-cn" / "cli-mcp-reference.md").read_text(encoding="utf-8")
    schema = (ROOT / "shiori" / "schema.sql").read_text(encoding="utf-8")

    assert "npm --prefix docs-site run docs:build" in contributing
    assert "Markdown under `docs/`" in contributing
    assert "src/content/docs" not in contributing
    assert "npm --prefix docs-site ci" in checklist
    assert "npm --prefix docs-site run docs:build -- --outDir <temp>" in checklist

    for page in (configuration, configuration_zh):
        assert "architecture/DESIGN.md" in page
        assert "docs/DESIGN.md" not in page
    for page in (cli_reference, cli_reference_zh):
        assert "../DESIGN/" not in page
    assert "architecture/DESIGN.md" in schema
    assert "docs/DESIGN.md" not in schema


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


def test_ci_verifies_bilingual_docs_and_workers_bundle_with_locked_node_dependencies() -> None:
    """CI must install locked Node deps and verify the docs and Workers bundle."""
    workflow = WORKFLOW.read_text(encoding="utf-8")

    expected = [
        "uses: actions/setup-node@820762786026740c76f36085b0efc47a31fe5020 # v7.0.0",
        "node-version: '26.7.0'",
        "cache: npm",
        "cache-dependency-path: docs-site/package-lock.json",
        "Install locked documentation dependencies",
        "run: npm --prefix docs-site ci",
        "Check documentation site and LLM index",
        'uv run python tools/docs_check.py --site-dir "${RUNNER_TEMP}/shiori-docs-site"',
        "Verify Cloudflare Workers Static Assets bundle",
        'npm --prefix docs-site run docs:workers:dry-run -- --outdir "${RUNNER_TEMP}/shiori-workers-bundle"',
        "uv run python tools/release_audit.py --root . --artifact-dir docs-site/dist",
    ]
    positions = [workflow.find(literal) for literal in expected]
    assert all(position >= 0 for position in positions), positions
    assert positions == sorted(positions), positions
    assert "wrangler deploy --config" not in workflow
