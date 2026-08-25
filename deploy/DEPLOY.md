# Deploying Bambu Monitor to the Synology DS223

The app is one small Python process: it holds a local-MQTT subscription to the
X2D and serves the dashboard on port **8770**, storing history in the Synology's
**existing MariaDB** (the same server familien-wiki uses). No Docker required.

Do the steps in order. Steps 1–6 are a one-time setup; after that the app
auto-starts on boot.

---

## 0. Prerequisites (already true on your NAS)
- **MariaDB 10** package installed and running (you use it for familien-wiki).
- **phpMyAdmin** (optional but easiest for step 2), or SSH access.

## 1. Install Python 3
Package Center → search **Python 3** → Install. (Any 3.9+ is fine.)

## 2. Create the database + user
Open **phpMyAdmin** → SQL tab → paste the contents of
[`schema_and_user.sql`](schema_and_user.sql), **after** replacing
`REPLACE_WITH_STRONG_PASSWORD` with a password you choose. Run it.

Then, in **Package Center → MariaDB 10 → open settings**, make sure
**"Enable TCP/IP connection"** is ticked (port 3306) — the Python app connects
over TCP, not the PHP socket.

## 3. Copy the app onto the NAS
Create a shared folder or reuse one, and copy the whole `bambu-monitor` folder to
e.g. **`/volume1/apps/bambu-monitor`** (File Station drag-and-drop is fine).
You need at least: `app.py`, `bambu_state.py`, `storage.py`, `filament_catalog.py`,
`printer.config.json`, `dashboard.html`, `requirements.txt`, `deploy/`.

> If you put it somewhere other than `/volume1/apps/bambu-monitor`, edit `APP_DIR`
> at the top of [`start.sh`](start.sh) to match.

## 4. Point the config at MariaDB
Edit `printer.config.json` on the NAS and change the storage block:
```json
"storage": {
  "backend": "mariadb",
  "sample_interval_sec": 20,
  "retention_days": 30,
  "mariadb": {
    "host": "127.0.0.1", "port": 3306,
    "user": "bambu", "password": "THE_PASSWORD_FROM_STEP_2",
    "database": "bambu_monitor"
  }
}
```
(Leave the `ip` / `access_code` / `serial` as they are.)

## 5. Create a virtualenv and install dependencies
SSH into the NAS (Control Panel → Terminal & SNMP → Enable SSH), then:
```sh
cd /volume1/apps/bambu-monitor
python3 -m venv venv
./venv/bin/python3 -m pip install --upgrade pip
./venv/bin/python3 -m pip install -r requirements.txt
```

## 6. Test-run it by hand
```sh
./venv/bin/python3 app.py
```
You should see `storage=mariadb` and `connected` telemetry. From another device on
your LAN, open **http://<NAS-IP>:8770** — the dashboard should show live data.
Press Ctrl+C to stop once confirmed.

> Port blocked? Control Panel → Security → Firewall: allow TCP **8770** (LAN only).

## 7. Auto-start on boot (+ watchdog)
Control Panel → **Task Scheduler**:
1. **Create → Triggered Task → User-defined script**
   - Task: `bambu-monitor start`, User: **root**, Event: **Boot-up**
   - Task Settings → Run command:
     `sh /volume1/apps/bambu-monitor/deploy/start.sh`
2. **Create → Scheduled Task → User-defined script** (watchdog / auto-restart)
   - User: **root**, Schedule: daily, **repeat every 5 minutes**
   - Same command as above.

`start.sh` is idempotent — the watchdog only relaunches the app if it has stopped.

Run task #1 once now (select it → **Run**) so you don't have to reboot.

---

## Updating later
Copy changed files over, then Task Scheduler → select `bambu-monitor start` →
there's no stop button, so kill it via SSH (`pkill -f app.py`); the 5-minute
watchdog restarts it with the new code (or run task #1 manually).

## Notes
- The Flask dev server is fine for single-user home use on the LAN. If you ever
  want a hardened server, `pip install waitress` and swap the last line of
  `app.py` — not needed for now.
- Backups: `bambu_monitor` is now part of your normal MariaDB backup, alongside
  familien-wiki.
