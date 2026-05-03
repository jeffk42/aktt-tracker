# AKTT Guild Stats - Design Notes

This document captures architectural reasoning and design decisions that
aren't obvious from reading the code or the README. Aimed at a future
maintainer (whether that's the original author returning months later, a
new chatbot session, or a guild-leadership successor).

## What this project is

A SQLite-backed parallel system that mirrors the legacy spreadsheet workflow
for AKTT (an Elder Scrolls Online trading guild). The legacy system uses
two Apps Scripts and several Google Sheets; this system runs on a
Proxmox LXC and accumulates the same data in a queryable database.

The phrase that drives all the architecture: **the spreadsheets remain
authoritative until the eventual web UI replaces them**. Everything in this
project either pulls FROM the spreadsheets (or from the same source data
the spreadsheets pull from) and mirrors them, or maintains parity by
processing the same inputs the spreadsheets do. We do NOT attempt to be
the source of truth for raffle drawings yet.

## Phasing

Five phases exist or are planned:

**Phase 1 — baseline ingest (delivered)**
- `schema.sql` core tables: `users`, `weeks`, `user_week_stats`,
  `bank_transactions`, `ingest_runs`
- `backfill.py` for the historical weekly raw data workbook (Feb 2022 onward)
- `ingest.py` reading `MasterMerchant.lua` + `GBLData.lua` weekly
- `validate.py` confirms numerical parity with `donation_summary.csv` from
  the legacy `guild_stats.py`

**Phase 2 — raffle modeling (delivered)**
- New tables: `raffles`, `raffle_entries`, `prizes`, `raffle_winners`,
  `manual_donations`
- `backfill_raffle.py` walks both raffle workbooks (standard + high roller),
  reconstructs ~362 raffles, ~18,920 entries, ~8,231 prizes, ~5,082 winners
  going back to Dec 2022 (standard) / Jul 2022 (HR)
- `donations.py` and `entry.py` CLIs for mail-in donations and event entries
- `import_winners.py` syncs prizes/winners from the raffle xlsx after a
  drawing
- `ingest.py` extended to create raffle_entries for raffle-eligible gold
  deposits during live ingest

**Phase 2.5a — file pipeline automation (delivered)**
- `automation/aktt_sync_windows.py` — drop-in for `guild_stats.py`,
  scp's lua files + manifest to LXC after each export
- `automation/process_drop.py` — LXC handler that runs ingest, archives or
  quarantines
- `automation/aktt-drop.{path,service}` — systemd units, event-driven via
  `PathChanged` on `manifest.json`

**Phase 2.5b — Drive integration (delivered)**
- `drive_sync.py` — Google Drive API wrapper (service-account auth)
- `donations.py import-from-sheet` — pulls donations workbook rows into
  `manual_donations` (source='sheet_import')
- `sync_from_drive.py` — wraps `import_winners.py` with Drive download
- `automation/aktt-drive-sync.{timer,service}` — periodic 30-minute sync

**Phase 3 — read-side web UI (delivered, except for officer-only forms)**
- FastAPI + Jinja2 + HTMX served by uvicorn behind Caddy. Read-only access
  to the SQLite database via the `guildstats.py` helpers; no writes from
  the web layer.
- Pages, in the order they shipped:
  - **3.1**: `/` dashboard (current trader, top-5 sellers/buyers/contributors,
    active member count, weekly goal) and `/u/@account` personal stats
    (full history, sortable per-week table, sales + contribution-composition
    charts).
  - **3.2**: `/rankings` leaderboards across six categories
    (sellers, contributors, buyers, item donors, raffle buyers, raffle wins),
    with a period selector — this week / 4w / 13w / 52w / lifetime — and
    a "Most Active" board on multi-week views.
  - **3.3**: `/traders` history with current-trader card, win-rate stats,
    location and NPC frequency leaderboards, and a sortable per-week table.
    Bid amounts deliberately hidden from the public site.
  - **3.4**: `/trends` guild-wide aggregates over time — four Chart.js
    panels (weekly sales, contribution composition stacked, active members,
    raffle tickets per drawing). The raffle chart uses a dual y-axis layout
    so the high-roller line isn't flattened by standard's much larger scale.
  - **3.5**: `/raffles` index with a featured "next/latest drawing" card
    and a click-anywhere-on-row table; per-drawing detail at
    `/raffles/{drawing_date}` showing standard + HR side by side with
    prize tables (winners + ticket numbers when drawn) and top-entrant
    leaderboards.
