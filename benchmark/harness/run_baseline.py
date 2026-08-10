"""Run the retrieval-quality baseline end to end and write a report.

Usage:
  python benchmark/harness/run_baseline.py \
      --provider deterministic \
      --outdir benchmark/reports/deterministic

  python benchmark/harness/run_baseline.py \
      --provider voyage-4-nano --model-revision <fixed_rev> \
      --outdir benchmark/reports/voyage-4-nano

The harness embeds the corpus + queries with the chosen provider, ranks by
cosine similarity, and computes Recall@5 / MRR@10 / nDCG@10 bucketed by query
category plus no-evidence behavior.  No API key is used; no product ranking is
tuned; no task #10 harness is modified.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from benchmark.generator.generate_vectors import (  # noqa: E402
    DeterministicProvider,
    VoyageNanoProvider,
    load_jsonl,
)
from benchmark.harness.metrics import (  # noqa: E402
    evaluate,
    load_documents,
    load_queries,
    write_report,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run shiori retrieval-quality baseline")
    parser.add_argument("--provider", choices=["voyage-4-nano", "deterministic"], default="deterministic")
    parser.add_argument("--model-revision", help="Fixed HF revision for voyage-4-nano")
    parser.add_argument("--dimension", type=int, default=1024)
    parser.add_argument("--corpus-dir", type=Path, default=Path("benchmark/corpus/v1"))
    parser.add_argument("--documents", type=Path)
    parser.add_argument("--queries", type=Path)
    parser.add_argument("--outdir", type=Path, default=Path("benchmark/reports/deterministic"))
    args = parser.parse_args(argv)

    docs_path = args.documents or (args.corpus_dir / "documents.jsonl")
    queries_path = args.queries or (args.corpus_dir / "queries.jsonl")

    documents = load_documents(docs_path)
    queries = load_queries(queries_path)

    if args.provider == "voyage-4-nano":
        provider = VoyageNanoProvider(revision=args.model_revision, dimension=args.dimension)
    else:
        provider = DeterministicProvider()
        if args.dimension != 1024:
            provider.dimension = args.dimension

    doc_texts = [doc["content"] for doc in documents]

    # For query text, we must embed the actual query string, not the id.
    raw_queries = {item["id"]: item["query"] for item in load_jsonl(queries_path)}
    query_texts = [raw_queries[q.query_id] for q in queries]

    doc_vectors = np.asarray(provider.embed_documents(doc_texts), dtype=np.float64)
    query_vectors = np.asarray([provider.embed_query(t) for t in query_texts], dtype=np.float64)

    results, summary = evaluate(documents, queries, query_vectors, doc_vectors, k=5)

    args.outdir.mkdir(parents=True, exist_ok=True)
    report_path = args.outdir / "baseline.md"
    latency_s = summary.get("retrieval_stage_latency_s")
    write_report(summary, latency_s if latency_s is not None else 0.0, report_path)

    # Per-query detail for peer verification.
    detail = []
    for r in results:
        detail.append(
            {
                "query_id": r.query_id,
                "category": r.category,
                "expected_no_evidence": r.expected_no_evidence,
                "recall5": r.recall5,
                "mrr10": r.mrr10,
                "ndcg10": r.ndcg10,
                "top_ranked_docs": r.ranked_doc_ids[:5],
            }
        )
    (args.outdir / "query_detail.json").write_text(json.dumps(detail, indent=2) + "\n", encoding="utf-8")
    (args.outdir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    print(json.dumps(summary, indent=2))
    print(f"report -> {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
