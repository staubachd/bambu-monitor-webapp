# Deploying Bambu Monitor to the Synology DS223

The app is one small Python process: it holds a local-MQTT subscription to the
X2D and serves the dashboard on port **8770**, storing history in the Synology's
**existing MariaDB** (the same server familien-wiki uses). MySQL works too, and
is offered in the wizard — the app speaks one protocol to both. No Docker required.

Do the steps in order. Steps 1–5 are a one-time setup; after that the app
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
`bootstrap.py`, `config_store.py`, `settings_schema.py`, `setup_wizard.py`,
`dashboard.html`, `setup.html`, `requirements.txt`, `deploy/`.

> There is no config file to copy. The app asks for everything on first run
> (step 6) and stores it in the database; only the database connection ends up
> on disk, in `instance/db.json`, which the wizard writes for you. Afterwards
> everything is editable from the **Settings page** — the gear in the top right
> of the dashboard.

> If you put it somewhere other than `/volume1/apps/bambu-monitor`, edit `APP_DIR`
> at the top of [`start.sh`](start.sh) to match.

## 4. Create a virtualenv and install dependencies
SSH into the NAS (Control Panel → Terminal & SNMP → Enable SSH), then:
```sh
cd /volume1/apps/bambu-monitor
python3 -m venv venv
./venv/bin/python3 -m pip install --upgrade pip
./venv/bin/python3 -m pip install -r requirements.txt
```

## 5. Run it, and answer the setup wizard
```sh
./venv/bin/python3 app.py
```
It will print `not configured yet`. From another device on your LAN, open
**http://<NAS-IP>:8770** — you get the setup wizard rather than the dashboard:

1. **Database** — MariaDB (or MySQL, if that is what you run), host `127.0.0.1`,
   port `3306`, user `bambu`, the password from step 2, database
   `bambu_monitor`. Press **Test connection**
   before continuing; it opens the connection and checks it can create a table,
   and says exactly what is wrong if it cannot.
2. **Printer** — IP, serial and LAN access code, all from the printer's screen
   under Settings › Network. **Test printer** confirms them.
3. **Plug, cloud & camera** — optional, skip what you do not have.
4. **Costs & filament** — electricity price and per-kg prices.
5. **Recording & safety** — the defaults are fine.

**Finish** writes `instance/db.json`, stores everything else in the database and
restarts the app; the page reloads into the dashboard by itself. Press Ctrl+C to
stop once you have confirmed live data.

> Upgrading an existing install? Copy your old `printer.config.json` across too
> and the wizard arrives prefilled from it, credentials included. On finish it
> is renamed to `printer.config.json.imported` and never read again. To do it
> without a browser: `./venv/bin/python3 tools/import_config.py --write`.

> To change any of this later: the Settings page for everything except the
> connection, and `./venv/bin/python3 app.py --setup` for that.

> Port blocked? Control Panel → Security → Firewall: allow TCP **8770** (LAN only).

## 6. Auto-start on boot (+ watchdog)
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
  familien-wiki — and since the settings live there too, that backup now covers
  the configuration as well. The only thing outside it is `instance/db.json`,
  which is five values you could retype in a minute.
