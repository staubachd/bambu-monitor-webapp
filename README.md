# Bambu Monitor

A self-hosted dashboard for the **Bambu Lab X2D** that answers the question the
printer never does: *what did that print actually cost?*

It talks to the printer over its **local MQTT** interface — no cloud account
needed — records every print, measures real electricity draw from a smart plug,
prices the filament, and serves a single-page dashboard. It is built to run
unattended on a **Synology NAS**, and runs just as happily on a laptop.

> Written specifically around the X2D (two nozzles, heated chamber, AMS), but the
> core is the standard Bambu schema and works on other Bambu printers with minor
> tweaks.

<img width="1999" alt="overview" src="https://github.com/user-attachments/assets/ea4019c7-f876-4b81-9d98-b8095be4118b" />

---

## Contents

- [Features](#features)
- [Screenshots](#screenshots)
- [Installation](#installation)
  - [What you need](#what-you-need)
  - [Try it on your own computer first](#try-it-on-your-own-computer-first)
  - [Installing on a Synology NAS](#installing-on-a-synology-nas)
  - [Optional extras](#optional-extras)
- [Updating](#updating)
- [Configuration](#configuration)
- [Backup and restore](#backup-and-restore)
- [HTTP API](#http-api)
- [Project layout](#project-layout)
- [Troubleshooting](#troubleshooting)
- [Disclaimer](#disclaimer)
- [License](#license)

---

## Features

**Live view of the machine.** Job state, progress, layers, time remaining and the
wall-clock finish time (with the weekday for overnight jobs). Both nozzles, bed,
chamber, all four fans on a 0–100 % scale, Wi-Fi, speed. Per-slot AMS filament
with type, brand, colour, remaining and humidity. HMS health warnings in the
header, each one acknowledgeable. Pushed to the browser over SSE — the page never
polls.

**Cost accounting**, which is why the app exists. A Tapo or Zigbee/MQTT smart plug
gives real watts, integrated per print into kWh and money. Filament is priced from
a rule chain — a price you typed beats one learned from your invoices, which beats
a brand × material matrix — and finished jobs carry the exact grams the slicer
computed. Every print row shows power, material and total, always rounded up, so a
quote can never come out under the real cost.

**Print history** that stays useful. Rename jobs, group the plates of one model
under a collapsible header with its own subtotals, override the grams, close a
print the app missed the end of, delete test prints. Jobs sliced from MakerWorld
link back to the model. An expandable panel per print shows everything known
about it.

**Filament inventory.** Every spool ever seen, named in full from its RFID colour
code (`PLA Basic · Jade White`), and remembered long after it is used up.
Bought-vs-used tracking from pasted or uploaded order confirmations, remaining
grams with a correction anchor, low-stock flags and one-click reorder links.
Third-party spools can be named and priced by hand — and doing so re-costs the
prints that already happened, so the totals never contradict the number you just
typed.

**Slicer data, read from the printer.** After a print, the app reads that job's
sliced file off the printer's USB drive and records the layer height, profile,
infill, supports, model height and exact filament weight — none of which any MQTT
frame or cloud response contains. It reads about 150 KB of a file that may be
hundreds of megabytes.

**USB drive overview.** What is on the stick, how full it is, and which print each
file belongs to.

**Statistics, maintenance reminders and a notes tab** with images, for the things
that otherwise live on a sticky note.

**Machine controls** — pause, resume, stop, light, speed, AMS slot assignment —
behind a confirm, with the commands the firmware rejects switched off by default.

**Live camera view** (optional), relayed locally through go2rtc. No cloud, no
Docker.

**Runs quiet.** A three-state recording toggle; in **Off** the app drops the
connection entirely so the NAS disks can sleep.

**English and German**, switchable in the header.

---

## Screenshots

Machine page:
<img width="2023" alt="machine" src="https://github.com/user-attachments/assets/256139ec-9f66-4930-9732-73da9322a894" />

Print history:
<img width="2019" alt="print_history" src="https://github.com/user-attachments/assets/f88c826f-99f5-4c5a-abda-04c1a296c749" />

Statistics:
<img width="2025" alt="statistics" src="https://github.com/user-attachments/assets/cfa53b64-40b0-4d30-85d3-6522b75d2842" />

Maintenance:
<img width="2014" alt="maintenance" src="https://github.com/user-attachments/assets/16aa5446-d695-454d-b8bb-97366646067d" />

---

## Installation

### What you need

| | |
|---|---|
| **The printer's IP, serial and LAN access code** | All three are on the printer: **Settings › Network**. The access code is the MQTT *and* the FTPS password. |
| **Python 3.9 or newer** | 3.9 is what the Synology package gives you; anything newer is fine. |
| **A database** | SQLite needs nothing at all. MariaDB or MySQL is recommended for a NAS install. |
| **A web server** | **None.** The app serves its own dashboard on port `8770`. A reverse proxy is optional — see [below](#optional-a-nicer-address). |

The printer does **not** need LAN-Only or Developer Mode. Telemetry works with
Cloud mode on.

---

### Try it on your own computer first

Ten minutes, no database to set up, and it tells you whether the printer talks to
you at all:

```bash
git clone <this repo>
cd bambu-monitor
pip install -r requirements.txt
python app.py
```

There is no config file to write. The first start has nothing to connect to, so it
serves a **setup wizard** instead of the dashboard — open
<http://localhost:8770> and answer it. Choose **SQLite** on the first page and it
needs nothing but a filename. The app restarts itself and the page turns into the
dashboard.

---

### Installing on a Synology NAS

This is the real deployment: the app runs on the NAS, so nothing depends on a PC
being switched on. Steps 1–6 are one-time.

#### 1. Install Python 3

**Package Center** → search **Python 3** → **Install**.

> Synology also ships an older `python3` in `/bin`. Use the package's interpreter,
> at `/var/packages/Python3.9/target/usr/bin/python3.9` — the commands below do.

#### 2. Create the database

Skip this if you want SQLite (fine for one printer; the app just writes a file).
For MariaDB or MySQL:

1. **Package Center** → install **MariaDB 10** if it isn't there already.
2. Open its settings and tick **Enable TCP/IP connection** (port 3306). The app
   connects over TCP, not the PHP socket — without this it cannot log in.
3. Run [`deploy/schema_and_user.sql`](deploy/schema_and_user.sql), after replacing
   `REPLACE_WITH_STRONG_PASSWORD` with a password you choose. Easiest through
   **phpMyAdmin** (Package Center → install → SQL tab → paste → Go), or over SSH.

That script creates a `bambu_monitor` database and a least-privilege `bambu` user.
**The app creates its own tables** on first start — you never run migrations by
hand.

#### 3. Copy the app onto the NAS

Create or reuse a shared folder and copy the project to
**`/volume1/apps/bambu-monitor`**. File Station drag-and-drop is fine.

> Putting it elsewhere is fine too — edit `APP_DIR` at the top of
> [`deploy/start.sh`](deploy/start.sh) to match. Nothing else hardcodes a path.

#### 4. Create a virtualenv and install dependencies

Enable SSH (**Control Panel → Terminal & SNMP → Enable SSH service**), then:

```sh
cd /volume1/apps/bambu-monitor
/var/packages/Python3.9/target/usr/bin/python3.9 -m venv venv
./venv/bin/python3 -m pip install --upgrade pip
./venv/bin/python3 -m pip install -r requirements.txt
```

#### 5. Start it and answer the wizard

```sh
./venv/bin/python3 app.py
```

It prints `not configured yet`. From any device on your LAN open
**`http://<NAS-IP>:8770`** — you get the setup wizard:

1. **Database** — MariaDB (or MySQL), host `127.0.0.1`, port `3306`, user `bambu`,
   the password from step 2, database `bambu_monitor`. Press **Test connection**
   first: it opens the connection, checks it can create a table, and says exactly
   what is wrong if it cannot.
2. **Printer** — IP, serial and LAN access code. **Test printer** confirms them.
3. **Plug, cloud, camera & slicer** — all optional, skip what you do not have.
4. **Costs & filament** — your electricity price and per-kg filament prices.
5. **Recording & safety** — the defaults are right for most people.

**Finish** writes the connection to `instance/db.json`, stores everything else in
the database, and restarts the app. The page reloads into the dashboard on its own.
Press `Ctrl+C` once you have seen live data.

#### 6. Make it start on its own

**Control Panel → Task Scheduler → Create → Triggered Task → User-defined script**,
user `root`, event **Boot-up**:

```sh
sh /volume1/apps/bambu-monitor/deploy/start.sh
```

Then add a second, **scheduled** task running the same line every 5 minutes as a
watchdog. `start.sh` is idempotent — it starts the app only if it is not already
running — so the watchdog is safe.

#### 7. Open it

**`http://<NAS-IP>:8770`**. Bookmark it; that is the whole app.

If the Synology firewall is on, allow TCP **8770**. To use a different port, set
the `BAMBU_PORT` environment variable.

#### Optional: a nicer address

To reach it as `https://printer.yournas.local` instead of an IP and a port:
**Control Panel → Login Portal → Advanced → Reverse Proxy** (DSM 7; *Application
Portal* on DSM 6) → **Create**, source `printer.<your-nas>` on 443, destination
`localhost:8770`. Enable **WebSocket** on the destination so the live updates keep
working.

> Do not expose it to the internet. It has no authentication, and it holds your
> printer's access code.

---

### Optional extras

All of these are off until you switch them on, and each is a page in **Settings**
(the gear, top right).

**Smart plug — the cost figures depend on it.** Two kinds are supported:

- **Tapo P110/P110M**: enter your TP-Link account e-mail, password and the plug's
  IP. `python tools/setup_power.py` verifies it from the command line.
- **Any plug that publishes to MQTT** (Zigbee2MQTT, Shelly, Tasmota, an IKEA
  INSPELNING via a Zigbee bridge): enter your broker and the topic.
  `python tools/sniff_power_mqtt.py` listens and prints the topics it hears,
  marking the ones that look like a power meter.

Without a plug everything else still works; the app says "not configured" rather
than showing 0 W.

**Slicer data — needs a USB stick in the printer.** With one plugged in, the
printer writes each sliced job to it, and the app reads the layer height, profile
and exact filament weight out of that file after the print. Switch on
**Settings → Slicer**. This also makes exact filament weights available *without* a
Bambu account.

**Camera.** On the printer, enable **LAN Mode Live View** (under LAN Only on the
touchscreen — it does not require LAN-Only mode). Download the
[go2rtc](https://github.com/AlexxIT/go2rtc) binary for your NAS architecture into
`go2rtc/` (ARM64 Synology → `go2rtc_linux_arm64`), then tick
**Settings → Camera** and restart. A **Live view** tab appears.

The printer serves **one** camera client at a time — if the Handy app or Bambu
Studio is watching, the relay gets silence. go2rtc needs TCP **1984** and UDP
**8555** if your firewall is on.

**Bambu Cloud** (optional). Enriches finished prints with the exact filament used.
Not needed if you use the slicer reader above. `python tools/setup_cloud.py` signs
in and stores a token.

---

## Updating

Copy the changed files over, then:

```sh
sh /volume1/apps/bambu-monitor/deploy/start.sh restart
```

`restart` kills the running instance and **waits for it to exit** before starting
the new one — a plain kill-then-start races and can silently start nothing.

New database columns are added automatically on startup; the log shows
`[store] migrated: added …`. `dashboard.html` is served fresh per request, so
changing only the page needs no restart at all — just reload.

> **Editing on Windows:** do not round-trip source files through PowerShell
> `Get-Content -Raw` + `Set-Content`. PowerShell 5.1 mis-encodes UTF-8 and will
> double-encode every `°`, `·`, `€` and umlaut in the file. Use a UTF-8-aware
> editor.

---

## Configuration

**There is no config file to edit.** Everything lives in the database and is
editable from the **Settings** page — the gear in the top right. The single
exception is the database connection itself, which obviously cannot live in the
database: the wizard writes it to `instance/db.json` (seven keys, `chmod 600`,
git-ignored).

Settings are grouped into tabs — Printer, Cost, Filament, Power, Cloud, Slicer,
Camera, Recording, Controls — plus a read-only **Database** tab and a **Backup**
tab. Each field says whether it takes effect immediately or needs a restart.

Two environment variables exist: `BAMBU_PORT` (default `8770`) and `BAMBU_HOST`.

> Upgrading from a pre-wizard version with a `printer.config.json`? Copy it across
> and the wizard arrives prefilled from it, credentials included; on finish it is
> renamed to `.imported` and never read again. Headless equivalent:
> `python tools/import_config.py --write`.

---

## Backup and restore

The print history is the part nothing else has a copy of. **Settings → Backup**
downloads everything worth keeping — prints, filaments, purchases, notes and
settings — as one JSON file.

Telemetry is deliberately left out (a temperature sample every 20 s: the bulk of
the database, and the only table nothing is computed from), and credentials are
left out unless you ask for them, because a backup gets e-mailed and synced.

Restoring offers two modes and tells you what it will do before it does it:
**merge** inserts what is missing and never overwrites, **replace** empties each
table first and says how many rows that will delete.

For a backup that happens on its own, point the Synology task scheduler at:

```sh
/volume1/apps/bambu-monitor/venv/bin/python3 \
    /volume1/apps/bambu-monitor/tools/backup.py export \
    --out /volume1/backup/bambu --keep 30
```

---

## HTTP API

Everything the dashboard does is a plain HTTP endpoint.

| Method & path | Purpose |
|---|---|
| `GET /` | The dashboard (a single HTML file). |
| `GET /events` | **SSE** stream of the live printer state. |
| `GET /api/state` | The same state, once. |
| `GET /api/history?hours=` | Telemetry time-series for the charts. |
| `GET /api/prints?limit=` | Print history with costs. |
| `GET /api/stats` | Aggregates for the Statistics tab. |
| `GET /api/filaments` | Inventory: identities, usage, stock, prices. |
| `GET /api/storage` | What is on the printer's USB drive, and how full it is. |
| `GET /api/slicer` | What the slicer reader has been doing. |
| `GET /api/settings` · `POST /api/settings` | Read and write settings. |
| `GET /api/backup` · `POST /api/backup/restore` | Export / import everything. |
| `POST /api/prints/label` | Rename a print. |
| `POST /api/prints/group` | Group prints under a name; a blank name ungroups. |
| `POST /api/prints/filament` | Override the filament grams. |
| `POST /api/prints/layerheight` | Set the layer height by hand (`0,2`, `0.2 mm`, `200 µm`). |
| `POST /api/prints/slicer` | Read a print's sliced file from the printer now. |
| `POST /api/prints/finish` | Close a print that never got an end time. |
| `POST /api/prints/delete` | Delete one print (refuses a running job). |
| `POST /api/filaments/left` | Correct a spool's remaining grams. |
| `POST /api/control` | Pause / resume / stop / light / speed. |
| `POST /api/cloud/refresh` | Trigger a cloud enrichment pass. |
| `GET /api/diag` | What the app is writing, and how often. |

---

## Project layout

| Path | What it is |
|---|---|
| `app.py` | The whole backend: MQTT thread, Flask routes, SSE, background workers. |
| `bambu_state.py` | Turns the printer's messy report into a clean state dict. Pure, with a self-test. |
| `storage.py` | One store, three backends (SQLite / MariaDB / MySQL). |
| `gcode_meta.py` | Reads slicer metadata off the printer's USB drive over FTPS. |
| `filament_catalog.py` | Colour-code → colour-name table and the order-confirmation parser. |
| `dashboard.html` | The entire frontend — HTML, CSS, JS, i18n. No build step, no external assets. |
| `bootstrap.py` · `config_store.py` · `settings_schema.py` | The connection file, the settings store, and the one table describing every setting. |
| `setup_wizard.py` · `setup.html` | First-run setup, served instead of the dashboard when there is no connection on file. |
| `backup.py` | Export and restore. |
| `deploy/` | `DEPLOY.md`, the SQL for the database, `start.sh`. |
| `tools/` | One-off helpers: cloud login, plug setup, MQTT sniffing, scheduled backups, diagnostics. |
| `tests/` | The test suite — `pwsh tests/runall.ps1`. |
| `docs/INTERNALS.md` | Why the awkward parts are the way they are. Only needed if you change the code. |

**Stack:** Python 3 + Flask (threaded, SSE), paho-mqtt over TLS, PyMySQL, optional
`tapo` and `pypdf`, go2rtc for the camera. The frontend is one hand-written HTML
file — vanilla JS, CSS Grid, inline-SVG charts, no framework and no external
assets, which matters on a NAS with no internet access.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Tiles stuck on **"Connecting…"** | Recording mode is **Off**, which intentionally drops the stream. Switch to Auto or On. |
| Dashboard loads but no telemetry | Check `app.log` for a traceback; confirm the printer IP and access code, and that only one instance is running. |
| MQTT keeps connecting and disconnecting | Two instances fighting over the printer's single `bblp` login. `start.sh` guards against this — check for a stray process. |
| `caching_sha2_password cannot be loaded` (MySQL 8) | PyMySQL needs `cryptography`: `./venv/bin/python3 -m pip install -r requirements.txt`. |
| `#1054 Unknown column …` | The schema is behind. Restart the app — columns are migrated on startup. If it persists, check the connection didn't get flipped to SQLite. |
| A print is stuck **"running"** with a runaway duration | The app was down when it ended. Press **Refresh** for cloud enrichment, or click its **Duration** cell and type how long it took (`5h 20m`). |
| No slicer data, or **"no sliced file on the drive"** | There is no USB stick in the printer, or that print's file has since been deleted. Only jobs whose file is still on the stick can be read. |
| A spool shows a **code** (`A00-N04`) instead of a colour name | That code isn't in the built-in table yet. Add it under Settings → Filament. |
| A purchase says **"ambiguous — colour unclear"** | Several colours of that product are on record and the name doesn't single one out (often a German name against an English one). Fix the line's **colour code**, not its name. |
| Uploading a PDF says **"pypdf is not installed"** | It must go in the app's venv: `./venv/bin/python3 -m pip install pypdf`. Or paste the invoice text instead. |
| Live view spins, no picture | The printer serves **one** camera client at a time — close the Handy app and Bambu Studio. If it stays wedged, power-cycle the printer and re-enable LAN Mode Live View. |
| Controls do nothing / "printer not connected" | Recording is **Off**; controls need a live connection. |
| **HMS 0500_0500_0001_0007** after a control | The firmware rejected the command. Nothing was written and nothing is damaged — this is why the gcode-based controls ship off. |
| Garbled `°` / `€` / umlauts after editing | A file was round-tripped through PowerShell. Re-edit with a UTF-8-aware tool. |

---

## Disclaimer

This is an independent, unofficial hobby project. It is **not affiliated with,
authorized, sponsored, or endorsed by Bambu Lab or TP-Link (Tapo)**. "Bambu Lab",
"X2D", "AMS", "Tapo" and other product names are trademarks of their respective
owners and are used here only to describe what this software interoperates with.

The tool talks to your own printer over its **local** interfaces and to a smart
plug on your own network. The optional Bambu Cloud enrichment uses an unofficial
API and may be subject to Bambu Lab's terms of service; it is **disabled by
default** — enable it at your own discretion and risk.

Provided **as is**, with no warranty (see [LICENSE](LICENSE)). Credentials are
stored in plaintext (in the database, and the database password in
`instance/db.json`, which is `chmod 600` and git-ignored). That is fine for a
trusted LAN, but the app has no authentication and should **not be exposed to the
public internet**.

## License

Released under the [MIT License](LICENSE).
