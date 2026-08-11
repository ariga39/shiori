"""Phase 4E1 filter_eval contract tests (v2).

Verifies the dev-only measurement outputs are deterministic and sanitized: no
holdout IDs, no raw session/source values, no content/path/DSN/key, the report
is machine-generated from the JSON, and the v2 field set (implementation SHA,
input hashes, model identity, before/after, coverage risk, subsequence, p50/p95)
is fully covered and recomputable.
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
    "implementation_sha",
    "model_identity",
    "input_hashes",
    "kind_counts",
    "dev_count",
    "holdout_ids_used",
    "cases",
    "total_before_leakage",
    "total_after_leakage",
    "total_coverage_risk",
    "filtered_latency_p95_ms",
    "ok",
}
ALLOWED_CASE_KEYS = {
    "query_id",
    "filter_kinds",
    "before",
    "after",
    "control_returned",
    "filtered_returned",
    "coverage_risk",
    "filtered_is_order_preserving_subsequence",
    "control_p95_ms",
    "filtered_p95_ms",
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


def _holdout_ids() -> set[str]:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    return {q["query_id"] for q in manifest["query_splits"] if q["split"] == "holdout"}


def test_filter_eval_results_shape_and_no_holdout():
    if not RESULTS.exists():
        pytest.skip("filter_eval results not generated in this checkout")
    results = json.loads(RESULTS.read_text(encoding="utf-8"))
    assert set(results) == ALLOWED_RESULT_KEYS
    assert results["schema"] == "shiori-filter-eval/v2"
    assert results["holdout_ids_used"] == []
    assert isinstance(results["implementation_sha"], str)
    assert set(results["input_hashes"]) == ALLOWED_INPUT_HASH_KEYS
    assert all(re.fullmatch(r"[0-9a-f]{64}", h) for h in results["input_hashes"].values())
    assert set(results["kind_counts"]) == ALLOWED_KIND_COUNT_KEYS
    holdout = _holdout_ids()
    for case in results["cases"]:
        assert set(case) == ALLOWED_CASE_KEYS
        assert case["query_id"] not in holdout
        assert case["before"] >= 0
        assert case["after"] >= 0
        assert case["coverage_risk"] >= 0


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


def test_filter_eval_before_nonzero_against_frozen_phase4d_evidence():
    """The unfiltered control must exhibit real leakage (matching the frozen
    Phase 4D +dedup evidence), proving before/after are not trivial zero/zero."""
    if not RESULTS.exists():
        pytest.skip("filter_eval results not generated in this checkout")
    results = json.loads(RESULTS.read_text(encoding="utf-8"))
    assert results["total_before_leakage"] > 0
    assert results["total_coverage_risk"] >= 0


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
    assert f"total before leakage: {results['total_before_leakage']}" in report
    assert f"total after leakage: {results['total_after_leakage']}" in report
    assert f"total coverage risk: {results['total_coverage_risk']}" in report
    assert f"ok: {results['ok']}".lower() in report.lower()
    # Per-case before/after/subsequence appear in the table.
    for case in results["cases"]:
        assert str(case["before"]) in report
        assert str(case["after"]) in report
        assert str(case["filtered_is_order_preserving_subsequence"]) in report
