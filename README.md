# shiori (拾遗)

Searchable long-term memory for AI agents. Shiori ingests explicitly selected
session or archive sources into PostgreSQL + pgvector, then exposes hybrid
vector/BM25 search through a CLI and a read-only MCP server.

## Install

The supported installation is a normal Python package. Python 3.11–3.13 is
supported; `uv` is used in this repository for reproducible development.

```bash
uv sync --locked --extra dev
uv run shiori --help
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
2. `SHIORI_*` environment variables;
3. the selected JSON/TOML file (`--config` or `SHIORI_CONFIG_FILE`);
4. safe numeric defaults for chunking, retries, and lock IDs only.

Minimal environment example (use a secret manager or a mode-0600 file for
the key; never commit or paste it):

```bash
export SHIORI_SESSIONS_DIR=/srv/shiori/sessions
export SHIORI_DATABASE_DSN='postgresql://user:password@db.example/shiori'
export SHIORI_EMBEDDING_PROVIDER=voyage
export SHIORI_VOYAGE_API_KEY='provided-by-your-secret-manager'
export SHIORI_VOYAGE_MODEL=voyage-4-large
export SHIORI_EMBED_DIM=1024
```

Instead of putting a key in the environment, set
`SHIORI_VOYAGE_KEY_FILE=/secure/shiori/voyage.key`. PostgreSQL may use a DSN or
an explicit `SHIORI_PG_CRED` key/value file. Diagnostics redact API keys and
DSN passwords.

For an isolated local or CI smoke run only, the provider can be explicitly
replaced by deterministic local vectors. This is opt-in and never sends
content over the network:

```bash
export SHIORI_EMBEDDING_PROVIDER=fake
export SHIORI_ALLOW_FAKE_EMBEDDINGS=true
export SHIORI_ENVIRONMENT=development
export SHIORI_VOYAGE_MODEL=shiori-fake-v1
export SHIORI_EMBED_DIM=1024
```

The fake model must use the reserved `shiori-fake-*` namespace. Search filters
both model and vector dimension, and production rejects that namespace, so
fake and Voyage vectors cannot be silently mixed. Do not use the fake provider
for production data or performance evaluation.

The old OpenClaw/Hermes paths are available only with the explicit
`--legacy-openclaw` migration switch. That switch is for a deliberate,
temporary migration and does not change the default configuration.

## CLI and MCP

The source is selected on every ingest invocation; no source is silently
discovered:

```bash
shiori ingest --source sessions
shiori ingest --source hermes
shiori ingest --source discord --file /srv/shiori/archive/channel.jsonl
shiori ingest --source sessions --dry-run
shiori query 'what did we decide about X?' --limit 5
shiori serve
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
use the installed `shiori` command.

## PostgreSQL + pgvector

The local deployment builds the repository `Dockerfile`, whose `pgvector/pg17`
base is pinned to
`sha256:7ae6051efd0e60444282c27c7e141af07f322ce033300e727a49c3dd11075e38`.
Compose runs that same local image; it does not pull or publish an image and
uses a project-scoped named volume for PostgreSQL data. The volume is neither
external nor assigned a shared fixed name; Compose derives its name from the
project name. Set `SHIORI_COMPOSE_PROJECT` when you need an explicit local
namespace, and use `SHIORI_PG_PORT` to change the host port. The deployment does
not accept a host `SHIORI_PG_DATA_DIR` bind path, because arbitrary host UID
ownership is not portable for the non-root database image.

```bash
SHIORI_PG_CRED=/secure/shiori/postgres.env \
  SHIORI_COMPOSE_PROJECT=shiori-local \
  ./deploy/run.sh up -d --build
shiori db migrate        # apply forward-only migrations (recommended)
```

The first `up` must include `--build` so the compose path exercises the pinned
Dockerfile. The named volume keeps rows across container restarts and is
removed only when you explicitly use `docker compose down --volumes` for the
same project. The resulting container runs as the non-root `postgres` user and
preloads `vector`. For portable data export or migration to another project,
use `shiori db backup` and `shiori db restore`; do not copy a host data directory.
CI runs the same compose build/runtime smoke and scans that exact local image;
it never pushes the image.

