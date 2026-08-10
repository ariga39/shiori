"""Phase 4A rename contract tests.

The product surface is canonical ``shiori`` / ``Shiori`` / ``SHIORI_*`` after
the rename from ``shiyi``.  These tests prove the installed CLI/package surface
is shiori-only and that no old brand leaks into the repo tree outside explicit
legacy-compatibility fixtures (config aliases and migration forward-conversion).
"""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"


def _walk_repo():
    ignored = {".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache", "dist", "build"}
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file():
            continue
        if any(part in ignored for part in path.parts):
            continue
        if path.name.endswith(".egg-info"):
            continue
        yield path


def test_pyproject_names_and_script_are_shiori_only() -> None:
    with PYPROJECT.open("rb") as fh:
        data = tomllib.load(fh)

    assert data["project"]["name"] == "shiori"
    scripts = data["project"]["scripts"]
    assert set(scripts) == {"shiori"}
    assert scripts["shiori"] == "shiori.cli:main"
    assert data["tool"]["setuptools"]["packages"]["find"]["include"] == ["shiori*"]
    assert "shiori" in data["tool"]["pyright"]["include"]


def test_no_old_brand_outside_legacy_fixtures() -> None:
    """The repo tree must not mention ``shiyi``/``SHIYI`` except in explicit
    legacy-compatibility fixtures (config aliases and migration forward-
    conversion code/tests)."""
    allowed_paths = {
        # config aliases: legacy SHIYI_* env + [shiyi] section compat
        ROOT / "shiori" / "config.py",
        # migration ledger forward conversion
        ROOT / "shiori" / "migrations.py",
        # schema comparison notes reference the legacy ledger name
        ROOT / "shiori" / "schema_migrations" / "__init__.py",
        # explicit legacy-compat tests
        ROOT / "tests" / "test_config_and_cli.py",
        ROOT / "tests" / "test_migrations.py",
        # this test asserts on the old brand (allows it to reference shiyi)
        ROOT / "tests" / "test_rename_contract.py",
    }
    offenders = []
    for path in _walk_repo():
        if path in allowed_paths:
            continue
        if path.suffix == ".pyc":
            continue
        if path.name.endswith(".egg-info"):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "shiyi" in text or "SHIYI" in text:
            offenders.append(str(path.relative_to(ROOT)))

    assert offenders == []


def test_cli_help_mentions_shiori_not_shiyi() -> None:
    from shiori.cli import _build_parser

    parser = _build_parser()
    assert parser.prog == "shiori"
    help_text = parser.format_help()
    assert "shiyi" not in help_text
    assert "shiori" in help_text


def test_top_level_modules_import_from_shiori_package() -> None:
    import ingest
    import ingest_discord
    import ingest_hermes
    import mcp_server
    import query

    for module in (ingest, ingest_discord, ingest_hermes, mcp_server, query):
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "from shiyi" not in source
        assert "import shiyi" not in source
        assert "from shiori" in source or "shiori." in source
