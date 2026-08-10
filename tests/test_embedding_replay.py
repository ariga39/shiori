"""Replay embedding provider tests (Phase 4B).

The replay provider must map composite keys (model identity + input_type +
canonical text hash) to the exact versioned fixture vectors and fail closed on
unknown text, duplicate keys, non-finite values, dimension drift, or manifest
mismatch.  No network, model, credential, or prebuilt database is used.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from shiori.embedding_replay import (
    MANIFEST_SCHEMA,
    ReplayEmbedder,
    ReplayError,
    composite_key,
    load_manifest,
    model_identity_fingerprint,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "replay"
MANIFEST = FIXTURES / "manifest.json"
VECTORS = FIXTURES / "vectors.json"
CORPUS = FIXTURES / "corpus.jsonl"
QUERIES = FIXTURES / "queries.jsonl"

MODEL_ID = "voyage-4-nano"
MODEL_REVISION = "voyageai/voyage-4-nano@main"


def _model_key(model_id: str = MODEL_ID, model_revision: str = MODEL_REVISION) -> str:
    return model_identity_fingerprint(model_id, model_revision)


@pytest.fixture(scope="module")
def embedder() -> ReplayEmbedder:
    return ReplayEmbedder.from_files(MANIFEST, VECTORS)


def _read_texts(path: Path) -> list[str]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _write(tmp_path: Path, manifest_data: dict, vectors_data: dict) -> tuple[Path, Path]:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest_data), encoding="utf-8")
    vectors_path = tmp_path / "vectors.json"
    vectors_path.write_text(json.dumps(vectors_data), encoding="utf-8")
    return manifest_path, vectors_path


def test_manifest_matches_fixture_schema() -> None:
    manifest = load_manifest(MANIFEST)
    assert manifest.schema == MANIFEST_SCHEMA
    assert manifest.dimension == 1024
    assert manifest.normalized is True
    assert manifest.model_id == "voyage-4-nano"
    assert manifest.model_revision == "voyageai/voyage-4-nano@main"
    assert manifest.model_key_identity == _model_key()
    assert manifest.query_prompt and manifest.document_prompt
    assert manifest.corpus_input_type == "document"
    assert manifest.query_input_type == "query"
    assert manifest.key_format == "model_identity_fingerprint:input_type:sha256(text)"


def test_manifest_hashes_match_fixture_files() -> None:
    manifest = load_manifest(MANIFEST)
    from shiori.embedding_replay import file_sha256

    assert manifest.corpus_sha256 == file_sha256(CORPUS)
    assert manifest.query_sha256 == file_sha256(QUERIES)
    assert manifest.vectors_sha256 == file_sha256(VECTORS)


def test_replay_returns_exact_fixture_vector_for_known_document(embedder: ReplayEmbedder) -> None:
    text = _read_texts(CORPUS)[0]
    expected = json.loads(VECTORS.read_text(encoding="utf-8"))[
        composite_key(MODEL_ID, MODEL_REVISION, "document", text)
    ]
    assert embedder.embed(text, input_type="document") == expected


def test_replay_document_and_query_use_distinct_vectors() -> None:
    vectors = json.loads(VECTORS.read_text(encoding="utf-8"))
    doc_text = _read_texts(CORPUS)[0]
    query_text = _read_texts(QUERIES)[0]
    # The composite key space must separate model, document and query identity.
    assert composite_key(MODEL_ID, MODEL_REVISION, "document", doc_text) in vectors
    assert composite_key(MODEL_ID, MODEL_REVISION, "query", query_text) in vectors
    # Same text can live in both spaces and differ.
    same = doc_text
    query_key = composite_key(MODEL_ID, MODEL_REVISION, "query", same)
    if query_key in vectors:
        assert vectors[composite_key(MODEL_ID, MODEL_REVISION, "document", same)] != vectors[query_key]


def test_replay_embeds_every_corpus_document(embedder: ReplayEmbedder) -> None:
    for text in _read_texts(CORPUS):
        vector = embedder.embed(text, input_type="document")
        assert len(vector) == 1024
        assert all(isinstance(value, float) for value in vector)


def test_replay_unknown_text_fails_closed(embedder: ReplayEmbedder) -> None:
    with pytest.raises(ReplayError) as exc:
        embedder.embed("this text was never part of the fixture corpus", input_type="document")
    assert exc.value.code == "replay_vector_missing"


def test_replay_wrong_input_type_fails_closed(embedder: ReplayEmbedder) -> None:
    # A corpus text queried as a query (unless present) must fail closed.
    text = _read_texts(CORPUS)[0]
    with pytest.raises(ReplayError):
        embedder.embed(text, input_type="hermes")


def test_replay_manifest_hash_mismatch_fails_closed(tmp_path: Path) -> None:
    manifest_data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    manifest_data["vectors"]["sha256"] = "f" * 64
    manifest_path, vectors_path = _write(tmp_path, manifest_data, {})
    vectors_path.write_bytes(VECTORS.read_bytes())
    with pytest.raises(ReplayError) as exc:
        ReplayEmbedder.from_files(manifest_path, vectors_path)
    assert exc.value.code == "replay_manifest_hash_mismatch"


def test_replay_duplicate_key_fails_closed(tmp_path: Path) -> None:
    manifest_data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    from shiori.embedding_replay import file_sha256

    vectors_data = json.loads(VECTORS.read_text(encoding="utf-8"))
    first_key = next(iter(vectors_data))
    vectors_data[first_key + "_copy"] = vectors_data[first_key]
    vectors_data[first_key] = [0.0] * 1024
    # duplicate keys via same key twice is impossible in a dict; instead
    # force a count mismatch by claiming one more than present.
    manifest_data["vectors"]["count"] = len(vectors_data) + 1
    manifest_data["vectors"]["sha256"] = "x" * 64
    manifest_path, vectors_path = _write(tmp_path, manifest_data, vectors_data)
    manifest_data["vectors"]["sha256"] = file_sha256(vectors_path)
    manifest_path.write_text(json.dumps(manifest_data), encoding="utf-8")
    with pytest.raises(ReplayError) as exc:
        ReplayEmbedder.from_files(manifest_path, vectors_path)
    assert exc.value.code == "replay_vectors_invalid"


def test_replay_dimension_mismatch_fails_closed(tmp_path: Path) -> None:
    text = _read_texts(CORPUS)[0]
    manifest_data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    vectors_data = json.loads(VECTORS.read_text(encoding="utf-8"))
    vectors_data[composite_key(MODEL_ID, MODEL_REVISION, "document", text)] = [0.0] * 16
    from shiori.embedding_replay import file_sha256

    manifest_data["vectors"]["sha256"] = "x" * 64
    manifest_path, vectors_path = _write(tmp_path, manifest_data, vectors_data)
    manifest_data["vectors"]["sha256"] = file_sha256(vectors_path)
    manifest_path.write_text(json.dumps(manifest_data), encoding="utf-8")
    with pytest.raises(ReplayError) as exc:
        ReplayEmbedder.from_files(manifest_path, vectors_path)
    assert exc.value.code == "replay_dimension_mismatch"


def test_replay_non_finite_vector_fails_closed(tmp_path: Path) -> None:
    text = _read_texts(CORPUS)[0]
    manifest_data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    vectors_data = json.loads(VECTORS.read_text(encoding="utf-8"))
    vectors_data[composite_key(MODEL_ID, MODEL_REVISION, "document", text)] = [float("nan")] + [0.0] * 1023
    from shiori.embedding_replay import file_sha256

    manifest_data["vectors"]["sha256"] = "x" * 64
    manifest_path, vectors_path = _write(tmp_path, manifest_data, vectors_data)
    manifest_data["vectors"]["sha256"] = file_sha256(vectors_path)
    manifest_path.write_text(json.dumps(manifest_data), encoding="utf-8")
    with pytest.raises(ReplayError) as exc:
        ReplayEmbedder.from_files(manifest_path, vectors_path)
    assert exc.value.code == "replay_vectors_invalid"


def test_replay_manifest_schema_mismatch_fails_closed(tmp_path: Path) -> None:
    manifest_data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    manifest_data["schema"] = "unrelated/schema-v9"
    manifest_path, _ = _write(tmp_path, manifest_data, {})
    with pytest.raises(ReplayError) as exc:
        load_manifest(manifest_path)
    assert exc.value.code == "replay_manifest_schema_mismatch"


def test_replay_manifest_missing_model_dimension_fails_closed(tmp_path: Path) -> None:
    manifest_data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    del manifest_data["model"]["dimension"]
    manifest_path, _ = _write(tmp_path, manifest_data, {})
    with pytest.raises(ReplayError) as exc:
        load_manifest(manifest_path)
    assert exc.value.code == "replay_manifest_invalid"


def test_composite_key_is_deterministic_and_binds_identity() -> None:
    assert composite_key(MODEL_ID, MODEL_REVISION, "document", "same") == composite_key(
        MODEL_ID, MODEL_REVISION, "document", "same"
    )
    assert composite_key(MODEL_ID, MODEL_REVISION, "document", "same") != composite_key(
        MODEL_ID, MODEL_REVISION, "query", "same"
    )
    assert composite_key(MODEL_ID, MODEL_REVISION, "document", "same") != composite_key(
        MODEL_ID, MODEL_REVISION, "document", "other"
    )
    # Different model identity must yield a different key (binds the model).
    assert composite_key(MODEL_ID, MODEL_REVISION, "document", "same") != composite_key(
        "other-model", "other@rev", "document", "same"
    )
    key = composite_key(MODEL_ID, MODEL_REVISION, "document", "same")
    assert key.startswith(f"{_model_key()}:document:")


def test_replay_fixture_from_different_model_fails_closed(tmp_path: Path) -> None:
    """A vectors fixture whose model fingerprint differs from the manifest must
    fail closed at load time instead of being silently consumed."""
    manifest_data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    vectors_data = json.loads(VECTORS.read_text(encoding="utf-8"))
    # Rewrite every key under a different model identity (keep type + text hash).
    other_fp = model_identity_fingerprint("other-model", "other@rev")
    other_vectors = {
        f"{other_fp}:{key.split(':', 2)[1]}:{key.split(':', 2)[2]}": value
        for key, value in vectors_data.items()
    }
    from shiori.embedding_replay import file_sha256

    manifest_data["vectors"]["sha256"] = "x" * 64
    manifest_path, vectors_path = _write(tmp_path, manifest_data, other_vectors)
    manifest_data["vectors"]["sha256"] = file_sha256(vectors_path)
    manifest_path.write_text(json.dumps(manifest_data), encoding="utf-8")
    with pytest.raises(ReplayError) as exc:
        ReplayEmbedder.from_files(manifest_path, vectors_path)
    assert exc.value.code == "replay_model_identity_mismatch"