- HTMX powers a single dynamic bit (the user-search dropdown). Everything
  else is plain server-rendered HTML; no SPA, no build step.

**Phase 4+ — replacing the spreadsheets**
- Officer-only web forms inside the existing FastAPI app for donation entry,
  manual raffle entries, and trader-bid recording (replaces the CLI scripts
  in interactive use)
- Database-side winner picking with random.org (replaces Friday Apps Script)
- A way to define prize lists per raffle (replaces tiered-prize sheet logic)
- At this point the legacy spreadsheets retire.

## Key design decisions

### Why SQLite, not Postgres

For ~500 users × ~250 weeks of data, SQLite is genuinely the right tool.
Single writer (the ingest script), many readers (the future web app),
backup is a file copy. Postgres would mean another process to babysit,
network configuration, password management, pg_dump etc. The migration
path to Postgres is well-trodden if the project ever outgrows SQLite —
basically a `pgloader` invocation plus connection-string change. We're
nowhere near needing it.

### Why a Proxmox LXC, not a mini PC

User already has Proxmox infrastructure. LXCs share the host kernel
(tiny resource overhead), benefit from existing snapshot/backup
mechanisms, and require no additional hardware. A dedicated mini PC
makes sense only if you specifically want physical isolation, which
isn't a requirement here.

### Why parallel system instead of full cutover

The user is the sole maintainer of a working system. Cutting over to a
new system risks data loss and breaks the social trust of guild members
who currently see results in spreadsheets they're familiar with. A
parallel system lets the database accumulate clean data while the
spreadsheets stay live. When the web UI is ready, cutover is a
configuration change, not a data migration.

### The "stuck SALES block" in MasterMerchant.lua

The MM addon's saved-vars file has a `SALES` section that LOOKS like it
contains individual sale records. We initially thought this could give
us per-transaction sales detail. After investigation, the SALES block
contains exclusively orphaned records from late 2021 (a 10-day window),
nothing more recent. Whatever MM's current sales tracking looks like,
it's not exposed via this export. So we use the `EXPORT` block (which
gives us per-user weekly aggregates) and accept that "who bought from
whom" detail isn't recoverable.

### Trade week vs raffle week boundaries

Two distinct week concepts:
- **Trade week** ends Tuesday 19:00 UTC (ESO market rollover). Used for
  user_week_stats, bank_transactions.week_id, manual_donations.week_id.
- **Raffle week** ends Friday 20:00 ET (drawing time). Used for
  raffles.drawing_date.

A bank deposit's transaction occurred_at gets two different week
classifications: its trade week (used for stats aggregation) and the
raffle it counts toward (used for raffle_entries). Both are derived from
the same UTC timestamp via different functions (`trade_week_ending` vs
`raffle_drawing_date_for`).

### Idempotency strategy

Every ingest path is designed to be re-runnable safely:
- `bank_transactions` deduplicated by transaction_id (game-supplied UUID)
- `user_week_stats` UPSERTs by (user_id, week_id)
- `raffle_entries` from bank deposits dedupe by (raffle_id,
  source_transaction_id) where source_transaction_id is the txid
- `raffle_entries` from promoted donations use a SYNTHETIC
  source_transaction_id of the form `donation:week<n>:user<m>` so
  re-promoting the same week is also a no-op
- `prizes` UPSERT by (raffle_id, category, display_order)
- `raffle_winners` UNIQUE on prize_id (one winner per prize)
- `manual_donations` from sheet imports use a "delete unpromoted
  sheet_import rows for the week, then re-insert" strategy that mirrors
  whatever's currently in the sheet without touching CLI-entered
  donations or already-promoted ones

### Why prizes are per-raffle copies, not a template table

The Apps Script's pattern is "duplicate the Current tab" — each new
week's raffle inherits prizes from the previous week's prizes, with
per-week overrides. We mirror this: each `raffles` row gets its own
`prizes` rows. There's no shared "default prize list" table.

