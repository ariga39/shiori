"""Versioned replay embedding provider for the fixture-backed E2E.

This provider replays pre-generated, versioned vectors keyed by a composite
identity: model identity + input_type (document/query) + canonical text hash.
It is the deterministic, offline seam that lets a full install → configure →
migrate → ingest → search chain run with real vector distributions (generated
by a pinned real model) without any model, network, or credential at runtime.
Unknown text, dimension mismatches, duplicate/non-finite keys, and manifest
drift fail closed; there is never a silent fallback.

Contract (Phase 4B): the manifest records the fixture schema, generator/model
revision, dimension, dtype/normalization, input-type prompts, corpus/query
versions and hashes, and an exact mapping ``input_type:sha256(text) -> vector``.
The repository ships the manifest, synthetic corpus/queries, and vectors; never
a prebuilt database.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MANIFEST_SCHEMA = "shiori-replay-fixture/v1"
INPUT_TYPES = ("document", "query")


class ReplayError(ValueError):
    """Stable, secret-safe failure raised by the replay provider."""

    def __init__(self, message: str, *, code: str):
        super().__init__(message)
        self.code = code


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """``json.loads`` object_pairs_hook that fails closed on duplicate keys.

    Python's default ``json.loads`` silently keeps the LAST value for a repeated
    object key, so a vectors fixture with a duplicated key would be consumed as
    if it were a single entry.  This hook rejects any repeated key during parse,
    before any validation, so the fixture cannot hide a real duplicate.
    """
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReplayError(
                f"replay vector fixture contains a duplicate key: {key!r}",
                code="replay_vectors_duplicate_key",
            )
        result[key] = value
    return result


@dataclass(frozen=True)
class ReplayManifest:
    """Validated metadata describing one versioned vector fixture."""

    schema: str
    generator_name: str
    generator_revision: str
    model_id: str
    model_revision: str
    model_key_identity: str
    dimension: int
    dtype: str
    normalized: bool
    prompt_identity: dict[str, str]
    corpus_version: int
    query_version: int
    corpus_input_type: str
    query_input_type: str
    corpus_sha256: str
    query_sha256: str
    vectors_sha256: str
    vector_count: int
    key_format: str


def stable_text_hash(text: str) -> str:
    """Return the stable text component used to look up a replayed vector."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def model_identity_fingerprint(model_id: str, model_revision: str) -> str:
    """Return the full SHA-256 fingerprint binding the exact model identity.

    The fingerprint is the complete digest of ``model_id|model_revision``, so a
    fixture produced by a different model (or a different pinned revision)
    cannot collide with, or silently satisfy, this fixture's lookup keys.  The
    full 64-hex digest avoids the unnecessary 64-bit truncation collision
    surface of a shortened prefix.
    """
    return hashlib.sha256(f"{model_id}|{model_revision}".encode()).hexdigest()


def composite_key(model_id: str, model_revision: str, input_type: str, text: str) -> str:
    """Return ``<modelfp>:<input_type>:sha256(text)``.

    Binds the model identity, the input type (document/query), and the canonical
    text bytes into one key.  A lookup can only succeed against a fixture that
    shares the same pinned model identity.
    """
    if input_type not in INPUT_TYPES:
        raise ReplayError(f"invalid input_type: {input_type!r}", code="replay_invalid_input_type")
    return f"{model_identity_fingerprint(model_id, model_revision)}:{input_type}:{stable_text_hash(text)}"


def file_sha256(path: Path) -> str:
    """Stream a file in fixed blocks and return its full sha256 hexdigest."""
    hasher = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            block = fh.read(1 << 20)
            if not block:
                break
            hasher.update(block)
    return hasher.hexdigest()


def _require_mapping(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key)
    if not isinstance(value, dict):
        raise ReplayError(
            f"replay manifest missing or invalid field: {key}",
            code="replay_manifest_invalid",
        )
    return value


