-- session-memory-pg schema
--
-- Canonical DDL for a fresh `shiori` database. The historical live database had
-- a legacy embedding_model default; fresh installs intentionally require every
-- write to declare its provider/model instead of silently selecting one. An
-- existing database needs an explicit migration before applying this change.
--
-- ⚠️  IMPORTANT (NB-C5-07): every statement uses `IF NOT EXISTS`, which is only
-- a bootstrap guard for an EMPTY database. It does NOT repair drift in an
-- existing database (missing/extra/differently-typed columns are left as-is).
-- Run this file only when creating a fresh database or rebuilding an existing
-- one; for an existing DB you must ALTER the live schema manually to match.
--
-- `timestamp_start` / `timestamp_end` are nullable: in the main ingest path a
-- chunk with an unparseable timestamp gets the file mtime written as a fallback
-- (`fallback_ts`, see ingest.py store_chunks), so NULL only occurs when
-- `fallback_ts` is None (store_chunks' old signature / API default / legacy
-- calls). `metadata` / `created_at` / `processed_at` / `chunks_created` /
-- `facts_created` are nullable in the live schema even though the insert paths
-- always provide values; DDL below preserves that.
--
-- Bootstrap (substitute operator-selected values; never commit credentials):
--   psql -h 127.0.0.1 -p 5433 -U <user> -d <database> -f schema.sql
-- For a fresh database, first create the db and run this file once.

-- ── Extensions (idempotent) ────────────────────────────────────────────────
CREATE EXTENSION IF NOT EXISTS vector;      -- vector(1024) type for embeddings
CREATE EXTENSION IF NOT EXISTS pg_trgm;     -- similarity() / gin_trgm_ops fallback

-- ── session_chunks ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS session_chunks (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id         text NOT NULL,
    source_type        text NOT NULL DEFAULT 'main_user',
    content            text NOT NULL,
    embedding          vector(1024),                        -- nullable: aborted batch
    embedding_model    text NOT NULL,                       -- provider/model is explicit in every write
    timestamp_start    timestamptz,                         -- nullable
    timestamp_end      timestamptz,                         -- nullable
    turn_index_start   integer,
    turn_index_end     integer,
    channel            text,                                -- only Discord sources
    metadata           jsonb DEFAULT '{}'::jsonb,           -- nullable in live
    created_at         timestamptz DEFAULT now(),           -- nullable in live
    content_tsvector   tsvector
);

-- ── ingestion_state (idempotent incremental checkpoints) ──────────────────
CREATE TABLE IF NOT EXISTS ingestion_state (
    file_path        text PRIMARY KEY,
    file_mtime       timestamptz NOT NULL,
    file_size        bigint NOT NULL,
    processed_offset bigint NOT NULL DEFAULT 0,
    processed_at     timestamptz DEFAULT now(),             -- nullable in live
    source_type      text NOT NULL,
    chunks_created   integer DEFAULT 0,                     -- nullable in live
    facts_created    integer DEFAULT 0                      -- nullable in live
);

-- ── session_facts ─────────────────────────────────────────────────────────
-- Present in the live DB (10 columns). No source file references it today;
-- documented here for schema fidelity (see docs/DESIGN.md §3.3).
CREATE TABLE IF NOT EXISTS session_facts (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id       text NOT NULL,
    category         text NOT NULL,
    content          text NOT NULL,
    embedding        vector(1024),
    embedding_model  text NOT NULL,
    "timestamp"      timestamptz NOT NULL,
    task_summary     text,
    metadata         jsonb DEFAULT '{}'::jsonb,             -- nullable in live
    created_at       timestamptz DEFAULT now()              -- nullable in live
);

-- ── Indexes ────────────────────────────────────────────────────────────────
-- Vector HNSW index used by query.py's `embedding <=> q` (cosine) search.
CREATE INDEX IF NOT EXISTS idx_chunks_embedding
    ON session_chunks USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 128);

-- Trigram index backing the query.py pg_trgm fallback (similarity(content, q)).
CREATE INDEX IF NOT EXISTS idx_chunks_trgm
    ON session_chunks USING gin (content gin_trgm_ops);

-- GIN tsvector index for the BM25-style ts_rank_cd lookup.
CREATE INDEX IF NOT EXISTS idx_chunks_tsvector
    ON session_chunks USING gin (content_tsvector);

-- Temporal filter / ordering on timestamp_start.
CREATE INDEX IF NOT EXISTS idx_chunks_time
    ON session_chunks USING btree (timestamp_start);

-- session_facts indexes (mirror live; see docs/DESIGN.md §3.3).
CREATE INDEX IF NOT EXISTS idx_facts_category
    ON session_facts USING btree (category);

CREATE INDEX IF NOT EXISTS idx_facts_embedding
    ON session_facts USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 128);

CREATE INDEX IF NOT EXISTS idx_facts_time
    ON session_facts USING btree ("timestamp");

CREATE INDEX IF NOT EXISTS idx_facts_trgm
    ON session_facts USING gin (content gin_trgm_ops);
