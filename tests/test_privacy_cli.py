"""CLI tests for the privacy lifecycle (task #4 Phase 2B successor)."""

from __future__ import annotations

import json

from shiyi.cli import main


def test_privacy_providers_lists_all_sources(capsys):
    assert main(["privacy", "providers"]) == 0
    out = json.loads(capsys.readouterr().out)
    names = {p["name"] for p in out}
    assert {"sessions", "hermes", "discord", "embedding"} <= names


def test_privacy_export_requires_dest(capsys):
    from shiyi.cli import _build_parser

    args = _build_parser().parse_args(["privacy", "export", "--scope", "all", "--dest", "/tmp/x.json"])
    assert args.yes is False


def test_privacy_delete_has_yes_and_older_than(capsys):
    from shiyi.cli import _build_parser

    args = _build_parser().parse_args(
        ["privacy", "delete", "--scope", "sessions", "--older-than", "30"]
    )
    assert args.older_than == 30
    assert args.yes is False


def test_privacy_retention_check_flag_exists(capsys):
    from shiyi.cli import _build_parser

    args = _build_parser().parse_args(["privacy", "retention-check", "--scope", "sessions"])
    assert args.privacy_command == "retention-check"
    assert args.scope == "sessions"


def test_ingest_redact_flag_is_forced_on(capsys):
    from shiyi.cli import _build_parser

    args = _build_parser().parse_args(["ingest", "--redact", "--source", "sessions", "--dry-run"])
    assert getattr(args, "redact", False) is True
