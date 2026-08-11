# Shiori Phase 4E3 Provenance-Preserving Dedup Report (72 development queries)

_Measurement-only. No acceptance thresholds. Holdout (48) untouched. Public datasets not run._

- base SHA: `3040125e2fd93b4b270cefdde03d30cc3bfb637f`
- model: `voyageai/voyage-4-nano@67fabc9bef010dabc5f6024aa1b1b6b93410426f` dim=1024 float32 L2
- dev queries: 72 (id set sha256 `c4ae751baeb3ac7d…`)
- result file sha256: `91cf669144daef112309895324f17f23bc4063acc5c740d73ffcf451e02796a9`
- runtime: python 3.13.15, psycopg2 2.9.12 (dt dec pq3 ext lo64), PostgreSQL 17.10 (Debian 17.10-1.pgdg12+1), pgvector 0.8.6

## Overall (per config, n=72)

| config | candR@20 | R@5 | MRR@10 | nDCG@10 | filter_leak | dupCov | dupRate | dedupDrop | covRisk | noevQ | noevFR |
|---|---|---|---|---|---|---|---|---|---|---|---|
| dense-only | 1.000 | 1.000 | 0.939 | 0.943 | 9 | 1.000 | 0.100 | N/A | 0 | 9 | 9 |
| lexical-only | 0.553 | 0.553 | 0.587 | 0.547 | 1 | 1.000 | 0.167 | N/A | 0 | 9 | 0 |
| rrf | 1.000 | 1.000 | 0.942 | 0.940 | 9 | 1.000 | 0.100 | N/A | 0 | 9 | 9 |
| +exact | 1.000 | 1.000 | 0.942 | 0.940 | 9 | 1.000 | 0.100 | N/A | 0 | 9 | 9 |
| +temporal | 1.000 | 0.992 | 0.950 | 0.936 | 9 | 1.000 | 0.100 | N/A | 0 | 9 | 9 |
| +dedup | 1.000 | 0.960 | 0.950 | 0.910 | 9 | 1.000 | 0.067 | 0.053 | 5 | 9 | 9 |

## Per-bucket nDCG@10 / R@5

| bucket | dense-only R5 | +temporal R5 | +dedup R5 | dense nDCG | +temporal nDCG | +dedup nDCG |
|---|---|---|---|---|---|---|
| exact | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 |
| paraphrase | 1.000 | 1.000 | 1.000 | 0.981 | 0.977 | 0.977 |
| multilingual | 1.000 | 1.000 | 0.917 | 0.895 | 0.907 | 0.855 |
| temporal | 1.000 | 0.944 | 0.944 | 0.883 | 0.853 | 0.853 |
| multi_turn | 1.000 | 1.000 | 0.963 | 0.878 | 0.849 | 0.807 |
| duplicate | 1.000 | 1.000 | 0.944 | 0.997 | 0.997 | 0.943 |
| no_evidence | N/A | N/A | N/A | N/A | N/A | N/A |
| filter | 1.000 | 1.000 | 0.963 | 1.000 | 1.000 | 0.965 |

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
| dense-only | 20.252 | 23.568 |
| lexical-only | 13.780 | 15.195 |
| rrf | 20.805 | 24.214 |
| +exact | 18.622 | 22.866 |
| +temporal | 20.499 | 22.974 |
| +dedup | 34.037 | 37.571 |

### +dedup per-stage

| stage | p50 (ms) | p95 (ms) |
|---|---|---|
| dedup | 11.884 | 13.743 |
| dense | 5.699 | 7.779 |
| exact | 0.000 | 0.599 |
| rrf | 0.040 | 0.058 |
| temporal | 0.000 | 0.000 |
| trigram | 0.767 | 1.069 |
| ts_rank_cd | 0.560 | 1.825 |

## Adapters (not run)

| dataset | status | note |
|---|---|---|
| longmemeval | local_only | user-supplied local data; raw/derived rows not committed; redistribution=unresolved |
| miracl | adapter_only | not_run / not_comparable_to_official; no corpus download, no committed topics/qrels |
| nfcorpus | local_only | official archive is user-supplied local-only (MD5 pinned a89dba18…); not run in this measurement |

## Known gaps

- Phase 4E3 changes only dedup: byte-identical content is collapsed only within exact session/source provenance while the >0.85 cosine guard remains unchanged.
- Coverage risk decreases from 12 to 5; the remaining five relevant drops are byte-identical doc-0017 with same-provenance doc-0018 kept.
- q-0039 doc-0002 is recovered; q-0042 keeps doc-0019 and representative doc-0018 while byte-identical doc-0017 is folded.
- The first five configs preserve metrics and projected stage/doc_id/rank/reason traces; latency changed, and q-0057 temporal score magnitude drifts with evaluation time.
- No-evidence false returns remain 9; counterfactual source/session/time values 9/9/3 are not active-filter leakage.

