"""Strict vector validation for the task #11 benchmark.

Fail closed on: duplicate document/query ids, missing or extra vectors,
wrong dimension, non-finite values, non-normalized vectors. Loaded before any
ranking so corrupted inputs are rejected, not silently truncated by zip/dot.
"""

from __future__ import annotations

import math


class VectorValidationError(ValueError):
    """Raised when vectors fail strict validation."""


def validate_vectors(
    vectors: dict,
    *,
    expected_doc_ids: set[str],
    expected_query_ids: set[str],
    expected_dim: int = 1024,
    norm_tolerance: float = 1e-3,
) -> None:
    """Validate document + query vectors strictly; raise on any violation.

    `vectors` is the parsed JSON structure from generate_vectors.py /
    run_benchmark.py (documents: [{id, embedding}], queries: [{query_id,
    embedding}]).
    """
    if not isinstance(vectors, dict):
        raise VectorValidationError("vectors root must be an object")

    documents = vectors.get("documents")
    queries = vectors.get("queries")
    if not isinstance(documents, list) or not isinstance(queries, list):
        raise VectorValidationError("vectors must have documents and queries lists")

    doc_emb = {}
    for item in documents:
        if not isinstance(item, dict) or "id" not in item or "embedding" not in item:
            raise VectorValidationError("document vector item missing id/embedding")
        doc_id = item["id"]
        if doc_id in doc_emb:
            raise VectorValidationError(f"duplicate document vector id: {doc_id}")
        _validate_embedding(item["embedding"], expected_dim, norm_tolerance)
        doc_emb[doc_id] = item["embedding"]

    query_emb = {}
    for item in queries:
        if not isinstance(item, dict) or "query_id" not in item or "embedding" not in item:
            raise VectorValidationError("query vector item missing query_id/embedding")
        qid = item["query_id"]
        if qid in query_emb:
            raise VectorValidationError(f"duplicate query vector id: {qid}")
        _validate_embedding(item["embedding"], expected_dim, norm_tolerance)
        query_emb[qid] = item["embedding"]

    if set(doc_emb) != expected_doc_ids:
        missing = expected_doc_ids - set(doc_emb)
        extra = set(doc_emb) - expected_doc_ids
        raise VectorValidationError(f"document vectors missing={sorted(missing)} extra={sorted(extra)}")
    if set(query_emb) != expected_query_ids:
        missing = expected_query_ids - set(query_emb)
        extra = set(query_emb) - expected_query_ids
        raise VectorValidationError(f"query vectors missing={sorted(missing)} extra={sorted(extra)}")


def _validate_embedding(emb, expected_dim: int, norm_tolerance: float) -> None:
    if not isinstance(emb, list):
        raise VectorValidationError("embedding must be a list")
    if len(emb) != expected_dim:
        raise VectorValidationError(f"embedding dim {len(emb)} != expected {expected_dim}")
    if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in emb):
        raise VectorValidationError("embedding contains non-finite or non-numeric values")
    norm = math.sqrt(sum(v * v for v in emb))
    if abs(norm - 1.0) > norm_tolerance:
        raise VectorValidationError(f"embedding not L2-normalized (norm={norm:.6f})")