This is slightly more storage but vastly more honest: historical raffles
preserve their actual prize structure regardless of what current rules
look like. If guild leadership changes prize tiers, the change applies
to NEW raffles only.

### `is_backfilled` flag semantics

The flag distinguishes "this row was imported from the historical
spreadsheets during one-time backfill" from "this row was populated
during live operation." Useful for:
- Auditing: "did the live ingest produce numbers consistent with the
  spreadsheet aggregates?"
- Debugging: a backfilled row that suddenly changes value is suspicious
- Future cleanup: if we ever want to re-derive backfilled aggregates
  from raw transaction data, the flag tells us where to start

### `source` column on raffle_entries

Distinguishes WHERE an entry came from:
- `bank_deposit` — auto-created from a raffle-eligible gold deposit in
  bank_transactions
- `mail_donation` — promoted from manual_donations (combined with bank
  item donations during promote)
- `event` — manually added via `entry.py` (FISHING/EVENT/ADJUSTMENT)
- `high_roller_qualifier` — auto-created in HR raffle from the same
  bank deposit that produced a standard raffle bank_deposit entry

This vocabulary is set in stone in code, not user-facing.

### `source` column on manual_donations

- `cli` — entered via `donations.py add` (default)
- `sheet_import` — pulled from the auction donations spreadsheet by
  `donations.py import-from-sheet`

The sheet importer wipes its own previous rows for a week before
re-inserting (only unpromoted ones). This means officer edits in the
spreadsheet propagate into the database on each sync, but CLI-entered
donations and already-promoted donations are never disturbed.

### Combined-source promote logic

The legacy `updateDonations` Apps Script reads mail-in donations from
the auction sheet, ADDS them to the raw data sheet's "donations"
column (which already contains bank-item donations), and uses the
combined value to compute raffle ticket entries. Our database mirrors
this: `promote_donations_to_raffle()` sums BOTH `manual_donations.value`
AND `bank_transactions` `dep_item` values per user before applying the
10,000-gold threshold and computing tickets via FLOOR(combined/2000).

Bank item donations are not "consumed" by promotion — they remain in
bank_transactions. The synthetic source_transaction_id pattern provides
idempotency.

### Service account vs OAuth for Drive

