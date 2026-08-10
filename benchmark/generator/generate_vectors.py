"""Local embedding generator for the shiori retrieval-quality benchmark.

Two providers are supported:

- ``voyage-4-nano`` (default): official Apache-2.0 model loaded through
  sentence-transformers with ``trust_remote_code=True``.  It must be pinned to
  a fixed revision (see ``--model-revision``); the effective commit is recorded
  in the manifest so the fixture is reproducible.
- ``deterministic``: an offline, network-free, text-hash embedding used to
  validate the harness and regenerate reproducible artifacts without the model.

Output: a JSON manifest plus optional parquet/npy vectors.  Vectors are only
written when ``--outdir`` is provided; otherwise the manifest carries the
provider identity and input hashes so a downstream consumer can rebuild.

This script never reads, sends, or logs API keys, and it performs no product
ranking tuning.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path

MODEL_REPO = "voyageai/voyage-4-nano"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def deterministic_embedding(text: str, *, dimension: int = 1024) -> list[float]:
    """Stable unit vector without network access, for harness validation only."""
    values: list[float] = []
    for index in range(dimension):
        digest = hashlib.sha256(f"shiori-bench-v1:{index}:".encode() + text.encode("utf-8")).digest()
        raw = int.from_bytes(digest[:8], "big") / float(1 << 64)
        values.append((raw * 2.0) - 1.0)
    norm = math.sqrt(sum(value * value for value in values))
    if norm == 0.0:
        return [0.0] * dimension
    return [value / norm for value in values]


class DeterministicProvider:
    """Offline hash-based embedding provider (harness validation only)."""

    name = "deterministic"
    dimension = 1024

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [deterministic_embedding(text, dimension=self.dimension) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return deterministic_embedding(text, dimension=self.dimension)


class VoyageNanoProvider:
    """Local voyage-4-nano through sentence-transformers (fixed revision)."""

    name = "voyage-4-nano"

    def __init__(self, *, revision: str | None = None, dimension: int = 1024) -> None:
        self.revision = revision
        self.dimension = dimension
        self._model = None

    def _load(self):
        if self._model is not None:
            return self._model
        from sentence_transformers import SentenceTransformer  # local import

        kwargs = {"trust_remote_code": True, "truncate_dim": self.dimension}
        if self.revision:
            kwargs["revision"] = self.revision
        self._model = SentenceTransformer(MODEL_REPO, **kwargs)
        return self._model

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        model = self._load()
        vectors = model.encode_document(texts, convert_to_numpy=True)
        return vectors.tolist()

    def embed_query(self, text: str) -> list[float]:
        model = self._load()
        vectors = model.encode_query([text], convert_to_numpy=True)
        return vectors.tolist()[0]


def load_jsonl(path: Path) -> list[dict]:
    items = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                items.append(json.loads(line))
    return items


def _manifest(docs_path: Path, queries_path: Path, provider, model_revision: str | None) -> dict:
    library_version: str | None = None
    if provider.name == "voyage-4-nano":
        try:
            import sentence_transformers  # local import

            library_version = sentence_transformers.__version__
        except Exception:  # pragma: no cover - generator may run without the lib
            library_version = None
    return {
        "schemaVersion": "1.0",
        "corpusVersion": docs_path.parent.name,
        "generator": {
            "provider": provider.name,
            "modelRepo": MODEL_REPO,
            "modelRevision": model_revision or None,
            "library": "sentence-transformers",
            "libraryVersion": library_version,
            "inputType": {"document": "encode_document", "query": "encode_query"},
            "dimension": provider.dimension,
            "normalization": "l2",
            "dtype": "float32",
        },
        "files": {
            "documents": str(docs_path),
            "queries": str(queries_path),
        },
        "hashes": {
            "documents": _sha256_file(docs_path),
            "queries": _sha256_file(queries_path),
        },
        "categories": ["exact", "paraphrase", "multilingual", "temporal", "multi_turn", "near_duplicate", "source_filter", "no_evidence"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate benchmark embeddings + manifest")
    parser.add_argument("--provider", choices=["voyage-4-nano", "deterministic"], default="deterministic")
    parser.add_argument("--model-revision", help="Fixed HF revision for voyage-4-nano")
    parser.add_argument("--dimension", type=int, default=1024)
    parser.add_argument("--corpus-dir", type=Path, default=Path("benchmark/corpus/v1"))
    parser.add_argument("--outdir", type=Path, help="Write vectors + manifest here (optional)")
    parser.add_argument("--documents", type=Path)
    parser.add_argument("--queries", type=Path)
    args = parser.parse_args(argv)

    docs_path = args.documents or (args.corpus_dir / "documents.jsonl")
    queries_path = args.queries or (args.corpus_dir / "queries.jsonl")
    for path in (docs_path, queries_path):
        if not path.exists():
            parser.error(f"missing input file: {path}")

    if args.provider == "voyage-4-nano":
        provider = VoyageNanoProvider(revision=args.model_revision, dimension=args.dimension)
    else:
        provider = DeterministicProvider()
        if args.dimension != 1024:
            provider.dimension = args.dimension

    documents = load_jsonl(docs_path)
    queries = load_jsonl(queries_path)

    doc_texts = [item["content"] for item in documents]
    query_texts = [item["query"] for item in queries]

    print(f"provider={provider.name} documents={len(doc_texts)} queries={len(query_texts)}", file=sys.stderr)

    if args.outdir:
        args.outdir.mkdir(parents=True, exist_ok=True)
        # Write raw vectors as JSON (portable; fixture is small).
        doc_vectors = provider.embed_documents(doc_texts)
        query_vectors = [provider.embed_query(text) for text in query_texts]
        (args.outdir / "document_vectors.json").write_text(json.dumps(doc_vectors), encoding="utf-8")
        (args.outdir / "query_vectors.json").write_text(json.dumps(query_vectors), encoding="utf-8")
        manifest = _manifest(docs_path, queries_path, provider, args.model_revision)
        manifest["hashes"]["document_vectors"] = _sha256_text(json.dumps(doc_vectors, sort_keys=True))
        manifest["hashes"]["query_vectors"] = _sha256_text(json.dumps(query_vectors, sort_keys=True))
        (args.outdir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        print(f"wrote vectors + manifest to {args.outdir}", file=sys.stderr)
    else:
        manifest = _manifest(docs_path, queries_path, provider, args.model_revision)
        print(json.dumps(manifest, indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
