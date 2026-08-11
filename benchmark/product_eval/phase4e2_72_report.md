Shiori Phase 4E2 Intent-Gated Temporal Decay Report (72 development queries)

_Measurement-only. No acceptance thresholds. Holdout (48) untouched. Public datasets not run._

- base SHA: `e2696653b119220de75e1e1fec49e6063e24fbd9`
- model: `voyageai/voyage-4-nano@67fabc9bef010dabc5f6024aa1b1b6b93410426f` dim=1024 float32 L2
- dev queries: 72 (id set sha256 `c4ae751baeb3ac7d…`)
- result file sha256: `d37ce61fda0dcedcf835769a1b3e64fb3fb17ed60abc88e59ff743fd8849d28e`
- runtime: python 3.13.15, psycopg2 2.9.12 (dt dec pq3 ext lo64), PostgreSQL 17.10 (Debian 17.10-1.pgdg12+1), pgvector 0.8.6

## Overall (per config, n=72)

| config | candR@20 | R@5 | MRR@10 | nDCG@10 | filter_leak | dupCov | dupRate | dedupDrop | covRisk | noevQ | noevFR |
|---|---|---|---|---|---|---|---|---|---|---|---|
| dense-only | 1.000 | 1.000 | 0.939 | 0.943 | 9 | 1.000 | 0.100 | N/A | 0 | 9 | 9 |
| lexical-only | 0.553 | 0.553 | 0.587 | 0.547 | 1 | 1.000 | 0.167 | N/A | 0 | 9 | 0 |
| rrf | 1.000 | 1.000 | 0.942 | 0.940 | 9 | 1.000 | 0.100 | N/A | 0 | 9 | 9 |
| +exact | 1.000 | 1.000 | 0.942 | 0.940 | 9 | 1.000 | 0.100 | N/A | 0 | 9 | 9 |
| +temporal | 1.000 | 0.992 | 0.950 | 0.936 | 9 | 1.000 | 0.100 | N/A | 0 | 9 | 9 |
| +dedup | 1.000 | 0.899 | 0.942 | 0.881 | 9 | 1.000 | 0.000 | 0.313 | 12 | 9 | 9 |

## Per-bucket nDCG@10 / R@5

| bucket | dense-only R5 | +temporal R5 | +dedup R5 | dense nDCG | +temporal nDCG | +dedup nDCG |
|---|---|---|---|---|---|---|
| exact | 1.000 | 1.000 | 0.944 | 1.000 | 1.000 | 0.976 |
| paraphrase | 1.000 | 1.000 | 0.963 | 0.981 | 0.977 | 0.961 |
| multilingual | 1.000 | 1.000 | 0.833 | 0.895 | 0.907 | 0.803 |
| temporal | 1.000 | 0.944 | 0.833 | 0.883 | 0.853 | 0.833 |
| multi_turn | 1.000 | 1.000 | 0.907 | 0.878 | 0.849 | 0.754 |
| duplicate | 1.000 | 1.000 | 0.944 | 0.997 | 0.997 | 0.948 |
| no_evidence | N/A | N/A | N/A | N/A | N/A | N/A |
| filter | 1.000 | 1.000 | 0.907 | 1.000 | 1.000 | 0.942 |

## Filter leakage by tag (per config)

| config | source_filter | session_filter | time_filter |
|---|---|---|---|
| dense-only | 9 | 9 | 3 |
| lexical-only | 1 | 0 | 0 |
| rrf | 9 | 9 | 3 |
| +exact | 9 | 9 | 3 |
| +temporal | 9 | 9 | 3 |
| +dedup | 9 | 9 | 3 |

## Temporal transitions (knowledge-update eligible)

| qid | pre_rank | post_rank | rank_changed | winner_transition | promoted_to_winner |
|---|---|---|---|---|---|
| q-0009 | 2 | 2 | False | False | False |
| q-0024 | 1 | 1 | False | False | False |
| q-0055 | 1 | 1 | False | False | False |
| q-0056 | 2 | 2 | False | False | False |
| q-0057 | 2 | 1 | True | True | True |
| q-0058 | 1 | 1 | False | False | False |
| q-0074 | 2 | 2 | False | False | False |
| q-0117 | 1 | 1 | False | False | False |

## No-evidence behavior (per config)

| config | queries | false_return | abstention_like |
|---|---|---|---|
| dense-only | 9 | 9 | 0 |
| lexical-only | 9 | 0 | 9 |
| rrf | 9 | 9 | 0 |
| +exact | 9 | 9 | 0 |
| +temporal | 9 | 9 | 0 |
| +dedup | 9 | 9 | 0 |

## Latency (real PostgreSQL, n=72)

### e2e

| config | p50 (ms) | p95 (ms) |
|---|---|---|
| dense-only | 16.049 | 18.643 |
| lexical-only | 12.834 | 13.949 |
| rrf | 19.662 | 22.503 |
| +exact | 19.236 | 22.022 |
| +temporal | 19.089 | 21.409 |
| +dedup | 36.842 | 41.056 |

### +dedup per-stage

| stage | p50 (ms) | p95 (ms) |
|---|---|---|
| dedup | 16.005 | 18.988 |
| dense | 5.506 | 6.918 |
| exact | 0.000 | 0.529 |
| rrf | 0.035 | 0.042 |
| temporal | 0.000 | 0.000 |
| trigram | 0.692 | 1.070 |
| ts_rank_cd | 0.488 | 1.512 |

## Adapters (not run)

| dataset | status | note |
|---|---|---|
| longmemeval | local_only | user-supplied local data; raw/derived rows not committed; redistribution=unresolved |
| miracl | adapter_only | not_run / not_comparable_to_official; no corpus download, no committed topics/qrels |
| nfcorpus | local_only | official archive is user-supplied local-only (MD5 pinned a89dba18…); not run in this measurement |

## Known gaps

- q-0057: grade-3 doc-0011 drops from rank 3 to rank 12 with Recall@5=1/2 while grade-2 doc-0012 reaches rank 1; frozen decay formula risk on a composite latest query.
- q-0086: grade-2 doc-0021 moves from rank 3 to rank 4 and duplicate nDCG@10 drops 1.0 -> 0.997316; a deterministic minor regression, not tie/noise.
- source/session/time 9/9/3 is an unfiltered counterfactual trace metric, not a Phase 4E1 active-filter regression; active filters remain 0/0/0.

