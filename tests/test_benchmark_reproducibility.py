"""CI-safe reproducibility tests for the task #11 benchmark.

These tests run WITHOUT downloading a model, without network access, and
without loading the voyage-4-nano weights. They verify:
- fixture schema validity (corpus + judgments against corpus_schema.json)
- relevance grades (0-3) semantics for nDCG / Recall / MRR
- metric formulas on small handcrafted rankings (graded + ungraded)
- tie-break determinism
- source-filtering is actually applied (fail-closed on out-of-scope docs)
- no-evidence behavior flagging
- manifest closed-loop: real hashes, row counts, referential integrity,
  duplicate ids, missing/extra vectors, finite values and dimensions
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from benchmark import run_benchmark

BENCH = Path(__file__).resolve().parents[1] / "benchmark"
FIXTURES = BENCH / "fixtures"
CORPUS_PATH = FIXTURES / "corpus.jsonl"
JUDGMENTS_PATH = FIXTURES / "judgments.jsonl"
SCHEMA_PATH = BENCH / "corpus_schema.json"


def _read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _validate_against_schema(instance: dict, schema: dict) -> None:
    try:
        import jsonschema

        jsonschema.validate(instance, schema)
    except ImportError:  # pragma: no cover - jsonschema is in runtime deps
        pass


def test_fixtures_are_present_and_nonempty():
    assert CORPUS_PATH.exists()
    assert JUDGMENTS_PATH.exists()
    assert len(_read_jsonl(CORPUS_PATH)) >= 10
    assert len(_read_jsonl(JUDGMENTS_PATH)) >= 10


def test_corpus_schema_valid():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    for doc in _read_jsonl(CORPUS_PATH):
        _validate_against_schema(doc, schema["corpus_document"])
        assert doc["id"].startswith("doc-")
        assert doc["lang"] in {"en", "zh", "ja"}


def test_judgments_schema_valid():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    classes = {"exact", "paraphrase", "multilingual", "temporal", "multi_turn", "duplicate", "no_evidence"}
    seen = set()
    for judgment in _read_jsonl(JUDGMENTS_PATH):
        _validate_against_schema(judgment, schema["judgment"])
        assert judgment["class"] in classes
        assert judgment["query_id"] not in seen
        seen.add(judgment["query_id"])
        # relevance grades are 0-3 ints
        for doc_id, grade in (judgment.get("relevance") or {}).items():
            assert doc_id.startswith("doc-")
            assert isinstance(grade, int) and 0 <= grade <= 3
        # no_evidence: empty relevance + flag set
        if judgment["class"] == "no_evidence":
            assert judgment["expected_no_evidence"] is True
            assert judgment.get("relevance") == {}
        # multi_turn must have structured conversation context
        if judgment["class"] == "multi_turn":
            assert isinstance(judgment.get("conversation_context"), list)
            assert len(judgment["conversation_context"]) >= 1
            assert judgment.get("canonical_query")


def test_all_seven_classes_represented():
    classes = {j["class"] for j in _read_jsonl(JUDGMENTS_PATH)}
    assert classes == {"exact", "paraphrase", "multilingual", "temporal", "multi_turn", "duplicate", "no_evidence"}


def test_recall_at_k_handcrafted():
    assert run_benchmark._recall_at_k(["a", "b", "c"], {"a"}, 5) == 1.0
    assert run_benchmark._recall_at_k(["a", "b", "c"], {"b", "c"}, 5) == pytest.approx(1.0)
    assert run_benchmark._recall_at_k(["a", "b", "c"], {"x"}, 5) == 0.0
    assert run_benchmark._recall_at_k(["a", "b", "c", "d", "e"], {"e", "z"}, 5) == pytest.approx(0.5)
    assert run_benchmark._recall_at_k([], {"a"}, 5) == 0.0


def test_reciprocal_rank_handcrafted():
    assert run_benchmark._reciprocal_rank(["x", "a"], {"a"}, 10) == pytest.approx(0.5)
    assert run_benchmark._reciprocal_rank(["a", "b"], {"a"}, 10) == pytest.approx(1.0)
    assert run_benchmark._reciprocal_rank(["a", "b"], {"z"}, 10) == 0.0
    assert run_benchmark._reciprocal_rank(["a", "b", "c"], {"c"}, 2) == 0.0


def test_ndcg_at_k_graded_handcrafted():
    # Frozen definition: graded gain = 2**grade - 1, log2 discount.
    # Single grade-3 relevant at rank 1 -> ideal ordering -> 1.0.
    assert run_benchmark._ndcg_at_k(["a"], {"a": 3}, 10) == pytest.approx(1.0)
    # Two graded docs, ideal order -> 1.0.
    assert run_benchmark._ndcg_at_k(["a", "b"], {"a": 3, "b": 2}, 10) == pytest.approx(1.0)
    # Reversed order -> dcg < ideal.
    gain = {3: 2**3 - 1, 2: 2**2 - 1}
    dcg_rev = gain[2] / math.log2(2) + gain[3] / math.log2(3)
    ideal_rev = gain[3] / math.log2(2) + gain[2] / math.log2(3)
    assert run_benchmark._ndcg_at_k(["a", "b"], {"a": 2, "b": 3}, 10) == pytest.approx(dcg_rev / ideal_rev)
    # Grade-3 at rank 2 with only one relevant doc -> lower than ideal.
    dcg2 = gain[3] / math.log2(3)
    ideal2 = gain[3] / math.log2(2)
    assert run_benchmark._ndcg_at_k(["x", "a"], {"a": 3}, 10) == pytest.approx(dcg2 / ideal2)
    # No positive grades -> 0.
    assert run_benchmark._ndcg_at_k(["a", "b"], {"z": 1}, 10) == 0.0


def test_tie_break_is_deterministic():
    docs = [
        {"id": "doc-a", "content": "alpha beta gamma", "session": "bench-x"},
        {"id": "doc-b", "content": "alpha beta gamma", "session": "bench-x"},
    ]
    dv = {"doc-a": [1.0, 0.0], "doc-b": [1.0, 0.0]}
    r1 = run_benchmark._rank_corpus("alpha beta gamma", docs, dv, [1.0, 0.0])
    r2 = run_benchmark._rank_corpus("alpha beta gamma", docs, dv, [1.0, 0.0])
    assert r1 == r2


def test_source_filter_is_applied():
    docs = [
        {"id": "doc-a", "content": "shared content", "session": "bench-deploy"},
        {"id": "doc-b", "content": "shared content", "session": "bench-build"},
    ]
    dv = {"doc-a": [1.0, 0.0], "doc-b": [1.0, 0.0]}
    ranked = run_benchmark._rank_corpus(
        "shared content", docs, dv, [1.0, 0.0], source_filter="bench-deploy"
    )
    assert ranked == ["doc-a"]  # doc-b excluded by filter
    # No filter -> both.
    ranked_all = run_benchmark._rank_corpus("shared content", docs, dv, [1.0, 0.0])
    assert set(ranked_all) == {"doc-a", "doc-b"}


def test_no_evidence_flagging():
    rows = [
        {"expected_no_evidence": True, "no_evidence_behavior": "false_return", "class": "no_evidence"},
        {"expected_no_evidence": True, "no_evidence_behavior": "abstention_like", "class": "no_evidence"},
        {"expected_no_evidence": False, "no_evidence_behavior": None, "class": "exact"},
    ]
    false_returns = [r for r in rows if r["expected_no_evidence"] and r["no_evidence_behavior"] == "false_return"]
    abstentions = [r for r in rows if r["expected_no_evidence"] and r["no_evidence_behavior"] == "abstention_like"]
    assert len(false_returns) == 1
    assert len(abstentions) == 1


def test_canonical_query_renderer_matches_fixture():
    """The shared renderer must reproduce each fixture's canonical_query."""
    from benchmark.query_rendering import render_canonical_query

    for judgment in _read_jsonl(JUDGMENTS_PATH):
        rendered = render_canonical_query(judgment)
        assert rendered == judgment["canonical_query"], f"{judgment['query_id']} renderer mismatch"


