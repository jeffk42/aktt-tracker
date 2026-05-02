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
SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/spreadsheets.readonly",
]
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


GOOGLE_SHEET_MIME = "application/vnd.google-apps.spreadsheet"


def get_sheets_service(key_path: str | None = None):
    """Build a Sheets v4 service; uses the same service-account key as Drive."""
    key_path = key_path or os.environ.get("AKTT_DRIVE_KEY")
    if not key_path:
        raise SystemExit("Service account key not specified.")
    creds = service_account.Credentials.from_service_account_file(
        key_path, scopes=SCOPES)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def _build_xlsx_via_sheets_api(spreadsheet_id: str, out_path: Path,
                               sheets_service, tab_name: str | None = None) -> Path:
    """Read cell values via the Sheets API and write a minimal xlsx via openpyxl.

    Used as a fallback when Drive's xlsx export fails (typical for very
    large Sheets with many tabs/formulas) and as the primary path when a
    specific tab_name is requested (much faster than full-workbook export).
    """
    import openpyxl  # imported lazily so only this fallback path needs it

    meta = sheets_service.spreadsheets().get(
        spreadsheetId=spreadsheet_id,
        fields="sheets(properties(title))",
    ).execute()
    all_titles = [s["properties"]["title"] for s in meta.get("sheets", [])]

    if tab_name:
        if tab_name not in all_titles:
            raise SystemExit(f"Tab {tab_name!r} not found; available: {all_titles}")
        titles = [tab_name]
    else:
        titles = all_titles

    # Sheets API limits: batchGet with ~hundreds of ranges is fine. We use a
    # generous A1:ZZ500 to capture entry tables, prize sections, and headers
    # without needing a per-tab schema-aware bound.
    BATCH = 50
    rows_by_title: dict = {}
    for i in range(0, len(titles), BATCH):
        chunk = titles[i:i + BATCH]
        ranges = [f"'{t}'!A1:ZZ500" for t in chunk]
        resp = sheets_service.spreadsheets().values().batchGet(
            spreadsheetId=spreadsheet_id,
            ranges=ranges,
            valueRenderOption="UNFORMATTED_VALUE",
            dateTimeRenderOption="SERIAL_NUMBER",
        ).execute()
        for title, vr in zip(chunk, resp.get("valueRanges", [])):
            rows_by_title[title] = vr.get("values", [])

    # Match Drive's xlsx-export behavior: strip the chars Excel forbids in sheet
    # names rather than substituting them, so tab names like "04/24/26" become
    # "042426" (matching the MMDDYY format consumers expect) instead of
    # "04_24_26" or similar.
    import re as _re
    _FORBIDDEN = _re.compile(r'[/\\?*:\[\]]')
    def _xlsx_safe(name: str) -> str:
        cleaned = _FORBIDDEN.sub("", name).strip()
        cleaned = cleaned[:31] or "Sheet"
        return cleaned

    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for title in titles:
        ws = wb.create_sheet(title=_xlsx_safe(title))
        for row in rows_by_title.get(title, []):
            ws.append(row)
    wb.save(out_path)
    return out_path


def export_sheet_as_xlsx(spreadsheet_id: str, out_path: str | Path,
                         service=None, key_path: str | None = None,
                         tab_name: str | None = None,
                         sheets_service=None) -> Path:
    """Download `spreadsheet_id` as an .xlsx file at `out_path`.

    Strategy:
      1. If `tab_name` is given OR the Drive export fails with cannotExportFile,
         use the Sheets API to read cells and build a small xlsx ourselves.
      2. Otherwise, native Google Sheet -> Drive export_media (fast path).
      3. Or already-xlsx file uploaded to Drive -> get_media (download bytes).

    Returns the local Path. Raises SystemExit on unrecoverable failure."""
    if service is None:
        service = get_service(key_path)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # If caller wants just one tab, go straight to Sheets API - much faster
    # than exporting an entire huge workbook just to read one sheet.
    if tab_name:
        if sheets_service is None:
            sheets_service = get_sheets_service(key_path)
        return _build_xlsx_via_sheets_api(spreadsheet_id, out_path,
                                          sheets_service, tab_name=tab_name)

    # Inspect the file's mimeType
    try:
        meta = service.files().get(fileId=spreadsheet_id,
                                   fields="mimeType,name").execute()
    except Exception as e:
        raise SystemExit(
            f"Drive lookup failed for {spreadsheet_id}: {e}\n"
            f"Check that the spreadsheet is shared with the service account."
        )

    mt = meta.get("mimeType", "")

    if mt == XLSX_MIME or "spreadsheetml" in mt or "excel" in mt.lower():
        # Uploaded xlsx -> download bytes directly (works regardless of size)
        request = service.files().get_media(fileId=spreadsheet_id)
        try:
            with open(out_path, "wb") as fh:
                downloader = MediaIoBaseDownload(fh, request)
                done = False
                while not done:
                    _status, done = downloader.next_chunk()
        except Exception as e:
            raise SystemExit(f"Drive download failed: {e}")
        return out_path

    if mt == GOOGLE_SHEET_MIME:
        # Native Google Sheet -> try Drive xlsx export, fall back to Sheets API
        try:
            request = service.files().export_media(fileId=spreadsheet_id,
                                                   mimeType=XLSX_MIME)
            with open(out_path, "wb") as fh:
                downloader = MediaIoBaseDownload(fh, request)
                done = False
                while not done:
                    _status, done = downloader.next_chunk()
            if out_path.stat().st_size >= 1024:
                return out_path
        except Exception as e:
            msg = str(e)
            if "cannotExportFile" not in msg and "exportSizeLimitExceeded" not in msg:
                # Unexpected error - re-raise
                raise SystemExit(f"Drive export failed for {spreadsheet_id}: {e}")
            # Fall through to Sheets API
            print(f"[drive-sync] Drive xlsx export not available for "
                  f"{meta.get('name')!r} ({msg.splitlines()[0]}); "
                  f"falling back to Sheets API")

        # Fallback: build xlsx via Sheets API (slower but works for big files)
        if sheets_service is None:
            sheets_service = get_sheets_service(key_path)
        return _build_xlsx_via_sheets_api(spreadsheet_id, out_path, sheets_service)

    raise SystemExit(
        f"Unsupported mimeType {mt!r} for {meta.get('name')!r}; "
        f"need a Google Sheet or an xlsx-format file."
    )


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
