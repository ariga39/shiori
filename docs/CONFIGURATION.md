---
title: Configuration contract
description: This is the current runtime contract. The internal architecture/DESIGN.md is a historical implementation record and is not a source of default paths.
slug: CONFIGURATION
---

This is the current runtime contract. The internal `architecture/DESIGN.md` is a
historical implementation record and is not a source of default paths.

## Resolution and validation

`shiori.config.load_config()` resolves values in this order:

1. explicit Python/API overrides;
2. `SHIORI_*` environment variables;
3. an explicitly selected JSON/TOML file;
4. safe non-secret numeric defaults.

The following are intentionally unset until the operator supplies them:

- session, Hermes SQLite, and Discord archive paths;
- PostgreSQL DSN or an explicit key/value credential file;
- embedding provider, key/key file, model, and vector dimension.

The installed CLI validates these requirements before opening PostgreSQL or
calling the embedding service. Failures use stable `error[code]: ...` output.
`Settings.redacted()` and `config_summary()` replace API keys and DSN
passwords before diagnostics are rendered.

`--legacy-openclaw` is an explicit migration mode. It supplies the legacy
source/credential paths only when the corresponding `SHIORI_*` values are not
already set; normal invocations never inspect those locations.

## Source selection

Every ingest command names one source:

```text
shiori ingest --source sessions
shiori ingest --source hermes
shiori ingest --source discord --file /path/to/archive.jsonl
```

The source path is passed through `SHIORI_SESSIONS_DIR`, `SHIORI_HERMES_DB`, or
`SHIORI_DISCORD_ARCHIVE_DIR`. `--file` is an explicit one-file Discord import
and does not enable directory discovery.

## PostgreSQL credentials

Use either:

- `SHIORI_DATABASE_DSN` / `SHIORI_DATABASE_URL`; or
- `SHIORI_PG_CRED`, pointing to a mode-0600 `key=value` file with `host`,
  `port`, `dbname`, `user`, and `password` (or a `dsn` entry).

There is no home-directory credential fallback. The Docker helper accepts the
same explicit file through `SHIORI_PG_CRED`, or accepts synthetic
`POSTGRES_DB`, `POSTGRES_USER`, and `POSTGRES_PASSWORD` variables supplied by
the caller.

## Embeddings

Production requires `SHIORI_EMBEDDING_PROVIDER=voyage`, one of
`SHIORI_VOYAGE_API_KEY` / `SHIORI_VOYAGE_KEY_FILE`,
`SHIORI_VOYAGE_MODEL`, and `SHIORI_EMBED_DIM`. The endpoint can be overridden
with `SHIORI_VOYAGE_API_URL`; the legacy switch supplies the historical Voyage
endpoint/model/dimension explicitly for migration.

The current PostgreSQL schema is `vector(1024)`, so `SHIORI_EMBED_DIM` must be
`1024` until a schema migration adds another dimension.

Tests use deterministic in-memory vectors and never use a production key.
There is no implicit fake provider. An isolated local/CI smoke run may opt in
explicitly with all of the following settings; this provider never performs a
network request and is rejected unless the opt-in flag is true:

```text
SHIORI_EMBEDDING_PROVIDER=fake
SHIORI_ALLOW_FAKE_EMBEDDINGS=true
SHIORI_ENVIRONMENT=development
SHIORI_VOYAGE_MODEL=shiori-fake-v1
SHIORI_EMBED_DIM=1024
```

`fake`, missing provider, missing key, missing model, and missing dimension are
rejected by the production preflight. The fake provider is not a production
embedding substitute; its model must use the reserved `shiori-fake-*`
namespace, which production also rejects. The normal configuration never
enables it.

## Test database isolation

Database tests activate only when all of these are present:

```text
SHIORI_TEST_DATABASE_DSN
SHIORI_TEST_DATABASE_NAME
SHIORI_TEST_DATABASE_MARKER
```

The fixture verifies `current_database()` and the marker row before using the
connection. It deletes only rows under its own `test-<run-id>` namespace. A CI
run creates a random temporary database and marker, applies the checked-in
forward-only migrations through `shiori db migrate`, then drops it in an
`always()` cleanup step. A separate isolated CI fixture applies the historical
`schema.sql` once and verifies that the same CLI command adopts the complete
legacy structure into the migration ledger; partial or drifted legacy schemas
are rejected. The fresh-database fixture does not use `schema.sql` as a
shortcut.
