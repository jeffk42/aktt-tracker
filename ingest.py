"""ingest.py - Live ingest of MasterMerchant.lua + GBLData.lua into SQLite.

Mirrors the semantics of the legacy guild_stats.py:
  * --week this   parses the EXPORT block as the CURRENT trade week
  * --week last   parses the EXPORT block as the JUST-ENDED trade week
  * GBL transactions are upserted by transaction_id (idempotent)
  * After ingest, total_deposits/total_raffle/total_donations on user_week_stats
    are recomputed from bank_transactions for the affected week.

Usage:
    python ingest.py --db guildstats.db \\
                     --mm  /path/to/MasterMerchant.lua \\
                     --gbl /path/to/GBLData.lua \\
                     --week this
"""
from __future__ import annotations
import argparse
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

from slpp import slpp as lua

from guildstats import (
    open_db, apply_schema,
    upsert_user, upsert_week, upsert_user_week_stats, upsert_bank_transaction,
    recompute_week_totals, ingest_run, trade_week_ending, WeekStats, BankTxn,
    compute_paid_free_hr, ensure_open_raffle, insert_raffle_entry,
    recompute_raffle_totals, raffle_drawing_date_for, RaffleEntry,
)

# --- configuration matching the existing guild_stats.py ---------------------
GUILD_NAME = "AK Tamriel Trade"
USER = "@jeffk42"
EXCLUDE_USERS = {"@aktt.guild"}

GBL_FIELD_INDEX = {
    "timestamp": 0, "username": 1, "transactionType": 2, "goldAmount": 3,
    "itemCount": 4, "itemDescription": 5, "itemLink": 6, "itemValue": 7,
    "transactionId": 8,
}


# --- helpers ----------------------------------------------------------------

def _to_int(v) -> int | None:
    """Accept '123', '123.0', 'nil', None -> int or None."""
    if v is None or v == "nil":
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def determine_target_week(week_param: str, now: datetime | None = None) -> datetime:
    """Return the trade-week-ending datetime that the EXPORT block represents
    when MM was exported with `week_param` ('this' or 'last') at time `now`."""
    now = now or datetime.now(timezone.utc)
    current_week_end = trade_week_ending(now)
    if week_param == "this":
        return current_week_end
    elif week_param == "last":
        return current_week_end - timedelta(days=7)
    raise ValueError(f"--week must be 'this' or 'last', got {week_param!r}")


