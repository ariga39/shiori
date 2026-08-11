"""Structured allowlist trace contract for Phase 4D (task #18).

A trace event may contain ONLY stable, non-sensitive identifiers and numeric
observables. Content, raw query text, embeddings, keys, and paths must NEVER
enter a trace event, whether or not they look synthetic. This module validates
trace events against the frozen allowlist and fails closed per-field.
"""

from __future__ import annotations

from typing import Any

# Frozen per-stage allowed keys (allowlist, not redaction).
TRACE_EVENT_FIELDS = frozenset(
    {
        "stage",        # dense | ts_rank_cd | exact | trigram | rrf | temporal | dedup
        "doc_id",       # stable document id
        "session_id",   # stable session id
        "source_type",  # stable source kind
        "rank",         # 1-based rank within the stage
        "score",        # numeric stage score
        "reason",       # stable reason code
        "latency_ms",   # numeric latency in milliseconds
    }
)

STAGES = ("dense", "ts_rank_cd", "exact", "trigram", "rrf", "temporal", "dedup")

# Stable reason codes emitted by the production seam.
REASONS = frozenset(
    {
        "vector",
        "ts_rank_cd",
        "exact_substring",
        "trigram_fallback",
        "rrf",
        "temporal_decay",
        "mmr_keep",
        "mmr_dedup",
        "stage",  # stage-level latency marker (no doc fields)
        "stage_disabled",  # stage skipped under an ablation (no candidate events)
    }
)


class TraceError(ValueError):
    """Raised when a trace event violates the allowlist contract."""


def validate_trace_event(event: dict[str, Any]) -> None:
    """Fail closed on any field not in the allowlist or any invalid value."""
    if not isinstance(event, dict):
        raise TraceError("trace event must be an object")
    unknown = set(event) - TRACE_EVENT_FIELDS
    if unknown:
        raise TraceError(f"trace event leaked forbidden field(s): {sorted(unknown)}")

    stage = event.get("stage")
    if stage is not None and stage not in STAGES:
        raise TraceError(f"trace event has unknown stage {stage!r}")

    reason = event.get("reason")
    if reason is not None and reason not in REASONS:
        raise TraceError(f"trace event has unknown reason code {reason!r}")

    score = event.get("score")
    if score is not None:
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            raise TraceError(f"trace event score must be numeric, got {score!r}")
        import math

        if not math.isfinite(float(score)):
            raise TraceError("trace event score must be finite")

    rank = event.get("rank")
    if rank is not None and (isinstance(rank, bool) or not isinstance(rank, int) or rank < 1):
        raise TraceError(f"trace event rank must be a positive integer, got {rank!r}")

    latency = event.get("latency_ms")
    if latency is not None:
        if isinstance(latency, bool) or not isinstance(latency, (int, float)):
            raise TraceError(f"trace event latency_ms must be numeric, got {latency!r}")

    for key in ("doc_id", "session_id", "source_type"):
        value = event.get(key)
        if value is not None and not isinstance(value, str):
            raise TraceError(f"trace event {key} must be a string, got {value!r}")


def validate_trace(events: list[dict[str, Any]]) -> None:
    """Validate a full trace (list of per-stage events)."""
    for event in events:
        validate_trace_event(event)
