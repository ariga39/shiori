# Shiori Phase 4D Baseline Report (72 development queries)

_Measurement-only. No acceptance thresholds. Holdout (48) untouched. Public datasets not run._

- base SHA: `49ab1598ea50cca3f001ad75993eda4896b58e82`
- model: `voyageai/voyage-4-nano@67fabc9bef010dabc5f6024aa1b1b6b93410426f` dim=1024 float32 L2
- dev queries: 72 (id set sha256 `c4ae751baeb3ac7d…`)
- result file sha256: `5192a02e75d93a0b775db9851bae298cc2d2333271c2d533228fac14d70c157c`
- runtime: python 3.13.15, psycopg2 2.9.12 (dt dec pq3 ext lo64), PostgreSQL 17.10 (Debian 17.10-1.pgdg12+1), pgvector 0.8.6

## Overall (per config, n=72)

| config | candR@20 | R@5 | MRR@10 | nDCG@10 | filter_leak | dupCov | dupRate | dedupDrop | covRisk | noevQ | noevFR |
|---|---|---|---|---|---|---|---|---|---|---|---|
| dense-only | 1.000 | 1.000 | 0.939 | 0.943 | 9 | 1.000 | 0.100 | N/A | 0 | 9 | 9 |
| lexical-only | 0.553 | 0.553 | 0.587 | 0.547 | 1 | 1.000 | 0.167 | N/A | 0 | 9 | 0 |
| rrf | 1.000 | 1.000 | 0.942 | 0.940 | 9 | 1.000 | 0.100 | N/A | 0 | 9 | 9 |
| +exact | 1.000 | 1.000 | 0.942 | 0.940 | 9 | 1.000 | 0.100 | N/A | 0 | 9 | 9 |
| +temporal | 0.992 | 0.780 | 0.756 | 0.740 | 9 | 1.000 | 0.100 | N/A | 0 | 9 | 9 |
| +dedup | 0.992 | 0.735 | 0.762 | 0.702 | 9 | 1.000 | 0.000 | 0.346 | 13 | 9 | 9 |

## Per-bucket nDCG@10 / R@5

| bucket | dense-only R5 | +temporal R5 | +dedup R5 | dense nDCG | +temporal nDCG | +dedup nDCG |
|---|---|---|---|---|---|---|
| exact | 1.000 | 1.000 | 0.944 | 1.000 | 0.982 | 0.926 |
| paraphrase | 1.000 | 0.796 | 0.815 | 0.981 | 0.856 | 0.856 |
| multilingual | 1.000 | 0.917 | 0.833 | 0.895 | 0.779 | 0.730 |
| temporal | 1.000 | 0.389 | 0.167 | 0.883 | 0.353 | 0.282 |
| multi_turn | 1.000 | 0.611 | 0.574 | 0.878 | 0.432 | 0.441 |
| duplicate | 1.000 | 1.000 | 0.944 | 0.997 | 1.000 | 0.948 |
| no_evidence | N/A | N/A | N/A | N/A | N/A | N/A |
| filter | 1.000 | 0.778 | 0.907 | 1.000 | 0.854 | 0.805 |

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
| q-0009 | 2 | 1 | True | True | True |
| q-0024 | 1 | 1 | False | False | False |
| q-0055 | 1 | 1 | False | False | False |
| q-0056 | 2 | 1 | True | True | True |
| q-0057 | 2 | 1 | True | True | True |
| q-0058 | 1 | 9 | True | True | False |
| q-0074 | 2 | 1 | True | True | True |
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
| dense-only | 18.616 | 21.351 |
| lexical-only | 12.824 | 13.987 |
| rrf | 19.632 | 21.703 |
| +exact | 19.709 | 21.812 |
| +temporal | 20.151 | 22.452 |
| +dedup | 38.561 | 42.873 |

### +dedup per-stage

| stage | p50 (ms) | p95 (ms) |
|---|---|---|
| dedup | 16.734 | 20.339 |
| dense | 5.793 | 7.305 |
| exact | 0.000 | 0.622 |
| rrf | 0.037 | 0.045 |
| temporal | 0.038 | 0.042 |
| trigram | 0.762 | 1.280 |
| ts_rank_cd | 0.541 | 1.618 |

## Adapters (not run)

| dataset | status | note |
|---|---|---|
| longmemeval | local_only | user-supplied local data; raw/derived rows not committed; redistribution=unresolved |
| miracl | adapter_only | not_run / not_comparable_to_official; no corpus download, no committed topics/qrels |
| nfcorpus | local_only | official archive is user-supplied local-only (MD5 pinned a89dba18…); not run in this measurement |

## Known gaps

- Production query.search() does not apply source/session/time filters; all dense-based configs show filter leakage (source=9, session=9, time=3 per config).
- +temporal degrades the temporal and filter buckets (decay lifts non-target docs); +dedup drops relevant docs (coverage risk).
- no_evidence returns false positives in the dense path (no abstention mechanism); lexical-only abstains by absence of candidates.

