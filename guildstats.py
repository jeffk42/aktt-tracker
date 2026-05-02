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
RAFFLE_HIGH_ROLLER_PRICE = 50000   # gold per HR ticket
RAFFLE_BUNDLE_PAID = 25            # paid tickets per bundle
RAFFLE_BUNDLE_FREE = 5             # free tickets per bundle (current rule)
DONATION_TICKET_PRICE = 2000       # gold value per free ticket on donations
DONATION_MIN_VALUE = 10000         # minimum mail-in value to grant raffle entry
RAFFLE_DEADLINE_DOW = 4            # Friday (Mon=0)
RAFFLE_DEADLINE_HOUR_LOCAL = 20    # 8pm in raffle timezone
RAFFLE_TIMEZONE = "US/Eastern"


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


# =============================================================================
# Phase 2: raffles, raffle entries, prizes, winners, manual donations
# =============================================================================

from datetime import time as _time
try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None


def raffle_deadline_utc(drawing_date: "datetime|date|str") -> datetime:
    """Given a Friday drawing date (date, datetime, or 'YYYY-MM-DD' string),
    return the deadline datetime in UTC (Friday 20:00 ET, DST-aware)."""
    if isinstance(drawing_date, str):
        drawing_date = datetime.strptime(drawing_date, "%Y-%m-%d").date()
    elif isinstance(drawing_date, datetime):
        drawing_date = drawing_date.date()
    if ZoneInfo is None:
        # Fallback: assume UTC-4 (EDT). Used only on environments without zoneinfo.
        local = datetime.combine(drawing_date, _time(RAFFLE_DEADLINE_HOUR_LOCAL, 0, 0))
        return (local + timedelta(hours=4)).replace(tzinfo=timezone.utc)
    local = datetime.combine(
        drawing_date, _time(RAFFLE_DEADLINE_HOUR_LOCAL, 0, 0),
        tzinfo=ZoneInfo(RAFFLE_TIMEZONE),
    )
    return local.astimezone(timezone.utc)


def raffle_drawing_date_for(at: datetime) -> "date":
    """Given a UTC datetime, return the date of the Friday that ENDS the raffle
    week containing `at` (i.e. the next Friday 8pm ET on or after `at`)."""
    if at.tzinfo is None:
        raise ValueError("raffle_drawing_date_for requires tz-aware datetime")
    # Walk forward day by day; cheap and correct
    if ZoneInfo is None:
        local = at.astimezone(timezone.utc)
    else:
        local = at.astimezone(ZoneInfo(RAFFLE_TIMEZONE))
    days_ahead = (RAFFLE_DEADLINE_DOW - local.weekday()) % 7
    candidate_date = (local + timedelta(days=days_ahead)).date()
    candidate_deadline = raffle_deadline_utc(candidate_date)
    if at <= candidate_deadline:
        return candidate_date
    # Already past this Friday's deadline; use next Friday
    return (local + timedelta(days=days_ahead + 7)).date()


