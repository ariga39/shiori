"""Offline tests for tools/generate_replay_fixtures.py (Phase 4B).

These tests never load a real model.  They inject a fake ``sentence_transformers``
module to prove the generator (a) always passes the FIXED repo id + exact
revision to the loader, (b) uses ``encode_document`` for documents and
``encode_query`` for queries (no manual prompt duplication), and (c) records the
exact identity in the manifest.  All generation targets a temp output dir so the
committed fixture is never touched.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GEN_PATH = ROOT / "tools" / "generate_replay_fixtures.py"
SESSIONS = ROOT / "tools" / "e2e-replay-sessions"


@pytest.fixture
def fake_st_module(monkeypatch):
    """Inject a fake sentence_transformers with a recording SentenceTransformer."""

    captured: dict = {"loader_kwargs": None, "encode_document": 0, "encode_query": 0}

    class _FakeST:
        def __init__(self, model_id, *, revision, **kwargs):
            captured["loader_kwargs"] = {"model_id": model_id, "revision": revision, **kwargs}

        def encode_document(self, texts, **kwargs):
            captured["encode_document"] += 1
            return [[1.0] * 1024 for _ in texts]

        def encode_query(self, texts, **kwargs):
            captured["encode_query"] += 1
            return [[1.0] * 1024 for _ in texts]

    module = types.ModuleType("sentence_transformers")
    module.SentenceTransformer = _FakeST
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)
    return captured


def _load_gen():
    if "generate_replay_fixtures" in sys.modules:
        del sys.modules["generate_replay_fixtures"]
    spec = importlib.util.spec_from_file_location("generate_replay_fixtures", GEN_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["generate_replay_fixtures"] = module
    spec.loader.exec_module(module)
    return module


def _gen_to(tmp_path: Path, gen, fake_st_module) -> Path:
    out = tmp_path / "out"
    gen.main(["--sessions", str(SESSIONS), "--out", str(out)])
    return out


def test_loader_receives_fixed_repo_id_and_exact_revision(fake_st_module, tmp_path: Path) -> None:
    gen = _load_gen()
    _gen_to(tmp_path, gen, fake_st_module)
    kwargs = fake_st_module["loader_kwargs"]
    assert kwargs["model_id"] == "voyageai/voyage-4-nano"
    assert kwargs["revision"] == "67fabc9bef010dabc5f6024aa1b1b6b93410426f"
    assert kwargs["truncate_dim"] == 1024
    assert kwargs["device"] == "cpu"


def test_documents_and_queries_use_distinct_encoders(fake_st_module, tmp_path: Path) -> None:
    gen = _load_gen()
    _gen_to(tmp_path, gen, fake_st_module)
    assert fake_st_module["encode_document"] == 1
    assert fake_st_module["encode_query"] == 1


def test_manifest_records_pinned_identity(fake_st_module, tmp_path: Path) -> None:
    import json

    gen = _load_gen()
    out = _gen_to(tmp_path, gen, fake_st_module)
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["model"]["id"] == "voyageai/voyage-4-nano"
    assert manifest["model"]["revision"] == "67fabc9bef010dabc5f6024aa1b1b6b93410426f"
    assert manifest["model"]["prompt_identity"] == {
        "query": "encode_query",
        "document": "encode_document",
    }
    assert len(manifest["model"]["key_identity"]) == 64


def test_model_constants_are_pinned() -> None:
    gen = _load_gen()
    assert gen.MODEL_ID == "voyageai/voyage-4-nano"
    assert gen.MODEL_REVISION == "67fabc9bef010dabc5f6024aa1b1b6b93410426f"
    assert len(gen.model_identity_fingerprint(gen.MODEL_ID, gen.MODEL_REVISION)) == 64
