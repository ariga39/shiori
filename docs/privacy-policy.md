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
  - GitHub-style tokens (`ghp_`/`gho_`/`ghu_`/`ghs_`/`ghr_`, `github_pat_`),
  - bearer authorization headers,
  - email addresses,
  - absolute filesystem paths and Windows absolute paths.
- Redaction is forced on: the CLI `ingest --redact` flag defaults to enabled and
  cannot be turned off, so a misconfiguration cannot silently store PII.
- Input types that cannot be safely handled (non-string) are rejected with a
  structured `PrivacyError`. This is not a claim that every unrecognized value
  is safe.

## Retention

Each source declares a positive retention window:

| source   | kind    | retention_days |
| -------- | ------- | -------------- |
| sessions | jsonl   | 90             |
| hermes   | sqlite  | 90             |
| discord  | jsonl   | 30             |

`shiyi privacy retention-check --scope <s>` reports the managed-data age of the
scope's rows using the store's aware-UTC `processed_at`/`created_at`, never the
external source file mtimes. It performs no deletion.

## Scope and the managed store

Privacy lifecycle operations act **only** on shiyi's own managed rows
(`session_chunks`, `session_facts`, `ingestion_state`). External source files
(`sessions_dir`, `hermes_db`, `discord_archive_dir`) are read-only provenance:
they are never unlinked, renamed, or rewritten by export or delete.

Scope resolution reuses the existing provenance rules and fails closed with
`scope_evidence_unavailable` when a scope cannot be uniquely attributed:

- sessions: session ids derived from the source basename,
- discord: session ids named `discord-{stem}`,
- hermes: session ids bound via the `hermes://<session_id>` `ingestion_state`
  binding.

## Export

- `shiyi privacy export --scope <s> --dest <p>` returns a dry-run (row count and
  destination) without writing.
- With `--yes`, the export is written atomically (same-directory temp file +
  fsync + chmod 0600 + atomic replace). A destination whose content is already
  identical reports `already_exported`; different content fails closed and is
  never overwritten.
- The artifact is a single deterministic JSON document containing readable
  content, timestamps, and provenance hashes. It never includes embeddings,
  tsvectors, secrets, DSNs, or absolute source paths.

## Delete

- `shiyi privacy delete --scope <s>` returns a dry-run count without touching
  anything.
- With `--yes`, deletion is a single transaction that removes only the managed
  rows bound to the selected scope; any failure rolls back all rows. Repeating
  the delete reports zero (idempotent). `--older-than N` narrows the deletion
  set to managed rows older than N days.
- External source files are byte-for-byte unchanged before and after.

## Provider disclosure

- `shiyi privacy providers` lists each source's provider endpoint, data flow,
  retention window, and local-only status.
- The embedding provider, when configured, is reported with its real endpoint
  and model; when not configured it is reported as `not_configured` rather than
  silently assumed.
