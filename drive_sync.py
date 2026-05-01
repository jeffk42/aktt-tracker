"""drive_sync.py - Download Google Sheets as .xlsx via a service account.

Used by sync_from_drive.py and donations.py's import-from-sheet command. Wraps
google-api-python-client + google-auth so callers don't have to think about
the Drive API directly.

Configuration (env vars or passed-in args):
  AKTT_DRIVE_KEY              path to service-account JSON key file
  AKTT_DRIVE_DONATIONS_ID     spreadsheet id of the Auction Donations workbook
  AKTT_DRIVE_STD_RAFFLE_ID    spreadsheet id of the AKTT Standard Raffle workbook
  AKTT_DRIVE_HR_RAFFLE_ID     spreadsheet id of the AKTT High-Roller Raffle workbook

Spreadsheet IDs are the part of the Drive URL after /d/ and before the next /.

Required pip packages on the LXC:
    pip install google-api-python-client google-auth

Each spreadsheet must be shared with the service account's email (Viewer is
sufficient).
"""
from __future__ import annotations
import os
import sys
from pathlib import Path

try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseDownload
except ImportError:
    print("ERROR: google-api-python-client + google-auth required:\n"
          "  pip install google-api-python-client google-auth", file=sys.stderr)
    raise

# Read-only scope is enough; we never write to the user's Drive.
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def get_service(key_path: str | None = None):
    """Build a Drive v3 service object using the service-account key."""
    key_path = key_path or os.environ.get("AKTT_DRIVE_KEY")
    if not key_path:
        raise SystemExit("Service account key not specified. "
                         "Pass --key or set AKTT_DRIVE_KEY.")
    if not Path(key_path).is_file():
        raise SystemExit(f"Service account key file not found: {key_path}")
    creds = service_account.Credentials.from_service_account_file(
        key_path, scopes=SCOPES)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def export_sheet_as_xlsx(spreadsheet_id: str, out_path: str | Path,
                         service=None, key_path: str | None = None) -> Path:
    """Download `spreadsheet_id` as an .xlsx file at `out_path`.
    Returns the path. Raises SystemExit on failure with a friendly message."""
    if service is None:
        service = get_service(key_path)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        request = service.files().export_media(fileId=spreadsheet_id,
                                               mimeType=XLSX_MIME)
        with open(out_path, "wb") as fh:
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                _status, done = downloader.next_chunk()
    except Exception as e:
        raise SystemExit(
            f"Drive export failed for {spreadsheet_id}: {e}\n"
            f"Check that the spreadsheet is shared with the service account "
            f"email (look in the JSON key for 'client_email')."
        )

    if out_path.stat().st_size < 1024:
        # An empty/tiny file usually means a permission or ID problem.
        raise SystemExit(f"Downloaded file looks suspiciously small "
                         f"({out_path.stat().st_size} bytes); "
                         f"verify the spreadsheet ID and sharing.")
    return out_path


def resolve_id(kind: str, override: str | None = None) -> str:
    """kind in {'donations', 'standard', 'high_roller'}. Returns the spreadsheet id."""
    if override:
        return override
    env_var = {
        "donations":   "AKTT_DRIVE_DONATIONS_ID",
        "standard":    "AKTT_DRIVE_STD_RAFFLE_ID",
        "high_roller": "AKTT_DRIVE_HR_RAFFLE_ID",
    }[kind]
    val = os.environ.get(env_var)
    if not val:
        raise SystemExit(f"Spreadsheet id not set for {kind}: "
                         f"pass it explicitly or set ${env_var}.")
    return val


def main():
    """CLI: download a workbook by kind. Useful for ad-hoc testing."""
    import argparse
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("kind", choices=("donations", "standard", "high_roller"))
    ap.add_argument("--key", default=None, help="Override AKTT_DRIVE_KEY")
    ap.add_argument("--id", default=None, help="Override the configured spreadsheet id")
    ap.add_argument("--out", default=None, help="Output .xlsx path (default: ./<kind>.xlsx)")
    args = ap.parse_args()

    spreadsheet_id = resolve_id(args.kind, args.id)
    out = Path(args.out or f"{args.kind}.xlsx")
    print(f"Downloading {args.kind} ({spreadsheet_id}) -> {out}")
    p = export_sheet_as_xlsx(spreadsheet_id, out, key_path=args.key)
    print(f"Saved {p.stat().st_size:,} bytes to {p}")


if __name__ == "__main__":
    main()
