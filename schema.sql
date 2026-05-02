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

-- =============================================================================
-- Phase 2 additions: raffles, entries, prizes, winners, manual donations.
-- Apply with: sqlite3 guildstats.db < schema.sql  (re-application is safe)
-- =============================================================================

-- One row per weekly drawing. Standard and high_roller for the same week are
-- two separate rows.
CREATE TABLE IF NOT EXISTS raffles (
    id                  INTEGER PRIMARY KEY,
    raffle_type         TEXT NOT NULL,                -- 'standard' | 'high_roller'
    drawing_date        TEXT NOT NULL,                -- "YYYY-MM-DD" (Friday)
    deadline_at         TEXT NOT NULL,                -- "YYYY-MM-DD HH:MM:SS" UTC
    status              TEXT NOT NULL DEFAULT 'open', -- 'open' | 'closed' | 'drawn'
    max_ticket_number   INTEGER,                      -- upper bound for drawing
    total_tickets_sold  INTEGER,                      -- cached aggregate
    is_backfilled       INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (raffle_type, drawing_date)
);

-- One row per (user, raffle, source-occurrence). A user with two separate
-- gold deposits gets two entries. Ticket counts are stored explicitly so
-- historical entries preserve whatever rule was in force at the time.
CREATE TABLE IF NOT EXISTS raffle_entries (
    id                      INTEGER PRIMARY KEY,
    raffle_id               INTEGER NOT NULL REFERENCES raffles(id),
    user_id                 INTEGER NOT NULL REFERENCES users(id),
    source                  TEXT NOT NULL,            -- 'bank_deposit' | 'mail_donation' | 'event' | 'high_roller_qualifier'
    gold_amount             INTEGER,                  -- gold deposited or assessed donation value
    paid_tickets            INTEGER NOT NULL DEFAULT 0,
    free_tickets            INTEGER NOT NULL DEFAULT 0,
    high_roller_tickets     INTEGER NOT NULL DEFAULT 0,
    descriptor              TEXT,                     -- 'DONATION' | 'FISHING' | 'EVENT' | NULL
    source_transaction_id   TEXT,                     -- FK by value to bank_transactions.transaction_id
    manual_donation_id      INTEGER REFERENCES manual_donations(id),
    occurred_at             TEXT NOT NULL,
    start_number            INTEGER,                  -- ticket range start in this raffle
    end_number              INTEGER,                  -- ticket range end (inclusive)
    is_backfilled           INTEGER NOT NULL DEFAULT 0,
    created_at              TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Tiered prize list per raffle. Prizes are awarded only if total_tickets_sold
-- reaches active_at_ticket_count. category distinguishes main from mini.
CREATE TABLE IF NOT EXISTS prizes (
    id                          INTEGER PRIMARY KEY,
    raffle_id                   INTEGER NOT NULL REFERENCES raffles(id),
    category                    TEXT NOT NULL DEFAULT 'main',  -- 'main' | 'mini'
    display_order               INTEGER NOT NULL,              -- 1, 2, 3... within category
    active_at_ticket_count      INTEGER,                       -- threshold; NULL for mini
    prize_type                  TEXT NOT NULL,                 -- 'gold' | 'item'
    gold_amount                 INTEGER,
    item_description            TEXT,
    notes                       TEXT,
    created_at                  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (raffle_id, category, display_order)
);

-- One row per prize awarded. Prize -> winner is 1:1.
CREATE TABLE IF NOT EXISTS raffle_winners (
    id                      INTEGER PRIMARY KEY,
    prize_id                INTEGER NOT NULL UNIQUE REFERENCES prizes(id),
    user_id                 INTEGER NOT NULL REFERENCES users(id),
    winning_ticket_number   INTEGER NOT NULL,
    drawn_at                TEXT NOT NULL DEFAULT (datetime('now')),
    source                  TEXT NOT NULL DEFAULT 'apps_script_ingest',  -- how the winner was decided
    notes                   TEXT
);

-- Mail-in donations the officer entered. These are independent records that
-- can be promoted into raffle_entries via the donations CLI.
CREATE TABLE IF NOT EXISTS manual_donations (
    id                      INTEGER PRIMARY KEY,
    user_id                 INTEGER NOT NULL REFERENCES users(id),
    week_id                 INTEGER NOT NULL REFERENCES weeks(id),
    value                   INTEGER NOT NULL,
    description             TEXT,
    received_at             TEXT,
    recorded_by             TEXT,
    promoted_to_raffle_id   INTEGER REFERENCES raffles(id),
    is_promoted             INTEGER NOT NULL DEFAULT 0,
    created_at              TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_raffles_drawing      ON raffles(drawing_date);
CREATE INDEX IF NOT EXISTS idx_raffles_type_date    ON raffles(raffle_type, drawing_date);
CREATE INDEX IF NOT EXISTS idx_re_raffle            ON raffle_entries(raffle_id);
CREATE INDEX IF NOT EXISTS idx_re_user              ON raffle_entries(user_id);
CREATE INDEX IF NOT EXISTS idx_re_source            ON raffle_entries(source);
CREATE INDEX IF NOT EXISTS idx_re_txid              ON raffle_entries(source_transaction_id);
CREATE INDEX IF NOT EXISTS idx_prizes_raffle        ON prizes(raffle_id);
CREATE INDEX IF NOT EXISTS idx_winners_user         ON raffle_winners(user_id);
CREATE INDEX IF NOT EXISTS idx_md_user              ON manual_donations(user_id);
CREATE INDEX IF NOT EXISTS idx_md_unpromoted        ON manual_donations(is_promoted) WHERE is_promoted = 0;

-- =============================================================================
-- Phase 2.5b additions: Drive integration support for manual_donations.
-- =============================================================================

-- 'source' lets us distinguish CLI-entered donations from sheet-imported ones,
-- so the sheet importer can refresh its own rows without touching CLI entries.
-- 'sheet_row_hash' is a content hash used by the sheet importer for idempotency.
ALTER TABLE manual_donations ADD COLUMN source        TEXT NOT NULL DEFAULT 'cli';
ALTER TABLE manual_donations ADD COLUMN sheet_row_hash TEXT;

CREATE INDEX IF NOT EXISTS idx_md_source_week ON manual_donations(source, week_id);

-- =============================================================================
-- Phase 2.5c: weekly guild trader bids
-- =============================================================================

-- One row per trade week WHERE THE GUILD WON A TRADER.
-- Weeks with no winning bid simply have no row here; a LEFT JOIN to weeks
-- reveals the gaps.
--
-- We track only the winning bid (per the user request) - losing bids are not
-- stored. trader_name + location are denormalized text rather than a lookup
-- table; the set of ESO traders changes rarely enough that normalizing
-- doesn't earn its keep, and the UI can group by name for "weeks at this
-- trader" stats.
CREATE TABLE IF NOT EXISTS guild_traders (
    id            INTEGER PRIMARY KEY,
    week_id       INTEGER NOT NULL UNIQUE REFERENCES weeks(id),
    trader_name   TEXT NOT NULL,         -- NPC name (e.g. "Faillaure Leleu")
    location      TEXT,                  -- where the NPC is (e.g. "Wayrest, Stormhaven")
    bid_amount    INTEGER NOT NULL,      -- winning bid in gold
    notes         TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_guild_traders_name ON guild_traders(trader_name);

-- =============================================================================
-- Phase 3: web UI support
-- =============================================================================

-- Key-value store for guild-wide settings used by the web app (e.g. the
-- weekly contribution goal). Update with:
--   UPDATE guild_settings SET value='50000' WHERE key='weekly_contribution_goal';
CREATE TABLE IF NOT EXISTS guild_settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT OR IGNORE INTO guild_settings (key, value)
VALUES ('weekly_contribution_goal', '40000'),
       ('site_title',                'AKTT Guild Stats');
