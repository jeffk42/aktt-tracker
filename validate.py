"""validate.py - compare ingested DB rows for the current trade week against
donation_summary.csv produced by the legacy guild_stats.py.

Reports any per-user mismatches across sales, taxes, deposits, raffle,
donations, purchases, and rank.

Usage:
    python validate.py --db guildstats.db \\
                       --csv /path/to/donation_summary.csv \\
                       --week this   # or 'last'
"""
from __future__ import annotations
import argparse
import csv
from datetime import datetime, timezone, timedelta
from pathlib import Path

from guildstats import open_db, trade_week_ending


CSV_FIELDS = ["username", "rank", "sales", "taxes", "deposits", "raffle", "donations", "purchases"]


def load_csv(path: str | Path):
    """Yield dicts. donation_summary.csv has an optional first line that is just
    a timestamp, then the data rows with no header."""
    with open(path, "r", encoding="utf-8") as f:
        first = f.readline().strip()
        # If the first line looks like the data prefix-date (no comma in slot 0,
        # or contains '/' or ':'), skip it.
        if first and "," not in first and (("/" in first) or (":" in first)):
            pass  # skip
        else:
            # rewind: process as data row
            yield _row_to_dict(first.split(","))
        for line in f:
            line = line.strip()
            if not line:
                continue
            yield _row_to_dict(line.split(","))


def _row_to_dict(parts):
    d = dict(zip(CSV_FIELDS, parts))
    return d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--csv", required=True)
    ap.add_argument("--week", default="this", choices=("this", "last"))
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    target = trade_week_ending(now)
    if args.week == "last":
        target = target - timedelta(days=7)
    print(f"Comparing CSV against trade week ending {target.date().isoformat()}")

    conn = open_db(args.db)
    week_row = conn.execute(
        "SELECT id FROM weeks WHERE ending_date = ?",
        (target.date().isoformat(),),
    ).fetchone()
    if not week_row:
        raise SystemExit(f"No week row in DB for ending_date={target.date()}")
    week_id = week_row["id"]

    # Build dict of DB rows for that week
    db_rows = {}
    for r in conn.execute(
        """
        SELECT u.account_name, s.rank, s.sales, s.taxes, s.purchases,
               s.total_deposits, s.total_raffle, s.total_donations
          FROM user_week_stats s
          JOIN users u ON u.id = s.user_id
         WHERE s.week_id = ?
        """, (week_id,),
    ):
        db_rows[r["account_name"]] = r

    csv_rows = {row["username"]: row for row in load_csv(args.csv) if row.get("username")}

    only_in_csv = set(csv_rows) - set(db_rows)
    only_in_db = set(db_rows) - set(csv_rows)
    if only_in_csv:
        print(f"  WARN: {len(only_in_csv)} users in CSV but not DB (sample): {sorted(only_in_csv)[:5]}")
    if only_in_db:
        print(f"  WARN: {len(only_in_db)} users in DB but not CSV (sample): {sorted(only_in_db)[:5]}")

    common = sorted(set(csv_rows) & set(db_rows))
    print(f"  {len(common)} users present in both")

    field_pairs = [
        ("rank",      "rank"),
        ("sales",     "sales"),
        ("taxes",     "taxes"),
        ("purchases", "purchases"),
        ("deposits",  "total_deposits"),
        ("raffle",    "total_raffle"),
        ("donations", "total_donations"),
    ]
    mismatches_by_field = {f: 0 for f, _ in field_pairs}
    sample_mismatches = []

    for user in common:
        c = csv_rows[user]
        d = db_rows[user]
        for csv_f, db_f in field_pairs:
            try:
                cv = int(float(c.get(csv_f, "0") or "0"))
            except ValueError:
                cv = 0
            dv = int(d[db_f] or 0)
            if cv != dv:
                mismatches_by_field[csv_f] += 1
                if len(sample_mismatches) < 15:
                    sample_mismatches.append((user, csv_f, cv, dv))

    print("\nField mismatch counts:")
    for f, _ in field_pairs:
        print(f"  {f:<10} {mismatches_by_field[f]}")

    if sample_mismatches:
        print("\nSample mismatches (user, field, csv, db):")
        for s in sample_mismatches:
            print(f"  {s[0]:<25} {s[1]:<10} csv={s[2]:<12} db={s[3]}")
    else:
        print("\nAll fields matched for users present in both. ✓")


if __name__ == "__main__":
    main()
