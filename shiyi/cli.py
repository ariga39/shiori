"""The installed ``shiyi`` command-line entry point."""

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
        help="Explicit migration mode: use legacy OpenClaw paths when SHIYI_* is unset",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="shiyi", description="Searchable long-term memory for AI agents")
    _config_args(parser)
    sub = parser.add_subparsers(dest="command", required=True)

    ingest = sub.add_parser("ingest", help="ingest one explicitly configured source")
    _config_args(ingest, suppress_default=True)
    ingest.add_argument("--source", choices=("sessions", "hermes", "discord"), required=True)
    ingest.add_argument("--dry-run", action="store_true", help="preview without database or embedding writes")
    ingest.add_argument("--force", action="store_true", help="reprocess unchanged records")
    ingest.add_argument("--file", help="Discord JSONL file (explicit file mode)")
    ingest.add_argument("--session", help="Hermes session id")

    query = sub.add_parser("query", help="search indexed memory")
    _config_args(query, suppress_default=True)
    query.add_argument("query", help="search query")
    query.add_argument("--limit", "-n", type=int, default=5)

    serve = sub.add_parser("serve", help="run the read-only MCP server")
    _config_args(serve, suppress_default=True)
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


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        settings = _load(args)
        if args.command == "ingest":
            return _run_ingest(args, settings)
        if args.command == "query":
            return _run_query(args, settings)
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