def _require_str(mapping: dict[str, Any], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ReplayError(
            f"replay manifest missing or invalid field: {key}",
            code="replay_manifest_invalid",
        )
    return value


def _require_int(mapping: dict[str, Any], key: str, *, minimum: int = 0) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ReplayError(
            f"replay manifest missing or invalid field: {key}",
            code="replay_manifest_invalid",
        )
    return value


def load_manifest(manifest_path: Path) -> ReplayManifest:
    """Load and validate a replay fixture manifest, failing closed on drift."""
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ReplayError("replay manifest cannot be read", code="replay_manifest_unreadable") from exc
    except json.JSONDecodeError as exc:
        raise ReplayError("replay manifest is not valid JSON", code="replay_manifest_invalid") from exc
    if not isinstance(raw, dict):
        raise ReplayError("replay manifest must be an object", code="replay_manifest_invalid")

    schema = _require_str(raw, "schema")
    if schema != MANIFEST_SCHEMA:
        raise ReplayError(
            f"replay manifest schema mismatch: {schema!r}",
            code="replay_manifest_schema_mismatch",
        )
    generator = _require_mapping(raw, "generator")
    model = _require_mapping(raw, "model")
    dimension = _require_int(model, "dimension", minimum=1)
    corpus = _require_mapping(raw, "corpus")
    queries = _require_mapping(raw, "queries")
    vectors = _require_mapping(raw, "vectors")
    if _require_str(corpus, "input_type") not in INPUT_TYPES:
        raise ReplayError("replay manifest corpus input_type is invalid", code="replay_manifest_invalid")
    if _require_str(queries, "input_type") not in INPUT_TYPES:
        raise ReplayError("replay manifest queries input_type is invalid", code="replay_manifest_invalid")
    model_id = _require_str(model, "id")
    model_revision = _require_str(model, "revision")
    declared_key_identity = _require_str(model, "key_identity")
    expected_key_identity = model_identity_fingerprint(model_id, model_revision)
    if declared_key_identity != expected_key_identity:
        raise ReplayError(
            "replay manifest model key identity does not match model id/revision",
            code="replay_model_identity_mismatch",
        )
    prompt_identity = model.get("prompt_identity")
    if (
        not isinstance(prompt_identity, dict)
        or not isinstance(prompt_identity.get("query"), str)
        or not isinstance(prompt_identity.get("document"), str)
    ):
        raise ReplayError(
            "replay manifest model.prompt_identity is invalid",
            code="replay_manifest_invalid",
        )
    return ReplayManifest(
        schema=schema,
        generator_name=_require_str(generator, "name"),
        generator_revision=_require_str(generator, "revision"),
        model_id=model_id,
        model_revision=model_revision,
        model_key_identity=declared_key_identity,
        dimension=dimension,
        dtype=_require_str(model, "dtype"),
        normalized=bool(model.get("normalized", False)),
        prompt_identity={"query": prompt_identity["query"], "document": prompt_identity["document"]},
        corpus_version=_require_int(corpus, "version"),
        query_version=_require_int(queries, "version"),
        corpus_input_type=_require_str(corpus, "input_type"),
        query_input_type=_require_str(queries, "input_type"),
        corpus_sha256=_require_str(corpus, "sha256"),
        query_sha256=_require_str(queries, "sha256"),
        vectors_sha256=_require_str(vectors, "sha256"),
        vector_count=_require_int(vectors, "count"),
        key_format=_require_str(vectors, "key_format"),
    )


class ReplayEmbedder:
    """Embed texts by exact composite-key lookup into a versioned fixture.

    Unknown texts, dimension mismatches, duplicate/non-finite keys, and
    manifest drift fail closed and never produce a guessed vector.
    """

    def __init__(self, manifest: ReplayManifest, vectors: dict[str, list[float]]):
        self._manifest = manifest
        self._vectors = vectors

    @classmethod
    def from_files(cls, manifest_path: Path, vectors_path: Path) -> ReplayEmbedder:
        manifest = load_manifest(manifest_path)
        actual = file_sha256(vectors_path)
        if manifest.vectors_sha256 and actual != manifest.vectors_sha256:
            raise ReplayError(
                "replay vector fixture does not match its manifest hash",
                code="replay_manifest_hash_mismatch",
            )
        try:
            vectors = json.loads(
                vectors_path.read_text(encoding="utf-8"),
                object_pairs_hook=_reject_duplicate_keys,
            )
        except ReplayError:
            raise
        except OSError as exc:
            raise ReplayError("replay vector fixture cannot be read", code="replay_vectors_unreadable") from exc
        except json.JSONDecodeError as exc:
            raise ReplayError("replay vector fixture is not valid JSON", code="replay_vectors_invalid") from exc
        if not isinstance(vectors, dict):
            raise ReplayError("replay vector fixture must be an object", code="replay_vectors_invalid")
        return cls(manifest, _validate_vectors(manifest, vectors))

    @property
    def manifest(self) -> ReplayManifest:
        return self._manifest

    def embed(self, text: str, *, input_type: str = "document") -> list[float]:
        if not isinstance(text, str):
            raise ReplayError("replay embedding input must be text", code="invalid_replay_input")
        key = composite_key(self._manifest.model_id, self._manifest.model_revision, input_type, text)
        vector = self._vectors.get(key)
        if vector is None:
            raise ReplayError(
                f"no replayed vector for the given text (unknown or drifted) [{input_type}]",
                code="replay_vector_missing",
            )
        if len(vector) != self._manifest.dimension:
            raise ReplayError(
                "replayed vector dimension does not match the manifest",
                code="replay_dimension_mismatch",
            )
        if not all(isinstance(value, (int, float)) and math.isfinite(float(value)) for value in vector):
            raise ReplayError(
                "replayed vector contains non-finite or non-numeric values",
                code="replay_dimension_mismatch",
            )
        return [float(value) for value in vector]


def _validate_vectors(manifest: ReplayManifest, vectors: dict[str, Any]) -> dict[str, list[float]]:
    """Validate every fixture vector and reject malformed entries.

    Each key must be ``<modelfp>:<input_type>:sha256(text)`` where the model
    fingerprint matches the manifest's declared model identity.  A fixture
    produced by a different model (or revision) fails closed rather than being
    silently consumed.
    """
    expected_model_fp = model_identity_fingerprint(manifest.model_id, manifest.model_revision)
    seen: set[str] = set()
    validated: dict[str, list[float]] = {}
    for key, value in vectors.items():
        parts = key.split(":") if isinstance(key, str) else []
        if len(parts) != 3:
            raise ReplayError(
                "replay vector fixture contains an invalid composite key",
                code="replay_vectors_invalid",
            )
        model_fp, input_type, _ = parts
        if model_fp != expected_model_fp:
            raise ReplayError(
                "replay vector fixture model identity does not match the manifest",
                code="replay_model_identity_mismatch",
            )
        if input_type not in INPUT_TYPES:
            raise ReplayError(
                "replay vector fixture contains an unknown input_type",
                code="replay_vectors_invalid",
            )
        if key in seen:
            raise ReplayError(
                "replay vector fixture contains a duplicate key",
                code="replay_vectors_duplicate_key",
            )
        seen.add(key)
        if not isinstance(value, list) or len(value) != manifest.dimension:
            raise ReplayError(
                "replay vector fixture has a dimension mismatch",
                code="replay_dimension_mismatch",
            )
        if not all(isinstance(item, (int, float)) and math.isfinite(float(item)) for item in value):
            raise ReplayError(
                "replay vector fixture contains non-finite or non-numeric values",
                code="replay_vectors_invalid",
            )
        validated[key] = [float(item) for item in value]
    if manifest.vector_count and len(validated) != manifest.vector_count:
        raise ReplayError(
            "replay vector fixture count does not match the manifest",
            code="replay_vectors_invalid",
        )
    return validated
