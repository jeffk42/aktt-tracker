"""AKTT Guild Stats web app.

FastAPI + Jinja2 + HTMX. Server-rendered HTML, no SPA. Reads from the
SQLite database via guildstats.py helpers (no writes - this is a read-only
public site).

Routes:
    GET  /                      Dashboard (current trader, top 5s, etc.)
    GET  /u/{account}           Personal stats for one user
    GET  /api/users/search      HTMX dropdown lookup (returns HTML fragment)

Run locally for development:
    uvicorn web.app:app --reload --host 127.0.0.1 --port 8000

Run in production via systemd (see automation/aktt-web.service):
    uvicorn web.app:app --host 127.0.0.1 --port 8000

Configure with env vars:
    AKTT_DB                 path to SQLite DB (default: ./guildstats.db)
"""
from __future__ import annotations
import os
import sqlite3
import sys
from datetime import datetime, timezone, timedelta, date as _date
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# Add parent dir to path so we can import guildstats
APP_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = APP_ROOT.parent
sys.path.insert(0, str(PROJECT_ROOT))

from guildstats import open_db, trade_week_ending  # noqa: E402

DB_PATH = os.environ.get("AKTT_DB", str(PROJECT_ROOT / "guildstats.db"))

app = FastAPI(title="AKTT Guild Stats", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(APP_ROOT / "static")), name="static")
templates = Jinja2Templates(directory=str(APP_ROOT / "templates"))


# --- Jinja filters -----------------------------------------------------------

def _format_gold(value) -> str:
    """123456 -> '123,456g'; None -> '—'."""
    if value is None or value == "":
        return "—"
    try:
        return f"{int(value):,}g"
    except (TypeError, ValueError):
        return str(value)


def _format_int(value) -> str:
    if value is None or value == "":
        return "—"
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return str(value)


def _format_pct(value) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value) * 100:.1f}%"
    except (TypeError, ValueError):
        return str(value)


def _format_date(value) -> str:
    """'2026-04-21' -> 'Apr 21, 2026'."""
    if not value:
        return "—"
    try:
        d = datetime.strptime(str(value)[:10], "%Y-%m-%d")
        return d.strftime("%b %-d, %Y")
    except ValueError:
        return str(value)


templates.env.filters["gold"] = _format_gold
templates.env.filters["intcomma"] = _format_int
templates.env.filters["pct"] = _format_pct
templates.env.filters["nicedate"] = _format_date


# --- shared helpers ----------------------------------------------------------

def get_db():
    """Open a fresh connection per request. SQLite handles concurrent reads
    fine; FastAPI workers are single-threaded per request so we don't need a
    pool."""
    return open_db(DB_PATH)


def get_setting(conn, key: str, default: Optional[str] = None) -> Optional[str]:
    row = conn.execute(
        "SELECT value FROM guild_settings WHERE key = ?", (key,)
    ).fetchone()
    return row["value"] if row else default


def site_context(conn) -> dict:
    """Common variables every template wants: site title, current trade week,
    time to rollover."""
    now = datetime.now(timezone.utc)
    cur_week_end = trade_week_ending(now)
    remaining = cur_week_end - now
    return {
        "site_title": get_setting(conn, "site_title", "AKTT Guild Stats"),
        "now_utc": now,
        "current_week_ending": cur_week_end,
        "time_remaining": _format_timedelta(remaining),
        "weekly_goal": int(get_setting(conn, "weekly_contribution_goal", "0") or 0),
    }


def _format_timedelta(td: timedelta) -> str:
    """timedelta -> 'Nd Nh Nm'."""
    total = int(td.total_seconds())
    if total < 0:
        return "rollover imminent"
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    mins, _ = divmod(rem, 60)
    return f"{days}d {hours}h {mins}m"


