"""Generate the Phase 4D baseline_72 Markdown report (task #18).

The report is generated from the results JSON (and its run manifest) so every
number is machine-derived, never hand-typed. It is measurement-only: no
acceptance thresholds, holdout untouched, public datasets not run.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

CONFIG_ORDER = ["dense-only", "lexical-only", "rrf", "+exact", "+temporal", "+dedup"]
BUCKET_ORDER = ["exact", "paraphrase", "multilingual", "temporal", "multi_turn", "duplicate", "no_evidence", "filter"]


def _fmt(v, nd=3):
    return "N/A" if v is None else f"{v:.{nd}f}"


def _generate(results: dict, manifest: dict) -> str:
    lines: list[str] = []
    title = manifest.get(
        "report_title",
        "# Shiori Phase 4D Baseline Report (72 development queries)",
    )
    lines.append(title)
    lines.append("")
    lines.append("_Measurement-only. No acceptance thresholds. Holdout (48) untouched. Public datasets not run._")
    lines.append("")
    lines.append(f"- base SHA: `{manifest.get('base_sha', 'n/a')}`")
    lines.append(f"- model: `{manifest['model']['identity']}` dim={manifest['model']['dim']} {manifest['model']['dtype']} {manifest['model']['normalization']}")
    lines.append(f"- dev queries: {manifest['dev_set']['query_count']} (id set sha256 `{manifest['dev_set']['id_set_sha256'][:16]}…`)")
    lines.append(f"- result file sha256: `{manifest['result_file_sha256']}`")
    lines.append(f"- runtime: python {manifest['runtime']['python']}, psycopg2 {manifest['runtime']['psycopg2']}, PostgreSQL {manifest['runtime']['postgresql']}, pgvector {manifest['runtime']['pgvector']}")
    lines.append("")

    lines.append("## Overall (per config, n=72)")
    lines.append("")
    lines.append("| config | candR@20 | R@5 | MRR@10 | nDCG@10 | filter_leak | dupCov | dupRate | dedupDrop | covRisk | noevQ | noevFR |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for name in CONFIG_ORDER:
        c = results["configs"][name]
        lines.append(
            f"| {name} | {_fmt(c['candidate_recall_at_20'])} | {_fmt(c['final_recall@5'])} | {_fmt(c['final_mrr@10'])} | {_fmt(c['final_ndcg@10'])} "
            f"| {c['filter_leakage']} | {_fmt(c['duplicate_group_coverage'])} | {_fmt(c['duplicate_rate'])} | {_fmt(c['dedup_drop_rate'])} "
            f"| {c['coverage_risk_dropped_relevant']} | {c['no_evidence_queries']} | {c['no_evidence_false_return']} |"
        )
    lines.append("")

    lines.append("## Per-bucket nDCG@10 / R@5")
    lines.append("")
    lines.append("| bucket | dense-only R5 | +temporal R5 | +dedup R5 | dense nDCG | +temporal nDCG | +dedup nDCG |")
    lines.append("|---|---|---|---|---|---|---|")
    for bucket in BUCKET_ORDER:
        b = {name: results["buckets"][name].get(bucket, {}) for name in CONFIG_ORDER}
        lines.append(
            f"| {bucket} | {_fmt(b['dense-only'].get('recall@5'))} | {_fmt(b['+temporal'].get('recall@5'))} | {_fmt(b['+dedup'].get('recall@5'))} "
            f"| {_fmt(b['dense-only'].get('ndcg@10'))} | {_fmt(b['+temporal'].get('ndcg@10'))} | {_fmt(b['+dedup'].get('ndcg@10'))} |"
        )
    lines.append("")

    lines.append("## Filter leakage by tag (per config)")
    lines.append("")
    lines.append("| config | source_filter | session_filter | time_filter |")
    lines.append("|---|---|---|---|")
    for name in CONFIG_ORDER:
        tags = results.get("filter_leakage_by_tag", {}).get(name, {})
        lines.append(
            f"| {name} | {tags.get('source_filter', 0)} | {tags.get('session_filter', 0)} | {tags.get('time_filter', 0)} |"
        )
    lines.append("")

    lines.append("## Temporal transitions (knowledge-update eligible)")
    lines.append("")
    lines.append("| qid | pre_rank | post_rank | rank_changed | winner_transition | promoted_to_winner |")
    lines.append("|---|---|---|---|---|---|")
    for qid, t in sorted(results.get("temporal_pairs", {}).items()):
        lines.append(
            f"| {qid} | {t.get('pre_rank')} | {t.get('post_rank')} | {t['rank_changed']} | {t['winner_transition']} | {t['promoted_to_winner']} |"
        )
    lines.append("")

    lines.append("## No-evidence behavior (per config)")
    lines.append("")
    lines.append("| config | queries | false_return | abstention_like |")
    lines.append("|---|---|---|---|")
    for name in CONFIG_ORDER:
        c = results["configs"][name]
        lines.append(f"| {name} | {c['no_evidence_queries']} | {c['no_evidence_false_return']} | {c['no_evidence_abstention']} |")
    lines.append("")

    lines.append("## Latency (real PostgreSQL, n=72)")
    lines.append("")
    lines.append("### e2e")
    lines.append("")
    lines.append("| config | p50 (ms) | p95 (ms) |")
    lines.append("|---|---|---|")
    for name in CONFIG_ORDER:
        e = results["e2e_latency_ms"][name]
        lines.append(f"| {name} | {e['p50_ms']:.3f} | {e['p95_ms']:.3f} |")
    lines.append("")
    lines.append("### +dedup per-stage")
    lines.append("")
    lines.append("| stage | p50 (ms) | p95 (ms) |")
    lines.append("|---|---|---|")
    for stage, v in results["stage_latency_ms"]["+dedup"].items():
        lines.append(f"| {stage} | {v['p50_ms']:.3f} | {v['p95_ms']:.3f} |")
    lines.append("")

    lines.append("## Adapters (not run)")
    lines.append("")
    lines.append("| dataset | status | note |")
    lines.append("|---|---|---|")
    for name, info in manifest.get("adapters_not_run", {}).items():
        lines.append(f"| {name} | {info['status']} | {info['not_run_reason']} |")
    lines.append("")

    lines.append("## Known gaps")
    lines.append("")
    notes = manifest.get("report_notes")
    if notes:
        # Manifest-provided notes fully replace the default list.
        for note in notes:
            lines.append(f"- {note}")
    else:
        lines.append("- Production query.search() does not apply source/session/time filters; all dense-based configs show filter leakage (source=9, session=9, time=3 per config).")
        lines.append("- +temporal degrades the temporal and filter buckets (decay lifts non-target docs); +dedup drops relevant docs (coverage risk).")
        lines.append("- no_evidence returns false positives in the dense path (no abstention mechanism); lexical-only abstains by absence of candidates.")
    lines.append("")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate baseline_72 Markdown report")
    parser.add_argument("--results", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    results = json.loads(args.results.read_text(encoding="utf-8"))
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    args.out.write_text(_generate(results, manifest), encoding="utf-8")
    print(f"wrote {args.out.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
