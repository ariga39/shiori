#!/usr/bin/env python3
"""
Query session memory via hybrid search (Voyage vector + BM25 tsvector).
Includes temporal decay and MMR deduplication.

Usage: python3 query.py "search query" [--limit N] [--offset N]
"""

import argparse
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import isfinite
from numbers import Real
from typing import Any, Generic, TypeVar

import numpy as np
import psycopg2
import requests

from shiori.config import ConfigError, Settings, credentials_from_settings, load_config
from shiori.embeddings import deterministic_embedding

VOYAGE_API_URL = "https://api.voyageai.com/v1/embeddings"
VOYAGE_MODEL = "voyage-4-large"
VOYAGE_KEY_PATH = None
VOYAGE_API_KEY = None
REPLAY_MANIFEST = None
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


# Phase 4D evaluation seam (behavior-preserving, context-local). A scoped
# evaluation context may install a stage configuration (ablations) and a
# collector. The SAME production search implementation executes under the
# config; the default (all stages enabled) must be item-for-item equivalent to
# no-context search. The context is a ContextVar so concurrent queries never
# share a trace, and it is always restored by the context manager even on
# exception. Collector errors are contained and never change search outcome.
class StageConfig:
    """Which production stages participate in a search (frozen ablation matrix).

    The frozen matrix semantics are:
    - dense: the pgvector cosine channel.
    - lexical: the ts_rank_cd channel PLUS its trigram fallback (one switch;
      ts_rank_cd and trigram are two observable stages of the SAME lexical
      channel, so a single switch drives both).
    - exact: the ILIKE substring channel.
    - temporal: the temporal-decay step.
    - dedup: the MMR dedup step.
    RRF is the multi-channel fusion mechanism (not a switch): it always fuses
    whatever candidate channels are enabled.

    Disabled stages are SKIPPED at their execution entry point (their SQL/step
    does not run), so an ablation's latency reflects the truly-executed stages.
    A configuration with no enabled candidate channel (dense/lexical/exact all
    off) is invalid and fails closed.
    """

    __slots__ = ("dense", "lexical", "exact", "temporal", "dedup")

    def __init__(self, *, dense=True, lexical=True, exact=True, temporal=True, dedup=True) -> None:
        values = {"dense": dense, "lexical": lexical, "exact": exact, "temporal": temporal, "dedup": dedup}
        for name, value in values.items():
            if not isinstance(value, bool):
                raise TypeError(f"StageConfig.{name} must be a bool, got {type(value).__name__}")
        self.dense = dense
        self.lexical = lexical
        self.exact = exact
        self.temporal = temporal
        self.dedup = dedup

    def has_candidate_channel(self) -> bool:
        return self.dense or self.lexical or self.exact

    def validate(self) -> None:
        # Re-assert strict bool typing (constructor already enforces it; this
        # guards against a mutated config object).
        for name in ("dense", "lexical", "exact", "temporal", "dedup"):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"StageConfig.{name} must be a bool, got {type(getattr(self, name)).__name__}")
        if not self.has_candidate_channel():
            raise ValueError(
                "StageConfig with no candidate channel (dense/lexical/exact all disabled) is invalid"
            )

    def stage_enabled(self, stage: str) -> bool:
        if stage in {"ts_rank_cd", "trigram"}:
            return self.lexical
        return bool(getattr(self, stage, True))


class EvalContext:
    """Scoped evaluation context: stage config + optional trace collector.

    `collector` is called as ``collector(stage, events)`` after each production
    stage. Any exception raised by the collector is recorded in
    ``context.errors`` and swallowed so production search outcome is unchanged.
    """

    __slots__ = ("config", "collector", "errors")

    def __init__(self, config: StageConfig, collector=None) -> None:
        self.config = config
        self.collector = collector
        self.errors: list[Exception] = []


_EVAL_CONTEXT_VAR: Any = None  # contextvars.ContextVar installed lazily at import time


def _init_eval_context_var():
    """Create the module-level ContextVar (idempotent)."""
    global _EVAL_CONTEXT_VAR
    if _EVAL_CONTEXT_VAR is None:
        import contextvars

        _EVAL_CONTEXT_VAR = contextvars.ContextVar("shiori_eval_context", default=None)


def _eval_context_var():
    """Return the module ContextVar (typed for pyright)."""
    _init_eval_context_var()
    return _EVAL_CONTEXT_VAR


def _eval_scope(config: StageConfig | None = None, collector=None):
    """Return a context manager installing a scoped EvalContext (must-restore).

    The context is stored in a ContextVar so the collector/config are local to
    the executing task/thread and never leak between concurrent queries. The
    previous value is always restored on exit (including on exception).
    """
    import contextlib

    var = _eval_context_var()

    @contextlib.contextmanager
    def _manager():
        ctx = EvalContext(config if config is not None else StageConfig(), collector)
        token = var.set(ctx)
        try:
            yield ctx
        finally:
            var.reset(token)

    return _manager()


def _current_eval() -> EvalContext | None:
    return _eval_context_var().get()


