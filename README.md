# shiyi (拾遗)

Searchable long-term memory for AI agents. Shiyi ingests explicitly selected
session or archive sources into PostgreSQL + pgvector, then exposes hybrid
vector/BM25 search through a CLI and a read-only MCP server.

## Install

The supported installation is a normal Python package. Python 3.11–3.13 is
supported; `uv` is used in this repository for reproducible development.

```bash
uv sync --extra dev
uv run shiyi --help
```

The package can also be built and installed by ordinary PEP 517 tooling:

```bash
uv build
python -m pip install dist/*.whl
```

## Configure explicitly

There are no implicit source, database, credential, or embedding-provider
paths. A command must identify its source and production embedding settings.
Configuration precedence is:

1. explicit keyword values used by the Python API;
2. `SHIYI_*` environment variables;
3. the selected JSON/TOML file (`--config` or `SHIYI_CONFIG_FILE`);
4. safe numeric defaults for chunking, retries, and lock IDs only.

Minimal environment example (use a secret manager or a mode-0600 file for
the key; never commit or paste it):

```bash
export SHIYI_SESSIONS_DIR=/srv/shiyi/sessions
export SHIYI_DATABASE_DSN='postgresql://user:password@db.example/shiyi'
export SHIYI_EMBEDDING_PROVIDER=voyage
export SHIYI_VOYAGE_API_KEY='provided-by-your-secret-manager'
export SHIYI_VOYAGE_MODEL=voyage-4-large
export SHIYI_EMBED_DIM=1024
```

Instead of putting a key in the environment, set
`SHIYI_VOYAGE_KEY_FILE=/secure/shiyi/voyage.key`. PostgreSQL may use a DSN or
an explicit `SHIYI_PG_CRED` key/value file. Diagnostics redact API keys and
DSN passwords.

The old OpenClaw/Hermes paths are available only with the explicit
`--legacy-openclaw` migration switch. That switch is for a deliberate,
temporary migration and does not change the default configuration.

## CLI and MCP

The source is selected on every ingest invocation; no source is silently
discovered:

```bash
shiyi ingest --source sessions
shiyi ingest --source hermes
shiyi ingest --source discord --file /srv/shiyi/archive/channel.jsonl
shiyi ingest --source sessions --dry-run
shiyi query 'what did we decide about X?' --limit 5
shiyi serve
```

`--dry-run` parses and chunks the selected source without opening PostgreSQL
or calling the embedding provider. Normal commands fail with a structured
error if the source, database, provider, key, model, or dimension is absent.
The MCP server exposes only the read-only `search` tool. Its `limit` is capped
at 20 and its `offset` is bounded; each page reports `has_more` and a stable
`next_offset`. Results carry explicit timestamp/session/source provenance plus
the embedding model and dimension used for compatibility filtering. Invalid
input, provider failures, dimension/model mismatches, and database failures
return stable error codes without backend text or credentials.

The original script entry points remain as compatibility wrappers and accept
the same `--config` and `--legacy-openclaw` switches. New deployments should
use the installed `shiyi` command.

## PostgreSQL + pgvector

The local deployment uses the official `pgvector/pgvector:pg17` image. It
does not use an external or pre-named Docker volume. The default data directory
is the project-local `.data/postgres` (ignored by Git), and both the port and
data directory can be changed with `SHIYI_PG_PORT` and
`SHIYI_PG_DATA_DIR`.

```bash
SHIYI_PG_CRED=/secure/shiyi/postgres.env ./deploy/run.sh up -d
shiyi db migrate        # apply forward-only migrations (recommended)
psql -h 127.0.0.1 -p 5433 -U <user> -d <db> -f schema.sql   # legacy bootstrap
```

Schema is managed by **forward-only migrations** (`shiyi/schema_migrations/`,
recorded in `shiyi_schema_migrations`). Run `shiyi db migrate` on a fresh or
existing database; it applies only unapplied migrations, each in its own
transaction and serialized by a PostgreSQL advisory lock. `shiyi db health`
reports repository version/table/extension status and distinguishes
`uninitialized / partial / current / drifted / ahead`; a database ahead of the
code head rejects writes. `shiyi db backup <path>` writes a pg_dump file (with
a sidecar manifest+digest) via 0600 temp + atomic rename and refuses overwrite
or symlink targets. `shiyi db restore <src> --target <newdb> --marker <m>`
restores into a freshly created, random-marker staging database only — it never
overwrites the current database and returns a password-free DSN for you to
switch to. `schema.sql` remains the legacy one-shot bootstrap reference and
does not repair drift on existing databases.

The credential file is explicit `key=value` data with `dbname`, `user`, and
`password` entries. `deploy/run.sh` never prints its contents and does not
look in a home-directory fallback.

## Tests and CI

The default test run is safe without PostgreSQL: database tests skip unless
all three opt-in variables are present:

```text
SHIYI_TEST_DATABASE_DSN
SHIYI_TEST_DATABASE_NAME
SHIYI_TEST_DATABASE_MARKER
```

When enabled, the test fixture verifies both the connected database name and
a marker row before any test cleanup. It deletes only rows in its own reserved
`test-<run-id>` namespace. CI creates a random database and marker on its
ephemeral PostgreSQL service, applies `schema.sql`, and runs the same suite.
Embedding unit tests use deterministic synthetic vectors; production never
selects a fake provider implicitly.

```bash
uv run ruff check .
uv run pyright
uv run pytest -q
```

## Search behavior

Hybrid retrieval combines semantic vector similarity, PostgreSQL full-text
ranking, and exact substring matching for short queries. Results then apply
temporal decay and MMR-style similarity suppression. The query service caps
page size, offset, candidate rows, and query text length; it uses deterministic
tie-breaks and a one-row look-ahead for truthful pagination. Rows from a
different embedding model or vector dimension are excluded rather than
silently mixed into a result page. The MCP surface performs no ingest or
other data-writing operation.

## License

The project is MIT-licensed in `LICENSE`. Direct dependency notices and the
offline metadata check are in `THIRD_PARTY_NOTICES.md`; MIT does not relicense
those dependencies.
