# AKTT Guild Stats - Phase 1

A SQLite-backed replacement for the spreadsheet-based weekly stats workflow.
Phase 1 is ingest-only: the legacy spreadsheet pipeline keeps running unchanged
while this database accumulates a clean, queryable record alongside it. Future
phases (web app, automated upload, etc.) will read from this database.

## What it does

* `backfill.py` - one-time import of the two archive workbooks (donations and
  raffle) into the database. Covers Feb 2022 through the most recent week.
* `ingest.py` - weekly run that takes the same `MasterMerchant.lua` and
  `GBLData.lua` files the legacy `guild_stats.py` consumes, and writes them to
  SQLite. Idempotent: running twice on the same export does no harm.
* `validate.py` - sanity check that compares the ingested current-week numbers
  against `donation_summary.csv` from the legacy pipeline. All seven fields
  should match exactly.

## Schema overview

* `weeks` - one row per trade week (ends Tuesday 19:00 UTC).
* `users` - one row per guild account, e.g. `@jeffk42`.
* `user_week_stats` - per-user, per-week aggregates: sales, taxes, purchases,
  rank, total deposits, total raffle, total donations. UNIQUE on (user, week).
* `bank_transactions` - individual guild bank transactions from `GBLData.lua`
  (or backfilled raffle entries). UNIQUE on the game's transaction_id.
* `ingest_runs` - audit log; one row per script invocation.

`is_backfilled` columns mark rows that came from the spreadsheet history vs
the live Lua ingest, so you can audit/reconcile later.

## Setup on the Proxmox LXC

A minimal Debian/Ubuntu LXC with 1 vCPU, 1 GB RAM, and 8 GB disk is plenty.

```bash
apt update && apt install -y python3 python3-pip sqlite3
pip3 install slpp openpyxl

# Drop these files (and the schema) into a working directory, e.g.:
mkdir -p /var/lib/guildstats
cd /var/lib/guildstats
# (copy schema.sql, guildstats.py, ingest.py, backfill.py, validate.py here)

# Initialize the database
sqlite3 guildstats.db < schema.sql
```

## One-time backfill

Copy the two archive workbooks to the LXC, then:

```bash
python3 backfill.py \
  --db guildstats.db \
  --donations "AKTT Weekly Raw Data.xlsx" \
  --raffle    "AKTT Standard Raffle.xlsx" \
  --schema    schema.sql
```

This populates ~218 weeks of `user_week_stats` and ~11k raffle transactions in
about 20 seconds. Re-running the backfill is safe; it UPSERTs by (user, week)
and ignores duplicate transaction IDs.

## Ongoing weekly ingest

Each time you do an in-game export and run the legacy `guild_stats.py`, also
copy `MasterMerchant.lua` and `GBLData.lua` to the LXC and run:

```bash
# For "this week" (mid-week snapshot):
python3 ingest.py --db guildstats.db \
  --mm  /path/to/MasterMerchant.lua \
  --gbl /path/to/GBLData.lua \
  --week this

# For "last week" (final snapshot taken just after Tuesday rollover):
python3 ingest.py --db guildstats.db \
  --mm  /path/to/MasterMerchant.lua \
  --gbl /path/to/GBLData.lua \
  --week last
```

Idempotent: re-running the same export does no harm. The script:

1. Upserts MM EXPORT data into `user_week_stats` for the target week
2. Upserts every transaction in GBL `history` into `bank_transactions`
   (deduped by transaction_id; GBL retains ~2 weeks of history so multiple
   weeks may be touched on a single run)
3. Recomputes `total_deposits`, `total_raffle`, `total_donations` for every
   affected week from `bank_transactions`

## Validating against the legacy pipeline

Run both `guild_stats.py` (legacy) and `ingest.py` (new) on the same Lua
exports, then:

```bash
python3 validate.py \
  --db guildstats.db \
  --csv /path/to/donation_summary.csv \
  --week this
```

If everything is wired up correctly, you should see all seven fields
(rank, sales, taxes, purchases, deposits, raffle, donations) match for every
user with no mismatches.

## Useful queries

```sql
-- Lifetime sales per user (top 20)
SELECT u.account_name, SUM(s.sales) AS lifetime_sales
  FROM user_week_stats s JOIN users u ON u.id = s.user_id
 GROUP BY u.id ORDER BY lifetime_sales DESC LIMIT 20;

-- Weekly guild totals
SELECT w.ending_date,
       SUM(s.sales)           AS sales,
       SUM(s.taxes)           AS taxes,
       SUM(s.total_deposits)  AS deposits,
       SUM(s.total_raffle)    AS raffle,
       SUM(s.total_donations) AS donations
  FROM user_week_stats s JOIN weeks w ON w.id = s.week_id
 GROUP BY w.id ORDER BY w.ending_date DESC LIMIT 12;

-- Raffle entries for one user across a date range
SELECT bt.occurred_at, bt.gold_amount, bt.gold_amount - 1 AS purchase_amount
  FROM bank_transactions bt JOIN users u ON u.id = bt.user_id
 WHERE u.account_name = '@Sairus'
   AND bt.transaction_type = 'dep_gold'
   AND ((bt.gold_amount - 1) % 1000) = 0
   AND bt.occurred_at >= '2026-01-01'
 ORDER BY bt.occurred_at;

-- Item donations: who's giving what
SELECT u.account_name, bt.item_description, bt.item_count, bt.item_value,
       bt.item_count * bt.item_value AS total_value, bt.occurred_at
  FROM bank_transactions bt JOIN users u ON u.id = bt.user_id
 WHERE bt.transaction_type = 'dep_item'
 ORDER BY total_value DESC LIMIT 50;
```

## Files in this delivery

| File                     | Role                                                    |
|--------------------------|---------------------------------------------------------|
| `schema.sql`             | SQLite schema (apply with `sqlite3 db < schema.sql`)    |
| `guildstats.py`          | Shared library (db helpers, week math, raffle predicate)|
| `backfill.py`            | One-time historical backfill from the two workbooks     |
| `ingest.py`              | Ongoing weekly ingest from MM + GBL Lua exports         |
| `validate.py`            | Compare DB against legacy `donation_summary.csv`        |
| `_smoketest_slpp_shim.py`| Sandbox-only Lua extractor; safe to delete on the LXC   |

## What's NOT here yet (future phases)

* Web UI / public-facing site
* Automatic file transfer from Windows to the LXC
* Discord bot, scheduled cron triggers, etc.

These are deliberately out of scope for phase 1. The point of phase 1 is to
get a clean, queryable database accumulating in parallel with the existing
spreadsheet workflow, so you can keep using what works while we build on top
of the new foundation.