# --- routes ------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    """Dashboard. Placeholder for phase 3.2 — for now, links to Personal Stats."""
    conn = get_db()
    try:
        ctx = site_context(conn)
        # Top 5 sellers / donors / buyers / raffle-buyers for the current week,
        # for a quick demo of the dashboard
        cur_week_end = ctx["current_week_ending"]
        wk_row = conn.execute(
            "SELECT id FROM weeks WHERE ending_date = ?",
            (cur_week_end.date().isoformat(),)
        ).fetchone()
        if wk_row:
            wid = wk_row["id"]
            top_sellers = conn.execute("""
                SELECT u.account_name, s.sales FROM user_week_stats s
                JOIN users u ON u.id = s.user_id
                WHERE s.week_id=? AND u.excluded=0 AND s.sales > 0
                ORDER BY s.sales DESC LIMIT 5
            """, (wid,)).fetchall()
            top_donors = conn.execute("""
                SELECT u.account_name,
                       (s.total_deposits + s.total_donations + s.total_raffle) AS contrib
                  FROM user_week_stats s
                  JOIN users u ON u.id = s.user_id
                 WHERE s.week_id=? AND u.excluded=0
                   AND (s.total_deposits + s.total_donations + s.total_raffle) > 0
                 ORDER BY contrib DESC LIMIT 5
            """, (wid,)).fetchall()
            top_buyers = conn.execute("""
                SELECT u.account_name, s.purchases FROM user_week_stats s
                JOIN users u ON u.id = s.user_id
                WHERE s.week_id=? AND u.excluded=0 AND s.purchases > 0
                ORDER BY s.purchases DESC LIMIT 5
            """, (wid,)).fetchall()
        else:
            top_sellers = top_donors = top_buyers = []

        # Current trader (no bid amount; user explicitly excluded that)
        trader = conn.execute("""
            SELECT gt.trader_name, gt.location FROM guild_traders gt
            JOIN weeks w ON w.id = gt.week_id
            WHERE w.ending_date = ?
        """, (cur_week_end.date().isoformat(),)).fetchone()

        # Total active members (have a row this week)
        active_members = conn.execute("""
            SELECT COUNT(*) AS n FROM user_week_stats s
            JOIN users u ON u.id = s.user_id
            WHERE s.week_id = (SELECT id FROM weeks WHERE ending_date = ?)
              AND u.excluded = 0
              AND (s.sales + s.taxes + s.purchases + s.total_deposits +
                   s.total_donations + s.total_raffle) > 0
        """, (cur_week_end.date().isoformat(),)).fetchone()
        total_members = conn.execute("""
            SELECT COUNT(*) AS n FROM user_week_stats s
            JOIN users u ON u.id = s.user_id
            WHERE s.week_id = (SELECT id FROM weeks WHERE ending_date = ?)
              AND u.excluded = 0
        """, (cur_week_end.date().isoformat(),)).fetchone()

        ctx.update(
            top_sellers=top_sellers, top_donors=top_donors, top_buyers=top_buyers,
            trader=trader,
            active_members=active_members["n"] if active_members else 0,
            total_members=total_members["n"] if total_members else 0,
        )
        return templates.TemplateResponse(request, "home.html", ctx)
    finally:
        conn.close()


