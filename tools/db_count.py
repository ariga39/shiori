#!/usr/bin/env python3
"""Run a read-only SQL count against the isolated E2E database.

Used by tools/e2e_replay_smoke.sh so the harness does not depend on a host
psql binary; psycopg2 is always present in the installed wheel venv.
"""

from __future__ import annotations

import argparse

import psycopg2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--sql", required=True)
    args = parser.parse_args()
    conn = psycopg2.connect(args.dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(args.sql)
            row = cur.fetchone()
            print(row[0] if row is not None else "")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