def _emit_eval(stage: str, events: list[dict]) -> None:
    eval_ctx = _current_eval()
    if eval_ctx is None or eval_ctx.collector is None:
        return
    try:
        eval_ctx.collector(stage, events)
    except Exception as exc:  # noqa: BLE001 - observer must never break search
        eval_ctx.errors.append(exc)


T = TypeVar("T")


@dataclass(frozen=True)
class SearchPage(Generic[T]):
    """Bounded page returned by the public query service.

    ``T`` is the row type: the default path carries legacy tuple rows, and the
    opt-in ``explain=True`` path carries structured dict rows.  The pagination
    fields are identical in both, so callers can treat the page uniformly.
    """

    results: list[T]
    limit: int
    offset: int
    has_more: bool
    next_offset: int | None


# Bounds for filter inputs (fail closed before SQL).
MAX_FILTER_VALUES = 50
MAX_SOURCE_TYPE_LEN = 50
MAX_SESSION_ID_LEN = 128


@dataclass(frozen=True)
class SearchFilters:
    """Typed, backward-compatible filter contract for hybrid search.

    ``None``/empty collections keep the current unfiltered behavior exactly.
    ``from_inputs`` is the only public constructor: it validates raw sequences
    (rejects a single string treated as an array, booleans, non-strings, more
    than ``MAX_FILTER_VALUES`` entries, duplicates) before canonicalizing to
    sorted tuples.  Time bounds are UTC-aware datetimes; ``time_from`` is
    inclusive on ``timestamp_start`` and ``time_to`` is exclusive, and rows
    with a NULL ``timestamp_start`` never match an explicit time filter.
    """

    source_types: tuple[str, ...] = ()
    session_ids: tuple[str, ...] = ()
    time_from: datetime | None = None
    time_to: datetime | None = None

    def __post_init__(self) -> None:
        # Re-validate canonical state so direct construction cannot bypass
        # from_inputs' fail-closed guarantees.  Canonical tuples must be
        # unique, sorted, bounded, and every item a non-empty string.
        for name, values, max_len in (
            ("source_types", self.source_types, MAX_SOURCE_TYPE_LEN),
            ("session_ids", self.session_ids, MAX_SESSION_ID_LEN),
        ):
            if not isinstance(values, tuple):
                raise QueryError(f"{name} must be a canonical tuple", code="invalid_filter_type")
            if len(values) > MAX_FILTER_VALUES:
                raise QueryError(f"{name} exceeds {MAX_FILTER_VALUES} values", code="filter_count_exceeded")
            if values != tuple(sorted(values)):
                raise QueryError(f"{name} must be canonical sorted", code="invalid_filter_type")
            for item in values:
                if isinstance(item, bool) or not isinstance(item, str):
                    raise QueryError(f"{name} contains a non-string value", code="invalid_filter_value")
                if not item or len(item) > max_len:
                    raise QueryError(f"{name} contains an invalid value", code="invalid_filter_value")
            if len(set(values)) != len(values):
                raise QueryError(f"{name} contains a duplicate value", code="duplicate_filter_value")
        for name, value in (("time_from", self.time_from), ("time_to", self.time_to)):
            if value is not None:
                if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
                    raise QueryError(f"{name} must be UTC-aware (offset 0)", code="invalid_timezone")
        if self.time_from is not None and self.time_to is not None and self.time_from >= self.time_to:
            raise QueryError("time_from must be before time_to", code="invalid_time_range")

    @classmethod
    def from_inputs(
        cls,
        *,
        source_types: Any = None,
        session_ids: Any = None,
        time_from: Any = None,
        time_to: Any = None,
    ) -> "SearchFilters":
        """Validate and canonicalize raw filter inputs (fail closed)."""
        src = cls._validate_values("source_types", source_types, max_len=MAX_SOURCE_TYPE_LEN)
        sess = cls._validate_values("session_ids", session_ids, max_len=MAX_SESSION_ID_LEN)
        tf = cls._validate_time("time_from", time_from)
        tt = cls._validate_time("time_to", time_to)
        if tf is not None and tt is not None and tf >= tt:
            raise QueryError("time_from must be before time_to", code="invalid_time_range")
        return cls(source_types=src, session_ids=sess, time_from=tf, time_to=tt)

    @staticmethod
    def _validate_values(name: str, value: Any, *, max_len: int) -> tuple[str, ...]:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            raise QueryError(f"{name} must be a list or tuple of strings", code="invalid_filter_type")
        items = list(value)
        if len(items) > MAX_FILTER_VALUES:
            raise QueryError(f"{name} exceeds {MAX_FILTER_VALUES} values", code="filter_count_exceeded")
        seen: set[str] = set()
        for item in items:
            if isinstance(item, bool) or not isinstance(item, str):
                raise QueryError(f"{name} contains a non-string value", code="invalid_filter_value")
            if not item:
                raise QueryError(f"{name} contains an empty string", code="invalid_filter_value")
            if len(item) > max_len:
                raise QueryError(f"{name} value exceeds length limit", code="invalid_filter_value")
            if item in seen:
                raise QueryError(f"{name} contains a duplicate value", code="duplicate_filter_value")
            seen.add(item)
        return tuple(sorted(items))

    # RFC3339 full timestamp: YYYY-MM-DD'T'HH:MM:SS[.fraction]('Z'|'+00:00')
    _RFC3339_RE = re.compile(
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)$"
    )

    @staticmethod
    def _validate_time(name: str, value: Any) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, str):
            # Strict RFC3339 UTC: full date/time with seconds, optional fraction,
            # and ONLY Z or +00:00 suffix.  Reject space-separated, compact,
            # missing-seconds, naive, and non-zero-offset forms.
            if not SearchFilters._RFC3339_RE.match(value):
                raise QueryError(f"{name} is not a valid RFC3339 UTC timestamp", code="invalid_time_format")
            raw = value[:-1] + "+00:00" if value.endswith("Z") else value
            try:
                parsed = datetime.fromisoformat(raw)
            except ValueError as exc:
                raise QueryError(f"{name} is not a valid RFC3339 UTC timestamp", code="invalid_time_format") from exc
            if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
                raise QueryError(f"{name} must be UTC", code="invalid_timezone")
        elif isinstance(value, datetime):
            if value.tzinfo is None or value.utcoffset() != timedelta(0):
                raise QueryError(f"{name} must be UTC-aware", code="invalid_timezone")
            parsed = value
        else:
            raise QueryError(f"{name} must be a datetime or RFC3339 string", code="invalid_time_format")
        return parsed.replace(tzinfo=UTC)

    @property
    def is_empty(self) -> bool:
        return not self.source_types and not self.session_ids and self.time_from is None and self.time_to is None


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
    global REPLAY_MANIFEST, PG_CRED_PATH, DATABASE_DSN, EMBED_DIM
    if settings.voyage_api_url is not None:
        VOYAGE_API_URL = settings.voyage_api_url
    if settings.voyage_model is not None:
        VOYAGE_MODEL = settings.voyage_model
    if settings.voyage_key_file is not None:
        VOYAGE_KEY_PATH = str(settings.voyage_key_file)
    if settings.voyage_api_key is not None:
        VOYAGE_API_KEY = settings.voyage_api_key
    if settings.replay_manifest is not None:
        REPLAY_MANIFEST = str(settings.replay_manifest)
    if settings.embedding_provider == "replay" and settings.replay_manifest is not None:
        # The replay provider's rows are filtered by the fixture's true model
        # identity (repo id + pinned revision), so search only touches rows the
        # replay path actually wrote.

        from shiori.embedding_replay import ReplayError, replay_model_identity

        try:
            VOYAGE_MODEL = replay_model_identity(settings.replay_manifest)
        except ReplayError:
            raise QueryError(
                "replay embedding provider could not resolve the fixture model identity",
                code="replay_model_identity_unavailable",
            ) from None
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
    if EMBEDDING_PROVIDER == "replay":
        from pathlib import Path

        from shiori.embedding_replay import ReplayEmbedder, ReplayError

        if not REPLAY_MANIFEST:
            raise QueryError(
                "replay embedding provider requires SHIORI_REPLAY_MANIFEST",
                code="replay_manifest_not_configured",
            )
        embedder = ReplayEmbedder.from_files(
            Path(REPLAY_MANIFEST),
            Path(REPLAY_MANIFEST).with_name("vectors.json"),
        )
        try:
            return embedder.embed(text, input_type="query")
        except ReplayError as exc:
            raise QueryError(f"replay embedding failed: {exc}", code="replay_embed_failed") from exc
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