Service account is the right choice for a non-interactive backend
process. OAuth would require a refresh token managed somehow and a
browser dance to bootstrap. Service accounts are scoped to specific
spreadsheets via Drive's share dialog, which is also more auditable
("which sheets does the bot have access to" = "which sheets have the
service-account email in their share list").

Tradeoff: a one-time Google Cloud project setup is required. About 15
minutes; documented in `automation/SETUP_DRIVE.md`.

### Event-driven vs polling for triggers

Phase 2.5a uses a systemd `PathChanged` path unit triggered by the
arrival of `manifest.json`. Zero CPU when idle, sub-second latency
when triggered. Phase 2.5b uses a systemd timer (every 30 min) for the
Drive sync because Drive doesn't push notifications to us — we have to
poll, but at low cadence.

A natural future improvement: add a webhook so the Apps Scripts can
notify the LXC after rollover/drawing, and switch the Drive sync to
event-driven. Not critical right now.

### Why server-rendered Jinja + HTMX, not an SPA

The web UI is a sequence of mostly-static pages served as plain HTML.
React or similar would mean a build step, a JS toolchain, client-side
routing — all carrying their own maintenance cost — for a site whose data
shape is "rows from a SQLite query." HTMX covers the one piece that does
need to be dynamic (the user-search dropdown) with three attributes on
the input element. No bundler, no `npm install`, no rebuild on deploy.

The pragmatic flip side: pages aren't "live" — you refresh to see new
ingest data. That's fine for a stats site that updates a few times a week.

### CDN for Chart.js and HTMX (deliberate, but logged)

Both libraries load from `cdnjs.cloudflare.com` / `unpkg.com` rather than
being served from `/static/`. This is a known trade-off: zero ops cost
today (no copying assets at deploy time, no version pinning beyond the URL)
in exchange for a runtime dependency on a third party and a small offline
risk. Self-hosting them is on the Phase 4 punch list and is purely a copy
job — no code changes required, just edit the two `<script src>` tags in
`base.html`.

### Period filtering: subquery, not Python list

`/rankings` (and `/trends`) accepts a `?period=current|4w|13w|52w|lifetime`
parameter. Each option resolves to a SQL fragment like
`s.week_id IN (SELECT id FROM weeks WHERE ending_date <= ? ORDER BY ending_date DESC LIMIT 4)`
rather than fetching week IDs in Python and binding them as
`IN (?, ?, ?, ?)`. The subquery approach keeps the prepared statement
shape constant regardless of period, which means no string-mangling per
request and no risk of an unbounded `IN (...)` list.

The `period` parameter is validated against a hard-coded tuple before any
SQL composition, so the f-string interpolation of the WHERE clause is
safe. Don't loosen that validation.

### Trade-week vs drawing-date period semantics

Most rankings filter by trade-week (`s.week_id IN ...`). But raffle wins
filter by drawing date — drawings are Friday-aligned while trade weeks end
Tuesday, so a "trade-week ID" boundary doesn't cleanly capture drawings.
For the period selector on `/rankings`, raffle wins use a calendar-day
cutoff (`drawing_date >= cur_end - 7*N days`) instead. Don't try to unify
these — the two boundary systems are real and on purpose.

### The dual-axis raffle chart on /trends

Standard raffles sell thousands of tickets per drawing; high-roller sells
single or double digits. On a shared y-axis the HR line gets compressed
to invisibility against the bottom of the chart. The trends page puts
each series on its own y-axis (standard left in gold, HR right in red),
with axis labels and tick colors matched to the line color so the visual
association is immediate. The right-axis grid is suppressed so the two
grids don't double up over the chart area.

If a future maintainer adds a third series (say, total raffle gold-in
per drawing), don't add a third axis — drop one or use bars on a separate
panel.

### Hidden trader bid amounts

`guild_traders.bid_amount` is recorded by the CLI and visible in the DB,
but the public `/traders` page never reads or displays it. The reason is
guild policy: bids are officer-only information. The page shows date,
trader name, location, and notes, plus aggregate stats (weeks won, win
rate, distinct locations, distinct NPCs) computed from those non-sensitive
columns.

If the policy changes, the column is already there and adding a "Bid"
table column is a one-line change. Don't drop the column from the schema.

### Partial current week excluded from /trends

The currently in-progress trade week is excluded from per-week aggregate
charts on `/trends` (`WHERE w.ending_date < cur_end`). Without this,
every chart shows a misleading dip at the right edge that "fills in" as
the week progresses. The current-week active-member count IS shown on a
headline card so live activity isn't invisible — but the time-series
charts are completed-weeks-only.

Same principle for the raffle-tickets chart, but expressed as
`WHERE ra.status = 'drawn'` since raffles transition through `open` →
`closed` → `drawn`. An open raffle has partial entries.

### sortable.js is shared, and the column-index bug

The same tiny client-side sortable lives in `web/static/sortable.js` and
is included once from `base.html`. The earlier inline copy in
`personal.html` had a latent bug: it captured the global `forEach` index
across all `.sortable th` elements on the page, which meant on a page
with two sortable tables the second table would sort by the wrong column.
The extracted version computes column index per-table from the actual
`<tr>` children. Pages with one sortable table didn't notice the bug;
the raffles page would have.

### Whole-row click navigation on /raffles

The raffle index table has every row linking to a per-drawing detail
page. The first cell already contains an `<a>` (so keyboard / screen
readers navigate properly), but mouse users expect to click anywhere in
the row. A small JS handler on `tr.row-link` provides that, with a guard
so clicks on actual `<a>` elements aren't double-handled.

### The path-mapping quirk in the chat sandbox

Inside the chat sandbox, the bash tool's view of the workspace can lag
or partially fail to enumerate files under `web/` — files that exist
and are readable via the file tools sometimes appear as empty
directories or missing files in `ls`. The Edit/Write tools work
correctly because they bypass the path mapping. The workaround during
development was to copy file contents to the sandbox `outputs/` directory
for `py_compile` and `node --check` smoke tests, then run from there.
The real LXC has no such issue.

## Known limitations and future cleanup

### `start_number` / `end_number` on live raffle_entries are NULL

When `ingest.py` creates raffle_entries from bank deposits, it doesn't
yet compute the running ticket-range numbers. These get filled in later
by `import_winners.py` when it sees the post-draw spreadsheet (where
the spreadsheet's running formulas have computed them). When the
database becomes authoritative for drawings, we'll need a "finalize"
step that assigns these numbers in occurred_at order before the draw.

### High-roller "two 25k deposits don't grant a ticket" weak spot

The user explicitly accepted this: HR tickets are computed per
deposit, so two separate 25,000-gold deposits earn 0 HR tickets even
though their combined value would earn 1. The legacy script has the
same behavior. Fixing it would require tracking running totals per
user, which adds complexity. Not worth it unless someone complains.

### Mini-prize description grouping

Mini raffle prizes in the spreadsheet sometimes have one description
spanning multiple winner rows ("Wheel of Amazingness... 10 winners
receive..."). Our backfill captures each row as a separate prize,
which loses the grouping. Each row has a winner; the description
appears only on the first row of a group. Not a data-integrity issue,
just a presentation issue. The web UI can re-group by detecting empty
description rows.

### `ingest_runs` "rows_inserted" overcounts

The counter increments on every UPSERT (whether INSERT or UPDATE), so
the audit log slightly overstates "new" rows. Cosmetic; not a real
problem.

### No rule-versioning for ticket math

Free-ticket bonus rules changed once historically (Buy 5 Get 1 → Buy
25 Get 5). Backfilled raffle_entries store the realized ticket counts
as recorded in the spreadsheet, so historical accuracy is preserved.
But there's no record of WHEN the rule changed. If you ever need to
query "what rule was in force on date X," you'd have to inspect the
ticket counts of entries from that period and infer it. A
`rule_versions` table would solve this; not built because not yet
needed.

## Things future-me should know

### File-tool truncation issue (sandbox-only)

Inside the chat sandbox, the Edit/Write tools occasionally truncated
files at certain byte boundaries when writing large content. The
workaround was to use bash heredocs. This is NOT a problem on the
actual LXC; it was a quirk of the sandbox path-mapping layer. Don't
let stale memory of "the Write tool drops bytes" bias future decisions.

### tzdata on minimal Linux

`ZoneInfo('US/Eastern')` fails on minimal Debian/Ubuntu installs that
lack system tzdata. The fix is `pip install tzdata` (the PyPI
package). Documented in the README and in the LXC setup guide. There's
a cosmetic fallback in `raffle_deadline_utc` that approximates EDT
when ZoneInfo is None, but it doesn't handle DST transitions. Always
install tzdata in production.

### slpp dependency

The Lua parser library `slpp` is required by `ingest.py`. The chat
sandbox couldn't install it (no PyPI access), so we used a regex-based
shim (`_smoketest_slpp_shim.py`) for sandbox testing. Production uses
real slpp. The shim file is safe to delete on the LXC.

### Existing `guild_stats.py` (the legacy script)

The user's original `guild_stats.py` (mentioned in early conversation
but not part of this codebase) continues to run on Windows and
generate `donation_summary.csv` etc. It's separate from this project.
The Phase 2.5a `aktt_sync_windows.py` is designed to be added as a
post-step to that script.

### Trade Week vs Raffle Week is on purpose

Don't try to "simplify" by collapsing them into one week concept. The
ESO market mechanic uses Tuesday boundaries; the guild's social
ritual uses Friday boundaries. Both are real and both matter.

## Quick orientation guide

If you (future me / future Jeff / a successor) are coming back to this
project after a long break:

1. Read README.md for the workflow and file overview
2. Read this file for the why-decisions
3. Read schema.sql top-to-bottom — it's the most authoritative single
   description of the data model
4. Skim guildstats.py module docstring + the `--- section ---` comment
   headers; that file is the shared library
5. For the Apps Script -> DB sync flow, the order is:
   `aktt_sync_windows.py` -> SCP -> `manifest.json` arrives ->
   systemd `aktt-drop.path` fires -> `process_drop.py` runs ->
   `ingest.py`. For Drive: systemd `aktt-drive-sync.timer` fires every
   30 min -> calls `donations.py import-from-sheet` and
   `sync_from_drive.py`.
6. To extend functionality, the model is: add helpers in `guildstats.py`,
   add a new top-level CLI script, optionally add a systemd unit
