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
from datetime import datetime, timezone, timedelta
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




# --- placeholder routes for pages still under construction -------------------
# These keep the nav from 404'ing while phase 3.2+ is being built.

@app.get("/rankings", response_class=HTMLResponse)
@app.get("/raffles", response_class=HTMLResponse)
@app.get("/traders", response_class=HTMLResponse)
@app.get("/trends", response_class=HTMLResponse)
def coming_soon(request: Request):
    conn = get_db()
    try:
        ctx = site_context(conn)
        ctx["page_path"] = request.url.path
        return templates.TemplateResponse(request, "coming_soon.html", ctx)
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
