# AKTT Guild Stats — Command Reference

A reference for every command in the project, organized by what you're
trying to do. All examples assume `$AKTT_DB` is set in the shell environment
(typically `/home/akttuser/aktt-tracker/guildstats.db`) and that you've
activated the venv (`source ~/venv/bin/activate`).

## Quick index

| I want to...                                  | Command                              |
|-----------------------------------------------|--------------------------------------|
| Run the weekly Lua-file ingest                | `python3 ingest.py …`                |
| Apply schema additions to existing DB         | `python3 migrate.py --db $AKTT_DB`   |
| Compare DB to legacy donation_summary.csv     | `python3 validate.py …`              |
| Record a mail-in donation                     | `python3 donations.py … add …`       |
| Pull donations from the Google Sheet          | `python3 donations.py … import-from-sheet` |
| Promote donations into the current raffle     | `python3 donations.py … promote …`   |
| Add a manual raffle entry (FISHING/EVENT)     | `python3 entry.py … add …`           |
| Pull winners from a drawn raffle (local file) | `python3 import_winners.py …`        |
| Pull everything from Drive (raffle + winners) | `python3 sync_from_drive.py …`       |
| Record this week's trader bid                 | `python3 traders.py … add …`         |
| One-time historical backfills                 | `python3 backfill*.py …`             |

## Setup (one-time)

### LXC dependencies

```bash
apt update && apt install -y python3 python3-pip python3-venv sqlite3
python3 -m venv ~/venv && source ~/venv/bin/activate
pip install slpp openpyxl tzdata google-api-python-client google-auth
```

### Initialize a fresh database

```bash
sqlite3 $AKTT_DB < schema.sql
```

### Bring an existing database up to the latest schema

`schema.sql`'s `CREATE TABLE IF NOT EXISTS` statements are idempotent and
safe to re-run, but `ALTER TABLE ADD COLUMN` is not. After pulling new code:

```bash
python3 migrate.py --db $AKTT_DB --dry-run    # see what would change
python3 migrate.py --db $AKTT_DB              # apply
```

## Day-to-day (the things you'll do every week)

### Live ingest (after each in-game export of the Lua files)

```bash
# Mid-week snapshot
python3 ingest.py --db $AKTT_DB --mm MasterMerchant.lua --gbl GBLData.lua --week this

# Just after Tuesday rollover (final snapshot of the previous week)
python3 ingest.py --db $AKTT_DB --mm MasterMerchant.lua --gbl GBLData.lua --week last
```

If you've wired up Phase 2.5a (the Windows -> LXC SCP pipeline), this
happens automatically — `aktt_sync_windows.py` runs at the tail of your
`guild_stats.py` and the systemd `aktt-drop.path` unit fires the ingest.

### Mail-in donations

```bash
# Record one (run as items arrive)
python3 donations.py --db $AKTT_DB add @user 60000 "gold mats and writs" --recorded-by @yourname

# See what's pending in the current trade week
python3 donations.py --db $AKTT_DB list

# See pending across all weeks
python3 donations.py --db $AKTT_DB list --all

# See everything including already-promoted entries
python3 donations.py --db $AKTT_DB list --show-promoted

# Pull donations from the Google Sheet (latest dated tab; idempotent)
python3 donations.py --db $AKTT_DB import-from-sheet

# Pull a specific tab
python3 donations.py --db $AKTT_DB import-from-sheet --tab 042126

# At Tuesday rollover, promote last week's donations into the current raffle
python3 donations.py --db $AKTT_DB promote --to-raffle current
# (Mirrors legacy: combines mail-in + bank item donations, threshold 10k)
```

### Manual raffle entries (FISHING / EVENT / etc.)

```bash
# Quick form: 5 free tickets in the current standard raffle
python3 entry.py --db $AKTT_DB add @user --tickets 5 --descriptor FISHING

# Explicit ticket counts
python3 entry.py --db $AKTT_DB add @user --paid 25 --free 5 --hr 1 --descriptor EVENT

# List entries on the current raffle
python3 entry.py --db $AKTT_DB list --raffle current

# List on a specific raffle id
python3 entry.py --db $AKTT_DB list --raffle 47

# Recent raffles overview
python3 entry.py --db $AKTT_DB list

# Remove a misentered row
python3 entry.py --db $AKTT_DB remove 12345
```

### Trader bids

