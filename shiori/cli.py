"""The installed ``shiori`` command-line entry point."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from .config import ConfigError, Settings, load_config


def _config_args(parser: argparse.ArgumentParser, *, suppress_default: bool = False) -> None:
    default = argparse.SUPPRESS if suppress_default else None
    parser.add_argument("--config", default=default, help="JSON/TOML config file")
    parser.add_argument(
        "--legacy-openclaw",
        action="store_true",
        default=argparse.SUPPRESS if suppress_default else False,
        help="Explicit migration mode: use legacy OpenClaw paths when SHIORI_* is unset",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="shiori", description="Searchable long-term memory for AI agents")
    _config_args(parser)
    sub = parser.add_subparsers(dest="command", required=True)

    ingest = sub.add_parser("ingest", help="ingest one explicitly configured source")
    _config_args(ingest, suppress_default=True)
    ingest.add_argument("--source", choices=("sessions", "hermes", "discord"), required=True)
    ingest.add_argument("--dry-run", action="store_true", help="preview without database or embedding writes")
    ingest.add_argument("--force", action="store_true", help="reprocess unchanged records")
    ingest.add_argument("--file", help="Discord JSONL file (explicit file mode)")
    ingest.add_argument("--session", help="Hermes session id")
    ingest.add_argument(
        "--redact",
        action="store_true",
        default=True,
        help="redact recognized PII at extraction (forced on; fail-closed)",
    )

    query = sub.add_parser("query", help="search indexed memory")
    _config_args(query, suppress_default=True)
    query.add_argument("query", help="search query")
    query.add_argument("--limit", "-n", type=int, default=5)
    query.add_argument("--source-type", action="append", default=[], help="Filter by exact source_type (repeatable)")
    query.add_argument("--session-id", action="append", default=[], help="Filter by exact session_id (repeatable)")
    query.add_argument("--time-from", default=None, help="UTC RFC3339 lower bound (inclusive on timestamp_start)")
    query.add_argument("--time-to", default=None, help="UTC RFC3339 upper bound (exclusive on timestamp_start)")
    query.add_argument("--explain", action="store_true", help="Print per-result retrieval explain line")

    serve = sub.add_parser("serve", help="run the read-only MCP server")
    _config_args(serve, suppress_default=True)

    db = sub.add_parser("db", help="database schema/repository operations")
    _config_args(db, suppress_default=True)
    db_sub = db.add_subparsers(dest="db_command", required=True)
    db_sub.add_parser("migrate", help="apply forward-only migrations")
    db_sub.add_parser("health", help="repository health/version check")
    backup = db_sub.add_parser("backup", help="backup repository to a pg_dump file")
    backup.add_argument("dest", help="backup path (must not exist)")
    restore = db_sub.add_parser("restore", help="restore a backup into a NEW staging database")
    restore.add_argument("src", help="backup path")
    restore.add_argument("--target", required=True, help="new staging database name (must not exist)")

    privacy = sub.add_parser("privacy", help="privacy lifecycle operations")
    _config_args(privacy, suppress_default=True)
    privacy_sub = privacy.add_subparsers(dest="privacy_command", required=True)
    privacy_sub.add_parser("providers", help="disclose sources, data flow, and retention")
    export = privacy_sub.add_parser("export", help="export managed data in a scope")
    export.add_argument("--scope", required=True)
    export.add_argument("--dest", required=True)
    export.add_argument("--yes", action="store_true", help="confirm and write the export")
    delete = privacy_sub.add_parser("delete", help="delete managed data in a scope")
    delete.add_argument("--scope", required=True)
    delete.add_argument("--older-than", type=int, help="only delete managed rows older than N days")
    delete.add_argument("--yes", action="store_true", help="confirm and delete")
    retention_check_parser = privacy_sub.add_parser("retention-check", help="report managed-data age and expiry")
    retention_check_parser.add_argument("--scope", required=True)
    return parser


def _load(args: argparse.Namespace) -> Settings:
    return load_config(config_path=args.config, legacy_openclaw=args.legacy_openclaw)


def _require_runtime(settings: Settings, *, source: str | None = None, dry_run: bool = False) -> None:
    if source is not None:
        settings.require_source(source)
    if not dry_run:
        settings.require_database()
        settings.require_embedding()


def _module_args(args: argparse.Namespace, *, include_file: bool = False, include_session: bool = False) -> list[str]:
    result: list[str] = []
    if args.dry_run:
        result.append("--dry-run")
    if args.force:
        result.append("--force")
    if include_file and args.file:
        result.extend(["--file", args.file])
    if include_session and args.session:
        result.extend(["--session", args.session])
    if args.config:
        result.extend(["--config", args.config])
    if args.legacy_openclaw:
        result.append("--legacy-openclaw")
    return result


def _run_ingest(args: argparse.Namespace, settings: Settings) -> int:
    # An explicit Discord file is itself the source boundary; directory
    # discovery must not be required in that mode.
    source = None if args.source == "discord" and args.file else args.source
    _require_runtime(settings, source=source, dry_run=args.dry_run)
    if args.source == "sessions":
        import ingest

        ingest.apply_settings(settings)
        ingest.main(_module_args(args))
    elif args.source == "hermes":
        import ingest_hermes

        ingest_hermes.main(_module_args(args, include_session=True))
    else:
        import ingest_discord

        ingest_discord.main(_module_args(args, include_file=True))
    return 0


def _run_query(args: argparse.Namespace, settings: Settings) -> int:
    _require_runtime(settings)
    import query

    query.apply_settings(settings)
    query_args = [args.query, "--limit", str(args.limit)]
    if args.config:
        query_args.extend(["--config", args.config])
    if args.legacy_openclaw:
        query_args.append("--legacy-openclaw")
    for source in getattr(args, "source_type", []):
        query_args.extend(["--source-type", source])
    for session in getattr(args, "session_id", []):
        query_args.extend(["--session-id", session])
    if getattr(args, "time_from", None):
        query_args.extend(["--time-from", args.time_from])
    if getattr(args, "time_to", None):
        query_args.extend(["--time-to", args.time_to])
    if getattr(args, "explain", False):
        query_args.append("--explain")
    query.main(query_args)
    return 0


def _run_serve(args: argparse.Namespace, settings: Settings) -> int:
    _require_runtime(settings)
    import asyncio

    import mcp_server
    import query

    query.apply_settings(settings)
    asyncio.run(mcp_server.run_server(settings))
    return 0


def _run_db(args: argparse.Namespace, settings: Settings) -> int:
    # DB schema/repository operations need the database, never embeddings.
    settings.require_database()
    import json as _json
    from pathlib import Path

    from .config import connect_database
    from .migrations import MigrationError, migrate, schema_version
    from .repository import backup, repository_health, restore

    conn = connect_database(settings)
    try:
        if args.db_command == "health":
            from pathlib import Path as _Path

            migrations_dir = _Path(__file__).resolve().parent / "schema_migrations"
            health = repository_health(conn, migrations_dir=migrations_dir)
            print(_json.dumps(health, sort_keys=True, default=str))
            return 0 if health["ok"] else 2
        if args.db_command == "migrate":
            from pathlib import Path as _Path

            migrations_dir = _Path(__file__).resolve().parent / "schema_migrations"
            applied = migrate(conn, migrations_dir=migrations_dir)
            print(_json.dumps({"applied": applied, "version": schema_version(conn)}, sort_keys=True))
            return 0
        if args.db_command == "backup":
            from pathlib import Path as _Path

            migrations_dir = _Path(__file__).resolve().parent / "schema_migrations"
            result = backup(conn, Path(args.dest), migrations_dir=migrations_dir)
            print(_json.dumps({"ok": result["ok"], "path": result["path"],
                               "manifest_path": result["manifest_path"],
                               "schema_head": result["schema_head"], "digest": result["digest"]},
                              sort_keys=True))
            return 0
        if args.db_command == "restore":
            from pathlib import Path as _Path

            migrations_dir = _Path(__file__).resolve().parent / "schema_migrations"
            result = restore(conn, Path(args.src), target_name=args.target,
                             migrations_dir=migrations_dir)
            print(_json.dumps({"ok": result["ok"], "staging_dsn": result["staging_dsn"],
                               "marker": result["marker"], "schema_head": result["schema_head"]},
                              sort_keys=True))
            return 0
        print("error[unknown_db_command]", file=sys.stderr)
        return 2
    except MigrationError as exc:
        print(f"error[{exc.code}]: {exc.message}", file=sys.stderr)
        return 2
    finally:
        conn.close()


def _run_privacy(args: argparse.Namespace, settings: Settings) -> int:
    import json as _json

    from .privacy import PrivacyError, delete_scope, export_scope, providers, retention_check

    if args.privacy_command == "providers":
        print(_json.dumps(providers(settings), sort_keys=True, ensure_ascii=False))
        return 0
    # export/delete/retention-check operate on the managed store, so they need a DB.
    from .config import connect_database

    conn = connect_database(settings)
    try:
        if args.privacy_command == "export":
            result = export_scope(
                conn, args.scope, args.dest, settings=settings, confirm=args.yes
            )
            print(_json.dumps(result, sort_keys=True, ensure_ascii=False))
            return 0
        if args.privacy_command == "retention-check":
            result = retention_check(conn, args.scope, settings=settings)
            print(_json.dumps(result, sort_keys=True, ensure_ascii=False))
            return 0
        result = delete_scope(
            conn, args.scope,
            settings=settings,
            confirm=args.yes,
            older_than_days=args.older_than,
        )
        print(_json.dumps(result, sort_keys=True, ensure_ascii=False))
        return 0
    except PrivacyError as exc:
        print(f"error[{exc.code}]: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        settings = _load(args)
        if args.command == "ingest":
            return _run_ingest(args, settings)
        if args.command == "query":
            return _run_query(args, settings)
        if args.command == "db":
            return _run_db(args, settings)
        if args.command == "privacy":
            return _run_privacy(args, settings)
        return _run_serve(args, settings)
    except ConfigError as exc:
        print(f"error[{exc.code}]: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"error[file_not_found]: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - keep runtime failures structured and secret-safe
        print(f"error[runtime_error]: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
