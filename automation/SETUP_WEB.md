# Web UI setup (Phase 3)

The FastAPI app lives in `web/` next to the existing scripts. It reads
from the same SQLite database via `guildstats.py`. No writes from the
web UI; it's a public read-only site.

## 1. Install dependencies

```bash
source ~/venv/bin/activate
pip install fastapi 'uvicorn[standard]' jinja2
```

## 2. Apply the latest schema (adds guild_settings)

```bash
sqlite3 $AKTT_DB < schema.sql
```

`guild_settings` seeds the weekly contribution goal (40,000 by default).
Change it later with:

```bash
sqlite3 $AKTT_DB "UPDATE guild_settings SET value='50000' WHERE key='weekly_contribution_goal';"
```

## 3. Run the dev server (manual smoke test)

```bash
cd /home/akttuser/aktt-tracker
AKTT_DB=$AKTT_DB uvicorn web.app:app --host 0.0.0.0 --port 8000 --reload
```

Then visit `http://<lxc-ip>:8000/`. You should see the dashboard with
current trader, top sellers, etc.

Try `http://<lxc-ip>:8000/u/@jeffk42` for the personal stats page.

## 4. Production deployment

Copy `aktt-web.service` to `/etc/systemd/system/` and enable it:

```bash
sudo cp automation/aktt-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now aktt-web.service
sudo systemctl status aktt-web.service
journalctl -u aktt-web.service -f          # live logs
```

The service binds to `127.0.0.1:8000` (loopback only). To expose it on
the network, put a reverse proxy (Caddy or nginx) in front.

## 5. Caddy reverse proxy + TLS

When you're ready to point `aktt.info` at the LXC:

```bash
sudo apt install caddy
sudo cp automation/Caddyfile.example /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

Caddy automatically obtains a Let's Encrypt cert as soon as DNS resolves.
For LAN-only testing, edit `aktt.info` in the Caddyfile to `:80` and skip
TLS entirely.

## 6. Firewall (assuming a separate VLAN as planned)

The LXC should expose only ports 80/443 to the public internet. Block
everything else, especially port 8000 directly. Caddy is the only thing
that should be reachable from outside.

```bash
# UFW example (Debian/Ubuntu)
sudo ufw allow 22/tcp     # ssh, restrict source if you can
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
```

## Pages

All five top-nav items are live as of Phase 3.5.

- `/`                          dashboard with current trader, top-5 sellers / buyers /
                               contributors, active member count, weekly goal
- `/u/@account`                personal stats page (full history, sales + contribution
                               charts, sortable per-week table)
- `/rankings`                  six leaderboards (sellers, contributors, buyers,
                               item donors, raffle buyers, raffle wins) plus
                               "Most Active" on multi-week views; period selector
                               at the top — `current`, `4w`, `13w`, `52w`, `lifetime`
- `/raffles`                   index of every drawing with std + HR ticket counts
                               and a featured "next/latest drawing" card
- `/raffles/{YYYY-MM-DD}`      per-drawing detail (standard + HR side by side,
                               prize tables with winners, top entrants)
- `/traders`                   trader history; current trader card, win-rate stats,
                               top locations / NPCs, sortable per-week table.
                               Bid amounts are intentionally NOT exposed.
- `/trends`                    guild-wide trends — four Chart.js panels
                               (weekly sales, contribution composition stacked,
                               active members, raffle tickets per drawing with
                               dual y-axes for std vs HR)
- `/api/users/search?q=...`    HTMX dropdown fragment for the search box

Routes that show long tables accept `?limit=N` to extend pagination.

## What's NOT in the web UI yet

- Officer-only forms for adding donations, manual entries, and trader bids
  (still done via the CLI scripts)
- Database-side raffle drawing
- Self-hosted Chart.js / HTMX (currently CDN-loaded)
