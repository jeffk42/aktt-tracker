"""migrate.py - Apply schema additions to an existing database safely.

SQLite's ALTER TABLE doesn't support "ADD COLUMN IF NOT EXISTS", so this
script checks the existing schema and only applies ADDs for columns that
aren't already present. Safe to re-run any number of times.

Usage:
    python migrate.py --db guildstats.db
    python migrate.py --db guildstats.db --dry-run
"""
from __future__ import annotations
import argparse
import sqlite3
import sys


# Each migration: (table, column, type+constraints).
# Add new entries here when the schema grows; never edit/delete existing ones.
COLUMN_MIGRATIONS = [
    # phase 2.5b: distinguish CLI vs sheet-imported donations
    ("manual_donations", "source",         "TEXT NOT NULL DEFAULT 'cli'"),
    ("manual_donations", "sheet_row_hash", "TEXT"),
]

# Indexes to ensure-exist. CREATE INDEX IF NOT EXISTS handles re-runs natively.
INDEX_MIGRATIONS = [
    ("idx_md_source_week",
     "CREATE INDEX IF NOT EXISTS idx_md_source_week ON manual_donations(source, week_id)"),
]


def column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == column for r in rows)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True)
    ap.add_argument("--dry-run", action="store_true",
                    help="Print what would be done without applying changes")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.execute("PRAGMA foreign_keys = ON")

    print(f"Inspecting {args.db}...")
    pending_cols = []
    for table, column, definition in COLUMN_MIGRATIONS:
        if column_exists(conn, table, column):
            print(f"  [ok]   {table}.{column} already present")
        else:
            pending_cols.append((table, column, definition))
            print(f"  [todo] {table}.{column} missing")

    if not pending_cols and all(  # quick check on indexes too
        # We can't easily check existence cheaply, so just trust IF NOT EXISTS
        True for _ in INDEX_MIGRATIONS
    ):
        print(f"\n{len(pending_cols)} column(s) to add, "
              f"{len(INDEX_MIGRATIONS)} index(es) to ensure")

    if args.dry_run:
        print("\n--dry-run: no changes applied.")
        return

    if pending_cols:
        for table, column, definition in pending_cols:
            sql = f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
            print(f"Applying: {sql}")
            conn.execute(sql)

    for name, sql in INDEX_MIGRATIONS:
        print(f"Ensuring index: {name}")
        conn.execute(sql)

    conn.commit()
    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