def _filter_predicate(filters: SearchFilters | None) -> tuple[str, tuple[Any, ...]]:
    """Build the shared parameterized WHERE fragment for every candidate SQL path.

    Returns (sql_fragment, params).  The fragment is appended before ranking /
    candidate limits.  No string interpolation of values ever occurs.
    """
    if filters is None or filters.is_empty:
        return "", ()
    clauses: list[str] = []
    params: list[Any] = []
    if filters.source_types:
        clauses.append(f"source_type IN ({', '.join(['%s'] * len(filters.source_types))})")
        params.extend(filters.source_types)
    if filters.session_ids:
        clauses.append(f"session_id IN ({', '.join(['%s'] * len(filters.session_ids))})")
        params.extend(filters.session_ids)
    if filters.time_from is not None or filters.time_to is not None:
        # Explicit time filter: NULL timestamp_start never matches.
        clauses.append("timestamp_start IS NOT NULL")
        if filters.time_from is not None:
            clauses.append("timestamp_start >= %s")
            params.append(filters.time_from)
        if filters.time_to is not None:
            clauses.append("timestamp_start < %s")
            params.append(filters.time_to)
    return " AND " + " AND ".join(clauses), tuple(params)


def _candidate_matches_filters(row: tuple[Any, ...], filters: SearchFilters | None) -> bool:
    """Invariant for raw SQL candidate rows (layout: id, content, score,
    timestamp_start, session_id, source_type, embedding, created_at, model, dim).

    A naive timestamp from the DB is never auto-attached a timezone: under an
    explicit time filter a naive timestamp is treated as non-matching (fail
    closed) rather than guessed UTC.
    """
    if filters is None or filters.is_empty:
        return True
    ts = row[3]
    session_id = row[4]
    source_type = row[5]
    if filters.source_types and source_type not in filters.source_types:
        return False
    if filters.session_ids and session_id not in filters.session_ids:
        return False
    if filters.time_from is not None or filters.time_to is not None:
        if ts is None or ts.tzinfo is None:
            return False
        aware = ts.astimezone(UTC)
        if filters.time_from is not None and aware < filters.time_from:
            return False
        if filters.time_to is not None and aware >= filters.time_to:
            return False
    return True


