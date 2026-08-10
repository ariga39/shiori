# Shiori Retrieval-Quality Baseline

- Total queries: 26
- Retrieval-stage latency (ranking only): 0.000024s

## Overall (evidence-bearing queries)

| metric | value |
|---|---|
| Recall@5 | 1.0 |
| MRR@10 | 1.0 |
| nDCG@10 | 0.9798 |

## By category

| category | count | recall@5 | mrr@10 | ndcg@10 |
|---|---|---|---|---|
| exact | 6 | 1.0 | 1.0 | 1.0 |
| multi_turn | 1 | 1.0 | 1.0 | 0.7967 |
| multilingual | 3 | 1.0 | 1.0 | 1.0 |
| near_duplicate | 3 | 1.0 | 1.0 | 1.0 |
| no_evidence | 4 | 0.0 | 0.0 | 0.0 |
| paraphrase | 4 | 1.0 | 1.0 | 1.0 |
| source_filter | 1 | 1.0 | 1.0 | 0.7579 |
| temporal | 4 | 1.0 | 1.0 | 1.0 |

## No-evidence behavior

- No-evidence queries: 4
- Queries returning any result: 4
- Queries returning a top grade >= 2 result: 0
