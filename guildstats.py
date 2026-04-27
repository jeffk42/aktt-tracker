"""Shared utilities for AKTT guild-stats ingest and backfill scripts.

Provides:
  - SQLite connection with sane defaults (foreign keys on, WAL, row factory)
  - Trade-week math (week ends Tue 19:00 UTC)
  - Idempotent UPSERT helpers for users, weeks, user_week_stats, bank_transactions
  - Raffle predicate helpers
  - Audit-run context manager (writes to ingest_runs)
"""
from __future__ import annotations
import sqlite3
import contextlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Iterable

# Trade week ends on Tuesday at 19:00:00 UTC.
TRADE_WEEK_DAY = 1            # Mon=0, Tue=1
TRADE_WEEK_HOUR = 19          # UTC

# Default raffle rules (matching guild_stats.py at the time this was written).
RAFFLE_TICKET_PRICE = 1000
RAFFLE_DEPOSIT_MODIFIER = 1


def open_db(path: str | Path) -> sqlite3.Connection:
    """Open a SQLite db with foreign keys, WAL, and a sane row factory."""
    conn = sqlite3.connect(str(path), isolation_level=None)  # autocommit; we open transactions explicitly
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def apply_schema(conn: sqlite3.Connection, schema_path: str | Path) -> None:
    """Apply schema.sql to the connection."""
    with open(schema_path, "r", encoding="utf-8") as f:
        conn.executescript(f.read())


# --- trade week math ----------------------------------------------------------

def trade_week_ending(at: datetime) -> datetime:
    """Return the UTC datetime of the Tuesday 19:00:00 that ENDS the trade week
    containing `at`. `at` must be timezone-aware.

    A transaction at exactly Tue 19:00 is considered part of the JUST-ENDED week
    (matching ESO market rollover convention).
    """
    if at.tzinfo is None:
        raise ValueError("trade_week_ending requires a timezone-aware datetime")
    at_utc = at.astimezone(timezone.utc)
    today_rollover = at_utc.replace(hour=TRADE_WEEK_HOUR, minute=0, second=0, microsecond=0)

    # Walk forward to the next Tuesday 19:00 (inclusive of the transaction's moment).
    days_ahead = (TRADE_WEEK_DAY - at_utc.weekday()) % 7
    candidate = today_rollover + timedelta(days=days_ahead)
    if candidate < at_utc:
        candidate += timedelta(days=7)
    return candidate


# --- raffle predicate ---------------------------------------------------------

def is_raffle_deposit(gold_amount: int,
                      ticket_price: int = RAFFLE_TICKET_PRICE,
                      modifier: int = RAFFLE_DEPOSIT_MODIFIER) -> bool:
    """A gold deposit counts as a raffle ticket purchase if (amount - modifier)
    is positive and divides cleanly by ticket_price. Mirrors guild_stats.py."""
    if gold_amount is None or gold_amount <= modifier:
        return False
    remainder = gold_amount - modifier
    return remainder > 0 and remainder % ticket_price == 0


def raffle_purchase_amount(gold_amount: int,
                           modifier: int = RAFFLE_DEPOSIT_MODIFIER) -> int:
    """The 'purchase amount' shown on raffle.csv (with the modifier removed)."""
    return gold_amount - modifier


# --- upsert helpers -----------------------------------------------------------

def upsert_user(conn: sqlite3.Connection, account_name: str, excluded: bool = False) -> int:
    """Get-or-create a user row. Returns user_id."""
    row = conn.execute(
        "INSERT INTO users (account_name, excluded) VALUES (?, ?) "
        "ON CONFLICT(account_name) DO UPDATE SET excluded = excluded.excluded "
        "RETURNING id",
        (account_name, 1 if excluded else 0),
    ).fetchone()
    return row["id"]


def upsert_week(conn: sqlite3.Connection, ending_at: datetime) -> int:
    """Get-or-create a week row. ending_at must be the Tuesday 19:00 UTC rollover."""
    if ending_at.tzinfo is None:
        ending_at = ending_at.replace(tzinfo=timezone.utc)
    ending_at_utc = ending_at.astimezone(timezone.utc).replace(microsecond=0, tzinfo=None)
    ending_date = ending_at_utc.date().isoformat()
    row = conn.execute(
        "INSERT INTO weeks (ending_date, ending_at) VALUES (?, ?) "
        "ON CONFLICT(ending_date) DO UPDATE SET ending_at = excluded.ending_at "
        "RETURNING id",
        (ending_date, ending_at_utc.isoformat(sep=" ")),
    ).fetchone()
    return row["id"]


@dataclass
class WeekStats:
    user_id: int
    week_id: int
    rank: Optional[int] = None
    sales: int = 0
    taxes: int = 0
    purchases: int = 0
    total_deposits: int = 0
    total_raffle: int = 0
    total_donations: int = 0
    is_backfilled: bool = False


