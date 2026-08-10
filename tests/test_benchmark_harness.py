"""Tests for the retrieval-quality benchmark harness (task #11).

These verify the metrics computation is recomputable and correct on a tiny
synthetic case, independent of the full corpus fixture or the embedding model.
"""

from __future__ import annotations

import numpy as np

from benchmark.harness.metrics import (
    QueryJudgment,
    evaluate,
    ndcg_at_k,
    rank_documents,
    recall_at_k,
    reciprocal_rank,
)


def test_recall_at_k_and_mrr_basic():
    ranked = [0, 1, 2, 3, 4]
    assert recall_at_k(ranked, {3}, k=5) == 1.0
    assert recall_at_k(ranked, {4}, k=5) == 1.0
    assert recall_at_k(ranked, {9}, k=5) == 0.0
    assert recall_at_k(ranked, {4, 9}, k=5) == 0.5
    assert reciprocal_rank(ranked, {3}, k=10) == 0.25  # 4th position


def test_ndcg_perfect_and_missed():
    relevance = {0: 3, 1: 3, 2: 3}
    assert ndcg_at_k([0, 1, 2, 3, 4], relevance, k=5) == 1.0
    assert ndcg_at_k([4, 5, 6, 0, 1], relevance, k=5) < 1.0
    assert ndcg_at_k([4, 5, 6, 7, 8], relevance, k=5) == 0.0


def test_rank_documents_orders_by_similarity():
    # document vectors orthogonal to each other, query close to doc 2
    docs = np.asarray(
        [
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )
    query = np.asarray([0.0, 0.95, 0.0], dtype=np.float64)
    assert rank_documents(query, docs, k=3)[0] == 1


def test_evaluate_buckets_and_no_evidence():
    documents = [{"id": f"d{i}"} for i in range(4)]
    queries = [
        QueryJudgment("q1", "exact", False, relevance={"d0": 3, "d1": 2}),
        QueryJudgment("q2", "no_evidence", True, relevance={}),
    ]
    # q1 nearest to d0, q2 arbitrary
    query_vectors = np.asarray(
        [
            [1.0, 0.0, 0.0, 0.0],
            [0.0, 0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )
    document_vectors = np.eye(4, dtype=np.float64)
    results, summary = evaluate(documents, queries, query_vectors, document_vectors, k=2)

    by_id = {r.query_id: r for r in results}
    # q1: relevance {d0,d1}; with k=2 ranking returns d0 (hit) but not d1,
    # so recall@5 (hits/2 relevant) = 0.5 and MRR = 1.0 (d0 at rank 1).
    assert by_id["q1"].recall5 == 0.5
    assert by_id["q1"].mrr10 == 1.0
    assert by_id["q1"].ndcg10 == 0.7039180890341347
    assert by_id["q2"].expected_no_evidence is True
    assert summary["by_category"]["exact"]["count"] == 1
    assert summary["by_category"]["no_evidence"]["count"] == 1
