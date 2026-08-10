#!/usr/bin/env python3
"""
Query session memory via hybrid search (Voyage vector + BM25 tsvector).
Includes temporal decay and MMR deduplication.

Usage: python3 query.py "search query" [--limit N] [--offset N]
"""

import argparse
from dataclasses import dataclass
from datetime import UTC, datetime
from math import isfinite
from numbers import Real
from typing import Any

import numpy as np
import psycopg2
import requests

from shiori.config import ConfigError, Settings, credentials_from_settings, load_config
from shiori.embeddings import deterministic_embedding

VOYAGE_API_URL = "https://api.voyageai.com/v1/embeddings"
VOYAGE_MODEL = "voyage-4-large"
VOYAGE_KEY_PATH = None
VOYAGE_API_KEY = None
EMBEDDING_PROVIDER = "voyage"
PG_CRED_PATH = None
DATABASE_DSN = None

# Temporal decay: score *= 2^(-days_old / HALF_LIFE_DAYS)
HALF_LIFE_DAYS = 30
# Prior applied to chunks with no timestamp AND no created_at (rare double-NULL).
# Without it, such chunks skip decay entirely and score as-if brand new.
NULL_TS_PRIOR = 0.25
# MMR: skip results with cosine similarity > this to already-selected results
MMR_SIM_THRESHOLD = 0.85

# Search is an agent-facing read surface.  Keep every input and candidate
# allocation bounded before it reaches PostgreSQL or the embedding provider.
DEFAULT_LIMIT = 5
MAX_PAGE_LIMIT = 20
MAX_SEARCH_LIMIT = 256
# Keep the final accessible offset inside the hard result window so a truthful
# look-ahead never advertises a next page beyond the service's own bound.
MAX_OFFSET = MAX_SEARCH_LIMIT - 1
MAX_CANDIDATES = 1000
MAX_QUERY_CHARS = 8000
DEFAULT_EMBED_DIM = 1024
EMBED_DIM = DEFAULT_EMBED_DIM


class QueryError(ConfigError):
    """Secret-safe, stable error raised by the bounded query service."""

    code = "query_failed"


@dataclass(frozen=True)
class SearchPage:
    """Bounded page returned by the public query service."""

    results: list[tuple[Any, ...]]
    limit: int
    offset: int
    has_more: bool
    next_offset: int | None


def _validate_query_text(query_text: Any) -> str:
    if not isinstance(query_text, str):
        raise QueryError("query must be a string", code="invalid_query")
    value = query_text.strip()
    if not value:
        raise QueryError("query must be a non-empty string", code="invalid_query")
    if len(value) > MAX_QUERY_CHARS:
        raise QueryError("query exceeds the maximum length", code="query_too_long")
    return value


def _normalise_search_args(limit: Any, offset: Any = 0) -> tuple[int, int]:
    # ``bool`` is an ``int`` subclass, but accepting True/False as a page size
    # makes JSON callers surprisingly request a one-row page or an invalid
    # zero-row page.  Reject it explicitly at the public boundary.
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise QueryError("limit must be an integer", code="invalid_limit")
    if limit <= 0:
        raise QueryError("limit must be positive", code="invalid_limit")
    if isinstance(offset, bool) or not isinstance(offset, int):
        raise QueryError("offset must be an integer", code="invalid_offset")
    if offset < 0 or offset > MAX_OFFSET:
        raise QueryError(f"offset must be between 0 and {MAX_OFFSET}", code="offset_out_of_bounds")
    # The compatibility script accepted larger limits.  Clamp them to a hard
    # resource ceiling rather than allowing an unbounded candidate pool.
    return min(limit, MAX_SEARCH_LIMIT), offset


def _validate_embedding_vector(value: Any, *, expected_dim: int = DEFAULT_EMBED_DIM) -> list[float]:
    if not isinstance(value, (list, tuple)):
        raise QueryError("embedding provider returned a non-vector", code="invalid_embedding")
    if len(value) != expected_dim:
        raise QueryError(
            f"embedding dimension does not match configured dimension {expected_dim}",
            code="embedding_dimension_mismatch",
        )
    result: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, Real):
            raise QueryError("embedding provider returned a non-numeric vector", code="invalid_embedding")
        number = float(item)
        if not isfinite(number):
            raise QueryError("embedding provider returned a non-finite vector", code="invalid_embedding")
        result.append(number)
    return result


def _escape_like(value: str) -> str:
    """Escape a user string for a PostgreSQL LIKE pattern.

    The escape character itself must be escaped first; otherwise a query that
    contains a backslash can change the meaning of the following wildcard.
    """
    return value.replace("\\", r"\\").replace("%", r"\%").replace("_", r"\_")


