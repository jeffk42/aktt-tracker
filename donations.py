"""donations.py - CLI for entering and managing mail-in auction donations.

Subcommands:
  add        Record a donation: user, value, optional description
  list       List donations (defaults to current trade week, unpromoted only)
  promote    Convert unpromoted donations from a trade week into raffle_entries
             on the next standard raffle (mirrors the Tuesday-rollover Apps Script)

Examples:
  python donations.py --db guildstats.db add @user 60000 "gold mats"
  python donations.py --db guildstats.db list
  python donations.py --db guildstats.db list --week 2026-04-21 --all
  python donations.py --db guildstats.db promote --to-raffle current
  python donations.py --db guildstats.db promote --week 2026-04-21 --to-raffle 47
"""
from __future__ import annotations
import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from guildstats import (
    open_db, upsert_user, upsert_week, trade_week_ending,
    add_manual_donation, promote_donations_to_raffle,
    find_open_raffle, raffle_drawing_date_for, upsert_raffle,
    DONATION_MIN_VALUE,
)


def _resolve_week_id(conn, week_arg: str | None) -> int:
    """Resolve --week. None means 'current trade week'. A YYYY-MM-DD value
    means 'the trade week ending that date'. Always upserts so it's safe."""
    if week_arg is None:
        end = trade_week_ending(datetime.now(timezone.utc))
    else:
        end_date = datetime.strptime(week_arg, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        # Snap to canonical Tuesday 19:00 UTC
        end = end_date.replace(hour=19, minute=0, second=0, microsecond=0)
    return upsert_week(conn, end)


def _resolve_raffle_id(conn, raffle_arg: str) -> int:
    """Resolve --to-raffle. 'current' means the next open standard raffle
    (creating one if necessary). A numeric id means that exact raffle."""
    if raffle_arg.lower() == "current":
        rid = find_open_raffle(conn, raffle_type="standard")
        if rid is not None:
            return rid
        # No open raffle — create one for the upcoming Friday
        drawing = raffle_drawing_date_for(datetime.now(timezone.utc))
        return upsert_raffle(conn, raffle_type="standard",
                             drawing_date=drawing, status="open")
    return int(raffle_arg)


def cmd_add(conn, args):
    user_id = upsert_user(conn, args.user)
    week_id = _resolve_week_id(conn, args.week)
    received_at = (datetime.strptime(args.received_at, "%Y-%m-%d %H:%M:%S")
                   .replace(tzinfo=timezone.utc)) if args.received_at else None
    don_id = add_manual_donation(conn, user_id=user_id, week_id=week_id,
                                 value=args.value, description=args.description,
                                 received_at=received_at,
                                 recorded_by=args.recorded_by)
    print(f"Added donation #{don_id}: {args.user} {args.value:,} gold "
          f"({args.description or 'no description'})")


def cmd_list(conn, args):
    if args.all_weeks:
        where = "1=1"
        params: tuple = ()
    else:
        week_id = _resolve_week_id(conn, args.week)
        where = "md.week_id = ?"
        params = (week_id,)
    promo_clause = "" if args.show_promoted else "AND md.is_promoted = 0"
    rows = conn.execute(
        f"""
        SELECT md.id, u.account_name, w.ending_date, md.value, md.description,
               md.is_promoted, md.promoted_to_raffle_id, md.received_at, md.recorded_by
          FROM manual_donations md
          JOIN users u ON u.id = md.user_id
          JOIN weeks w ON w.id = md.week_id
         WHERE {where} {promo_clause}
         ORDER BY md.received_at DESC, md.id DESC
        """, params).fetchall()
    if not rows:
        print("(no donations matched)")
        return
    print(f"{'ID':>5}  {'USER':<22} {'WEEK ENDING':<11}  {'VALUE':>10}  PROMOTED  DESCRIPTION")
    for r in rows:
        promo = f"r#{r['promoted_to_raffle_id']}" if r["is_promoted"] else "no"
        desc = (r["description"] or "")[:40]
        print(f"{r['id']:>5}  {r['account_name']:<22} {r['ending_date']:<11}  "
              f"{r['value']:>10,}  {promo:<8}  {desc}")
    total = sum(r["value"] for r in rows)
    print(f"\n{len(rows)} rows, total {total:,} gold")


def cmd_promote(conn, args):
    week_id = _resolve_week_id(conn, args.week)
    raffle_id = _resolve_raffle_id(conn, args.to_raffle)
    raffle_row = conn.execute(
        "SELECT raffle_type, drawing_date FROM raffles WHERE id=?",
        (raffle_id,)).fetchone()
    if not raffle_row:
        print(f"ERROR: raffle id {raffle_id} not found", file=sys.stderr)
        sys.exit(2)
    print(f"Promoting unpromoted donations from week_id={week_id} "
          f"into {raffle_row['raffle_type']} raffle id={raffle_id} "
          f"(drawing {raffle_row['drawing_date']})")
    promoted = promote_donations_to_raffle(conn, week_id=week_id,
                                           raffle_id=raffle_id,
                                           min_value=args.min_value)
    print(f"Promoted {promoted} user(s).")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True)
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="Record one mail-in donation")
    a.add_argument("user", help="Account name e.g. @jeffk42")
    a.add_argument("value", type=int, help="Estimated value in gold")
    a.add_argument("description", nargs="?", default=None,
                   help="Free-text description of items donated")
    a.add_argument("--week", default=None,
                   help="Trade-week ending date YYYY-MM-DD; default = current week")
    a.add_argument("--received-at", default=None,
                   help="When the items were received: 'YYYY-MM-DD HH:MM:SS' UTC; default = now")
    a.add_argument("--recorded-by", default=None,
                   help="Officer name for audit trail")
    a.set_defaults(func=cmd_add)

    l = sub.add_parser("list", help="List donations")
    l.add_argument("--week", default=None,
                   help="Trade-week ending date YYYY-MM-DD; default = current")
    l.add_argument("--all", dest="all_weeks", action="store_true",
                   help="Show donations from every week, ignoring --week")
    l.add_argument("--show-promoted", action="store_true",
                   help="Include donations that have already been promoted")
    l.set_defaults(func=cmd_list)

    p = sub.add_parser("promote", help="Promote a week's donations into a raffle")
    p.add_argument("--week", default=None,
                   help="Trade-week ending date YYYY-MM-DD; default = current")
    p.add_argument("--to-raffle", default="current",
                   help="Raffle id, or 'current' for the next open standard raffle")
    p.add_argument("--min-value", type=int, default=DONATION_MIN_VALUE,
                   help="Per-user minimum aggregated value to promote")
    p.set_defaults(func=cmd_promote)

    args = ap.parse_args()
    conn = open_db(args.db)
    conn.execute("BEGIN")
    try:
        args.func(conn, args)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


if __name__ == "__main__":
    main()