Schema is managed by **forward-only migrations** (`shiori/schema_migrations/`,
recorded in `shiori_schema_migrations`). Run `shiori db migrate` on a fresh or
existing database; it applies only unapplied migrations, each in its own
transaction and serialized by a PostgreSQL advisory lock. `shiori db health`
reports repository version/table/extension status and distinguishes
`uninitialized / partial / current / drifted / ahead`; a database ahead of the
code head rejects writes. `shiori db backup <path>` writes a pg_dump file (with
a sidecar manifest+digest) via 0600 temp + atomic rename and refuses overwrite
or symlink targets. `shiori db restore <src> --target <newdb>`
restores into a freshly created, random-marker staging database only — it never
overwrites the current database and returns a password-free DSN for you to
switch to. `schema.sql` remains a legacy one-shot bootstrap reference. When an
existing database has the complete canonical legacy structure, `shiori db
migrate` verifies it and records the initial migration without replaying DDL;
partial or drifted legacy structures fail closed. New installations and
upgrades use the CLI migration command above. CI separately exercises the
legacy bootstrap-to-head path in an isolated database.

The credential file is explicit `key=value` data with `dbname`, `user`, and
`password` entries. `deploy/run.sh` never prints its contents and does not
look in a home-directory fallback.

## Tests and CI

The default test run is safe without PostgreSQL: database tests skip unless
all three opt-in variables are present:

```text
SHIORI_TEST_DATABASE_DSN
SHIORI_TEST_DATABASE_NAME
SHIORI_TEST_DATABASE_MARKER
```

When enabled, the test fixture verifies both the connected database name and
a marker row before any test cleanup. It deletes only rows in its own reserved
`test-<run-id>` namespace. CI creates a random database and marker on its
ephemeral PostgreSQL service, applies the checked-in migrations through
`shiori db migrate`, runs the same suite, and separately upgrades a synthetic
`schema.sql` database through the same CLI command. The legacy fixture is
never used as a shortcut for the fresh-database path.
Embedding unit tests use deterministic synthetic vectors; production never
selects a fake provider implicitly.

```bash
uv run ruff check .
uv run pyright
uv run pytest -q
```

The repository also carries a clean-machine lifecycle harness. Hosted CI runs
the exact installed-wheel commands for migration, health, three synthetic
source adapters, fake-provider search, privacy export/delete, backup/restore,
and the real MCP stdio boundary; it uses a fresh temporary `HOME` and an
isolated PostgreSQL database. The harness is synthetic and does not read host
credentials or real source data:

```bash
SHIORI_TEST_DATABASE_MARKER=ci-local-1-1-1 \
  tools/clean_machine_smoke.sh --cli /path/to/venv/bin/shiori \
  --python /path/to/venv/bin/python \
  --dsn postgresql://user@127.0.0.1:5432/shiori_test \
  --database-name shiori_test --workdir /tmp/shiori-smoke
```

Release evidence is separated into these gates:

| Gate | Local | Hosted CI | Not executed locally |
| --- | --- | --- | --- |
| locked install, lint, type, unit tests | proven when the commands above pass | required | — |
| PostgreSQL/pgvector migrations and lifecycle | requires an explicitly isolated database | required | if no local database is provided |
| dependency vulnerability audit | `uv export --locked` + pinned `pip-audit` | required | — |
| reachable history/artifact secret and PII audit | `uv run python tools/release_audit.py` | required | — |
| pinned Docker build and HIGH/CRITICAL scan | requires Docker | required | when Docker is unavailable |

The private release candidate is not a public release: CI does not create a
tag, publish a package, push an image, deploy, register an external service,
or change repository visibility.
The complete gate list and known limitations are recorded in
`docs/RELEASE_CHECKLIST.md`.

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