def _row_provenance(row: tuple[Any, ...]) -> dict[str, Any]:
    """Return stable, explicit provenance for a legacy result tuple.

    The first five tuple positions are the long-standing ``query.search``
    compatibility shape.  Newer rows append model and dimension fields, so
    callers that consumed the old tuple do not silently break.
    """
    return {
        "timestamp": row[2],
        "session_id": row[3],
        "source_type": row[4],
        "embedding_model": row[5] if len(row) > 5 else None,
        "embedding_dimension": row[6] if len(row) > 6 else None,
    }


def _unpack_search_row(row: tuple[Any, ...]) -> tuple[Any, ...]:
    """Normalize current and test-double result rows.

    The compatibility shape has eight columns; the bounded service appends
    model and vector-dimension provenance.  Keeping this adapter local lets
    older embedding callers continue to consume the first five positions.
    """
    if len(row) >= 10:
        return tuple(row[:10])
    if len(row) == 8:
        return (*row, VOYAGE_MODEL, EMBED_DIM)
    raise QueryError("search backend returned an invalid row", code="search_backend_invalid")


def apply_settings(settings: Settings) -> None:
    global VOYAGE_API_URL, VOYAGE_MODEL, VOYAGE_KEY_PATH, VOYAGE_API_KEY, EMBEDDING_PROVIDER
    global PG_CRED_PATH, DATABASE_DSN, EMBED_DIM
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
    if settings.embedding_provider is not None:
        EMBEDDING_PROVIDER = settings.embedding_provider
    EMBED_DIM = settings.embed_dim if settings.embed_dim is not None else DEFAULT_EMBED_DIM


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
    def connect(*args, **kwargs):
        conn = None
        try:
            conn = psycopg2.connect(*args, **kwargs)
            # The query service is deliberately read-only.  Session-local SET
            # statements (used for pgvector recall) remain allowed, while any
            # accidental DML fails closed at the PostgreSQL boundary.
            conn.set_session(readonly=True)
            return conn
        except QueryError:
            raise
        except Exception as exc:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            raise QueryError("database is unavailable", code="search_unavailable") from exc

    if DATABASE_DSN:
        return connect(DATABASE_DSN)
    creds = load_credentials()
    if "dsn" in creds:
        return connect(creds["dsn"])
    required = ("host", "port", "dbname", "user", "password")
    missing = [key for key in required if key not in creds or not creds[key]]
    if missing:
        raise ConfigError("database credentials missing: " + ", ".join(missing), code="invalid_database_config")
    return connect(
        host=creds["host"],
        port=int(creds["port"]),
        dbname=creds["dbname"],
        user=creds["user"],
        password=creds["password"],
    )


def embed_query(text):
    text = _validate_query_text(text)
    if EMBEDDING_PROVIDER == "fake":
        return deterministic_embedding(text, dimension=EMBED_DIM)
    api_key = _read_voyage_key()
    try:
        resp = requests.post(
            VOYAGE_API_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": VOYAGE_MODEL,
                "input": [text],
                "input_type": "query",
            },
            timeout=30,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise QueryError("embedding provider is unavailable", code="embedding_unavailable") from exc
    try:
        payload = resp.json()
        if not isinstance(payload, dict):
            raise TypeError("embedding response is not an object")
        response_model = payload.get("model")
        if response_model is not None and response_model != VOYAGE_MODEL:
            raise QueryError("embedding provider returned an unexpected model", code="embedding_model_mismatch")
        embedding = payload["data"][0]["embedding"]
    except QueryError:
        raise
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise QueryError("embedding provider returned an invalid response", code="invalid_embedding") from exc
    return _validate_embedding_vector(embedding, expected_dim=EMBED_DIM)


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


