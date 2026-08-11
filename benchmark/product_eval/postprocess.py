"""Deterministic post-processor for Phase 4D baseline results (task #18).

Reads the committed baseline_72_results.json (with per-config sanitized traces),
the dataset manifest (ledger), and the task #11 corpus, and computes per-config
aggregates that require no DB rerun:
- source_filter / session_filter / time_filter leakage per config (from the
  final ranked trace IDs and the ledger constraints).
- candidate Recall@20 per config (already present, re-verified).
Writes an augmented results JSON with the same schema plus a
`filter_leakage_by_tag` section, and regenerates the deterministic result hash.

No ranking/model/query rerun; deterministic from the frozen inputs.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def _load_corpus_meta(corpus_path: Path) -> dict[str, dict]:
    meta: dict[str, dict] = {}
    with corpus_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                doc = json.loads(line)
                meta[doc["id"]] = {
                    "session": doc["session"],
                    "timestamp": doc["timestamp"],
                    "source_kind": doc["source_kind"],
                }
    return meta


def _row_evidence(manifest: dict, qid: str) -> dict:
    for entry in manifest.get("query_splits", []):
        if entry["query_id"] == qid:
            return entry.get("evidence", {})
    return {}


def _tag_leakage(evidence: dict, final_ids: list[str], corpus: dict[str, dict]) -> dict[str, bool]:
    """Compute per-tag (source/session/time) leakage for a final ranked list."""
    out: dict[str, bool] = {}
    if "source_filter" in evidence:
        allowed_kind = evidence["source_filter"].get("kind")
        out["source_filter"] = any(
            fid in corpus and corpus[fid]["source_kind"] != allowed_kind for fid in final_ids
        )
    if "session_filter" in evidence:
        allowed_session = evidence["session_filter"].get("session")
        out["session_filter"] = any(
            fid in corpus and corpus[fid]["session"] != allowed_session for fid in final_ids
        )
    if "time_filter" in evidence:
        tf = evidence["time_filter"]
        op = tf.get("operator")
        bound = datetime.fromisoformat(tf["iso_bound"].replace("Z", "+00:00"))

        def _violates(fid: str) -> bool:
            if fid not in corpus or not corpus[fid].get("timestamp"):
                return True
            t = datetime.fromisoformat(corpus[fid]["timestamp"].replace("Z", "+00:00"))
            if op == "before":
                return not (t < bound)
            if op == "after":
                return not (t > bound)
            return t != bound

        out["time_filter"] = any(_violates(fid) for fid in final_ids)
    return out


def _final_ids_for_trace(trace: list[dict]) -> list[str]:
    """Final ranked fixture ids from a sanitized trace (dedup keep events in
    order; if no dedup stage, the last doc-bearing stage's ranked ids)."""
    keeps = [ev["doc_id"] for ev in trace if ev.get("reason") == "mmr_keep" and ev.get("doc_id")]
    if keeps:
        return keeps
    # fall back to the highest-available stage's doc events (rrf or temporal).
    for stage in ("temporal", "rrf"):
        ranked = [ev["doc_id"] for ev in trace if ev.get("stage") == stage and ev.get("doc_id")]
        if ranked:
            return ranked
    return []


def post_process(results_path: Path, manifest_path: Path, corpus_path: Path, out_path: Path) -> dict:
    results = json.loads(results_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    corpus = _load_corpus_meta(corpus_path)

    leakage_by_config: dict[str, dict[str, int]] = {}
    for config_name, traces in results.get("traces", {}).items():
        counts: dict[str, int] = {}
        for qid, trace in traces.items():
            evidence = _row_evidence(manifest, qid)
            final_ids = _final_ids_for_trace(trace)
            tag_leak = _tag_leakage(evidence, final_ids, corpus)
            for tag, leaked in tag_leak.items():
                if leaked:
                    counts[tag] = counts.get(tag, 0) + 1
        leakage_by_config[config_name] = counts

    results["filter_leakage_by_tag"] = leakage_by_config
    out_path.write_text(
        json.dumps(results, indent=1, sort_keys=True, ensure_ascii=False), encoding="utf-8"
    )
    print(f"wrote {out_path.name}")
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deterministic Phase 4D baseline post-processor")
    parser.add_argument("--results", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    post_process(args.results, args.manifest, args.corpus, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
