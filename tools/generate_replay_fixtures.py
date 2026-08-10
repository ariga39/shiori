#!/usr/bin/env python3
"""Generate the versioned replay-embedding fixture for the Phase 4B E2E.

Vectors are produced by a real, pinned embedding model —
``voyageai/voyage-4-nano`` (Apache-2.0, MRL 1024-dim) — run once on this
machine and committed as a versioned fixture.  No hash/PRNG vector is used.
CI only validates and replays the committed vectors; it never runs this
generator, downloads a model, or calls a network/API.

Chunk texts are derived from the exact synthetic session files the E2E harness
ingests, using the same ``[role] content`` extraction ingest applies, so the
replay keys are byte-identical at runtime.  Output files:

- corpus.jsonl / queries.jsonl  (exact chunk/query texts, desensitized)
- vectors.json                 (composite-key -> [1024 floats])
- manifest.json                (schema/model revision/versions/hashes)

Re-running the generator reproduces the same vectors (deterministic model
inference on fixed text), so the manifest hashes are stable in git.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path

MANIFEST_SCHEMA = "shiori-replay-fixture/v1"
MODEL_REVISION = "voyageai/voyage-4-nano@main"
MODEL_ID = "voyage-4-nano"
DIMENSION = 1024
DTYPE = "float32"
NORMALIZED = True
GENERATOR_NAME = "voyage-4-nano-offline"
GENERATOR_REVISION = "2026-08-11-1"
QUERY_PROMPT = "Represent the query for retrieving supporting documents: "
DOCUMENT_PROMPT = "Represent the document for retrieval: "


def stable_text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def model_identity_fingerprint(model_id: str, model_revision: str) -> str:
    """Short fingerprint binding the exact model id + pinned revision."""
    return hashlib.sha256(f"{model_id}|{model_revision}".encode()).hexdigest()[:16]


def composite_key(model_id: str, model_revision: str, input_type: str, text: str) -> str:
    return f"{model_identity_fingerprint(model_id, model_revision)}:{input_type}:{stable_text_hash(text)}"


def chunk_texts_from_sessions(sessions_dir: Path) -> list[str]:
    """Derive the exact chunk texts ingest would embed for these session files.

    Uses ingest's own message extraction and token chunking so the replay keys
    are byte-identical to what ingest embeds at runtime.  The synthetic corpus
    is kept short (well under the chunk window) so each session is a small
    number of stable chunks.
    """
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    import ingest

    texts: list[str] = []
    for path in sorted(sessions_dir.glob("*.jsonl")):
        messages = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        chunks = ingest.chunk_messages(messages, session_id="fixture", source_type="main_user")
        for chunk in chunks:
            texts.append(chunk["content"])
    return texts


def queries() -> list[str]:
    return [
        "when is the Q3 board meeting?",
        "how are schema migrations applied?",
        "what search ranking method is used?",
        "when can we publish the release?",
        "what is the default chunk size?",
        "hardware refresh budget decision",
    ]


def _embed(model, texts: list[str], *, input_type: str) -> list[list[float]]:
    """Embed with the pinned model and the input-type prompt, returning
    normalized 1024-dim float vectors."""
    prompt = QUERY_PROMPT if input_type == "query" else DOCUMENT_PROMPT
    vectors = model.encode(
        [prompt + text for text in texts],
        prompt_name=input_type,
        truncate_dim=DIMENSION,
        normalize_embeddings=True,
        convert_to_numpy=True,
    )
    result = []
    for vector in vectors:
        values = [float(value) for value in vector]
        norm = math.sqrt(sum(v * v for v in values))
        if norm == 0.0:
            raise SystemExit("model produced a zero vector")
        result.append([round(v / norm, 6) for v in values])
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate the offline replay fixture")
    parser.add_argument("--sessions", type=Path, default=Path("tools/e2e-replay-sessions"))
    parser.add_argument("--out", type=Path, default=Path("tests/fixtures/replay"))
    parser.add_argument("--model", type=str, default="voyageai/voyage-4-nano")
    args = parser.parse_args()

    corpus_texts = chunk_texts_from_sessions(args.sessions)
    if not corpus_texts:
        raise SystemExit("no session texts found; refusing an empty corpus")
    query_texts = queries()
    if len(set(corpus_texts)) != len(corpus_texts):
        raise SystemExit("corpus contains duplicate chunk texts; refusing")
    if len(set(query_texts)) != len(query_texts):
        raise SystemExit("query set contains duplicates; refusing")

    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(args.model, trust_remote_code=True)

    # Composite keys: model identity + input_type + canonical text hash.  A
    # document and a query with the same text map to different vectors, and a
    # fixture from a different model identity cannot satisfy the lookup.
    vectors: dict[str, list[float]] = {}
    document_embeds = _embed(model, corpus_texts, input_type="document")
    query_embeds = _embed(model, query_texts, input_type="query")
    for text, vector in zip(corpus_texts, document_embeds, strict=True):
        key = composite_key(MODEL_ID, MODEL_REVISION, "document", text)
        vectors[key] = vector
    for text, vector in zip(query_texts, query_embeds, strict=True):
        key = composite_key(MODEL_ID, MODEL_REVISION, "query", text)
        vectors[key] = vector

    args.out.mkdir(parents=True, exist_ok=True)
    corpus_path = args.out / "corpus.jsonl"
    queries_path = args.out / "queries.jsonl"
    vectors_path = args.out / "vectors.json"
    manifest_path = args.out / "manifest.json"

    corpus_path.write_text("".join(json.dumps(t, ensure_ascii=False) + "\n" for t in corpus_texts), encoding="utf-8")
    queries_path.write_text("".join(json.dumps(t, ensure_ascii=False) + "\n" for t in query_texts), encoding="utf-8")
    vectors_path.write_text(
        json.dumps({key: vectors[key] for key in sorted(vectors)}, separators=(",", ":")),
        encoding="utf-8",
    )

    import importlib.metadata

    def _lib_version(name: str) -> str:
        try:
            return importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            return "unknown"

    manifest = {
        "schema": MANIFEST_SCHEMA,
        "generator": {
            "name": GENERATOR_NAME,
            "revision": GENERATOR_REVISION,
            "model": MODEL_REVISION,
            "model_id": MODEL_ID,
            "library": "sentence-transformers",
            "libraries": {
                "sentence_transformers": _lib_version("sentence-transformers"),
                "transformers": _lib_version("transformers"),
                "torch": _lib_version("torch"),
            },
        },
        "model": {
            "id": MODEL_ID,
            "revision": MODEL_REVISION,
            "dimension": DIMENSION,
            "dtype": DTYPE,
            "normalized": NORMALIZED,
            "query_prompt": QUERY_PROMPT.strip(),
            "document_prompt": DOCUMENT_PROMPT.strip(),
            "key_identity": model_identity_fingerprint(MODEL_ID, MODEL_REVISION),
        },
        "corpus": {
            "version": 1,
            "count": len(corpus_texts),
            "input_type": "document",
            "sha256": hashlib.sha256(corpus_path.read_bytes()).hexdigest(),
        },
        "queries": {
            "version": 1,
            "count": len(query_texts),
            "input_type": "query",
            "sha256": hashlib.sha256(queries_path.read_bytes()).hexdigest(),
        },
        "vectors": {
            "count": len(vectors),
            "sha256": hashlib.sha256(vectors_path.read_bytes()).hexdigest(),
            "key_format": "model_identity_fingerprint:input_type:sha256(text)",
        },
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"fixture written to {args.out} ({len(corpus_texts)} docs, {len(query_texts)} queries)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
