# Retrieval-Quality Baseline Report (task #11)

Model: voyageai/voyage-4-nano (dim 1024, L2, encode_query/encode_document, truncate_dim=1024).
Corpus documents: 22; queries: 18; metrics: recall@5, mrr@10, ndcg@10.
Main baseline: pure voyage cosine (no mixed scoring).

## Aggregate by query class

| class | query_count | recall@5 | mrr@10 | ndcg@10 |
|---|---|---|---|---|
| exact | 3 | 1.000 | 1.000 | 1.000 |
| paraphrase | 3 | 1.000 | 1.000 | 1.000 |
| multilingual | 4 | 1.000 | 1.000 | 0.989 |
| temporal | 2 | 1.000 | 1.000 | 0.917 |
| multi_turn | 2 | 1.000 | 0.417 | 0.565 |
| duplicate | 1 | 1.000 | 1.000 | 0.984 |
| no_evidence | 3 | N/A | N/A | N/A |

## Aggregate by language

| lang | query_count | recall@5 | mrr@10 | ndcg@10 |
|---|---|---|---|---|
| en | 11 | 1.000 | 0.883 | 0.895 |
| zh | 4 | 1.000 | 1.000 | 1.000 |
| ja | 3 | 1.000 | 1.000 | 0.978 |

## Aggregate by direction

| direction | query_count | recall@5 | mrr@10 | ndcg@10 |
|---|---|---|---|---|
| en | 9 | 1.000 | 0.854 | 0.869 |
| en_to_ja | 1 | 1.000 | 1.000 | 0.956 |
| en_to_zh | 2 | 1.000 | 1.000 | 1.000 |
| ja | 2 | 1.000 | 1.000 | 1.000 |
| zh | 3 | 1.000 | 1.000 | 1.000 |
| zh_to_en | 1 | 1.000 | 1.000 | 1.000 |

## Aggregate by source filter

| filter | query_count | recall@5 | mrr@10 | ndcg@10 |
|---|---|---|---|---|
| bench-build | 1 | 1.000 | 1.000 | 1.000 |
| bench-deploy | 1 | 1.000 | 1.000 | 1.000 |
| none | 16 | 1.000 | 0.910 | 0.916 |

## Latency

| phase | sample_count | p50 (s) | p95 (s) | mean (s) |
|---|---|---|---|---|
| cold_query_encode | 1 | 0.4290 | 0.4290 | 0.4290 |
| cold_retrieval | 1 | 0.0034 | 0.0034 | 0.0034 |
| cold_e2e | 1 | 0.4324 | 0.4324 | 0.4324 |
| warm_query_encode | 17 | 0.2341 | 0.2959 | 0.2346 |
| warm_retrieval | 17 | 0.0033 | 0.0033 | 0.0030 |
| warm_e2e | 17 | 0.2358 | 0.2992 | 0.2376 |
| model_prefetch | 1 | 1.9686 | 1.9686 | 1.9686 (model prefetch/cache (download time included here, not in model_load)) |
| model_load | 1 | 0.9202 | 0.9202 | 0.9202 (model load, cache-backed offline (download excluded)) |

## No-evidence behavior

| query_id | expected_no_evidence | behavior |
|---|---|---|
| q-0014 | true | false_return |
| q-0015 | true | false_return |
| q-0016 | true | false_return |

## Gap ranking (data-supported, no-evidence excluded)

Sorted worst-first by aggregate nDCG@10 across quality query classes (no-evidence has null quality metrics and is reported separately):
1. `multi_turn` nDCG@10=0.565 (queries=2)
2. `temporal` nDCG@10=0.917 (queries=2)
3. `duplicate` nDCG@10=0.984 (queries=1)
4. `multilingual` nDCG@10=0.989 (queries=4)
5. `exact` nDCG@10=1.000 (queries=3)
6. `paraphrase` nDCG@10=1.000 (queries=3)
