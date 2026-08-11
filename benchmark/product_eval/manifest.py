"""Dataset manifest loader and split/bucket/cross-coverage validation (Phase 4D).

The manifest is the single source of truth for split/tag/license/revision/hash
metadata. Query/judgment rows reuse the task #11 corpus_schema.json and carry no
split fields; this module maps query_ids to splits, buckets, tags, and audit
evidence, then enforces the frozen counts AND verifies each tag has a
machine-checkable basis consistent with the task #11 corpus metadata.
"""

from __future__ import annotations

import json
from pathlib import Path

_BUCKET_TO_CLASS = {
    "exact": "exact",
    "paraphrase": "paraphrase",
    "multilingual": "multilingual",
    "temporal": "temporal",
    "multi_turn": "multi_turn",
    "duplicate": "duplicate",
    "no_evidence": "no_evidence",
    # The "filter" bucket covers source/session/time-filter queries; each
    # filter dimension has its own independent minimum.
    "filter": "exact",
}


def load_manifest(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def load_golden_rows(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _load_corpus_meta(corpus_path: Path) -> dict[str, dict]:
    """doc_id -> {session, timestamp, lang, source_kind} from the task #11 corpus."""
    meta: dict[str, dict] = {}
    with corpus_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                doc = json.loads(line)
                meta[doc["id"]] = {
                    "session": doc["session"],
                    "timestamp": doc["timestamp"],
                    "lang": doc["lang"],
                    "source_kind": doc["source_kind"],
                }
    return meta


def _query_meta(manifest: dict) -> dict[str, dict]:
    """query_id -> {split, bucket, tags, evidence} from the manifest."""
    by_id: dict[str, dict] = {}
    for entry in manifest.get("query_splits", []):
        by_id[entry["query_id"]] = {
            "split": entry["split"],
            "bucket": entry["bucket"],
            "tags": set(entry.get("tags", [])),
            "evidence": entry.get("evidence", {}),
        }
    return by_id


def validate_split(manifest_path: Path, rows_path: Path) -> tuple[set[str], set[str]]:
    """Return (tune, holdout) query id sets; enforce full coverage + disjoint."""
    manifest = load_manifest(manifest_path)
    rows = load_golden_rows(rows_path)
    by_id = _query_meta(manifest)
    all_ids = {row["query_id"] for row in rows}
    if set(by_id) != all_ids:
        missing = sorted(all_ids - set(by_id))
        extra = sorted(set(by_id) - all_ids)
        raise ValueError(f"manifest/rows mismatch: missing={missing} extra={extra}")
    tune = {qid for qid, meta in by_id.items() if meta["split"] == "tune"}
    holdout = {qid for qid, meta in by_id.items() if meta["split"] == "holdout"}
    if tune & holdout:
        raise ValueError("tune/holdout overlap in manifest")
    expected_tune = manifest["golden_queries"]["split_counts"]["tune"]
    expected_holdout = manifest["golden_queries"]["split_counts"]["holdout"]
    if len(tune) != expected_tune or len(holdout) != expected_holdout:
        raise ValueError(
            f"split counts mismatch: tune={len(tune)} (expected {expected_tune}) "
            f"holdout={len(holdout)} (expected {expected_holdout})"
        )
    return tune, holdout


def validate_bucket_counts(manifest: dict, rows: list[dict]) -> None:
    """Each of the 8 main buckets must match the manifest's frozen count."""
    by_id = _query_meta(manifest)
    counts: dict[str, int] = {}
    for row in rows:
        meta = by_id.get(row["query_id"])
        if meta is None:
            raise ValueError(f"row {row['query_id']} missing from manifest")
        counts[meta["bucket"]] = counts.get(meta["bucket"], 0) + 1
    expected = manifest["golden_queries"]["bucket_counts"]
    if counts != expected:
        raise ValueError(f"bucket counts mismatch: got {counts} expected {expected}")
    # Row `class` must be consistent with the frozen bucket mapping.
    for row in rows:
        meta = by_id[row["query_id"]]
        if _BUCKET_TO_CLASS[meta["bucket"]] != row["class"]:
            raise ValueError(
                f"row {row['query_id']} class {row['class']} inconsistent with bucket {meta['bucket']}"
            )


def validate_cross_coverage(manifest_path: Path, rows_path: Path) -> None:
    """Each cross-coverage tag must meet its independent minimum AND carry
    auditable evidence consistent with the corpus metadata AND the query's own
    relevance (the frozen contract: evidence is authored, code only verifies).

    The validator recomputes lang/CJK/sessions/timestamps from the REAL rows
    and corpus (never trusting ledger self-reported counts). Every tag must
    carry a non-empty `why` plus its semantic fields; any mismatch fails closed.
    """
    manifest = load_manifest(manifest_path)
    corpus = _load_corpus_meta(manifest_path.parent.parent / "fixtures" / "corpus.jsonl")
    rows = load_golden_rows(rows_path)
    row_by_id = {row["query_id"]: row for row in rows}
    by_id = _query_meta(manifest)
    tag_counts: dict[str, int] = {}
    for qid, meta in by_id.items():
        evidence = meta["evidence"]
        if not isinstance(evidence, dict):
            raise ValueError(f"query {qid} missing evidence map")
        row = row_by_id[qid]
        relevant = {doc_id for doc_id, grade in (row.get("relevance") or {}).items() if grade > 0}
        for tag in meta["tags"]:
            if tag not in evidence:
                raise ValueError(f"query {qid} tag {tag} has no evidence")
            ev = evidence[tag]
            tag_counts[tag] = tag_counts.get(tag, 0) + 1
            _check_evidence(tag, qid, ev, corpus, relevant=relevant, row=row)
    for tag, minimum in manifest["cross_coverage_targets"].items():
        if tag_counts.get(tag, 0) < minimum:
            raise ValueError(
                f"cross-coverage tag {tag}: got {tag_counts.get(tag, 0)} < minimum {minimum}"
            )


def _check_evidence(tag: str, qid: str, ev: dict, corpus: dict[str, dict], *, relevant: set[str], row: dict) -> None:
    why = ev.get("why") or ev.get("reason")
    if not why:
        raise ValueError(f"query {qid} tag {tag} evidence missing a non-empty why/reason")
    docs = ev.get("docs") or ev.get("relevant_docs")
    if tag in {"hard_negative", "cross_session", "knowledge_update", "same_name"}:
        if not docs:
            raise ValueError(f"query {qid} tag {tag} evidence missing doc ids")
        for doc_id in docs:
            if doc_id not in corpus:
                raise ValueError(f"query {qid} tag {tag} references unknown doc {doc_id}")
        if tag == "cross_session":
            if set(docs) != relevant:
                raise ValueError(f"query {qid} cross_session evidence docs do not match relevance: ev={sorted(docs)} rel={sorted(relevant)}")
            sessions = {corpus[d]["session"] for d in docs}
            if len(sessions) < 2:
                raise ValueError(f"query {qid} cross_session evidence does not span >= 2 sessions")
        if tag == "knowledge_update":
            if not docs or not set(docs).issubset(relevant):
                raise ValueError(f"query {qid} knowledge_update evidence docs not in relevance")
            stamps = {corpus[d]["timestamp"] for d in docs}
            if len(stamps) < 2:
                raise ValueError(f"query {qid} knowledge_update evidence lacks distinct timestamps")
        if tag == "hard_negative":
            non_rel = ev.get("non_relevant_docs")
            if not non_rel:
                raise ValueError(f"query {qid} hard_negative evidence missing non_relevant_docs")
            for doc_id in non_rel:
                if doc_id not in corpus:
                    raise ValueError(f"query {qid} hard_negative references unknown doc {doc_id}")
                if doc_id in relevant:
                    raise ValueError(f"query {qid} hard_negative non_relevant_docs {doc_id} is actually relevant")
            if set(ev.get("relevant_docs", [])) != relevant:
                raise ValueError(f"query {qid} hard_negative relevant_docs do not match relevance")
        if tag == "same_name":
            if not set(docs).issubset(relevant):
                raise ValueError(f"query {qid} same_name evidence docs not in relevance")
            if not ev.get("entity"):
                raise ValueError(f"query {qid} same_name evidence missing an entity")
    if tag == "long_chinese":
        # Recomputed from the REAL row, never trusted from the ledger.
        lang = row.get("lang")
        import re

        cjk = len(re.findall(r"[\u4e00-\u9fff]", row.get("query_text") or ""))
        if lang != "zh" or cjk < 8:
            raise ValueError(f"query {qid} long_chinese does not hold: lang={lang} cjk={cjk}")
    if tag == "time_filter":
        op = ev.get("operator")
        iso_bound = ev.get("iso_bound")
        eligible = ev.get("eligible_docs")
        if op not in {"before", "after", "at"}:
            raise ValueError(f"query {qid} time_filter operator must be before|after|at")
        if not isinstance(iso_bound, str) or not iso_bound.endswith(("Z", "+00:00")):
            raise ValueError(f"query {qid} time_filter iso_bound must be timezone-aware ISO8601")
        from datetime import datetime

        try:
            parsed_bound = datetime.fromisoformat(iso_bound.replace("Z", "+00:00"))
            assert parsed_bound.tzinfo is not None
        except Exception as exc:
            raise ValueError(f"query {qid} time_filter iso_bound not parseable") from exc
        if not eligible or not isinstance(eligible, list):
            raise ValueError(f"query {qid} time_filter missing eligible_docs")
        # Recompute the exact eligible set from the corpus timestamps and the
        # operator predicate; the ledger must match it precisely.
        computed = []
        for did, meta in sorted(corpus.items()):
            ts = meta.get("timestamp")
            if not ts:
                continue
            try:
                parsed_ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except Exception:
                continue
            if op == "before" and parsed_ts < parsed_bound:
                computed.append(did)
            elif op == "after" and parsed_ts > parsed_bound:
                computed.append(did)
            elif op == "at" and parsed_ts == parsed_bound:
                computed.append(did)
        if set(eligible) != set(computed):
            raise ValueError(
                f"query {qid} time_filter eligible_docs does not match corpus predicate: "
                f"ledger={sorted(eligible)} computed={computed}"
            )
    elif tag == "source_filter":
        kind = ev.get("kind")
        if kind is None or kind not in {"synthetic-note", "synthetic-faq", "synthetic-log"}:
            raise ValueError(f"query {qid} source_filter evidence missing/unknown kind {kind!r}")
    elif tag == "session_filter":
        filter_value = ev.get("session")
        if filter_value is None:
            raise ValueError(f"query {qid} session_filter evidence missing session")
        if filter_value not in {meta["session"] for meta in corpus.values()}:
            raise ValueError(f"query {qid} session_filter references unknown session {filter_value}")
    if tag == "duplicate_groups":
        groups = ev.get("groups")
        if not groups:
            raise ValueError(f"query {qid} duplicate_groups evidence missing groups")
        seen_members: set[str] = set()
        for group in groups:
            if len(group) < 2:
                raise ValueError(f"query {qid} duplicate_groups group must have >=2 members")
            if len(set(group)) != len(group):
                raise ValueError(f"query {qid} duplicate_groups group has duplicate members")
            for doc_id in group:
                if doc_id not in corpus:
                    raise ValueError(f"query {qid} duplicate_groups references unknown doc {doc_id}")
                if doc_id in seen_members:
                    raise ValueError(f"query {qid} duplicate_groups groups overlap on {doc_id}")
                seen_members.add(doc_id)
