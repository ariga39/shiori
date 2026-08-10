"""0001 — initial schema (tables + indexes), equivalent to schema.sql."""


def upgrade(cur) -> None:
    cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
    cur.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    cur.execute(
        """
        CREATE TABLE session_chunks (
            id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            session_id         text NOT NULL,
            source_type        text NOT NULL DEFAULT 'main_user',
            content            text NOT NULL,
            embedding          vector(1024),
            embedding_model    text NOT NULL,
            timestamp_start    timestamptz,
            timestamp_end      timestamptz,
            turn_index_start   integer,
            turn_index_end     integer,
            channel            text,
            metadata           jsonb DEFAULT '{}'::jsonb,
            created_at         timestamptz DEFAULT now(),
            content_tsvector   tsvector
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE ingestion_state (
            file_path        text PRIMARY KEY,
            file_mtime       timestamptz NOT NULL,
            file_size        bigint NOT NULL,
            processed_offset bigint NOT NULL DEFAULT 0,
            processed_at     timestamptz DEFAULT now(),
            source_type      text NOT NULL,
            chunks_created   integer DEFAULT 0,
            facts_created    integer DEFAULT 0
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE session_facts (
            id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            session_id       text NOT NULL,
            category         text NOT NULL,
            content          text NOT NULL,
            embedding        vector(1024),
            embedding_model  text NOT NULL,
            "timestamp"      timestamptz NOT NULL,
            task_summary     text,
            metadata         jsonb DEFAULT '{}'::jsonb,
            created_at       timestamptz DEFAULT now()
        )
        """
    )

    cur.execute(
        "CREATE INDEX idx_chunks_embedding ON session_chunks "
        "USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 128)"
    )
    cur.execute("CREATE INDEX idx_chunks_trgm ON session_chunks USING gin (content gin_trgm_ops)")
    cur.execute("CREATE INDEX idx_chunks_tsvector ON session_chunks USING gin (content_tsvector)")
    cur.execute("CREATE INDEX idx_chunks_time ON session_chunks USING btree (timestamp_start)")
    cur.execute("CREATE INDEX idx_facts_category ON session_facts USING btree (category)")
    cur.execute(
        "CREATE INDEX idx_facts_embedding ON session_facts "
        "USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 128)"
    )
    cur.execute('CREATE INDEX idx_facts_time ON session_facts USING btree ("timestamp")')
    cur.execute("CREATE INDEX idx_facts_trgm ON session_facts USING gin (content gin_trgm_ops)")
