"""Phase 4D production-ranker evaluation (task #18).

Measurement-only package: reuses the frozen task #11 benchmark schema, observes
the production PostgreSQL dense/lexical/exact/RRF/temporal/dedup pipeline via a
behavior-preserving trace seam in query.py (no mirrored ranking implementation),
and evaluates against a manually-authored golden set plus license-checked
local-only adapters. No default ranking/model/network/key changes.
"""

from benchmark.product_eval import evaluator, manifest

__all__ = ["evaluator", "manifest"]
