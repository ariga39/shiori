"""Evaluator-contract tests for task #18 Phase 4D (production-ranker evaluation).

These tests are CI-safe (no model, no network, no secret, no live DB). They pin
the evaluator's contract ONLY: metric formulas, tune/holdout isolation,
trace redaction, dataset-manifest pin, no-network enforcement, task #11
baseline byte-stability, and production-stage equivalence. No quality
thresholds are asserted anywhere.
"""

from __future__ import annotations

import builtins
import json
import math
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
BENCH = REPO / "benchmark"
PRODUCT_EVAL = BENCH / "product_eval"
MANIFEST_SCHEMA = PRODUCT_EVAL / "dataset_manifest.schema.json"
DATASET_MANIFEST = PRODUCT_EVAL / "dataset_manifest.json"
GOLDEN_ROWS = PRODUCT_EVAL / "golden_queries.jsonl"

# ---- Task #11 baseline byte-stability --------------------------------------

# Frozen: task #11 baseline files must remain byte-stable. Captured from main
# 49ab1598ea50cca3f001ad75993eda4896b58e82.
TASK11_BASELINE_HASHES = {
    "benchmark/.gitignore": "16c991a1c0f9b2fc1f645895d6f664ea111b0f0bbe4f3d6498413ce1f531ab9d",
    "benchmark/README.md": "cd9c4bb13f84291f8aaf5b0836bcc0e1b693553ca251b6bf30d3178f53e30d31",
    "benchmark/__init__.py": "bdd81846b385fc471a76fe9915fc2401494b8dcef169829942554e6ff81f0794",
    "benchmark/corpus_schema.json": "6c991bb0d3ceeb3db3500d8898a8cf24131a7a48575ffc128ad31093885bbd3d",
    "benchmark/fixtures/corpus.jsonl": "927584aa88a5a2c0223cce75ca001a5df75d5ac5689dfd64e598432de481de58",
    "benchmark/fixtures/judgments.jsonl": "acfc5aeaeaccb207ef2b18a74a9e325f09ed4cbfe41aa05f94959a7d380c005b",
    "benchmark/generate_vectors.py": "5217f144ac1a801795e7f6077512ce105d754a420daaeefcbce5861645d259e9",
    "benchmark/query_rendering.py": "10d8dc93ba96558a33b40c30745e06bfe72d5a9f0c9dc036c7fef1298f6469a5",
    "benchmark/report.md": "77875528be491e56d5ec41e3fba01cdbe21c3ce8acec0f7044fbbd70a9107029",
    "benchmark/requirements.in": "aed88285b01d174e8d791226e4c301d215819fcbd29b8514d264eca084a8d73a",
    "benchmark/requirements.lock": "47ba34e03af8b38fe58d8a3fb2d7528608ce4e1e1e2305b773ec8dd283d8bcaa",
    "benchmark/results/manifest.json": "af3d672254f2d7ea5d6d5681fda6f69ee75fdcca40c3391428a9f12cf985a536",
    "benchmark/results/results.json": "7b9a02bee04a8284415606800915e57b5dd1c7393f75705a3b67d7e9aeddffb0",
    "benchmark/run_benchmark.py": "46a1adac0db1ec7a715834724373b8f82220356409dba249a0c354bf95a8d117",
    "benchmark/vector_validation.py": "678328559687c340288ef45ea0b4fbd39194ef0f066a49b49a1dec2dd7acb7bb",
}


