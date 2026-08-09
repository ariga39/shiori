from datetime import UTC, datetime

import ingest


def test_none_returns_none():
    assert ingest.parse_timestamp(None) is None


def test_iso_z_parses():
    ts = ingest.parse_timestamp("2026-08-03T12:34:56Z")
    assert ts is not None
    assert (ts.year, ts.month, ts.day, ts.hour, ts.minute, ts.second) == (2026, 8, 3, 12, 34, 56)


def test_iso_fractional_parses():
    ts = ingest.parse_timestamp("2026-08-03T12:34:56.123Z")
    assert ts is not None
    assert (ts.year, ts.month, ts.day, ts.hour, ts.minute, ts.second) == (2026, 8, 3, 12, 34, 56)


def test_epoch_seconds_parses():
    ts = ingest.parse_timestamp(1785760496)
    assert ts == datetime(2026, 8, 3, 12, 34, 56, tzinfo=UTC)


def test_epoch_milliseconds_parses_same():
    ts = ingest.parse_timestamp(1785760496000)
    assert ts == datetime(2026, 8, 3, 12, 34, 56, tzinfo=UTC)


def test_bad_format_returns_none():
    assert ingest.parse_timestamp("not-a-timestamp") is None


def test_bad_type_returns_none():
    assert ingest.parse_timestamp(["2026-08-03T00:00:00Z"]) is None