```bash
# Record this week's winning bid (defaults to current trade week)
python3 traders.py --db $AKTT_DB add --name "Zoe Frernile" --location "Gonfalon Bay" --bid 12500000

# Specific week
python3 traders.py --db $AKTT_DB add --name "Amirudda" --location Leyawiin --bid 6709709 --week 2026-05-05 --notes "first bid since update"

# See last 12 weeks (default)
python3 traders.py --db $AKTT_DB list

# See everything
python3 traders.py --db $AKTT_DB list --all

# Remove a week (e.g. if you didn't actually win, or to fix an entry)
python3 traders.py --db $AKTT_DB remove --week 2026-05-05
```

### Pulling raffle results from Drive after the Apps Script runs

```bash
# Pull both raffle workbooks' latest dated tabs, sync entries+prizes+winners
python3 sync_from_drive.py --db $AKTT_DB

# Just standard raffle
python3 sync_from_drive.py --db $AKTT_DB --only standard

# Specific tab in both workbooks
python3 sync_from_drive.py --db $AKTT_DB --tab 042426

# Keep the downloaded xlsx for inspection
python3 sync_from_drive.py --db $AKTT_DB --keep-xlsx
```

If you've wired up the systemd timer (`aktt-drive-sync.timer`), this runs
every 30 minutes automatically.

### Pulling raffle results from a local xlsx (no Drive)

```bash
python3 import_winners.py --db $AKTT_DB \
    --standard "AKTT Standard Raffle.xlsx" \
    --highroller "AKTT High-Roller Raffle.xlsx"

# Specific tab
python3 import_winners.py --db $AKTT_DB \
    --standard "AKTT Standard Raffle.xlsx" \
    --tab 042426
```

## One-time backfills

Order doesn't matter; each is idempotent.

```bash
# Phase 1: per-user weekly stats + raffle-deposit-derived bank txns
python3 backfill.py --db $AKTT_DB \
    --donations "AKTT Weekly Raw Data.xlsx" \
    --raffle    "AKTT Standard Raffle.xlsx" \
    --schema    schema.sql

# Phase 2: full raffle history (entries, prizes, winners) for both raffles
python3 backfill_raffle.py --db $AKTT_DB \
    --standard   "AKTT Standard Raffle.xlsx" \
    --highroller "AKTT High-Roller Raffle.xlsx" \
    --schema     schema.sql

# Phase 2.5c: trader-bid history (2022 - present)
python3 backfill_traders.py --db $AKTT_DB --xlsx "Trader Bids.xlsx"

# Specific years only
python3 backfill_traders.py --db $AKTT_DB --xlsx "Trader Bids.xlsx" --years 2024,2025,2026
```

## Validation and diagnostics

### Compare DB to legacy donation_summary.csv

```bash
python3 validate.py --db $AKTT_DB \
    --csv /path/to/donation_summary.csv --week this
```

Should report `0` mismatches across all 7 fields (rank, sales, taxes,
purchases, deposits, raffle, donations).

### Inspect what a Drive download produced

```bash
python3 sync_from_drive.py --db $AKTT_DB --keep-xlsx
# look at /tmp/aktt-sync-XXXXX/standard.xlsx etc.
python3 -c "
import openpyxl
wb = openpyxl.load_workbook('/tmp/aktt-sync-XXXXX/standard.xlsx', read_only=True)
print(wb.sheetnames[:10])
"
```

### Find oversized trader bids (likely data-entry errors)

```bash
sqlite3 $AKTT_DB "
SELECT w.ending_date, gt.location, gt.trader_name, gt.bid_amount
  FROM guild_traders gt JOIN weeks w ON w.id = gt.week_id
 WHERE gt.bid_amount > 50000000
 ORDER BY gt.bid_amount DESC;
"
```

### Check the audit log

```bash
sqlite3 $AKTT_DB "SELECT id, source, started_at, completed_at,
                          rows_inserted, rows_updated, rows_skipped, notes
                     FROM ingest_runs
                    ORDER BY id DESC LIMIT 10;"
```

### Confirm a week was processed

```bash
sqlite3 $AKTT_DB "
SELECT (SELECT COUNT(*) FROM user_week_stats WHERE week_id = w.id) AS user_rows,
       (SELECT COUNT(*) FROM bank_transactions WHERE week_id = w.id) AS bank_txns,
       (SELECT COUNT(*) FROM raffle_entries re JOIN raffles r ON r.id = re.raffle_id
         WHERE r.drawing_date BETWEEN date(w.ending_date) AND date(w.ending_date, '+7 days')
       ) AS raffle_entries
  FROM weeks w
 WHERE w.ending_date = '2026-04-21';
"
```

## Useful queries

### Lifetime sales per user (top 20)

```sql
SELECT u.account_name, SUM(s.sales) AS lifetime_sales
  FROM user_week_stats s JOIN users u ON u.id = s.user_id
 GROUP BY u.id ORDER BY lifetime_sales DESC LIMIT 20;
```

