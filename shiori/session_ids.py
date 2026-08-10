"""Shared session-id provenance helpers.

Both the ingest adapters and the privacy lifecycle must derive session ids
from the same rules so that the resolver never diverges from what was actually
written. This module is the single source of truth for those derivations.
"""

from __future__ import annotations

import os


def discord_session_id(channel_name: str) -> str:
    """Mirror ingest_discord.build_chunks: ``discord-{channel_name}``.

    Unconditional: ``general.jsonl`` -> ``discord-general`` and
    ``discord-general.jsonl`` -> ``discord-discord-general``. No normalization
    is applied, so the resolver always matches what the adapter wrote.
    """
    return f"discord-{channel_name}"


def derive_session_id(file_path: str) -> str:
    """Mirror ingest.derive_session_id for a provenance file path."""
    basename = os.path.basename(file_path)
    uuid_part = basename.split(".")[0]
    if ".deleted." in basename:
        return uuid_part + ":deleted"
    return uuid_part
