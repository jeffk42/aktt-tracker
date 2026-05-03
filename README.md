# AKTT Guild Stats

A SQLite-backed replacement for the spreadsheet-driven weekly stats workflow.

The system runs alongside the legacy spreadsheet pipeline. The database
accumulates a clean, queryable record and is queried by a public read-only
web UI; the spreadsheets remain authoritative for in-game raffle drawings
(via the existing Apps Script) until the database takes over winner-picking
in Phase 4.

## Files

| File                     | Role                                                          |
|--------------------------|---------------------------------------------------------------|
| `schema.sql`             | SQLite schema (idempotent; `sqlite3 db < schema.sql`)         |
| `migrate.py`             | Applies missing `ALTER TABLE` columns to an existing DB       |
| `guildstats.py`          | Shared library: db helpers, week math, raffle/donation logic  |
| `backfill.py`            | One-time import of donations workbook history (Phase 1)       |
| `backfill_raffle.py`     | One-time import of standard + high-roller raffle history      |
| `backfill_traders.py`    | One-time import of trader-bid history (2022 onward)           |
| `ingest.py`              | Weekly ingest from MasterMerchant.lua + GBLData.lua           |
| `validate.py`            | Compare DB against legacy `donation_summary.csv` for sanity   |
| `donations.py`           | CLI for mail-in donations (add / list / promote / sheet sync) |
| `entry.py`               | CLI for manual raffle entries (FISHING/EVENT/etc)             |
| `traders.py`             | CLI for weekly winning trader bid (add / list / remove)       |
| `import_winners.py`      | Pull final entries+prizes+winners from raffle xlsx after draw |
| `sync_from_drive.py`     | Drive-side wrapper for import_winners (downloads + imports)   |
| `drive_sync.py`          | Google Drive API helper (service-account auth)                |
| `web/app.py`             | FastAPI read-only web UI (dashboard, rankings, raffles, etc.) |
| `web/templates/*.html`   | Jinja2 templates for each page                                |
| `web/static/*`           | CSS + tiny shared sortable-table JS                           |
| `automation/*`           | systemd units, Caddy config, Windows SCP helper, setup guides |
| `_smoketest_slpp_shim.py`| Sandbox-only Lua extractor; safe to delete on the LXC         |

## LXC setup

A Debian/Ubuntu LXC with 2 vCPU, 2 GB RAM, 8 GB disk is plenty.

```bash
apt update && apt install -y python3 python3-pip python3-venv sqlite3
python3 -m venv ~/venv && source ~/venv/bin/activate
pip install slpp openpyxl tzdata \
            google-api-python-client google-auth \
            fastapi 'uvicorn[standard]' jinja2
```

`tzdata` is required: the raffle-deadline math uses `ZoneInfo('US/Eastern')`,
which fails with `'No time zone found with key US/Eastern'` on minimal Linux
images that don't have system tzdata installed. The PyPI `tzdata` package is
the canonical fix.

Initialize the database:

```bash
sqlite3 guildstats.db < schema.sql
```

## One-time backfill

```bash
# Phase 1: weekly stats + raffle deposits from the donations workbook
python3 backfill.py --db guildstats.db \
    --donations "AKTT Weekly Raw Data.xlsx" \
    --raffle    "AKTT Standard Raffle.xlsx" \
    --schema    schema.sql

# Phase 2: full raffle history (entries, prizes, winners) for both raffles
python3 backfill_raffle.py --db guildstats.db \
    --standard   "AKTT Standard Raffle.xlsx" \
    --highroller "AKTT High-Roller Raffle.xlsx" \
    --schema     schema.sql
```

The phase-1 backfill takes ~20 seconds; the phase-2 backfill takes ~30 seconds.

## Weekly workflow (parallel to the legacy spreadsheet for now)

**Mid-week, after each in-game export.** Copy the two Lua files to the LXC, then:

```bash
# Mid-week snapshot:
python3 ingest.py --db guildstats.db --mm MasterMerchant.lua --gbl GBLData.lua --week this

# Just after Tuesday rollover, to capture the final snapshot of the prior week:
python3 ingest.py --db guildstats.db --mm MasterMerchant.lua --gbl GBLData.lua --week last
```

The ingest is idempotent: running it twice on the same export does no harm.
It updates `user_week_stats`, `bank_transactions`, and creates `raffle_entries`
for every raffle-eligible gold deposit (both standard and high-roller raffles).

**When mail-in items arrive:**

