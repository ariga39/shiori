"""Evaluator core for Phase 4D (task #18).

Pure, CI-safe helpers: frozen metric formulas (matching task #11), tune/holdout
isolation. Traces are governed by the separate allowlist contract in
benchmark.product_eval.trace (no content/key/path may enter a trace event).
No model, network, or database imports.
"""

from __future__ import annotations

import math

# Frozen metric formulas (identical to task #11 run_benchmark definitions):
# Recall@k treats grade > 0 as relevant; MRR@k uses the first relevant rank;
# nDCG@k uses graded gain 2**grade - 1 with log2 discount.

DEFAULT_TOP_K = 10


class IsolationError(ValueError):
    """Raised when a holdout query leaks into the development/tune split."""


def recall_at_k(ranked: list[str], relevant: set[str], k: int = DEFAULT_TOP_K) -> float:
    """Recall@k: relevant-in-top-k / total-relevant (grade > 0 counts)."""
    if not relevant:
        return 0.0
    return len(set(ranked[:k]) & relevant) / len(relevant)


def reciprocal_rank(ranked: list[str], relevant: set[str], k: int = DEFAULT_TOP_K) -> float:
    """MRR@k: 1/rank of the first relevant doc (0 when none in top-k)."""
    for i, doc_id in enumerate(ranked[:k], start=1):
        if doc_id in relevant:
            return 1.0 / i
    return 0.0


def ndcg_at_k(ranked: list[str], relevance: dict[str, int], k: int = DEFAULT_TOP_K) -> float:
    """nDCG@k with graded gain 2**grade - 1 and log2 discount (frozen)."""
    rel_map = {doc_id: (2**grade - 1) for doc_id, grade in relevance.items() if grade > 0}
    if not rel_map:
        return 0.0
    dcg = 0.0
    for i, doc_id in enumerate(ranked[:k], start=1):
        if doc_id in rel_map:
            dcg += rel_map[doc_id] / math.log2(i + 1)
    ideal = sum(gain / math.log2(i + 1) for i, gain in enumerate(sorted(rel_map.values(), reverse=True), start=1))
    return dcg / ideal if ideal > 0 else 0.0


def assert_isolation(tune: set[str], holdout: set[str]) -> None:
    """Fail closed if any holdout query id appears in the tune split."""
    leak = tune & holdout
    if leak:
        raise IsolationError(f"holdout query leaked into tune split: {sorted(leak)}")