def upsert_user_week_stats(conn: sqlite3.Connection, s: WeekStats) -> str:
    """UPSERT one user_week_stats row. Returns 'inserted' or 'updated'."""
    cur = conn.execute(
        """
        INSERT INTO user_week_stats
            (user_id, week_id, rank, sales, taxes, purchases,
             total_deposits, total_raffle, total_donations, is_backfilled, snapshot_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(user_id, week_id) DO UPDATE SET
            rank            = excluded.rank,
            sales           = excluded.sales,
            taxes           = excluded.taxes,
            purchases       = excluded.purchases,
            total_deposits  = excluded.total_deposits,
            total_raffle    = excluded.total_raffle,
            total_donations = excluded.total_donations,
            is_backfilled   = excluded.is_backfilled,
            snapshot_at     = datetime('now')
        RETURNING (snapshot_at = datetime('now')) AS _ignored,
                  (CASE WHEN id IS NULL THEN 'inserted' ELSE 'present' END) AS _ignored2
        """,
        (s.user_id, s.week_id, s.rank, s.sales, s.taxes, s.purchases,
         s.total_deposits, s.total_raffle, s.total_donations,
         1 if s.is_backfilled else 0),
    )
    cur.fetchone()
    # SQLite doesn't directly tell us insert-vs-update from RETURNING; we use
    # changes() which returns 1 for both, so we just report the call succeeded.
    return "upserted"


@dataclass
class BankTxn:
    transaction_id: str
    user_id: int
    week_id: int
    transaction_type: str  # dep_gold, dep_item, wd_gold, wd_item
    gold_amount: Optional[int]
    item_count: Optional[int]
    item_description: Optional[str]
    item_link: Optional[str]
    item_value: Optional[int]
    occurred_at: datetime
    is_backfilled: bool = False


def upsert_bank_transaction(conn: sqlite3.Connection, t: BankTxn) -> str:
    """INSERT OR IGNORE on transaction_id. Returns 'inserted' or 'skipped'."""
    occurred_str = t.occurred_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.execute(
        """
        INSERT INTO bank_transactions
            (transaction_id, user_id, week_id, transaction_type,
             gold_amount, item_count, item_description, item_link, item_value,
             occurred_at, is_backfilled)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(transaction_id) DO NOTHING
        """,
        (t.transaction_id, t.user_id, t.week_id, t.transaction_type,
         t.gold_amount, t.item_count, t.item_description, t.item_link, t.item_value,
         occurred_str, 1 if t.is_backfilled else 0),
    )
    return "inserted" if cur.rowcount == 1 else "skipped"


# --- recompute weekly totals from bank_transactions ---------------------------

def recompute_week_totals(conn: sqlite3.Connection, week_id: int,
                          ticket_price: int = RAFFLE_TICKET_PRICE,
                          modifier: int = RAFFLE_DEPOSIT_MODIFIER) -> int:
    """Recompute total_deposits, total_raffle, total_donations on user_week_stats
    for the given week, using bank_transactions in that same week.

    Returns the number of user_week_stats rows updated.
    """
    # Pull aggregates per user_id for that week
    rows = conn.execute(
        """
        SELECT bt.user_id,
               SUM(CASE WHEN bt.transaction_type = 'dep_gold'
                        AND ((bt.gold_amount - ?) % ?) != 0
                        THEN bt.gold_amount ELSE 0 END) AS total_deposits,
               SUM(CASE WHEN bt.transaction_type = 'dep_gold'
                        AND ((bt.gold_amount - ?) % ?) = 0
                        AND bt.gold_amount > ?
                        THEN bt.gold_amount - ? ELSE 0 END) AS total_raffle,
               SUM(CASE WHEN bt.transaction_type = 'dep_item' AND bt.item_value IS NOT NULL
                        THEN bt.item_value * COALESCE(bt.item_count, 0) ELSE 0 END) AS total_donations
        FROM bank_transactions bt
        WHERE bt.week_id = ?
        GROUP BY bt.user_id
        """,
        (modifier, ticket_price, modifier, ticket_price, modifier, modifier, week_id),
    ).fetchall()

    updated = 0
    for r in rows:
        cur = conn.execute(
            """
            INSERT INTO user_week_stats
                (user_id, week_id, total_deposits, total_raffle, total_donations, is_backfilled)
            VALUES (?, ?, ?, ?, ?, 0)
            ON CONFLICT(user_id, week_id) DO UPDATE SET
                total_deposits  = excluded.total_deposits,
                total_raffle    = excluded.total_raffle,
                total_donations = excluded.total_donations
            """,
            (r["user_id"], week_id,
             r["total_deposits"] or 0, r["total_raffle"] or 0, r["total_donations"] or 0),
        )
        updated += cur.rowcount
    return updated


# --- audit-run context manager -----------------------------------------------

@contextlib.contextmanager
def ingest_run(conn: sqlite3.Connection, source: str, **fields):
    """Open a row in ingest_runs, yield a dict you can mutate, close it on exit."""
    row = conn.execute(
        """
        INSERT INTO ingest_runs (source, week_param, mm_filename, gbl_filename,
                                 workbook_filename, notes)
        VALUES (?, ?, ?, ?, ?, ?)
        RETURNING id
        """,
        (source, fields.get("week_param"), fields.get("mm_filename"),
         fields.get("gbl_filename"), fields.get("workbook_filename"),
         fields.get("notes")),
    ).fetchone()
    run_id = row["id"]
    counts = {"rows_inserted": 0, "rows_updated": 0, "rows_skipped": 0, "notes": fields.get("notes")}
    try:
        yield counts
    finally:
        conn.execute(
            """
            UPDATE ingest_runs
               SET rows_inserted = ?, rows_updated = ?, rows_skipped = ?,
                   notes = COALESCE(?, notes), completed_at = datetime('now')
             WHERE id = ?
            """,
            (counts["rows_inserted"], counts["rows_updated"],
             counts["rows_skipped"], counts.get("notes"), run_id),
        )