@app.get("/u/{account_name}", response_class=HTMLResponse)
def personal_stats(request: Request, account_name: str,
                   limit: int = Query(default=104, ge=4, le=9999)):
    """Personal stats page. URL is the account name with leading @ for
    bookmarking. limit caps the per-week table; default ~2 years."""
    conn = get_db()
    try:
        ctx = site_context(conn)

        # Allow links without the @ (e.g. /u/jeffk42) by adding it.
        if not account_name.startswith("@"):
            return RedirectResponse(url=f"/u/@{account_name}", status_code=302)

        user = conn.execute(
            "SELECT id, account_name FROM users "
            "WHERE account_name = ? COLLATE NOCASE AND excluded = 0",
            (account_name,)
        ).fetchone()
        if not user:
            raise HTTPException(404, f"User {account_name} not found")

        # Lifetime totals
        lifetime = conn.execute("""
            SELECT COALESCE(SUM(sales), 0)           AS sales,
                   COALESCE(SUM(taxes), 0)           AS taxes,
                   COALESCE(SUM(purchases), 0)       AS purchases,
                   COALESCE(SUM(total_deposits), 0)  AS deposits,
                   COALESCE(SUM(total_raffle), 0)    AS raffle,
                   COALESCE(SUM(total_donations), 0) AS donations,
                   COUNT(*)                           AS weeks_active
              FROM user_week_stats WHERE user_id = ?
        """, (user["id"],)).fetchone()

        contrib = (lifetime["taxes"] + lifetime["deposits"] +
                   lifetime["raffle"] + lifetime["donations"])
        lifetime_dict = dict(lifetime)
        lifetime_dict["contribution"] = contrib

        # Per-week history (newest first). Ranks are computed via window
        # functions across all non-excluded users for each week the user
        # appears in. Limited subquery so we don't rank-sort all 102k rows.
        weeks = conn.execute("""
            WITH user_weeks AS (
              SELECT week_id FROM user_week_stats WHERE user_id = ?
            ),
            ranked AS (
              SELECT
                s.user_id, s.week_id,
                RANK() OVER (PARTITION BY s.week_id ORDER BY s.sales DESC)
                  AS sales_rank,
                RANK() OVER (PARTITION BY s.week_id
                             ORDER BY (s.taxes + s.total_deposits +
                                       s.total_raffle + s.total_donations) DESC)
                  AS contrib_rank,
                RANK() OVER (PARTITION BY s.week_id ORDER BY s.purchases DESC)
                  AS purchase_rank
              FROM user_week_stats s
              JOIN users u ON u.id = s.user_id
              WHERE s.week_id IN (SELECT week_id FROM user_weeks)
                AND u.excluded = 0
            )
            SELECT w.ending_date,
                   s.sales, s.taxes, s.purchases,
                   s.total_deposits AS deposits,
                   s.total_raffle   AS raffle,
                   s.total_donations AS donations,
                   (s.taxes + s.total_deposits + s.total_raffle + s.total_donations)
                       AS contribution,
                   gt.trader_name, gt.location AS trader_location,
                   r.sales_rank, r.contrib_rank, r.purchase_rank
              FROM user_week_stats s
              JOIN weeks w ON w.id = s.week_id
              LEFT JOIN guild_traders gt ON gt.week_id = w.id
              JOIN ranked r ON r.user_id = s.user_id AND r.week_id = s.week_id
             WHERE s.user_id = ?
             ORDER BY w.ending_date DESC
             LIMIT ?
        """, (user["id"], user["id"], limit)).fetchall()

        # For chart: reverse chronological back to chronological
        chart_data = list(reversed([
            {
                "ending_date": w["ending_date"],
                "sales": w["sales"],
                "taxes": w["taxes"],
                "deposits": w["deposits"],
                "raffle": w["raffle"],
                "donations": w["donations"],
                "contribution": w["contribution"],
            } for w in weeks
        ]))

        # Current week stats card with computed ranks
        cur = conn.execute("""
            WITH ranked AS (
              SELECT
                s.user_id,
                RANK() OVER (ORDER BY s.sales DESC) AS sales_rank,
                RANK() OVER (ORDER BY (s.taxes + s.total_deposits +
                                       s.total_raffle + s.total_donations) DESC)
                  AS contrib_rank,
                RANK() OVER (ORDER BY s.purchases DESC) AS purchase_rank
              FROM user_week_stats s
              JOIN users u ON u.id = s.user_id
              JOIN weeks w ON w.id = s.week_id
              WHERE w.ending_date = ? AND u.excluded = 0
            )
            SELECT s.sales, s.taxes, s.purchases,
                   s.total_deposits AS deposits,
                   s.total_raffle AS raffle,
                   s.total_donations AS donations,
                   r.sales_rank, r.contrib_rank, r.purchase_rank
              FROM user_week_stats s
              JOIN weeks w ON w.id = s.week_id
              JOIN ranked r ON r.user_id = s.user_id
             WHERE s.user_id = ? AND w.ending_date = ?
        """, (ctx["current_week_ending"].date().isoformat(),
              user["id"],
              ctx["current_week_ending"].date().isoformat())).fetchone()

        ctx.update(
            user=user, lifetime=lifetime_dict, weeks=weeks,
            current_week=cur, chart_data=chart_data, limit=limit,
        )
        return templates.TemplateResponse(request, "personal.html", ctx)
    finally:
        conn.close()


# --- rankings helpers --------------------------------------------------------

ALLOWED_PERIODS = ("current", "4w", "13w", "52w", "lifetime")
_PERIOD_LABELS = {
    "current": "This Week",
    "4w": "Last 4 Weeks",
    "13w": "Last 13 Weeks",
    "52w": "Last 52 Weeks",
    "lifetime": "Lifetime",
}
_PERIOD_WEEKS = {"4w": 4, "13w": 13, "52w": 52}
# Day-budget for filtering raffle drawings that don't align cleanly to trade
# weeks. Roughly N weeks of drawings.
_PERIOD_DAYS = {"current": 7, "4w": 28, "13w": 91, "52w": 364}


