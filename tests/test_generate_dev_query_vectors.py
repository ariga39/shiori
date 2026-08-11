"""CI-safe tests for tools/generate_dev_query_vectors.py (task #18).

These run WITHOUT a real model (a fake SentenceTransformer is installed in
sys.modules) and verify:
- select_dev_ids returns exactly 72 dev ids, rejecting duplicate/missing/extra
  rows;
- running main() with the fake model emits exactly 72 dev vectors, zero
  holdout ids, and calls encode_query with dim 1024 / float32 / L2 / the pinned
  revision.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PRODUCT_EVAL = REPO / "benchmark" / "product_eval"
MANIFEST = PRODUCT_EVAL / "dataset_manifest.json"
ROWS = PRODUCT_EVAL / "golden_queries.jsonl"

sys.path.insert(0, str(REPO))


class FakeSentenceTransformer:
    """A fake sentence-transformer for the generator CI test."""

    def __init__(self, *args, **kwargs):
        self.model_id = kwargs.get("model_id") or (args[0] if args else None)
        self.revision = kwargs.get("revision")
        self.truncate_dim = kwargs.get("truncate_dim")
        self.device = kwargs.get("device")
        self.calls = []

    def encode_query(self, text, *, truncate_dim=1024, precision="float32", normalize_embeddings=True, show_progress_bar=False):
        self.calls.append(
            {
                "truncate_dim": truncate_dim,
                "precision": precision,
                "normalize_embeddings": normalize_embeddings,
            }
        )
        # Deterministic 1024-dim L2-normalized vector.
        emb = [0.0] * 1024
        emb[0] = 1.0
        return type("Emb", (), {"tolist": lambda self: emb})()


def _install_fake_model(monkeypatch, module="sentence_transformers"):
    fake = FakeSentenceTransformer("fake", revision="67fabc9bef010dabc5f6024aa1b1b6b93410426f", truncate_dim=1024, device="cpu")
    st = type(sys)(f"{module}")
    st.SentenceTransformer = lambda *a, **k: fake
    monkeypatch.setitem(sys.modules, module, st)
    return fake


def test_select_dev_ids_exactly_72():
    from tools.generate_dev_query_vectors import select_dev_ids

    dev = select_dev_ids(MANIFEST, ROWS)
    assert len(dev) == 72
    ids = {r["query_id"] for r in dev}
    splits = {s["query_id"]: s["split"] for s in json.loads(MANIFEST.read_text(encoding="utf-8"))["query_splits"]}
    holdout = {q for q, s in splits.items() if s == "holdout"}
    assert ids.isdisjoint(holdout)


def test_select_dev_ids_rejects_duplicate_rows(tmp_path: Path):
    from tools.generate_dev_query_vectors import select_dev_ids

    rows = [json.loads(line) for line in ROWS.read_text(encoding="utf-8").splitlines() if line.strip()]
    dup = list(rows)
    dup.append(dict(rows[0]))  # duplicate query_id
    dup_path = tmp_path / "dup.jsonl"
    dup_path.write_text("\n".join(json.dumps(r) for r in dup), encoding="utf-8")
    with pytest.raises(SystemExit):
        select_dev_ids(MANIFEST, dup_path)


def test_select_dev_ids_rejects_missing_row(tmp_path: Path):
    from tools.generate_dev_query_vectors import select_dev_ids

    rows = [json.loads(line) for line in ROWS.read_text(encoding="utf-8").splitlines() if line.strip()]
    rows.pop(0)  # missing a query
    missing_path = tmp_path / "missing.jsonl"
    missing_path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    with pytest.raises(SystemExit):
        select_dev_ids(MANIFEST, missing_path)


def test_select_dev_ids_rejects_extra_row(tmp_path: Path):
    from tools.generate_dev_query_vectors import select_dev_ids

    rows = [json.loads(line) for line in ROWS.read_text(encoding="utf-8").splitlines() if line.strip()]
    extra = dict(rows[0])
    extra["query_id"] = "q-9999"
    rows.append(extra)
    extra_path = tmp_path / "extra.jsonl"
    extra_path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    with pytest.raises(SystemExit):
        select_dev_ids(MANIFEST, extra_path)


def test_generator_main_emits_72_dev_vectors(monkeypatch, tmp_path: Path):
    from tools.generate_dev_query_vectors import main as gen_main

    fake = _install_fake_model(monkeypatch)
    out = tmp_path / "dev_vectors.json"
    rc = gen_main(
        [
            "--manifest", str(MANIFEST),
            "--rows", str(ROWS),
            "--out", str(out),
        ]
    )
    assert rc == 0
    vec = json.loads(out.read_text(encoding="utf-8"))
    assert len(vec) == 72
    ids = {v["query_id"] for v in vec}
    splits = {s["query_id"]: s["split"] for s in json.loads(MANIFEST.read_text(encoding="utf-8"))["query_splits"]}
    holdout = {q for q, s in splits.items() if s == "holdout"}
    assert ids.isdisjoint(holdout)
    # encode_query contract: 1024 / float32 / L2 / pinned revision.
    assert fake.calls, "encode_query never called"
    for call in fake.calls:
        assert call["truncate_dim"] == 1024
        assert call["precision"] == "float32"
        assert call["normalize_embeddings"] is True
    # pinned revision was passed to the constructor.
    assert fake.revision == "67fabc9bef010dabc5f6024aa1b1b6b93410426f"
    assert fake.truncate_dim == 1024
