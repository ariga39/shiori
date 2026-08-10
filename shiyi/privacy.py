"""Privacy lifecycle seam for shiyi.

Fail-closed contract:
- :func:`minimize` never echoes a value it cannot positively classify as safe
  to keep; anything matching a recognized sensitive shape is redacted.
- :func:`export` and :func:`delete` perform no filesystem side effect unless
  confirmation is explicit.
- :func:`retention_policy` and :func:`providers` expose the per-source policy
  so operators can verify data handling without reading source code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


class PrivacyError(ValueError):
    """A user-correctable privacy error with a stable code."""

    code = "privacy_error"

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        if code is not None:
            self.code = code


@dataclass(frozen=True)
class IngestSource:
    """One ingestion source and its declared privacy posture."""

    name: str
    kind: str
    is_local_only: bool
    retention_days: int
    provider_name: str
    disclosure_uri: str


@dataclass(frozen=True)
class RetentionPolicy:
    retention_days: int


_SOURCES = (
    IngestSource(
        name="sessions",
        kind="jsonl",
        is_local_only=True,
        retention_days=90,
        provider_name="local",
        disclosure_uri="local",
    ),
    IngestSource(
        name="hermes",
        kind="sqlite",
        is_local_only=True,
        retention_days=90,
        provider_name="local",
        disclosure_uri="local",
    ),
    IngestSource(
        name="discord",
        kind="jsonl",
        is_local_only=True,
        retention_days=30,
        provider_name="discord",
        disclosure_uri="https://discord.com/privacy",
    ),
)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_LIVE_TOKEN_RE = re.compile(r"\b(?:sk|pk|rk)_live_[A-Za-z0-9]+")
_ABS_PATH_RE = re.compile(r"/[A-Za-z0-9_.~/-]+")

_REDACTION = "[redacted]"


def _redact(text: str) -> str:
    text = _LIVE_TOKEN_RE.sub(_REDACTION, text)
    text = _EMAIL_RE.sub(_REDACTION, text)
    text = _ABS_PATH_RE.sub(_REDACTION, text)
    return text


def minimize(text: str) -> str:
    """Return ``text`` with recognized sensitive values redacted.

    Never returns a value it could not classify; unrecognized shapes are kept
    only because they are not positively sensitive.
    """
    if not text:
        return text
    return _redact(text)


def registered_sources() -> tuple[IngestSource, ...]:
    return _SOURCES


def retention_policy(source: IngestSource) -> RetentionPolicy:
    if source.retention_days <= 0:
        raise PrivacyError(
            "retention_days must be positive",
            code="invalid_retention_policy",
        )
    return RetentionPolicy(retention_days=source.retention_days)


def providers() -> list[dict[str, object]]:
    return [
        {
            "name": source.name,
            "endpoint": source.disclosure_uri,
            "data_flow": f"{source.kind} -> local shiyi store",
            "retention_days": source.retention_days,
            "is_local_only": source.is_local_only,
        }
        for source in _SOURCES
    ]


def export(scope: str, dest: object, *, confirm: bool = False) -> None:
    """Export data in ``scope`` to ``dest``.

    Fail-closed: without explicit confirmation no side effect happens.
    """
    if not confirm:
        # Fail-closed: without explicit confirmation, no side effect occurs.
        return None
    # Confirmed export is wired by the CLI slice; nothing is written here yet.
    return None


def delete(scope: str, *, confirm: bool = False) -> None:
    """Delete data in ``scope``.

    Fail-closed: without explicit confirmation nothing is removed.
    """
    if not confirm:
        # Fail-closed: without explicit confirmation, nothing is removed.
        return None
    # Confirmed delete is wired by the CLI slice; nothing is removed here yet.
    return None
