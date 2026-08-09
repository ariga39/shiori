#!/usr/bin/env python3
"""
Query session memory via hybrid search (Voyage vector + BM25 tsvector).
Includes temporal decay and MMR deduplication.

Usage: python3 query.py "search query" [--limit N]
"""

import argparse
from datetime import UTC, datetime

import numpy as np
import psycopg2
import requests

from shiyi.config import ConfigError, Settings, credentials_from_settings, load_config

VOYAGE_API_URL = "https://api.voyageai.com/v1/embeddings"
VOYAGE_MODEL = "voyage-4-large"
VOYAGE_KEY_PATH = None
VOYAGE_API_KEY = None
PG_CRED_PATH = None
DATABASE_DSN = None

# Temporal decay: score *= 2^(-days_old / HALF_LIFE_DAYS)
HALF_LIFE_DAYS = 30
# Prior applied to chunks with no timestamp AND no created_at (rare double-NULL).
# Without it, such chunks skip decay entirely and score as-if brand new.
NULL_TS_PRIOR = 0.25
# MMR: skip results with cosine similarity > this to already-selected results
MMR_SIM_THRESHOLD = 0.85


def apply_settings(settings: Settings) -> None:
    global VOYAGE_API_URL, VOYAGE_MODEL, VOYAGE_KEY_PATH, VOYAGE_API_KEY
    global PG_CRED_PATH, DATABASE_DSN
    if settings.voyage_api_url is not None:
        VOYAGE_API_URL = settings.voyage_api_url
    if settings.voyage_model is not None:
        VOYAGE_MODEL = settings.voyage_model
    if settings.voyage_key_file is not None:
        VOYAGE_KEY_PATH = str(settings.voyage_key_file)
    if settings.voyage_api_key is not None:
        VOYAGE_API_KEY = settings.voyage_api_key
    if settings.pg_cred_file is not None:
        PG_CRED_PATH = str(settings.pg_cred_file)
    if settings.database_dsn is not None:
        DATABASE_DSN = settings.database_dsn


def _read_voyage_key():
    if VOYAGE_API_KEY:
        return VOYAGE_API_KEY
    if VOYAGE_KEY_PATH:
        try:
            with open(VOYAGE_KEY_PATH, encoding="utf-8") as f:
                value = f.read().strip()
        except OSError as exc:
            raise ConfigError("Voyage key file cannot be read", code="key_file_unreadable") from exc
        if not value:
            raise ConfigError("Voyage key file is empty", code="key_file_empty")
        return value
    return load_config().read_voyage_key()


def load_credentials(path=None):
    if path is not None:
        creds = {}
        with open(path, encoding="utf-8") as fh:
            for raw_line in fh:
                raw_line = raw_line.strip()
                if "=" in raw_line:
                    k, v = raw_line.split("=", 1)
                    creds[k.strip()] = v.strip()
        return creds
    if PG_CRED_PATH:
        return load_credentials(PG_CRED_PATH)
    return credentials_from_settings(load_config())


def get_db():
    if DATABASE_DSN:
        return psycopg2.connect(DATABASE_DSN)
    creds = load_credentials()
    if "dsn" in creds:
        return psycopg2.connect(creds["dsn"])
    required = ("host", "port", "dbname", "user", "password")
    missing = [key for key in required if key not in creds or not creds[key]]
    if missing:
        raise ConfigError("database credentials missing: " + ", ".join(missing), code="invalid_database_config")
    return psycopg2.connect(
        host=creds["host"],
        port=int(creds["port"]),
        dbname=creds["dbname"],
        user=creds["user"],
        password=creds["password"],
    )


