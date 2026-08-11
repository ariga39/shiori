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
import subprocess
import sys
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
    # Report byte-equals the manifest-driven generator output (full offline
    # byte-equality closure).  The committed report must regenerate exactly.
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


# ── Phase 4E2 manifest-driven report metadata (genuine red, task #29) ────────


P4E2_REPORT_TITLE = "# Shiori Phase 4E2 Intent-Gated Temporal Decay Report (72 development queries)"
P4E2_REPORT_NOTES = [
    # Frozen literals for the report_notes contract.
    "q-0057: grade-3 doc-0011 drops from rank 3 to rank 12 with Recall@5=1/2 while grade-2 doc-0012 reaches rank 1; frozen decay formula risk on a composite latest query.",
    "q-0086: grade-2 doc-0021 moves from rank 3 to rank 4 and duplicate nDCG@10 drops 1.0 -> 0.997316; a deterministic minor regression, not tie/noise.",
    "source/session/time 9/9/3 is an unfiltered counterfactual trace metric, not a Phase 4E1 active-filter regression; active filters remain 0/0/0.",
]


def test_build_report_cli_honors_phase4e2_title_and_notes(tmp_path):
    """Behavior spec: the public `build_report` CLI must honor manifest-level
    `report_title`/`report_notes` — a Phase 4E2 H1 title and the three frozen
    notes replace the default Phase 4D baseline title and Known-gaps list, and
    the two stale claims must not appear."""

    if not P4E2_MANIFEST.exists() or not P4E2_RESULTS.exists():
        pytest.skip("phase4e2 deliverables not committed")
    manifest = json.loads(P4E2_MANIFEST.read_text(encoding="utf-8"))
    manifest["report_title"] = P4E2_REPORT_TITLE
    manifest["report_notes"] = P4E2_REPORT_NOTES
    tmp_manifest = tmp_path / "manifest.json"
    tmp_manifest.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    out_report = tmp_path / "report.md"

    proc = subprocess.run(
        [
            sys.executable, "-m", "benchmark.product_eval.build_report",
            "--results", str(P4E2_RESULTS),
            "--manifest", str(tmp_manifest),
            "--out", str(out_report),
        ],
        capture_output=True,
        text=True,
        cwd=REPO,
    )
    assert proc.returncode == 0, proc.stderr
    text = out_report.read_text(encoding="utf-8")

    # Phase 4E2 title is honored (as the H1 first line).
    assert text.startswith(P4E2_REPORT_TITLE + "\n")
    # Every frozen note appears verbatim.
    for note in P4E2_REPORT_NOTES:
        assert note in text
    # The two stale/incorrect Known-gaps claims are gone.
    assert "does not apply source/session/time filters" not in text
    assert "+temporal degrades the temporal and filter buckets" not in text


# ── Phase 4E3 formal results closure (genuine red, task #33) ────────────────


P4E3_RESULTS = PRODUCT_EVAL / "phase4e3_72_results.json"
P4E3_RESULTS_SHA = "91cf669144daef112309895324f17f23bc4063acc5c740d73ffcf451e02796a9"


def _p4e3_split_ids():
    splits = json.loads(DATASET_MANIFEST.read_text(encoding="utf-8"))["query_splits"]
    dev = {s["query_id"] for s in splits if s["split"] == "tune"}
    holdout = {s["query_id"] for s in splits if s["split"] == "holdout"}
    return dev, holdout


