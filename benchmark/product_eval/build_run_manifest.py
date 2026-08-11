"""Generate the Phase 4D baseline_72 run manifest (task #18).

Records exact base SHA, runner/schema versions, model identity, the 72-dev id
set + hash, input file hashes, result hash, library versions, and adapter
not_run statuses. No local paths, DSNs, or keys are recorded.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sys
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from benchmark.product_eval.identity import EMBED_DIM, MODEL_ID, MODEL_REVISION  # noqa: E402


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate baseline_72 run manifest")
    parser.add_argument("--base-sha", required=True, help="exact live main SHA the run started from")
    parser.add_argument("--results", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path, help="baseline_72_report.md")
    parser.add_argument("--dev-vectors", required=True, type=Path)
    parser.add_argument("--doc-id-map", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path, help="dataset_manifest.json")
    parser.add_argument("--evidence-ledger", required=True, type=Path)
    parser.add_argument("--golden-rows", required=True, type=Path)
    parser.add_argument("--schema", required=True, type=Path, help="dataset_manifest.schema.json")
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--judgments", required=True, type=Path)
    parser.add_argument("--corpus-schema", required=True, type=Path)
    parser.add_argument("--pg-version", required=True, help="PostgreSQL server version")
    parser.add_argument("--pgvector-version", required=True, help="pgvector extension version")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)

    results = json.loads(args.results.read_text(encoding="utf-8"))
    dev_ids = sorted(results["smoke_query_ids"])
    dev_set_hash = _sha256_hex("\n".join(dev_ids) + "\n")

    # Committed inputs: present in the repo, CI can recompute their hashes.
    committed_inputs = {
        "golden_queries.jsonl": _sha256(args.golden_rows),
        "dataset_manifest.json": _sha256(args.manifest),
        "evidence_ledger.json": _sha256(args.evidence_ledger),
        "dataset_manifest.schema.json": _sha256(args.schema),
        "fixtures/corpus.jsonl": _sha256(args.corpus),
        "fixtures/judgments.jsonl": _sha256(args.judgments),
        "corpus_schema.json": _sha256(args.corpus_schema),
    }

    # Local-run inputs: generated locally (ignored, not committed); CI cannot
    # recompute them without the model cache. Declared with hash + generator
    # contract only; they never make CI claims of reproducibility.
    local_run_inputs = {
        "dev_query_vectors.json": {
            "committed": False,
            "sha256": _sha256(args.dev_vectors),
            "generator": "tools/generate_dev_query_vectors.py",
            "generator_version": "1",
            "purpose": "offline 72-dev query embeddings for the baseline run (pinned voyage-4-nano)",
        },
        "doc_id_map.json": {
            "committed": False,
            "sha256": _sha256(args.doc_id_map),
            "generator": "tools/phase4d_ingest_corpus.py",
            "generator_version": "1",
            "purpose": "DB uuid -> fixture doc id mapping produced by local corpus ingest",
        },
    }

    manifest = {
        "manifest_version": "2",
        "base_sha": args.base_sha,
        "runner": {"name": "benchmark.product_eval.runner", "version": "1", "configs": sorted(results["configs"].keys())},
        "postprocess": {"name": "benchmark.product_eval.postprocess", "version": "1"},
        "report_generator": {"name": "benchmark.product_eval.build_report", "version": "1"},
        "schema": {"dataset_manifest_schema": "1", "corpus_schema": "2", "results_schema": "1"},
        "model": {
            "id": MODEL_ID,
            "revision": MODEL_REVISION,
            "identity": f"{MODEL_ID}@{MODEL_REVISION}",
            "dim": EMBED_DIM,
            "dtype": "float32",
            "normalization": "L2",
            "embedding_provider": "replay-local",
            "query_encode": "encode_query",
            "document_encode": "encode_document",
        },
        "dev_set": {
            "query_count": len(dev_ids),
            "query_ids": dev_ids,
            "id_set_sha256": dev_set_hash,
        },
        "committed_inputs_sha256": committed_inputs,
        "local_run_inputs": local_run_inputs,
        "result_file_sha256": _sha256(args.results),
        "report_file_sha256": _sha256(args.report),
        "runtime": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "psycopg2": str(getattr(psycopg2, "__version__", "unknown")),
            "postgresql": args.pg_version,
            "pgvector": args.pgvector_version,
        },
        "adapters_not_run": {
            "longmemeval": {"status": "local_only", "not_run_reason": "user-supplied local data; raw/derived rows not committed; redistribution=unresolved"},
            "nfcorpus": {"status": "local_only", "not_run_reason": "official archive is user-supplied local-only (MD5 pinned a89dba18…); not run in this measurement"},
            "miracl": {"status": "adapter_only", "not_run_reason": "not_run / not_comparable_to_official; no corpus download, no committed topics/qrels"},
        },
    }

    args.out.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    print(f"wrote {args.out.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