### Raffle wins per user, all-time

```sql
SELECT u.account_name, COUNT(*) AS wins
  FROM raffle_winners w JOIN users u ON u.id = w.user_id
 GROUP BY u.id ORDER BY wins DESC LIMIT 20;
```

### Tickets vs wins (rough win-rate proxy)

```sql
WITH tix AS (
    SELECT user_id, SUM(paid_tickets+free_tickets) AS total_tix
      FROM raffle_entries WHERE source != 'high_roller_qualifier'
     GROUP BY user_id
), wins AS (
    SELECT user_id, COUNT(*) AS wins FROM raffle_winners GROUP BY user_id
)
SELECT u.account_name, tix.total_tix, COALESCE(wins.wins, 0) AS wins
  FROM users u JOIN tix ON tix.user_id = u.id
  LEFT JOIN wins ON wins.user_id = u.id
 WHERE tix.total_tix > 100
 ORDER BY total_tix DESC LIMIT 30;
```

### Trader history per NPC

```sql
SELECT trader_name, location, COUNT(*) AS weeks_held,
       MIN(bid_amount) AS min_bid, MAX(bid_amount) AS max_bid,
       AVG(bid_amount) AS avg_bid
  FROM guild_traders
 GROUP BY trader_name, location
 ORDER BY weeks_held DESC;
```

### Weeks where the guild had no trader

```sql
SELECT w.ending_date FROM weeks w
LEFT JOIN guild_traders gt ON gt.week_id = w.id
WHERE gt.id IS NULL
ORDER BY w.ending_date DESC LIMIT 20;
```

### Weekly guild totals (recent 12)

```sql
SELECT w.ending_date,
       SUM(s.sales)           AS sales,
       SUM(s.taxes)           AS taxes,
       SUM(s.total_deposits)  AS deposits,
       SUM(s.total_raffle)    AS raffle,
       SUM(s.total_donations) AS donations
  FROM user_week_stats s JOIN weeks w ON w.id = s.week_id
 GROUP BY w.id ORDER BY w.ending_date DESC LIMIT 12;
```

## Environment variables that affect behavior

| Var                          | Used by                          | Purpose                                          |
|------------------------------|----------------------------------|--------------------------------------------------|
| `AKTT_DB`                    | most scripts                     | Path to the SQLite database                      |
| `AKTT_DRIVE_KEY`             | drive_sync, sync_from_drive      | Service-account JSON key                         |
| `AKTT_DRIVE_DONATIONS_ID`    | donations import-from-sheet      | Donations workbook spreadsheet ID                |
| `AKTT_DRIVE_STD_RAFFLE_ID`   | sync_from_drive                  | Standard raffle workbook spreadsheet ID          |
| `AKTT_DRIVE_HR_RAFFLE_ID`    | sync_from_drive                  | High-roller raffle workbook spreadsheet ID       |
| `AKTT_APP_DIR`               | process_drop.py (systemd)        | Where ingest.py lives on the LXC                 |
| `AKTT_INCOMING`              | process_drop.py                  | Drop directory the path unit watches             |
| `AKTT_PROCESSED`             | process_drop.py                  | Where successful ingest inputs are archived      |
| `AKTT_FAILED`                | process_drop.py                  | Where failed ingest inputs are quarantined       |

## File-by-file index

See README.md for the canonical file table. Quick reminders of which file
holds what:

| File                  | Purpose (one-line)                                      |
|-----------------------|----------------------------------------------------------|
| `schema.sql`          | All DDL; idempotent for `CREATE TABLE`                   |
| `migrate.py`          | Applies missing `ALTER TABLE` columns to existing DBs    |
| `guildstats.py`       | Shared helpers — db, week math, raffle/donation/trader   |
| `ingest.py`           | Weekly Lua-file ingest                                   |
| `backfill.py`         | One-time donations-workbook + raffle.csv import          |
| `backfill_raffle.py`  | One-time full raffle history                             |
| `backfill_traders.py` | One-time trader-bid history                              |
| `donations.py`        | Mail-in donations CLI (add/list/promote/import)          |
| `entry.py`            | Manual raffle entries CLI (add/list/remove)              |
| `traders.py`          | Trader-bid CLI (add/list/remove)                         |
| `import_winners.py`   | Sync raffle prizes/winners from local xlsx               |
| `sync_from_drive.py`  | Drive-side wrapper for import_winners                    |
| `drive_sync.py`       | Drive API helper (download spreadsheets as xlsx)         |
| `validate.py`         | DB-vs-CSV parity check                                   |
| `automation/*`        | systemd units, Windows scp helper, setup guides          |
