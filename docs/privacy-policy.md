# Privacy Policy

Shiyi stores searchable long-term memory for AI agents. This document states
the local data-minimization and lifecycle contract that the ingestion and
privacy seams enforce.

## Data minimization (fail-closed)

- Every message text entering the store passes through `shiyi.privacy.minimize`
  at the extraction seam (`ingest.extract_text_from_message` for sessions and
  Hermes, `ingest_discord.format_message` for Discord).
- Recognized sensitive shapes are redacted before storage:
  - provider live API tokens (`sk_`/`pk_`/`rk_` + `_live_`),
  - email addresses,
  - absolute filesystem paths.
- Redaction is forced on: the CLI `ingest --redact` flag defaults to enabled and
  cannot be turned off, so a misconfiguration cannot silently store PII.
- Values that cannot be positively classified are kept; anything positively
  sensitive is never echoed.

## Retention

Each source declares a positive retention window:

| source   | kind    | retention_days |
| -------- | ------- | -------------- |
| sessions | jsonl   | 90             |
| hermes   | sqlite  | 90             |
| discord  | jsonl   | 30             |

`shiyi privacy providers` discloses endpoint, data flow, retention, and
local-only status for every registered source.

## Export and delete (fail-closed)

- `shiyi privacy export --scope <s>` performs no side effect without explicit
  confirmation; it reports a dry-run otherwise.
- `shiyi privacy delete --scope <s>` performs no removal without explicit
  confirmation.

## Provider disclosure

- `shiyi privacy providers` lists each source's provider endpoint and data flow
  so operators can verify handling without reading source code.
- All current sources are read from local data (JSONL / SQLite); no source sends
  stored content to a third party at ingestion time except the configured
  embedding provider, which is disclosed per source.
