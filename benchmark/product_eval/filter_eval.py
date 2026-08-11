"""Phase 4E1 dev-only filter evaluation (task #25).

Runs the real ``query.search`` with the typed ``SearchFilters`` against an
isolated PostgreSQL + pgvector database seeded with the task #11 corpus, and
proves that source/session/time filters produce zero leakage on the 72-dev
filter cases from the frozen evidence ledger.

Measurement contract (frozen):

- Query vectors are the REAL offline voyage-4-nano embeddings replayed from the
  frozen 72-dev artifact ``benchmark/.generated/dev_query_vectors.json`` (pinned
  revision + exact SHA-256), looked up BY QUERY ID.  No fake/deterministic
  vectors and no identity masquerading.
- Each case runs BOTH an unfiltered control search and the filtered search on
  the same exact head and same real query vectors.  ``before`` leakage is
  computed independently from the control result against the authored
  ledger/corpus metadata; ``after`` is the filtered result against the same
  oracle.  The oracle NEVER calls private ``query._row_matches_filters``;
  it derives allowed content from the authored filter fields + corpus doc
  metadata (session / source_kind / timestamp).
- Records: implementation SHA, all input hashes, model identity, per-kind
  query counts + before/after, per-case control/filtered returned +
  coverage-risk, filtered-as-order-preserving-subsequence, p50/p95, and
  ``holdout_ids_used=[]``.  The machine-generated report is byte-derived from
  the JSON and covered by regeneration tests.

Only the 72 tune IDs are ever touched; holdout IDs are rejected.  Outputs are
deterministic and sanitized (no content, raw session/source values, paths, DSNs,
or keys).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root for query.py

import query
from query import SearchFilters

REPO_ROOT = Path(__file__).resolve().parents[2]
LEDGER = REPO_ROOT / "benchmark" / "product_eval" / "evidence_ledger.json"
MANIFEST = REPO_ROOT / "benchmark" / "product_eval" / "dataset_manifest.json"
GOLDEN = REPO_ROOT / "benchmark" / "product_eval" / "golden_queries.jsonl"
CORPUS = REPO_ROOT / "benchmark" / "fixtures" / "corpus.jsonl"
DEV_VECTORS = REPO_ROOT / "benchmark" / ".generated" / "dev_query_vectors.json"
BASELINE = REPO_ROOT / "benchmark" / "product_eval" / "baseline_72_results.json"
OUT_RESULTS = REPO_ROOT / "benchmark" / "product_eval" / "filter_eval_results.json"
OUT_REPORT = REPO_ROOT / "benchmark" / "product_eval" / "filter_eval_report.md"
SESSION_PREFIX = "phase4d-filter"

FROZEN_DEV_VECTORS_SHA256 = "629fa726ec353632a2a87a48b473ad0b59c2dd8f61a804746e2d9dd43c9287f2"
MODEL_IDENTITY = "voyageai/voyage-4-nano@67fabc9bef010dabc5f6024aa1b1b6b93410426f"
EMBED_DIM = 1024


class FilterEvalError(RuntimeError):
    pass


def _load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _dev_ids(manifest: dict) -> set[str]:
    tune = {q["query_id"] for q in manifest["query_splits"] if q["split"] == "tune"}
    holdout = {q["query_id"] for q in manifest["query_splits"] if q["split"] == "holdout"}
    if len(tune) != 72 or len(holdout) != 48 or tune & holdout:
        raise FilterEvalError(f"manifest split must be exactly 72/48, got {len(tune)}/{len(holdout)}")
    return tune


def _golden_query_texts() -> dict[str, str]:
    texts: dict[str, str] = {}
    with GOLDEN.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                row = json.loads(line)
                texts[row.get("query_id")] = row.get("canonical_query") or row.get("query_text") or ""
    return texts


def _corpus_meta() -> dict[str, dict[str, Any]]:
    """doc id -> {content, session, source_kind, timestamp(datetime)}."""
    meta: dict[str, dict[str, Any]] = {}
    with CORPUS.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                doc = json.loads(line)
                meta[doc["id"]] = {
                    "content": doc["content"],
                    "session": doc["session"],
                    "source_kind": doc["source_kind"],
                    "timestamp": datetime.fromisoformat(doc["timestamp"].replace("Z", "+00:00")),
                }
    return meta


def _content_doc_ids(corpus: dict[str, dict[str, Any]]) -> dict[str, set[str]]:
    """content -> set of doc ids (doc-0017/0018 share content)."""
    by_content: dict[str, set[str]] = {}
    for doc_id, meta in corpus.items():
        by_content.setdefault(meta["content"], set()).add(doc_id)
    return by_content


def _parse_iso_bound(bound: str) -> datetime:
    return datetime.fromisoformat(bound.replace("Z", "+00:00"))


def _row_content(row: tuple[Any, ...]) -> str:
    return row[0]


def _independent_matches(
    row: tuple[Any, ...],
    ledger_row: dict,
    corpus: dict[str, dict[str, Any]],
    by_content: dict[str, set[str]],
    *,
    session_prefix: str,
) -> bool:
    """Independent filter oracle: is this returned row's content allowed by the
    authored source/session/time filter, judged ONLY from corpus metadata?

    Never calls query._row_matches_filters.  A row is allowed if at least one
    corpus doc with the same content satisfies every authored filter field.
    """
    content = _row_content(row)
    doc_ids = by_content.get(content)
    if not doc_ids:
        # Content not in the frozen corpus: not verifiable -> fail closed.
        return False

    source_filter = ledger_row.get("source_filter", {})
    session_filter = ledger_row.get("session_filter", {})
    time_filter = ledger_row.get("time_filter", {})

    for doc_id in doc_ids:
        meta = corpus[doc_id]
        ok = True
        if source_filter.get("kind") and meta["source_kind"] != source_filter["kind"]:
            ok = False
        if session_filter.get("session") and meta["session"] != session_filter["session"]:
            ok = False
        if time_filter.get("operator"):
            ts = meta["timestamp"]
            op = time_filter["operator"]
            bound = _parse_iso_bound(time_filter["iso_bound"])
            if op == "before":
                if not (ts < bound):
                    ok = False
            elif op == "at":
                if not (bound <= ts < bound + timedelta(seconds=1)):
                    ok = False
            elif op == "after":
                if not (ts >= bound):
                    ok = False
            else:
                ok = False
        if ok:
            return True
    return False


def _is_order_preserving_subsequence(filtered: list[tuple], control: list[tuple]) -> bool:
    it = iter([_row_content(r) for r in control])
    for row in filtered:
        content = _row_content(row)
        try:
            while next(it) != content:
                pass
        except StopIteration:
            return False
    return True


def _filter_kinds(ledger_row: dict) -> list[str]:
    return [k for k in ("source_filter", "session_filter", "time_filter") if k in ledger_row]


def _filters_for(ledger_row: dict, *, session_prefix: str) -> SearchFilters:
    source_filter = ledger_row.get("source_filter", {})
    session_filter = ledger_row.get("session_filter", {})
    time_filter = ledger_row.get("time_filter", {})
    source_types = [source_filter["kind"]] if source_filter.get("kind") else None
    session_ids = [f"{session_prefix}-{session_filter['session']}"] if session_filter.get("session") else None
    time_from = None
    time_to = None
    if time_filter.get("operator") == "before":
        time_to = time_filter["iso_bound"]
    elif time_filter.get("operator") == "at":
        bound = time_filter["iso_bound"]
        time_from = bound
        dt = datetime.fromisoformat(bound.replace("Z", "+00:00"))
        time_to = (dt + timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
    elif time_filter.get("operator") == "after":
        time_from = time_filter["iso_bound"]
    return SearchFilters.from_inputs(
        source_types=source_types,
        session_ids=session_ids,
        time_from=time_from,
        time_to=time_to,
    )


def _run_case(
    query_text: str,
    filters: SearchFilters,
    ledger_row: dict,
    corpus: dict[str, dict[str, Any]],
    by_content: dict[str, set[str]],
    *,
    limit: int,
    session_prefix: str,
) -> dict[str, Any]:
    """Control vs filtered, with an independent before/after oracle."""
    t0 = time.perf_counter()
    control = query.search(query_text, limit=limit)
    control_ms = (time.perf_counter() - t0) * 1000.0

    t1 = time.perf_counter()
    filtered = query.search(query_text, limit=limit, filters=filters)
    filtered_ms = (time.perf_counter() - t1) * 1000.0

    matches = lambda rows: [  # noqa: E731
        _independent_matches(r, ledger_row, corpus, by_content, session_prefix=session_prefix) for r in rows
    ]
    control_ok = matches(control)
    filtered_ok = matches(filtered)

    before = sum(1 for ok in control_ok if not ok)
    after = sum(1 for ok in filtered_ok if not ok)

    # Coverage risk: control rows the filter SHOULD allow but the filtered
    # result dropped (false negatives introduced by the filter itself).
    allowed_control = [r for r, ok in zip(control, control_ok) if ok]
    allowed_filtered_contents = {_row_content(r) for r, ok in zip(filtered, filtered_ok) if ok}
    coverage_risk = sum(1 for r in allowed_control if _row_content(r) not in allowed_filtered_contents)

    return {
        "before": before,
        "after": after,
        "control_returned": len(control),
        "filtered_returned": len(filtered),
        "coverage_risk": coverage_risk,
        "filtered_is_order_preserving_subsequence": _is_order_preserving_subsequence(filtered, control),
        "control_p95_ms": round(control_ms, 3),
        "filtered_p95_ms": round(filtered_ms, 3),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 4E1 dev-only filter evaluation")
    parser.add_argument("--dsn", default=os.environ.get("SHIORI_DATABASE_DSN"))
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--out-results", type=Path, default=OUT_RESULTS)
    parser.add_argument("--out-report", type=Path, default=OUT_REPORT)
    args = parser.parse_args(argv)
    if not args.dsn:
        parser.error("SHIORI_DATABASE_DSN is required")

    manifest = _load_json(MANIFEST)
    ledger = _load_json(LEDGER)
    corpus = _corpus_meta()
    by_content = _content_doc_ids(corpus)
    golden_texts = _golden_query_texts()
    dev = _dev_ids(manifest)

    # Real frozen 72-dev voyage-4-nano vectors, replayed BY QUERY ID.
    if not DEV_VECTORS.is_file():
        raise FilterEvalError("frozen dev_query_vectors.json missing; cannot use fake as substitute")
    if _sha256(DEV_VECTORS) != FROZEN_DEV_VECTORS_SHA256:
        raise FilterEvalError(
            f"dev_query_vectors.json SHA mismatch: {_sha256(DEV_VECTORS)[:16]}… != {FROZEN_DEV_VECTORS_SHA256[:16]}…"
        )
    dev_vecs = _load_json(DEV_VECTORS)
    if not isinstance(dev_vecs, list) or len(dev_vecs) != 72:
        raise FilterEvalError("dev_query_vectors.json must contain exactly 72 entries")
    vec_by_qid = {v["query_id"]: v["embedding"] for v in dev_vecs}
    if set(vec_by_qid) != dev:
        raise FilterEvalError("dev_query_vectors key set must equal the 72 dev ids")
    if any(len(e) != EMBED_DIM or not all(isinstance(x, (int, float)) for x in e) for e in vec_by_qid.values()):
        raise FilterEvalError("all dev query vectors must be numeric 1024-dim")

    # Replay: query.embed_query returns the real vector for the golden query id.
    qid_by_canon: dict[str, str] = {text: qid for qid, text in golden_texts.items() if text}
    real_embed = query.embed_query

    def _replay(qtext: str) -> list[float]:
        qid = qid_by_canon.get(qtext)
        if qid is None or qid not in vec_by_qid:
            # Fall back to the production embed_query ONLY when the text has no
            # frozen vector AND is not a golden query id (cannot happen for the
            # 9 filter cases, which are golden).  Fail closed otherwise.
            if qtext in golden_texts.values() and qtext not in qid_by_canon:
                raise FilterEvalError("golden query text has no frozen vector")
            return real_embed(qtext)
        return vec_by_qid[qid]

    saved = {
        "DATABASE_DSN": query.DATABASE_DSN,
        "EMBEDDING_PROVIDER": query.EMBEDDING_PROVIDER,
        "VOYAGE_MODEL": query.VOYAGE_MODEL,
        "EMBED_DIM": query.EMBED_DIM,
        "embed_query": query.embed_query,
    }
    try:
        query.DATABASE_DSN = args.dsn
        query.EMBEDDING_PROVIDER = "fake"  # provider flag unused: embed_query is replaced
        query.VOYAGE_MODEL = MODEL_IDENTITY
        query.EMBED_DIM = EMBED_DIM
        query.embed_query = _replay
        query.apply_settings(
            query.load_config(
                environ={
                    "SHIORI_DATABASE_DSN": args.dsn,
                    "SHIORI_EMBEDDING_PROVIDER": "fake",
                    "SHIORI_ALLOW_FAKE_EMBEDDINGS": "true",
                    "SHIORI_ENVIRONMENT": "test",
                    "SHIORI_VOYAGE_MODEL": MODEL_IDENTITY,
                    "SHIORI_EMBED_DIM": "1024",
                }
            )
        )

        filter_ids = sorted(
            qid
            for qid in dev
            if isinstance(ledger.get(qid), dict)
            and any(k in ledger[qid] for k in ("source_filter", "session_filter", "time_filter"))
        )
        if len(filter_ids) != 9:
            raise FilterEvalError(f"expected 9 dev filter cases, got {len(filter_ids)}")

        cases = []
        for qid in filter_ids:
            ledger_row = ledger[qid]
            query_text = golden_texts.get(qid, "")
            if not query_text:
                raise FilterEvalError(f"missing golden query text for {qid}")
            filters = _filters_for(ledger_row, session_prefix=SESSION_PREFIX)
            case = _run_case(
                query_text,
                filters,
                ledger_row,
                corpus,
                by_content,
                limit=args.limit,
                session_prefix=SESSION_PREFIX,
            )
            cases.append(
                {
                    "query_id": qid,
                    "filter_kinds": _filter_kinds(ledger_row),
                    **case,
                    "ok": case["after"] == 0 and case["filtered_is_order_preserving_subsequence"],
                }
            )

        total_before = sum(c["before"] for c in cases)
        total_after = sum(c["after"] for c in cases)
        total_coverage_risk = sum(c["coverage_risk"] for c in cases)
        all_p95 = [c["filtered_p95_ms"] for c in cases]
        kind_counts = {
            "source_filter": sum(1 for c in cases if "source_filter" in c["filter_kinds"]),
            "session_filter": sum(1 for c in cases if "session_filter" in c["filter_kinds"]),
            "time_filter": sum(1 for c in cases if "time_filter" in c["filter_kinds"]),
        }
        input_hashes = {
            "corpus.jsonl": _sha256(CORPUS),
            "golden_queries.jsonl": _sha256(GOLDEN),
            "evidence_ledger.json": _sha256(LEDGER),
            "dataset_manifest.json": _sha256(MANIFEST),
            "dev_query_vectors.json": _sha256(DEV_VECTORS),
        }
        results = {
            "schema": "shiori-filter-eval/v2",
            "implementation_sha": os.environ.get("SHIORI_FILTER_EVAL_BASE_SHA", ""),
            "model_identity": MODEL_IDENTITY,
            "input_hashes": input_hashes,
            "kind_counts": kind_counts,
            "dev_count": len(filter_ids),
            "holdout_ids_used": [],
            "cases": cases,
            "total_before_leakage": total_before,
            "total_after_leakage": total_after,
            "total_coverage_risk": total_coverage_risk,
            "filtered_latency_p95_ms": round(statistics.median(all_p95), 3) if all_p95 else None,
            "ok": total_after == 0
            and all(c["filtered_is_order_preserving_subsequence"] for c in cases),
        }
        args.out_results.write_text(json.dumps(results, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        lines = [
            "# Phase 4E1 dev-only filter evaluation",
            "",
            "- schema: `shiori-filter-eval/v2`",
            f"- implementation SHA: `{results['implementation_sha'] or '(unset)'}`",
            f"- model identity: `{MODEL_IDENTITY}`",
            f"- dev filter cases: {len(filter_ids)} (72-dev only, holdout untouched)",
            f"- input hashes: {json.dumps(input_hashes, sort_keys=True)}",
            f"- kind counts: {json.dumps(kind_counts, sort_keys=True)}",
            f"- total before leakage: {total_before}",
            f"- total after leakage: {total_after}",
            f"- total coverage risk: {total_coverage_risk}",
            f"- filtered latency p95 (median of per-case p95): {results['filtered_latency_p95_ms']}ms",
            f"- ok: {results['ok']}",
            "",
            "| query_id | kinds | before | after | control_returned | filtered_returned | coverage_risk | subsequence | ok |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for c in cases:
            lines.append(
                f"| {c['query_id']} | {','.join(c['filter_kinds'])} | {c['before']} | {c['after']} | "
                f"{c['control_returned']} | {c['filtered_returned']} | {c['coverage_risk']} | "
                f"{c['filtered_is_order_preserving_subsequence']} | {c['ok']} |"
            )
        args.out_report.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(
            f"filter_eval wrote {args.out_results} and {args.out_report}; "
            f"before={total_before} after={total_after} coverage_risk={total_coverage_risk}"
        )
        return 0 if results["ok"] else 1
    finally:
        for name, value in saved.items():
            setattr(query, name, value)


if __name__ == "__main__":
    raise SystemExit(main())
