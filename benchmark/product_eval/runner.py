"""Phase 4D runner: exercises the REAL PostgreSQL search pipeline via the
behavior-preserving ablation seam (task #18).

The runner NEVER recomputes ranking in benchmark code. It installs each frozen
StageConfig through query._eval_scope, calls the real query.search against
PostgreSQL, collects the emitted per-stage trace, validates every event with
benchmark.product_eval.trace.validate_trace_event, and computes the frozen
metrics by REUSING benchmark.product_eval.evaluator (Recall@k, MRR@k, nDCG@k
with graded gains).

Development-split isolation is enforced at process startup: the split sidecar
allows ONLY the 72 development query ids; any holdout id that reaches
embedding/search/trace/report fails closed. The local query-vector file must
contain EXACTLY the 72 development ids (no holdout, no extra, no duplicate,
finite 1024-dim floats).

All query.* global overrides are restored via try/finally so an exception never
pollutes later runs. The report contains only aggregates and stable ids (no
content, no local paths, no keys).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
from pathlib import Path

# Make the repo root importable when run as a direct script.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import query
from benchmark.product_eval import evaluator
from benchmark.product_eval.identity import EMBED_DIM, MODEL_IDENTITY
from benchmark.product_eval.manifest import load_golden_rows, load_manifest
from benchmark.product_eval.trace import validate_trace_event

# Frozen ablation matrix (the SAME semantics as the golden Phase 4D contract).
FROZEN_CONFIGS: dict[str, dict] = {
    "dense-only": {"dense": True, "lexical": False, "exact": False, "temporal": False, "dedup": False},
    "lexical-only": {"dense": False, "lexical": True, "exact": False, "temporal": False, "dedup": False},
    "rrf": {"dense": True, "lexical": True, "exact": False, "temporal": False, "dedup": False},
    "+exact": {"dense": True, "lexical": True, "exact": True, "temporal": False, "dedup": False},
    "+temporal": {"dense": True, "lexical": True, "exact": True, "temporal": True, "dedup": False},
    "+dedup": {"dense": True, "lexical": True, "exact": True, "temporal": True, "dedup": True},
}

CANDIDATE_TOP_K = 20
FINAL_TOP_K = 10

# Single frozen embedding identity is imported from identity.py (one source of
# truth shared by runner + ingest + SQL gate).

class RunnerError(ValueError):
    """Raised when the runner violates a Phase 4D contract."""


def _read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _load_development_ids(manifest_path: Path) -> set[str]:
    manifest = load_manifest(manifest_path)
    dev = {s["query_id"] for s in manifest["query_splits"] if s["split"] == "tune"}
    holdout = {s["query_id"] for s in manifest["query_splits"] if s["split"] == "holdout"}
    if len(dev) != 72:
        raise RunnerError(f"development split must contain exactly 72 ids, got {len(dev)}")
    if len(holdout) != 48:
        raise RunnerError(f"holdout split must contain exactly 48 ids, got {len(holdout)}")
    if dev & holdout:
        raise RunnerError("development/holdout splits overlap")
    return dev


def _guard_row(row: dict, dev_ids: set[str]) -> None:
    qid = row.get("query_id")
    if qid is not None and qid not in dev_ids:
        raise RunnerError(f"holdout query id {qid} reached the runner (fail closed)")


def _p_percentile(values, p):
    if not values:
        return 0.0
    sorted_v = sorted(values)
    idx = max(0, min(len(sorted_v) - 1, int(math.ceil(p / 100.0 * len(sorted_v)) - 1)))
    return sorted_v[idx]


def _aggregate_smoke(config_name, per_config):
    """Aggregate per-query results for one config into the smoke report."""
    qual = [r for r in per_config if not r["expected_no_evidence"]]
    no_ev = [r for r in per_config if r["expected_no_evidence"]]
    return {
        "config": config_name,
        "query_count": len(per_config),
        "candidate_recall_at_20": (
            statistics.mean([r["candidate_recall@20"] for r in qual]) if qual else None
        ),
        "final_recall@5": statistics.mean([r["recall@5"] for r in qual]) if qual else None,
        "final_mrr@10": statistics.mean([r["mrr@10"] for r in qual]) if qual else None,
        "final_ndcg@10": statistics.mean([r["ndcg@10"] for r in qual]) if qual else None,
        "dedup_drop_rate": (
            statistics.mean([r["dedup_drop_rate"] for r in qual if r["dedup_drop_rate"] is not None])
            if any(r["dedup_drop_rate"] is not None for r in qual)
            else None
        ),
        "duplicate_group_coverage": (
            statistics.mean([r["duplicate_group_coverage"] for r in qual if r["duplicate_group_coverage"] is not None])
            if any(r["duplicate_group_coverage"] is not None for r in qual)
            else None
        ),
        "duplicate_rate": (
            statistics.mean([r["duplicate_rate"] for r in qual if r["duplicate_rate"] is not None])
            if any(r["duplicate_rate"] is not None for r in qual)
            else None
        ),
        "coverage_risk_dropped_relevant": sum(
            1 for r in per_config if r.get("coverage_risk_dropped_relevant")
        ),
        "filter_leakage": sum(1 for r in per_config if r.get("filter_leakage")),
        "no_evidence_queries": len(no_ev),
        "no_evidence_false_return": sum(1 for r in no_ev if r.get("no_evidence_behavior") == "false_return"),
        "no_evidence_abstention": sum(1 for r in no_ev if r.get("no_evidence_behavior") == "abstention_like"),
    }


def run_smoke(
    *,
    manifest_path: Path,
    rows_path: Path,
    dev_limit: int = 5,
    embedding_json: Path | None = None,
    doc_id_map: Path | None = None,
    query_ids: list[str] | None = None,
) -> dict:
    """Run a development-split smoke over `dev_limit` queries for every frozen
    config. Returns the aggregate smoke report (aggregates + stable ids only;
    no content/paths/keys).

    `embedding_json` must contain EXACTLY the 72 development query vectors
    (offline-generated with the pinned voyage-4-nano model into
    benchmark/.generated/). `doc_id_map` maps DB row uuid -> fixture doc id.
    `query_ids`, when given, selects specific development ids (still a subset
    of the 72); otherwise the first `dev_limit` development ids are used.
    """
    if not isinstance(dev_limit, int) or isinstance(dev_limit, bool) or not 1 <= dev_limit <= 72:
        raise RunnerError(f"dev_limit must be an int in [1, 72], got {dev_limit!r}")
    dev_ids = _load_development_ids(manifest_path)
    rows = load_golden_rows(rows_path)
    dev_rows = [r for r in rows if r["query_id"] in dev_ids]
    if query_ids is not None:
        query_ids = list(dict.fromkeys(query_ids))  # dedup, preserve order
        extra = set(query_ids) - dev_ids
        if extra:
            raise RunnerError(f"query_ids contains non-development ids: {sorted(extra)}")
        if len(query_ids) != dev_limit:
            raise RunnerError(f"query_ids length {len(query_ids)} must equal dev_limit {dev_limit}")
        dev_rows = [r for r in dev_rows if r["query_id"] in set(query_ids)]
    if len(dev_rows) < 1:
        raise RunnerError("no development rows available for smoke")
    smoke_rows = dev_rows[:dev_limit]
    for row in smoke_rows:
        _guard_row(row, dev_ids)

    # doc_id_map is REQUIRED and must be an exact 1:1 mapping of the 22 DB
    # uuid rows -> the 22 fixture doc ids (no missing/extra).
    if doc_id_map is None or not doc_id_map.is_file():
        raise RunnerError("doc_id_map is required for the Phase 4D smoke")
    _fixture_of: dict[str, str] = json.loads(doc_id_map.read_text(encoding="utf-8"))
    if len(_fixture_of) != 22:
        raise RunnerError(f"doc_id_map must map exactly 22 rows, got {len(_fixture_of)}")
    if len(set(_fixture_of)) != len(_fixture_of) or len(set(_fixture_of.values())) != len(_fixture_of):
        raise RunnerError("doc_id_map must be a bijection (unique keys and unique values)")
    if set(_fixture_of.values()) != set(_corpus_fixture_ids()):
        raise RunnerError("doc_id_map values must be exactly the 22 fixture doc ids")

    def _to_fixture(doc_id: str) -> str:
        if doc_id not in _fixture_of:
            raise RunnerError("DB row uuid has no fixture mapping")
        return _fixture_of[doc_id]

    dsn_env = os.environ.get("SHIORI_DATABASE_DSN")
    if not dsn_env:
        raise RunnerError("SHIORI_DATABASE_DSN is required for the Phase 4D smoke")

    # Strict embedding-key closure: exactly the 72 dev ids, finite 1024-dim.
    if embedding_json is None:
        raise RunnerError("embedding_json is required for the Phase 4D smoke")
    if not embedding_json.is_file():
        raise RunnerError("embedding_json file is missing or unreadable")
    _embedding_lookup: dict[str, list[float]] = {}
    vec = json.loads(embedding_json.read_text(encoding="utf-8"))
    seen: set[str] = set()
    for item in vec:
        qid = item.get("query_id")
        emb = item.get("embedding")
        if not isinstance(qid, str) or not isinstance(emb, list):
            raise RunnerError("malformed embedding entry")
        if qid in seen:
            raise RunnerError(f"duplicate embedding query id: {qid}")
        seen.add(qid)
        if len(emb) != EMBED_DIM:
            raise RunnerError(f"embedding for {qid} has dim {len(emb)} != {EMBED_DIM}")
        if not all(isinstance(x, (int, float)) and math.isfinite(float(x)) for x in emb):
            raise RunnerError(f"embedding for {qid} has non-finite/non-numeric values")
        _embedding_lookup[qid] = [float(x) for x in emb]
    if set(_embedding_lookup) != dev_ids:
        missing = sorted(dev_ids - set(_embedding_lookup))
        extra = sorted(set(_embedding_lookup) - dev_ids)
        raise RunnerError(f"embedding key set must equal the 72 dev ids: missing={missing} extra={extra}")

    _canonical_emb: dict[str, list[float]] = {}
    for row in dev_rows:
        _guard_row(row, dev_ids)
        canon = row.get("canonical_query") or row["query_text"]
        qid = row["query_id"]
        if qid in _embedding_lookup:
            _canonical_emb[canon] = _embedding_lookup[qid]

    def _local_embed(qtext):
        if qtext in _canonical_emb:
            return _canonical_emb[qtext]
        raise RunnerError("no local embedding for the query identity")

    # Save + restore query module globals (never leak across runs/exceptions).
    saved = {
        "DATABASE_DSN": query.DATABASE_DSN,
        "EMBEDDING_PROVIDER": query.EMBEDDING_PROVIDER,
        "VOYAGE_MODEL": query.VOYAGE_MODEL,
        "EMBED_DIM": query.EMBED_DIM,
        "embed_query": query.embed_query,
    }
    try:
        query.DATABASE_DSN = dsn_env
        query.EMBEDDING_PROVIDER = "fake"
        query.VOYAGE_MODEL = MODEL_IDENTITY
        query.EMBED_DIM = EMBED_DIM
        query.embed_query = _local_embed

        # Assert the DB contains EXACTLY the frozen embedding identity rows.
        _assert_model_identity(query, dsn_env)

        report = _run_configs(
            query=query,
            configs=FROZEN_CONFIGS,
            smoke_rows=smoke_rows,
            dev_ids=dev_ids,
            to_fixture=_to_fixture,
            manifest_path=manifest_path,
        )
    finally:
        for name, value in saved.items():
            setattr(query, name, value)
    return report


def _corpus_fixture_ids() -> set[str]:
    """The 22 fixture doc ids from the committed task #11 corpus."""
    corpus_path = Path(__file__).resolve().parents[2] / "benchmark" / "fixtures" / "corpus.jsonl"
    return {doc["id"] for doc in _read_jsonl(corpus_path)}