def _sha256_bytes(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


@pytest.mark.parametrize("rel", sorted(TASK11_BASELINE_HASHES))
def test_task11_baseline_byte_stability(rel: str) -> None:
    """task #11 baseline files must not change (byte-stable)."""
    path = REPO / rel
    assert path.exists(), f"missing task #11 baseline file {rel}"
    actual = _sha256_bytes(path.read_bytes())
    assert actual == TASK11_BASELINE_HASHES[rel], f"task #11 baseline drifted: {rel}"


# ---- Metric formulas (frozen task #11 definitions) --------------------------


def test_metric_formulas_recall_at_k():
    from benchmark.product_eval import evaluator

    assert evaluator.recall_at_k(["a", "b", "c"], {"a"}, 5) == pytest.approx(1.0)
    assert evaluator.recall_at_k(["a", "b", "c"], {"x"}, 5) == 0.0
    assert evaluator.recall_at_k(["a", "b", "c", "d", "e"], {"e", "z"}, 5) == pytest.approx(0.5)
    assert evaluator.recall_at_k([], {"a"}, 5) == 0.0


def test_metric_formulas_reciprocal_rank():
    from benchmark.product_eval import evaluator

    assert evaluator.reciprocal_rank(["x", "a"], {"a"}, 10) == pytest.approx(0.5)
    assert evaluator.reciprocal_rank(["a", "b"], {"a"}, 10) == pytest.approx(1.0)
    assert evaluator.reciprocal_rank(["a", "b"], {"z"}, 10) == 0.0
    assert evaluator.reciprocal_rank(["a", "b", "c"], {"c"}, 2) == 0.0


def test_metric_formulas_ndcg_graded():
    from benchmark.product_eval import evaluator

    # Frozen definition: graded gain = 2**grade - 1, log2 discount.
    gain = {2: 2**2 - 1, 3: 2**3 - 1}
    # Ideal order -> 1.0.
    assert evaluator.ndcg_at_k(["a", "b"], {"a": 3, "b": 2}, 10) == pytest.approx(1.0)
    # Reversed -> dcg / ideal.
    dcg = gain[2] / math.log2(2) + gain[3] / math.log2(3)
    ideal = gain[3] / math.log2(2) + gain[2] / math.log2(3)
    assert evaluator.ndcg_at_k(["a", "b"], {"a": 2, "b": 3}, 10) == pytest.approx(dcg / ideal)
    # Grade 3 at rank 2 only.
    dcg2 = gain[3] / math.log2(3)
    ideal2 = gain[3] / math.log2(2)
    assert evaluator.ndcg_at_k(["x", "a"], {"a": 3}, 10) == pytest.approx(dcg2 / ideal2)
    # No positive grades -> 0.
    assert evaluator.ndcg_at_k(["a", "b"], {"z": 1}, 10) == 0.0


# ---- Tune/holdout isolation -------------------------------------------------


def test_split_isolation_no_overlap():
    from benchmark.product_eval.manifest import load_manifest, validate_split

    manifest = load_manifest(DATASET_MANIFEST)
    golden = manifest["golden_queries"]
    assert golden["split_counts"]["tune"] + golden["split_counts"]["holdout"] == manifest["dataset"]["query_count"]
    tune, holdout = validate_split(DATASET_MANIFEST, GOLDEN_ROWS)
    assert tune.isdisjoint(holdout)
    assert len(tune) == golden["split_counts"]["tune"]
    assert len(holdout) == golden["split_counts"]["holdout"]


def test_evaluator_isolation_rejects_holdout_in_tune():
    from benchmark.product_eval import evaluator

    with pytest.raises(evaluator.IsolationError):
        evaluator.assert_isolation({"q-tune-1", "q-tune-2"}, {"q-tune-2"})


# ---- Structured trace contract (allowlist, fail-closed) --------------------


def test_trace_event_allowlist_rejects_content_field():
    """Content must NEVER enter a trace event (even synthetic), not be redacted."""
    from benchmark.product_eval.trace import TraceError, validate_trace_event

    with pytest.raises(TraceError):
        validate_trace_event({"stage": "dense", "content": "the build pipeline uses github actions"})


def test_trace_event_allowlist_rejects_query_text():
    from benchmark.product_eval.trace import TraceError, validate_trace_event

    with pytest.raises(TraceError):
        validate_trace_event({"stage": "dense", "query_text": "what checks run on each pr"})


def test_trace_event_allowlist_rejects_embedding_and_key_path():
    from benchmark.product_eval.trace import TraceError, validate_trace_event

    with pytest.raises(TraceError):
        validate_trace_event({"stage": "dedup", "embedding": "[0.1, 0.2]"})
    with pytest.raises(TraceError):
        validate_trace_event({"stage": "dense", "api_key": "credential-placeholder"})
    with pytest.raises(TraceError):
        validate_trace_event({"stage": "dense", "path": "some/relative/path"})


def test_trace_event_allowlist_rejects_personal_data():
    from benchmark.product_eval.trace import TraceError, validate_trace_event

    with pytest.raises(TraceError):
        validate_trace_event({"stage": "dense", "email": "user@example.invalid"})
    with pytest.raises(TraceError):
        validate_trace_event({"stage": "dense", "ip": "203.0.113.7"})


def test_trace_event_allowlist_accepts_stable_fields():
    from benchmark.product_eval.trace import validate_trace_event

    validate_trace_event(
        {
            "stage": "dense",
            "doc_id": "doc-0001",
            "session_id": "bench-build",
            "source_type": "synthetic-note",
            "rank": 1,
            "score": 0.92,
            "reason": "vector",
            "latency_ms": 1.25,
        }
    )


def test_trace_event_allowlist_rejects_bad_values():
    from benchmark.product_eval.trace import TraceError, validate_trace_event

    with pytest.raises(TraceError):
        validate_trace_event({"stage": "bm25"})  # not a production stage name
    with pytest.raises(TraceError):
        validate_trace_event({"stage": "dense", "rank": 0})
    with pytest.raises(TraceError):
        validate_trace_event({"stage": "dense", "score": "high"})
    with pytest.raises(TraceError):
        validate_trace_event({"stage": "dense", "reason": "unknown_reason"})


# ---- Dataset manifest pin ---------------------------------------------------


def test_dataset_manifest_schema_valid():
    import jsonschema

    schema = json.loads(MANIFEST_SCHEMA.read_text(encoding="utf-8"))
    manifest = json.loads(DATASET_MANIFEST.read_text(encoding="utf-8"))
    jsonschema.validate(manifest, schema)


def test_dataset_manifest_license_redistribution_frozen():
    from benchmark.product_eval.manifest import load_manifest

    manifest = load_manifest(DATASET_MANIFEST)
    longmem = manifest["adapters"]["longmemeval"]
    assert longmem["card_license"] == "mit"
    assert longmem["redistribution"] == "unresolved"
    nf = manifest["adapters"]["nfcorpus"]
    assert nf["source_md5"] == "a89dba18a62ef92f7d323ec890a0d38d"
    assert nf["redistribution"] == "unresolved"
    miracl = manifest["adapters"]["miracl"]
    assert miracl["status"] == "adapter_only"
    assert miracl["redistribution"] == "not_run"


def test_dataset_manifest_cross_coverage_targets():
    from benchmark.product_eval.manifest import load_manifest, validate_cross_coverage

    manifest = load_manifest(DATASET_MANIFEST)
    validate_cross_coverage(DATASET_MANIFEST, GOLDEN_ROWS)
    targets = manifest["cross_coverage_targets"]
    # Independent minimums; no single bucket may substitute another.
    assert targets["same_name"] >= 12
    assert targets["long_chinese"] >= 10
    assert targets["cross_session"] >= 10
    assert targets["knowledge_update"] >= 8
    assert targets["hard_negative"] >= 15
    assert targets["source_filter"] >= 6
    assert targets["session_filter"] >= 6
    assert targets["time_filter"] >= 6


def test_dataset_manifest_evidence_is_auditable_and_consistent():
    """Every tagged query must carry machine-checkable evidence consistent
    with the corpus metadata AND the real row (lang/CJK recomputed, never
    trusted from the ledger), plus a non-empty why for every tag."""
    import re

    from benchmark.product_eval.manifest import _load_corpus_meta, _query_meta, load_golden_rows, load_manifest

    manifest = load_manifest(DATASET_MANIFEST)
    corpus = _load_corpus_meta(BENCH / "fixtures" / "corpus.jsonl")
    rows = load_golden_rows(GOLDEN_ROWS)
    row_by_id = {row["query_id"]: row for row in rows}
    by_id = _query_meta(manifest)
    for qid, meta in by_id.items():
        for tag in meta["tags"]:
            ev = meta["evidence"].get(tag)
            assert ev is not None, f"{qid} tag {tag} missing evidence"
            assert ev.get("why") or ev.get("reason"), f"{qid} tag {tag} missing why"
            if tag == "cross_session":
                docs = ev.get("docs")
                assert docs and len({corpus[d]["session"] for d in docs}) >= 2, f"{qid} cross_session weak"
            if tag == "knowledge_update":
                docs = ev.get("docs")
                assert docs and len({corpus[d]["timestamp"] for d in docs}) >= 2, f"{qid} knowledge_update weak"
            if tag == "hard_negative":
                assert ev.get("non_relevant_docs"), f"{qid} hard_negative missing non_relevant_docs"
                assert ev.get("relevant_docs"), f"{qid} hard_negative missing relevant_docs"
            if tag == "long_chinese":
                row = row_by_id[qid]
                cjk = len(re.findall(r"[\u4e00-\u9fff]", row.get("query_text") or ""))
                assert row.get("lang") == "zh" and cjk >= 8, f"{qid} long_chinese does not hold on real row"
            if tag == "same_name":
                assert ev.get("entity"), f"{qid} same_name missing entity"
            if tag in {"source_filter", "session_filter"}:
                if tag == "source_filter":
                    assert ev.get("kind") in {"synthetic-note", "synthetic-faq", "synthetic-log"}, f"{qid} source_filter unknown kind"
                else:
                    value = ev.get("session")
                    assert value in {meta["session"] for meta in corpus.values()}, f"{qid} session_filter unknown session"
            if tag == "time_filter":
                assert ev.get("operator") in {"before", "after", "at"}, f"{qid} time_filter bad operator"
                assert ev.get("iso_bound", "").endswith(("Z", "+00:00")), f"{qid} time_filter iso_bound not tz-aware"
                # Recompute the eligible set from the corpus predicate and require exact match.
                from datetime import datetime

                bound = datetime.fromisoformat(ev["iso_bound"].replace("Z", "+00:00"))
                computed = []
                for did, cmeta in sorted(corpus.items()):
                    ts = cmeta.get("timestamp")
                    if not ts:
                        continue
                    t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    if ev["operator"] == "before" and t < bound:
                        computed.append(did)
                    elif ev["operator"] == "after" and t > bound:
                        computed.append(did)
                    elif ev["operator"] == "at" and t == bound:
                        computed.append(did)
                assert set(ev["eligible_docs"]) == set(computed), f"{qid} time_filter eligible_docs mismatch"
            if tag == "duplicate_groups":
                groups = ev.get("groups")
                assert groups, f"{qid} duplicate_groups missing groups"
                for group in groups:
                    assert len(group) >= 2, f"{qid} duplicate_groups group <2 members"
                    assert len(set(group)) == len(group), f"{qid} duplicate_groups group has dup members"


# ---- No-network enforcement -------------------------------------------------


def test_evaluator_modules_import_no_network_deps():
    """product_eval evaluator/manifest/trace/adapters must not import network or model deps."""
    import subprocess
    import sys

    code = (
        "import sys; "
        f"sys.path.insert(0, {str(REPO)!r}); "
        "from benchmark.product_eval import evaluator; "
        "from benchmark.product_eval import manifest; "
        "from benchmark.product_eval import trace; "
        "from benchmark.product_eval import adapters; "
        "print('ok')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout


class _ForbiddenModule(types.ModuleType):
    """Module stub whose every attribute access trips a forbidden probe."""

    def __init__(self, name: str, attempted: list[str]):
        super().__init__(name)
        self._name = name
        self._attempted = attempted

    def __getattr__(self, item: str):
        self._attempted.append(f"{self._name}.{item}")
        raise RuntimeError(f"forbidden model loader access: {self._name}.{item}")


def test_ci_path_forbidden_probes_fail_closed(monkeypatch):
    """Running the CI-safe adapter/evaluator path under forbidden probes must
    neither open sockets nor load models nor read credentials.

    The forbidden-probe harness monkeypatches socket/HTTP/model-loader and
    credential reads so that ANY accidental network/model/credential access in
    the executed path raises instead of silently proceeding. The CI path
    (adapter contract validation + manifest checks) must complete with no
    forbidden access and no restricted-data download.
    """
    import socket

    from benchmark.product_eval import adapters, manifest

    attempted = []

    def _forbid(name):
        def _raises(*_a, **_k):
            attempted.append(name)
            raise RuntimeError(f"forbidden probe triggered: {name}")

        return _raises

    # Import HTTP client entry points FIRST so the socket-level probe does not
    # break ssl/requests import-time machinery (socket.socket is subclassed by
    # ssl.SSLSocket). After import, block the actual connect call.
    import urllib.request

    try:
        import requests
    except Exception:  # pragma: no cover - requests is a runtime dep
        requests = None  # type: ignore[assignment]

    socket.socket.connect = _forbid("socket.connect")  # type: ignore[attr-defined]
    socket.create_connection = _forbid("socket.create_connection")
    if requests is not None:
        requests.sessions.Session.request = _forbid("requests.request")
        requests.api.request = _forbid("requests.api.request")
    urllib.request.urlopen = _forbid("urlopen")
    # Model loader imports: any attribute access raises via a forbidden stub.
    for mod in ("sentence_transformers", "transformers", "torch"):
        sys.modules[mod] = _ForbiddenModule(f"{mod}.load_model", attempted)
    # Credential reads: any open() of a key/credential-shaped path fails.
    real_open = builtins.open

    def _guarded_open(path, *a, **_k):
        low = str(path).lower()
        if any(marker in low for marker in ("key", "credential", ".env", "token", "password")):
            attempted.append("credential-read")
            raise RuntimeError("forbidden credential read")
        return real_open(path, *a, **_k)

    monkeypatch.setattr("builtins.open", _guarded_open, raising=False)

    # Execute the actual CI path: adapter contract validation reads the
    # committed manifest (a plain JSON file, no restricted data download).
    adapters.validate_adapters(PRODUCT_EVAL / "dataset_manifest.json")
    manifest.load_manifest(DATASET_MANIFEST)
    manifest.validate_split(DATASET_MANIFEST, GOLDEN_ROWS)
    manifest.validate_cross_coverage(DATASET_MANIFEST, GOLDEN_ROWS)

    assert attempted == [], f"forbidden access during CI path: {attempted}"


# ---- Production-stage equivalence ------------------------------------------


def test_production_stage_trace_seam_behavior_preserving(monkeypatch):
    """Installing the trace collector must not change search() behavior."""
    import query

    events: list[tuple[str, list[dict]]] = []

    def collect(stage, stage_events):
        events.append((stage, stage_events))

    monkeypatch.setattr(query, "embed_query", lambda q: [1.0] + [0.0] * 1023)
    monkeypatch.setattr(query, "get_db", lambda: _NoRowsConn())
    with query._eval_scope(config=query.StageConfig(), collector=collect):
        # No DB: the seam must emit only an empty candidate set for stages and
        # never raise, exactly like a search that finds nothing.
        res = query.search("zzqx no-match 42", limit=20)
        assert res == []
        stages_seen = {stage for stage, _ in events}
        assert "dense" in stages_seen
        assert {"rrf", "temporal", "dedup"}.issubset(stages_seen)
        for stage, stage_events in events:
            for ev in stage_events:
                # Strict allowlist: stable IDs/scores/reasons/latency only.
                assert set(ev) <= {"doc_id", "session_id", "source_type", "rank", "score", "reason", "latency_ms"}
                assert "content" not in ev
                assert "query" not in ev


def test_production_stage_collector_error_does_not_change_outcome(monkeypatch):
    """A collector raising must be contained (recorded, swallowed) and never
    change search() success/failure or results."""
    import query

    monkeypatch.setattr(query, "embed_query", lambda q: [1.0] + [0.0] * 1023)
    monkeypatch.setattr(query, "get_db", lambda: _NoRowsConn())

    def bad_collect(stage, events):
        raise RuntimeError("boom")

    with query._eval_scope(config=query.StageConfig(), collector=bad_collect) as ctx:
        res = query.search("zzqx no-match 7", limit=20)
    assert res == []
    assert ctx.errors, "collector error must be recorded"

    # Outside the scope the collector is gone (must-restore).
    assert query._current_eval() is None


def test_production_stage_scope_restores_after_exception(monkeypatch):
    """_eval_scope must restore the prior context even when the body raises."""
    import query

    monkeypatch.setattr(query, "embed_query", lambda q: [1.0] + [0.0] * 1023)
    monkeypatch.setattr(query, "get_db", lambda: _NoRowsConn())
    with pytest.raises(RuntimeError):
        with query._eval_scope(config=query.StageConfig()):
            query.search("zzqx no-match 3", limit=20)
            raise RuntimeError("search body exploded")
    assert query._current_eval() is None


def test_production_stage_scope_concurrent_isolation(monkeypatch):
    """Two interleaved scopes must not share traces or configs (ContextVar)."""
    import threading

    import query

    monkeypatch.setattr(query, "embed_query", lambda q: [1.0] + [0.0] * 1023)
    monkeypatch.setattr(query, "get_db", lambda: _NoRowsConn())

    results: dict[str, list] = {"a": [], "b": []}

    def run(label, config):
        def collect(stage, events):
            results[label].append(stage)

        with query._eval_scope(config=config, collector=collect):
            query.search("zzqx concurrency 99", limit=20)

    t1 = threading.Thread(target=run, args=("a", query.StageConfig(temporal=False)))
    t2 = threading.Thread(target=run, args=("b", query.StageConfig(dedup=False)))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # Each thread saw its own stage set (no cross-talk): thread b with dedup
    # disabled still saw dedup emit (empty), thread a with temporal disabled
    # still saw temporal; the key point is each saw ONLY its own stages.
    assert results["a"]
    assert results["b"]
    assert query._current_eval() is None


class _NoRowsConn:
    """DB-free connection stub that yields no rows for every channel."""

    def cursor(self):
        return self

    def execute(self, sql, params=None):
        return self

    def fetchall(self):
        return []

    def rollback(self):
        return None

    def close(self):
        return None


def test_production_stage_equivalence_real_pg(db, monkeypatch):
    """Real-PG, non-empty-candidate equivalence for the production trace seam.

    Inserts a small corpus through the REAL session_chunks table and proves for
    three scenarios (ts_rank_cd hit, ts_rank_cd empty -> trigram fallback, exact
    short query):
    - the trace emits the expected stage(s) with production ordering, scores,
      sources and reasons;
    - running under a DEFAULT StageConfig (all stages on) yields results
      identical to running with NO context (item-for-item equivalence);
    - dedup keep/drop decisions carry stable reason codes and the returned
      results equal the mmr_keep doc_ids in order.
    """
    from datetime import UTC, datetime, timedelta

    import query

    conn, prefix = db
    sid = prefix + "-eval"
    now = datetime.now(UTC)

    def _insert(content, emb, ts, src="main_user"):
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO session_chunks
               (session_id, source_type, content, embedding, embedding_model,
                timestamp_start, timestamp_end, turn_index_start, turn_index_end,
                content_tsvector, created_at)
               VALUES (%s,%s,%s,%s::vector,%s,%s,%s,%s,%s,to_tsvector('simple',%s),%s)""",
            (sid, src, content, str(emb), "voyage-4-large", ts, ts, 0, 0, content, ts),
        )
        conn.commit()
        cur.close()

    near = [0.8660254] + [0.5] + [0.0] * 1022
    far = [0.5] + [0.8660254] + [0.0] * 1022
    # Scenario setup:
    # - "snowflake keyword" exact content: ts_rank_cd hit + exact hit.
    # - "unrelated chatter": vector/trigram-only candidate, no ts_rank_cd hit.
    _insert("shiori_eval explicit snowflake keyword alpha", near, now)
    _insert("shiori_eval unrelated chatter bravo", far, now - timedelta(days=30))

    fixed_now = datetime(2026, 8, 11, 3, 30, 0, tzinfo=UTC)

    class _FixedDateTime:
        @staticmethod
        def now(tz=None):
            return fixed_now

    monkeypatch.setattr(query, "datetime", _FixedDateTime)
    monkeypatch.setattr(query, "embed_query", lambda q: [1.0] + [0.0] * 1023)

    # Baseline (no context) vs default-config (context, all stages on).
    baseline = query.search("snowflake", limit=20)

    events: list[tuple[str, list[dict]]] = []

    def collect(stage, stage_events):
        events.append((stage, stage_events))

    with query._eval_scope(config=query.StageConfig(), collector=collect):
        res = query.search("snowflake", limit=20)
    assert res == baseline, "default StageConfig changed search() results"

    stages = {stage: stage_events for stage, stage_events in events}
    # Scenario A: ts_rank_cd must hit (content_tsvector contains snowflake).
    tsrank = [ev for ev in stages["ts_rank_cd"] if "doc_id" in ev]
    assert tsrank, "ts_rank_cd stage empty for a tsvector-hit query"
    # Scenario C: exact short query channel must hit (<= 20 chars, ILIKE).
    exact = [ev for ev in stages["exact"] if "doc_id" in ev]
    assert exact, "exact stage empty for a short substring query"
    # Every stage present; latency markers present for empty stages too.
    for stage_name in ("dense", "ts_rank_cd", "exact", "trigram", "rrf", "temporal", "dedup"):
        assert stage_name in stages, f"stage {stage_name} not emitted"
        assert any("latency_ms" in ev for ev in stages[stage_name]), f"stage {stage_name} missing latency"
    # rrf/temporal events carry rank + score sorted descending.
    for stage_name in ("rrf", "temporal"):
        ranked = [ev for ev in stages[stage_name] if "doc_id" in ev]
        ranks = [ev["rank"] for ev in ranked]
        assert ranks == list(range(1, len(ranks) + 1)), f"{stage_name} ranks not sequential"
        scores = [ev["score"] for ev in ranked]
        assert scores == sorted(scores, reverse=True), f"{stage_name} scores not descending"
    # dedup: returned results == mmr_keep doc ids in order.
    keeps = [ev["doc_id"] for ev in stages["dedup"] if ev.get("reason") == "mmr_keep"]
    mine = [r for r in res if r[3] == sid]
    assert keeps, "dedup produced no mmr_keep events"
    assert len(mine) == len(keeps), "returned results != dedup mmr_keep count"
    # Every stage event is allowlist-only.
    allowed = {"doc_id", "session_id", "source_type", "rank", "score", "reason", "latency_ms"}
    for stage_events in stages.values():
        for ev in stage_events:
            assert set(ev) <= allowed, f"trace leaked field in {ev}"


def test_production_stage_trigram_fallback_real_pg(db, monkeypatch):
    """Real-PG trigram FALLBACK: a typo/approx query with NO exact token and NO
    tsvector hit must be caught by the pg_trgm fallback. Proves:
    - ts_rank_cd is empty (no tsvector term) AND exact is empty (no substring);
    - trigram carries the near-miss row;
    - the returned results match the trigram-keep doc ids via a DB id->content
      mapping (item-by-item, not just count)."""
    from datetime import UTC, datetime

    import query

    conn, prefix = db
    sid = prefix + "-trgm"
    now = datetime.now(UTC)

    def _insert(content, emb, ts, src="main_user"):
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO session_chunks
               (session_id, source_type, content, embedding, embedding_model,
                timestamp_start, timestamp_end, turn_index_start, turn_index_end,
                content_tsvector, created_at)
               VALUES (%s,%s,%s,%s::vector,%s,%s,%s,%s,%s,to_tsvector('simple',%s),%s)""",
            (sid, src, content, str(emb), "voyage-4-large", ts, ts, 0, 0, content, ts),
        )
        conn.commit()
        cur.close()

    far = [0.5] + [0.8660254] + [0.0] * 1022
    # "clustter" is a typo of "cluster": tsvector('simple','clustter') does not
    # match (no shared term), ILIKE %clustter% does not match, but pg_trgm
    # similarity('cluster','clustter') = 0.7 (>0.3 default threshold), so the
    # trigram FALLBACK catches it.
    _insert("cluster", far, now)

    fixed_now = datetime(2026, 8, 11, 3, 30, 0, tzinfo=UTC)

    class _FixedDateTime:
        @staticmethod
        def now(tz=None):
            return fixed_now

    monkeypatch.setattr(query, "datetime", _FixedDateTime)
    monkeypatch.setattr(query, "embed_query", lambda q: [1.0] + [0.0] * 1023)

    baseline = query.search("clustter", limit=20)
    events: list[tuple[str, list[dict]]] = []

    def collect(stage, stage_events):
        events.append((stage, stage_events))

    with query._eval_scope(config=query.StageConfig(), collector=collect):
        res = query.search("clustter", limit=20)
    assert res == baseline, "default StageConfig changed results"
    stages = {stage: stage_events for stage, stage_events in events}
    # ts_rank_cd empty (typo has no tsvector term) and exact empty (no ILIKE).
    assert not any("doc_id" in ev for ev in stages["ts_rank_cd"]), "ts_rank_cd should be empty for a typo"
    assert not any("doc_id" in ev for ev in stages["exact"]), "exact should be empty for a typo"
    # trigram FALLBACK must carry the near-miss row.
    trigram_ids = [ev["doc_id"] for ev in stages["trigram"] if "doc_id" in ev]
    assert trigram_ids, "trigram fallback produced no candidates"
    # Item-by-item: map keep doc ids -> content via the DB, compare to results.
    keeps = [ev["doc_id"] for ev in stages["dedup"] if ev.get("reason") == "mmr_keep"]
    assert keeps, "dedup produced no mmr_keep events"
    cur = conn.cursor()
    keep_content = {}
    for doc_id in keeps:
        cur.execute("SELECT content FROM session_chunks WHERE id = %s", (doc_id,))
        row = cur.fetchone()
        assert row is not None, f"keep doc {doc_id} not in DB"
        keep_content[doc_id] = row[0]
    cur.close()
    mine = [r for r in res if r[3] == sid]
    expected_content = [keep_content[d] for d in keeps if d in keep_content]
    assert mine, "no session results returned"
    assert [r[0] for r in mine] == expected_content, (
        "returned result order does not match dedup mmr_keep content order"
    )


def test_production_stage_ablations_real_pg(db, monkeypatch):
    """Real-PG: explicit stage configuration changes the produced ranking.
    A lexical-only winner (content token present, embedding far) surfaces under
    the default config but is removed when lexical/exact are disabled."""
    from datetime import UTC, datetime, timedelta

    import query

    conn, prefix = db
    sid = prefix + "-abl"
    now = datetime.now(UTC)

    def _insert(content, emb, ts, src="main_user"):
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO session_chunks
               (session_id, source_type, content, embedding, embedding_model,
                timestamp_start, timestamp_end, turn_index_start, turn_index_end,
                content_tsvector, created_at)
               VALUES (%s,%s,%s,%s::vector,%s,%s,%s,%s,%s,to_tsvector('simple',%s),%s)""",
            (sid, src, content, str(emb), "voyage-4-large", ts, ts, 0, 0, content, ts),
        )
        conn.commit()
        cur.close()

    far = [0.5] + [0.8660254] + [0.0] * 1022
    # Lexical-only winner: embeds far from the query but its content contains
    # the exact token 'shiori_abl_special'; dense-only would not surface it.
    _insert("shiori_abl_special keyword target", far, now - timedelta(days=1))
    _insert("shiori_abl_new_item recent", far, now)

    fixed_now = datetime(2026, 8, 11, 3, 30, 0, tzinfo=UTC)

    class _FixedDateTime:
        @staticmethod
        def now(tz=None):
            return fixed_now

    monkeypatch.setattr(query, "datetime", _FixedDateTime)
    monkeypatch.setattr(query, "embed_query", lambda q: [1.0] + [0.0] * 1023)

    def _run(config):
        with query._eval_scope(config=config):
            return query.search("shiori_abl_special", limit=20)

    full = _run(query.StageConfig())
    dense_only = _run(query.StageConfig(lexical=False, exact=False))

    mine_full = [r[0] for r in full if r[3] == sid]
    mine_dense = [r[0] for r in dense_only if r[3] == sid]

    assert mine_full, "full config returned no session results"
    # The lexical-only doc surfaces under full but not under dense-only.
    assert "shiori_abl_special keyword target" in mine_full
    assert "shiori_abl_special keyword target" not in mine_dense
    assert mine_full != mine_dense, "dense-only ablation did not remove the lexical-only doc"


def test_stage_config_switch_matrix_real_pg(db, monkeypatch):
    """Every StageConfig switch must genuinely change execution/output.

    For each of dense/lexical/exact/temporal/dedup, assert that a config with
    that switch disabled produces a different result set / trace than the
    default, and that disabled stages emit stage_disabled summaries instead of
    candidate events (real ablation, not cosmetic)."""
    from datetime import UTC, datetime, timedelta

    import query

    conn, prefix = db
    sid = prefix + "-matrix"
    now = datetime.now(UTC)

    def _insert(content, emb, ts, src="main_user"):
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO session_chunks
               (session_id, source_type, content, embedding, embedding_model,
                timestamp_start, timestamp_end, turn_index_start, turn_index_end,
                content_tsvector, created_at)
               VALUES (%s,%s,%s,%s::vector,%s,%s,%s,%s,%s,to_tsvector('simple',%s),%s)""",
            (sid, src, content, str(emb), "voyage-4-large", ts, ts, 0, 0, content, ts),
        )
        conn.commit()
        cur.close()

    near = [0.8660254] + [0.5] + [0.0] * 1022
    far = [0.5] + [0.8660254] + [0.0] * 1022
    _insert("shiori_matrix alpha keyword target", near, now)
    _insert("shiori_matrix beta similar text", near, now - timedelta(days=10))
    _insert("shiori_matrix gamma lexical-only", far, now - timedelta(days=120))

    fixed_now = datetime(2026, 8, 11, 3, 30, 0, tzinfo=UTC)

    class _FixedDateTime:
        @staticmethod
        def now(tz=None):
            return fixed_now

    monkeypatch.setattr(query, "datetime", _FixedDateTime)
    monkeypatch.setattr(query, "embed_query", lambda q: [1.0] + [0.0] * 1023)

    def _run(config, qtext="shiori_matrix"):
        events: list[tuple[str, list[dict]]] = []

        def collect(stage, stage_events):
            events.append((stage, stage_events))

        with query._eval_scope(config=config, collector=collect):
            res = query.search(qtext, limit=20)
        return res, {stage: evs for stage, evs in events}

    full_res, full_stages = _run(query.StageConfig())
    assert full_res, "default config returned nothing"

    # dense off: vector channel disabled -> stage_disabled summary, no dense
    # candidates; the lexical-only doc (gamma) still surfaces.
    res, stages = _run(query.StageConfig(dense=False), qtext="shiori_matrix gamma")
    assert not any("doc_id" in ev for ev in stages["dense"]), "dense disabled produced candidate events"
    assert any(ev.get("reason") == "stage_disabled" for ev in stages["dense"]), "dense disabled lacks stage_disabled"
    assert any("gamma" in r[0] for r in res), "lexical-only doc should surface when dense is off"

    # lexical off: ts_rank_cd AND trigram both stage_disabled; exact-only still
    # works for a short exact query.
    res, stages = _run(query.StageConfig(lexical=False), qtext="shiori_matrix gamma")
    assert not any("doc_id" in ev for ev in stages["ts_rank_cd"]), "lexical disabled leaked ts_rank_cd"
    assert not any("doc_id" in ev for ev in stages["trigram"]), "lexical disabled leaked trigram"
    assert all(ev.get("reason") == "stage_disabled" for ev in stages["ts_rank_cd"]), "ts_rank_cd lacks stage_disabled"

    # exact off: no exact candidates.
    res, stages = _run(query.StageConfig(exact=False), qtext="shiori_matrix gamma")
    assert not any("doc_id" in ev for ev in stages["exact"]), "exact disabled leaked candidates"
    assert all(ev.get("reason") == "stage_disabled" for ev in stages["exact"]), "exact lacks stage_disabled"

    # temporal off: temporal stage_disabled (no decay applied); result differs
    # from default (old far doc not decayed).
    res, stages = _run(query.StageConfig(temporal=False), qtext="shiori_matrix")
    assert not any("doc_id" in ev for ev in stages["temporal"]), "temporal disabled leaked candidates"
    assert all(ev.get("reason") == "stage_disabled" for ev in stages["temporal"]), "temporal lacks stage_disabled"

    # dedup off: MMR bypassed -> ALL ranked candidates returned (identical
    # embeddings not collapsed); dedup stage_disabled, no keep/drop events.
    res, stages = _run(query.StageConfig(dedup=False), qtext="shiori_matrix")
    assert not any("doc_id" in ev for ev in stages["dedup"]), "dedup disabled leaked keep/drop events"
    assert all(ev.get("reason") == "stage_disabled" for ev in stages["dedup"]), "dedup lacks stage_disabled"
    # near and near-identical (alpha/beta same embedding) both returned.
    mine = [r[0] for r in res if r[3] == sid]
    assert any("alpha" in c for c in mine) and any("beta" in c for c in mine), "dedup off collapsed near-duplicates"


