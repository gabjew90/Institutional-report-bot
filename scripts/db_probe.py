#!/usr/bin/env python3
"""Read the database without being able to change it or to block the worker.

    python scripts/db_probe.py "SELECT status, COUNT(*) FROM pdf_files GROUP BY 1"
    python scripts/db_probe.py --tables
    python scripts/db_probe.py --schema pdf_files
    echo "SELECT ..." | python scripts/db_probe.py -          # SQL on standard input

On the worker (the SQL on standard input has no quoting to fight and no
length limit, where a `railway ssh` command line is cut near 8 KB):

    railway ssh "/opt/venv/bin/python scripts/db_probe.py --tables"
    echo "SELECT ..." | railway ssh "/opt/venv/bin/python scripts/db_probe.py -"

Why this exists (2026-11-23, audit fix 2): `import db` from a second process
ran the schema pass against the live database and stalled the worker for 30
minutes on 2026-09-04, and again on 09-21. db.get_connection() now refuses a
second process on a database the worker owns. This script is the way to read:
it never imports `db` or `config`, opens the file with mode=ro and
query_only, refuses ATTACH, and interrupts a statement that runs past
--timeout so a slow read cannot hold the write-ahead log open.

Standard library only, so it runs in a `railway ssh` shell, which cannot load
numpy. Output is tab-separated with a header line, or one JSON object per row
with --json.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

DEFAULT_DB = "/data/reports.db"
MAX_CELL = 2000


def connect(db_path: str, timeout_s: float = 20.0) -> sqlite3.Connection:
    path = Path(db_path)
    if not path.is_file():
        raise SystemExit(f"db_probe: no database at {db_path}")
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")

    def authorize(action, *_):
        if action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    conn.set_authorizer(authorize)

    deadline = time.monotonic() + timeout_s
    conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 20000)
    return conn


def run(conn: sqlite3.Connection, sql: str, max_rows: int) -> tuple[list[str], list[tuple], bool]:
    """(column names, rows, whether more rows were left unread)."""
    cur = conn.execute(sql)
    cols = [d[0] for d in (cur.description or [])]
    rows = cur.fetchmany(max_rows + 1)
    more = len(rows) > max_rows
    return cols, [tuple(r) for r in rows[:max_rows]], more


def _cell(v, full: bool) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bytes):
        return f"<{len(v)} bytes>"
    s = str(v).replace("\t", " ").replace("\r", " ").replace("\n", "\\n")
    return s if full or len(s) <= MAX_CELL else s[:MAX_CELL] + f"... (+{len(s) - MAX_CELL} chars)"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Read-only SQL against the bot's database.")
    ap.add_argument("sql", nargs="?", help='a SELECT, or "-" to read the SQL from standard input')
    ap.add_argument("--db", default=os.environ.get("DB_PATH") or DEFAULT_DB)
    ap.add_argument("--tables", action="store_true", help="list tables with their row counts")
    ap.add_argument("--schema", metavar="TABLE", help="print one table's columns and indexes")
    ap.add_argument("--json", action="store_true", help="one JSON object per row")
    ap.add_argument("--max-rows", type=int, default=200)
    ap.add_argument("--full", action="store_true", help="do not shorten long cells")
    ap.add_argument("--timeout", type=float, default=20.0, help="seconds before a statement is interrupted")
    a = ap.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    conn = connect(a.db, a.timeout)
    try:
        return _dispatch(conn, a, ap)
    except sqlite3.OperationalError as e:
        text = str(e)
        if "interrupted" in text:
            text = f"stopped after {a.timeout:.0f} s (--timeout). Narrow the query."
        print(f"db_probe: {text}", file=sys.stderr)
        return 1
    except (sqlite3.Error, sqlite3.Warning) as e:
        print(f"db_probe: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()


def _dispatch(conn: sqlite3.Connection, a, ap) -> int:
    if a.tables:
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY 1")]
        for n in names:
            count = conn.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0]
            print(f"{n}\t{count}")
        return 0
    if a.schema:
        cols = conn.execute("SELECT name, type, \"notnull\", pk FROM pragma_table_info(?)", (a.schema,)).fetchall()
        if not cols:
            print(f"db_probe: no table named {a.schema}", file=sys.stderr)
            return 1
        for c in cols:
            flags = " ".join(x for x in ("PRIMARY KEY" if c[3] else "", "NOT NULL" if c[2] else "") if x)
            print(f"{c[0]}\t{c[1]}\t{flags}".rstrip())
        for i in conn.execute("SELECT name FROM pragma_index_list(?)", (a.schema,)):
            on = ", ".join(r[0] or "<expr>" for r in conn.execute(
                "SELECT name FROM pragma_index_info(?)", (i[0],)))
            print(f"index {i[0]} ({on})")
        return 0
    sql = sys.stdin.read() if a.sql in (None, "-") else a.sql
    if not sql.strip():
        ap.error('give a SELECT, "-" with SQL on standard input, --tables or --schema TABLE')
    cols, rows, more = run(conn, sql, a.max_rows)
    if a.json:
        for r in rows:
            print(json.dumps({c: (f"<{len(v)} bytes>" if isinstance(v, bytes) else v)
                              for c, v in zip(cols, r)}, ensure_ascii=False))
    else:
        if cols:
            print("\t".join(cols))
        for r in rows:
            print("\t".join(_cell(v, a.full) for v in r))
    if more:
        print(f"db_probe: more than {a.max_rows} rows, the rest were not read (--max-rows)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