def _content_fixture_map() -> dict[str, str]:
    """content -> fixture doc id from the committed task #11 corpus.

    Two corpus docs (doc-0017/doc-0018) share identical content and session;
    for those, content maps to a stable canonical id and the consistency check
    treats them as equivalent (content-level, order-preserving).
    """
    corpus_path = Path(__file__).resolve().parents[2] / "benchmark" / "fixtures" / "corpus.jsonl"
    by_content: dict[str, str] = {}
    for doc in _read_jsonl(corpus_path):
        by_content.setdefault(doc["content"], doc["id"])
    return by_content


def _content_ids(corpus_path: Path) -> dict[str, set[str]]:
    """content -> set of fixture doc ids (for ambiguous duplicate content)."""
    by_content: dict[str, set[str]] = {}
    for doc in _read_jsonl(corpus_path):
        by_content.setdefault(doc["content"], set()).add(doc["id"])
    return by_content


def _row_ledger_tags(manifest_path: Path, qid: str) -> dict:
    """The ledger tags for a query id from the committed manifest splits."""
    manifest = load_manifest(manifest_path)
    for entry in manifest.get("query_splits", []):
        if entry["query_id"] == qid:
            return {tag: True for tag in entry.get("tags", [])}
    return {}


def _row_ledger_evidence(manifest_path: Path, qid: str) -> dict:
    """The ledger evidence map for a query id from the committed manifest."""
    manifest = load_manifest(manifest_path)
    for entry in manifest.get("query_splits", []):
        if entry["query_id"] == qid:
            return entry.get("evidence", {})
    return {}