```bash
# Record one donation
python3 donations.py --db guildstats.db add @user 60000 "gold mats and writs" --recorded-by @yourname

# See what's pending in the current week
python3 donations.py --db guildstats.db list

# At the Tuesday rollover, promote last week's donations into the current raffle.
# Defaults: --week current, --to-raffle current. min_value default = 10,000.
python3 donations.py --db guildstats.db promote --to-raffle current
```

**Manual entries (FISHING / EVENT / ADJUSTMENT):**

```bash
# Quick form: 5 free tickets in the current standard raffle
python3 entry.py --db guildstats.db add @user --tickets 5 --descriptor FISHING

# Explicit ticket counts
python3 entry.py --db guildstats.db add @user --paid 25 --free 5 --hr 1 --descriptor EVENT

python3 entry.py --db guildstats.db list --raffle current
python3 entry.py --db guildstats.db remove 12345
```

**After the legacy Apps Script has drawn winners** (Friday after 8pm ET), pull
the final entry list, prizes, and winners back into the database:

```bash
python3 import_winners.py --db guildstats.db \
    --standard   "AKTT Standard Raffle.xlsx" \
    --highroller "AKTT High-Roller Raffle.xlsx"
```

The Apps Script renames the just-drawn `Current` tab to `MMDDYY` and creates a
fresh empty `Current`, so `import_winners.py` reads the latest `MMDDYY` tab by
default. Pass `--tab 042426` to import a specific tab. This step replaces any
manual entries and re-syncs prizes/winners; bank-deposit entries (which were
already created at live ingest time) are deduplicated by transaction_id.

## Schema overview

Phase 1 tables: `users`, `weeks`, `user_week_stats`, `bank_transactions`,
`ingest_runs`.

Phase 2 tables:

- `raffles` — one row per weekly drawing. Standard and high-roller for the
  same week are two separate rows.
- `raffle_entries` — one row per (user, raffle, source-occurrence) with
  explicit paid / free / high-roller ticket counts. Source is one of
  `bank_deposit`, `mail_donation`, `event`, `high_roller_qualifier`.
- `prizes` — tiered prize list per raffle. `category` is `main` or `mini`,
  `active_at_ticket_count` is the threshold for unlocking.
- `raffle_winners` — one row per prize awarded.
- `manual_donations` — mail-in items the officer has entered. The `is_promoted`
  flag and `promoted_to_raffle_id` track whether a donation has been rolled
  into a raffle yet.

## Useful queries

```sql
-- Lifetime sales per user (top 20)
SELECT u.account_name, SUM(s.sales) AS lifetime_sales
  FROM user_week_stats s JOIN users u ON u.id = s.user_id
 GROUP BY u.id ORDER BY lifetime_sales DESC LIMIT 20;

-- Raffle wins per user, all-time
SELECT u.account_name, COUNT(*) AS wins
  FROM raffle_winners w JOIN users u ON u.id = w.user_id
 GROUP BY u.id ORDER BY wins DESC LIMIT 20;

-- Tickets vs wins (rough win-rate proxy)
WITH tix AS (
    SELECT user_id, SUM(paid_tickets+free_tickets) AS total_tix
      FROM raffle_entries WHERE source != 'high_roller_qualifier'
     GROUP BY user_id
), wins AS (
    SELECT user_id, COUNT(*) AS wins FROM raffle_winners GROUP BY user_id
)
SELECT u.account_name, tix.total_tix, COALESCE(wins.wins,0) AS wins
  FROM users u JOIN tix ON tix.user_id = u.id
  LEFT JOIN wins ON wins.user_id = u.id
 WHERE tix.total_tix > 100
 ORDER BY total_tix DESC LIMIT 30;
```

## Web UI

A read-only FastAPI app lives in `web/`. Routes:

- `/`                          dashboard (current trader, top-5s, weekly goal)
- `/u/@account`                personal stats for one user (charts, history)
- `/rankings`                  leaderboards with period selector (4w / 13w / 52w / lifetime)
- `/raffles`                   index of every drawing
- `/raffles/{drawing_date}`    per-drawing detail (prizes, winners, top entrants)
- `/traders`                   trader-bid history (bid amounts hidden by design)
- `/trends`                    guild-wide trends over time, four Chart.js panels
- `/api/users/search?q=...`    HTMX dropdown lookup

Setup and deployment are documented in `automation/SETUP_WEB.md`. The dev
server runs with `uvicorn web.app:app --reload`; production goes behind
Caddy via the `aktt-web.service` systemd unit.

## What's NOT here yet

- Officer-only forms inside the web UI for donations / manual entries / trader bids
  (CLI scripts are still the way to do these)
- Database-side winner picking (the Friday Apps Script still drives drawings)
- Self-hosted Chart.js / HTMX (currently loaded from CDN; planned for offline reliability)
- Discord bot integration