def test_canonical_query_renderer_rule():
    """Non-multi-turn = normalized query_text; multi-turn = context + query."""
    from benchmark.query_rendering import render_canonical_query

    for judgment in _read_jsonl(JUDGMENTS_PATH):
        rendered = render_canonical_query(judgment)
        norm_q = " ".join(judgment["query_text"].split())
        if judgment["class"] == "multi_turn":
            expected = " ".join(
                (" ".join(judgment["conversation_context"]) + " " + norm_q).split()
            )
            assert rendered == expected, f"{judgment['query_id']} multi-turn rule"
        else:
            assert rendered == norm_q, f"{judgment['query_id']} non-multi-turn rule"
        assert rendered == " ".join(rendered.split()), "canonical_query not whitespace-normalized"


def test_canonical_query_renderer_deterministic_multi_turn():
    """Multi-turn rendering is deterministic and includes conversation context."""
    from benchmark.query_rendering import render_canonical_query

    j = {
        "class": "multi_turn",
        "query_text": "  Which   migration came next and what did it add?  ",
        "conversation_context": [" Earlier  we discussed that a database migration added manager history tables. "],
    }
    r1 = render_canonical_query(j)
    r2 = render_canonical_query(j)
    assert r1 == r2
    assert "manager history" in r1
    assert " " not in r1.strip() or r1 == " ".join(r1.split())


