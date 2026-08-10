"""Local-only embedding generation for the task #11 retrieval benchmark.

Generates 1024-dim, float32, L2-normalized embeddings for corpus documents and
queries using the pinned open-weight `voyageai/voyage-4-nano` model via
sentence-transformers `encode_query` / `encode_document` with
`truncate_dim=1024`.

This script is LOCAL-ONLY by contract: it downloads the pinned model into the
local cache (~704 MB) and is never invoked by CI or tests. Generated vectors
are written to the output directory and are NOT committed by default.

Contract (frozen):
- Model revision: 67fabc9bef010dabc5f6024aa1b1b6b93410426f
- truncate_dim=1024, float32, L2 normalization, encode_query/encode_document
- The 1024 dimension is an explicit fixed value (HF card default is 2048).
- No Voyage API; no credentials.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

# Make the `benchmark` package importable when run as a direct script.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

MODEL_ID = "voyageai/voyage-4-nano"
MODEL_REVISION = "67fabc9bef010dabc5f6024aa1b1b6b93410426f"
TRUNCATE_DIM = 1024
SCHEMA_VERSION = "1"


def _read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local voyage-4-nano embedding generator (task #11)")
    parser.add_argument("--fixtures", required=True, type=Path, help="benchmark/fixtures directory")
    parser.add_argument("--out", required=True, type=Path, help="output directory for vectors/manifest")
    args = parser.parse_args(argv)

    corpus_path = args.fixtures / "corpus.jsonl"
    judgments_path = args.fixtures / "judgments.jsonl"
    if not corpus_path.exists() or not judgments_path.exists():
        parser.error(f"missing corpus/judgments fixtures in {args.fixtures}")
    args.out.mkdir(parents=True, exist_ok=True)

    corpus = _read_jsonl(corpus_path)
    judgments = _read_jsonl(judgments_path)

    # Import lazily so the script fails clearly when the benchmark deps are absent.
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        print(
            "sentence-transformers is not installed; install benchmark/requirements.lock "
            "in an isolated venv (see benchmark/README.md).",
            file=sys.stderr,
        )
        return 2

    # Deterministic CPU float32 inference: pin device and dtype so repeated runs
    # on the same machine produce byte-identical embeddings.
    model = SentenceTransformer(
        MODEL_ID,
        revision=MODEL_REVISION,
        trust_remote_code=True,
        truncate_dim=TRUNCATE_DIM,
        device="cpu",
    )

    # Documents: use encode_document (task-specific prompt).
    doc_texts = [doc["content"] for doc in corpus]
    doc_embeddings = model.encode_document(
        doc_texts,
        truncate_dim=TRUNCATE_DIM,
        precision="float32",
        normalize_embeddings=True,
        show_progress_bar=False,
    ).tolist()

    # Queries: use encode_query (task-specific prompt) on the SAME canonical
    # query text the live harness uses (shared renderer).
    from benchmark.query_rendering import render_canonical_query

    query_texts = [render_canonical_query(judgment) for judgment in judgments]
    query_embeddings = model.encode_query(
        query_texts,
        truncate_dim=TRUNCATE_DIM,
        precision="float32",
        normalize_embeddings=True,
        show_progress_bar=False,
    ).tolist()

    vectors = {
        "documents": [
            {"id": doc["id"], "embedding": emb}
            for doc, emb in zip(corpus, doc_embeddings, strict=True)
        ],
        "queries": [
            {"query_id": judgment["query_id"], "embedding": emb}
            for judgment, emb in zip(judgments, query_embeddings, strict=True)
        ],
    }
    vectors_path = args.out / "vectors.json"
    vectors_path.write_text(json.dumps(vectors), encoding="utf-8")

    import platform

    try:
        import sentence_transformers as st
        import torch

        st_version = st.__version__
        torch_version = torch.__version__
    except Exception:  # pragma: no cover
        st_version = "unknown"
        torch_version = "unknown"

    manifest = {
        "manifest_version": SCHEMA_VERSION,
        "model": {"id": MODEL_ID, "revision": MODEL_REVISION},
        "prompt_identity": {"query": "encode_query", "document": "encode_document"},
        "embedding": {
            "dim": TRUNCATE_DIM,
            "dtype": "float32",
            "normalization": "L2",
            "dim_is_fixed": True,
            "note": "HF model card open-weight default is 2048; Voyage API docs list 1024 default. "
            "1024 is an explicit fixed value for this benchmark.",
            "determinism": {
                "device": "cpu",
                "note": "Byte-identical vectors are guaranteed on the same machine/hardware with "
                "pinned library versions. Cross-hardware numeric variation is expected; do not "
                "generalize a single-machine bitwise hash into a cross-platform guarantee.",
            },
        },
        "files": {
            "corpus.jsonl": {"sha256": _sha256(corpus_path), "rows": len(corpus)},
            "judgments.jsonl": {"sha256": _sha256(judgments_path), "rows": len(judgments)},
            "corpus_schema.json": {"sha256": _sha256(args.fixtures.parent / "corpus_schema.json")},
            "generate_vectors.py": {"sha256": _sha256(Path(__file__).resolve())},
            "run_benchmark.py": {"sha256": _sha256(Path(__file__).parent / "run_benchmark.py")},
            "query_rendering.py": {"sha256": _sha256(Path(__file__).parent / "query_rendering.py")},
            "vector_validation.py": {"sha256": _sha256(Path(__file__).parent / "vector_validation.py")},
            "requirements.lock": {"sha256": _sha256(Path(__file__).parent / "requirements.lock")},
            "vectors.json": {"sha256": _sha256(vectors_path), "rows": len(doc_embeddings) + len(query_embeddings)},
        },
        "libraries": {"sentence_transformers": st_version, "torch": torch_version},
        "os": {"platform": platform.platform(), "python": platform.python_version()},
    }
    manifest_path = args.out / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")

    print(f"wrote {vectors_path}")
    print(f"wrote {manifest_path}")
    print(f"documents={len(doc_embeddings)} queries={len(query_embeddings)} dim={TRUNCATE_DIM}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