def compute_paid_free_hr(gold_amount: int) -> tuple[int, int, int]:
    """Apply current rules to a gold deposit (NOT including the +1 modifier).
    Returns (paid_tickets, free_tickets, high_roller_tickets).

    paid  = floor((gold-1)/1000)            # the +1 marks raffle intent
    free  = floor(paid / 25) * 5            # "Buy 25 Get 5 Free"
    HR    = floor((gold-1) / 50000)
    """
    if gold_amount is None or gold_amount <= 0:
        return 0, 0, 0
    purchase = gold_amount - RAFFLE_DEPOSIT_MODIFIER
    paid = purchase // RAFFLE_TICKET_PRICE
    free = (paid // RAFFLE_BUNDLE_PAID) * RAFFLE_BUNDLE_FREE
    hr = purchase // RAFFLE_HIGH_ROLLER_PRICE
    return paid, free, hr


def compute_donation_free_tickets(value: int) -> int:
    """Item donation: tickets = FLOOR(value / 2000). All counted as free tickets;
    no contribution to paid or high_roller tickets."""
    if value is None or value <= 0:
        return 0
    return value // DONATION_TICKET_PRICE


# --- raffle table helpers ----------------------------------------------------

def upsert_raffle(conn: sqlite3.Connection,
                  raffle_type: str,
                  drawing_date,
                  status: str = "open",
                  is_backfilled: bool = False) -> int:
    """Get-or-create a raffle row. drawing_date can be date/datetime/str."""
    if isinstance(drawing_date, datetime):
        drawing_date = drawing_date.date()
    if not isinstance(drawing_date, str):
        drawing_date_str = drawing_date.isoformat()
    else:
        drawing_date_str = drawing_date
    deadline = raffle_deadline_utc(drawing_date_str)
    deadline_str = deadline.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    row = conn.execute(
        """
        INSERT INTO raffles (raffle_type, drawing_date, deadline_at, status, is_backfilled)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(raffle_type, drawing_date) DO UPDATE SET
            deadline_at = excluded.deadline_at
        RETURNING id
        """,
        (raffle_type, drawing_date_str, deadline_str, status, 1 if is_backfilled else 0),
    ).fetchone()
    return row["id"]


def find_open_raffle(conn: sqlite3.Connection, raffle_type: str = "standard") -> Optional[int]:
    """Return the id of the open raffle of the given type, or None."""
    row = conn.execute(
        "SELECT id FROM raffles WHERE raffle_type = ? AND status = 'open' "
        "ORDER BY drawing_date ASC LIMIT 1",
        (raffle_type,),
    ).fetchone()
    return row["id"] if row else None


def ensure_open_raffle(conn: sqlite3.Connection,
                       at: datetime,
                       raffle_type: str = "standard") -> int:
    """Find or create the open raffle of the given type that should hold a
    transaction occurring at `at` (UTC datetime)."""
    drawing_date = raffle_drawing_date_for(at)
    return upsert_raffle(conn, raffle_type, drawing_date, status="open")


# --- raffle entry helpers ----------------------------------------------------

@dataclass
class RaffleEntry:
    raffle_id: int
    user_id: int
    source: str                     # bank_deposit | mail_donation | event | high_roller_qualifier
    occurred_at: datetime
    gold_amount: Optional[int] = None
    paid_tickets: int = 0
    free_tickets: int = 0
    high_roller_tickets: int = 0
    descriptor: Optional[str] = None
    source_transaction_id: Optional[str] = None
    manual_donation_id: Optional[int] = None
    start_number: Optional[int] = None
    end_number: Optional[int] = None
    is_backfilled: bool = False


def insert_raffle_entry(conn: sqlite3.Connection, e: RaffleEntry) -> int:
    """Insert a raffle entry. For idempotency in live ingest, we de-duplicate on
    (raffle_id, source_transaction_id) when source_transaction_id is set."""
    occurred_str = e.occurred_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    if e.source_transaction_id:
        # Skip if already present for this raffle+transaction
        existing = conn.execute(
            "SELECT id FROM raffle_entries WHERE raffle_id=? AND source_transaction_id=?",
            (e.raffle_id, e.source_transaction_id),
        ).fetchone()
        if existing:
            return existing["id"]
    cur = conn.execute(
        """
        INSERT INTO raffle_entries
            (raffle_id, user_id, source, gold_amount, paid_tickets, free_tickets,
             high_roller_tickets, descriptor, source_transaction_id, manual_donation_id,
             occurred_at, start_number, end_number, is_backfilled)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (e.raffle_id, e.user_id, e.source, e.gold_amount,
         e.paid_tickets, e.free_tickets, e.high_roller_tickets,
         e.descriptor, e.source_transaction_id, e.manual_donation_id,
         occurred_str, e.start_number, e.end_number,
         1 if e.is_backfilled else 0),
    )
    return cur.lastrowid


def recompute_raffle_totals(conn: sqlite3.Connection, raffle_id: int) -> None:
    """Update raffle.max_ticket_number and total_tickets_sold based on entries.

    max_ticket_number = max(end_number) — upper bound for random.org range,
    includes paid + free + donation tickets.

    total_tickets_sold = sum(paid_tickets) — matches the spreadsheet semantics
    where prize-unlock thresholds compare against paid tickets only.
    For HR raffles paid_tickets is the HR ticket count, so the same SUM works.
    """
    row = conn.execute(
        """
        SELECT MAX(end_number) AS max_end,
               COALESCE(SUM(paid_tickets), 0) AS total_paid
        FROM raffle_entries WHERE raffle_id = ?
        """,
        (raffle_id,),
    ).fetchone()
    conn.execute(
        "UPDATE raffles SET max_ticket_number=?, total_tickets_sold=? WHERE id=?",
        (row["max_end"], row["total_paid"], raffle_id),
    )


# --- prizes ------------------------------------------------------------------

@dataclass
class Prize:
    raffle_id: int
    category: str           # 'main' | 'mini'
    display_order: int
    prize_type: str         # 'gold' | 'item'
    active_at_ticket_count: Optional[int] = None
    gold_amount: Optional[int] = None
    item_description: Optional[str] = None
    notes: Optional[str] = None


def upsert_prize(conn: sqlite3.Connection, p: Prize) -> int:
    cur = conn.execute(
        """
        INSERT INTO prizes
            (raffle_id, category, display_order, active_at_ticket_count,
             prize_type, gold_amount, item_description, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(raffle_id, category, display_order) DO UPDATE SET
            active_at_ticket_count = excluded.active_at_ticket_count,
            prize_type             = excluded.prize_type,
            gold_amount            = excluded.gold_amount,
            item_description       = excluded.item_description,
            notes                  = excluded.notes
        RETURNING id
        """,
        (p.raffle_id, p.category, p.display_order, p.active_at_ticket_count,
         p.prize_type, p.gold_amount, p.item_description, p.notes),
    )
    return cur.fetchone()["id"]


# --- winners -----------------------------------------------------------------

def upsert_winner(conn: sqlite3.Connection, prize_id: int, user_id: int,
                  winning_ticket_number: Optional[int],
                  source: str = "apps_script_ingest",
                  notes: Optional[str] = None) -> int:
    cur = conn.execute(
        """
        INSERT INTO raffle_winners
            (prize_id, user_id, winning_ticket_number, source, notes)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(prize_id) DO UPDATE SET
            user_id               = excluded.user_id,
            winning_ticket_number = excluded.winning_ticket_number,
            source                = excluded.source,
            notes                 = excluded.notes
        RETURNING id
        """,
        (prize_id, user_id, winning_ticket_number, source, notes),
    )
    return cur.fetchone()["id"]


# --- manual donations --------------------------------------------------------

def add_manual_donation(conn: sqlite3.Connection, user_id: int, week_id: int,
                        value: int, description: Optional[str] = None,
                        received_at: Optional[datetime] = None,
                        recorded_by: Optional[str] = None) -> int:
    """Record a mail-in donation. Returns manual_donations.id."""
    if received_at is None:
        received_at = datetime.now(timezone.utc)
    received_str = received_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.execute(
        """
        INSERT INTO manual_donations (user_id, week_id, value, description,
                                      received_at, recorded_by)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (user_id, week_id, value, description, received_str, recorded_by),
    )
    return cur.lastrowid


def promote_donations_to_raffle(conn: sqlite3.Connection,
                                week_id: int,
                                raffle_id: int,
                                min_value: int = DONATION_MIN_VALUE) -> int:
    """Promote a week's donations into a raffle as DONATION entries, mirroring
    the legacy Apps Script's Tuesday-rollover behavior:

        per user: total = sum(unpromoted manual_donations) + sum(bank dep_item value*count)
        if total > min_value:
            create one raffle_entries row with FLOOR(total/2000) free tickets
            mark the manual_donations rows as promoted

    Bank item donations are not "consumed" - they always count toward the next
    week's promote. Idempotency comes from a synthetic source_transaction_id
    of the form 'donation:week<n>:user<m>', so re-running the promote on the
    same week is a no-op (insert_raffle_entry dedups by that key).

    Returns the count of users promoted (i.e. raffle_entries rows created or
    updated)."""
    rows = conn.execute(
        """
        WITH md AS (
            SELECT user_id,
                   SUM(value) AS mail_value,
                   GROUP_CONCAT(id) AS donation_ids,
                   MIN(received_at) AS earliest
              FROM manual_donations
             WHERE week_id = ? AND is_promoted = 0
             GROUP BY user_id
        ),
        bt AS (
            SELECT user_id,
                   SUM(item_value * COALESCE(item_count, 0)) AS item_value
              FROM bank_transactions
             WHERE week_id = ? AND transaction_type = 'dep_item'
               AND item_value IS NOT NULL
             GROUP BY user_id
        )
        SELECT u.id AS user_id,
               COALESCE(md.mail_value, 0)  AS mail_value,
               COALESCE(bt.item_value, 0)  AS item_value,
               COALESCE(md.mail_value, 0) + COALESCE(bt.item_value, 0) AS total_value,
               md.donation_ids,
               md.earliest
          FROM users u
          LEFT JOIN md ON md.user_id = u.id
          LEFT JOIN bt ON bt.user_id = u.id
         WHERE (COALESCE(md.mail_value, 0) + COALESCE(bt.item_value, 0)) > ?
        """,
        (week_id, week_id, min_value),
    ).fetchall()
    promoted = 0
    for r in rows:
        total = r["total_value"]
        free = compute_donation_free_tickets(total)
        if r["earliest"]:
            occurred = datetime.strptime(r["earliest"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        else:
            # No mail-in donation; use "now" as the proxy timestamp
            occurred = datetime.now(timezone.utc)
        first_id = int(r["donation_ids"].split(",")[0]) if r["donation_ids"] else None
        synthetic_txid = f"donation:week{week_id}:user{r['user_id']}"
        e = RaffleEntry(
            raffle_id=raffle_id, user_id=r["user_id"], source="mail_donation",
            occurred_at=occurred, gold_amount=total,
            paid_tickets=0, free_tickets=free, high_roller_tickets=0,
            descriptor="DONATION", manual_donation_id=first_id,
            source_transaction_id=synthetic_txid,
        )
        insert_raffle_entry(conn, e)
        if r["donation_ids"]:
            conn.execute(
                "UPDATE manual_donations SET is_promoted=1, promoted_to_raffle_id=? "
                "WHERE id IN (" + r["donation_ids"] + ")",
                (raffle_id,),
            )
        promoted += 1
    return promoted


# --- guild traders -----------------------------------------------------------

@dataclass
class TraderBid:
    week_id: int
    trader_name: str
    location: Optional[str] = None
    bid_amount: int = 0
    notes: Optional[str] = None


def upsert_trader_bid(conn: sqlite3.Connection, t: TraderBid) -> str:
    """Insert or update a guild_traders row keyed by week_id."""
    cur = conn.execute(
        """
        INSERT INTO guild_traders
            (week_id, trader_name, location, bid_amount, notes, updated_at)
        VALUES (?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(week_id) DO UPDATE SET
            trader_name = excluded.trader_name,
            location    = excluded.location,
            bid_amount  = excluded.bid_amount,
            notes       = excluded.notes,
            updated_at  = datetime('now')
        """,
        (t.week_id, t.trader_name, t.location, t.bid_amount, t.notes),
    )
    return "upserted"


def delete_trader_bid(conn: sqlite3.Connection, week_id: int) -> int:
    """Delete a guild_traders row (e.g. to mark a week as 'no trader won').
    Returns the number of rows removed."""
    cur = conn.execute("DELETE FROM guild_traders WHERE week_id = ?", (week_id,))
    return cur.rowcount

