"""Phase 4E1 dev-only filter evaluation (task #25).

Runs the real ``query.search`` with the typed ``SearchFilters`` against an
isolated PostgreSQL + pgvector database seeded with the task #11 corpus, and
proves that source/session/time filters produce zero leakage on the 72-dev
filter cases from the frozen evidence ledger.

Measurement only: no ranking/model/threshold/temporal/dedup changes.  Only the
72 tune IDs are ever touched; holdout IDs are rejected.  Outputs are
deterministic and sanitized (no content, raw session/source values, paths, DSNs,
or keys).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
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
OUT_RESULTS = REPO_ROOT / "benchmark" / "product_eval" / "filter_eval_results.json"
OUT_REPORT = REPO_ROOT / "benchmark" / "product_eval" / "filter_eval_report.md"
SESSION_PREFIX = "phase4d-filter"


class FilterEvalError(RuntimeError):
    pass


def _load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def _dev_ids(manifest: dict) -> set[str]:
    tune = {q["query_id"] for q in manifest["query_splits"] if q["split"] == "tune"}
    if len(tune) != 72:
        raise FilterEvalError(f"expected 72 dev ids, got {len(tune)}")
    return tune


def _golden_query_texts() -> dict[str, str]:
    texts: dict[str, str] = {}
    with GOLDEN.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                row = json.loads(line)
                qid = row.get("query_id")
                texts[qid] = row.get("canonical_query") or row.get("query_text") or ""
    return texts


def _corpus_meta() -> dict[str, dict[str, Any]]:
    meta: dict[str, dict[str, Any]] = {}
    with CORPUS.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                doc = json.loads(line)
                meta[doc["id"]] = {
                    "session": doc["session"],
                    "timestamp": doc["timestamp"],
                    "source_kind": doc["source_kind"],
                }
    return meta


def _filters_for(ledger_row: dict, corpus: dict[str, dict[str, Any]], *, session_prefix: str) -> SearchFilters:
    """Build SearchFilters from the ledger's authored filter fields."""
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
        # Inclusive instant: [bound, bound + 1s) to catch the exact timestamp.
        bound = time_filter["iso_bound"]
        time_from = bound
        from datetime import datetime, timedelta

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
    *,
    limit: int,
) -> dict[str, Any]:
    """Run real search with filters; verify zero leakage."""
    rows = query.search(query_text, limit=limit, filters=filters)
    leakage = 0
    for row in rows:
        # Final result layout: (content, score, timestamp, session_id, source_type, ...)
        if not query._row_matches_filters(row, filters):
            leakage += 1
    return {"leakage": leakage, "returned": len(rows)}


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
    golden_texts = _golden_query_texts()
    dev = _dev_ids(manifest)

    query.DATABASE_DSN = args.dsn
    query.apply_settings(
        query.load_config(
            environ={
                "SHIORI_DATABASE_DSN": args.dsn,
                # Deterministic offline provider so any query text embeds; the
                # corpus vectors are real voyage-4-nano (both 1024-dim L2), so
                # cosine similarity remains meaningful.  No network/key/model.
                "SHIORI_EMBEDDING_PROVIDER": "fake",
                "SHIORI_ALLOW_FAKE_EMBEDDINGS": "true",
                "SHIORI_ENVIRONMENT": "test",
                # The dense channel filters rows by embedding_model == the
                # corpus identity, so the query must claim the same identity
                # even though the query vector is deterministically generated.
                "SHIORI_VOYAGE_MODEL": "voyageai/voyage-4-nano@67fabc9bef010dabc5f6024aa1b1b6b93410426f",
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
        filters = _filters_for(ledger_row, corpus, session_prefix=SESSION_PREFIX)
        case = _run_case(query_text, filters, limit=args.limit)
        kinds = [k for k in ("source_filter", "session_filter", "time_filter") if k in ledger_row]
        cases.append(
            {
                "query_id": qid,
                "filter_kinds": kinds,
                "leakage": case["leakage"],
                "returned": case["returned"],
                "ok": case["leakage"] == 0,
            }
        )
    total_leakage = sum(c["leakage"] for c in cases)
    results = {
        "schema": "shiori-filter-eval/v1",
        "base_sha": os.environ.get("SHIORI_FILTER_EVAL_BASE_SHA", ""),
        "dev_count": len(filter_ids),
        "holdout_ids_used": [],
        "cases": cases,
        "total_leakage": total_leakage,
        "ok": total_leakage == 0,
    }
    args.out_results.write_text(json.dumps(results, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "# Phase 4E1 dev-only filter evaluation",
        "",
        "- schema: `shiori-filter-eval/v1`",
        f"- dev filter cases: {len(filter_ids)} (72-dev only, holdout untouched)",
        f"- total leakage: {total_leakage}",
        f"- ok: {total_leakage == 0}",
        "",
        "| query_id | kinds | returned | leakage | ok |",
        "|---|---|---|---|---|",
    ]
    for c in cases:
        lines.append(
            f"| {c['query_id']} | {','.join(c['filter_kinds'])} | {c['returned']} | {c['leakage']} | {c['ok']} |"
        )
    args.out_report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"filter_eval wrote {args.out_results} and {args.out_report}; total_leakage={total_leakage}")
    return 0 if total_leakage == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