def _aggregate_by_bucket(manifest_path: Path, per_config_results: dict[str, list[dict]]) -> dict:
    """Per-config x per-bucket aggregates of the frozen metrics."""
    manifest = load_manifest(manifest_path)
    bucket_of = {s["query_id"]: s["bucket"] for s in manifest.get("query_splits", [])}
    buckets = sorted({b for b in bucket_of.values()})
    out: dict[str, dict] = {}
    for config_name, rows in per_config_results.items():
        out[config_name] = {}
        for bucket in buckets:
            q_rows = [r for r in rows if bucket_of.get(r["query_id"]) == bucket]
            qual = [r for r in q_rows if not r["expected_no_evidence"]]
            if not qual:
                out[config_name][bucket] = {
                    "query_count": len(q_rows),
                    "recall@5": None,
                    "mrr@10": None,
                    "ndcg@10": None,
                }
                continue
            out[config_name][bucket] = {
                "query_count": len(q_rows),
                "recall@5": statistics.mean([r["recall@5"] for r in qual]),
                "mrr@10": statistics.mean([r["mrr@10"] for r in qual]),
                "ndcg@10": statistics.mean([r["ndcg@10"] for r in qual]),
            }
    return out


def _duplicate_metrics(groups: list[list[str]], final_ranked: list[str]) -> tuple[float | None, float | None]:
    """(duplicate_group_coverage, final_duplicate_rate).

    - duplicate_group_coverage: fraction of authored groups with >=1 member in
      the final result.
    - final_duplicate_rate: sum(max(0, hits-1)) over groups / final result
      count (redundancy ratio). None when no groups.
    """
    if not groups:
        return None, None
    represented = sum(1 for group in groups if any(fid in final_ranked for fid in group))
    coverage = represented / len(groups)
    redundancy = sum(max(0, sum(1 for fid in final_ranked if fid in group) - 1) for group in groups)
    rate = redundancy / len(final_ranked) if len(final_ranked) > 0 else 0.0
    return coverage, rate