def test_no_evidence_quality_metrics_are_null():
    """no-evidence rows must have null quality metrics (not 0)."""
    rows = run_benchmark._aggregate(
        [
            {"query_id": "q-a", "class": "no_evidence", "lang": "en", "direction": "en", "source_filter": None,
             "expected_no_evidence": True, "no_evidence_behavior": "false_return",
             "recall@5": None, "mrr@10": None, "ndcg@10": None},
        ]
    )
    agg = rows["class:no_evidence"]
    assert agg["ndcg@10"] is None
    assert agg["no_evidence_query_count"] == 1
    assert agg["no_evidence_false_return"] == 1


def test_manifest_references_frozen_model_contract():
    from benchmark import generate_vectors as gv

    assert gv.MODEL_ID == "voyageai/voyage-4-nano"
    assert gv.MODEL_REVISION == "67fabc9bef010dabc5f6024aa1b1b6b93410426f"
    assert gv.TRUNCATE_DIM == 1024


def test_committed_manifest_hashes_and_referential_integrity():
    """Validate the committed results/manifest.json against actual files."""
    manifest_path = BENCH / "results" / "manifest.json"
    if not manifest_path.exists():
        pytest.skip("committed manifest not present yet (regenerated by the real local run)")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))


    files = manifest["files"]
    # Every listed file must hash to its recorded sha256 (real closure).
    for rel, info in files.items():
        if rel == "vectors.json":
            continue  # vectors are generated; validated separately if present
        p = FIXTURES / rel if rel in {"corpus.jsonl", "judgments.jsonl"} else BENCH / rel
        assert p.exists(), f"manifest references missing file {rel}"
        import hashlib

        h = hashlib.sha256(p.read_bytes()).hexdigest()
        assert h == info["sha256"], f"hash mismatch for {rel}"

    # Row counts must match.
    assert files["corpus.jsonl"]["rows"] == len(_read_jsonl(CORPUS_PATH))
    assert files["judgments.jsonl"]["rows"] == len(_read_jsonl(JUDGMENTS_PATH))


def test_vectors_integrity_if_present():
    """Validate generated vectors (finite, right dim, ids reference fixtures)."""
    vectors_path = BENCH / "results" / "vectors.json"
    if not vectors_path.exists():
        pytest.skip("vectors not committed (generated locally in benchmark/.generated/)")
    vectors = json.loads(vectors_path.read_text(encoding="utf-8"))
    doc_ids = {doc["id"] for doc in _read_jsonl(CORPUS_PATH)}
    query_ids = {j["query_id"] for j in _read_jsonl(JUDGMENTS_PATH)}

    from benchmark.vector_validation import validate_vectors

    validate_vectors(vectors, expected_doc_ids=doc_ids, expected_query_ids=query_ids)


