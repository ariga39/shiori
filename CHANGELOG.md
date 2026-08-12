# Changelog

<!-- towncrier release notes start -->

## 0.1.0 (unreleased)

### Added

- Established the installable project foundation and isolated test environment.
- Added bounded CLI search and a read-only MCP server.
- Added versioned PostgreSQL migrations, health checks, backup, and restore.
- Added ingestion privacy controls, retention, export, and deletion.
- Renamed the product to Shiori with data-safe compatibility.
- Added a synthetic retrieval-quality benchmark and reproducible local vectors.
- Added real installed-wheel end-to-end ingestion, restart, CLI, and MCP coverage.
- Added production-pipeline retrieval ablations and external sanity adapters.
- Added typed source, session, and time filters across query, CLI, and MCP.
- Applied temporal ranking only when the query expresses time intent.
- Preserved provenance while deduplicating true duplicate results.
- Added opt-in explainable retrieval evidence without changing default responses.
- Added a one-source MkDocs site and deterministic llms.txt index.