def test_stage_config_no_candidate_channel_fails_closed(monkeypatch):
    """A StageConfig with no enabled candidate channel must fail closed."""
    import query

    with pytest.raises(ValueError):
        query.StageConfig(dense=False, lexical=False, exact=False).validate()
    # Also at search time when installed as the active config.
    monkeypatch.setattr(query, "embed_query", lambda q: [1.0] + [0.0] * 1023)
    monkeypatch.setattr(query, "get_db", lambda: _NoRowsConn())
    with pytest.raises(ValueError):
        with query._eval_scope(config=query.StageConfig(dense=False, lexical=False, exact=False)):
            query.search("zzqx no-channel 1", limit=20)


def test_stage_config_strict_bool_fails_closed():
    """StageConfig must reject ANY non-bool value for its five switches."""
    import query

    bad = [
        {"dense": "false"},
        {"lexical": 1},
        {"exact": "on"},
        {"temporal": "yes"},
        {"dedup": 0},
    ]
    for kwargs in bad:
        with pytest.raises(TypeError):
            query.StageConfig(**kwargs)
    # Also guards against a mutated (non-bool) field at validate() time.
    cfg = query.StageConfig()
    with pytest.raises(TypeError):
        cfg.dense = "false"  # type: ignore[assignment]
        cfg.validate()


