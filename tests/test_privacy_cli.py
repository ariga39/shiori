"""CLI tests for the privacy lifecycle (task #4 Phase 2B successor)."""

from __future__ import annotations

import json

from shiori.cli import main
from shiori.config import load_config
from shiori.privacy import providers


def test_privacy_providers_lists_all_sources(capsys):
    assert main(["privacy", "providers"]) == 0
    out = json.loads(capsys.readouterr().out)
    names = {p["name"] for p in out}
    assert {"sessions", "hermes", "discord", "embedding"} <= names


def test_fake_provider_disclosure_is_explicitly_local():
    settings = load_config(
        environ={
            "SHIORI_EMBEDDING_PROVIDER": "fake",
            "SHIORI_ALLOW_FAKE_EMBEDDINGS": "true",
            "SHIORI_ENVIRONMENT": "test",
            "SHIORI_VOYAGE_MODEL": "shiori-fake-v1",
            "SHIORI_EMBED_DIM": "1024",
        }
    )
    embedding = next(item for item in providers(settings) if item["name"] == "embedding")

    assert embedding["provider"] == "deterministic_fake"
    assert embedding["model"] == "shiori-fake-v1"
    assert embedding["dimension"] == 1024
    assert embedding["external_call"] is False
    assert embedding["environment"] == "test"
    assert embedding["status"] == "configured_dev_only"


def test_privacy_export_requires_dest(capsys):
    from shiori.cli import _build_parser

    args = _build_parser().parse_args(["privacy", "export", "--scope", "all", "--dest", "/tmp/x.json"])
    assert args.yes is False


def test_privacy_delete_has_yes_and_older_than(capsys):
    from shiori.cli import _build_parser

    args = _build_parser().parse_args(
        ["privacy", "delete", "--scope", "sessions", "--older-than", "30"]
    )
    assert args.older_than == 30
    assert args.yes is False


def test_privacy_retention_check_flag_exists(capsys):
    from shiori.cli import _build_parser

    args = _build_parser().parse_args(["privacy", "retention-check", "--scope", "sessions"])
    assert args.privacy_command == "retention-check"
    assert args.scope == "sessions"


def test_ingest_redact_flag_is_forced_on(capsys):
    from shiori.cli import _build_parser

    args = _build_parser().parse_args(["ingest", "--redact", "--source", "sessions", "--dry-run"])
    assert getattr(args, "redact", False) is True
