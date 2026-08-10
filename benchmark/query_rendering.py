"""Shared query rendering for the task #11 benchmark.

The same canonical-query renderer is used by the vector generator and the live
harness so the committed vectors hash, manifest, and results always bind to the
same query text.

Rule (frozen):
- non-multi-turn: canonical query = whitespace-normalized `query_text`
  (never an unvalidated independent string).
- multi-turn: canonical query = whitespace-normalized
  `" ".join(conversation_context) + " " + query_text`.
"""

from __future__ import annotations


def _norm(text: str) -> str:
    return " ".join(text.split())


def render_canonical_query(judgment: dict) -> str:
    """Return the canonical retrieval query for a judgment (deterministic).

    Multi-turn uses the conversation context plus the current query; all other
    classes use the whitespace-normalized `query_text`. The result MUST match
    the fixture's `canonical_query` (validated by tests).
    """
    qtext = _norm(judgment.get("query_text", ""))
    context = judgment.get("conversation_context") or []
    if judgment.get("class") == "multi_turn" and context:
        return _norm(" ".join(context) + " " + qtext)
    return qtext
