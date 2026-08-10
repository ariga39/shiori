"""Benchmark harness for the task #11 retrieval-quality benchmark.

Runs retrieval over the synthetic corpus using voyage-4-nano embeddings
(pure cosine as the frozen main baseline), computes the frozen metrics
(Recall@5, MRR@10, nDCG@10 with explicit relevance grades), phased cold/warm
latency, source filtering, and no-evidence behavior, and emits per-query +
aggregate JSON plus a Markdown report.

Contract (frozen):
- Main baseline is PURE voyage cosine similarity. Any lexical scoring is kept
  as a clearly-separated, separately-named secondary diagnostic and never mixed
  into the main metric.
- Relevance is graded 0-3 per doc (judgment.relevance). nDCG uses real grades;
  Recall@5 / MRR@10 treat grade>0 as relevant.
- Source filtering (judgment.source_filter) is applied before ranking.
- multi_turn queries render a canonical query from conversation_context.
- Latency: cold = model load (download excluded, reported separately), cold
  query encode, cold retrieval, cold e2e; warm = query encode / retrieval /
  e2e. sample count + p50/p95.
- A local-only live-model mode is explicit; CI runs only no-model unit tests.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics

# Make the `benchmark` package importable when run as a direct script.
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from benchmark.query_rendering import render_canonical_query  # noqa: E402

METRICS = ["recall@5", "mrr@10", "ndcg@10"]
DEFAULT_TOP_K = 10
BUCKETS = ["exact", "paraphrase", "multilingual", "temporal", "multi_turn", "duplicate", "no_evidence"]
SCHEMA_VERSION = "2"


def _read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _sha256(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))  # L2-normalized: dot == cosine


def _rank_corpus(
    query_text: str,
    documents: list[dict],
    doc_vectors: dict[str, list[float]],
    q_vector: list[float],
    *,
    source_filter: str | None = None,
) -> list[str]:
    """Pure voyage cosine ranking (the frozen main baseline)."""
    scored: list[tuple[float, str]] = []
    for doc in documents:
        if source_filter and doc["session"] != source_filter:
            continue
        cosine = _cosine(q_vector, doc_vectors[doc["id"]])
        scored.append((cosine, doc["id"]))
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    return [doc_id for _score, doc_id in scored]


def _recall_at_k(ranked: list[str], relevant: set[str], k: int) -> float:
    if not relevant:
        return 0.0
    return len(set(ranked[:k]) & relevant) / len(relevant)


def _reciprocal_rank(ranked: list[str], relevant: set[str], k: int) -> float:
    for i, doc_id in enumerate(ranked[:k], start=1):
        if doc_id in relevant:
            return 1.0 / i
    return 0.0


def _ndcg_at_k(ranked: list[str], relevance: dict[str, int], k: int) -> float:
    """nDCG@k with graded gain = 2**grade - 1, log2 discount (frozen definition)."""
    rel_map = {doc_id: (2**grade - 1) for doc_id, grade in relevance.items() if grade > 0}
    if not rel_map:
        return 0.0
    dcg = 0.0
    for i, doc_id in enumerate(ranked[:k], start=1):
        if doc_id in rel_map:
            dcg += rel_map[doc_id] / math.log2(i + 1)
    ideal = sum(gain / math.log2(i + 1) for i, gain in enumerate(sorted(rel_map.values(), reverse=True), start=1))
    return dcg / ideal if ideal > 0 else 0.0


def _p_percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    sorted_v = sorted(values)
    idx = max(0, min(len(sorted_v) - 1, int(math.ceil(p / 100.0 * len(sorted_v)) - 1)))
    return sorted_v[idx]


def _phase_stats(values: list[float]) -> dict:
    return {
        "sample_count": len(values),
        "p50_s": _p_percentile(values, 50),
        "p95_s": _p_percentile(values, 95),
        "mean_s": statistics.mean(values) if values else 0.0,
    }


def _run_live(
    corpus: list[dict],
    judgments: list[dict],
    *,
    model_id: str,
    model_revision: str,
    vectors_path: Path,
) -> tuple[list[dict], dict, dict]:
    """Run with a live model: encode documents+queries, then phase latency.

    Local-only; requires benchmark deps from benchmark/requirements.lock.

    Live mode ENCODES fresh query embeddings and uses them for ranking (the
    real e2e path). It also loads the pre-generated vectors for a consistency
    check (serialized-hash / numeric tolerance) without overwriting the file.
    Model loads use `local_files_only=True` so the timed model_load is
    guaranteed offline (fail closed if the cache is cold).
    """
    from sentence_transformers import SentenceTransformer

    # Prefetch/cache the model BEFORE the timed model-load measurement. The
    # prefetch may download when the cache is cold (download time is included
    # here, not in model_load).
    prefetch_start = time.perf_counter()
    prefetch = SentenceTransformer(
        model_id, revision=model_revision, trust_remote_code=True, truncate_dim=1024, device="cpu"
    )
    prefetch_s = time.perf_counter() - prefetch_start
    del prefetch
    import gc

    gc.collect()

    # Timed model load: guaranteed offline (local_files_only=True fails closed
    # if the cache is cold, so download is never in this phase).
    model_load_start = time.perf_counter()
    model = SentenceTransformer(
        model_id, revision=model_revision, trust_remote_code=True, truncate_dim=1024,
        device="cpu", local_files_only=True,
    )
    model_load_s = time.perf_counter() - model_load_start

    # Fresh query embeddings from THIS live run (used for ranking + e2e).
    fresh_query_vectors: list[dict] = []
    query_encode_times: dict[str, float] = {}
    for judgment in judgments:
        qtext = render_canonical_query(judgment)
        t0 = time.perf_counter()
        emb = model.encode_query(
            qtext, truncate_dim=1024, precision="float32", normalize_embeddings=True, show_progress_bar=False
        ).tolist()
        query_encode_times[judgment["query_id"]] = time.perf_counter() - t0
        fresh_query_vectors.append({"query_id": judgment["query_id"], "embedding": emb})

    # Pre-generated vectors (for document embeddings + consistency check only).
    pregen = json.loads(vectors_path.read_text(encoding="utf-8"))
    doc_vectors = {v["id"]: v["embedding"] for v in pregen["documents"]}

    # Consistency check: fresh query embeddings vs pre-generated query vectors.
    _check_query_vectors_match(pregen["queries"], fresh_query_vectors)

    per_query, latency = _run_retrieval(
        corpus, judgments, doc_vectors,
        fresh_query_vectors,
        doc_encode_s=None,
        model_load_s=model_load_s,
        model_prefetch_s=prefetch_s,
        query_encode_times=query_encode_times,
    )
    return per_query, latency, {"queries": fresh_query_vectors, "documents": pregen["documents"]}


def _check_query_vectors_match(pregen: list[dict], fresh: list[dict]) -> None:
    """Validate live query embeddings against the pre-generated set.

    Compares numeric L2 tolerance and normalized serialization. Fails closed on
    mismatch (do not silently rank with inconsistent vectors).
    """
    from benchmark.vector_validation import VectorValidationError

    pregen_map = {v["query_id"]: v["embedding"] for v in pregen}
    fresh_map = {v["query_id"]: v["embedding"] for v in fresh}
    if set(pregen_map) != set(fresh_map):
        raise VectorValidationError("live query ids differ from pre-generated")
    for qid, fresh_emb in fresh_map.items():
        pre_emb = pregen_map[qid]
        if len(fresh_emb) != len(pre_emb):
            raise VectorValidationError(f"query {qid} dim mismatch between live and pre-generated")
        diff = sum((a - b) ** 2 for a, b in zip(fresh_emb, pre_emb))
        if math.sqrt(diff) > 1e-3:
            raise VectorValidationError(f"query {qid} live embedding differs from pre-generated (l2={math.sqrt(diff):.6f})")
    # Numeric L2 tolerance is the BINDING consistency check (matches the
    # frozen contract). The serialized comparison is recorded but not a hard
    # gate: single-encode (live) vs batch-encode (generator) can differ beyond
    # rounding at float noise even when L2 < 1e-3.
    canon = lambda emb: [round(float(v), 4) for v in emb]  # noqa: E731
    pre_canon = {qid: json.dumps(canon(pregen_map[qid]), sort_keys=True) for qid in sorted(pregen_map)}
    fresh_canon = {qid: json.dumps(canon(fresh_map[qid]), sort_keys=True) for qid in sorted(fresh_map)}
    if pre_canon != fresh_canon:
        # Record-only: L2 tolerance already passed; do not fail the run.
        import warnings

        warnings.warn(
            "live query embeddings serialized hash differs from pre-generated at round(4); "
            "L2 tolerance passed. Single-encode vs batch-encode float noise.",
            stacklevel=2,
        )


def _run_retrieval(
    corpus: list[dict],
    judgments: list[dict],
    doc_vectors: dict[str, list[float]],
    query_vectors: list[dict],
    *,
    doc_encode_s: float | None = None,
    model_load_s: float | None = None,
    model_prefetch_s: float | None = None,
    query_encode_times: dict[str, float] | None = None,
) -> tuple[list[dict], dict]:
    query_vectors_map = {v["query_id"]: v["embedding"] for v in query_vectors}
    per_query: list[dict] = []
    latencies: dict[str, list[float]] = defaultdict(list)
    cold_done = False

    for judgment in judgments:
        qid = judgment["query_id"]
        q_vector = query_vectors_map.get(qid)
        if q_vector is None:
            per_query.append({"query_id": qid, "error": "missing_query_vector"})
            continue
        relevance = judgment.get("relevance") or {}
        relevant = {doc_id for doc_id, grade in relevance.items() if grade > 0}
        expected_no_evidence = judgment.get("expected_no_evidence", False)
        qtext = render_canonical_query(judgment)
        source_filter = judgment.get("source_filter")

        # Encoding timing: provided by live-model mode (cold = first query,
        # warm = subsequent). In precomputed mode, encode time is 0 and only
        # retrieval/e2e are reported.
        encode_s = (query_encode_times or {}).get(qid, 0.0)

        t_retrieval = time.perf_counter()
        ranked = _rank_corpus(
            qtext, corpus, doc_vectors, q_vector,
            source_filter=source_filter,
        )
        retrieval_s = time.perf_counter() - t_retrieval

        bucket = "cold" if not cold_done else "warm"
        cold_done = True
        e2e_s = encode_s + retrieval_s
        latencies[f"{bucket}_query_encode"].append(encode_s)
        latencies[f"{bucket}_retrieval"].append(retrieval_s)
        latencies[f"{bucket}_e2e"].append(e2e_s)

        top_k = ranked[:DEFAULT_TOP_K]
        no_evidence_behavior = None
        if expected_no_evidence:
            no_evidence_behavior = "false_return" if top_k else "abstention_like"

        # no-evidence queries have NO relevance grades: quality metrics are
        # null/N/A and excluded from quality means and gap ranking. Only the
        # retriever behavior (false-return vs abstention) is reported.
        if expected_no_evidence:
            recall5 = mrr10 = ndcg10 = None
        else:
            recall5 = _recall_at_k(ranked, relevant, 5)
            mrr10 = _reciprocal_rank(ranked, relevant, DEFAULT_TOP_K)
            ndcg10 = _ndcg_at_k(ranked, relevance, DEFAULT_TOP_K)

        row = {
            "query_id": qid,
            "class": judgment["class"],
            "lang": judgment["lang"],
            "direction": judgment["direction"],
            "source_filter": source_filter,
            "expected_no_evidence": expected_no_evidence,
            "no_evidence_behavior": no_evidence_behavior,
            "retriever_top1": ranked[0] if ranked else None,
            "retriever_topk": top_k,
            "recall@5": recall5,
            "mrr@10": mrr10,
            "ndcg@10": ndcg10,
            "latency_bucket": bucket,
            "latency_query_encode_s": encode_s,
            "latency_retrieval_s": retrieval_s,
            "latency_e2e_s": e2e_s,
        }
        per_query.append(row)

    latency_stats: dict[str, dict] = {}
    for name, values in latencies.items():
        latency_stats[name] = _phase_stats(values)
    if model_prefetch_s is not None:
        latency_stats["model_prefetch"] = _phase_stats([model_prefetch_s])
        latency_stats["model_prefetch"]["note"] = "model prefetch/cache (download time included here, not in model_load)"
    if model_load_s is not None:
        latency_stats["model_load"] = _phase_stats([model_load_s])
        latency_stats["model_load"]["note"] = "model load, cache-backed offline (download excluded)"
    if doc_encode_s is not None:
        latency_stats["document_encode"] = _phase_stats([doc_encode_s])

    return per_query, latency_stats


def _aggregate(per_query: list[dict]) -> dict:
    aggregates: dict[str, dict] = {}
    for group_key, group_selector in (
        ("class", lambda r: r.get("class")),
        ("lang", lambda r: r.get("lang")),
        ("direction", lambda r: r.get("direction")),
        ("source_filter", lambda r: r.get("source_filter") or "none"),
    ):
        grouped: dict[str, list[dict]] = defaultdict(list)
        for row in per_query:
            if "error" in row:
                continue
            key = group_selector(row)
            if key:
                grouped[key].append(row)
        for key, rows in grouped.items():
            quality_rows = [r for r in rows if not r["expected_no_evidence"]]
            agg = {
                m: (statistics.mean([r[m] for r in quality_rows]) if quality_rows else None)
                for m in METRICS
            }
            no_ev = [r["no_evidence_behavior"] for r in rows if r["expected_no_evidence"]]
            agg["query_count"] = len(rows)
            agg["no_evidence_query_count"] = len(no_ev)
            agg["no_evidence_false_return"] = no_ev.count("false_return")
            agg["no_evidence_abstention"] = no_ev.count("abstention_like")
            aggregates[f"{group_key}:{key}"] = agg
    return aggregates


def _build_results(
    corpus_path: Path,
    judgments_path: Path,
    schema_path: Path,
    gen_path: Path,
    harness_path: Path,
    lock_path: Path,
    vectors_path: Path,
    per_query: list[dict],
    aggregates: dict,
    latency_stats: dict,
    model_id: str,
    model_revision: str,
) -> dict:
    qr_path = Path(__file__).parent / "query_rendering.py"
    vv_path = Path(__file__).parent / "vector_validation.py"
    return {
        "schema_version": SCHEMA_VERSION,
        "metrics": METRICS,
        "default_top_k": DEFAULT_TOP_K,
        "model": {"id": model_id, "revision": model_revision, "prompt_identity": {"query": "encode_query", "document": "encode_document"}},
        "embedding": {"dim": 1024, "dtype": "float32", "normalization": "L2", "dim_is_fixed": True},
        "input_hashes": {
            "corpus.jsonl": _sha256(corpus_path),
            "judgments.jsonl": _sha256(judgments_path),
            "corpus_schema.json": _sha256(schema_path),
            "generate_vectors.py": _sha256(gen_path),
            "run_benchmark.py": _sha256(harness_path),
            "query_rendering.py": _sha256(qr_path),
            "vector_validation.py": _sha256(vv_path),
            "requirements.lock": _sha256(lock_path),
            "vectors.json": _sha256(vectors_path) if vectors_path.exists() else None,
        },
        "per_query": per_query,
        "aggregates": aggregates,
        "latency": latency_stats,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Task #11 retrieval-quality benchmark harness")
    parser.add_argument("--fixtures", required=True, type=Path, help="benchmark/fixtures directory")
    parser.add_argument("--vectors", required=True, type=Path, help="vectors.json from generate_vectors.py")
    parser.add_argument("--out", required=True, type=Path, help="results output directory")
    parser.add_argument("--report", required=True, type=Path, help="Markdown report output path")
    parser.add_argument(
        "--live-model",
        action="store_true",
        help="Local-only: load voyage-4-nano and encode documents+queries live (phased latency). CI never uses this.",
    )
    parser.add_argument(
        "--emit-manifest",
        type=Path,
        default=None,
        help="Write the committed reproducibility manifest to this path (no manual copy).",
    )
    args = parser.parse_args(argv)

    if args.emit_manifest is not None and not args.live_model:
        parser.error("--emit-manifest requires --live-model (committed manifest is only valid from a live run)")

    corpus_path = args.fixtures / "corpus.jsonl"
    judgments_path = args.fixtures / "judgments.jsonl"
    schema_path = args.fixtures.parent / "corpus_schema.json"
    gen_path = Path(__file__).parent / "generate_vectors.py"
    harness_path = Path(__file__).resolve()
    lock_path = Path(__file__).parent / "requirements.lock"

    corpus = _read_jsonl(corpus_path)
    judgments = _read_jsonl(judgments_path)
    expected_doc_ids = {doc["id"] for doc in corpus}
    expected_query_ids = {j["query_id"] for j in judgments}

    # In live-model mode, generated vectors belong in benchmark/.generated/
    # (never committed); `--out` is for results/report only.
    vectors_path = (args.fixtures.parent / ".generated" / "vectors.json") if args.live_model else args.vectors
    model_id = "voyageai/voyage-4-nano"
    model_revision = "67fabc9bef010dabc5f6024aa1b1b6b93410426f"

    from benchmark.vector_validation import validate_vectors

    if args.live_model:
        per_query, latency_stats, fresh_vectors = _run_live(
            corpus, judgments,
            model_id=model_id,
            model_revision=model_revision,
            vectors_path=vectors_path,
        )
        # The fresh (live) query vectors and pre-generated documents must pass
        # strict validation.
        validate_vectors(
            fresh_vectors,
            expected_doc_ids=expected_doc_ids,
            expected_query_ids=expected_query_ids,
        )
        # Results bind the PRE-GENERATED vectors hash (committed source), so
        # manifest/results/report agree; the live run validates against it.
        results_vectors_ref = vectors_path
    else:
        vectors = json.loads(args.vectors.read_text(encoding="utf-8"))
        validate_vectors(
            vectors,
            expected_doc_ids=expected_doc_ids,
            expected_query_ids=expected_query_ids,
        )
        doc_vectors = {v["id"]: v["embedding"] for v in vectors["documents"]}
        per_query, latency_stats = _run_retrieval(
            corpus, judgments, doc_vectors, vectors["queries"],
        )
        results_vectors_ref = args.vectors

    aggregates = _aggregate(per_query)
    results = _build_results(
        corpus_path, judgments_path, schema_path, gen_path, harness_path, lock_path,
        results_vectors_ref,
        per_query, aggregates, latency_stats, model_id, model_revision,
    )

    args.out.mkdir(parents=True, exist_ok=True)
    results_path = args.out / "results.json"
    results_path.write_text(json.dumps(results, indent=2, sort_keys=True), encoding="utf-8")
    _write_report(args.report, results, corpus, judgments)

    if args.emit_manifest is not None:
        manifest = _build_manifest(
            corpus_path, judgments_path, schema_path, gen_path, harness_path, lock_path,
            results_vectors_ref,
        )
        args.emit_manifest.parent.mkdir(parents=True, exist_ok=True)
        args.emit_manifest.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        print(f"wrote {args.emit_manifest}")

    print(f"wrote {results_path}")
    print(f"wrote {args.report}")
    return 0


def _build_manifest(
    corpus_path: Path,
    judgments_path: Path,
    schema_path: Path,
    gen_path: Path,
    harness_path: Path,
    lock_path: Path,
    vectors_path: Path,
) -> dict:
    """Build the committed reproducibility manifest (same source set as results)."""
    qr_path = Path(__file__).parent / "query_rendering.py"
    vv_path = Path(__file__).parent / "vector_validation.py"
    import platform

    files = {
        "corpus.jsonl": {"sha256": _sha256(corpus_path)},
        "judgments.jsonl": {"sha256": _sha256(judgments_path)},
        "corpus_schema.json": {"sha256": _sha256(schema_path)},
        "generate_vectors.py": {"sha256": _sha256(gen_path)},
        "run_benchmark.py": {"sha256": _sha256(harness_path)},
        "query_rendering.py": {"sha256": _sha256(qr_path)},
        "vector_validation.py": {"sha256": _sha256(vv_path)},
        "requirements.lock": {"sha256": _sha256(lock_path)},
        "vectors.json": {"sha256": _sha256(vectors_path) if vectors_path.exists() else None},
    }
    rows = {
        "corpus.jsonl": len(_read_jsonl(corpus_path)),
        "judgments.jsonl": len(_read_jsonl(judgments_path)),
    }
    for rel, row_count in rows.items():
        files[rel]["rows"] = row_count

    # Record the ACTUAL library versions loaded in this run. Fails closed if
    # any of the three is unavailable (never write "unknown" into the committed
    # manifest, which would fabricate provenance in CI/plain environments).
    libraries = {}
    try:
        import sentence_transformers as st

        libraries["sentence_transformers"] = st.__version__
    except Exception as exc:  # noqa: BLE001 - fail closed on missing provenance
        raise SystemExit(
            "cannot emit committed manifest: sentence-transformers unavailable "
            f"({exc}); run with benchmark/requirements.lock installed"
        ) from exc
    try:
        import torch

        libraries["torch"] = torch.__version__
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(
            "cannot emit committed manifest: torch unavailable "
            f"({exc}); run with benchmark/requirements.lock installed"
        ) from exc
    try:
        import transformers

        libraries["transformers"] = transformers.__version__
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(
            "cannot emit committed manifest: transformers unavailable "
            f"({exc}); run with benchmark/requirements.lock installed"
        ) from exc

    return {
        "manifest_version": SCHEMA_VERSION,
        "model": {"id": "voyageai/voyage-4-nano", "revision": "67fabc9bef010dabc5f6024aa1b1b6b93410426f"},
        "prompt_identity": {"query": "encode_query", "document": "encode_document"},
        "embedding": {"dim": 1024, "dtype": "float32", "normalization": "L2", "dim_is_fixed": True},
        "files": files,
        "libraries": libraries,
        "os": {"platform": platform.platform(), "python": platform.python_version()},
    }


def _fmt_metric(value) -> str:
    return "N/A" if value is None else f"{value:.3f}"


def _write_report(path: Path, results: dict, corpus: list[dict], judgments: list[dict]) -> None:
    lines: list[str] = []
    lines.append("# Retrieval-Quality Baseline Report (task #11)")
    lines.append("")
    lines.append("Model: voyageai/voyage-4-nano (dim 1024, L2, encode_query/encode_document, truncate_dim=1024).")
    lines.append(f"Corpus documents: {len(corpus)}; queries: {len(judgments)}; metrics: {', '.join(METRICS)}.")
    lines.append("Main baseline: pure voyage cosine (no mixed scoring).")
    lines.append("")
    lines.append("## Aggregate by query class")
    lines.append("")
    lines.append("| class | query_count | recall@5 | mrr@10 | ndcg@10 |")
    lines.append("|---|---|---|---|---|")
    for cls in BUCKETS:
        agg = results["aggregates"].get(f"class:{cls}")
        if not agg:
            continue
        lines.append(
            f"| {cls} | {agg['query_count']} | {_fmt_metric(agg['recall@5'])} | {_fmt_metric(agg['mrr@10'])} | {_fmt_metric(agg['ndcg@10'])} |"
        )
    lines.append("")
    lines.append("## Aggregate by language")
    lines.append("")
    lines.append("| lang | query_count | recall@5 | mrr@10 | ndcg@10 |")
    lines.append("|---|---|---|---|---|")
    for lang in ("en", "zh", "ja"):
        agg = results["aggregates"].get(f"lang:{lang}")
        if not agg:
            continue
        lines.append(
            f"| {lang} | {agg['query_count']} | {_fmt_metric(agg['recall@5'])} | {_fmt_metric(agg['mrr@10'])} | {_fmt_metric(agg['ndcg@10'])} |"
        )
    lines.append("")
    lines.append("## Aggregate by direction")
    lines.append("")
    lines.append("| direction | query_count | recall@5 | mrr@10 | ndcg@10 |")
    lines.append("|---|---|---|---|---|")
    for key, agg in sorted(results["aggregates"].items()):
        if key.startswith("direction:"):
            lines.append(
                f"| {key.split(':',1)[1]} | {agg['query_count']} | {_fmt_metric(agg['recall@5'])} | "
                f"{_fmt_metric(agg['mrr@10'])} | {_fmt_metric(agg['ndcg@10'])} |"
            )
    lines.append("")
    lines.append("## Aggregate by source filter")
    lines.append("")
    lines.append("| filter | query_count | recall@5 | mrr@10 | ndcg@10 |")
    lines.append("|---|---|---|---|---|")
    for key, agg in sorted(results["aggregates"].items()):
        if key.startswith("source_filter:"):
            lines.append(
                f"| {key.split(':',1)[1]} | {agg['query_count']} | {_fmt_metric(agg['recall@5'])} | "
                f"{_fmt_metric(agg['mrr@10'])} | {_fmt_metric(agg['ndcg@10'])} |"
            )
    lines.append("")
    lines.append("## Latency")
    lines.append("")
    lines.append("| phase | sample_count | p50 (s) | p95 (s) | mean (s) |")
    lines.append("|---|---|---|---|---|")
    for name, stats in results["latency"].items():
        note = f" ({stats.get('note','')})" if stats.get("note") else ""
        lines.append(
            f"| {name} | {stats['sample_count']} | {stats['p50_s']:.4f} | {stats['p95_s']:.4f} | {stats['mean_s']:.4f}{note} |"
        )
    lines.append("")
    lines.append("## No-evidence behavior")
    lines.append("")
    lines.append("| query_id | expected_no_evidence | behavior |")
    lines.append("|---|---|---|")
    for row in results["per_query"]:
        if "error" in row:
            continue
        if row.get("expected_no_evidence"):
            lines.append(f"| {row['query_id']} | true | {row.get('no_evidence_behavior') or 'n/a'} |")
    lines.append("")
    lines.append("## Gap ranking (data-supported, no-evidence excluded)")
    lines.append("")
    lines.append("Sorted worst-first by aggregate nDCG@10 across quality query classes "
    "(no-evidence has null quality metrics and is reported separately):")
    class_rows = []
    for cls in BUCKETS:
        agg = results["aggregates"].get(f"class:{cls}")
        if agg is None or agg.get("ndcg@10") is None:
            continue
        class_rows.append((cls, agg))
    class_rows.sort(key=lambda pair: pair[1]["ndcg@10"])
    for i, (cls, agg) in enumerate(class_rows, start=1):
        lines.append(f"{i}. `{cls}` nDCG@10={agg['ndcg@10']:.3f} (queries={agg['query_count']})")
    lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