def _temporal_transition(pre_rank: int | None, post_rank: int | None) -> dict:
    """rank_changed vs top-1 winner_transition vs promoted_to_winner."""
    rank_changed = bool(pre_rank is not None and post_rank is not None and pre_rank != post_rank)
    winner_transition = bool(
        pre_rank is not None and post_rank is not None and (pre_rank == 1) != (post_rank == 1)
    )
    promoted = bool(pre_rank is not None and post_rank == 1 and pre_rank != 1)
    return {
        "rank_changed": rank_changed,
        "winner_transition": winner_transition,
        "promoted_to_winner": promoted,
    }


def _assert_model_identity(query_mod, dsn) -> None:
    """Fail closed unless the isolated DB contains EXACTLY 22 rows, all with the
    frozen embedding identity and 1024-dim vectors (never pass on an empty
    table)."""
    import psycopg2

    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM session_chunks")
            row = cur.fetchone()
            count = row[0] if row is not None else 0
            if count != 22:
                raise RunnerError(f"DB must contain exactly 22 corpus rows, got {count}")
            cur.execute("SELECT DISTINCT embedding_model FROM session_chunks")
            models = {row[0] for row in cur.fetchall()}
            if models != {MODEL_IDENTITY}:
                raise RunnerError(
                    f"DB embedding_model must be exactly {{'{MODEL_IDENTITY}'}}, got {sorted(models)}"
                )
            cur.execute(
                "SELECT count(*) FROM session_chunks WHERE embedding IS NULL OR vector_dims(embedding) != %s",
                (EMBED_DIM,),
            )
            row2 = cur.fetchone()
            bad_dims = row2[0] if row2 is not None else 0
            if bad_dims != 0:
                raise RunnerError(f"DB has {bad_dims} rows with missing or non-{EMBED_DIM}-dim vectors")
    finally:
        conn.close()


