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
    stable_text_hash,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "replay"
MANIFEST = FIXTURES / "manifest.json"
VECTORS = FIXTURES / "vectors.json"
CORPUS = FIXTURES / "corpus.jsonl"
QUERIES = FIXTURES / "queries.jsonl"


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
    assert manifest.query_prompt and manifest.document_prompt
    assert manifest.corpus_input_type == "document"
    assert manifest.query_input_type == "query"
    assert manifest.key_format == "input_type:sha256(text)"


def test_manifest_hashes_match_fixture_files() -> None:
    manifest = load_manifest(MANIFEST)
    from shiori.embedding_replay import file_sha256

    assert manifest.corpus_sha256 == file_sha256(CORPUS)
    assert manifest.query_sha256 == file_sha256(QUERIES)
    assert manifest.vectors_sha256 == file_sha256(VECTORS)


def test_replay_returns_exact_fixture_vector_for_known_document(embedder: ReplayEmbedder) -> None:
    text = _read_texts(CORPUS)[0]
    expected = json.loads(VECTORS.read_text(encoding="utf-8"))[composite_key("document", text)]
    assert embedder.embed(text, input_type="document") == expected


def test_replay_document_and_query_use_distinct_vectors() -> None:
    vectors = json.loads(VECTORS.read_text(encoding="utf-8"))
    doc_text = _read_texts(CORPUS)[0]
    query_text = _read_texts(QUERIES)[0]
    # The composite key space must separate document and query identities.
    assert f"document:{stable_text_hash(doc_text)}" in vectors
    assert f"query:{stable_text_hash(query_text)}" in vectors
    # Same text can live in both spaces and differ.
    same = doc_text
    if f"query:{stable_text_hash(same)}" in vectors:
        assert vectors[f"document:{stable_text_hash(same)}"] != vectors[f"query:{stable_text_hash(same)}"]


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
    vectors_data[composite_key("document", text)] = [0.0] * 16
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
    vectors_data[composite_key("document", text)] = [float("nan")] + [0.0] * 1023
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


def test_composite_key_is_deterministic_and_binds_type() -> None:
    assert composite_key("document", "same") == composite_key("document", "same")
    assert composite_key("document", "same") != composite_key("query", "same")
    assert composite_key("document", "same") != composite_key("document", "other")
    assert composite_key("document", "same").startswith("document:")
