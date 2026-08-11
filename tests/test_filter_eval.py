"""Phase 4E1 filter_eval contract tests.

Verifies the dev-only measurement outputs are deterministic and sanitized: no
holdout IDs, no raw session/source values, no content/path/DSN/key, and the
report is machine-generated from the JSON.
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
ALLOWED_RESULT_KEYS = {"schema", "base_sha", "dev_count", "holdout_ids_used", "cases", "total_leakage", "ok"}
ALLOWED_CASE_KEYS = {"query_id", "filter_kinds", "leakage", "returned", "ok"}


def _holdout_ids() -> set[str]:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    return {q["query_id"] for q in manifest["query_splits"] if q["split"] == "holdout"}


def test_filter_eval_results_shape_and_no_holdout():
    if not RESULTS.exists():
        pytest.skip("filter_eval results not generated in this checkout")
    results = json.loads(RESULTS.read_text(encoding="utf-8"))
    assert set(results) == ALLOWED_RESULT_KEYS
    assert results["holdout_ids_used"] == []
    holdout = _holdout_ids()
    for case in results["cases"]:
        assert set(case) == ALLOWED_CASE_KEYS
        assert case["query_id"] not in holdout
        assert case["leakage"] >= 0


def test_filter_eval_report_is_machine_generated_and_sanitized():
    if not RESULTS.exists() or not REPORT.exists():
        pytest.skip("filter_eval outputs not generated in this checkout")
    results = json.loads(RESULTS.read_text(encoding="utf-8"))
    report = REPORT.read_text(encoding="utf-8")
    for case in results["cases"]:
        assert case["query_id"] in report
    assert "total leakage" in report.lower()
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
    # The report's leakage total matches the JSON.
    assert f"total leakage: {results['total_leakage']}" in report
    assert f"ok: {results['ok']}".lower() in report.lower()
