"""sync_from_drive.py - Pull the latest raffle workbooks from Google Drive
and run import_winners against them.

Suitable for cron/systemd timer triggers. Idempotent: re-running on the same
state is a no-op (entries are deduped by transaction_id; prizes/winners
upsert by (raffle_id, category, display_order) / prize_id).

Configuration via env vars (see drive_sync.py for the full list):
  AKTT_DRIVE_KEY              service-account JSON key path
  AKTT_DRIVE_STD_RAFFLE_ID    standard raffle spreadsheet id
  AKTT_DRIVE_HR_RAFFLE_ID     high-roller raffle spreadsheet id
  AKTT_DB                     path to guildstats.db (default: ./guildstats.db)

Usage:
    python sync_from_drive.py                  # both workbooks, latest tab
    python sync_from_drive.py --only standard
    python sync_from_drive.py --tab 042426     # specific tab
"""
from __future__ import annotations
import argparse
import os
import sys
import tempfile
from pathlib import Path

from drive_sync import export_sheet_as_xlsx, resolve_id, get_service
import import_winners
from guildstats import open_db, ingest_run


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=os.environ.get("AKTT_DB", "guildstats.db"))
    ap.add_argument("--key", default=None, help="Override AKTT_DRIVE_KEY")
    ap.add_argument("--only", choices=("standard", "high_roller"), default=None,
                    help="Only sync one workbook (default: both)")
    ap.add_argument("--tab", default=None,
                    help="Specific MMDDYY tab; default = latest dated tab")
    ap.add_argument("--keep-xlsx", action="store_true",
                    help="Don't delete the downloaded xlsx files after import")
    args = ap.parse_args()

    service = get_service(args.key)

    targets = []
    if args.only != "high_roller":
        targets.append(("standard",   resolve_id("standard")))
    if args.only != "standard":
        targets.append(("high_roller", resolve_id("high_roller")))

    conn = open_db(args.db)
    tmpdir = Path(tempfile.mkdtemp(prefix="aktt-sync-"))
    try:
        downloads = {}
        for kind, spreadsheet_id in targets:
            out = tmpdir / f"{kind}.xlsx"
            print(f"Downloading {kind} ({spreadsheet_id}) -> {out}")
            export_sheet_as_xlsx(spreadsheet_id, out, service=service)
            downloads[kind] = str(out)

        with ingest_run(conn, "sync_from_drive",
                        workbook_filename="(drive)") as counts:
            conn.execute("BEGIN")
            try:
                if "standard" in downloads:
                    r = import_winners.import_workbook(
                        conn, downloads["standard"], "standard", args.tab)
                    counts["rows_inserted"] += r["entries"] + r["prizes"] + r["winners"]
                if "high_roller" in downloads:
                    r = import_winners.import_workbook(
                        conn, downloads["high_roller"], "high_roller", args.tab)
                    counts["rows_inserted"] += r["entries"] + r["prizes"] + r["winners"]
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
    finally:
        if not args.keep_xlsx:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)
    print("\nDone.")


if __name__ == "__main__":
    main()