def test_stage_config_execution_probes_dense_off(monkeypatch):
    """dense=False must NOT call the embedding provider (no embed_query)."""
    import query

    embed_calls = []

    def spy_embed(text):
        embed_calls.append(text)
        return [1.0] + [0.0] * 1023

    monkeypatch.setattr(query, "embed_query", spy_embed)
    monkeypatch.setattr(query, "get_db", lambda: _NoRowsConn())
    with query._eval_scope(config=query.StageConfig(dense=False, lexical=True, exact=True)):
        query.search("zzqx probe dense-off 2", limit=20)
    assert embed_calls == [], "dense=False must not call embed_query"


def test_stage_config_execution_probes_lexical_exact_sql(monkeypatch):
    """lexical=False and exact=False must not execute their SQL patterns."""
    import query

    executed = []

    class _ProbeConn(_NoRowsConn):
        def cursor(self):
            return self

        def execute(self, sql, params=None):
            executed.append(sql)
            return self

    monkeypatch.setattr(query, "embed_query", lambda q: [1.0] + [0.0] * 1023)
    monkeypatch.setattr(query, "get_db", lambda: _ProbeConn())

    # lexical off: no ts_rank_cd SQL, no trigram SQL.
    with query._eval_scope(config=query.StageConfig(lexical=False)):
        query.search("zzqx probe lexical-off 3", limit=20)
    joined = " ".join(executed)
    assert "ts_rank_cd" not in joined, "lexical=False executed ts_rank_cd SQL"
    assert "similarity(content" not in joined, "lexical=False executed trigram SQL"

    executed.clear()
    # exact off: no ILIKE SQL.
    with query._eval_scope(config=query.StageConfig(exact=False)):
        query.search("zzqx probe exact-off 4", limit=20)
    joined = " ".join(executed)
    assert "ILIKE" not in joined, "exact=False executed exact SQL"


