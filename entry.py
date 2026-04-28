"""entry.py - General-purpose CLI for adding manual raffle entries.

Use this for tickets earned outside the standard gold/donation pipelines:
event prizes, fishing contests, manual adjustments, etc.

Subcommands:
  add        Insert one raffle_entries row (with explicit ticket counts)
  list       List entries for a raffle (or recent raffles)
  remove     Delete an entry by id

Examples:
  # Award @user 5 free tickets in the current standard raffle, marked as FISHING
  python entry.py --db guildstats.db add @user --tickets 5 --descriptor FISHING

  # Award explicit paid + free + HR ticket counts
  python entry.py --db guildstats.db add @user --paid 25 --free 5 --hr 1 \
      --descriptor EVENT --raffle 47

  # List all entries on the current open standard raffle
  python entry.py --db guildstats.db list --raffle current

  # Remove an entry
  python entry.py --db guildstats.db remove 123
"""
from __future__ import annotations
import argparse
import sys
from datetime import datetime, timezone

from guildstats import (
    open_db, upsert_user, find_open_raffle, raffle_drawing_date_for, upsert_raffle,
    insert_raffle_entry, recompute_raffle_totals, RaffleEntry,
)


def _resolve_raffle(conn, raffle_arg: str, raffle_type: str = "standard") -> int:
    if raffle_arg.lower() == "current":
        rid = find_open_raffle(conn, raffle_type=raffle_type)
        if rid is not None:
            return rid
        drawing = raffle_drawing_date_for(datetime.now(timezone.utc))
        return upsert_raffle(conn, raffle_type=raffle_type,
                             drawing_date=drawing, status="open")
    return int(raffle_arg)


def cmd_add(conn, args):
    # Resolve ticket counts: --tickets is shorthand for --free
    paid = args.paid or 0
    free = args.free if args.free is not None else (args.tickets or 0)
    hr = args.hr or 0
    if paid + free + hr <= 0:
        print("ERROR: must specify at least one ticket count "
              "(--tickets, --paid, --free, or --hr)", file=sys.stderr)
        sys.exit(2)

    raffle_id = _resolve_raffle(conn, args.raffle, raffle_type=args.raffle_type)
    user_id = upsert_user(conn, args.user)
    occurred = (datetime.strptime(args.occurred_at, "%Y-%m-%d %H:%M:%S")
                .replace(tzinfo=timezone.utc)) if args.occurred_at else datetime.now(timezone.utc)

    e = RaffleEntry(
        raffle_id=raffle_id, user_id=user_id, source=args.source,
        occurred_at=occurred,
        gold_amount=args.gold_amount,
        paid_tickets=paid, free_tickets=free, high_roller_tickets=hr,
        descriptor=args.descriptor.upper() if args.descriptor else None,
    )
    eid = insert_raffle_entry(conn, e)
    recompute_raffle_totals(conn, raffle_id)
    print(f"Added entry #{eid} on raffle {raffle_id}: "
          f"{args.user}  paid={paid} free={free} hr={hr}  "
          f"descriptor={args.descriptor or '-'}")


def cmd_list(conn, args):
    if args.raffle is None:
        # Show last 5 raffles
        rids = [r["id"] for r in conn.execute(
            "SELECT id FROM raffles ORDER BY drawing_date DESC LIMIT 5").fetchall()]
    else:
        rid = _resolve_raffle(conn, args.raffle, raffle_type=args.raffle_type)
        rids = [rid]

    for rid in rids:
        r = conn.execute(
            "SELECT raffle_type, drawing_date, total_tickets_sold, max_ticket_number "
            "FROM raffles WHERE id=?", (rid,)).fetchone()
        if not r:
            continue
        print(f"\n=== raffle id={rid}  {r['raffle_type']}  drawing {r['drawing_date']}  "
              f"sold={r['total_tickets_sold']}  max={r['max_ticket_number']} ===")
        rows = conn.execute(
            """
            SELECT re.id, u.account_name, re.source, re.descriptor,
                   re.gold_amount, re.paid_tickets, re.free_tickets,
                   re.high_roller_tickets, re.start_number, re.end_number,
                   re.occurred_at
              FROM raffle_entries re
              JOIN users u ON u.id = re.user_id
             WHERE re.raffle_id = ?
             ORDER BY re.start_number, re.id
            """, (rid,)).fetchall()
        if not rows:
            print("  (no entries)")
            continue
        print(f"  {'ID':>5}  {'USER':<22} {'SRC':<22} {'GOLD':>10} "
              f"{'PAID':>5} {'FREE':>5} {'HR':>3}  {'TICKETS':<13}  DESC")
        for r in rows:
            tickets = (f"{r['start_number']}-{r['end_number']}"
                       if r['start_number'] is not None else "")
            print(f"  {r['id']:>5}  {r['account_name']:<22} {r['source']:<22} "
                  f"{(r['gold_amount'] or 0):>10,} {r['paid_tickets']:>5} "
                  f"{r['free_tickets']:>5} {r['high_roller_tickets']:>3}  "
                  f"{tickets:<13}  {r['descriptor'] or ''}")


def cmd_remove(conn, args):
    row = conn.execute("SELECT raffle_id FROM raffle_entries WHERE id=?",
                       (args.entry_id,)).fetchone()
    if not row:
        print(f"ERROR: entry id {args.entry_id} not found", file=sys.stderr)
        sys.exit(2)
    raffle_id = row["raffle_id"]
    conn.execute("DELETE FROM raffle_entries WHERE id=?", (args.entry_id,))
    recompute_raffle_totals(conn, raffle_id)
    print(f"Deleted entry #{args.entry_id} from raffle {raffle_id}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True)
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="Add a raffle entry")
    a.add_argument("user", help="Account name e.g. @jeffk42")
    a.add_argument("--tickets", type=int, default=None,
                   help="Free tickets to grant (shortcut for --free)")
    a.add_argument("--paid", type=int, default=0)
    a.add_argument("--free", type=int, default=None)
    a.add_argument("--hr", type=int, default=0,
                   help="High-roller tickets (rare for manual entries)")
    a.add_argument("--descriptor", default=None,
                   help="Tag like FISHING, EVENT, ADJUSTMENT")
    a.add_argument("--source", default="event",
                   choices=("event", "mail_donation", "bank_deposit"),
                   help="Entry source classification (default 'event')")
    a.add_argument("--raffle", default="current",
                   help="Raffle id, or 'current' for the open one (default)")
    a.add_argument("--raffle-type", default="standard",
                   choices=("standard", "high_roller"))
    a.add_argument("--gold-amount", type=int, default=None,
                   help="Optional gold amount for record-keeping")
    a.add_argument("--occurred-at", default=None,
                   help="'YYYY-MM-DD HH:MM:SS' UTC; default = now")
    a.set_defaults(func=cmd_add)

    l = sub.add_parser("list", help="List raffle entries")
    l.add_argument("--raffle", default=None,
                   help="Raffle id, 'current', or omit for last 5 raffles")
    l.add_argument("--raffle-type", default="standard",
                   choices=("standard", "high_roller"))
    l.set_defaults(func=cmd_list)

    r = sub.add_parser("remove", help="Delete a raffle entry by id")
    r.add_argument("entry_id", type=int)
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
