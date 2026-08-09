"""Schema migrations package.

``_structural_tables`` parses ``schema.sql`` to enumerate the expected table
names, so a machine test can prove the forward-only ``0001_initial`` migration
and the legacy ``schema.sql`` never silently diverge.
"""

from __future__ import annotations

import pathlib
import re


def _structural_tables() -> set[str]:
    """Return the set of table names declared by the legacy schema.sql."""
    schema = pathlib.Path(__file__).resolve().parents[2] / "schema.sql"
    text = schema.read_text(encoding="utf-8")
    return set(re.findall(r"CREATE TABLE IF NOT EXISTS\s+([a-z_]+)", text))
