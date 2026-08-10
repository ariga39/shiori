"""Tests for the privacy lifecycle seam (shiyi.privacy).

Fail-closed contract:
- minimize() never echoes a value it cannot positively classify as safe to keep.
- export()/delete() never touch the filesystem unless confirmation is explicit.
- retention_policy() exposes a per-source retention window.
- providers() discloses every registered source's data flow and retention.
"""

from __future__ import annotations

import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from shiyi import privacy  # noqa: E402


def test_minimize_fail_closed_redacts_pii() -> None:
    text = "my token sk_live_1234567890abcdef lives in /home/alice/.shiyi/config.json, email a@b.example"
    out = privacy.minimize(text)
    assert "sk_live_1234567890abcdef" not in out
    assert "a@b.example" not in out


def test_minimize_keeps_known_safe_text() -> None:
    text = "session memory chunk about fund planning on 2026-08-03"
    out = privacy.minimize(text)
    assert "fund planning" in out


def test_export_requires_managed_store(tmp_path) -> None:
    dest = tmp_path / "export.json"
    with pytest.raises(privacy.PrivacyError):
        privacy.export(scope="all", dest=dest, confirm=False)


def test_delete_requires_managed_store(tmp_path) -> None:
    with pytest.raises(privacy.PrivacyError):
        privacy.delete(scope="all", confirm=False)


def test_retention_policy_has_valid_days() -> None:
    sources = privacy.registered_sources()
    assert sources
    for source in sources:
        policy = privacy.retention_policy(source)
        assert policy.retention_days > 0


def test_providers_disclose_all_registered_sources() -> None:
    providers = privacy.providers()
    names = {p["name"] for p in providers}
    for source in privacy.registered_sources():
        assert source.name in names
    for provider in providers:
        if provider["name"] == "embedding":
            assert provider["status"] == "not_configured"
            continue
        assert provider["endpoint"]
        assert provider["retention_days"] > 0


def test_extract_text_from_message_redacts_pii() -> None:
    import ingest

    obj = {
        "message": {
            "role": "user",
            "content": "token sk_live_abcdef0123456789 and email a@b.example and /home/u/config.json",
        }
    }
    out = ingest.extract_text_from_message(obj)
    assert out is not None
    assert "sk_live_abcdef0123456789" not in out
    assert "a@b.example" not in out
    assert "/home/u/config.json" not in out


def test_format_message_redacts_pii() -> None:
    import ingest_discord

    msg = {
        "type": 0,
        "timestamp": "2026-08-03T10:00:00Z",
        "author": {"username": "alice"},
        "content": "token sk_live_abcdef0123456789 email a@b.example /home/u/config.json",
    }
    out = ingest_discord.format_message(msg)
    assert out is not None
    assert "sk_live_abcdef0123456789" not in out
    assert "a@b.example" not in out
    assert "/home/u/config.json" not in out
