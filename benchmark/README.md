# Shiori Retrieval-Quality Benchmark (Phase 4C / task #11)

Versioned synthetic corpus, graded relevance judgments, a fixed-revision local
embedding generator, and a recomputable retrieval-quality harness for shiori.

## Purpose

Measure embedding+retrieval quality on a controlled, desensitized corpus before
any product-side tuning. The baseline answers: "on queries we can score, how
well does retrieval rank the correct documents?" — split by query category, and
with explicit no-evidence behavior tracking.

This task deliberately does **not** tune chunking, temporal decay, RRF/MMR,
filtering, or confidence logic. It only produces data-supported gap ranking and
recommendations.

## Layout

- `corpus/v1/documents.jsonl` — 36 synthetic documents (desensitized; no real
  PII, emails, private IPs, host paths, or credentials).
- `corpus/v1/queries.jsonl` — 26 graded queries across 8 categories:
  exact, paraphrase, multilingual, temporal, multi_turn, near_duplicate,
  source_filter, no_evidence.
- `generator/generate_vectors.py` — local embedding generator. Supports
  `voyage-4-nano` (sentence-transformers, `trust_remote_code`, pinned revision,
  1024 dim, document/query input types, L2 normalization) and a `deterministic`
  offline provider for reproducible harness validation without the model.
- `generator/verify_fixture.py` — rebuild validation: recomputes hashes over
  corpus + vectors and fails closed on mismatch.
- `harness/metrics.py` — provider-agnostic metrics (Recall@5, MRR@10, nDCG@10,
  retrieval latency, no-evidence behavior, per-category buckets).
- `harness/run_baseline.py` — CLI to embed, rank, and write a baseline report.
- `manifest.schema.json` — JSON schema for generated manifests.
- `reports/<provider>/` — generated manifest, vectors, per-query detail, and
  `baseline.md`.

## Reproduce

```sh
# Deterministic provider (no network/model) — validates the harness:
python benchmark/generator/generate_vectors.py --provider deterministic \
  --corpus-dir benchmark/corpus/v1 --outdir benchmark/reports/deterministic
python benchmark/harness/run_baseline.py --provider deterministic \
  --outdir benchmark/reports/deterministic

# Real local model (fixed revision; requires sentence-transformers + torch):
python benchmark/generator/generate_vectors.py --provider voyage-4-nano \
  --model-revision 67fabc9bef010dabc5f6024aa1b1b6b93410426f \
  --corpus-dir benchmark/corpus/v1 --outdir benchmark/reports/voyage-4-nano
python benchmark/harness/run_baseline.py --provider voyage-4-nano \
  --model-revision 67fabc9bef010dabc5f6024aa1b1b6b93410426f \
  --outdir benchmark/reports/voyage-4-nano

# Rebuild validation (after regenerating vectors):
python benchmark/generator/verify_fixture.py \
  --manifest benchmark/reports/<provider>/manifest.json \
  --documents benchmark/corpus/v1/documents.jsonl \
  --queries benchmark/corpus/v1/queries.jsonl \
  --document-vectors benchmark/reports/<provider>/document_vectors.json \
  --query-vectors benchmark/reports/<provider>/query_vectors.json
```

Raw vector JSON is git-ignored and must be regenerated locally before running
the harness or the fixture verifier. Committed artifacts are the corpus,
graded judgments, manifest (with hashes), and the baseline report + per-query
detail + summary.

## Boundaries

- No API key is read, recorded, or sent.
- The `voyage-4-large` API results stay private; only local `voyage-4-nano`
  outputs (Apache-2.0) may be committed.
- No product ranking/chunking/filter tuning.
- task #10's E2E harness is not modified; only the shared corpus fixture
  contract is aligned.
