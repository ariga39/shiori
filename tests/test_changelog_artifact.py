from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHANGELOG = ROOT / "CHANGELOG.md"

EXPECTED_HEADERS = [
    "# Changelog",
    "<!-- towncrier release notes start -->",
    "## 0.1.0 (unreleased)",
    "### Added",
]

EXPECTED_BULLETS = [
    "Established the installable project foundation and isolated test environment.",
    "Added bounded CLI search and a read-only MCP server.",
    "Added versioned PostgreSQL migrations, health checks, backup, and restore.",
    "Added ingestion privacy controls, retention, export, and deletion.",
    "Renamed the product from shiyi to Shiori with data-safe compatibility.",
    "Added a synthetic retrieval-quality benchmark and reproducible local vectors.",
    "Added real installed-wheel end-to-end ingestion, restart, CLI, and MCP coverage.",
    "Added production-pipeline retrieval ablations and external sanity adapters.",
    "Added typed source, session, and time filters across query, CLI, and MCP.",
    "Applied temporal ranking only when the query expresses time intent.",
    "Preserved provenance while deduplicating true duplicate results.",
    "Added opt-in explainable retrieval evidence without changing default responses.",
]


def test_changelog_lists_audited_history_in_order() -> None:
    text = CHANGELOG.read_text(encoding="utf-8")

    position = 0
    for header in EXPECTED_HEADERS:
        index = text.find(header, position)
        assert index >= 0, f"missing header after position {position}: {header!r}"
        position = index + len(header)

    for bullet in EXPECTED_BULLETS:
        index = text.find(bullet, position)
        assert index >= 0, f"missing bullet after position {position}: {bullet!r}"
        position = index + len(bullet)
