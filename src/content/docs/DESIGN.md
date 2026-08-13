---
title: Design
description: Shiori turns conversation history into semantically searchable memory through an ingestion and query pipeline.
---

Shiori turns conversation history into semantically searchable memory through an ingestion and query pipeline. This document describes the current architecture. Historical material from the earlier `session-memory-pg` implementation (old script names, `python3 ingest.py`/`query.py` invocations, and OpenClaw default paths) is preserved below only as clearly labeled legacy context and is not the current main path. The installation, configuration, and runtime contracts are authoritative in the root `README.md`, `pyproject.toml`, `shiori.config.Settings`, and `CONFIGURATION`.

## 1. Entry points and read/write boundary

The installed `shiori` command is the current main path:

- `shiori ingest --source <sessions|hermes|discord> [--file <path>] [--dry-run]` writes managed rows.
- `shiori query [--limit/-n] [--explain]` reads the searchable memory.
- `shiori serve` starts the local read-only MCP stdio server exposing one `search` tool.
- `shiori db migrate|health|backup|restore` manages schema and portable snapshots.
- `shiori privacy retention-check|export|delete|providers` implements the lifecycle contract.

The MCP surface is strictly read-only: it cannot ingest, migrate, delete, export, or modify source data. The original root-level scripts (`ingest.py`, `ingest_discord.py`, `ingest_hermes.py`, `query.py`, `mcp_server.py`) remain as compatibility wrappers that accept the same `--config` and `--legacy-openclaw` switches; new deployments use the installed `shiori` command.

## 2. Data model

### 2.1 `session_chunks` — the memory fragment table

The managed row set used by retrieval. Key fields include `id` (uuid PK), `session_id`, `source_type`, `content`, `embedding` (`vector(1024)`), `embedding_model`, `timestamp_start`/`timestamp_end`, `turn_index_start`/`turn_index_end`, `metadata`, `created_at`, `content_tsvector` (built with the `'simple'` text configuration), and `channel` (Discord only). `embedding_model` and vector dimension participate in compatibility filtering at query time.

### 2.2 `ingestion_state` — the checkpoint table

Records each processed file (`file_path`, `file_mtime`, `file_size`, `processed_offset`, `source_type`, `chunks_created`, `processed_at`) so reprocessing is incremental and idempotent. A partially failed file records size 0 to force a retry on the next run.

### 2.3 `session_facts` — legacy status

A `session_facts` table exists in some live databases (with an HNSW embedding index and category/time/trigram indexes). The ingestion and retrieval pipelines do not use it, while privacy lifecycle operations still count, export, and delete legacy rows. It is recorded here as a legacy structural fact, not an active ingestion or retrieval capability.

## 3. Ingestion pipeline

### 3.1 Source discovery

Every ingest names one source explicitly; nothing is silently discovered.

- sessions: files discovered under the configured sessions root using the real adapter selection rules, with session ids derived from the basename.
- hermes: session data from the configured Hermes SQLite database.
- discord: every `*.jsonl` under the configured discord root maps to `discord-{stem}`; `--file` imports exactly one file.

### 3.2 Parse and filter

Each adapter parses its archive and filters to supported message shapes: sessions and Hermes keep `user`/`assistant` text and drop tool/attachment/empty fragments; Discord keeps ordinary/reply messages and formats `[timestamp] author: content` with attachment/embed markers. Sensitive shapes are redacted by `shiori.privacy.minimize` before storage (see the privacy lifecycle below).

### 3.3 Token chunking

Text is split into fixed-size chunks (`CHUNK_TOKENS` with `CHUNK_OVERLAP`) using a tokenizer, mapping token offsets back to character ranges so each chunk records its covered `timestamp_start/end` and `turn_index_start/end`.

### 3.4 Embedding and validation

Chunks are embedded through the configured provider (production uses Voyage with `EMBED_DIM = 1024`), batched with bounded retries and rate limiting. Responses must be finite vectors of the configured dimension; provider/model mismatches fail closed rather than mixing incompatible vectors. The deterministic `fake` provider is opt-in only, never implicitly selected, and never used for production data.

### 3.5 Atomic storage, checkpoints, and locks