def test_phase4e3_results_and_dedup_contract():
    """Genuine-red closure for the committed Phase 4E3 results.

    The formal results file is NOT yet committed, so this test must fail on a
    missing file (no fallback to `.generated`).  Once present, it pins the
    exact SHA and verifies the provenance-aware dedup contract end to end.
    """
    if not P4E3_RESULTS.exists():
        pytest.fail(f"formal phase4e3 results missing at {P4E3_RESULTS}")

    assert _sha256_bytes(P4E3_RESULTS.read_bytes()) == P4E3_RESULTS_SHA
    results = json.loads(P4E3_RESULTS.read_text(encoding="utf-8"))

    # Exact 72 development ids, disjoint from the 48 holdout ids.
    dev, holdout = _p4e3_split_ids()
    assert len(results["smoke_query_ids"]) == 72
    assert set(results["smoke_query_ids"]) == dev
    assert set(results["smoke_query_ids"]).isdisjoint(holdout)

    # First-five configs: configs/buckets/candidate_sources/
    # filter_leakage_by_tag/temporal_pairs equal the committed Phase 4E2.
    p4e2 = json.loads(P4E2_RESULTS.read_text(encoding="utf-8"))
    first_five = ["dense-only", "lexical-only", "rrf", "+exact", "+temporal"]
    for name in first_five:
        assert results["configs"][name] == p4e2["configs"][name], f"config drift: {name}"
        assert results["buckets"][name] == p4e2["buckets"][name], f"bucket drift: {name}"
        assert results["candidate_sources"][name] == p4e2["candidate_sources"][name], f"source drift: {name}"
        assert results["filter_leakage_by_tag"][name] == p4e2["filter_leakage_by_tag"][name], f"leakage drift: {name}"
        assert results["temporal_pairs"] == p4e2["temporal_pairs"], "temporal_pairs drift"

    # Trace equivalence ignores latency and time-dependent scores: project each
    # trace event to (stage, doc_id, rank, reason) only.
    for name in first_five:
        o = {
            qid: [(e["stage"], e["doc_id"], e["rank"], e["reason"]) for e in evs]
            for qid, evs in p4e2["traces"][name].items()
        }
        n = {
            qid: [(e["stage"], e["doc_id"], e["rank"], e["reason"]) for e in evs]
            for qid, evs in results["traces"][name].items()
        }
        assert n == o, f"trace drift: {name}"

    # +dedup fixed observable literals (frozen from the measured run).
    d = results["configs"]["+dedup"]
    assert d["final_recall@5"] == 0.9603174603174603
    assert d["final_mrr@10"] == 0.9497354497354498
    assert d["final_ndcg@10"] == 0.9101678917360182
    assert d["coverage_risk_dropped_relevant"] == 5
    assert d["dedup_drop_rate"] == 0.05339105339105339
    assert d["duplicate_group_coverage"] == 1.0
    assert d["no_evidence_false_return"] == 9
    assert d["filter_leakage"] == 9
    assert results["filter_leakage_by_tag"]["+dedup"] == {
        "source_filter": 9,
        "session_filter": 9,
        "time_filter": 3,
    }

    # Recovered distinct evidence (from the public sanitized +dedup trace).
    dedup_trace = results["traces"]["+dedup"]
    recovered = {
        (qid, e["doc_id"])
        for qid, evs in dedup_trace.items()
        for e in evs
        if e.get("reason") == "mmr_keep"
    }
    for qid, doc in [
        ("q-0009", "doc-0016"),
        ("q-0024", "doc-0015"),
        ("q-0055", "doc-0015"),
        ("q-0056", "doc-0016"),
        ("q-0074", "doc-0016"),
        ("q-0117", "doc-0015"),
        ("q-0039", "doc-0002"),
    ]:
        assert (qid, doc) in recovered, f"expected recovered keep {(qid, doc)}"

    # True duplicates still fold, with the byte-identical representative kept.
    dropped = {
        (qid, e["doc_id"])
        for qid, evs in dedup_trace.items()
        for e in evs
        if e.get("reason") == "mmr_dedup"
    }
    for qid in ("q-0041", "q-0042", "q-0073", "q-0086", "q-0116"):
        assert (qid, "doc-0017") in dropped, f"expected true-duplicate drop {qid}/doc-0017"
        assert (qid, "doc-0018") in recovered, f"expected representative keep {qid}/doc-0018"
    assert ("q-0042", "doc-0019") in recovered, "expected q-0042 zh representative doc-0019 keep"
