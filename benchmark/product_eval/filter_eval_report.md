# Phase 4E1 dev-only filter evaluation

- schema: `shiori-filter-eval/v4`
- harness SHA: `352e8c18cf1c71711aff135a9c363bb2089ae22c`
- implementation SHA: `6621a680c64beb48a852b6f0fae098ea9235137b`
- embedding mode: `pinned_local_replay`
- model identity: `voyageai/voyage-4-nano@67fabc9bef010dabc5f6024aa1b1b6b93410426f`
- dev filter cases: 9 (72-dev only, holdout untouched)
- latency reps: 10
- input hashes: {"corpus.jsonl": "927584aa88a5a2c0223cce75ca001a5df75d5ac5689dfd64e598432de481de58", "dataset_manifest.json": "b951d799d251046cef513cde5ac6153e11f3d0e434f01841f11380890bc93d74", "dev_query_vectors.json": "629fa726ec353632a2a87a48b473ad0b59c2dd8f61a804746e2d9dd43c9287f2", "evidence_ledger.json": "7941af743d0f263981a1a2fb2f1fe3b5b856ec14a5c2dc498233b56c5eb40e8c", "golden_queries.jsonl": "d3d8b0167c1fa8d0c142f587f5c4872296320cb4b22d90b5cfb68d469d932970"}
- kind counts: {"session_filter": 9, "source_filter": 9, "time_filter": 3}
- leakage by kind (before/after query counts): {"session": {"after_query_count": 0, "before_query_count": 9}, "source": {"after_query_count": 0, "before_query_count": 9}, "time": {"after_query_count": 0, "before_query_count": 3}}
- total before leakage (rows): 105
- total after leakage (rows): 0
- total coverage risk (rows): 0
- latency p50/p95 (aggregate over 90 raw samples): {"control_p50_ms": 54.154, "control_p95_ms": 58.497, "filtered_p50_ms": 22.633, "filtered_p95_ms": 25.134, "latency_reps": 10}
- unfiltered regression: {"base_head_latency_p50_p95_ms": {"+dedup": {"base_p50": 38.561, "base_p95": 42.873, "head_p50": 37.886, "head_p95": 42.801}, "+exact": {"base_p50": 19.709, "base_p95": 21.812, "head_p50": 19.284, "head_p95": 21.927}, "+temporal": {"base_p50": 20.151, "base_p95": 22.452, "head_p50": 19.958, "head_p95": 22.302}, "dense-only": {"base_p50": 18.616, "base_p95": 21.351, "head_p50": 18.292, "head_p95": 21.239}, "lexical-only": {"base_p50": 12.824, "base_p95": 13.987, "head_p50": 12.893, "head_p95": 13.818}, "rrf": {"base_p50": 19.632, "base_p95": 21.703, "head_p50": 18.376, "head_p95": 20.674}}, "config_metric_deltas": {"+dedup": {"candidate_recall_at_20": 0.0, "filter_leakage": 0, "final_mrr@10": 0.0, "final_ndcg@10": 0.0, "final_recall@5": 0.0}, "+exact": {"candidate_recall_at_20": 0.0, "filter_leakage": 0, "final_mrr@10": 0.0, "final_ndcg@10": 0.0, "final_recall@5": 0.0}, "+temporal": {"candidate_recall_at_20": 0.0, "filter_leakage": 0, "final_mrr@10": 0.0, "final_ndcg@10": 0.0, "final_recall@5": 0.0}, "dense-only": {"candidate_recall_at_20": 0.0, "filter_leakage": 0, "final_mrr@10": 0.0, "final_ndcg@10": 0.0, "final_recall@5": 0.0}, "lexical-only": {"candidate_recall_at_20": 0.0, "filter_leakage": 0, "final_mrr@10": 0.0, "final_ndcg@10": 0.0, "final_recall@5": 0.0}, "rrf": {"candidate_recall_at_20": 0.0, "filter_leakage": 0, "final_mrr@10": 0.0, "final_ndcg@10": 0.0, "final_recall@5": 0.0}}, "frozen_baseline_runner_sha256": "5192a02e75d93a0b775db9851bae298cc2d2333271c2d533228fac14d70c157c", "head_runner_sha256": "5895d6ab64406c4f997ec8845a87c75562b411272ea636956519d18c95349eeb", "score_tolerance_note": "score-only diffs are ~1e-9 float noise from temporal-decay now between separate runs; doc/rank/reason/stage diffs are the regression signal", "trace_mismatch": {"doc_rank_reason_stage": 0, "events": 23427, "score_only": 4269}}
- ok: True

| query_id | kinds | before | after | control_returned | filtered_returned | coverage_risk | subsequence | ok |
|---|---|---|---|---|---|---|---|---|
| q-0111 | source_filter,session_filter | 12 | 0 | 13 | 1 | 0 | True | True |
| q-0112 | source_filter,session_filter | 12 | 0 | 13 | 1 | 0 | True | True |
| q-0113 | source_filter,session_filter | 10 | 0 | 13 | 3 | 0 | True | True |
| q-0114 | source_filter,session_filter,time_filter | 10 | 0 | 13 | 3 | 0 | True | True |
| q-0115 | source_filter,session_filter | 12 | 0 | 13 | 1 | 0 | True | True |
| q-0116 | source_filter,session_filter,time_filter | 13 | 0 | 13 | 0 | 0 | True | True |
| q-0117 | source_filter,session_filter,time_filter | 12 | 0 | 13 | 1 | 0 | True | True |
| q-0118 | source_filter,session_filter | 12 | 0 | 13 | 1 | 0 | True | True |
| q-0119 | source_filter,session_filter | 12 | 0 | 13 | 1 | 0 | True | True |
