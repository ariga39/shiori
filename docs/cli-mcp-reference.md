# CLI and MCP reference

Shiori provides an installed command-line interface and a local, read-only MCP
stdio server. Both use the same configured search service, but their pagination
surfaces are intentionally different.

## CLI commands

Ingest always names a configured source explicitly:

```bash
shiori ingest --source sessions
```

Search indexed memory or start the MCP server with:

```bash
shiori query 'what did we decide?'
shiori serve
```

Database and privacy lifecycle commands are available under `shiori db` and
`shiori privacy`.

## Query options

The installed `shiori query` command accepts `--limit` (also `-n`), repeatable
source/session filters, inclusive/exclusive RFC3339 time bounds, and the opt-in
`--explain` diagnostic. The installed CLI returns the first bounded page; it
does not expose an offset flag.

Explain diagnostics go to stderr so normal result text remains pipeable. The
reported RRF score and channel matches describe retrieval ranking and
corroboration; they are not a correctness probability.

## MCP search

The MCP server exposes one tool named `search`. Its input includes the query,
`limit`, bare `offset`, the same structured filters, and optional `explain`.
The response includes `results`, `count`, `limit`, `offset`, `has_more`, and
`next_offset`. When explanation is enabled it also includes the additive
explanation fields documented in the [design](DESIGN.md#571-explainable-retrieval-phase-4f1-task-39).

The MCP surface is read-only: it cannot ingest, migrate, delete, export, or
modify source data.

## Limits and errors

MCP pages accept at most 20 results. The offset is bounded from 0 through 255,
and pagination reports `has_more` plus a stable `next_offset` rather than an
unbounded count query.

Invalid input, configuration failures, provider failures, and database errors
return stable error codes. Responses do not expose backend exception text,
credentials, or connection details.