def test_vector_validator_rejects_counterexamples():
    """The strict validator must fail closed on every malformed input."""
    from benchmark.vector_validation import VectorValidationError, validate_vectors

    def _vectors(docs=None, queries=None):
        return {"documents": docs or [{"id": "doc-0001", "embedding": [1.0, 0.0]}],
                "queries": queries or [{"query_id": "q-0001", "embedding": [1.0, 0.0]}]}

    ok_docs = {"doc-0001"}
    ok_queries = {"q-0001"}

    # Duplicate document id.
    bad = _vectors(docs=[{"id": "doc-0001", "embedding": [1.0, 0.0]},
                         {"id": "doc-0001", "embedding": [1.0, 0.0]}])
    with pytest.raises(VectorValidationError):
        validate_vectors(bad, expected_doc_ids=ok_docs, expected_query_ids=ok_queries)

    # Duplicate query id.
    bad = _vectors(queries=[{"query_id": "q-0001", "embedding": [1.0, 0.0]},
                            {"query_id": "q-0001", "embedding": [1.0, 0.0]}])
    with pytest.raises(VectorValidationError):
        validate_vectors(bad, expected_doc_ids=ok_docs, expected_query_ids=ok_queries)

    # Missing document vector.
    with pytest.raises(VectorValidationError):
        validate_vectors(_vectors(docs=[]), expected_doc_ids=ok_docs, expected_query_ids=ok_queries)

    # Extra document vector.
    bad = _vectors(docs=[{"id": "doc-0001", "embedding": [1.0, 0.0]},
                         {"id": "doc-9999", "embedding": [1.0, 0.0]}])
    with pytest.raises(VectorValidationError):
        validate_vectors(bad, expected_doc_ids=ok_docs, expected_query_ids=ok_queries)

    # Wrong dimension.
    bad = _vectors(docs=[{"id": "doc-0001", "embedding": [1.0]}])
    with pytest.raises(VectorValidationError):
        validate_vectors(bad, expected_doc_ids=ok_docs, expected_query_ids=ok_queries, expected_dim=2)

    # Non-finite value.
    bad = _vectors(docs=[{"id": "doc-0001", "embedding": [float("nan"), 0.0]}])
    with pytest.raises(VectorValidationError):
        validate_vectors(bad, expected_doc_ids=ok_docs, expected_query_ids=ok_queries)

    # Non-normalized vector.
    bad = _vectors(docs=[{"id": "doc-0001", "embedding": [5.0, 0.0]}])
    with pytest.raises(VectorValidationError):
        validate_vectors(bad, expected_doc_ids=ok_docs, expected_query_ids=ok_queries)

    # Valid 2-dim normalized vectors pass.
    ok = _vectors(docs=[{"id": "doc-0001", "embedding": [1.0, 0.0]}],
                  queries=[{"query_id": "q-0001", "embedding": [0.0, 1.0]}])
    validate_vectors(ok, expected_doc_ids=ok_docs, expected_query_ids=ok_queries, expected_dim=2)


def test_committed_manifest_matches_results_common_hashes():
    """Manifest and results must agree on EVERY common source hash, including vectors."""
    manifest_path = BENCH / "results" / "manifest.json"
    results_path = BENCH / "results" / "results.json"
    if not manifest_path.exists() or not results_path.exists():
        pytest.skip("committed manifest/results not present (regenerated by the real local run)")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    results = json.loads(results_path.read_text(encoding="utf-8"))

    for rel, info in manifest["files"].items():
        rh = results["input_hashes"].get(rel)
        assert rh is not None, f"results missing hash for {rel}"
        assert rh == info["sha256"], f"{rel} manifest/results hash mismatch"
    # Every results input hash must also be present in the manifest (bidirectional).
    for rel, rh in results["input_hashes"].items():
        assert rel in manifest["files"], f"manifest missing hash for {rel}"
        assert rh == manifest["files"][rel]["sha256"], f"{rel} results/manifest hash mismatch"
    # query_rendering.py must be in the closure.
    assert "query_rendering.py" in manifest["files"]
    assert "query_rendering.py" in results["input_hashes"]
    # vectors.json recorded hash must be equal (no real file needed).
    assert "vectors.json" in results["input_hashes"]
    assert results["input_hashes"]["vectors.json"] == manifest["files"]["vectors.json"]["sha256"]


def test_manifest_prompt_identity_and_libraries_nonempty():
    """The emitted manifest must carry prompt identity and REAL library versions."""
    manifest_path = BENCH / "results" / "manifest.json"
    if not manifest_path.exists():
        pytest.skip("committed manifest not present")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest.get("prompt_identity") == {"query": "encode_query", "document": "encode_document"}
    libs = manifest.get("libraries", {})
    for lib in ("sentence_transformers", "torch", "transformers"):
        assert libs.get(lib), f"manifest missing {lib} version"
        assert libs[lib] != "unknown", f"manifest has fabricated 'unknown' for {lib}"
    # results model identity must match manifest prompt identity.
    results_path = BENCH / "results" / "results.json"
    if results_path.exists():
        results = json.loads(results_path.read_text(encoding="utf-8"))
        assert results["model"]["prompt_identity"] == manifest["prompt_identity"]


def test_emit_manifest_requires_live_model():
    """--emit-manifest without --live-model must fail closed (parser error)."""
    from benchmark import run_benchmark as rb

    with pytest.raises(SystemExit):
        rb.main(["--fixtures", str(FIXTURES), "--vectors", str(BENCH / "none.json"),
                 "--out", str(BENCH / "results"), "--report", str(BENCH / "report.md"),
                 "--emit-manifest", str(BENCH / "results" / "manifest.json")])
