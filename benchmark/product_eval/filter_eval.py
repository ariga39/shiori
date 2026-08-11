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
import re
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

# Frozen 72-dev unfiltered baseline + the exact-head runner artifact that was
# compared against it (see unfiltered_regression in the results).
BASELINE_RUNNER_SHA256 = "5192a02e75d93a0b775db9851bae298cc2d2333271c2d533228fac14d70c157c"
HEAD_RUNNER_SHA256 = "5895d6ab64406c4f997ec8845a87c75562b411272ea636956519d18c95349eeb"

# Latency measurement: N timed repetitions per case after a single warm-up call.
LATENCY_REPS = 10


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


def _independent_kind_verdicts(
    row: tuple[Any, ...],
    ledger_row: dict,
    corpus: dict[str, dict[str, Any]],
    by_content: dict[str, set[str]],
    *,
    session_prefix: str,
) -> dict[str, bool]:
    """Per-kind independent verdicts (source/session/time) from corpus metadata.

    Never calls query._row_matches_filters.  A kind passes if at least one
    corpus doc with the same content satisfies that authored field.  Kinds with
    no authored filter are reported True (no constraint).
    """
    content = _row_content(row)
    doc_ids = by_content.get(content)
    if not doc_ids:
        return {"source": False, "session": False, "time": False}

    source_filter = ledger_row.get("source_filter", {})
    session_filter = ledger_row.get("session_filter", {})
    time_filter = ledger_row.get("time_filter", {})
    has_source = bool(source_filter.get("kind"))
    has_session = bool(session_filter.get("session"))
    has_time = bool(time_filter.get("operator"))

    verdicts = {"source": not has_source, "session": not has_session, "time": not has_time}
    for doc_id in doc_ids:
        meta = corpus[doc_id]
        if has_source and meta["source_kind"] == source_filter["kind"]:
            verdicts["source"] = True
        if has_session and meta["session"] == session_filter["session"]:
            verdicts["session"] = True
        if has_time:
            ts = meta["timestamp"]
            op = time_filter["operator"]
            bound = _parse_iso_bound(time_filter["iso_bound"])
            ok_time = (
                ts < bound
                if op == "before"
                else (bound <= ts < bound + timedelta(seconds=1))
                if op == "at"
                else (ts >= bound)
                if op == "after"
                else False
            )
            if ok_time:
                verdicts["time"] = True
    return verdicts


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

    Never calls query._row_matches_filters.  A row is allowed if every authored
    kind is satisfied by at least one corpus doc with the same content.
    """
    verdicts = _independent_kind_verdicts(row, ledger_row, corpus, by_content, session_prefix=session_prefix)
    return all(verdicts.values())


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
    latency_reps: int,
) -> dict[str, Any]:
    """Control vs filtered, with an independent before/after oracle.

    Latency: one warm-up call, then ``latency_reps`` timed repetitions for both
    the control and filtered searches; p50/p95 are true percentiles of the
    aggregate repetition samples.
    """
    # Warm-up (populate caches / jit paths) then timed repetitions.
    query.search(query_text, limit=limit)
    query.search(query_text, limit=limit, filters=filters)

    control_ms: list[float] = []
    filtered_ms: list[float] = []
    control: list[tuple] = []
    filtered: list[tuple] = []
    for _ in range(latency_reps):
        t0 = time.perf_counter()
        control = query.search(query_text, limit=limit)
        control_ms.append((time.perf_counter() - t0) * 1000.0)
        t1 = time.perf_counter()
        filtered = query.search(query_text, limit=limit, filters=filters)
        filtered_ms.append((time.perf_counter() - t1) * 1000.0)

    matches = lambda rows: [  # noqa: E731
        _independent_matches(r, ledger_row, corpus, by_content, session_prefix=session_prefix) for r in rows
    ]
    control_ok = matches(control)
    filtered_ok = matches(filtered)

    before = sum(1 for ok in control_ok if not ok)
    after = sum(1 for ok in filtered_ok if not ok)

    # Per-kind before/after query counts (independent per-kind verdicts).
    before_kind = {"source": 0, "session": 0, "time": 0}
    after_kind = {"source": 0, "session": 0, "time": 0}
    kind_keys = _filter_kinds(ledger_row)
    for r in control:
        for kind in kind_keys:
            if not _independent_kind_verdicts(r, ledger_row, corpus, by_content, session_prefix=session_prefix)[
                kind.replace("_filter", "")
            ]:
                before_kind[kind.replace("_filter", "")] += 1
    for r in filtered:
        for kind in kind_keys:
            if not _independent_kind_verdicts(r, ledger_row, corpus, by_content, session_prefix=session_prefix)[
                kind.replace("_filter", "")
            ]:
                after_kind[kind.replace("_filter", "")] += 1

    # Coverage risk: control rows the filter SHOULD allow but the filtered
    # result dropped (false negatives introduced by the filter itself).
    allowed_control = [r for r, ok in zip(control, control_ok) if ok]
    allowed_filtered_contents = {_row_content(r) for r, ok in zip(filtered, filtered_ok) if ok}
    coverage_risk = sum(1 for r in allowed_control if _row_content(r) not in allowed_filtered_contents)

    def _percentile(vals: list[float], p: float) -> float:
        s = sorted(vals)
        idx = min(int(len(s) * p / 100.0), len(s) - 1)
        return s[idx]

    return {
        "before": before,
        "after": after,
        "before_kind": before_kind,
        "after_kind": after_kind,
        "control_returned": len(control),
        "filtered_returned": len(filtered),
        "coverage_risk": coverage_risk,
        "filtered_is_order_preserving_subsequence": _is_order_preserving_subsequence(filtered, control),
        "control_latency_ms": {
            "sample_count": len(control_ms),
            "p50_ms": round(_percentile(control_ms, 50), 3),
            "p95_ms": round(_percentile(control_ms, 95), 3),
        },
        "filtered_latency_ms": {
            "sample_count": len(filtered_ms),
            "p50_ms": round(_percentile(filtered_ms, 50), 3),
            "p95_ms": round(_percentile(filtered_ms, 95), 3),
        },
        "_control_raw_ms": control_ms,
        "_filtered_raw_ms": filtered_ms,
    }


def _runtime_head() -> str:
    """Exact HEAD of the harness checkout (must match the runtime commit)."""
    import subprocess

    out = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    head = out.stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        raise FilterEvalError(f"git rev-parse HEAD returned invalid sha: {head!r}")
    return head


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 4E1 dev-only filter evaluation")
    parser.add_argument("--dsn", default=os.environ.get("SHIORI_DATABASE_DSN"))
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--out-results", type=Path, default=OUT_RESULTS)
    parser.add_argument("--out-report", type=Path, default=OUT_REPORT)
    parser.add_argument(
        "--harness-sha",
        default=None,
        help="expected exact HEAD; if given, must equal `git rev-parse HEAD` (fail closed)",
    )
    args = parser.parse_args(argv)
    if not args.dsn:
        parser.error("SHIORI_DATABASE_DSN is required")

    harness_sha = _runtime_head()
    if args.harness_sha and args.harness_sha != harness_sha:
        raise FilterEvalError(
            f"--harness-sha {args.harness_sha} does not match runtime HEAD {harness_sha}"
        )

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
    all_golden_texts = set(golden_texts.values())

    def _replay(qtext: str) -> list[float]:
        # Pinned local replay ONLY: the 72-dev id -> frozen vector map is the
        # complete universe.  Unknown/missing text, duplicate/missing/extra ids,
        # wrong dimension or non-finite values all fail closed.  The original
        # provider is NEVER called (no model/API/network).
        qid = qid_by_canon.get(qtext)
        if qid is None:
            if qtext in all_golden_texts:
                raise FilterEvalError(f"golden query text has no frozen vector: {qid}")
            raise FilterEvalError("replay only supports the frozen 72-dev golden query texts")
        emb = vec_by_qid.get(qid)
        if emb is None:
            raise FilterEvalError(f"no frozen vector for dev query id {qid}")
        if len(emb) != EMBED_DIM or not all(isinstance(x, (int, float)) and __import__("math").isfinite(float(x)) for x in emb):
            raise FilterEvalError(f"dev query vector for {qid} is not finite {EMBED_DIM}-dim")
        return emb

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
        all_control_raw: list[float] = []
        all_filtered_raw: list[float] = []
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
                latency_reps=LATENCY_REPS,
            )
            raw_control = case.pop("_control_raw_ms")
            raw_filtered = case.pop("_filtered_raw_ms")
            all_control_raw.extend(raw_control)
            all_filtered_raw.extend(raw_filtered)
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
        kind_counts = {
            "source_filter": sum(1 for c in cases if "source_filter" in c["filter_kinds"]),
            "session_filter": sum(1 for c in cases if "session_filter" in c["filter_kinds"]),
            "time_filter": sum(1 for c in cases if "time_filter" in c["filter_kinds"]),
        }
        # Per-kind before/after QUERY counts (query-level, reconciles to the
        # frozen Phase 4D evidence 9/9/3 before / 0/0/0 after).
        before_kind = {"source": 0, "session": 0, "time": 0}
        after_kind = {"source": 0, "session": 0, "time": 0}
        for c in cases:
            for kind in c["before_kind"]:
                if c["before_kind"][kind] > 0:
                    before_kind[kind] += 1
                if c["after_kind"][kind] > 0:
                    after_kind[kind] += 1
        leakage_by_kind = {
            "source": {"before_query_count": before_kind["source"], "after_query_count": after_kind["source"]},
            "session": {"before_query_count": before_kind["session"], "after_query_count": after_kind["session"]},
            "time": {"before_query_count": before_kind["time"], "after_query_count": after_kind["time"]},
        }

        # True aggregate latency: percentiles over ALL raw timing samples
        # (9 cases x LATENCY_REPS), not the mean of per-case percentiles.
        def _pct(vals: list[float], p: float) -> float:
            s = sorted(vals)
            return s[min(int(len(s) * p / 100.0), len(s) - 1)]

        latency = {
            "latency_reps": LATENCY_REPS,
            "control_p50_ms": round(_pct(all_control_raw, 50), 3),
            "control_p95_ms": round(_pct(all_control_raw, 95), 3),
            "filtered_p50_ms": round(_pct(all_filtered_raw, 50), 3),
            "filtered_p95_ms": round(_pct(all_filtered_raw, 95), 3),
        }

        # 72-dev unfiltered base-vs-head regression (independent oracle), read
        # from the frozen baseline + the exact-head runner artifact.  The SHAs
        # are validated against the ACTUAL files before loading (fail closed),
        # and trace comparison requires exact config/qid/event-length equality.
        if not BASELINE.is_file():
            raise FilterEvalError("frozen baseline_72_results.json missing for unfiltered_regression")
        if _sha256(BASELINE) != BASELINE_RUNNER_SHA256:
            raise FilterEvalError(
                f"baseline_72_results.json SHA mismatch: {_sha256(BASELINE)[:16]}… != {BASELINE_RUNNER_SHA256[:16]}…"
            )
        baseline = _load_json(BASELINE)
        head_runner_path = REPO_ROOT / "benchmark" / ".generated" / "task25_runner_72.json"
        if not head_runner_path.is_file():
            raise FilterEvalError("head runner artifact .generated/task25_runner_72.json missing")
        if _sha256(head_runner_path) != HEAD_RUNNER_SHA256:
            raise FilterEvalError(
                f"task25_runner_72.json SHA mismatch: {_sha256(head_runner_path)[:16]}… != {HEAD_RUNNER_SHA256[:16]}…"
            )
        head_runner = _load_json(head_runner_path)

        base_configs = set(baseline["configs"])
        head_configs = set(head_runner["configs"])
        if base_configs != head_configs:
            raise FilterEvalError(f"runner config sets differ: base-head={base_configs - head_configs}")
        config_deltas: dict[str, dict[str, float]] = {}
        trace_mismatch = {"doc_rank_reason_stage": 0, "score_only": 0, "events": 0}
        for cfg in sorted(base_configs):
            bb = baseline["configs"][cfg]
            hh = head_runner["configs"][cfg]
            config_deltas[cfg] = {
                k: round(hh[k] - bb[k], 9)
                for k in ("final_recall@5", "final_mrr@10", "final_ndcg@10", "candidate_recall_at_20", "filter_leakage")
            }
            bt = baseline["traces"][cfg]
            ht = head_runner["traces"][cfg]
            if set(bt) != set(ht):
                raise FilterEvalError(f"{cfg}: trace qid sets differ")
            for qid in sorted(bt):
                if len(bt[qid]) != len(ht[qid]):
                    raise FilterEvalError(f"{cfg}/{qid}: trace event length differs")
                for be, he in zip(bt[qid], ht[qid]):
                    trace_mismatch["events"] += 1
                    if (
                        be.get("doc_id") != he.get("doc_id")
                        or be.get("rank") != he.get("rank")
                        or be.get("reason") != he.get("reason")
                        or be.get("stage") != he.get("stage")
                    ):
                        trace_mismatch["doc_rank_reason_stage"] += 1
                    elif be.get("score") != he.get("score"):
                        trace_mismatch["score_only"] += 1
        base_lat = baseline["e2e_latency_ms"]
        head_lat = head_runner["e2e_latency_ms"]
        unfiltered_regression = {
            "frozen_baseline_runner_sha256": BASELINE_RUNNER_SHA256,
            "head_runner_sha256": HEAD_RUNNER_SHA256,
            "config_metric_deltas": config_deltas,
            "trace_mismatch": trace_mismatch,
            "score_tolerance_note": "score-only diffs are ~1e-9 float noise from temporal-decay now between separate runs; doc/rank/reason/stage diffs are the regression signal",
            "base_head_latency_p50_p95_ms": {
                cfg: {
                    "base_p50": round(base_lat[cfg]["p50_ms"], 3),
                    "base_p95": round(base_lat[cfg]["p95_ms"], 3),
                    "head_p50": round(head_lat[cfg]["p50_ms"], 3),
                    "head_p95": round(head_lat[cfg]["p95_ms"], 3),
                }
                for cfg in baseline["configs"]
            },
        }

        input_hashes = {
            "corpus.jsonl": _sha256(CORPUS),
            "golden_queries.jsonl": _sha256(GOLDEN),
            "evidence_ledger.json": _sha256(LEDGER),
            "dataset_manifest.json": _sha256(MANIFEST),
            "dev_query_vectors.json": _sha256(DEV_VECTORS),
        }
        results = {
            "schema": "shiori-filter-eval/v4",
            "harness_sha": harness_sha,
            "implementation_sha": os.environ.get("SHIORI_FILTER_EVAL_BASE_SHA", ""),
            "embedding_mode": "pinned_local_replay",
            "model_identity": MODEL_IDENTITY,
            "input_hashes": input_hashes,
            "kind_counts": kind_counts,
            "leakage_by_kind": leakage_by_kind,
            "latency": latency,
            "unfiltered_regression": unfiltered_regression,
            "dev_count": len(filter_ids),
            "holdout_ids_used": [],
            "cases": cases,
            "total_before_leakage": total_before,
            "total_after_leakage": total_after,
            "total_coverage_risk": total_coverage_risk,
            "ok": total_after == 0
            and all(c["filtered_is_order_preserving_subsequence"] for c in cases),
        }
        args.out_results.write_text(json.dumps(results, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        lines = [
            "# Phase 4E1 dev-only filter evaluation",
            "",
            "- schema: `shiori-filter-eval/v4`",
            f"- harness SHA: `{results['harness_sha']}`",
            f"- implementation SHA: `{results['implementation_sha'] or '(unset)'}`",
            f"- embedding mode: `{results['embedding_mode']}`",
            f"- model identity: `{MODEL_IDENTITY}`",
            f"- dev filter cases: {len(filter_ids)} (72-dev only, holdout untouched)",
            f"- latency reps: {LATENCY_REPS}",
            f"- input hashes: {json.dumps(input_hashes, sort_keys=True)}",
            f"- kind counts: {json.dumps(kind_counts, sort_keys=True)}",
            f"- leakage by kind (before/after query counts): {json.dumps(leakage_by_kind, sort_keys=True)}",
            f"- total before leakage (rows): {total_before}",
            f"- total after leakage (rows): {total_after}",
            f"- total coverage risk (rows): {total_coverage_risk}",
            f"- latency p50/p95 (aggregate over {len(all_filtered_raw)} raw samples): {json.dumps(latency, sort_keys=True)}",
            f"- unfiltered regression: {json.dumps(unfiltered_regression, sort_keys=True)}",
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
