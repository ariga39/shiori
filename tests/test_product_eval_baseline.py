"""Offline contract tests for the Phase 4D baseline_72 deliverables (task #18).

These tests are CI-safe (no DB, no model, no network, no `.generated`). They
verify:
- the run manifest's committed-input hashes recompute from the actual files;
- local-run inputs are declared with committed=false + hash (not CI-recomputable);
- the report hash and the machine-generated report bytes match exactly;
- the results IDs are exactly the 72 development ids and disjoint from the 48
  holdout ids;
- every sanitized trace event passes the allowlist validator;
- a fresh-clone (`.generated` hidden) still lets offline CI tests run;
- results/report/manifest contain no content, local paths, DSNs, or keys;
- task #11 baseline files remain byte-stable.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PRODUCT_EVAL = REPO / "benchmark" / "product_eval"
RESULTS = PRODUCT_EVAL / "baseline_72_results.json"
RUN_MANIFEST = PRODUCT_EVAL / "baseline_72_manifest.json"
REPORT = PRODUCT_EVAL / "baseline_72_report.md"
DATASET_MANIFEST = PRODUCT_EVAL / "dataset_manifest.json"
GOLDEN_ROWS = PRODUCT_EVAL / "golden_queries.jsonl"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_manifest_committed_inputs_recompute_every_hash():
    """Every committed input hash must recompute from the repo file."""
    m = json.loads(RUN_MANIFEST.read_text(encoding="utf-8"))
    committed = m["committed_inputs_sha256"]
    assert len(committed) >= 7
    rel_map = {
        "golden_queries.jsonl": PRODUCT_EVAL / "golden_queries.jsonl",
        "dataset_manifest.json": PRODUCT_EVAL / "dataset_manifest.json",
        "evidence_ledger.json": PRODUCT_EVAL / "evidence_ledger.json",
        "dataset_manifest.schema.json": PRODUCT_EVAL / "dataset_manifest.schema.json",
        "fixtures/corpus.jsonl": REPO / "benchmark" / "fixtures" / "corpus.jsonl",
        "fixtures/judgments.jsonl": REPO / "benchmark" / "fixtures" / "judgments.jsonl",
        "corpus_schema.json": REPO / "benchmark" / "corpus_schema.json",
    }
    for rel, expected in committed.items():
        assert rel in rel_map, f"unexpected committed input {rel}"
        assert _sha256_bytes(rel_map[rel].read_bytes()) == expected, f"hash drift for {rel}"


def test_manifest_local_run_inputs_declared():
    """Local-run inputs must be declared with committed=false + hash + generator."""
    m = json.loads(RUN_MANIFEST.read_text(encoding="utf-8"))
    for rel, info in m["local_run_inputs"].items():
        assert info["committed"] is False
        assert len(info["sha256"]) == 64
        assert info.get("generator") and info.get("generator_version") and info.get("purpose")


def test_manifest_result_and_report_hashes():
    m = json.loads(RUN_MANIFEST.read_text(encoding="utf-8"))
    assert m["result_file_sha256"] == _sha256_bytes(RESULTS.read_bytes())
    assert m["report_file_sha256"] == _sha256_bytes(REPORT.read_bytes())


def test_report_bytes_equal_generator_output():
    """The committed report must byte-equal build_report._generate output."""
    from benchmark.product_eval.build_report import _generate

    results = json.loads(RESULTS.read_text(encoding="utf-8"))
    manifest = json.loads(RUN_MANIFEST.read_text(encoding="utf-8"))
    generated = _generate(results, manifest)
    assert generated == REPORT.read_text(encoding="utf-8")


def test_results_ids_exactly_72_dev_and_disjoint_holdout():
    splits = {s["query_id"]: s["split"] for s in json.loads(DATASET_MANIFEST.read_text(encoding="utf-8"))["query_splits"]}
    dev = {q for q, s in splits.items() if s == "tune"}
    holdout = {q for q, s in splits.items() if s == "holdout"}
    assert len(dev) == 72
    assert len(holdout) == 48
    results = json.loads(RESULTS.read_text(encoding="utf-8"))
    smoke = set(results["smoke_query_ids"])
    assert len(smoke) == 72
    assert smoke == dev
    assert smoke.isdisjoint(holdout)
    # Every config's traces keys must be exactly the 72 dev ids.
    for config_name, traces in results["traces"].items():
        assert set(traces.keys()) == dev, f"config {config_name} traces not exactly 72 dev"
    # temporal_pairs keys are a subset of dev.
    tp = set(results.get("temporal_pairs", {}).keys())
    assert tp.issubset(dev)
    # No holdout stable id anywhere in the results structure (walk).

    walk = json.dumps(results)
    holdout_q = [q for q in sorted(holdout)]
    for q in holdout_q:
        assert q not in walk, f"holdout id {q} appears in results"


def test_every_trace_event_passes_allowlist_validator():
    """Every sanitized trace event must pass the allowlist validator."""
    from benchmark.product_eval.trace import validate_trace_event

    results = json.loads(RESULTS.read_text(encoding="utf-8"))
    count = 0
    for config_name, traces in results["traces"].items():
        for qid, trace in traces.items():
            for event in trace:
                validate_trace_event({k: v for k, v in event.items() if v is not None})
                count += 1
    assert count > 0


def test_offline_contracts_run_without_generated(monkeypatch):
    """With .generated hidden/missing (fresh clone), the offline contract
    functions must still run: committed-input hash closure, report
    regeneration, trace validation, and dev-ID closure."""
    generated = REPO / "benchmark" / ".generated"
    hidden = REPO / "benchmark" / ".generated_hidden_for_test"
    if generated.exists():
        generated.rename(hidden)
    try:
        assert not generated.exists()
        # committed-input hash closure (recompute every committed hash).
        m = json.loads(RUN_MANIFEST.read_text(encoding="utf-8"))
        rel_map = {
            "golden_queries.jsonl": PRODUCT_EVAL / "golden_queries.jsonl",
            "dataset_manifest.json": PRODUCT_EVAL / "dataset_manifest.json",
            "evidence_ledger.json": PRODUCT_EVAL / "evidence_ledger.json",
            "dataset_manifest.schema.json": PRODUCT_EVAL / "dataset_manifest.schema.json",
            "fixtures/corpus.jsonl": REPO / "benchmark" / "fixtures" / "corpus.jsonl",
            "fixtures/judgments.jsonl": REPO / "benchmark" / "fixtures" / "judgments.jsonl",
            "corpus_schema.json": REPO / "benchmark" / "corpus_schema.json",
        }
        for rel, expected in m["committed_inputs_sha256"].items():
            assert _sha256_bytes(rel_map[rel].read_bytes()) == expected
        # report regeneration must byte-equal the committed report.
        from benchmark.product_eval.build_report import _generate

        results = json.loads(RESULTS.read_text(encoding="utf-8"))
        generated_report = _generate(results, m)
        assert generated_report == REPORT.read_text(encoding="utf-8")
        # trace validation must pass for every event.
        from benchmark.product_eval.trace import validate_trace_event

        for config_name, traces in results["traces"].items():
            for qid, trace in traces.items():
                for event in trace:
                    validate_trace_event({k: v for k, v in event.items() if v is not None})
        # dev-ID closure: 72 dev / 48 holdout disjoint, smoke == dev.
        splits = {s["query_id"]: s["split"] for s in json.loads(DATASET_MANIFEST.read_text(encoding="utf-8"))["query_splits"]}
        dev = {q for q, s in splits.items() if s == "tune"}
        holdout = {q for q, s in splits.items() if s == "holdout"}
        assert len(dev) == 72 and len(holdout) == 48
        assert set(results["smoke_query_ids"]) == dev
        assert set(results["smoke_query_ids"]).isdisjoint(holdout)
    finally:
        if hidden.exists():
            hidden.rename(generated)


def test_deliverables_have_no_secrets_or_paths():
    text = RESULTS.read_text(encoding="utf-8") + RUN_MANIFEST.read_text(encoding="utf-8") + REPORT.read_text(encoding="utf-8")
    forbidden = [
        r"/Users/", r"/home/", r"/root/", r"sk-live", r"AKIA", r"PRIVATE KEY",
        r"gh[pousr]_", r"@example", r"postgresql://", r"PGPASSWORD", r"SHIORI_DATABASE_DSN",
    ]
    for pat in forbidden:
        assert not re.search(pat, text), f"forbidden pattern {pat} in deliverables"
    for snippet in ("A database migration added", "构建流水线", "Only verified production releases"):
        assert snippet not in text, "content snippet leaked into deliverables"


def test_no_holdout_ids_in_deliverables():
    results = json.loads(RESULTS.read_text(encoding="utf-8"))
    manifest = json.loads(RUN_MANIFEST.read_text(encoding="utf-8"))
    splits = {s["query_id"]: s["split"] for s in json.loads(DATASET_MANIFEST.read_text(encoding="utf-8"))["query_splits"]}
    holdout = {q for q, s in splits.items() if s == "holdout"}
    assert set(manifest["dev_set"]["query_ids"]).isdisjoint(holdout)
    assert set(results["smoke_query_ids"]).isdisjoint(holdout)


def test_task11_baseline_byte_stable():
    for rel, expected in {
        "benchmark/fixtures/corpus.jsonl": "927584aa88a5a2c0223cce75ca001a5df75d5ac5689dfd64e598432de481de58",
        "benchmark/fixtures/judgments.jsonl": "acfc5aeaeaccb207ef2b18a74a9e325f09ed4cbfe41aa05f94959a7d380c005b",
        "benchmark/corpus_schema.json": "6c991bb0d3ceeb3db3500d8898a8cf24131a7a48575ffc128ad31093885bbd3d",
        "benchmark/run_benchmark.py": "46a1adac0db1ec7a715834724373b8f82220356409dba249a0c354bf95a8d117",
    }.items():
        assert _sha256_bytes((REPO / rel).read_bytes()) == expected, f"{rel} drifted"


# ── Phase 4E2 independent 72-dev measurement closure (task #29) ──────────────


P4E2_RESULTS = PRODUCT_EVAL / "phase4e2_72_results.json"
P4E2_MANIFEST = PRODUCT_EVAL / "phase4e2_72_manifest.json"
P4E2_REPORT = PRODUCT_EVAL / "phase4e2_72_report.md"
P4E2_RESULTS_SHA = "d37ce61fda0dcedcf835769a1b3e64fb3fb17ed60abc88e59ff743fd8849d28e"


def test_phase4e2_results_sha_and_ids():
    if not P4E2_RESULTS.exists():
        pytest.skip("phase4e2 results not committed")
    assert _sha256_bytes(P4E2_RESULTS.read_bytes()) == P4E2_RESULTS_SHA
    results = json.loads(P4E2_RESULTS.read_text(encoding="utf-8"))
    splits = {s["query_id"]: s["split"] for s in json.loads(DATASET_MANIFEST.read_text(encoding="utf-8"))["query_splits"]}
    dev = {q for q, s in splits.items() if s == "tune"}
    holdout = {q for q, s in splits.items() if s == "holdout"}
    assert len(results["smoke_query_ids"]) == 72
    assert set(results["smoke_query_ids"]) == dev
    assert set(results["smoke_query_ids"]).isdisjoint(holdout)
    for config_name, traces in results["traces"].items():
        assert set(traces.keys()) == dev, f"{config_name} traces not exactly 72 dev"


def test_phase4e2_manifest_closure():
    if not P4E2_MANIFEST.exists() or not P4E2_RESULTS.exists() or not P4E2_REPORT.exists():
        pytest.skip("phase4e2 deliverables not committed")
    m = json.loads(P4E2_MANIFEST.read_text(encoding="utf-8"))
    assert m["result_file_sha256"] == _sha256_bytes(P4E2_RESULTS.read_bytes())
    assert m["report_file_sha256"] == _sha256_bytes(P4E2_REPORT.read_bytes())
    assert m["dev_set"]["query_count"] == 72
    # Pinned 72-dev vectors match the frozen Phase 4D pin.
    assert m["local_run_inputs"]["dev_query_vectors.json"]["sha256"] == "629fa726ec353632a2a87a48b473ad0b59c2dd8f61a804746e2d9dd43c9287f2"
    # Report byte-equals the unchanged generator output (offline closure).
    from benchmark.product_eval.build_report import _generate

    results = json.loads(P4E2_RESULTS.read_text(encoding="utf-8"))
    generated = _generate(results, m)
    assert generated == P4E2_REPORT.read_text(encoding="utf-8")


def test_phase4e2_report_derives_from_results():
    if not P4E2_REPORT.exists() or not P4E2_RESULTS.exists():
        pytest.skip("phase4e2 deliverables not committed")
    results = json.loads(P4E2_RESULTS.read_text(encoding="utf-8"))
    report = P4E2_REPORT.read_text(encoding="utf-8")
    for cfg in results["configs"]:
        assert cfg in report
    assert "72 development queries" in report
    assert "Holdout (48) untouched" in report