- Storage is all-or-nothing per session: the new batch is written only if the whole batch embedded successfully, giving "replace entirely or change nothing" semantics.
- Each insert is savepoint-protected; any failure rolls back the whole batch.
- `ingestion_state` checkpoints the file mtime/size so unchanged files are skipped and changed files reprocessed.
- A PostgreSQL advisory lock serializes concurrent runs of the same command.

## 4. Retrieval

### 4.1 Candidate channels

Hybrid retrieval builds candidates from three channels:

- dense: pgvector cosine similarity over the query embedding;
- lexical: PostgreSQL full-text ranking (`ts_rank_cd` over `content_tsvector`, trigram fallback for degenerate input);
- exact: substring matching for short queries.

Rows from a different embedding model or vector dimension are excluded rather than silently mixed into a page. Candidate pools are bounded so resources cannot grow without limit.

### 4.2 RRF fusion

Channel rankings are combined by reciprocal rank fusion (`score += 1/(k + rank)`), a ranking signal, not a correctness probability.

### 4.3 Intent-gated temporal decay

Decay applies only under explicit time intent (structured time bounds or a bounded recency grammar); ordinary fact/history queries receive no decay. The decay formula and half-life are unchanged from the frozen contract.

### 4.4 Provenance-preserving dedup

Near-duplicate suppression drops embeddings similar beyond a fixed threshold while preserving provenance-bearing distinct fragments.

### 4.5 Bounded pagination

Pages cap the result count and bound the offset. Pagination reports `has_more` plus a stable `next_offset` using a one-row look-ahead instead of an unbounded count query.

### 4.6 Opt-in explanation

`shiori query --explain` (or MCP `search` with `explain:true`) reports per-result `score_kind`, `adjustments`, `channels` (with `matched` and `candidate_rank`), `matched_channel_count`, and `multi_channel`, plus a page-level `explain_summary`. Explain fields describe retrieval ranking and corroboration; none is a probability, confidence score, or hard threshold. Diagnostics go to stderr on the CLI so stdout remains pipe-clean.

## 5. Database, container, credentials, and privacy lifecycle

- Schema is managed by forward-only migrations recorded in `shiori_schema_migrations`, applied by `shiori db migrate` in isolated transactions serialized by an advisory lock. `schema.sql` is a legacy one-shot bootstrap reference; legacy structures are verified and recorded rather than replayed.
- The container image is a pinned `pgvector/pg17` build used by compose with a project-scoped named volume, non-root `postgres` user, and `vector` preload. `shiori db backup`/`restore` provide portable snapshots; host data directories are never bound.
- Credentials are explicit: a DSN or a mode-0600 `key=value` file. There is no home-directory credential fallback.
- The privacy lifecycle acts only on managed rows and leaves external source files byte-for-byte unchanged: `retention-check` reports age, `export` writes a deterministic atomic artifact without embeddings/tsvectors/secrets, `delete` is a single idempotent transaction, and `providers` discloses endpoints and `configured`/`not_configured` status.

## 6. Known limitations and future direction

- `session_facts` has no active source references; memory granularity is currently fragments, not structured facts.
- Keyword ranking uses PostgreSQL full-text ranking rather than a true BM25 implementation.
- Embedding comparison for dedup happens in Python for the candidate window, which is fine at current scale but could be pushed into pgvector.
- Connections are created per command; a pool would reduce overhead under high-frequency querying.
- Only text is indexed; images, attachments, and tool-call bodies are dropped (attachments may retain a placeholder for Discord).

Future directions include structured fact extraction, vectorized MMR, connection pooling, embedding caching by content hash, and monitoring/alerting over ingestion failures, provider latency, query latency, and index growth.

## 7. Key constants

| Constant | Value | Notes |
| --- | --- | --- |
| `CHUNK_TOKENS` / `CHUNK_OVERLAP` | 400 / 80 | token chunking window |
| `EMBED_DIM` | 1024 | `vector(1024)` schema |
| `HALF_LIFE_DAYS` | 30 | temporal decay half-life |
| RRF `k` | 60 | fusion constant |
| MMR similarity threshold | 0.85 | near-duplicate suppression |
| MCP `limit` cap | 20 | max results per page |
| MCP `offset` bound | 0..255 | bounded pagination |
| fake provider namespace | `shiori-fake-*` | rejected in production |

All technical literals (commands, environment variables, constants, error codes) are authoritative in the current source and root documentation; this document summarizes them without inventing new contracts.