def _row_matches_filters(row: tuple[Any, ...], filters: SearchFilters | None) -> bool:
    """Post-SQL invariant: every final result must satisfy the filters.

    Final result rows have layout
    ``(content, score, timestamp_start, session_id, source_type, model, dim)``.
    A leakage here fails closed (``filter_leakage``) instead of silently dropping.
    """
    if filters is None or filters.is_empty:
        return True
    ts = row[2]
    session_id = row[3]
    source_type = row[4]
    if filters.source_types and source_type not in filters.source_types:
        return False
    if filters.session_ids and session_id not in filters.session_ids:
        return False
    if filters.time_from is not None or filters.time_to is not None:
        # A naive timestamp is never matched under an explicit time filter
        # (fail closed); we do not guess a timezone.
        if ts is None or ts.tzinfo is None:
            return False
        aware = ts.astimezone(UTC)
        if filters.time_from is not None and aware < filters.time_from:
            return False
        if filters.time_to is not None and aware >= filters.time_to:
            return False
    return True


def _coerce_filters(filters: SearchFilters | None) -> SearchFilters:
    """Type-gate the public filters parameter (None or SearchFilters only)."""
    if filters is None:
        return SearchFilters()
    if not isinstance(filters, SearchFilters):
        raise QueryError("filters must be a SearchFilters instance", code="invalid_filter_type")
    return filters


# Phase 4E2 recency grammar: a standalone English ``latest`` token, a leading
# CJK/JP prefix (``最新``/``直近``, after NFKC, ignoring leading whitespace), or
# a relative ``last <1..365> day|days`` sequence is explicit "prefer the
# newest/recent" intent; the explicit veto ``not latest`` / ``not the latest``
# conservatively disables text recency intent.  Matching is NFKC+casefold then
# token-boundary regexes so ``latestness``/``mylatest``/``latest_2``/``last
# 0 days``/``last 366 days`` do not hit.  Small and documented.
_NOT_LATEST_TOKEN_RE = re.compile(r"(?<!\w)not\s+(?:the\s+)?latest(?!\w)")
_LATEST_TOKEN_RE = re.compile(r"(?<!\w)latest(?!\w)")
_RELATIVE_DAYS_RE = re.compile(r"(?<!\w)last\s+([0-9]{1,3})\s+days?(?!\w)")
_RELATIVE_CN_DAYS_RE = re.compile(r"^过去\s*([0-9]{1,3})\s*天")
_RELATIVE_JA_DAYS_RE = re.compile(r"^過去\s*([0-9]{1,3})\s*日")


def _has_recency_intent(query_text: str) -> bool:
    """True when the query text expresses explicit recency intent."""
    folded = unicodedata.normalize("NFKC", query_text).casefold()
    if _NOT_LATEST_TOKEN_RE.search(folded) is not None:
        # Explicit veto: ``not latest``/``not the latest`` conservatively means
        # no recency intent.
        return False
    if _LATEST_TOKEN_RE.search(folded) is not None:
        return True
    # Japanese negated-prefix veto: ``最新ではない``/``直近ではない`` at the
    # start of the lstrip'd text conservatively means no recency intent.  This
    # must precede the positive CJK prefix check.
    stripped = folded.lstrip()
    if stripped.startswith(("最新ではない", "直近ではない")):
        return False
    # CJK/JP prefix operators: ``最新``/``直近`` after leading whitespace are
    # explicit "prefer the newest/recent" (documented prefix grammar, not
    # arbitrary scanning).
    if stripped.startswith(("最新", "直近")):
        return True
    # Relative days: ``last N day|days`` with 1 <= N <= 365.
    m = _RELATIVE_DAYS_RE.search(folded)
    if m is not None:
        n = int(m.group(1))
        if 1 <= n <= 365:
            return True
    # Chinese/Japanese relative days, anchored at the start of the (lstrip'd)
    # folded text: ``过去 <1..365> 天`` / ``過去 <1..365> 日``.
    for rel_re in (_RELATIVE_CN_DAYS_RE, _RELATIVE_JA_DAYS_RE):
        m = rel_re.match(stripped)
        if m is not None:
            n = int(m.group(1))
            if 1 <= n <= 365:
                return True
    return False


