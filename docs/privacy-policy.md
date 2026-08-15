---
title: Privacy Policy
description: This document states the local data-minimization and lifecycle contract that the ingestion and privacy seams enforce.
---

Shiori stores searchable long-term memory for AI agents. This document states
the local data-minimization and lifecycle contract that the ingestion and
privacy seams enforce.

## Data minimization (fail-closed)

- Every message text entering the store passes through `shiori.privacy.minimize`
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

`shiori privacy retention-check --scope <s>` reports the managed-data age of the
scope's rows using the store's aware-UTC `processed_at`/`created_at`, never the
external source file mtimes. It performs no deletion.

## Scope and the managed store

Privacy lifecycle operations act **only** on shiori's own managed rows
(`session_chunks`, `session_facts`, `ingestion_state`). External source files
(`sessions_dir`, `hermes_db`, `discord_archive_dir`) are read-only provenance:
they are never unlinked, renamed, or rewritten by export or delete.

Scope resolution reuses the existing provenance rules and fails closed with
`scope_evidence_unavailable` when a scope cannot be uniquely attributed. It
works with real shapes — absolute source paths, plain discord stems
(`general.jsonl` → `discord-general`), and arbitrary hermes session ids — and
does not depend on any caller-supplied prefix:

- sessions: files discovered under the configured sessions root using the real
  adapter selection rules, session ids derived from the basename,
- discord: every `*.jsonl` under the configured discord root maps to
  `discord-{stem}`,
- hermes: session ids bound via the `hermes://<session_id>` `ingestion_state`
  binding.

`scope=all` resolves sessions, discord, and hermes atomically; if any scope
cannot be unambiguously attributed the whole operation fails closed with zero
side effects. Symlinked or out-of-root provenance is rejected.

## Export

- `shiori privacy export --scope <s> --dest <p>` returns a dry-run (row count and
  destination) without writing.
- With `--yes`, the export is written atomically (same-directory temp file +
  fsync + chmod 0600 + atomic replace). A destination whose content is already
  identical reports `already_exported`; different content fails closed and is
  never overwritten.
- The artifact is a single deterministic JSON document containing readable
  content, timestamps, and provenance hashes. It never includes embeddings,
  tsvectors, secrets, DSNs, or absolute source paths.

## Delete

- `shiori privacy delete --scope <s>` returns a dry-run count without touching
  anything.
- With `--yes`, deletion is a single transaction that removes only the managed
  rows and checkpoints bound to the selected scope's resolved file paths; any
  failure rolls back all rows. Repeating the delete reports zero (idempotent).
  `--older-than N` narrows the deletion set to managed rows whose
  `processed_at` is older than N days.
- External source files are byte-for-byte unchanged before and after.

## Provider disclosure

- `shiori privacy providers` lists each source's provider endpoint, data flow,
  retention window, and local-only status, and reports `configured` or
  `not_configured` for each source and for the embedding provider.
- The embedding provider, when configured, is reported with its real endpoint
  and model; when not configured it is reported as `not_configured` rather than
  silently assumed.
