# Phase 4D product_eval (task #18)

Measurement-only evaluation of the PRODUCTION shiori search pipeline via the
behavior-preserving ablation seam. No default ranking/model/threshold/schema
changes. Local-only datasets; no API keys; CI is network-free.

## Frozen boundaries

- Development/holdout split: exactly **72 development (tune)** and **48
  holdout** query ids (frozen per-bucket 60/40). Holdout is NEVER viewed or
  generated in any run/report.
- Public datasets (LongMemEval, NFCorpus, MIRACL) are **local-only
  user-supplied**; raw/derived rows are never committed; redistribution is
  `unresolved`/`not_run` where the chain is not closed.
- Model identity (single source in `benchmark/product_eval/identity.py`):
  `voyageai/voyage-4-nano@67fabc9bef010dabc5f6024aa1b1b6b93410426f`,
  dim 1024, float32, L2, `encode_query`/`encode_document`.

## Reproducibility commands

All commands are local-only. CI runs only the offline tests (no model, no
network, no DB, no `.generated`).

### 1. task #11 document vectors (local model cache)

```bash
uv venv .venv-benchmark
uv pip install --python .venv-benchmark/bin/python -r benchmark/requirements.lock
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
  .venv-benchmark/bin/python benchmark/generate_vectors.py \
  --fixtures benchmark/fixtures --out benchmark/.generated
```

### 2. 72 development query vectors (local model cache, frozen split)

```bash
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
  .venv-benchmark/bin/python tools/generate_dev_query_vectors.py \
  --manifest benchmark/product_eval/dataset_manifest.json \
  --rows benchmark/product_eval/golden_queries.jsonl \
  --out benchmark/.generated/dev_query_vectors.json
```

The generator selects EXACTLY the 72 development ids; any holdout/extra/missing
id fails closed. Rebuilding on the same machine reproduces
`dev_query_vectors.json` byte-for-byte.

### 3. Isolated PostgreSQL: migrate + ingest (doc map)

Start the pgvector container, create the isolated DB, then:

```bash
uv run shiori db migrate   # with SHIORI_DATABASE_DSN set
uv run python tools/phase4d_ingest_corpus.py \
  --dsn "$SHIORI_DATABASE_DSN" \
  --corpus benchmark/fixtures/corpus.jsonl \
  --vectors benchmark/.generated/vectors.json
```

This writes `benchmark/.generated/doc_id_map.json` (DB uuid -> fixture doc id).

### 4. Run the 72-dev baseline (real PostgreSQL)

`--dev-limit 72` selects the frozen 72 development ids; `--query-ids` is NOT
needed for the full baseline (the runner's exact-key closure already enforces
the dev-only embedding set).

```bash
uv run python benchmark/product_eval/runner.py \
  --manifest benchmark/product_eval/dataset_manifest.json \
  --rows benchmark/product_eval/golden_queries.jsonl \
  --dev-limit 72 \
  --embedding-json benchmark/.generated/dev_query_vectors.json \
  --doc-id-map benchmark/.generated/doc_id_map.json \
  --out benchmark/product_eval/baseline_72_results.json
```

The runner enforces exact dev-only embedding-key closure, the frozen 6-config
matrix, strict return/trace consistency, and writes sanitized stable-ID traces.

### 5. Deterministic post-process (no DB rerun)

```bash
uv run python benchmark/product_eval/postprocess.py \
  --results benchmark/product_eval/baseline_72_results.json \
  --manifest benchmark/product_eval/dataset_manifest.json \
  --corpus benchmark/fixtures/corpus.jsonl \
  --out benchmark/product_eval/baseline_72_results.json
```

Adds per-tag (source/session/time) filter leakage from the existing traces.

### 6. Build run manifest + Markdown report

Exact repo-relative flags (run from the repo root):

```bash
uv run python benchmark/product_eval/build_run_manifest.py \
  --base-sha "$(git rev-parse HEAD)" \
  --results benchmark/product_eval/baseline_72_results.json \
  --report benchmark/product_eval/baseline_72_report.md \
  --dev-vectors benchmark/.generated/dev_query_vectors.json \
  --doc-id-map benchmark/.generated/doc_id_map.json \
  --manifest benchmark/product_eval/dataset_manifest.json \
  --evidence-ledger benchmark/product_eval/evidence_ledger.json \
  --golden-rows benchmark/product_eval/golden_queries.jsonl \
  --schema benchmark/product_eval/dataset_manifest.schema.json \
  --corpus benchmark/fixtures/corpus.jsonl \
  --judgments benchmark/fixtures/judgments.jsonl \
  --corpus-schema benchmark/corpus_schema.json \
  --pg-version "17.10 (Debian 17.10-1.pgdg12+1)" \
  --pgvector-version 0.8.6 \
  --out benchmark/product_eval/baseline_72_manifest.json

uv run python benchmark/product_eval/build_report.py \
  --results benchmark/product_eval/baseline_72_results.json \
  --manifest benchmark/product_eval/baseline_72_manifest.json \
  --out benchmark/product_eval/baseline_72_report.md
```

`--pg-version` / `--pgvector-version` can be obtained from the live isolated
database via:

```bash
docker exec <pg-container> psql -U <user> -d postgres -tAc "SELECT version();"
docker exec <pg-container> psql -U <user> -d postgres -tAc \
  "SELECT extversion FROM pg_extension WHERE extname='vector';"
```

### 7. Offline verification (CI-safe)

```bash
uv run pytest tests/test_product_eval_contract.py \
  tests/test_product_eval_adapters.py tests/test_product_eval_runner.py
```

## Deliverables

Only the latest generation is committed; earlier generations live in git
history and can be regenerated with the commands above (output paths are
examples).

- `phase4e3_72_results.json` — per-config/per-bucket metrics, temporal pairs,
  per-tag filter leakage, per-config sanitized traces, stage/e2e latency.
- `phase4e3_72_manifest.json` — base SHA, model identity, committed/local input
  hashes, result/report hashes, runtime versions, adapter not-run status.
- `phase4e3_72_report.md` — machine-generated Markdown report.