def search(query, limit=DEFAULT_LIMIT, offset=0):
    query = _validate_query_text(query)
    limit, offset = _normalise_search_args(limit, offset)
    # Obtain the provider result before opening a database connection.  This
    # avoids leaking a connection when the provider returns malformed data or
    # an embedding dimension that does not match the configured schema.
    query_embedding = _validate_embedding_vector(embed_query(query), expected_dim=EMBED_DIM)
    try:
        conn = get_db()
    except QueryError:
        raise
    except Exception as exc:
        raise QueryError("database is unavailable", code="search_unavailable") from exc
    try:
        cur = conn.cursor()
    except Exception as exc:
        try:
            conn.close()
        except Exception:
            pass
        raise QueryError("search backend is unavailable", code="search_unavailable") from exc

    # Fetch enough rows to service the requested offset, but never allocate an
    # unbounded candidate pool.  ``search_page`` uses one extra row to expose
    # an honest ``has_more`` value without a second count query.
    result_limit = min(limit + offset, MAX_SEARCH_LIMIT)
    now = datetime.now(UTC)

    # Candidate pool size
    pool = min(max(result_limit * 5, 30), MAX_CANDIDATES)

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

    # Vector search ---------------------------------------------------------
    try:
        cur.execute("""
            SELECT id, content, 1 - (embedding <=> %s::vector) as vscore,
                   timestamp_start, session_id, source_type, embedding::text, created_at,
                   embedding_model, vector_dims(embedding)
            FROM session_chunks
            WHERE embedding IS NOT NULL
              AND embedding_model = %s
              AND vector_dims(embedding) = %s
            ORDER BY embedding <=> %s::vector, id
            LIMIT %s
        """, (str(query_embedding), VOYAGE_MODEL, EMBED_DIM, str(query_embedding), pool))
        vector_rows = cur.fetchall()
    except Exception as exc:
        conn.rollback()
        cur.close()
        conn.close()
        raise QueryError("search backend is unavailable", code="search_unavailable") from exc

    # BM25 (tsvector) search -----------------------------------------------
    tsq = _build_tsquery(query)
    bm25_rows = []
    if tsq:
        try:
            cur.execute("""
                SELECT id, content, ts_rank_cd(content_tsvector, to_tsquery('simple', %s)) as tscore,
                       timestamp_start, session_id, source_type, embedding::text, created_at,
                       embedding_model, vector_dims(embedding)
                FROM session_chunks
                WHERE content_tsvector @@ to_tsquery('simple', %s)
                  AND embedding_model = %s
                  AND (embedding IS NULL OR vector_dims(embedding) = %s)
                ORDER BY tscore DESC, id
                LIMIT %s
            """, (tsq, tsq, VOYAGE_MODEL, EMBED_DIM, pool))
            bm25_rows = cur.fetchall()
        except Exception:
            # tsvector column might not exist yet during migration. The failed
            # query aborts the transaction; roll it back so the pg_trgm fallback
            # below still runs on a usable connection.
            conn.rollback()

    # Exact substring (ILIKE) search ---------------------------------------
    # Short queries, especially two-to-four-character CJK names, score
    # terribly under both vector (semantic neighbors crowd them out of the
    # small pool) and BM25 (tsquery splits CJK into single chars joined by
    # AND: each character is high-frequency, so ts_rank is diluted to noise).
    # pg_trgm similarity is useless for 2-char strings (only 1 trigram).
    # Exact substring match is the reliable channel for entity/name queries:
    # it finds the chunks that literally contain the query.  We add it as a
    # third RRF channel with a bonus so entity hits surface instead of being
    # buried under temporally-recent-but-irrelevant vector neighbors.
    exact_rows = []
    if len(query.strip()) <= 20:
        try:
            escaped = _escape_like(query)
            cur.execute("""
                SELECT id, content, 1.0 as tscore,
                       timestamp_start, session_id, source_type, embedding::text, created_at,
                       embedding_model, vector_dims(embedding)
                FROM session_chunks
                WHERE content ILIKE %s ESCAPE '\\'
                AND embedding_model = %s
                AND (embedding IS NULL OR vector_dims(embedding) = %s)
                ORDER BY timestamp_start DESC NULLS LAST, id
                LIMIT %s
            """, (f"%{escaped}%", VOYAGE_MODEL, EMBED_DIM, pool))
            exact_rows = cur.fetchall()
        except Exception:
            conn.rollback()

    # If BM25 returned nothing, fall back to trigram similarity
    if not bm25_rows:
        try:
            cur.execute("""
                SELECT id, content, similarity(content, %s) as tscore,
                       timestamp_start, session_id, source_type, embedding::text, created_at,
                       embedding_model, vector_dims(embedding)
                FROM session_chunks
                WHERE content %% %s
                  AND embedding_model = %s
                  AND (embedding IS NULL OR vector_dims(embedding) = %s)
                ORDER BY similarity(content, %s) DESC, id
                LIMIT %s
            """, (query, query, VOYAGE_MODEL, EMBED_DIM, query, pool))
            bm25_rows = cur.fetchall()
        except Exception:
            conn.rollback()
            pass

    cur.close()
    conn.close()

    # RRF fusion -----------------------------------------------------------
    k = 60  # RRF constant
    scores = {}   # id -> rrf_score
    meta = {}     # id -> (content, timestamp, session_id, source_type, embedding_str, created_at, model, dim)

    for rank, row in enumerate(vector_rows, 1):
        rid, content, vscore, ts, sid, stype, emb_str, created_at, model, dimension = _unpack_search_row(row)
        scores[rid] = scores.get(rid, 0) + 1.0 / (k + rank)
        meta[rid] = (content, ts, sid, stype, emb_str, created_at, model, dimension)

    for rank, row in enumerate(bm25_rows, 1):
        rid, content, tscore, ts, sid, stype, emb_str, created_at, model, dimension = _unpack_search_row(row)
        scores[rid] = scores.get(rid, 0) + 1.0 / (k + rank)
        if rid not in meta:
            meta[rid] = (content, ts, sid, stype, emb_str, created_at, model, dimension)

    # Exact-substring hits get a rank bonus so entity/name matches are not
    # buried: they are treated as if they ranked at position 1 in their own
    # channel (1/(k+1), approximately 0.0164) plus the fact that BM25/vector
    # may also hit.
    # This deliberately favors literal containment for short queries.
    for rank, row in enumerate(exact_rows, 1):
        rid, content, tscore, ts, sid, stype, emb_str, created_at, model, dimension = _unpack_search_row(row)
        bonus_rank = 1  # exact matches rank at the top of their channel
        scores[rid] = scores.get(rid, 0) + 1.0 / (k + bonus_rank)
        if rid not in meta:
            meta[rid] = (content, ts, sid, stype, emb_str, created_at, model, dimension)

    # Temporal decay --------------------------------------------------------
    for rid in scores:
        content, ts, sid, stype, emb_str, created_at, model, dimension = meta[rid]
        eff_ts = ts if ts is not None else created_at
        if eff_ts:
            days_old = (now - eff_ts).total_seconds() / 86400
            decay = 2 ** (-days_old / HALF_LIFE_DAYS)
            scores[rid] *= decay
        else:
            # Both timestamp and created_at are NULL (rare). Don't skip decay
            # (which would rank it as brand-new); apply a fixed low prior.
            scores[rid] *= NULL_TS_PRIOR

    # Sort by decayed RRF score --------------------------------------------
    ranked = sorted(scores.keys(), key=lambda rid: (-scores[rid], str(rid)))

    # MMR deduplication -----------------------------------------------------
    selected = []
    selected_embeddings = []

    for rid in ranked:
        if len(selected) >= result_limit:
            break

        content, ts, sid, stype, emb_str, created_at, model, dimension = meta[rid]

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

        selected.append((content, scores[rid], ts, sid, stype, model, dimension))

    return selected[offset : offset + limit]


