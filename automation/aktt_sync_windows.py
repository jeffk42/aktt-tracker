"""aktt_sync_windows.py - Tail to add to your existing guild_stats.py.

After guild_stats.py finishes generating its CSVs, call push_to_lxc() to
copy MasterMerchant.lua + GBLData.lua + a small manifest.json onto the LXC.
The manifest is written LAST so it serves as the "ready" trigger that the
LXC's systemd path unit watches for.

Requires:
  * Windows 10/11 with built-in OpenSSH client (`scp` on PATH)
  * SSH key auth set up to the LXC (no password prompts at runtime)

Quick-start integration in guild_stats.py:

    from aktt_sync_windows import push_to_lxc

    # ... existing code that produces the lua files and the CSVs ...

    push_to_lxc(
        mm_path=os.path.abspath(SOURCE_FILES["mm"]),
        gbl_path=os.path.abspath(SOURCE_FILES["gbl"]),
        week=week,                      # 'this' or 'last'
        lxc_user="akttuser",            # <-- edit
        lxc_host="aktt.example.local",  # <-- edit (IP or hostname)
        lxc_dir="/var/lib/aktt-stats/incoming",  # must exist on LXC
        ssh_key=None,                   # or r"C:\\Users\\you\\.ssh\\id_ed25519"
    )
"""
from __future__ import annotations
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def _scp(local: str, remote: str, ssh_key: str | None) -> None:
    args = ["scp"]
    if ssh_key:
        args += ["-i", ssh_key]
    # -p preserves mtime which is handy for systemd path-unit semantics
    args += ["-p", local, remote]
    result = subprocess.run(args, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        sys.stderr.write(f"scp failed: {result.stderr}\n")
        raise SystemExit(f"scp {local} -> {remote} failed")


def push_to_lxc(mm_path: str, gbl_path: str, week: str,
                lxc_user: str, lxc_host: str, lxc_dir: str,
                ssh_key: str | None = None,
                guild_name: str | None = None) -> None:
    """Copy the two Lua files + a manifest onto the LXC. Manifest goes LAST.

    Raises SystemExit on any scp failure. Safe to call repeatedly - the LXC
    side dedupes via transaction_id.
    """
    if not Path(mm_path).is_file():
        raise SystemExit(f"mm_path not found: {mm_path}")
    if not Path(gbl_path).is_file():
        raise SystemExit(f"gbl_path not found: {gbl_path}")
    if week not in ("this", "last"):
        raise SystemExit(f"week must be 'this' or 'last', got {week!r}")

    target_dir = lxc_dir.rstrip("/")
    base_target = f"{lxc_user}@{lxc_host}:{target_dir}"

    # 1. Push the data files first under stable filenames
    print(f"[aktt-sync] pushing MasterMerchant.lua -> {base_target}/MasterMerchant.lua")
    _scp(mm_path, f"{base_target}/MasterMerchant.lua", ssh_key)
    print(f"[aktt-sync] pushing GBLData.lua -> {base_target}/GBLData.lua")
    _scp(gbl_path, f"{base_target}/GBLData.lua", ssh_key)

    # 2. Write manifest.json locally and push it LAST. Its arrival on the LXC
    #    is the trigger the systemd path unit fires on.
    manifest = {
        "version": 1,
        "week": week,
        "ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mm_filename": "MasterMerchant.lua",
        "gbl_filename": "GBLData.lua",
        "guild_name": guild_name,
        "source_host": os.environ.get("COMPUTERNAME") or "windows",
    }
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as tf:
        json.dump(manifest, tf, indent=2)
        local_manifest = tf.name
    try:
        print(f"[aktt-sync] pushing manifest.json (trigger) -> {base_target}/manifest.json")
        _scp(local_manifest, f"{base_target}/manifest.json", ssh_key)
    finally:
        try:
            os.remove(local_manifest)
        except OSError:
            pass
    print("[aktt-sync] done.")


if __name__ == "__main__":
    # CLI fallback for ad-hoc use
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--mm", required=True)
    p.add_argument("--gbl", required=True)
    p.add_argument("--week", required=True, choices=("this", "last"))
    p.add_argument("--lxc-user", required=True)
    p.add_argument("--lxc-host", required=True)
    p.add_argument("--lxc-dir", default="/var/lib/aktt-stats/incoming")
    p.add_argument("--ssh-key", default=None)
    a = p.parse_args()
    push_to_lxc(mm_path=a.mm, gbl_path=a.gbl, week=a.week,
                lxc_user=a.lxc_user, lxc_host=a.lxc_host,
                lxc_dir=a.lxc_dir, ssh_key=a.ssh_key)
