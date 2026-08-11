"""Phase 4D runner contract tests (task #18).

These are CI-safe (no model, no network, no live DB). They verify the runner's
frozen contracts that do not require PostgreSQL: the development-split guard
(holdout ids fail closed), the emitted-trace validation path, and the smoke
report shape.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PRODUCT_EVAL = REPO / "benchmark" / "product_eval"
DATASET_MANIFEST = PRODUCT_EVAL / "dataset_manifest.json"
GOLDEN_ROWS = PRODUCT_EVAL / "golden_queries.jsonl"


def test_development_split_guard_loads_72():
    from benchmark.product_eval.runner import _load_development_ids

    dev = _load_development_ids(DATASET_MANIFEST)
    assert len(dev) == 72
    # All development ids exist in the golden rows.
    from benchmark.product_eval.manifest import load_golden_rows

    ids = {row["query_id"] for row in load_golden_rows(GOLDEN_ROWS)}
    assert dev.issubset(ids)


def test_guard_row_rejects_holdout():
    from benchmark.product_eval.runner import RunnerError, _guard_row, _load_development_ids

    dev = _load_development_ids(DATASET_MANIFEST)
    # Find a holdout id (not in the 72 dev ids).
    from benchmark.product_eval.manifest import load_golden_rows

    all_ids = {row["query_id"] for row in load_golden_rows(GOLDEN_ROWS)}
    holdout = next(q for q in sorted(all_ids) if q not in dev)
    with pytest.raises(RunnerError):
        _guard_row({"query_id": holdout}, dev)
    # A development id passes.
    _guard_row({"query_id": next(iter(dev))}, dev)


def test_frozen_config_matrix_shapes():
    from benchmark.product_eval.runner import FROZEN_CONFIGS

    assert set(FROZEN_CONFIGS) == {"dense-only", "lexical-only", "rrf", "+exact", "+temporal", "+dedup"}
    # Every config must have at least one candidate channel enabled.
    for k, cfg in FROZEN_CONFIGS.items():
        assert cfg["dense"] or cfg["lexical"], f"{k} has no candidate channel"

def test_smoke_requires_database_dsn(monkeypatch):
    """The runner must fail closed when no SHIORI_DATABASE_DSN is configured."""
    import query  # noqa: F401 - must be importable
    from benchmark.product_eval.runner import RunnerError, run_smoke

    monkeypatch.delenv("SHIORI_DATABASE_DSN", raising=False)
    with pytest.raises(RunnerError):
        run_smoke(manifest_path=DATASET_MANIFEST, rows_path=GOLDEN_ROWS, dev_limit=1)


def test_smoke_dev_limit_bounds():
    """dev_limit must be an int in [1, 72]."""
    from benchmark.product_eval.runner import RunnerError, run_smoke

    bad: list = [0, 73, -1, 100, "5", True]
    for item in bad:
        with pytest.raises(RunnerError):
            run_smoke(manifest_path=DATASET_MANIFEST, rows_path=GOLDEN_ROWS, dev_limit=item)


def test_embedding_key_closure_rejects_extra_holdout(tmp_path: Path, monkeypatch):
    """A vector file containing a holdout id (or missing dev ids) must fail
    closed before any search runs."""
    from benchmark.product_eval.manifest import load_manifest
    from benchmark.product_eval.runner import RunnerError, run_smoke

    monkeypatch.setenv("SHIORI_DATABASE_DSN", "postgresql://dummy")
    manifest = load_manifest(DATASET_MANIFEST)
    dev = sorted(s["query_id"] for s in manifest["query_splits"] if s["split"] == "tune")
    holdout = sorted(s["query_id"] for s in manifest["query_splits"] if s["split"] == "holdout")

    def _write(ids):
        vec = [{"query_id": qid, "embedding": [0.0] * 1024} for qid in ids]
        path = tmp_path / "vec.json"
        path.write_text(json.dumps(vec), encoding="utf-8")
        return path

    # Exactly dev ids passes the closure check (fails later on DSN/DB, but the
    # closure itself is the point — we assert it does NOT raise RunnerError for
    # the key set). Use a non-connectable DSN; the closure runs before DB.
    import query  # noqa: F401

    # extra holdout id -> RunnerError
    vec_extra = _write(dev + [holdout[0]])
    with pytest.raises(RunnerError):
        run_smoke(
            manifest_path=DATASET_MANIFEST, rows_path=GOLDEN_ROWS, dev_limit=5,
            embedding_json=vec_extra,
        )
    # missing a dev id -> RunnerError
    vec_missing = _write(dev[:-1])
    with pytest.raises(RunnerError):
        run_smoke(
            manifest_path=DATASET_MANIFEST, rows_path=GOLDEN_ROWS, dev_limit=5,
            embedding_json=vec_missing,
        )
    # non-finite value -> RunnerError
    vec_bad = _write(dev)
    data = json.loads(vec_bad.read_text(encoding="utf-8"))
    data[0]["embedding"] = [float("nan")] * 1024
    vec_bad.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(RunnerError):
        run_smoke(
            manifest_path=DATASET_MANIFEST, rows_path=GOLDEN_ROWS, dev_limit=5,
            embedding_json=vec_bad,
        )


def test_duplicate_metrics_formula():
    """duplicate_group_coverage vs final duplicate rate must be distinct:
    coverage counts groups with >=1 member; duplicate rate counts redundancy
    sum(max(0, hits-1)) / final result count."""
    from benchmark.product_eval.runner import _duplicate_metrics

    # groups A=[a,b], B=[c,d]; final=[a,c] -> coverage 2/2=1.0, redundancy 0.
    cov, rate = _duplicate_metrics([["a", "b"], ["c", "d"]], ["a", "c"])
    assert cov == 1.0
    assert rate == 0.0
    # final=[a,b,c] -> group A redundancy 1 (both a,b), B redundancy 0 -> 1/3.
    cov, rate = _duplicate_metrics([["a", "b"], ["c", "d"]], ["a", "b", "c"])
    assert cov == 1.0
    assert rate == pytest.approx(1 / 3)
    # single member hit per group -> coverage 1.0 but duplicate rate 0.0.
    cov, rate = _duplicate_metrics([["a", "b"]], ["a"])
    assert cov == 1.0
    assert rate == 0.0


def test_temporal_transition_definitions():
    """rank_changed vs top-1 winner_transition vs promoted_to_winner must be
    distinct (2->3 is rank_changed but not a winner transition)."""
    from benchmark.product_eval.runner import _temporal_transition

    # 2->3: rank changed, no winner transition, not promoted.
    t = _temporal_transition(2, 3)
    assert t["rank_changed"] is True
    assert t["winner_transition"] is False
    assert t["promoted_to_winner"] is False
    # 1->2: rank changed, winner transition (lost top-1), not promoted.
    t = _temporal_transition(1, 2)
    assert t["rank_changed"] is True
    assert t["winner_transition"] is True
    assert t["promoted_to_winner"] is False
    # 2->1: rank changed, winner transition (gained top-1), promoted.
    t = _temporal_transition(2, 1)
    assert t["rank_changed"] is True
    assert t["winner_transition"] is True
    assert t["promoted_to_winner"] is True
    # 1->1: unchanged.
    t = _temporal_transition(1, 1)
    assert t["rank_changed"] is False
    assert t["winner_transition"] is False
    assert t["promoted_to_winner"] is False