def search_page(query_text: str, *, limit: int = MAX_PAGE_LIMIT, offset: int = 0) -> SearchPage:
    """Return a bounded, stable page without exposing an unbounded count query."""
    query_text = _validate_query_text(query_text)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise QueryError("limit must be a positive integer", code="invalid_limit")
    if limit > MAX_PAGE_LIMIT:
        raise QueryError(f"limit must be at most {MAX_PAGE_LIMIT}", code="limit_out_of_bounds")
    _, offset = _normalise_search_args(limit, offset)
    # Ask the compatibility search function for one look-ahead row.  Calling
    # it without ``offset`` at zero keeps monkeypatched/legacy integrations
    # working while still making ``has_more`` truthful.
    requested = offset + limit + 1
    all_rows = search(query_text, limit=requested)
    page = all_rows[offset : offset + limit]
    has_more = len(all_rows) > offset + limit
    return SearchPage(
        results=page,
        limit=limit,
        offset=offset,
        has_more=has_more,
        next_offset=offset + limit if has_more else None,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description="Query session memory (v2)")
    parser.add_argument("query", help="Search query")
    parser.add_argument("--limit", "-n", type=int, default=DEFAULT_LIMIT, help="Max results")
    parser.add_argument("--offset", type=int, default=0, help="Bounded result offset")
    parser.add_argument("--config", help="JSON/TOML config file")
    parser.add_argument(
        "--legacy-openclaw",
        action="store_true",
        help="Explicit migration mode: use legacy OpenClaw paths when SHIORI_* is unset",
    )
    args = parser.parse_args(argv)

    settings = load_config(config_path=args.config, legacy_openclaw=args.legacy_openclaw)
    settings.require_database()
    settings.require_embedding()
    apply_settings(settings)

    results = search(args.query, args.limit, args.offset)

    if not results:
        print("No results found.")
        return

    for i, row in enumerate(results, 1):
        content, score, ts, session_id, source_type = row[:5]
        print(f"--- Result {i} (score: {score:.6f}, time: {ts}, type: {source_type}) ---")
        preview = content[:500]
        if len(content) > 500:
            preview += "..."
        print(preview)
        print()


if __name__ == "__main__":
    main()
