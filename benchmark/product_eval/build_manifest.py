"""Assemble benchmark/product_eval/dataset_manifest.json.

Merges the generated query_splits with the frozen adapter/license/revision
metadata. The output manifest is the committed source of truth; this script is
deterministic and must not change the committed output.
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main() -> None:
    splits = json.loads((HERE / "query_splits.json").read_text(encoding="utf-8"))
    tune = sum(1 for s in splits if s["split"] == "tune")
    holdout = sum(1 for s in splits if s["split"] == "holdout")
    buckets: dict[str, int] = {}
    tag_counts: dict[str, int] = {}
    for s in splits:
        buckets[s["bucket"]] = buckets.get(s["bucket"], 0) + 1
        for tag in s["tags"]:
            tag_counts[tag] = tag_counts.get(tag, 0) + 1

    # Only the frozen cross-coverage tags become targets (duplicate_groups is a
    # metric ground-truth tag, not a coverage target).
    _COVERAGE_TAGS = {
        "same_name", "long_chinese", "cross_session", "knowledge_update",
        "hard_negative", "source_filter", "session_filter", "time_filter",
    }
    cross_coverage_targets = {
        tag: count for tag, count in tag_counts.items() if tag in _COVERAGE_TAGS
    }

    manifest = {
        "manifest_version": "1",
        "dataset": {
            "name": "shiori-phase4d-golden",
            "rows_jsonl": "golden_queries.jsonl",
            "schema_ref": "benchmark/corpus_schema.json",
            "query_count": len(splits),
            "tune_count": tune,
            "holdout_count": holdout,
        },
        "golden_queries": {
            "bucket_counts": {
                "exact": buckets.get("exact", 0),
                "paraphrase": buckets.get("paraphrase", 0),
                "multilingual": buckets.get("multilingual", 0),
                "temporal": buckets.get("temporal", 0),
                "multi_turn": buckets.get("multi_turn", 0),
                "duplicate": buckets.get("duplicate", 0),
                "no_evidence": buckets.get("no_evidence", 0),
                "filter": buckets.get("filter", 0),
            },
            "split_counts": {"tune": tune, "holdout": holdout},
        },
        "cross_coverage_targets": cross_coverage_targets,
        "adapters": {
            "longmemeval": {
                "status": "local_only",
                "hf_repo": "xiaowu0162/longmemeval-cleaned",
                "revision": "98d7416c24c778c2fee6e6f3006e7a073259d48f",
                "card_license": "mit",
                "upstream_provenance": "session data derived from ShareGPT/UltraChat-style public conversations; card license=mit is the publisher declaration only",
                "redistribution": "unresolved",
                "files": [
                    {"path": "longmemeval_oracle.json", "bytes": 15388478, "sha256": "821a2034d219ab45846873dd14c14f12cfe7776e73527a483f9dac095d38620c"},
                    {"path": "longmemeval_s_cleaned.json", "bytes": 277383467, "sha256": "d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442"},
                    {"path": "longmemeval_m_cleaned.json", "bytes": 2737100077, "sha256": "9d79e5524794a2e6900a3aa9cb7d9152c5a3e8319c9a87c25494ba1eacee495f"},
                ],
            },
            "nfcorpus": {
                "status": "local_only",
                "source": "https://public.ukp.informatik.tu-darmstadt.de/thakur/BEIR/datasets/nfcorpus.zip",
                "source_md5": "a89dba18a62ef92f7d323ec890a0d38d",
                "card_license": "cc-by-sa-4.0",
                "upstream_provenance": "BEIR wrapper cc-by-sa-4.0 (HF card); upstream NFCorpus research distribution license not closed",
                "redistribution": "unresolved",
            },
            "miracl": {
                "status": "adapter_only",
                "hf_repo": "miracl/miracl",
                "revision": "5be20db9509754dadad47689368639fcec739c00",
                "card_license": "apache-2.0",
                "scope": "zh/ja/en monolingual; local-only adapter contract, not_run / not_comparable_to_official; no corpus download, no committed topics/qrels",
                "redistribution": "not_run",
                "files": [
                    {"lang": "en", "split": "dev", "path": "topics/dev.topics.jsonl", "bytes": 36782},
                    {"lang": "en", "split": "test-a", "path": "topics/test-a.topics.jsonl", "bytes": 34135},
                    {"lang": "en", "split": "test-b", "path": "topics/test-b.topics.jsonl", "bytes": 109792},
                    {"lang": "en", "split": "dev", "path": "qrels/dev.qrels.tsv", "bytes": 167817},
                    {"lang": "ja", "split": "dev", "path": "topics/dev.topics.jsonl", "bytes": 50019},
                    {"lang": "ja", "split": "test-a", "path": "topics/test-a.topics.jsonl", "bytes": 38489},
                    {"lang": "ja", "split": "test-b", "path": "topics/test-b.topics.jsonl", "bytes": 66260},
                    {"lang": "ja", "split": "dev", "path": "qrels/dev.qrels.tsv", "bytes": 160882},
                    {"lang": "zh", "split": "dev", "path": "topics/dev.topics.jsonl", "bytes": 16903},
                    {"lang": "zh", "split": "test-b", "path": "topics/test-b.topics.jsonl", "bytes": 39712},
                    {"lang": "zh", "split": "dev", "path": "qrels/dev.qrels.tsv", "bytes": 94140},
                ],
            },
        },
        "query_splits": splits,
    }

    (HERE / "dataset_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(f"wrote dataset_manifest.json ({len(splits)} queries)")


if __name__ == "__main__":
    main()
