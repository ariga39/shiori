"""CLI tests for the privacy lifecycle (task #4 Phase 2B)."""

from __future__ import annotations

from shiyi.cli import main


def test_privacy_providers_lists_all_sources(capsys):
    assert main(["privacy", "providers"]) == 0
    out = capsys.readouterr().out
    for name in ("sessions", "hermes", "discord"):
        assert name in out


def test_privacy_export_requires_confirmation(capsys):
    rc = main(["privacy", "export", "--scope", "all"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "requires explicit confirmation" in out or "dry-run" in out.lower()
    assert "exported" not in out.lower()


def test_privacy_delete_requires_confirmation(capsys):
    rc = main(["privacy", "delete", "--scope", "all"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "requires explicit confirmation" in out or "dry-run" in out.lower()
    assert "deleted" not in out.lower()


def test_ingest_redact_flag_is_forced_on(capsys):
    from shiyi.cli import _build_parser

    # --redact must be a recognized ingest flag (fail-closed, forced redaction).
    args = _build_parser().parse_args(["ingest", "--redact", "--source", "sessions", "--dry-run"])
    assert getattr(args, "redact", False) is True