def _uws_period_clause(period: str, cur_end: str):
    """Return (sql_fragment, args) that filters user_week_stats `s` by period.
    Fragment begins with ' AND' if non-empty so it can be appended to a WHERE."""
    if period == "current":
        return (" AND s.week_id = (SELECT id FROM weeks WHERE ending_date = ?)",
                (cur_end,))
    if period in _PERIOD_WEEKS:
        return (" AND s.week_id IN (SELECT id FROM weeks "
                "WHERE ending_date <= ? ORDER BY ending_date DESC LIMIT ?)",
                (cur_end, _PERIOD_WEEKS[period]))
    return "", ()


def _drawing_period_clause(period: str, cur_end: str):
    """Return (sql_fragment, args) that filters raffles `ra` by drawing_date for
    the period. Day-based, since drawings sit at Friday boundaries while trade
    weeks roll Tuesday."""
    if period == "lifetime":
        return "", ()
    days = _PERIOD_DAYS[period]
    cutoff = (datetime.fromisoformat(cur_end) - timedelta(days=days)).date().isoformat()
    return " AND ra.drawing_date >= ? AND ra.drawing_date <= ?", (cutoff, cur_end)


@app.get("/rankings", response_class=HTMLResponse)
def rankings(request: Request, period: str = Query(default="lifetime")):
    """Leaderboards across the guild for a chosen period."""
    if period not in ALLOWED_PERIODS:
        period = "lifetime"
    conn = get_db()
    try:
        ctx = site_context(conn)
        cur_end = ctx["current_week_ending"].date().isoformat()
        wclause, wargs = _uws_period_clause(period, cur_end)
        dclause, dargs = _drawing_period_clause(period, cur_end)

        def _board(metric_sql: str, limit: int = 25):
            sql = f"""
                SELECT u.account_name, ({metric_sql}) AS value
                  FROM user_week_stats s
                  JOIN users u ON u.id = s.user_id
                 WHERE u.excluded = 0 {wclause}
                 GROUP BY u.id
                HAVING value > 0
                 ORDER BY value DESC, u.account_name ASC
                 LIMIT {int(limit)}
            """
            return conn.execute(sql, wargs).fetchall()

        top_sellers      = _board("SUM(s.sales)")
        top_contributors = _board(
            "SUM(s.taxes + s.total_deposits + s.total_raffle + s.total_donations)"
        )
        top_buyers       = _board("SUM(s.purchases)")
        top_donors       = _board("SUM(s.total_donations)")
        top_raffle_spend = _board("SUM(s.total_raffle)")

        # Most active: weeks-with-activity in the period. Meaningless for a
        # single-week view, so only computed for multi-week periods.
        most_active = []
        if period != "current":
            active_sql = f"""
                SELECT u.account_name, COUNT(*) AS value
                  FROM user_week_stats s
                  JOIN users u ON u.id = s.user_id
                 WHERE u.excluded = 0 {wclause}
                   AND (s.sales + s.taxes + s.purchases + s.total_deposits +
                        s.total_donations + s.total_raffle) > 0
                 GROUP BY u.id
                HAVING value > 0
                 ORDER BY value DESC, u.account_name ASC
                 LIMIT 25
            """
            most_active = conn.execute(active_sql, wargs).fetchall()

        # Most raffle wins (count of prizes won). Joined raffles->prizes->winners.
        wins_sql = f"""
            SELECT u.account_name, COUNT(*) AS value
              FROM raffle_winners rw
              JOIN prizes p  ON p.id  = rw.prize_id
              JOIN raffles ra ON ra.id = p.raffle_id
              JOIN users u   ON u.id  = rw.user_id
             WHERE u.excluded = 0 {dclause}
             GROUP BY u.id
            HAVING value > 0
             ORDER BY value DESC, u.account_name ASC
             LIMIT 25
        """
        most_wins = conn.execute(wins_sql, dargs).fetchall()

        ctx.update(
            period=period,
            period_label=_PERIOD_LABELS[period],
            top_sellers=top_sellers,
            top_contributors=top_contributors,
            top_buyers=top_buyers,
            top_donors=top_donors,
            top_raffle_spend=top_raffle_spend,
            most_active=most_active,
            most_wins=most_wins,
        )
        return templates.TemplateResponse(request, "rankings.html", ctx)
    finally:
        conn.close()