def search(query, limit=DEFAULT_LIMIT, offset=0, *, filters: SearchFilters | None = None, explain: bool = False):
    query = _validate_query_text(query)
    limit, offset = _normalise_search_args(limit, offset)
    filters = _coerce_filters(filters)
    filter_sql, filter_params = _filter_predicate(filters)
    eval_ctx = _current_eval()
    config = eval_ctx.config if eval_ctx is not None else StageConfig()
    config.validate()
    # The query embedding is only needed by the dense (vector) channel. Fetch
    # and validate it ONLY inside the dense-enabled path, so a dense-off
    # ablation never calls the embedding provider (no model/API/replay lookup)
    # and its latency/failure surface is a true ablation.
    query_embedding = None
    if config.dense:
        # Obtain the provider result before opening a database connection.  This
        # avoids leaking a connection when the provider returns malformed data
        # or an embedding dimension that does not match the configured schema.
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
    vector_rows = []
    if config.dense:
        _t0 = time.perf_counter()
        try:
            cur.execute("""
                SELECT id, content, 1 - (embedding <=> %s::vector) as vscore,
                       timestamp_start, session_id, source_type, embedding::text, created_at,
                       embedding_model, vector_dims(embedding)
                FROM session_chunks
                WHERE embedding IS NOT NULL
                  AND embedding_model = %s
                  AND vector_dims(embedding) = %s
                """ + filter_sql + """
                ORDER BY embedding <=> %s::vector, id
                LIMIT %s
            """, (str(query_embedding), VOYAGE_MODEL, EMBED_DIM, *filter_params, str(query_embedding), pool))
            vector_rows = cur.fetchall()
            for row in vector_rows:
                if not _candidate_matches_filters(row, filters):
                    raise QueryError("vector candidate violated the active filters", code="filter_leakage")
        except QueryError:
            # Fail closed WITHOUT leaking the connection/cursor.
            try:
                cur.close()
            finally:
                conn.close()
            raise
        except Exception as exc:
            conn.rollback()
            cur.close()
            conn.close()
            raise QueryError("search backend is unavailable", code="search_unavailable") from exc
        _dense_s = time.perf_counter() - _t0
    else:
        _dense_s = 0.0

    # Phase 4D trace: dense (vector) stage. Disabled stages emit only a
    # stage_disabled summary with the (real, ~0) elapsed time; no candidates.
    if config.dense:
        dense_events = [
            {
                "doc_id": row[0],
                "session_id": row[4],
                "source_type": row[5],
                "rank": rank,
                "score": row[2],
                "reason": "vector",
            }
            for rank, row in enumerate(vector_rows, start=1)
        ]
    else:
        dense_events = []
    _emit_eval("dense", dense_events + [{"latency_ms": _dense_s * 1000.0, "reason": "stage" if config.dense else "stage_disabled"}])

    # BM25 (tsvector) search -----------------------------------------------
    tsq = _build_tsquery(query)
    bm25_rows = []
    _tsrank_s = 0.0
    if config.lexical and tsq:
        _t1 = time.perf_counter()
        try:
            cur.execute("""
                SELECT id, content, ts_rank_cd(content_tsvector, to_tsquery('simple', %s)) as tscore,
                       timestamp_start, session_id, source_type, embedding::text, created_at,
                       embedding_model, vector_dims(embedding)
                FROM session_chunks
                WHERE content_tsvector @@ to_tsquery('simple', %s)
                  AND embedding_model = %s
                  AND (embedding IS NULL OR vector_dims(embedding) = %s)
                """ + filter_sql + """
                ORDER BY tscore DESC, id
                LIMIT %s
            """, (tsq, tsq, VOYAGE_MODEL, EMBED_DIM, *filter_params, pool))
            bm25_rows = cur.fetchall()
            for row in bm25_rows:
                if not _candidate_matches_filters(row, filters):
                    raise QueryError("ts_rank_cd candidate violated the active filters", code="filter_leakage")
        except QueryError:
            try:
                cur.close()
            finally:
                conn.close()
            raise
        except Exception:
            # tsvector column might not exist yet during migration. The failed
            # query aborts the transaction; roll it back so the pg_trgm fallback
            # below still runs on a usable connection.
            conn.rollback()
        _tsrank_s = time.perf_counter() - _t1

    # Phase 4D trace: ts_rank_cd (lexical) stage. Disabled emits only a
    # stage_disabled summary. The trigram fallback below is a separate
    # observable stage of the SAME lexical channel.
    if config.lexical:
        tsrank_events = [
            {
                "doc_id": row[0],
                "session_id": row[4],
                "source_type": row[5],
                "rank": rank,
                "score": row[2],
                "reason": "ts_rank_cd",
            }
            for rank, row in enumerate(bm25_rows, start=1)
        ]
    else:
        tsrank_events = []
    _emit_eval("ts_rank_cd", tsrank_events + [{"latency_ms": _tsrank_s * 1000.0, "reason": "stage" if config.lexical else "stage_disabled"}])

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
    _exact_s = 0.0
    if config.exact and len(query.strip()) <= 20:
        _t2 = time.perf_counter()
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
                """ + filter_sql + """
                ORDER BY timestamp_start DESC NULLS LAST, id
                LIMIT %s
            """, (f"%{escaped}%", VOYAGE_MODEL, EMBED_DIM, *filter_params, pool))
            exact_rows = cur.fetchall()
            for row in exact_rows:
                if not _candidate_matches_filters(row, filters):
                    raise QueryError("exact candidate violated the active filters", code="filter_leakage")
        except QueryError:
            try:
                cur.close()
            finally:
                conn.close()
            raise
        except Exception:
            conn.rollback()
        _exact_s = time.perf_counter() - _t2

    # Phase 4D trace: exact (substring) stage. Disabled emits only a
    # stage_disabled summary.
    if config.exact:
        exact_events = [
            {
                "doc_id": row[0],
                "session_id": row[4],
                "source_type": row[5],
                "rank": rank,
                "score": row[2],
                "reason": "exact_substring",
            }
            for rank, row in enumerate(exact_rows, start=1)
        ]
    else:
        exact_events = []
    _emit_eval("exact", exact_events + [{"latency_ms": _exact_s * 1000.0, "reason": "stage" if config.exact else "stage_disabled"}])

    # If BM25 returned nothing, fall back to trigram similarity
    trigram_used = False
    trigram_rows = []
    _trigram_s = 0.0
    if config.lexical and not bm25_rows:
        _t3 = time.perf_counter()
        try:
            cur.execute("""
                SELECT id, content, similarity(content, %s) as tscore,
                       timestamp_start, session_id, source_type, embedding::text, created_at,
                       embedding_model, vector_dims(embedding)
                FROM session_chunks
                WHERE content %% %s
                  AND embedding_model = %s
                  AND (embedding IS NULL OR vector_dims(embedding) = %s)
                """ + filter_sql + """
                ORDER BY similarity(content, %s) DESC, id
                LIMIT %s
            """, (query, query, VOYAGE_MODEL, EMBED_DIM, *filter_params, query, pool))
            trigram_rows = cur.fetchall()
            for row in trigram_rows:
                if not _candidate_matches_filters(row, filters):
                    raise QueryError("trigram candidate violated the active filters", code="filter_leakage")
            trigram_used = True
            # Preserve production behavior: the trigram results remain the
            # lexical channel for RRF fusion below (the trace reports the same
            # rows under the "trigram" stage name, separately observable).
            bm25_rows = trigram_rows
        except QueryError:
            try:
                cur.close()
            finally:
                conn.close()
            raise
        except Exception:
            conn.rollback()
            trigram_rows = []
        _trigram_s = time.perf_counter() - _t3

    # Phase 4D trace: trigram fallback stage. Disabled lexical emits only a
    # stage_disabled summary. When the fallback did not run (ts_rank_cd hit),
    # an empty event keeps the stage observable and separately distinguishable.
    _emit_eval(
        "trigram",
        [
            {
                "doc_id": row[0],
                "session_id": row[4],
                "source_type": row[5],
                "rank": rank,
                "score": row[2],
                "reason": "trigram_fallback",
            }
            for rank, row in enumerate(trigram_rows if trigram_used else [], start=1)
        ]
        + [{"latency_ms": _trigram_s * 1000.0, "reason": "stage" if config.lexical else "stage_disabled"}],
    )

    cur.close()
    conn.close()

    # RRF fusion -----------------------------------------------------------
    k = 60  # RRF constant
    scores = {}   # id -> rrf_score
    meta = {}     # id -> (content, timestamp, session_id, source_type, embedding_str, created_at, model, dim)
    _t4 = time.perf_counter()

    if config.dense:
        for rank, row in enumerate(vector_rows, 1):
            rid, content, vscore, ts, sid, stype, emb_str, created_at, model, dimension = _unpack_search_row(row)
            scores[rid] = scores.get(rid, 0) + 1.0 / (k + rank)
            meta[rid] = (content, ts, sid, stype, emb_str, created_at, model, dimension)

    if config.lexical:
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
    if config.exact:
        for rank, row in enumerate(exact_rows, 1):
            rid, content, tscore, ts, sid, stype, emb_str, created_at, model, dimension = _unpack_search_row(row)
            bonus_rank = 1  # exact matches rank at the top of their channel
            scores[rid] = scores.get(rid, 0) + 1.0 / (k + bonus_rank)
            if rid not in meta:
                meta[rid] = (content, ts, sid, stype, emb_str, created_at, model, dimension)
    _rrf_s = time.perf_counter() - _t4

    # Phase 4D trace: rrf fusion stage (pre-temporal). RRF is the multi-channel
    # fusion mechanism (always applied over enabled channels); it is never a
    # disabled stage. Rank by descending score.
    rrf_ranked = sorted(scores, key=lambda rid: (-scores[rid], str(rid)))
    _emit_eval(
        "rrf",
        [
            {
                "doc_id": rid,
                "session_id": meta[rid][2],
                "source_type": meta[rid][3],
                "rank": rank,
                "score": scores[rid],
                "reason": "rrf",
            }
            for rank, rid in enumerate(rrf_ranked, start=1)
        ]
        + [{"latency_ms": _rrf_s * 1000.0, "reason": "stage"}],
    )

    # Temporal decay --------------------------------------------------------
    # Phase 4E2: temporal decay applies ONLY under explicit temporal intent —
    # structured time bounds on SearchFilters, or a recognized recency-intent
    # token in the query text (grammar slice 1: standalone ``latest``).  An
    # ordinary fact/history query without either must not decay, so
    # older-but-more-relevant results are not penalized by age alone.
    # Formula/half-life/score are unchanged.
    _temporal_s = 0.0
    _explicit_time_intent = filters is not None and (
        filters.time_from is not None or filters.time_to is not None
    )
    _recency_intent = _has_recency_intent(query)
    if config.temporal and (_explicit_time_intent or _recency_intent):
        _t6 = time.perf_counter()
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
        _temporal_s = time.perf_counter() - _t6

    # Phase 4D trace: temporal decay stage (post-decay, pre-sort). Disabled
    # (config.temporal off, or no explicit time intent under Phase 4E2) emits
    # only a stage_disabled summary (no decay applied). Rank by descending
    # score. Allowlist only.
    temporal_ranked = sorted(scores, key=lambda rid: (-scores[rid], str(rid)))
    if config.temporal and (_explicit_time_intent or _recency_intent):
        temporal_events = [
            {
                "doc_id": rid,
                "session_id": meta[rid][2],
                "source_type": meta[rid][3],
                "rank": rank,
                "score": scores[rid],
                "reason": "temporal_decay",
            }
            for rank, rid in enumerate(temporal_ranked, start=1)
        ]
        _temporal_stage = "stage"
    else:
        temporal_events = []
        _temporal_stage = "stage_disabled"
    _emit_eval("temporal", temporal_events + [{"latency_ms": _temporal_s * 1000.0, "reason": _temporal_stage}])

    # Sort by decayed RRF score --------------------------------------------
    ranked = sorted(scores.keys(), key=lambda rid: (-scores[rid], str(rid)))

    # MMR deduplication -----------------------------------------------------
    _t5 = time.perf_counter()
    if config.dedup:
        # Enabled: run the MMR loop (parses embeddings, calls _cosine_sim) and
        # record keep/drop decisions with stable reason codes.  Each selected
        # embedding keeps its content/session/source provenance, so a candidate
        # is only dropped when it is a true duplicate of a selected chunk: same
        # embedding similarity above the threshold AND identical content,
        # session_id, and source_type.  A related-but-distinct chunk (different
        # content or different provenance) is preserved as separate evidence.
        selected = []
        selected_ids: list[str] = []
        selected_embeddings: list[tuple] = []
        dedup_events: list[dict] = []
        for rid in ranked:
            if len(selected) >= result_limit:
                break

            content, ts, sid, stype, emb_str, created_at, model, dimension = meta[rid]

            # Parse embedding for MMR comparison
            too_similar = False
            if emb_str and selected_embeddings:
                try:
                    emb = [float(x) for x in emb_str.strip("[]").split(",")]
                    for sel_emb, sel_content, sel_sid, sel_stype in selected_embeddings:
                        if (
                            _cosine_sim(emb, sel_emb) > MMR_SIM_THRESHOLD
                            and content == sel_content
                            and sid == sel_sid
                            and stype == sel_stype
                        ):
                            too_similar = True
                            break
                    if too_similar:
                        dedup_events.append({
                            "doc_id": rid,
                            "session_id": sid,
                            "source_type": stype,
                            "rank": len(selected) + 1,
                            "score": scores[rid],
                            "reason": "mmr_dedup",
                        })
                        continue
                    selected_embeddings.append((emb, content, sid, stype))
                except (ValueError, AttributeError):
                    pass
            elif emb_str:
                try:
                    emb = [float(x) for x in emb_str.strip("[]").split(",")]
                    selected_embeddings.append((emb, content, sid, stype))
                except (ValueError, AttributeError):
                    pass

            selected.append((content, scores[rid], ts, sid, stype, model, dimension))
            selected_ids.append(rid)
            dedup_events.append({
                "doc_id": rid,
                "session_id": sid,
                "source_type": stype,
                "rank": len(selected),
                "score": scores[rid],
                "reason": "mmr_keep",
            })
        _dedup_s = time.perf_counter() - _t5
        selected_events = dedup_events
    else:
        # Disabled: TRUE bypass. Branch BEFORE the MMR loop so no embedding is
        # parsed and _cosine_sim is never called; take the ranked candidates
        # directly (undeduped) and emit a stage_disabled summary.
        _dedup_s = time.perf_counter() - _t5
        selected_events = []
        selected = [
            (meta[rid][0], scores[rid], meta[rid][1], meta[rid][2], meta[rid][3], meta[rid][6], meta[rid][7])
            for rid in ranked[:result_limit]
        ]
        selected_ids = list(ranked[:result_limit])
    _emit_eval(
        "dedup",
        selected_events
        + [{"latency_ms": _dedup_s * 1000.0, "reason": "stage" if config.dedup else "stage_disabled"}],
    )

    # Post-SQL invariant: every final candidate must satisfy the filters.
    # A leakage here fails closed (filter_leakage) instead of silently dropping.
    if not filters.is_empty:
        for row in selected:
            if not _row_matches_filters(row, filters):
                raise QueryError(
                    "search result violated the active filters",
                    code="filter_leakage",
                )

    page = selected[offset : offset + limit]

    # Phase 4F1: opt-in explain output.  When disabled the default return path
    # is untouched (identical tuple list and order).  When enabled, each page
    # row becomes a structured dict carrying the standard fields at top level
    # and an ``explain`` sub-dict built from the REAL candidate pools (vector /
    # lexical ts_rank_cd+trigram / exact), not a recomputation.
    if not explain:
        return page

    # Build per-channel candidate rank maps from the real candidate pools.
    # Row id is column 0; rank is the 1-based position in that channel's pool.
    dense_rank = {row[0]: rank for rank, row in enumerate(vector_rows, start=1)}
    lexical_rank = {row[0]: rank for rank, row in enumerate(bm25_rows, start=1)}
    exact_rank = {row[0]: rank for rank, row in enumerate(exact_rows, start=1)}

    explained: list[dict[str, Any]] = []
    page_ids = selected_ids[offset : offset + len(page)]
    for row, rid in zip(page, page_ids):
        channels = {
            "dense": {
                "matched": rid in dense_rank,
                "candidate_rank": dense_rank.get(rid),
            },
            "lexical": {
                "matched": rid in lexical_rank,
                "candidate_rank": lexical_rank.get(rid),
            },
            "exact": {
                "matched": rid in exact_rank,
                "candidate_rank": exact_rank.get(rid),
            },
        }
        matched_count = sum(1 for c in channels.values() if c["matched"])
        result: dict[str, Any] = {
            "content": row[0],
            "score": row[1],
            "timestamp": row[2],
            "session_id": row[3],
            "source_type": row[4],
            "embedding_model": row[5] if len(row) > 5 else None,
            "embedding_dimension": row[6] if len(row) > 6 else None,
            "explain": {
                "score_kind": "rrf",
                "adjustments": [],
                "channels": channels,
                "matched_channel_count": matched_count,
                "multi_channel": matched_count >= 2,
            },
        }
        explained.append(result)

    return explained


def search_page(
    query_text: str,
    *,
    limit: int = MAX_PAGE_LIMIT,
    offset: int = 0,
    filters: SearchFilters | None = None,
    explain: bool = False,
) -> SearchPage[Any]:
    """Return a bounded, stable page without exposing an unbounded count query.

    ``explain=False`` keeps the exact legacy call shape to ``search`` and
    returns tuple rows; ``explain=True`` forwards the flag so ``search``
    returns slice1 structured dict rows.  Pagination semantics are unchanged.
    """
    query_text = _validate_query_text(query_text)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise QueryError("limit must be a positive integer", code="invalid_limit")
    if limit > MAX_PAGE_LIMIT:
        raise QueryError(f"limit must be at most {MAX_PAGE_LIMIT}", code="limit_out_of_bounds")
    _, offset = _normalise_search_args(limit, offset)
    filters = _coerce_filters(filters)
    # Ask the compatibility search function for one look-ahead row.  Calling
    # it without ``offset`` at zero keeps monkeypatched/legacy integrations
    # working while still making ``has_more`` truthful.
    requested = offset + limit + 1
    if explain:
        if filters.is_empty:
            all_rows = search(query_text, limit=requested, explain=True)
        else:
            all_rows = search(query_text, limit=requested, filters=filters, explain=True)
    else:
        if filters.is_empty:
            all_rows = search(query_text, limit=requested)
        else:
            all_rows = search(query_text, limit=requested, filters=filters)
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
    parser.add_argument("--source-type", action="append", default=[], help="Filter by exact source_type (repeatable)")
    parser.add_argument("--session-id", action="append", default=[], help="Filter by exact session_id (repeatable)")
    parser.add_argument("--time-from", default=None, help="UTC RFC3339 lower bound (inclusive on timestamp_start)")
    parser.add_argument("--time-to", default=None, help="UTC RFC3339 upper bound (exclusive on timestamp_start)")
    args = parser.parse_args(argv)

    settings = load_config(config_path=args.config, legacy_openclaw=args.legacy_openclaw)
    settings.require_database()
    settings.require_embedding()
    apply_settings(settings)

    filters = SearchFilters.from_inputs(
        source_types=args.source_type if args.source_type else None,
        session_ids=args.session_id if args.session_id else None,
        time_from=args.time_from,
        time_to=args.time_to,
    )
    results = search(args.query, args.limit, args.offset, filters=filters)

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
