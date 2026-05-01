# Google Drive integration setup

Once-only setup so the LXC can pull the donations workbook and the two raffle
workbooks from your Drive without any browser interaction.

## 1. Create a Google Cloud project

Visit https://console.cloud.google.com/projectcreate. Name it whatever you
like (e.g. `aktt-tracker`). You don't need a billing account; the Drive API's
free tier covers our usage many times over.

## 2. Enable the Drive API

In the project, go to **APIs & Services -> Library** and search for
`Google Drive API`. Click **Enable**.

## 3. Create a service account

**APIs & Services -> Credentials -> Create credentials -> Service account**.

* Name: `aktt-drive-reader`
* Role: leave blank (no project-level role needed; access is per-spreadsheet)
* Click **Done**.

Open the service account, go to **Keys -> Add key -> Create new key -> JSON**.
A JSON file downloads. This is the credential the LXC uses; treat it like a
password.

## 4. Get the service-account email address

In the JSON file, look for `"client_email": "aktt-drive-reader@aktt-tracker-XXXXX.iam.gserviceaccount.com"`.

Copy that email; you'll share spreadsheets with it next.

## 5. Share the three spreadsheets with the service account

For each of these:

* AKTT Standard Raffle
* AKTT High-Roller Raffle
* Auction Donations

Open the spreadsheet in your browser, click **Share**, paste the service-account
email, set permission to **Viewer**, and uncheck "Notify people". Click **Share**.

## 6. Get the spreadsheet IDs

Each spreadsheet's URL looks like:

    https://docs.google.com/spreadsheets/d/<SPREADSHEET_ID>/edit#gid=0

Grab the `<SPREADSHEET_ID>` part for each of the three.

## 7. Install the JSON key on the LXC

```bash
mkdir -p /etc/aktt
sudo cp ~/Downloads/aktt-tracker-XXXX.json /etc/aktt/drive-key.json
sudo chown root:akttuser /etc/aktt/drive-key.json
sudo chmod 640 /etc/aktt/drive-key.json
```

## 8. Set up environment for the akttuser shell

Add to `~akttuser/.bashrc` (or wherever you keep service env):

```bash
export AKTT_DRIVE_KEY=/etc/aktt/drive-key.json
export AKTT_DRIVE_DONATIONS_ID=1e-W8wjAMqAA2dqaI-...        # your donations sheet
export AKTT_DRIVE_STD_RAFFLE_ID=1LOJhYFomxLaq9aHZBf3...     # your standard raffle
export AKTT_DRIVE_HR_RAFFLE_ID=1VgGRywKQ97NXSIIbGpvt...     # your HR raffle
export AKTT_DB=/home/akttuser/aktt-tracker/guildstats.db
```

## 9. Install the Python deps

```bash
source ~/venv/bin/activate
pip install google-api-python-client google-auth
```

## 10. Smoke-test it

```bash
# Download the donations workbook to a temp file
python3 drive_sync.py donations --out /tmp/donations.xlsx
ls -la /tmp/donations.xlsx     # should be tens of kB

# Pull current donations into the database
python3 donations.py --db $AKTT_DB import-from-sheet

# Pull the latest raffle drawing into the database
python3 sync_from_drive.py --db $AKTT_DB
```

If any step fails with a permissions error, double-check that the
service-account email is in the spreadsheet's share list.

## 11. Automate (recommended)

The `automation/aktt-drive-sync.timer` and `aktt-drive-sync.service` units
run a periodic pull. Default cadence:

* donations sync: every 30 minutes
* winners sync: every 30 minutes (idempotent; only re-imports the latest
  drawn tab)

Install:

```bash
sudo cp aktt-drive-sync.path     /etc/systemd/system/    # not needed; this is timer-based
sudo cp aktt-drive-sync.timer    /etc/systemd/system/
sudo cp aktt-drive-sync.service  /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now aktt-drive-sync.timer
```

Inspect: `systemctl list-timers aktt-drive-sync.timer` and
`journalctl -u aktt-drive-sync.service -n 50`.
