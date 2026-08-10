"""Recomputable retrieval-quality metrics for the shiori benchmark.

The harness is deliberately provider-agnostic: it consumes a list of document
vectors, a list of query vectors, and graded judgments, then produces metrics
bucketed by query category.  It performs no product ranking tuning and never
touches chunking, filtering, decay, or reranking logic.

Metrics:
- Recall@5
- MRR@10
- nDCG@10
- retrieval-stage latency (embedding is excluded; only ranking time)
- no-evidence behavior (does the system return results / which top grade)
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

DEFAULT_K = 5
MRR_K = 10
NDCG_K = 10


@dataclass
class QueryJudgment:
    query_id: str
    category: str
    expected_no_evidence: bool
    relevance: dict[str, int] = field(default_factory=dict)  # doc_id -> grade (1..3)


def load_documents(path: Path) -> list[dict]:
    items = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                items.append(json.loads(line))
    return items


def load_queries(path: Path) -> list[QueryJudgment]:
    judgments: list[QueryJudgment] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            raw = json.loads(line)
            relevance = {j["doc_id"]: j["grade"] for j in raw.get("judgments", [])}
            judgments.append(
                QueryJudgment(
                    query_id=raw["id"],
                    category=raw["category"],
                    expected_no_evidence=raw.get("expected_no_evidence", False),
                    relevance=relevance,
                )
            )
    return judgments


def _cosine_similarity_matrix(query_vectors: np.ndarray, document_vectors: np.ndarray) -> np.ndarray:
    q = query_vectors / np.linalg.norm(query_vectors, axis=1, keepdims=True)
    d = document_vectors / np.linalg.norm(document_vectors, axis=1, keepdims=True)
    return q @ d.T


def rank_documents(
    query_vector: np.ndarray,
    document_vectors: np.ndarray,
    k: int,
) -> list[int]:
    """Return document indices ranked by cosine similarity (top-k)."""
    sims = query_vector @ document_vectors.T
    order = np.argsort(sims)[::-1][:k]
    return [int(i) for i in order]


def recall_at_k(ranked: list[int], relevant: set[int], k: int) -> float:
    if not relevant:
        return 0.0
    hit = sum(1 for doc in ranked[:k] if doc in relevant)
    return hit / len(relevant)


def reciprocal_rank(ranked: list[int], relevant: set[int], k: int = MRR_K) -> float:
    for rank, doc in enumerate(ranked[:k], start=1):
        if doc in relevant:
            return 1.0 / rank
    return 0.0


def _dcg(relevances: Iterable[int], k: int) -> float:
    return sum(rel / np.log2(idx + 2) for idx, rel in enumerate(list(relevances)[:k]))


def ndcg_at_k(ranked: list[int], relevance: dict[int, int], k: int = NDCG_K) -> float:
    gains = [relevance.get(doc, 0) for doc in ranked[:k]]
    ideal = sorted((relevance.get(doc, 0) for doc in relevance), reverse=True)
    dcg = _dcg(gains, k)
    idcg = _dcg(ideal, k)
    return dcg / idcg if idcg > 0 else 0.0


def rank_all(
    query_vectors: np.ndarray,
    document_vectors: np.ndarray,
    k: int,
) -> tuple[list[list[int]], float]:
    """Rank all queries and return (top-k doc indices per query, ranking latency s)."""
    sims = _cosine_similarity_matrix(query_vectors, document_vectors)
    start = time.perf_counter()
    order = np.argsort(sims, axis=1)[:, ::-1][:, :k]
    latency = time.perf_counter() - start
    return [list(map(int, row)) for row in order], latency


def _grade_to_gain(grade: int) -> int:
    # grade 3 -> 3, 2 -> 2, 1 -> 1 (direct linear gain)
    return max(0, int(grade))


@dataclass
class QueryResult:
    query_id: str
    category: str
    expected_no_evidence: bool
    ranked_doc_ids: list[str]
    recall5: float
    mrr10: float
    ndcg10: float
    top_grade: int
    returned_any: bool


def evaluate(
    documents: list[dict],
    queries: list[QueryJudgment],
    query_vectors: np.ndarray,
    document_vectors: np.ndarray,
    *,
    k: int = DEFAULT_K,
) -> tuple[list[QueryResult], dict]:
    doc_ids = [doc["id"] for doc in documents]
    id_to_index = {doc_id: i for i, doc_id in enumerate(doc_ids)}
    ranked_all, latency = rank_all(query_vectors, document_vectors, k)

    results: list[QueryResult] = []
    for q, ranked in zip(queries, ranked_all):
        relevant = {id_to_index[doc_id] for doc_id in q.relevance}
        relevance_graded = {id_to_index[doc_id]: _grade_to_gain(g) for doc_id, g in q.relevance.items()}
        ranked_ids = [doc_ids[i] for i in ranked]
        top_grade = max((relevance_graded.get(i, 0) for i in ranked), default=0)
        results.append(
            QueryResult(
                query_id=q.query_id,
                category=q.category,
                expected_no_evidence=q.expected_no_evidence,
                ranked_doc_ids=ranked_ids,
                recall5=recall_at_k(ranked, relevant, k),
                mrr10=reciprocal_rank(ranked, relevant, MRR_K),
                ndcg10=ndcg_at_k(ranked, relevance_graded, NDCG_K),
                top_grade=top_grade,
                returned_any=len(ranked) > 0,
            )
        )

    summary = summarize(results, latency=latency)
    return results, summary


def _mean(values: Iterable[float]) -> float:
    values = list(values)
    return float(np.mean(values)) if values else 0.0


def summarize(results: list[QueryResult], latency: float = 0.0) -> dict:
    categories = sorted({r.category for r in results})
    buckets: dict[str, dict] = {}
    for cat in categories:
        subset = [r for r in results if r.category == cat]
        buckets[cat] = {
            "count": len(subset),
            "recall@5": round(_mean(r.recall5 for r in subset), 4),
            "mrr@10": round(_mean(r.mrr10 for r in subset), 4),
            "ndcg@10": round(_mean(r.ndcg10 for r in subset), 4),
        }

    evidence_queries = [r for r in results if not r.expected_no_evidence]
    no_evidence_queries = [r for r in results if r.expected_no_evidence]

    no_evidence_returned_any = [r.query_id for r in no_evidence_queries if r.returned_any and r.top_grade >= 2]

    return {
        "total_queries": len(results),
        "by_category": buckets,
        "overall": {
            "recall@5": round(_mean(r.recall5 for r in evidence_queries), 4),
            "mrr@10": round(_mean(r.mrr10 for r in evidence_queries), 4),
            "ndcg@10": round(_mean(r.ndcg10 for r in evidence_queries), 4),
        },
        "no_evidence": {
            "count": len(no_evidence_queries),
            "returned_any_result": len(no_evidence_queries),
            "returned_top_grade_ge2": len(no_evidence_returned_any),
            "queries_returned_high_grade": no_evidence_returned_any,
        },
        "retrieval_stage_latency_s": None if latency is None else round(latency, 6),
    }


def write_report(summary: dict, latency_s: float, out_path: Path) -> None:
    summary["retrieval_stage_latency_s"] = round(latency_s, 6)
    lines = [
        "# Shiori Retrieval-Quality Baseline",
        "",
        f"- Total queries: {summary['total_queries']}",
        f"- Retrieval-stage latency (ranking only): {latency_s:.6f}s",
        "",
        "## Overall (evidence-bearing queries)",
        "",
        "| metric | value |",
        "|---|---|",
        f"| Recall@5 | {summary['overall']['recall@5']} |",
        f"| MRR@10 | {summary['overall']['mrr@10']} |",
        f"| nDCG@10 | {summary['overall']['ndcg@10']} |",
        "",
        "## By category",
        "",
        "| category | count | recall@5 | mrr@10 | ndcg@10 |",
        "|---|---|---|---|---|",
    ]
    for cat, stats in summary["by_category"].items():
        lines.append(
            f"| {cat} | {stats['count']} | {stats['recall@5']} | {stats['mrr@10']} | {stats['ndcg@10']} |"
        )
    lines += [
        "",
        "## No-evidence behavior",
        "",
        f"- No-evidence queries: {summary['no_evidence']['count']}",
        f"- Queries returning any result: {summary['no_evidence']['returned_any_result']}",
        f"- Queries returning a top grade >= 2 result: {summary['no_evidence']['returned_top_grade_ge2']}",
    ]
    if summary["no_evidence"]["queries_returned_high_grade"]:
        lines.append(f"- Query ids with high-grade results: {', '.join(summary['no_evidence']['queries_returned_high_grade'])}")
    lines.append("")
    out_path.write_text("\n".join(lines), encoding="utf-8")
