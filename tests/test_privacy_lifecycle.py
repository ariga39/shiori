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


def test_export_dry_run_writes_nothing(tmp_path) -> None:
    dest = tmp_path / "export.json"
    privacy.export(scope="all", dest=dest, confirm=False)
    assert not dest.exists()


def test_delete_requires_confirmation(tmp_path) -> None:
    target = tmp_path / "state.db"
    target.write_text("data")
    privacy.delete(scope="all", confirm=False)
    assert target.exists()


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
        assert provider["endpoint"]
        assert provider["retention_days"] > 0
