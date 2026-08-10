"""Typed, explicit configuration for shiori.

Configuration precedence is deliberately boring and observable:

1. values passed directly to :func:`load_config`;
2. ``SHIORI_*`` environment variables;
3. an explicitly selected JSON/TOML config file;
4. safe non-secret defaults (chunking and retry limits only).

Legacy ``SHIYI_*`` environment variables and ``[shiyi]`` config sections are
accepted as compatible inputs for one migration cycle.  Setting both a
canonical ``SHIORI_*``/``[shiori]`` value and its legacy alias for the same
field fails closed; it is never silently resolved.

Data-source paths, database credentials, and embedding provider settings have
no implicit OpenClaw/Hermes/Discord defaults.  The old paths are available
only through the explicit ``legacy_openclaw`` migration switch.
"""

from __future__ import annotations

import json
import os
import re
import stat
import tomllib
from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit


class ConfigError(ValueError):
    """A user-correctable configuration error with a stable code."""

    code = "invalid_config"

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        if code is not None:
            self.code = code


def _path(value: Any) -> Path | None:
    if value is None or value == "":
        return None
    if not isinstance(value, (str, os.PathLike)):
        raise ConfigError("path values must be strings")
    return Path(value).expanduser()


def _text(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ConfigError("text values must be strings")
    return value


def _positive_int(value: Any, name: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be an integer", code="invalid_config_value") from exc
    if result <= 0:
        raise ConfigError(f"{name} must be positive", code="invalid_config_value")
    return result


def _read_config_file(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"config file cannot be read: {path}", code="config_file_unreadable") from exc

    try:
        if path.suffix.lower() == ".toml":
            data = tomllib.loads(raw)
        else:
            data = json.loads(raw)
    except (json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"config file is not valid JSON/TOML: {path}", code="invalid_config_file") from exc

    if not isinstance(data, dict):
        raise ConfigError("config file root must be an object", code="invalid_config_file")
    has_canonical = isinstance(data.get("shiori"), dict)
    has_legacy = isinstance(data.get("shiyi"), dict)
    if has_canonical and has_legacy:
        raise ConfigError(
            "config file must not define both [shiori] and [shiyi] sections",
            code="config_section_conflict",
        )
    if has_canonical:
        section = data["shiori"]
    elif has_legacy:
        section = data["shiyi"]
    else:
        section = data
    if not isinstance(section, dict):
        raise ConfigError("[shiori] config section must be an object", code="invalid_config_file")
    return dict(section)


def _key_value_file(path: Path) -> dict[str, str]:
    try:
        file_stat = path.lstat()
    except OSError as exc:
        raise ConfigError("database credential file is unreadable", code="credential_file_unreadable") from exc
    mode = stat.S_IMODE(file_stat.st_mode)
    if (
        not stat.S_ISREG(file_stat.st_mode)
        or path.is_symlink()
        or mode & 0o077
        or not mode & 0o400
        or mode & 0o100
    ):
        raise ConfigError(
            "database credential file must be a private regular file",
            code="credential_file_permissions",
        )
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ConfigError("database credential file is unreadable", code="credential_file_unreadable") from exc
    values: dict[str, str] = {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ConfigError("database credential file contains an invalid line", code="invalid_database_config")
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in values:
            raise ConfigError(
                "database credential file contains a duplicate or empty key",
                code="invalid_database_config",
            )
        values[key] = value.strip()
    return values


def _validated_pg_credentials(values: dict[str, str]) -> dict[str, str]:
    """Validate and return the two documented psycopg2 credential shapes."""
    if "dsn" in values:
        if len(values) != 1 or not values["dsn"]:
            raise ConfigError("database credential dsn is invalid", code="invalid_database_config")
        return {"dsn": values["dsn"]}

    required = ("host", "port", "dbname", "user", "password")
    unknown = sorted(set(values) - set(required))
    if unknown:
        raise ConfigError("database credential file contains an unknown key", code="invalid_database_config")
    missing = [key for key in required if not values.get(key)]
    if missing:
        raise ConfigError(
            "database credentials missing: " + ", ".join(missing),
            code="invalid_database_config",
        )
    try:
        port = int(values["port"])
    except ValueError as exc:
        raise ConfigError("database credential port is invalid", code="invalid_database_config") from exc
    if not 1 <= port <= 65535:
        raise ConfigError("database credential port is invalid", code="invalid_database_config")
    return {**values, "port": str(port)}


@dataclass(frozen=True)
class Settings:
    """Validated runtime settings.

    ``None`` is intentional for all data-source and provider requirements: a
    production command must say where it gets data and how it embeds it.
    """

    sessions_dir: Path | None = None
    hermes_db: Path | None = None
    discord_archive_dir: Path | None = None
    database_dsn: str | None = None
    pg_cred_file: Path | None = None
    embedding_provider: str | None = None
    voyage_api_url: str | None = None
    voyage_api_key: str | None = None
    voyage_key_file: Path | None = None
    voyage_model: str | None = None
    embed_dim: int | None = None
    allow_fake_embeddings: bool = False
    environment: str | None = None
    log_file: Path | None = None
    chunk_tokens: int = 400
    chunk_overlap: int = 80
    voyage_batch_size: int = 128
    voyage_rps_limit: int = 8
    embed_timeout: int = 60
    max_retries: int = 3
    sessions_lock_id: int = 784321
    discord_lock_id: int = 784322
    legacy_openclaw: bool = False

    def __post_init__(self) -> None:
        if self.environment is not None and self.environment not in {"development", "test", "production"}:
            raise ConfigError(
                "environment must be development, test, or production",
                code="invalid_environment",
            )
        if self.chunk_overlap >= self.chunk_tokens:
            raise ConfigError("chunk_overlap must be smaller than chunk_tokens", code="invalid_config_value")
        for name in (
            "chunk_tokens",
            "chunk_overlap",
            "voyage_batch_size",
            "voyage_rps_limit",
            "embed_timeout",
            "max_retries",
            "sessions_lock_id",
            "discord_lock_id",
        ):
            if not isinstance(getattr(self, name), int) or getattr(self, name) <= 0:
                raise ConfigError(f"{name} must be a positive integer", code="invalid_config_value")
        if self.embed_dim is not None and (not isinstance(self.embed_dim, int) or self.embed_dim <= 0):
            raise ConfigError("embed_dim must be a positive integer", code="invalid_config_value")

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        config_path: str | os.PathLike[str] | None = None,
        legacy_openclaw: bool = False,
        **overrides: Any,
    ) -> Settings:
        """Build settings using explicit > environment > file precedence."""
        return load_config(
            environ=environ,
            config_path=config_path,
            legacy_openclaw=legacy_openclaw,
            **overrides,
        )

    def require_source(self, source: str) -> Path:
        field = {
            "sessions": "sessions_dir",
            "hermes": "hermes_db",
            "discord": "discord_archive_dir",
        }.get(source)
        if field is None:
            raise ConfigError(f"unknown source: {source}", code="unknown_source")
        value = getattr(self, field)
        if value is None:
            raise ConfigError(
                f"{source} source is disabled; configure SHIORI_{field.upper()}",
                code="source_not_configured",
            )
        if not value.exists():
            raise ConfigError(f"{source} source does not exist", code="source_not_found")
        if source == "hermes" and not value.is_file():
            raise ConfigError("hermes source must be a file", code="invalid_source_type")
        if source != "hermes" and not value.is_dir():
            raise ConfigError(f"{source} source must be a directory", code="invalid_source_type")
        return value

    def require_database(self) -> str | Path:
        if self.database_dsn:
            return self.database_dsn
        if self.pg_cred_file:
            if not self.pg_cred_file.is_file():
                raise ConfigError("database credential file does not exist", code="credential_file_not_found")
            return self.pg_cred_file
        raise ConfigError(
            "database is not configured; set SHIORI_DATABASE_DSN or SHIORI_PG_CRED",
            code="database_not_configured",
        )

    def require_embedding(self) -> None:
        missing = []
        if not self.embedding_provider:
            missing.append("SHIORI_EMBEDDING_PROVIDER")
        if self.embedding_provider == "fake":
            if self.environment not in {"development", "test"}:
                raise ConfigError(
                    "fake embedding provider requires environment=development or test",
                    code="fake_embedding_environment_required",
                )
            if not self.allow_fake_embeddings:
                raise ConfigError(
                    "fake embedding provider requires explicit local-development opt-in",
                    code="fake_embedding_not_allowed",
                )
            if not self.voyage_model:
                missing.append("SHIORI_VOYAGE_MODEL")
            if self.embed_dim is None:
                missing.append("SHIORI_EMBED_DIM")
            if missing:
                raise ConfigError(
                    "embedding configuration is incomplete: " + ", ".join(missing),
                    code="embedding_not_configured",
                )
            assert self.voyage_model is not None
            if not (self.voyage_model.startswith("shiori-fake-") or self.voyage_model.startswith("shiyi-fake-")):
                raise ConfigError(
                    "fake embeddings require a model name in the reserved shiori-fake-* namespace",
                    code="fake_embedding_model_reserved",
                )
            if self.embed_dim != 1024:
                raise ConfigError(
                    "this schema requires embed_dim=1024",
                    code="unsupported_embedding_dimension",
                )
            return
        if not self.voyage_api_key and not self.voyage_key_file:
            missing.append("SHIORI_VOYAGE_API_KEY or SHIORI_VOYAGE_KEY_FILE")
        elif self.voyage_key_file and not self.voyage_key_file.is_file():
            raise ConfigError("Voyage key file does not exist", code="key_file_not_found")
        if not self.voyage_model:
            missing.append("SHIORI_VOYAGE_MODEL")
        if self.embed_dim is None:
            missing.append("SHIORI_EMBED_DIM")
        if missing:
            raise ConfigError(
                "embedding configuration is incomplete: " + ", ".join(missing),
                code="embedding_not_configured",
            )
        assert self.voyage_model is not None
        if self.voyage_model.startswith("shiyi-fake-") or self.voyage_model.startswith("shiori-fake-"):
            raise ConfigError(
                "the shiori-fake-* model namespace is reserved for deterministic local vectors",
                code="fake_embedding_model_reserved",
            )
        if self.embedding_provider != "voyage":
            raise ConfigError(
                f"unsupported embedding provider: {self.embedding_provider}",
                code="unsupported_embedding_provider",
            )
        if self.embed_dim != 1024:
            raise ConfigError(
                "this schema requires embed_dim=1024",
                code="unsupported_embedding_dimension",
            )

    def read_voyage_key(self) -> str:
        self.require_embedding()
        if self.embedding_provider == "fake":
            raise ConfigError(
                "fake embedding provider does not use a provider key",
                code="fake_embedding_key_unavailable",
            )
        if self.voyage_api_key:
            # SHIORI_VOYAGE_KEY is the canonical name.  If its
            # value names an existing file, treat it as an explicit key-file
            # reference; otherwise it is an injected key value.
            candidate = Path(self.voyage_api_key).expanduser()
            if self.voyage_key_file is None and candidate.is_file():
                try:
                    value = candidate.read_text(encoding="utf-8").strip()
                except OSError as exc:
                    raise ConfigError("Voyage key file cannot be read", code="key_file_unreadable") from exc
                if not value:
                    raise ConfigError("Voyage key file is empty", code="key_file_empty")
                return value
            return self.voyage_api_key
        assert self.voyage_key_file is not None
        try:
            value = self.voyage_key_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ConfigError("Voyage key file cannot be read", code="key_file_unreadable") from exc
        if not value:
            raise ConfigError("Voyage key file is empty", code="key_file_empty")
        return value

    def redacted(self) -> dict[str, Any]:
        """Return safe diagnostics; never include secrets or DSN passwords."""
        result = asdict(self)
        for key, value in list(result.items()):
            if isinstance(value, Path):
                result[key] = "<redacted-path>"
        if result.get("voyage_api_key"):
            result["voyage_api_key"] = "<redacted>"
        if result.get("database_dsn"):
            result["database_dsn"] = redact_dsn(result["database_dsn"])
        return result


Config = Settings


_ENV_FIELDS: dict[str, tuple[str, ...]] = {
    "sessions_dir": ("SHIORI_SESSIONS_DIR",),
    "hermes_db": ("SHIORI_HERMES_DB",),
    "discord_archive_dir": ("SHIORI_DISCORD_ARCHIVE_DIR",),
    "database_dsn": ("SHIORI_DATABASE_DSN", "SHIORI_DATABASE_URL", "SHIORI_PG_DSN"),
    "pg_cred_file": ("SHIORI_PG_CRED", "SHIORI_PG_CRED_FILE"),
    "embedding_provider": ("SHIORI_EMBEDDING_PROVIDER",),
    "voyage_api_url": ("SHIORI_VOYAGE_API_URL",),
    "voyage_api_key": ("SHIORI_VOYAGE_API_KEY", "SHIORI_VOYAGE_KEY"),
    "voyage_key_file": ("SHIORI_VOYAGE_KEY_FILE",),
    "voyage_model": ("SHIORI_VOYAGE_MODEL",),
    "embed_dim": ("SHIORI_EMBED_DIM",),
    "allow_fake_embeddings": ("SHIORI_ALLOW_FAKE_EMBEDDINGS",),
    "environment": ("SHIORI_ENVIRONMENT",),
    "log_file": ("SHIORI_LOG_FILE",),
    "chunk_tokens": ("SHIORI_CHUNK_TOKENS",),
    "chunk_overlap": ("SHIORI_CHUNK_OVERLAP",),
    "voyage_batch_size": ("SHIORI_VOYAGE_BATCH_SIZE",),
    "voyage_rps_limit": ("SHIORI_VOYAGE_RPS_LIMIT",),
    "embed_timeout": ("SHIORI_EMBED_TIMEOUT",),
    "max_retries": ("SHIORI_MAX_RETRIES",),
    "sessions_lock_id": ("SHIORI_SESSIONS_LOCK_ID",),
    "discord_lock_id": ("SHIORI_DISCORD_LOCK_ID",),
}

# Legacy aliases for one migration cycle.  Each field maps to the legacy
# ``SHIYI_*`` variable that used to be canonical.  If both a canonical
# ``SHIORI_*`` variable and its legacy alias are set for the same field,
# configuration fails closed instead of guessing which one wins.
_LEGACY_ENV_FIELDS: dict[str, tuple[str, ...]] = {
    "sessions_dir": ("SHIYI_SESSIONS_DIR",),
    "hermes_db": ("SHIYI_HERMES_DB",),
    "discord_archive_dir": ("SHIYI_DISCORD_ARCHIVE_DIR",),
    "database_dsn": ("SHIYI_DATABASE_DSN", "SHIYI_DATABASE_URL", "SHIYI_PG_DSN"),
    "pg_cred_file": ("SHIYI_PG_CRED", "SHIYI_PG_CRED_FILE"),
    "embedding_provider": ("SHIYI_EMBEDDING_PROVIDER",),
    "voyage_api_url": ("SHIYI_VOYAGE_API_URL",),
    "voyage_api_key": ("SHIYI_VOYAGE_API_KEY", "SHIYI_VOYAGE_KEY"),
    "voyage_key_file": ("SHIYI_VOYAGE_KEY_FILE",),
    "voyage_model": ("SHIYI_VOYAGE_MODEL",),
    "embed_dim": ("SHIYI_EMBED_DIM",),
    "allow_fake_embeddings": ("SHIYI_ALLOW_FAKE_EMBEDDINGS",),
    "environment": ("SHIYI_ENVIRONMENT",),
    "log_file": ("SHIYI_LOG_FILE",),
    "chunk_tokens": ("SHIYI_CHUNK_TOKENS",),
    "chunk_overlap": ("SHIYI_CHUNK_OVERLAP",),
    "voyage_batch_size": ("SHIYI_VOYAGE_BATCH_SIZE",),
    "voyage_rps_limit": ("SHIYI_VOYAGE_RPS_LIMIT",),
    "embed_timeout": ("SHIYI_EMBED_TIMEOUT",),
    "max_retries": ("SHIYI_MAX_RETRIES",),
    "sessions_lock_id": ("SHIYI_SESSIONS_LOCK_ID",),
    "discord_lock_id": ("SHIYI_DISCORD_LOCK_ID",),
}


_PATH_FIELDS = {"sessions_dir", "hermes_db", "discord_archive_dir", "pg_cred_file", "voyage_key_file", "log_file"}
_INT_FIELDS = {
    "embed_dim",
    "chunk_tokens",
    "chunk_overlap",
    "voyage_batch_size",
    "voyage_rps_limit",
    "embed_timeout",
    "max_retries",
    "sessions_lock_id",
    "discord_lock_id",
}

_BOOL_FIELDS = {"allow_fake_embeddings"}


def _normalise_value(name: str, value: Any) -> Any:
    if name in _PATH_FIELDS:
        return _path(value)
    if name in _INT_FIELDS:
        if value is None or value == "":
            return None
        return _positive_int(value, name)
    if name in _BOOL_FIELDS:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"1", "true", "yes", "on"}:
                return True
            if lowered in {"0", "false", "no", "off"}:
                return False
        raise ConfigError(f"{name} must be a boolean", code="invalid_config_value")
    if name in {"database_dsn", "embedding_provider", "voyage_api_url", "voyage_api_key", "voyage_model", "environment"}:
        return _text(value)
    return value


def _legacy_values() -> dict[str, Any]:
    root = Path("~/.openclaw").expanduser()
    return {
        "sessions_dir": root / "agents/main/sessions",
        "hermes_db": Path("~/.hermes/state.db").expanduser(),
        "discord_archive_dir": root / "workspace/data/discord-archive",
        "pg_cred_file": root / "credentials/session-memory-pg.txt",
        "voyage_key_file": root / "credentials/voyage-api-key.txt",
        "embedding_provider": "voyage",
        "voyage_api_url": "https://api.voyageai.com/v1/embeddings",
        "voyage_model": "voyage-4-large",
        "embed_dim": 1024,
    }


def load_config(
    environ: Mapping[str, str] | None = None,
    *,
    config_path: str | os.PathLike[str] | None = None,
    legacy_openclaw: bool = False,
    **overrides: Any,
) -> Settings:
    """Load settings with explicit values taking precedence over env/file."""
    env = dict(os.environ if environ is None else environ)
    canonical_config_file = env.get("SHIORI_CONFIG_FILE", "")
    legacy_config_file = env.get("SHIYI_CONFIG_FILE", "")
    if config_path is None:
        if canonical_config_file and legacy_config_file:
            raise ConfigError(
                "both SHIORI_CONFIG_FILE and SHIYI_CONFIG_FILE are set",
                code="config_file_conflict",
            )
        selected_path = canonical_config_file or legacy_config_file
    else:
        selected_path = config_path
    values: dict[str, Any] = {}
    if selected_path:
        values.update(_read_config_file(Path(selected_path).expanduser()))

    for field_name, env_names in _ENV_FIELDS.items():
        for env_name in env_names:
            if env_name in env and env[env_name] != "":
                values[field_name] = env[env_name]
                break

    # Legacy ``SHIYI_*`` variables are accepted as compatible inputs for one
    # migration cycle.  A field set through both a canonical ``SHIORI_*`` name
    # and its legacy alias fails closed; the two are never merged or guessed.
    for field_name, legacy_names in _LEGACY_ENV_FIELDS.items():
        if field_name in values:
            for legacy_name in legacy_names:
                if legacy_name in env and env[legacy_name] != "":
                    raise ConfigError(
                        f"both {_ENV_FIELDS[field_name][0]} and {legacy_name} are set",
                        code="env_alias_conflict",
                    )
            continue
        for legacy_name in legacy_names:
            if legacy_name in env and env[legacy_name] != "":
                values[field_name] = env[legacy_name]
                break

    # Explicit keyword values are the highest-priority layer.  This also lets
    # tests inject values without mutating process-global environment state.
    values.update({key: value for key, value in overrides.items() if value is not None})
    if legacy_openclaw:
        for key, value in _legacy_values().items():
            values.setdefault(key, value)

    known = {field.name for field in fields(Settings)}
    unknown = sorted(set(values) - known)
    if unknown:
        raise ConfigError("unknown configuration keys: " + ", ".join(unknown), code="unknown_config_key")

    normalised = {key: _normalise_value(key, value) for key, value in values.items()}
    normalised["legacy_openclaw"] = legacy_openclaw
    return Settings(**normalised)


def redact_dsn(dsn: str) -> str:
    """Redact passwords in URL and libpq key/value DSNs."""
    if not isinstance(dsn, str):
        return "<redacted>"
    if "://" in dsn:
        parts = urlsplit(dsn)
        if parts.username is not None:
            host = parts.hostname or ""
            if parts.port is not None:
                host += f":{parts.port}"
            userinfo = parts.username
            if parts.password is not None:
                userinfo += ":<redacted>"
            netloc = f"{userinfo}@{host}"
            return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    return re.sub(r"(\bpassword\s*=\s*)(?:'[^']*'|\"[^\"]*\"|[^\s]+)", r"\1<redacted>", dsn, flags=re.I)


def credentials_from_settings(settings: Settings) -> dict[str, str]:
    """Return one explicit, validated psycopg2 connection shape.

    A ``SHIORI_PG_CRED`` file may use either a complete ``dsn=...`` entry or
    the documented ``host/port/dbname/user/password`` shape. The latter is
    returned as keyword arguments so libpq performs its own safe parameter
    handling; callers must not assume every result has a ``dsn`` key.
    """
    if settings.database_dsn:
        return {"dsn": settings.database_dsn}
    if settings.pg_cred_file is None:
        raise ConfigError(
            "database is not configured; set SHIORI_DATABASE_DSN or SHIORI_PG_CRED",
            code="database_not_configured",
        )
    return _validated_pg_credentials(_key_value_file(settings.pg_cred_file))


def connect_database(settings: Settings) -> Any:
    """Open the configured database without exposing DSNs or credentials."""
    import psycopg2

    try:
        # psycopg2's bundled stubs model only the positional DSN overload even
        # though the runtime accepts the documented libpq keyword parameters.
        # Keep the validated shape explicit at our boundary and let the driver
        # perform the actual parameter handling.
        connect_fn: Any = psycopg2.connect
        return connect_fn(**credentials_from_settings(settings))
    except ConfigError:
        raise
    except Exception as exc:  # noqa: BLE001 - normalize driver errors safely
        raise ConfigError("database connection failed", code="database_connection_failed") from exc


def config_summary(settings: Settings) -> str:
    """Safe one-line diagnostic suitable for CLI output."""
    return json.dumps(settings.redacted(), sort_keys=True, default=str)
