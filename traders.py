"""traders.py - CLI for guild_traders (weekly winning trader bid).

Subcommands:
  add        Record a winning bid for a trade week
  list       List recent or all trader weeks
  remove     Mark a week as 'no trader won' (deletes the row)

Examples:
  # Record this Tuesday's win
  python traders.py --db guildstats.db add \\
      --name "Zoe Frernile" --location "Gonfalon Bay" --bid 12500000

  # Specific week
  python traders.py --db guildstats.db add \\
      --name "Amirudda" --location Leyawiin --bid 6709709 --week 2026-05-05

  # See the last 12 weeks
  python traders.py --db guildstats.db list

  # See everything ever
  python traders.py --db guildstats.db list --all

  # Remove a week (we didn't win that week)
  python traders.py --db guildstats.db remove --week 2026-05-05
"""
from __future__ import annotations
import argparse
import sys
from datetime import datetime, timezone

from guildstats import (
    open_db, upsert_week, trade_week_ending,
    upsert_trader_bid, delete_trader_bid, TraderBid,
)


def _resolve_week_id(conn, week_arg: str | None) -> int:
    """None -> current trade week. 'YYYY-MM-DD' -> the trade week ending that date."""
    if week_arg is None:
        end = trade_week_ending(datetime.now(timezone.utc))
    else:
        d = datetime.strptime(week_arg, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end = d.replace(hour=19, minute=0, second=0, microsecond=0)
    return upsert_week(conn, end)


def cmd_add(conn, args):
    week_id = _resolve_week_id(conn, args.week)
    upsert_trader_bid(conn, TraderBid(
        week_id=week_id,
        trader_name=args.name,
        location=args.location,
        bid_amount=args.bid,
        notes=args.notes,
    ))
    week_label = args.week or "current"
    print(f"Recorded trader for week {week_label}: "
          f"{args.name} @ {args.location or '(no location)'}, bid={args.bid:,}")


def cmd_list(conn, args):
    if args.all_weeks:
        where = "1=1"
        params: tuple = ()
        order = "ORDER BY w.ending_date DESC"
        limit = ""
    else:
        where = "1=1"
        params = ()
        order = "ORDER BY w.ending_date DESC"
        limit = f"LIMIT {args.limit}"
    rows = conn.execute(
        f"""
        SELECT w.ending_date, gt.location, gt.trader_name, gt.bid_amount, gt.notes
          FROM guild_traders gt JOIN weeks w ON w.id = gt.week_id
         WHERE {where} {order} {limit}
        """, params
    ).fetchall()
    if not rows:
        print("(no trader weeks recorded)")
        return
    print(f"{'WEEK ENDING':<11}  {'LOCATION':<14}  {'TRADER':<22}  {'BID':>12}  NOTES")
    for r in rows:
        notes = (r["notes"] or "")[:40]
        loc = (r["location"] or "")[:14]
        print(f"{r['ending_date']:<11}  {loc:<14}  {r['trader_name']:<22}  "
              f"{r['bid_amount']:>12,}  {notes}")
    print(f"\n{len(rows)} row(s)")


def cmd_remove(conn, args):
    week_id = _resolve_week_id(conn, args.week)
    n = delete_trader_bid(conn, week_id)
    week_label = args.week or "current"
    if n == 0:
        print(f"No trader recorded for week {week_label} (nothing to remove)")
    else:
        print(f"Removed trader entry for week {week_label}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True)
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="Record a winning trader bid")
    a.add_argument("--name", required=True, help="NPC trader name (e.g. 'Zoe Frernile')")
    a.add_argument("--location", default=None,
                   help="Where the NPC is (e.g. 'Gonfalon Bay')")
    a.add_argument("--bid", type=int, required=True, help="Winning bid in gold")
    a.add_argument("--week", default=None,
                   help="Trade-week ending date YYYY-MM-DD; default = current")
    a.add_argument("--notes", default=None)
    a.set_defaults(func=cmd_add)

    l = sub.add_parser("list", help="Show recorded trader weeks")
    l.add_argument("--all", dest="all_weeks", action="store_true",
                   help="Show every recorded week (default: most recent N)")
    l.add_argument("--limit", type=int, default=12,
                   help="Limit when --all is not given (default 12)")
    l.set_defaults(func=cmd_list)

    r = sub.add_parser("remove",
                       help="Delete a trader entry (e.g. correct an error or mark 'no win')")
    r.add_argument("--week", default=None,
                   help="Trade-week ending date YYYY-MM-DD; default = current")
    r.set_defaults(func=cmd_remove)

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
