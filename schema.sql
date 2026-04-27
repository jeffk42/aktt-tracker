-- AKTT Guild Stats - SQLite schema
-- Apply with: sqlite3 guildstats.db < schema.sql
-- Re-applying is safe (CREATE IF NOT EXISTS).

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

-- Guild members (and historical members). Username includes the leading "@".
CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY,
    account_name    TEXT NOT NULL UNIQUE,            -- e.g. "@jeffk42"
    excluded        INTEGER NOT NULL DEFAULT 0,      -- 1 to omit from reports (guild bank account, etc.)
    first_seen_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Each row is one trade week. The week ENDS on the Tuesday at 19:00 UTC
-- recorded in `ending_at`; `ending_date` is just the calendar date for convenience.
CREATE TABLE IF NOT EXISTS weeks (
    id              INTEGER PRIMARY KEY,
    ending_date     TEXT NOT NULL UNIQUE,            -- "YYYY-MM-DD"
    ending_at       TEXT NOT NULL,                   -- "YYYY-MM-DD HH:MM:SS" UTC
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Per-user-per-week aggregates. One row per (user, week).
-- Sales/taxes/purchases/rank come from the MasterMerchant EXPORT block (live)
-- or from the spreadsheet history (backfill).
-- total_deposits/total_raffle/total_donations are recomputed from
-- bank_transactions for live weeks, or pulled from the spreadsheet for backfilled weeks.
CREATE TABLE IF NOT EXISTS user_week_stats (
    id                  INTEGER PRIMARY KEY,
    user_id             INTEGER NOT NULL REFERENCES users(id),
    week_id             INTEGER NOT NULL REFERENCES weeks(id),
    rank                INTEGER,
    sales               INTEGER NOT NULL DEFAULT 0,  -- gold sold via guild trader
    taxes               INTEGER NOT NULL DEFAULT 0,  -- guild's tax cut from sales
    purchases           INTEGER NOT NULL DEFAULT 0,  -- gold spent buying from guild trader
    total_deposits      INTEGER NOT NULL DEFAULT 0,  -- gold deposited (excluding raffle)
    total_raffle        INTEGER NOT NULL DEFAULT 0,  -- gold spent on raffle (purchase amount, no +1)
    total_donations     INTEGER NOT NULL DEFAULT 0,  -- estimated value of items deposited
    is_backfilled       INTEGER NOT NULL DEFAULT 0,  -- 1 if from spreadsheet, 0 if from Lua ingest
    snapshot_at         TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (user_id, week_id)
);

-- Individual guild-bank transactions from GBLData.lua (live) or the
-- AKTT Standard Raffle workbook (raffle backfill only, type='dep_gold').
-- transaction_id is ESO's globally-unique ID, so re-ingest is naturally idempotent.
CREATE TABLE IF NOT EXISTS bank_transactions (
    id                  INTEGER PRIMARY KEY,
    transaction_id      TEXT NOT NULL UNIQUE,        -- from the game
    user_id             INTEGER NOT NULL REFERENCES users(id),
    week_id             INTEGER NOT NULL REFERENCES weeks(id),
    transaction_type    TEXT NOT NULL,               -- dep_gold, dep_item, wd_gold, wd_item
    gold_amount         INTEGER,                     -- for *_gold rows
    item_count          INTEGER,                     -- for *_item rows
    item_description    TEXT,
    item_link           TEXT,
    item_value          INTEGER,                     -- estimated unit value, for dep_item
    occurred_at         TEXT NOT NULL,               -- "YYYY-MM-DD HH:MM:SS" UTC
    is_backfilled       INTEGER NOT NULL DEFAULT 0,
    ingested_at         TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Audit log: one row per script invocation that touches the data.
CREATE TABLE IF NOT EXISTS ingest_runs (
    id                  INTEGER PRIMARY KEY,
    source              TEXT NOT NULL,               -- 'live_ingest' | 'backfill_donations' | 'backfill_raffle'
    week_param          TEXT,                        -- 'this' | 'last' | NULL
    mm_filename         TEXT,
    gbl_filename        TEXT,
    workbook_filename   TEXT,
    rows_inserted       INTEGER NOT NULL DEFAULT 0,
    rows_updated        INTEGER NOT NULL DEFAULT 0,
    rows_skipped        INTEGER NOT NULL DEFAULT 0,
    notes               TEXT,
    started_at          TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at        TEXT
);

CREATE INDEX IF NOT EXISTS idx_uws_week    ON user_week_stats(week_id);
CREATE INDEX IF NOT EXISTS idx_uws_user    ON user_week_stats(user_id);
CREATE INDEX IF NOT EXISTS idx_bt_week     ON bank_transactions(week_id);
CREATE INDEX IF NOT EXISTS idx_bt_user     ON bank_transactions(user_id);
CREATE INDEX IF NOT EXISTS idx_bt_occurred ON bank_transactions(occurred_at);
CREATE INDEX IF NOT EXISTS idx_bt_type     ON bank_transactions(transaction_type);
