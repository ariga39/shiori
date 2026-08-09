"""Deterministic, offline-only embedding fixture helpers.

This module is deliberately under ``tests/``. Production code must configure
and call a real embedding provider; it must never import this helper.
"""

from __future__ import annotations

import hashlib
import math


def deterministic_embedding(text: str, dimension: int = 1024) -> list[float]:
    """Return a stable unit vector without network access or credentials."""
    if not isinstance(text, str) or dimension <= 0:
        raise ValueError("text must be a string and dimension must be positive")
    values: list[float] = []
    seed = text.encode("utf-8")
    for index in range(dimension):
        digest = hashlib.sha256(seed + index.to_bytes(4, "big")).digest()
        values.append((int.from_bytes(digest[:8], "big") / 2**64) * 2.0 - 1.0)
    norm = math.sqrt(sum(value * value for value in values))
    return [value / norm for value in values]
