"""Deterministic generator for the Phase 4D (task #18) golden query set.

Emits benchmark/product_eval/golden_queries.jsonl and the query_splits section
of dataset_manifest.json. All query text and relevance grades are explicitly
authored here (no seed-derived content), mapped to the task #11 corpus.

Cross-coverage tags are backed by an EXPLICIT hand-authored ledger (see
_compute_evidence). Code only attaches the authored evidence; it never infers a
tag from corpus metadata. Semantic decisions (what counts as a hard negative,
whether an answer truly needs multiple sessions, etc.) are authored and then
validated for consistency by benchmark.product_eval.manifest.

Bucket/split counts (frozen contract):
- 120 total: exact 15, paraphrase 15, multilingual 20, temporal 15,
  multi_turn 15, duplicate 10, no_evidence 15, filter 15.
- Split: tune 72 / holdout 48.
Cross-coverage (independent minimums): same_name>=12, long_chinese>=10,
cross_session>=10, knowledge_update>=8, hard_negative>=15, source_filter>=6,
session_filter>=6, time_filter>=6.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
CORPUS = HERE.parent / "fixtures" / "corpus.jsonl"

# Time-bound words that make a query a time_filter.
_TIME_BOUNDS = ("when", "latest", "most recent", "moved", "before", "after", "什么时候", "最新", "改到", "到期")
# Change words that indicate a knowledge update (fact changed over time).
_UPDATE_WORDS = ("moved", "changed", "改到", "提前", "最新", "most recent", "latest", "now", "什么时候")
# Multi-session / multi-doc hard-negative candidates within a session share
# session + at least one lexical term with the query.
_CJK = re.compile(r"[\u4e00-\u9fff]")

# filter-bucket query -> its source_filter session (authored, matches the row).
_FILTER_SESSION = {
    "q-0111": "bench-build",
    "q-0112": "bench-build",
    "q-0113": "bench-db",
    "q-0114": "bench-db",
    "q-0115": "bench-mcp",
    "q-0116": "bench-deploy",
    "q-0117": "bench-plan",
    "q-0118": "bench-ops",
    "q-0119": "bench-build",
    "q-0120": "bench-mcp",
    "q-0121": "bench-nav",
    "q-0122": "bench-plan",
    "q-0123": "bench-db",
    "q-0124": "bench-plan",
    "q-0125": "bench-deploy",
}


def _load_corpus() -> dict[str, dict]:
    docs: dict[str, dict] = {}
    with CORPUS.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                doc = json.loads(line)
                docs[doc["id"]] = doc
    return docs


def _q(
    qid: str,
    text: str,
    cls: str,
    lang: str,
    direction: str,
    relevance: dict[str, int],
    *,
    canonical: str | None = None,
    conversation: list[str] | None = None,
    source_filter: str | None = None,
    split: str = "tune",
) -> dict:
    canonical = canonical if canonical is not None else " ".join(text.split())
    row = {
        "query_id": qid,
        "class": cls,
        "lang": lang,
        "direction": direction,
        "query_text": text,
        "canonical_query": canonical,
        "relevance": relevance,
        "expected_no_evidence": cls == "no_evidence",
        "conversation_context": conversation or [],
        "source_filter": source_filter,
    }
    row["_split"] = split
    row["_bucket"] = "filter" if cls == "exact" and source_filter else cls
    return row


def _build_rows() -> list[dict]:
    rows: list[dict] = []

    # ---- exact (15) -----------------------------------------------------
    exact = [
        ("which migration added the manager history tables", "exact", "en", "en", {"doc-0009": 3}),
        ("which migration added the freshness column to the nav snapshot table", "exact", "en", "en", {"doc-0010": 3}),
        ("which migration introduced the provenance column", "exact", "en", "en", {"doc-0011": 3}),
        ("what command writes validated nav quotes into the durable snapshot table", "exact", "en", "en", {"doc-0012": 3}),
        ("what embedding dimension does postgresql store for session chunks", "exact", "en", "en", {"doc-0004": 3}),
        ("does the mcp server ever perform writes", "exact", "en", "en", {"doc-0006": 3}),
        ("what is the build server caching to speed up incremental builds", "exact", "en", "en", {"doc-0022": 3}),
        ("what checks does the build pipeline run on each pull request", "exact", "en", "en", {"doc-0001": 3}),
        ("when was the quarterly report deadline moved to", "exact", "en", "en", {"doc-0015": 3, "doc-0016": 2}),
        ("what does the onboarding checklist verify before the first commit", "exact", "en", "en", {"doc-0020": 3}),
        ("what is used for short queries to complement vector search", "exact", "en", "en", {"doc-0014": 3}),
        ("which environment may only receive verified production releases", "exact", "en", "en", {"doc-0017": 3, "doc-0018": 3, "doc-0021": 2}),
        ("构建流水线在每个拉取请求上运行哪些检查", "exact", "zh", "zh", {"doc-0002": 3}),
        ("CLI 刷新命令把什么写入持久化快照表", "exact", "zh", "zh", {"doc-0013": 3}),
        ("MCP 服务器提供什么类型的搜索工具", "exact", "zh", "zh", {"doc-0007": 3}),
    ]
    for i, (text, cls, lang, direction, rel) in enumerate(exact, start=1):
        rows.append(_q(f"q-{i:04d}", text, cls, lang, direction, rel, split="tune"))

    # ---- paraphrase (15) -------------------------------------------------
    paraphrase = [
        ("can you tell me about the database migration that tracks fund manager history", "paraphrase", "en", "en", {"doc-0009": 3}),
        ("i need the migration that put a freshness field on the nav snapshot table", "paraphrase", "en", "en", {"doc-0010": 3}),
        ("which change added provenance tracking to each nav quote", "paraphrase", "en", "en", {"doc-0011": 3}),
        ("how do i write validated nav quotes into the durable snapshot store after the migration tracked manager history", "paraphrase", "en", "en", {"doc-0012": 3, "doc-0009": 2}),
        ("what is the vector dimension for session chunk embeddings in postgres and what does the mcp server expose read only", "paraphrase", "en", "en", {"doc-0004": 3, "doc-0006": 2}),
        ("is the mcp server strictly read only", "paraphrase", "en", "en", {"doc-0006": 3}),
        ("why does the build server keep a cached dependency tree", "paraphrase", "en", "en", {"doc-0022": 3}),
        ("what does ci run for lint, typecheck and tests and what does onboarding verify before the first commit", "paraphrase", "en", "en", {"doc-0001": 3, "doc-0020": 2}),
        ("when exactly is the quarterly report now due and which migration is the most recent", "paraphrase", "en", "en", {"doc-0016": 3, "doc-0015": 2, "doc-0011": 2}),
        ("what is verified during onboarding before the first git commit", "paraphrase", "en", "en", {"doc-0020": 3}),
        ("how does the system find exact entity matches for short queries stored as embeddings", "paraphrase", "en", "en", {"doc-0014": 3, "doc-0004": 2}),
        ("which environment is restricted to verified releases after the build server verifies", "paraphrase", "en", "en", {"doc-0017": 3, "doc-0022": 2}),
        ("拉取请求上构建流水线会跑哪些静态检查与测试", "paraphrase", "zh", "zh", {"doc-0002": 3}),
        ("刷新命令如何把验证后的净值写入持久化表", "paraphrase", "zh", "zh", {"doc-0013": 3}),
        ("mcp 服务器提供只读的什么接口", "paraphrase", "zh", "zh", {"doc-0007": 3}),
    ]
    for i, (text, cls, lang, direction, rel) in enumerate(paraphrase, start=16):
        rows.append(_q(f"q-{i:04d}", text, cls, lang, direction, rel, split="tune"))

    # ---- multilingual (20) ------------------------------------------------
    multilingual = [
        ("构建流水线用 GitHub Actions 在每个拉取请求上运行什么？", "multilingual", "zh", "en_to_zh", {"doc-0002": 3}),
        ("ビルドパイプラインは各プルリクエストで何を実行しますか？", "multilingual", "ja", "en_to_ja", {"doc-0003": 3}),
        ("PostgreSQL 如何存储会话分块？", "multilingual", "zh", "en_to_zh", {"doc-0005": 3}),
        ("MCPサーバーは読み取り専用ですか？", "multilingual", "ja", "en_to_ja", {"doc-0008": 3}),
        ("Which migration tracks fund manager history?", "multilingual", "en", "zh_to_en", {"doc-0009": 3}),
        ("Which migration added the freshness column?", "multilingual", "en", "zh_to_en", {"doc-0010": 3}),
        ("CLI 刷新命令把验证后的净值写入哪个表？", "multilingual", "zh", "zh", {"doc-0013": 3}),
        ("どうすれば NAV をスナップショットに永続化できますか？", "multilingual", "ja", "en_to_ja", {"doc-0012": 3}),
        ("The build pipeline runs lint, typecheck and tests on each pull request. 对还是错？", "multilingual", "zh", "en_to_zh", {"doc-0002": 3}),
        ("构建服务器缓存的依赖树有什么作用？", "multilingual", "zh", "zh", {"doc-0022": 3}),
        ("Does the staging environment accept unverified releases after the build server verifies the dependency tree?", "multilingual", "en", "zh_to_en", {"doc-0017": 3, "doc-0022": 2}),
        ("部署暂存环境需要满足什么条件？", "multilingual", "zh", "zh", {"doc-0019": 3, "doc-0017": 2}),
        ("MCP サーバーは書き込みを行いますか？", "multilingual", "ja", "en_to_ja", {"doc-0008": 3}),
        ("exact substring matching 用于什么场景？", "multilingual", "zh", "en_to_zh", {"doc-0014": 3}),
        ("What does the onboarding checklist verify before the first commit?", "multilingual", "en", "zh_to_en", {"doc-0020": 3}),
        ("季报截止日期改到了什么时候？", "multilingual", "zh", "en_to_zh", {"doc-0015": 3, "doc-0016": 2}),
        ("Which migration introduced the provenance column?", "multilingual", "en", "zh_to_en", {"doc-0011": 3}),
        ("セッションチャンクの埋め込み次元はいくつですか？", "multilingual", "ja", "en_to_ja", {"doc-0004": 3}),
        ("NAV の引用を永続化する CLI コマンドは？", "multilingual", "ja", "en_to_ja", {"doc-0012": 3}),
        ("The quarterly report deadline is now the last working day of August. 什么时候？", "multilingual", "zh", "en_to_zh", {"doc-0016": 3, "doc-0015": 2}),
    ]
    for i, (text, cls, lang, direction, rel) in enumerate(multilingual, start=31):
        rows.append(_q(f"q-{i:04d}", text, cls, lang, direction, rel, split="tune" if i <= 42 else "holdout"))
    # ---- temporal (15) -----------------------------------------------------
    temporal = [
        ("what was the quarterly report deadline before it moved", "temporal", "en", "en", {"doc-0015": 3}),
        ("which migration came first, manager history or freshness", "temporal", "en", "en", {"doc-0009": 3, "doc-0010": 2}),
        ("what was added most recently to the nav pipeline", "temporal", "en", "en", {"doc-0011": 3}),
        ("what did the earliest migration in the db session add", "temporal", "en", "en", {"doc-0009": 3}),
        ("when does the quarterly report become due now", "temporal", "en", "en", {"doc-0016": 3, "doc-0015": 2}),
        ("the deadline moved from when to when", "temporal", "en", "en", {"doc-0015": 3, "doc-0016": 2}),
        ("which migration is the latest one for nav snapshots and how are validated quotes persisted", "temporal", "en", "en", {"doc-0011": 3, "doc-0012": 2}),
        ("what was in the nav pipeline before provenance was added", "temporal", "en", "en", {"doc-0010": 3, "doc-0011": 1}),
        ("after the freshness migration, what came next", "temporal", "en", "en", {"doc-0011": 3}),
        ("the first migration added manager history, then what", "temporal", "en", "en", {"doc-0010": 3}),
        ("季度报告截止日期从什么时候改到了什么时候", "temporal", "zh", "zh", {"doc-0015": 3, "doc-0016": 2}),
        ("净值迁移的顺序：先建了哪些表", "temporal", "zh", "zh", {"doc-0009": 3}),
        ("哪个迁移是最新的", "temporal", "zh", "zh", {"doc-0011": 3}),
        ("报告截止日期现在是什么时候", "temporal", "zh", "zh", {"doc-0016": 3}),
        ("最初添加的迁移创建了哪些表", "temporal", "zh", "zh", {"doc-0009": 3}),
    ]
    for i, (text, cls, lang, direction, rel) in enumerate(temporal, start=51):
        rows.append(_q(f"q-{i:04d}", text, cls, lang, direction, rel, split="tune"))

    # ---- multi_turn (15) ---------------------------------------------------
    multi_turn = [
        ("which table records fund manager history", "multi_turn", "en", "en", {"doc-0009": 3},
         ["Earlier we talked about database migrations.", "You mentioned manager history tables."]),
        ("did that come before or after the freshness column", "multi_turn", "en", "en", {"doc-0010": 3, "doc-0009": 2},
         ["We were listing the nav pipeline migrations.", "Manager history came first."]),
        ("so what did the later migration add", "multi_turn", "en", "en", {"doc-0011": 3},
         ["We are walking through the db migrations.", "Freshness was added second."]),
        ("how do we persist it durably", "multi_turn", "en", "en", {"doc-0012": 3},
         ["The cli refresh command was mentioned.", "We need validated nav quotes saved."]),
        ("what checks run on each pr and how does the build server speed up incremental builds", "multi_turn", "en", "en", {"doc-0001": 3, "doc-0022": 2},
         ["We were describing the build pipeline.", "It uses GitHub Actions.", "Then you mentioned the build server."]),
        ("what dimension is stored and which migration added the freshness column", "multi_turn", "en", "en", {"doc-0004": 3, "doc-0010": 2},
         ["Session chunks are stored in postgresql.", "They use vector embeddings.", "Earlier you mentioned a freshness migration."]),
        ("is it read only", "multi_turn", "en", "en", {"doc-0006": 3},
         ["We discussed the mcp server tools.", "There are search tools exposed."]),
        ("which env accepts it and what does the staging checklist verify", "multi_turn", "en", "en", {"doc-0017": 3, "doc-0021": 2, "doc-0020": 2},
         ["We talked about verified releases.", "Only some environments may receive them.", "You also mentioned an onboarding checklist."]),
        ("so when is it due now", "multi_turn", "en", "en", {"doc-0016": 3, "doc-0015": 2},
         ["The quarterly report deadline changed.", "It was moved earlier."]),
        ("what gets verified first", "multi_turn", "en", "en", {"doc-0020": 3},
         ["New team members follow a checklist.", "Git identity was mentioned."]),
        ("which cache helps incremental builds", "multi_turn", "en", "en", {"doc-0022": 3},
         ["The build server was discussed.", "It speeds up repeated builds."]),
        ("什么时候到期", "multi_turn", "zh", "zh", {"doc-0016": 3, "doc-0015": 2},
         ["我们刚讨论过季度报告期限。", "截止日期被提前了。"]),
        ("它写入哪个表", "multi_turn", "zh", "zh", {"doc-0013": 3},
         ["我们讨论了 CLI 刷新命令。", "需要持久化净值快照。"]),
        ("哪个环境能接收", "multi_turn", "zh", "zh", {"doc-0019": 3, "doc-0017": 2},
         ["我们刚说过经过验证的版本。", "只有特定环境可以部署。"]),
        ("有哪些检查", "multi_turn", "zh", "zh", {"doc-0002": 3},
         ["我们描述了构建流水线。", "它运行在每次拉取请求上。"]),
    ]
    for i, (text, cls, lang, direction, rel, conversation) in enumerate(multi_turn, start=66):
        rows.append(
            _q(
                f"q-{i:04d}", text, cls, lang, direction, rel,
                conversation=conversation,
                split="tune",
            )
        )

    # ---- duplicate (10) ----------------------------------------------------
    duplicate = [
        ("which migration added the manager history tables", "duplicate", "en", "en", {"doc-0009": 3}),
        ("which migration added the freshness column", "duplicate", "en", "en", {"doc-0010": 3}),
        ("what command writes validated nav quotes into the durable snapshot table", "duplicate", "en", "en", {"doc-0012": 3}),
        ("does the mcp server ever perform writes", "duplicate", "en", "en", {"doc-0006": 3}),
        ("what checks does the build pipeline run on each pull request", "duplicate", "en", "en", {"doc-0001": 3}),
        ("which environment may only receive verified production releases", "duplicate", "en", "en", {"doc-0017": 3, "doc-0018": 3, "doc-0021": 2}),
        ("when was the quarterly report deadline moved to", "duplicate", "en", "en", {"doc-0015": 3, "doc-0016": 2}),
        ("what is used for short queries to complement vector search", "duplicate", "en", "en", {"doc-0014": 3}),
        ("构建流水线在每个拉取请求上运行哪些检查", "duplicate", "zh", "zh", {"doc-0002": 3}),
        ("CLI 刷新命令把什么写入持久化快照表", "duplicate", "zh", "zh", {"doc-0013": 3}),
    ]
    for i, (text, cls, lang, direction, rel) in enumerate(duplicate, start=81):
        rows.append(_q(f"q-{i:04d}", text, cls, lang, direction, rel, split="holdout"))

    # ---- no_evidence (15) --------------------------------------------------
    no_evidence = [
        "which meeting scheduled a code review for the refund flow",
        "what is the exact version number of the deployment image",
        "who approved the staging promotion for the incident fix",
        "what password does the onboarding runbook set for new shells",
        "how much does the nightly batch job cost per month",
        "which employee filed the expense report for the offsite",
        "what is the latency of the us-east-1 cache region",
        "which pull request bumped the python dependency to 3.13",
        "what is the api key used by the notification service",
        "when is the next security training session scheduled",
        "哪个版本的镜像被部署到了生产环境",
        "这次事故的处理人是谁",
        "新员工的入职密码是什么",
        "夜间批处理任务每月花费多少",
        "下一次安全培训是什么时候",
    ]
    for i, text in enumerate(no_evidence, start=96):
        rows.append(_q(f"q-{i:04d}", text, "no_evidence", "en" if i < 106 else "zh", "en" if i < 106 else "zh", {}, split="holdout"))

    # ---- filter (15) -------------------------------------------------------
    filter_queries = [
        ("what checks run on each pull request", "filter", "en", "en", {"doc-0001": 3}, "bench-build"),
        ("what does the build server cache", "filter", "en", "en", {"doc-0022": 3}, "bench-build"),
        ("what migration added the manager history tables", "filter", "en", "en", {"doc-0009": 3}, "bench-db"),
        ("which migration added the provenance column most recently", "filter", "en", "en", {"doc-0011": 3}, "bench-db"),
        ("what does the mcp server expose", "filter", "en", "en", {"doc-0006": 3}, "bench-mcp"),
        ("which environment accepted the most recent verified release", "filter", "en", "en", {"doc-0017": 3, "doc-0018": 3, "doc-0021": 2}, "bench-deploy"),
        ("when is the quarterly report due", "filter", "en", "en", {"doc-0016": 3, "doc-0015": 2}, "bench-plan"),
        ("what does onboarding verify", "filter", "en", "en", {"doc-0020": 3}, "bench-ops"),
        ("构建流水线在每个拉取请求上运行哪些检查", "filter", "zh", "zh", {"doc-0002": 3}, "bench-build"),
        ("MCP 服务器提供只读搜索工具吗", "filter", "zh", "zh", {"doc-0007": 3}, "bench-mcp"),
        ("CLI 刷新命令写入什么", "filter", "zh", "zh", {"doc-0013": 3}, "bench-nav"),
        ("季报截止日期改到了什么时候", "filter", "zh", "zh", {"doc-0016": 3, "doc-0015": 2}, "bench-plan"),
        ("最新一次迁移添加了什么", "filter", "zh", "zh", {"doc-0011": 3}, "bench-db"),
        ("现在季报什么时候到期", "filter", "zh", "zh", {"doc-0016": 3, "doc-0015": 2}, "bench-plan"),
        ("哪些环境接收经过验证的版本", "filter", "zh", "zh", {"doc-0019": 3, "doc-0017": 2}, "bench-deploy"),
    ]
    for i, (text, cls, lang, direction, rel, sf) in enumerate(filter_queries, start=111):
        rows.append(
            _q(
                f"q-{i:04d}", text, "exact", lang, direction, rel,
                source_filter=sf,
                split="holdout",
            )
        )
    return rows


def _compute_evidence(rows: list[dict]) -> None:
    """Attach the EXPLICIT, hand-authored ledger to each row.

    The ledger is a static mapping: each tag a query carries is backed by a
    human-written reason and the exact doc ids that justify it. This module
    only attaches the authored evidence; it never infers a tag from corpus
    metadata. Semantic decisions (what counts as a hard negative, whether an
    answer truly needs multiple sessions, etc.) are authored here and validated
    for consistency by benchmark.product_eval.manifest.
    """

    # Authoritative ledger: query_id -> tag -> evidence (committed JSON).
    # EVERY tag/evidence comes exclusively from evidence_ledger.json. The
    # generator only attaches the authored evidence; it never derives tags or
    # evidence in code. Evidence doc ids must be real corpus docs and consistent
    # with the row's relevance/source_filter (validated separately).
    # hard_negative states the relevant doc, the non-relevant near-neighbor,
    # and WHY it is not relevant. cross_session / knowledge_update state why
    # the answer genuinely needs multi-session or time-updated evidence.
    ledger = json.loads((HERE / "evidence_ledger.json").read_text(encoding="utf-8"))

    for row in rows:
        qid = row["query_id"]
        evidence = dict(ledger.get(qid, {}))
        row["_evidence"] = evidence
        row["_tags"] = sorted(evidence)


def _assign_splits(rows: list[dict]) -> None:
    """Frozen 60/40 development/holdout split, per bucket, MECHANICALLY by
    query order (not by quality). Bucket counts (tune/holdout):
    exact 9/6, paraphrase 9/6, multilingual 12/8, temporal 9/6,
    multi_turn 9/6, duplicate 6/4, no_evidence 9/6, filter 9/6.
    """
    tune_per_bucket = {
        "exact": 9, "paraphrase": 9, "multilingual": 12, "temporal": 9,
        "multi_turn": 9, "duplicate": 6, "no_evidence": 9, "filter": 9,
    }
    counts: dict[str, int] = {}
    for row in rows:
        bucket = row["_bucket"]
        n = counts.get(bucket, 0)
        counts[bucket] = n + 1
        row["_split"] = "tune" if n < tune_per_bucket[bucket] else "holdout"


def _verify_counts(rows: list[dict]) -> None:
    buckets: dict[str, int] = {}
    splits: dict[str, int] = {}
    tags: dict[str, int] = {}
    for row in rows:
        buckets[row["_bucket"]] = buckets.get(row["_bucket"], 0) + 1
        splits[row["_split"]] = splits.get(row["_split"], 0) + 1
        for tag in row["_tags"]:
            tags[tag] = tags.get(tag, 0) + 1
    expected = {"exact": 15, "paraphrase": 15, "multilingual": 20, "temporal": 15,
                "multi_turn": 15, "duplicate": 10, "no_evidence": 15, "filter": 15}
    assert len(rows) == 120, len(rows)
    assert buckets == expected, buckets
    assert splits == {"tune": 72, "holdout": 48}, splits
    min_tags = {"same_name": 12, "long_chinese": 10, "cross_session": 10,
                "knowledge_update": 8, "hard_negative": 15, "source_filter": 6,
                "session_filter": 6, "time_filter": 6}
    for tag, minimum in min_tags.items():
        assert tags.get(tag, 0) >= minimum, f"{tag}: {tags.get(tag, 0)} < {minimum}"
    print("bucket counts:", buckets)
    print("split counts:", splits)
    print("tag counts:", {k: tags.get(k, 0) for k in min_tags})


def main() -> None:
    rows = _build_rows()
    _compute_evidence(rows)
    _assign_splits(rows)
    _verify_counts(rows)

    out_rows = []
    splits = []
    for row in rows:
        split = row.pop("_split")
        bucket = row.pop("_bucket")
        tags = row.pop("_tags")
        evidence = row.pop("_evidence")
        out_rows.append(row)
        splits.append({"query_id": row["query_id"], "split": split, "bucket": bucket, "tags": tags, "evidence": evidence})
    out_rows.sort(key=lambda r: r["query_id"])
    splits.sort(key=lambda s: s["query_id"])

    with (HERE / "golden_queries.jsonl").open("w", encoding="utf-8") as fh:
        for row in out_rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    (HERE / "query_splits.json").write_text(
        json.dumps(splits, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    print("wrote golden_queries.jsonl and query_splits.json")


if __name__ == "__main__":
    main()