def embed_query(text):
    api_key = _read_voyage_key()
    resp = requests.post(
        VOYAGE_API_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": VOYAGE_MODEL,
            "input": [text[:8000]],
            "input_type": "query",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["data"][0]["embedding"]


def _cosine_sim(a, b):
    a, b = np.array(a), np.array(b)
    dot = np.dot(a, b)
    norm = np.linalg.norm(a) * np.linalg.norm(b)
    return dot / norm if norm > 0 else 0.0


def _build_tsquery(query_text: str) -> str:
    """Build a tsquery from query text. Split on whitespace, join with &.
    Works for CJK (single chars become terms via 'simple' config) and latin."""
    words = query_text.split()
    if not words:
        return ""
    # Escape each word and join with &
    terms = []
    for w in words:
        # Remove special tsquery chars
        clean = w.replace("'", "").replace("&", "").replace("|", "").replace("!", "").replace("(", "").replace(")", "").strip()
        if clean:
            terms.append(f"'{clean}'")
    if not terms:
        return ""
    return " & ".join(terms)


def search(query, limit=5):
    conn = get_db()
    cur = conn.cursor()

    query_embedding = embed_query(query)
    now = datetime.now(UTC)

    # Candidate pool size
    pool = max(limit * 5, 30)

    # Raise HNSW ef_search so the vector pool query actually retrieves the
    # nearest `pool` candidates. The index default (40) has poor recall once
    # the table grows into the tens of thousands of rows, silently dropping
    # relevant chunks from the pool (and thus from the final results).
    # Clamp to [200, 1000]: since shared_preload_libraries='vector' (2026-08-03)
    # the GUC is registered at startup, so a SET above the pgvector cap (1000,
    # i.e. pool > 1000 / limit > 200) now raises InvalidParameterValue instead of
    # being silently treated as a custom placeholder. Clamping keeps the value
    # legal; the except branch below is a defensive fallback only.
    try:
        cur.execute("SET hnsw.ef_search = %s", (min(max(pool, 200), 1000),))
    except Exception:
        # ef_search GUC unavailable on older pgvector (or out of range). The
        # failed SET aborts the transaction; roll it back so the vector query
        # below still runs.
        conn.rollback()

    # ── Vector search ────────────────────────────────────────────────────
    cur.execute("""
        SELECT id, content, 1 - (embedding <=> %s::vector) as vscore,
               timestamp_start, session_id, source_type, embedding::text, created_at
        FROM session_chunks
        WHERE embedding IS NOT NULL
        ORDER BY embedding <=> %s::vector
        LIMIT %s
    """, (str(query_embedding), str(query_embedding), pool))
    vector_rows = cur.fetchall()

    # ── BM25 (tsvector) search ───────────────────────────────────────────
    tsq = _build_tsquery(query)
    bm25_rows = []
    if tsq:
        try:
            cur.execute("""
                SELECT id, content, ts_rank_cd(content_tsvector, to_tsquery('simple', %s)) as tscore,
                       timestamp_start, session_id, source_type, embedding::text, created_at
                FROM session_chunks
                WHERE content_tsvector @@ to_tsquery('simple', %s)
                ORDER BY tscore DESC
                LIMIT %s
            """, (tsq, tsq, pool))
            bm25_rows = cur.fetchall()
        except Exception:
            # tsvector column might not exist yet during migration. The failed
            # query aborts the transaction; roll it back so the pg_trgm fallback
            # below still runs on a usable connection.
            conn.rollback()

    # ── Exact substring (ILIKE) search ───────────────────────────────────
    # Short queries — especially 2-4 char CJK names like 「日和」 — score
    # terribly under both vector (semantic neighbors crowd them out of the
    # small pool) and BM25 (tsquery splits CJK into single chars joined by
    # AND: '日' & '和', both high-frequency, so ts_rank is diluted to noise).
    # pg_trgm similarity is useless for 2-char strings (only 1 trigram).
    # Exact substring match is the reliable channel for entity/name queries:
    # it finds the chunks that literally contain the query.  We add it as a
    # third RRF channel with a bonus so entity hits surface instead of being
    # buried under temporally-recent-but-irrelevant vector neighbors.
    exact_rows = []
    if len(query.strip()) <= 20:
        try:
            escaped = query.replace("%", r"\%").replace("_", r"\_")
            cur.execute("""
                SELECT id, content, 1.0 as tscore,
                       timestamp_start, session_id, source_type, embedding::text, created_at
                FROM session_chunks
                WHERE content ILIKE %s ESCAPE '\'
                ORDER BY timestamp_start DESC
                LIMIT %s
            """, (f"%{escaped}%", pool))
            exact_rows = cur.fetchall()
        except Exception:
            conn.rollback()

    # If BM25 returned nothing, fall back to trigram similarity
    if not bm25_rows:
        try:
            cur.execute("""
                SELECT id, content, similarity(content, %s) as tscore,
                       timestamp_start, session_id, source_type, embedding::text, created_at
                FROM session_chunks
                WHERE content %% %s
                ORDER BY similarity(content, %s) DESC
                LIMIT %s
            """, (query, query, query, pool))
            bm25_rows = cur.fetchall()
        except Exception:
            pass

    cur.close()
    conn.close()

    # ── RRF fusion ───────────────────────────────────────────────────────
    k = 60  # RRF constant
    scores = {}   # id -> rrf_score
    meta = {}     # id -> (content, timestamp, session_id, source_type, embedding_str, created_at)

    for rank, row in enumerate(vector_rows, 1):
        rid, content, vscore, ts, sid, stype, emb_str, created_at = row
        scores[rid] = scores.get(rid, 0) + 1.0 / (k + rank)
        meta[rid] = (content, ts, sid, stype, emb_str, created_at)

    for rank, row in enumerate(bm25_rows, 1):
        rid, content, tscore, ts, sid, stype, emb_str, created_at = row
        scores[rid] = scores.get(rid, 0) + 1.0 / (k + rank)
        if rid not in meta:
            meta[rid] = (content, ts, sid, stype, emb_str, created_at)

    # Exact-substring hits get a rank bonus so entity/name matches are not
    # buried: they are treated as if they ranked at position 1 in their own
    # channel (1/(k+1) ≈ 0.0164) plus the fact that BM25/vector may also hit.
    # This deliberately favors literal containment for short queries.
    for rank, row in enumerate(exact_rows, 1):
        rid, content, tscore, ts, sid, stype, emb_str, created_at = row
        bonus_rank = 1  # exact matches rank at the top of their channel
        scores[rid] = scores.get(rid, 0) + 1.0 / (k + bonus_rank)
        if rid not in meta:
            meta[rid] = (content, ts, sid, stype, emb_str, created_at)

    # ── Temporal decay ───────────────────────────────────────────────────
    for rid in scores:
        content, ts, sid, stype, emb_str, created_at = meta[rid]
        eff_ts = ts if ts is not None else created_at
        if eff_ts:
            days_old = (now - eff_ts).total_seconds() / 86400
            decay = 2 ** (-days_old / HALF_LIFE_DAYS)
            scores[rid] *= decay
        else:
            # Both timestamp and created_at are NULL (rare). Don't skip decay
            # (which would rank it as brand-new); apply a fixed low prior.
            scores[rid] *= NULL_TS_PRIOR

    # ── Sort by decayed RRF score ────────────────────────────────────────
    ranked = sorted(scores.keys(), key=lambda rid: scores[rid], reverse=True)

    # ── MMR deduplication ────────────────────────────────────────────────
    selected = []
    selected_embeddings = []

    for rid in ranked:
        if len(selected) >= limit:
            break

        content, ts, sid, stype, emb_str, created_at = meta[rid]

        # Parse embedding for MMR comparison
        if emb_str and selected_embeddings:
            try:
                emb = [float(x) for x in emb_str.strip("[]").split(",")]
                too_similar = False
                for sel_emb in selected_embeddings:
                    if _cosine_sim(emb, sel_emb) > MMR_SIM_THRESHOLD:
                        too_similar = True
                        break
                if too_similar:
                    continue
                selected_embeddings.append(emb)
            except (ValueError, AttributeError):
                pass
        elif emb_str:
            try:
                emb = [float(x) for x in emb_str.strip("[]").split(",")]
                selected_embeddings.append(emb)
            except (ValueError, AttributeError):
                pass

        selected.append((content, scores[rid], ts, sid, stype))

    return selected


def main(argv=None):
    parser = argparse.ArgumentParser(description="Query session memory (v2)")
    parser.add_argument("query", help="Search query")
    parser.add_argument("--limit", "-n", type=int, default=5, help="Max results")
    parser.add_argument("--config", help="JSON/TOML config file")
    parser.add_argument(
        "--legacy-openclaw",
        action="store_true",
        help="Explicit migration mode: use legacy OpenClaw paths when SHIYI_* is unset",
    )
    args = parser.parse_args(argv)

    settings = load_config(config_path=args.config, legacy_openclaw=args.legacy_openclaw)
    settings.require_database()
    settings.require_embedding()
    apply_settings(settings)

    results = search(args.query, args.limit)

    if not results:
        print("No results found.")
        return

    for i, (content, score, ts, session_id, source_type) in enumerate(results, 1):
        print(f"--- Result {i} (score: {score:.6f}, time: {ts}, type: {source_type}) ---")
        preview = content[:500]
        if len(content) > 500:
            preview += "..."
        print(preview)
        print()


if __name__ == "__main__":
    main()