def _run_configs(*, query, configs, smoke_rows, dev_ids, to_fixture, manifest_path) -> dict:
    per_config_results: dict[str, list[dict]] = {}
    per_config_latency: dict[str, dict[str, dict]] = {}
    per_config_sources: dict[str, dict[str, int]] = {}
    per_config_e2e: dict[str, dict] = {}

    for config_name, switches in configs.items():
        config = query.StageConfig(**switches)
        per_query: list[dict] = []
        stage_latency_ms: dict[str, list[float]] = {}
        e2e_ms: list[float] = []
        candidate_sources: dict[str, int] = {}
        for row in smoke_rows:
            qid = row["query_id"]
            _guard_row(row, dev_ids)
            events: list[tuple[str, list[dict]]] = []

            def collect(stage, stage_events):
                events.append((stage, stage_events))

            import time

            _t0 = time.perf_counter()
            with query._eval_scope(config=config, collector=collect):
                returned = query.search(
                    row.get("canonical_query") or row["query_text"],
                    limit=FINAL_TOP_K,
                )
            e2e_ms.append((time.perf_counter() - _t0) * 1000.0)
            # Validate the emitted trace contract (adds per-stage latency check).
            for stage, stage_events in events:
                for ev in stage_events:
                    ev["stage"] = stage
                    validate_trace_event(ev)
            stages = {stage: evs for stage, evs in events}
            for stage, evs in events:
                lat = [ev["latency_ms"] for ev in evs if isinstance(ev.get("latency_ms"), (int, float))]
                if lat:
                    stage_latency_ms.setdefault(stage, []).extend(float(x) for x in lat)
            for stage in ("dense", "ts_rank_cd", "exact", "trigram"):
                for ev in stages.get(stage, []):
                    if "doc_id" in ev:
                        candidate_sources[stage] = candidate_sources.get(stage, 0) + 1

            relevance = row.get("relevance") or {}
            relevant = {doc_id for doc_id, grade in relevance.items() if grade > 0}
            expected_no_evidence = row.get("expected_no_evidence", False)

            dedup_events = stages.get("dedup", [])
            dedup_active = config.dedup and any(ev.get("reason") in ("mmr_keep", "mmr_dedup") for ev in dedup_events)
            keeps = [
                to_fixture(ev["doc_id"]) for ev in dedup_events if ev.get("reason") == "mmr_keep"
            ]
            drops = [
                to_fixture(ev["doc_id"]) for ev in dedup_events if ev.get("reason") == "mmr_dedup"
            ]
            candidate_ranked = [
                to_fixture(ev["doc_id"])
                for ev in stages.get("temporal", [])
                if "doc_id" in ev
            ]
            if not candidate_ranked:
                candidate_ranked = [
                    to_fixture(ev["doc_id"])
                    for ev in stages.get("rrf", [])
                    if "doc_id" in ev
                ]
            final_ranked = keeps if dedup_active else candidate_ranked[:FINAL_TOP_K]

            # Consistency: the actual query.search() returned rows (by content)
            # must map, IN ORDER, to the final trace fixture ids. The lengths
            # must be EXACTLY equal (including the empty-return/non-empty-trace
            # case, which must fail closed). Ambiguous duplicate content
            # (doc-0017/doc-0018) is treated as a set-match at that position.
            corpus_path = Path(__file__).resolve().parents[2] / "benchmark" / "fixtures" / "corpus.jsonl"
            content_ids = _content_ids(corpus_path)
            returned_contents = [row[0] for row in returned]
            if len(returned_contents) != len(final_ranked):
                raise RunnerError(
                    f"trace/return length mismatch for {qid} config {config_name}: "
                    f"returned={len(returned_contents)} trace_final={len(final_ranked)}"
                )
            for i, content in enumerate(returned_contents):
                possible = content_ids.get(content, set())
                expected_id = final_ranked[i]
                if expected_id not in possible:
                    raise RunnerError(
                        f"trace/return mismatch for {qid} config {config_name} at rank {i+1}: "
                        f"returned content maps to {sorted(possible)} but trace has {expected_id}"
                    )

            # dedup_drop_rate: drops/(keeps+drops) when the dedup stage ran;
            # N/A (None) when there is no dedup stage.
            dedup_drop_rate = (
                (len(drops) / (len(keeps) + len(drops))) if dedup_active and (keeps or drops) else None
            )
            coverage_risk_dropped_relevant = bool(set(drops) & relevant)

            from benchmark.product_eval.manifest import _load_corpus_meta

            corpus = _load_corpus_meta(
                Path(__file__).resolve().parents[2] / "benchmark" / "fixtures" / "corpus.jsonl"
            )

            # Per-tag filter leakage against the ledger evidence (source_kind /
            # session / time constraints). Each final-ranked doc is checked
            # against the tag's constraint.
            row_evidence = _row_ledger_evidence(manifest_path, qid)
            leakage: dict[str, bool] = {}
            if "source_filter" in row_evidence:
                allowed_kind = row_evidence["source_filter"].get("kind")
                leakage["source_filter"] = any(
                    fid in corpus and corpus[fid]["source_kind"] != allowed_kind for fid in final_ranked
                )
            if "session_filter" in row_evidence:
                allowed_session = row_evidence["session_filter"].get("session")
                leakage["session_filter"] = any(
                    fid in corpus and corpus[fid]["session"] != allowed_session for fid in final_ranked
                )
            if "time_filter" in row_evidence:
                from datetime import datetime

                tf = row_evidence["time_filter"]
                op = tf.get("operator")
                parsed_bound = datetime.fromisoformat(tf["iso_bound"].replace("Z", "+00:00"))

                def _violates(fid) -> bool:
                    if fid not in corpus:
                        return True
                    ts = corpus[fid].get("timestamp")
                    if not ts:
                        return True
                    parsed_ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    if op == "before":
                        return not (parsed_ts < parsed_bound)
                    if op == "after":
                        return not (parsed_ts > parsed_bound)
                    return parsed_ts != parsed_bound  # at

                leakage["time_filter"] = any(_violates(fid) for fid in final_ranked)
            filter_leakage = any(leakage.values())

            # duplicate metrics from authored duplicate_groups:
            # - duplicate_group_coverage: fraction of authored groups with >=1
            #   member in the final result.
            # - final_duplicate_rate: total redundancy sum(max(0, hits-1)) over
            #   groups / final result count (>=2-member near-duplicate groups).
            duplicate_group_coverage = None
            final_duplicate_rate = None
            if "duplicate_groups" in row_evidence:
                groups = row_evidence["duplicate_groups"].get("groups", [])
                duplicate_group_coverage, final_duplicate_rate = _duplicate_metrics(groups, final_ranked)

            # temporal winner: ONLY for authored knowledge_update queries, record
            # the rank of the NEWEST relevant doc (the update target) in the
            # final ranking. Post-processed across configs (pre=+exact,
            # post=+temporal) after all configs run.
            temporal_newest_rank = None  # None = not eligible
            if _row_ledger_tags(manifest_path, qid).get("knowledge_update"):
                if relevant and len(relevant) >= 2:
                    newest_fid = max(relevant, key=lambda fid: corpus.get(fid, {}).get("timestamp", ""))
                    for rank, fid in enumerate(final_ranked, start=1):
                        if fid == newest_fid:
                            temporal_newest_rank = rank
                            break
                    if temporal_newest_rank is None:
                        temporal_newest_rank = len(final_ranked) + 1  # not in top

            # Per-query sanitized stable-ID trace (allowed fields only),
            # INCLUDING stage latency summary events so p50/p95 are recomputable.
            sanitized_trace = [
                {
                    "doc_id": to_fixture(ev["doc_id"]),
                    "stage": ev.get("stage") or stage,
                    "rank": ev.get("rank"),
                    "score": ev.get("score"),
                    "reason": ev.get("reason"),
                    "latency_ms": ev.get("latency_ms"),
                }
                for stage, stage_events in events
                for ev in stage_events
                if "doc_id" in ev
            ]
            for stage, stage_events in events:
                for ev in stage_events:
                    if "doc_id" not in ev and "latency_ms" in ev:
                        sanitized_trace.append(
                            {
                                "doc_id": None,
                                "stage": ev.get("stage") or stage,
                                "rank": None,
                                "score": None,
                                "reason": ev.get("reason"),
                                "latency_ms": ev.get("latency_ms"),
                            }
                        )
            for event in sanitized_trace:
                validate_trace_event({k: v for k, v in event.items() if v is not None})

            row_result = {
                "query_id": qid,
                "expected_no_evidence": expected_no_evidence,
                "candidate_recall@20": evaluator.recall_at_k(candidate_ranked, relevant, CANDIDATE_TOP_K),
                "recall@5": None if expected_no_evidence else evaluator.recall_at_k(final_ranked, relevant, 5),
                "mrr@10": None if expected_no_evidence else evaluator.reciprocal_rank(final_ranked, relevant, FINAL_TOP_K),
                "ndcg@10": None if expected_no_evidence else evaluator.ndcg_at_k(final_ranked, relevance, FINAL_TOP_K),
                "dedup_drop_rate": dedup_drop_rate,
                "duplicate_group_coverage": duplicate_group_coverage,
                "duplicate_rate": final_duplicate_rate,
                "coverage_risk_dropped_relevant": coverage_risk_dropped_relevant,
                "filter_leakage": filter_leakage,
                "temporal_newest_rank": temporal_newest_rank,
                "no_evidence_behavior": (
                    "false_return" if expected_no_evidence and final_ranked else ("abstention_like" if expected_no_evidence else None)
                ),
                "trace": sanitized_trace,
            }
            per_query.append(row_result)
        per_config_results[config_name] = per_query
        per_config_latency[config_name] = {
            stage: {"sample_count": len(v), "p50_ms": _p_percentile(v, 50), "p95_ms": _p_percentile(v, 95)}
            for stage, v in stage_latency_ms.items()
        }
        per_config_sources[config_name] = candidate_sources
        per_config_e2e[config_name] = {
            "sample_count": len(e2e_ms),
            "p50_ms": _p_percentile(e2e_ms, 50),
            "p95_ms": _p_percentile(e2e_ms, 95),
        }

    # Temporal paired post-processing: for authored knowledge-update queries,
    # pair the +exact rank (pre) vs the +temporal rank (post) of the newest
    # relevant doc. Report rank_changed, top-1 winner transition
    # ((pre==1)!=(post==1)), and promoted_to_winner (post==1 while pre>1).
    temporal_pairs: dict[str, dict] = {}
    for qid in {r["query_id"] for r in per_config_results.get("+exact", [])}:
        pre = next(
            (r for r in per_config_results.get("+exact", []) if r["query_id"] == qid), None
        )
        post = next(
            (r for r in per_config_results.get("+temporal", []) if r["query_id"] == qid), None
        )
        if pre is None or post is None:
            continue
        pre_rank = pre.get("temporal_newest_rank")
        post_rank = post.get("temporal_newest_rank")
        if pre_rank is None and post_rank is None:
            continue
        temporal_pairs[qid] = {
            "pre_rank": pre_rank,
            "post_rank": post_rank,
            **_temporal_transition(pre_rank, post_rank),
        }

    return {
        "dev_ids_loaded": len(dev_ids),
        "smoke_query_ids": [r["query_id"] for r in smoke_rows],
        "configs": {name: _aggregate_smoke(name, per) for name, per in per_config_results.items()},
        "buckets": _aggregate_by_bucket(manifest_path, per_config_results),
        "stage_latency_ms": per_config_latency,
        "e2e_latency_ms": per_config_e2e,
        "candidate_sources": per_config_sources,
        "traces": {
            name: {r["query_id"]: r["trace"] for r in per}
            for name, per in per_config_results.items()
        },
        "temporal_pairs": temporal_pairs,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 4D development-split smoke runner")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--rows", required=True, type=Path)
    parser.add_argument("--dev-limit", type=int, default=5, help="number of development queries to smoke (1..72)")
    parser.add_argument("--query-ids", type=str, default=None, help="comma-separated development query ids to smoke (subset of 72)")
    parser.add_argument("--embedding-json", type=Path, default=None, help="local 72-dev query vectors (benchmark/.generated/)")
    parser.add_argument("--doc-id-map", type=Path, default=None, help="local uuid->doc-XXXX map (benchmark/.generated/doc_id_map.json)")
    parser.add_argument("--out", type=Path, default=None, help="write the smoke report JSON here")
    args = parser.parse_args(argv)

    report = run_smoke(
        manifest_path=args.manifest,
        rows_path=args.rows,
        dev_limit=args.dev_limit,
        embedding_json=args.embedding_json,
        doc_id_map=args.doc_id_map,
        query_ids=args.query_ids.split(",") if args.query_ids else None,
    )
    text = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
        print(f"wrote {args.out.name}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
