# Shiori Retrieval-Quality Baseline

- Total queries: 26
- Retrieval-stage latency (ranking only): 0.000396s

## Overall (evidence-bearing queries)

| metric | value |
|---|---|
| Recall@5 | 0.1288 |
| MRR@10 | 0.172 |
| nDCG@10 | 0.1223 |

## By category

| category | count | recall@5 | mrr@10 | ndcg@10 |
|---|---|---|---|---|
| exact | 6 | 0.0833 | 0.0417 | 0.044 |
| multi_turn | 1 | 0.0 | 0.0 | 0.0 |
| multilingual | 3 | 0.1667 | 0.1111 | 0.1022 |
| near_duplicate | 3 | 0.1667 | 0.3333 | 0.2044 |
| no_evidence | 4 | 0.0 | 0.0 | 0.0 |
| paraphrase | 4 | 0.25 | 0.5 | 0.3066 |
| source_filter | 1 | 0.0 | 0.0 | 0.0 |
| temporal | 4 | 0.0833 | 0.05 | 0.0702 |

## No-evidence behavior

- No-evidence queries: 4
- Queries returning any result: 4
- Queries returning a top grade >= 2 result: 0
