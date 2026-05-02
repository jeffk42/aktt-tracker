"""process_drop.py - LXC-side handler triggered by the aktt-drop.path systemd
unit when a new manifest.json appears in the incoming directory.

Reads the manifest, runs ingest.py with the right --week argument, and on
success archives the input files to processed/<timestamp>/. On failure they
go to failed/<timestamp>/ with a copy of the error log so you can debug.

Designed to be invoked by systemd; safe to also run manually:
    /home/akttuser/venv/bin/python3 /opt/aktt/process_drop.py
"""
from __future__ import annotations
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# --- defaults; override via environment if you want ---
APP_DIR      = Path(os.environ.get("AKTT_APP_DIR",   "/home/akttuser/aktt-tracker"))
INCOMING_DIR = Path(os.environ.get("AKTT_INCOMING",  "/var/lib/aktt-stats/incoming"))
PROCESSED_DIR= Path(os.environ.get("AKTT_PROCESSED", "/var/lib/aktt-stats/processed"))
FAILED_DIR   = Path(os.environ.get("AKTT_FAILED",    "/var/lib/aktt-stats/failed"))
DB_PATH      = Path(os.environ.get("AKTT_DB",        APP_DIR / "guildstats.db"))
PYTHON_BIN   = Path(os.environ.get("AKTT_PYTHON",    APP_DIR / "venv/bin/python3"))


def main() -> int:
    manifest_path = INCOMING_DIR / "manifest.json"
    if not manifest_path.is_file():
        # Path unit triggered but file isn't here yet (or got moved already).
        # Quiet exit; systemd will fire again if it reappears.
        return 0

    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"[aktt-drop] cannot read manifest: {e}", file=sys.stderr)
        _quarantine([manifest_path], reason="bad-manifest")
        return 2

    mm_name  = manifest.get("mm_filename", "MasterMerchant.lua")
    gbl_name = manifest.get("gbl_filename", "GBLData.lua")
    week     = manifest.get("week")
    if week not in ("this", "last"):
        print(f"[aktt-drop] manifest has invalid week={week!r}", file=sys.stderr)
        _quarantine([manifest_path], reason="bad-week")
        return 2

    mm_path  = INCOMING_DIR / mm_name
    gbl_path = INCOMING_DIR / gbl_name
    if not mm_path.is_file() or not gbl_path.is_file():
        print(f"[aktt-drop] missing input file(s): mm={mm_path.exists()} gbl={gbl_path.exists()}",
              file=sys.stderr)
        _quarantine([manifest_path, mm_path, gbl_path], reason="missing-input")
        return 2

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = INCOMING_DIR / f"ingest-{timestamp}.log"

    cmd = [
        str(PYTHON_BIN), str(APP_DIR / "ingest.py"),
        "--db", str(DB_PATH),
        "--mm", str(mm_path),
        "--gbl", str(gbl_path),
        "--week", week, "--schema", str(APP_DIR / "schema.sql"),
    ]
    print(f"[aktt-drop] running: {' '.join(cmd)}")
    with open(log_path, "w", encoding="utf-8") as logf:
        result = subprocess.run(cmd, stdout=logf, stderr=subprocess.STDOUT,
                                cwd=str(APP_DIR))

    if result.returncode != 0:
        print(f"[aktt-drop] ingest FAILED (exit {result.returncode}); see {log_path}",
              file=sys.stderr)
        _quarantine([manifest_path, mm_path, gbl_path, log_path], reason=f"ingest-rc{result.returncode}")
        return 1

    # Success - archive
    archive_dir = PROCESSED_DIR / timestamp
    archive_dir.mkdir(parents=True, exist_ok=True)
    for f in (manifest_path, mm_path, gbl_path, log_path):
        if f.exists():
            shutil.move(str(f), str(archive_dir / f.name))
    print(f"[aktt-drop] processed week={week}; archived to {archive_dir}")
    return 0


def _quarantine(paths, reason: str) -> None:
    """Move offending files to FAILED_DIR/<timestamp>-<reason>/."""
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    qdir = FAILED_DIR / f"{timestamp}-{reason}"
    qdir.mkdir(parents=True, exist_ok=True)
    for p in paths:
        if p.exists():
            try:
                shutil.move(str(p), str(qdir / p.name))
            except Exception as e:
                print(f"[aktt-drop] could not move {p} to quarantine: {e}",
                      file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