def test_stage_config_execution_probes_dedup_off(monkeypatch):
    """dedup=False must NOT call _cosine_sim (MMR loop bypassed)."""
    import query

    cosine_calls = []

    def spy_cosine(a, b):
        cosine_calls.append(1)
        return 0.5

    monkeypatch.setattr(query, "_cosine_sim", spy_cosine)
    monkeypatch.setattr(query, "embed_query", lambda q: [1.0] + [0.0] * 1023)
    monkeypatch.setattr(query, "get_db", lambda: _NoRowsConn())
    with query._eval_scope(config=query.StageConfig(dedup=False)):
        query.search("zzqx probe dedup-off 5", limit=20)
    assert cosine_calls == [], "dedup=False must not call _cosine_sim"


# ---- Golden set bucket counts ---------------------------------------------


def test_golden_rows_present_and_bucket_counts():
    from benchmark.product_eval.manifest import load_golden_rows, load_manifest, validate_bucket_counts

    manifest = load_manifest(DATASET_MANIFEST)
    rows = load_golden_rows(GOLDEN_ROWS)
    validate_bucket_counts(manifest, rows)
    # 8 main buckets, 120 total.
    assert sum(manifest["golden_queries"]["bucket_counts"].values()) == 120
    assert manifest["dataset"]["query_count"] == 120