@app.get("/traders", response_class=HTMLResponse)
def traders(request: Request, limit: int = Query(default=26, ge=4, le=9999)):
    """Guild trader history. Bid amounts are intentionally NOT exposed
    (officer-only info per design). Shows current trader, recent history,
    most-frequent locations and NPCs, plus aggregate counts."""
    conn = get_db()
    try:
        ctx = site_context(conn)
        cur_end = ctx["current_week_ending"].date().isoformat()

        current = conn.execute("""
            SELECT gt.trader_name, gt.location, gt.notes, w.ending_date
              FROM guild_traders gt JOIN weeks w ON w.id = gt.week_id
             WHERE w.ending_date = ?
        """, (cur_end,)).fetchone()

        # Most-recent trader BEFORE the current week, for context.
        previous = conn.execute("""
            SELECT gt.trader_name, gt.location, w.ending_date
              FROM guild_traders gt JOIN weeks w ON w.id = gt.week_id
             WHERE w.ending_date < ?
             ORDER BY w.ending_date DESC LIMIT 1
        """, (cur_end,)).fetchone()

        history = conn.execute("""
            SELECT w.ending_date, gt.trader_name, gt.location, gt.notes
              FROM guild_traders gt JOIN weeks w ON w.id = gt.week_id
             ORDER BY w.ending_date DESC
             LIMIT ?
        """, (limit,)).fetchall()

        agg = conn.execute("""
            SELECT COUNT(*)                       AS total_won,
                   COUNT(DISTINCT location)       AS distinct_locations,
                   COUNT(DISTINCT trader_name)    AS distinct_traders,
                   MIN(w.ending_date)             AS first_week,
                   MAX(w.ending_date)             AS latest_week
              FROM guild_traders gt JOIN weeks w ON w.id = gt.week_id
        """).fetchone()
        agg_dict = dict(agg) if agg else {
            "total_won": 0, "distinct_locations": 0, "distinct_traders": 0,
            "first_week": None, "latest_week": None,
        }

        # Win rate: weeks-won / weeks-since-first-recorded-win.
        win_rate = None
        weeks_in_range = None
        if agg_dict["first_week"]:
            first_d = _date.fromisoformat(agg_dict["first_week"])
            cur_d   = _date.fromisoformat(cur_end)
            weeks_in_range = ((cur_d - first_d).days // 7) + 1
            if weeks_in_range > 0:
                win_rate = agg_dict["total_won"] / weeks_in_range

        top_locations = conn.execute("""
            SELECT location, COUNT(*) AS weeks
              FROM guild_traders
             WHERE location IS NOT NULL AND TRIM(location) != ''
             GROUP BY location
             ORDER BY weeks DESC, location ASC
             LIMIT 15
        """).fetchall()

        top_traders = conn.execute("""
            SELECT trader_name, COUNT(*) AS weeks
              FROM guild_traders
             WHERE trader_name IS NOT NULL AND TRIM(trader_name) != ''
             GROUP BY trader_name
             ORDER BY weeks DESC, trader_name ASC
             LIMIT 15
        """).fetchall()

        ctx.update(
            current=current, previous=previous,
            history=history, limit=limit,
            agg=agg_dict, win_rate=win_rate, weeks_in_range=weeks_in_range,
            top_locations=top_locations, top_traders=top_traders,
        )
        return templates.TemplateResponse(request, "traders.html", ctx)
    finally:
        conn.close()


@app.get("/trends", response_class=HTMLResponse)
def trends(request: Request, limit: int = Query(default=104, ge=8, le=9999)):
    """Guild-wide activity over time. Aggregates per completed trade week,
    plus per-raffle ticket counts. The currently in-progress trade week is
    excluded from the per-week series so partial data doesn't pull the right
    end of every chart down."""
    conn = get_db()
    try:
        ctx = site_context(conn)
        cur_end = ctx["current_week_ending"].date().isoformat()
        # Cutoff for raffle-side window (calendar-day approximation of N weeks).
        cutoff = (_date.fromisoformat(cur_end) - timedelta(weeks=limit)).isoformat()

        # Per-week aggregates excluding the in-progress current week.
        weekly = conn.execute("""
            WITH visible AS (
              SELECT id FROM weeks
               WHERE ending_date < ?
               ORDER BY ending_date DESC LIMIT ?
            )
            SELECT w.ending_date,
                   SUM(s.sales)            AS sales,
                   SUM(s.taxes)            AS taxes,
                   SUM(s.purchases)        AS purchases,
                   SUM(s.total_deposits)   AS deposits,
                   SUM(s.total_raffle)     AS raffle,
                   SUM(s.total_donations)  AS donations,
                   COUNT(DISTINCT CASE
                      WHEN (s.sales + s.taxes + s.purchases + s.total_deposits +
                            s.total_donations + s.total_raffle) > 0
                      THEN s.user_id END)  AS active_members
              FROM user_week_stats s
              JOIN users u ON u.id = s.user_id
              JOIN weeks w ON w.id = s.week_id
             WHERE u.excluded = 0
               AND w.id IN (SELECT id FROM visible)
             GROUP BY w.id
             ORDER BY w.ending_date ASC
        """, (cur_end, limit)).fetchall()

        # 52-week trailing totals for the headline cards.
        trailing_row = conn.execute("""
            SELECT COALESCE(SUM(s.sales), 0)            AS total_sales,
                   COALESCE(SUM(s.taxes + s.total_deposits +
                                s.total_raffle + s.total_donations), 0)
                                                       AS total_contribution,
                   COALESCE(SUM(s.purchases), 0)       AS total_purchases,
                   COALESCE(SUM(s.total_donations), 0) AS total_donations
              FROM user_week_stats s
              JOIN users u ON u.id = s.user_id
             WHERE u.excluded = 0
               AND s.week_id IN (
                  SELECT id FROM weeks
                   WHERE ending_date < ?
                   ORDER BY ending_date DESC LIMIT 52
               )
        """, (cur_end,)).fetchone()
        trailing = dict(trailing_row) if trailing_row else {
            "total_sales": 0, "total_contribution": 0,
            "total_purchases": 0, "total_donations": 0,
        }

        # Live (partial-week) active member count.
        cur_active_row = conn.execute("""
            SELECT COUNT(*) AS n
              FROM user_week_stats s
              JOIN users u ON u.id = s.user_id
             WHERE s.week_id = (SELECT id FROM weeks WHERE ending_date = ?)
               AND u.excluded = 0
               AND (s.sales + s.taxes + s.purchases + s.total_deposits +
                    s.total_donations + s.total_raffle) > 0
        """, (cur_end,)).fetchone()
        cur_active = cur_active_row["n"] if cur_active_row else 0

        # Per-raffle ticket counts. Drawn raffles only — open ones have partial
        # data and would draw misleading spikes at the right edge.
        raffles = conn.execute("""
            SELECT ra.drawing_date,
                   ra.raffle_type,
                   COALESCE(SUM(re.paid_tickets + re.free_tickets +
                                re.high_roller_tickets), 0) AS tickets,
                   COUNT(DISTINCT re.user_id) AS entrants
              FROM raffles ra
              LEFT JOIN raffle_entries re ON re.raffle_id = ra.id
             WHERE ra.status = 'drawn'
               AND ra.drawing_date >= ?
             GROUP BY ra.id
             ORDER BY ra.drawing_date ASC
        """, (cutoff,)).fetchall()

        weekly_chart = [{
            "ending_date": r["ending_date"],
            "sales":     r["sales"]     or 0,
            "taxes":     r["taxes"]     or 0,
            "purchases": r["purchases"] or 0,
            "deposits":  r["deposits"]  or 0,
            "raffle":    r["raffle"]    or 0,
            "donations": r["donations"] or 0,
            "active_members": r["active_members"] or 0,
        } for r in weekly]

        std_raffle_data = [{
            "date":     r["drawing_date"],
            "tickets":  r["tickets"],
            "entrants": r["entrants"],
        } for r in raffles if r["raffle_type"] == "standard"]
        hr_raffle_data = [{
            "date":     r["drawing_date"],
            "tickets":  r["tickets"],
            "entrants": r["entrants"],
        } for r in raffles if r["raffle_type"] == "high_roller"]

        # Total weeks available, for the "show all" link math.
        total_weeks_row = conn.execute(
            "SELECT COUNT(*) AS n FROM weeks WHERE ending_date < ?",
            (cur_end,)
        ).fetchone()
        total_weeks = total_weeks_row["n"] if total_weeks_row else 0

        ctx.update(
            limit=limit,
            weekly_chart=weekly_chart,
            std_raffle_data=std_raffle_data,
            hr_raffle_data=hr_raffle_data,
            trailing_52w=trailing,
            cur_active=cur_active,
            total_weeks=total_weeks,
        )
        return templates.TemplateResponse(request, "trends.html", ctx)
    finally:
        conn.close()


def _format_remaining(deadline_str: str, now_utc: datetime) -> str:
    """Parse 'YYYY-MM-DD HH:MM:SS' (UTC) and return 'Nd Nh Nm remaining' or
    'closed' if past."""
    try:
        d = datetime.fromisoformat(deadline_str.replace(" ", "T"))
    except (ValueError, AttributeError):
        return ""
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    delta = d - now_utc
    if delta.total_seconds() <= 0:
        return "closed"
    return _format_timedelta(delta) + " remaining"


@app.get("/raffles", response_class=HTMLResponse)
def raffles_index(request: Request,
                  limit: int = Query(default=26, ge=4, le=9999)):
    """Index of raffles: featured (next/latest) drawing on top, then a table
    of recent drawings linking through to per-drawing detail pages."""
    conn = get_db()
    try:
        ctx = site_context(conn)
        now = ctx["now_utc"]

        # Total drawing dates ever, for the show-all link.
        total_row = conn.execute(
            "SELECT COUNT(DISTINCT drawing_date) AS n FROM raffles"
        ).fetchone()
        total_drawings = total_row["n"] if total_row else 0

        # The N most recent drawing DATES (each date has up to 2 raffles:
        # standard + high_roller). Aggregate ticket/entrant counts per raffle
        # are joined in via subqueries.
        rows = conn.execute("""
            WITH recent_dates AS (
              SELECT DISTINCT drawing_date FROM raffles
               ORDER BY drawing_date DESC LIMIT ?
            )
            SELECT ra.id, ra.raffle_type, ra.drawing_date, ra.status,
                   ra.deadline_at, ra.total_tickets_sold,
                   COALESCE(t.tickets, 0)        AS tickets,
                   COALESCE(t.entrants, 0)       AS entrants,
                   COALESCE(p.prizes_count, 0)   AS prizes_count,
                   COALESCE(w.winners_count, 0)  AS winners_count
              FROM raffles ra
              LEFT JOIN (
                SELECT raffle_id,
                       SUM(paid_tickets + free_tickets + high_roller_tickets) AS tickets,
                       COUNT(DISTINCT user_id) AS entrants
                  FROM raffle_entries GROUP BY raffle_id
              ) t ON t.raffle_id = ra.id
              LEFT JOIN (
                SELECT raffle_id, COUNT(*) AS prizes_count
                  FROM prizes GROUP BY raffle_id
              ) p ON p.raffle_id = ra.id
              LEFT JOIN (
                SELECT pr.raffle_id, COUNT(*) AS winners_count
                  FROM raffle_winners rw
                  JOIN prizes pr ON pr.id = rw.prize_id
                 GROUP BY pr.raffle_id
              ) w ON w.raffle_id = ra.id
             WHERE ra.drawing_date IN (SELECT drawing_date FROM recent_dates)
             ORDER BY ra.drawing_date DESC, ra.raffle_type ASC
        """, (limit,)).fetchall()

        # Group by drawing_date so each row of the index renders with both
        # raffles together.
        drawings: list[dict] = []
        date_idx: dict[str, dict] = {}
        for r in rows:
            d = r["drawing_date"]
            if d not in date_idx:
                bucket = {
                    "drawing_date": d,
                    "standard": None,
                    "high_roller": None,
                    "any_open": False,
                }
                date_idx[d] = bucket
                drawings.append(bucket)
            bucket = date_idx[d]
            cell = dict(r)
            cell["remaining"] = (_format_remaining(r["deadline_at"], now)
                                 if r["status"] != "drawn" else "")
            bucket[r["raffle_type"]] = cell
            if r["status"] != "drawn":
                bucket["any_open"] = True

        featured = drawings[0] if drawings else None

        ctx.update(
            limit=limit,
            drawings=drawings,
            featured=featured,
            total_drawings=total_drawings,
        )
        return templates.TemplateResponse(request, "raffles.html", ctx)
    finally:
        conn.close()


@app.get("/raffles/{drawing_date}", response_class=HTMLResponse)
def raffle_detail(request: Request, drawing_date: str):
    """Per-drawing detail: standard + HR raffles for one Friday, side by side."""
    # Validate the date format to keep the SQL parameter clean.
    try:
        _date.fromisoformat(drawing_date)
    except ValueError:
        raise HTTPException(404, f"Bad drawing date: {drawing_date}")

    conn = get_db()
    try:
        ctx = site_context(conn)
        now = ctx["now_utc"]

        raffles = conn.execute("""
            SELECT id, raffle_type, drawing_date, deadline_at, status,
                   total_tickets_sold, max_ticket_number, is_backfilled
              FROM raffles
             WHERE drawing_date = ?
             ORDER BY raffle_type ASC
        """, (drawing_date,)).fetchall()
        if not raffles:
            raise HTTPException(404, f"No raffle on {drawing_date}")

        sections = []
        for ra in raffles:
            stats = conn.execute("""
                SELECT COALESCE(SUM(paid_tickets + free_tickets +
                                    high_roller_tickets), 0) AS tickets,
                       COUNT(DISTINCT user_id)               AS entrants,
                       COUNT(*)                              AS entries,
                       COALESCE(SUM(gold_amount), 0)         AS gold_in
                  FROM raffle_entries WHERE raffle_id = ?
            """, (ra["id"],)).fetchone()

            # Prizes joined to winner. Main category before mini, then by
            # display_order.
            prizes = conn.execute("""
                SELECT p.id, p.category, p.display_order,
                       p.active_at_ticket_count,
                       p.prize_type, p.gold_amount, p.item_description, p.notes,
                       u.account_name        AS winner_name,
                       rw.winning_ticket_number,
                       rw.drawn_at
                  FROM prizes p
                  LEFT JOIN raffle_winners rw ON rw.prize_id = p.id
                  LEFT JOIN users u ON u.id = rw.user_id
                 WHERE p.raffle_id = ?
                 ORDER BY (CASE p.category WHEN 'main' THEN 0 ELSE 1 END),
                          p.display_order ASC
            """, (ra["id"],)).fetchall()

            # Top entrants by total tickets in this raffle
            top_entrants = conn.execute("""
                SELECT u.account_name,
                       SUM(re.paid_tickets + re.free_tickets +
                           re.high_roller_tickets) AS tickets,
                       SUM(re.paid_tickets) AS paid,
                       SUM(re.free_tickets) AS free,
                       SUM(re.high_roller_tickets) AS hr
                  FROM raffle_entries re
                  JOIN users u ON u.id = re.user_id
                 WHERE re.raffle_id = ? AND u.excluded = 0
                 GROUP BY u.id
                HAVING tickets > 0
                 ORDER BY tickets DESC, u.account_name ASC
                 LIMIT 25
            """, (ra["id"],)).fetchall()

            ra_dict = dict(ra)
            ra_dict["remaining"] = (_format_remaining(ra["deadline_at"], now)
                                    if ra["status"] != "drawn" else "")
            sections.append({
                "raffle": ra_dict,
                "stats": dict(stats) if stats else {
                    "tickets": 0, "entrants": 0, "entries": 0, "gold_in": 0
                },
                "prizes": prizes,
                "top_entrants": top_entrants,
            })

        # Adjacent dates for prev/next navigation
        nav = conn.execute("""
            SELECT
              (SELECT MAX(drawing_date) FROM raffles WHERE drawing_date < ?) AS prev_date,
              (SELECT MIN(drawing_date) FROM raffles WHERE drawing_date > ?) AS next_date
        """, (drawing_date, drawing_date)).fetchone()

        ctx.update(
            drawing_date=drawing_date,
            sections=sections,
            prev_date=nav["prev_date"] if nav else None,
            next_date=nav["next_date"] if nav else None,
        )
        return templates.TemplateResponse(request, "raffle_detail.html", ctx)
    finally:
        conn.close()


@app.get("/api/users/search", response_class=HTMLResponse)
def user_search(request: Request, q: str = Query(default="", min_length=0)):
    """HTMX-friendly dropdown: returns an HTML fragment of matching users."""
    conn = get_db()
    try:
        q_norm = q.strip()
        if not q_norm:
            return HTMLResponse("")
        rows = conn.execute("""
            SELECT account_name FROM users
             WHERE excluded = 0 AND account_name LIKE ? COLLATE NOCASE
             ORDER BY account_name LIMIT 12
        """, (f"%{q_norm}%",)).fetchall()
        return templates.TemplateResponse(
            request, "_user_search_results.html",
            {"results": [r["account_name"] for r in rows]}
        )
    finally:
        conn.close()




# --- error handlers ----------------------------------------------------------

@app.exception_handler(404)
def not_found(request: Request, exc):
    """404 needs the full site_context so base.html can render its nav, footer,
    etc. Without it, Jinja raises UndefinedError on {{ site_title }} and the
    response becomes a 500."""
    conn = get_db()
    try:
        ctx = site_context(conn)
        ctx["detail"] = str(exc.detail) if hasattr(exc, "detail") else "Not found"
        return templates.TemplateResponse(request, "404.html", ctx, status_code=404)
    finally:
        conn.close()