def load_lua(path: str | Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    return lua.decode("{" + text + "}")


# --- MM EXPORT parsing ------------------------------------------------------

def parse_mm_export(mm_path: str | Path):
    """Yield (account_name, sales, taxes, purchases, rank) tuples from the
    EXPORT block, mirroring guild_stats.py's parse logic."""
    data = load_lua(mm_path)
    try:
        export = data["ShopkeeperSavedVars"]["Default"][USER]["$AccountWide"]["EXPORT"][GUILD_NAME]
    except KeyError as e:
        raise SystemExit(f"MM EXPORT path not found in {mm_path}: missing key {e}")

    # Skip "version" or any other non-integer-keyed entries
    for key, raw in export.items():
        if not isinstance(key, int):
            continue
        parts = raw.split("&")
        # Expected formats:
        #   username & sales & purchases & taxes & rank   (5 fields, modern)
        #   username & sales & purchases & rank           (4 fields, legacy)
        if len(parts) == 5:
            account, sales, purchases, taxes, rank = parts
            yield account, _to_int(sales) or 0, _to_int(taxes) or 0, _to_int(purchases) or 0, _to_int(rank)
        elif len(parts) == 4:
            account, sales, purchases, rank = parts
            yield account, _to_int(sales) or 0, 0, _to_int(purchases) or 0, _to_int(rank)
        else:
            print(f"  warn: skipping malformed EXPORT row [{key}]: {parts!r}", file=sys.stderr)


# --- GBL parsing ------------------------------------------------------------

def parse_gbl_history(gbl_path: str | Path):
    """Yield raw transaction dicts from the GBL history block."""
    data = load_lua(gbl_path)
    try:
        history = data["GBLDataSavedVariables"]["Default"][USER]["$AccountWide"]["history"][GUILD_NAME]
    except KeyError as e:
        raise SystemExit(f"GBL history path not found in {gbl_path}: missing key {e}")

    for key, raw in history.items():
        if not isinstance(key, int):
            continue
        # GBL serializes \t literally as the two characters '\' and 't'
        parts = raw.split("\\t")
        if len(parts) < 9:
            print(f"  warn: skipping malformed GBL row [{key}]: {parts!r}", file=sys.stderr)
            continue
        yield {
            "timestamp":       _to_int(parts[GBL_FIELD_INDEX["timestamp"]]),
            "username":        parts[GBL_FIELD_INDEX["username"]],
            "transactionType": parts[GBL_FIELD_INDEX["transactionType"]],
            "goldAmount":      _to_int(parts[GBL_FIELD_INDEX["goldAmount"]]),
            "itemCount":       _to_int(parts[GBL_FIELD_INDEX["itemCount"]]),
            "itemDescription": parts[GBL_FIELD_INDEX["itemDescription"]] if parts[GBL_FIELD_INDEX["itemDescription"]] != "nil" else None,
            "itemLink":        parts[GBL_FIELD_INDEX["itemLink"]] if parts[GBL_FIELD_INDEX["itemLink"]] != "nil" else None,
            "itemValue":       _to_int(parts[GBL_FIELD_INDEX["itemValue"]]),
            "transactionId":   parts[GBL_FIELD_INDEX["transactionId"]],
        }


# --- main ingest ------------------------------------------------------------

def run(db_path: str, mm_path: str, gbl_path: str, week_param: str, schema_path: str | None) -> None:
    target_week_end = determine_target_week(week_param)
    print(f"Target trade week ending: {target_week_end.isoformat()}")

    conn = open_db(db_path)
    if schema_path:
        apply_schema(conn, schema_path)

    with ingest_run(conn, "live_ingest",
                    week_param=week_param,
                    mm_filename=str(mm_path), gbl_filename=str(gbl_path)) as counts:

        conn.execute("BEGIN")
        try:
            week_id = upsert_week(conn, target_week_end)

            # 1. MM EXPORT -> user_week_stats (sales/taxes/purchases/rank)
            mm_count = 0
            for account, sales, taxes, purchases, rank in parse_mm_export(mm_path):
                if account in EXCLUDE_USERS:
                    counts["rows_skipped"] += 1
                    continue
                user_id = upsert_user(conn, account, excluded=False)
                upsert_user_week_stats(conn, WeekStats(
                    user_id=user_id, week_id=week_id, rank=rank,
                    sales=sales, taxes=taxes, purchases=purchases,
                    is_backfilled=False,
                ))
                mm_count += 1
            print(f"MM: {mm_count} user-week rows upserted")

            # 2. GBL history -> bank_transactions (all rows; UPSERT by txid)
            gbl_inserted = gbl_skipped = 0
            affected_weeks: set[int] = {week_id}
            for txn in parse_gbl_history(gbl_path):
                if txn["username"] in EXCLUDE_USERS:
                    gbl_skipped += 1
                    continue
                if txn["timestamp"] is None or not txn["transactionId"]:
                    gbl_skipped += 1
                    continue
                occurred = datetime.fromtimestamp(txn["timestamp"], tz=timezone.utc)
                txn_week_end = trade_week_ending(occurred)
                txn_week_id = upsert_week(conn, txn_week_end)
                affected_weeks.add(txn_week_id)
                user_id = upsert_user(conn, txn["username"], excluded=False)
                result = upsert_bank_transaction(conn, BankTxn(
                    transaction_id=txn["transactionId"],
                    user_id=user_id,
                    week_id=txn_week_id,
                    transaction_type=txn["transactionType"],
                    gold_amount=txn["goldAmount"],
                    item_count=txn["itemCount"],
                    item_description=txn["itemDescription"],
                    item_link=txn["itemLink"],
                    item_value=txn["itemValue"],
                    occurred_at=occurred,
                    is_backfilled=False,
                ))
                if result == "inserted":
                    gbl_inserted += 1
                else:
                    gbl_skipped += 1
            print(f"GBL: {gbl_inserted} new transactions, {gbl_skipped} skipped (already-present or excluded)")

            # 2b. Raffle entries: every raffle-eligible bank_transaction in the
            # affected weeks gets a raffle_entries row in the appropriate
            # standard (and HR) raffle. insert_raffle_entry dedupes by
            # (raffle_id, source_transaction_id), so this is idempotent.
            re_inserted = 0
            re_affected_raffles: set[int] = set()
            week_clause = "(" + ",".join(str(w) for w in affected_weeks) + ")"
            elig_rows = conn.execute(
                "SELECT bt.transaction_id, bt.user_id, bt.gold_amount, bt.occurred_at "
                "  FROM bank_transactions bt "
                " WHERE bt.transaction_type = 'dep_gold' "
                "   AND bt.week_id IN " + week_clause + " "
                "   AND ((bt.gold_amount - 1) % 1000) = 0 "
                "   AND bt.gold_amount > 1 "
                "   AND NOT EXISTS (SELECT 1 FROM raffle_entries re "
                "                    WHERE re.source_transaction_id = bt.transaction_id "
                "                      AND re.source = 'bank_deposit')"
            ).fetchall()
            for r in elig_rows:
                gold = r["gold_amount"]
                paid, free, hr = compute_paid_free_hr(gold)
                if paid <= 0 and hr <= 0:
                    continue
                occurred = datetime.strptime(r["occurred_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                # Standard raffle entry
                std_id = ensure_open_raffle(conn, at=occurred, raffle_type="standard")
                insert_raffle_entry(conn, RaffleEntry(
                    raffle_id=std_id, user_id=r["user_id"], source="bank_deposit",
                    occurred_at=occurred, gold_amount=gold,
                    paid_tickets=paid, free_tickets=free, high_roller_tickets=hr,
                    source_transaction_id=r["transaction_id"], is_backfilled=False,
                ))
                re_affected_raffles.add(std_id)
                # High-roller raffle entry, only if HR tickets earned
                if hr > 0:
                    hr_id = ensure_open_raffle(conn, at=occurred, raffle_type="high_roller")
                    insert_raffle_entry(conn, RaffleEntry(
                        raffle_id=hr_id, user_id=r["user_id"], source="high_roller_qualifier",
                        occurred_at=occurred, gold_amount=gold,
                        paid_tickets=hr, free_tickets=0, high_roller_tickets=hr,
                        source_transaction_id=r["transaction_id"], is_backfilled=False,
                    ))
                    re_affected_raffles.add(hr_id)
                re_inserted += 1
            for rid in re_affected_raffles:
                recompute_raffle_totals(conn, rid)
            print(f"Raffle entries: {re_inserted} new bank_deposit entries across {len(re_affected_raffles)} raffles")

            # 3. Recompute aggregate totals for every affected week.
            # This makes deposit/raffle/donation totals reflect the transactions
            # we just (re-)ingested, matching the legacy donation_summary.csv view.
            for wid in affected_weeks:
                recompute_week_totals(conn, wid)
            print(f"Recomputed week totals for {len(affected_weeks)} weeks")

            counts["rows_inserted"] = mm_count + gbl_inserted
            counts["rows_skipped"] = gbl_skipped
            counts["notes"] = (f"target_week={target_week_end.date().isoformat()}; "
                               f"affected_weeks={len(affected_weeks)}")

            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    print("Done.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True, help="Path to SQLite database file")
    ap.add_argument("--mm", required=True, help="Path to MasterMerchant.lua")
    ap.add_argument("--gbl", required=True, help="Path to GBLData.lua")
    ap.add_argument("--week", default="this", choices=("this", "last"),
                    help="Whether MM was exported as 'this week' or 'last week'")
    ap.add_argument("--schema", default=None,
                    help="Optional path to schema.sql; applied before ingest if given")
    args = ap.parse_args()
    run(args.db, args.mm, args.gbl, args.week, args.schema)


if __name__ == "__main__":
    main()
