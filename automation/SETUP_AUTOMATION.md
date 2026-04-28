# AKTT Automation Setup (Phase 2.5a)

This wires up: run `guild_stats.py` on Windows -> Lua files land on the LXC
-> systemd path unit fires -> `ingest.py` updates the database. No manual
copy/paste.

## Prerequisites

* The phase-1/phase-2 deployment is already running on the LXC.
* OpenSSH client is installed on Windows (built in to Win 10/11; check with
  `ssh -V` in PowerShell).
* Your LXC has an SSH server reachable from your Windows box.

## 1. Create an SSH key pair on Windows (one-time)

```powershell
ssh-keygen -t ed25519 -f $HOME\.ssh\aktt_lxc
```

Copy the *public* half to the LXC:

```powershell
type $HOME\.ssh\aktt_lxc.pub | ssh akttuser@aktt.example.local "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys"
```

Test that it works without a password prompt:

```powershell
ssh -i $HOME\.ssh\aktt_lxc akttuser@aktt.example.local hostname
```

## 2. Set up the LXC drop directory and ownership

On the LXC, as root:

```bash
useradd -m -s /bin/bash akttuser   # if you don't already have a service user
mkdir -p /var/lib/aktt-stats/{incoming,processed,failed}
chown -R akttuser:akttuser /var/lib/aktt-stats
```

Make sure `akttuser` has the venv at `/home/akttuser/venv` with the project
dependencies (`pip install slpp openpyxl tzdata`), and that the project files
live at `/home/akttuser/aktt-tracker/` (or set `AKTT_APP_DIR` in the service
unit).

## 3. Install the systemd path + service units

Copy `aktt-drop.path` and `aktt-drop.service` to `/etc/systemd/system/`,
then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now aktt-drop.path
sudo systemctl status aktt-drop.path
```

`status` should show `Active: active (waiting)`.

## 4. Wire the Windows side into guild_stats.py

Drop `aktt_sync_windows.py` next to `guild_stats.py`. At the very end of
`guild_stats.py`'s `if __name__ == "__main__":` block, after the existing
ingest finishes, add:

```python
from aktt_sync_windows import push_to_lxc

push_to_lxc(
    mm_path=os.path.abspath(SOURCE_FILES["mm"]),
    gbl_path=os.path.abspath(SOURCE_FILES["gbl"]),
    week=week,
    lxc_user="akttuser",
    lxc_host="aktt.example.local",       # <-- your LXC hostname or IP
    lxc_dir="/var/lib/aktt-stats/incoming",
    ssh_key=r"C:\Users\you\.ssh\aktt_lxc",   # or None to use default key
)
```

## 5. Test end-to-end

Run `guild_stats.py` as you normally would. You should see:

```
[aktt-sync] pushing MasterMerchant.lua -> akttuser@...
[aktt-sync] pushing GBLData.lua -> ...
[aktt-sync] pushing manifest.json (trigger) -> ...
[aktt-sync] done.
```

Within a second or two on the LXC:

```bash
journalctl -u aktt-drop.service -n 50 --no-pager
ls /var/lib/aktt-stats/processed/  # should contain a new <timestamp>/ dir
```

If anything fails, the input files plus a per-run log get moved to
`/var/lib/aktt-stats/failed/<timestamp>-<reason>/` for debugging.

## Troubleshooting

* `aktt-drop.path` triggers on `PathChanged`, which fires on file modification
  *or* creation. A re-run with the same filename works fine.
* The processor exits cleanly (rc=0) if the manifest doesn't exist when it
  fires - this can happen during normal cleanup races.
* To inspect pending drops manually: `ls -la /var/lib/aktt-stats/incoming/`
* To re-process a failed drop: move the contents of `failed/<timestamp>/`
  back into `incoming/` (manifest.json last) and the unit will re-fire.
