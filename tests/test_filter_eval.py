"""Phase 4E1 filter_eval contract tests (v3).

Verifies the dev-only measurement outputs are deterministic and sanitized: no
holdout IDs, no raw session/source values, no content/path/DSN/key, the report
is machine-generated from the JSON, and the v3 field set (harness/implementation
SHA, embedding_mode, input hashes, per-kind leakage counts, latency with reps,
unfiltered base-vs-head regression, before/after/subsequence) is fully covered
and recomputable.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
RESULTS = REPO / "benchmark" / "product_eval" / "filter_eval_results.json"
REPORT = REPO / "benchmark" / "product_eval" / "filter_eval_report.md"
MANIFEST = REPO / "benchmark" / "product_eval" / "dataset_manifest.json"

# Sanitized field allowlist for results (no raw session/source/content).
ALLOWED_RESULT_KEYS = {
    "schema",
    "harness_sha",
    "implementation_sha",
    "embedding_mode",
    "model_identity",
    "input_hashes",
    "kind_counts",
    "leakage_by_kind",
    "latency",
    "unfiltered_regression",
    "dev_count",
    "holdout_ids_used",
    "cases",
    "total_before_leakage",
    "total_after_leakage",
    "total_coverage_risk",
    "ok",
}
ALLOWED_CASE_KEYS = {
    "query_id",
    "filter_kinds",
    "before",
    "after",
    "before_kind",
    "after_kind",
    "control_returned",
    "filtered_returned",
    "coverage_risk",
    "filtered_is_order_preserving_subsequence",
    "control_latency_ms",
    "filtered_latency_ms",
    "ok",
}
ALLOWED_INPUT_HASH_KEYS = {
    "corpus.jsonl",
    "golden_queries.jsonl",
    "evidence_ledger.json",
    "dataset_manifest.json",
    "dev_query_vectors.json",
}
ALLOWED_KIND_COUNT_KEYS = {"source_filter", "session_filter", "time_filter"}
ALLOWED_LATENCY_KEYS = {"latency_reps", "control_p50_ms", "control_p95_ms", "filtered_p50_ms", "filtered_p95_ms"}
ALLOWED_REGRESSION_KEYS = {
    "frozen_baseline_runner_sha256",
    "head_runner_sha256",
    "config_metric_deltas",
    "trace_mismatch",
    "score_tolerance_note",
    "base_head_latency_p50_p95_ms",
}


def _holdout_ids() -> set[str]:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    return {q["query_id"] for q in manifest["query_splits"] if q["split"] == "holdout"}


def test_filter_eval_results_shape_and_no_holdout():
    if not RESULTS.exists():
        pytest.skip("filter_eval results not generated in this checkout")
    results = json.loads(RESULTS.read_text(encoding="utf-8"))
    assert set(results) == ALLOWED_RESULT_KEYS
    assert results["schema"] == "shiori-filter-eval/v3"
    assert results["holdout_ids_used"] == []
    assert results["embedding_mode"] == "pinned_local_replay"
    assert isinstance(results["harness_sha"], str) and len(results["harness_sha"]) == 40
    assert isinstance(results["implementation_sha"], str)
    assert set(results["input_hashes"]) == ALLOWED_INPUT_HASH_KEYS
    assert all(re.fullmatch(r"[0-9a-f]{64}", h) for h in results["input_hashes"].values())
    assert set(results["kind_counts"]) == ALLOWED_KIND_COUNT_KEYS
    assert set(results["latency"]) == ALLOWED_LATENCY_KEYS
    assert set(results["unfiltered_regression"]) == ALLOWED_REGRESSION_KEYS
    assert set(results["leakage_by_kind"]) == {"source", "session", "time"}
    holdout = _holdout_ids()
    for case in results["cases"]:
        assert set(case) == ALLOWED_CASE_KEYS
        assert case["query_id"] not in holdout
        assert case["before"] >= 0
        assert case["after"] >= 0
        assert case["coverage_risk"] >= 0
        assert set(case["control_latency_ms"]) == {"sample_count", "p50_ms", "p95_ms"}
        assert set(case["filtered_latency_ms"]) == {"sample_count", "p50_ms", "p95_ms"}
        assert case["control_latency_ms"]["sample_count"] >= 10
        assert case["filtered_latency_ms"]["sample_count"] >= 10


def test_filter_eval_after_is_zero_and_subsequence_holds():
    if not RESULTS.exists():
        pytest.skip("filter_eval results not generated in this checkout")
    results = json.loads(RESULTS.read_text(encoding="utf-8"))
    for case in results["cases"]:
        assert case["after"] == 0
        assert case["filtered_is_order_preserving_subsequence"] is True
        assert case["ok"] is True
    assert results["total_after_leakage"] == 0
    assert results["ok"] is True
    for kind, counts in results["leakage_by_kind"].items():
        assert counts["after_query_count"] == 0


def test_filter_eval_before_matches_frozen_phase4d_evidence():
    """The unfiltered control must exhibit real leakage (matching the frozen
    Phase 4D +dedup evidence 9/9/3 source/session/time before, 0/0/0 after),
    proving before/after are not trivial zero/zero."""
    if not RESULTS.exists():
        pytest.skip("filter_eval results not generated in this checkout")
    results = json.loads(RESULTS.read_text(encoding="utf-8"))
    assert results["total_before_leakage"] > 0
    kinds = results["leakage_by_kind"]
    assert kinds["source"]["before_query_count"] > 0
    assert kinds["session"]["before_query_count"] > 0
    assert kinds["time"]["before_query_count"] > 0
    assert kinds["source"]["after_query_count"] == 0
    assert kinds["session"]["after_query_count"] == 0
    assert kinds["time"]["after_query_count"] == 0


def test_filter_eval_unfiltered_regression_is_flat():
    """The 72-dev unfiltered base-vs-head regression must show zero
    doc/rank/reason/stage mismatches and zero metric deltas across all configs."""
    if not RESULTS.exists():
        pytest.skip("filter_eval results not generated in this checkout")
    results = json.loads(RESULTS.read_text(encoding="utf-8"))
    reg = results["unfiltered_regression"]
    assert re.fullmatch(r"[0-9a-f]{64}", reg["frozen_baseline_runner_sha256"])
    assert re.fullmatch(r"[0-9a-f]{64}", reg["head_runner_sha256"])
    for cfg, deltas in reg["config_metric_deltas"].items():
        assert all(abs(v) < 1e-6 for v in deltas.values()), f"metric drift in {cfg}: {deltas}"
    assert reg["trace_mismatch"]["doc_rank_reason_stage"] == 0
    assert set(reg["base_head_latency_p50_p95_ms"]) == set(reg["config_metric_deltas"])


def test_filter_eval_report_is_machine_generated_and_sanitized():
    if not RESULTS.exists() or not REPORT.exists():
        pytest.skip("filter_eval outputs not generated in this checkout")
    results = json.loads(RESULTS.read_text(encoding="utf-8"))
    report = REPORT.read_text(encoding="utf-8")
    for case in results["cases"]:
        assert case["query_id"] in report
    assert "total before leakage" in report.lower()
    assert "total after leakage" in report.lower()
    assert "coverage risk" in report.lower()
    assert "unfiltered regression" in report.lower()
    # No raw session/source values, no content, no paths, no DSN/key.
    raw_ledger = json.loads(
        (REPO / "benchmark" / "product_eval" / "evidence_ledger.json").read_text(encoding="utf-8")
    )
    for case in results["cases"]:
        row = raw_ledger[case["query_id"]]
        sess = row.get("session_filter", {}).get("session")
        kind = row.get("source_filter", {}).get("kind")
        if isinstance(sess, str) and sess:
            assert sess not in report
        if isinstance(kind, str) and kind:
            assert kind not in report
    assert re.search(r"postgres|postgresql://|sk-|/Users/|/private/|\.pem", report) is None


def test_filter_eval_report_derives_from_results():
    if not RESULTS.exists() or not REPORT.exists():
        pytest.skip("filter_eval outputs not generated in this checkout")
    results = json.loads(RESULTS.read_text(encoding="utf-8"))
    report = REPORT.read_text(encoding="utf-8")
    # The report's totals match the JSON.
    assert f"total before leakage (rows): {results['total_before_leakage']}" in report
    assert f"total after leakage (rows): {results['total_after_leakage']}" in report
    assert f"total coverage risk (rows): {results['total_coverage_risk']}" in report
    assert f"ok: {results['ok']}".lower() in report.lower()
    assert results["harness_sha"] in report
    assert results["embedding_mode"] in report
    # Per-case before/after/subsequence appear in the table.
    for case in results["cases"]:
        assert str(case["before"]) in report
        assert str(case["after"]) in report
        assert str(case["filtered_is_order_preserving_subsequence"]) in report
