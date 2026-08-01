# Bambu Monitor

A self-hosted monitoring and cost-accounting dashboard for the **Bambu Lab X2D**
3D printer. It connects to the printer over its **local MQTT** interface, keeps a
live normalized view of everything the machine reports, records telemetry and
per-print history to a database, measures real electricity draw via a **Tapo smart
plug**, enriches finished jobs from the **Bambu Cloud**, and serves a single-page
web dashboard.

It is designed to run **entirely on a Synology NAS** (alongside an existing app on
the same box) so nothing depends on a PC being switched on, while still being fully
runnable on Windows/macOS/Linux for development.

> The X2D is a dual-nozzle machine with a heated chamber and an AMS. The app is
> written specifically around that hardware (two extruders, packed temperature
> encoding, reversed firmware nozzle numbering, chamber heater) but the core is
> the standard Bambu schema and works with other Bambu printers with minor tweaks.

---

## Table of contents

- [What it does](#what-it-does)
- [Screenshotted at a glance](#screenshotted-at-a-glance)
- [Architecture](#architecture)
- [Technology stack](#technology-stack)
- [Where the data comes from](#where-the-data-comes-from)
- [Project layout](#project-layout)
- [Configuration](#configuration)
- [Running locally](#running-locally)
- [Deploying to the Synology NAS](#deploying-to-the-synology-nas)
- [Live view (camera)](#live-view-camera)
- [HTTP API](#http-api)
- [Storage & database schema](#storage--database-schema)
- [How the tricky bits work](#how-the-tricky-bits-work)
- [Troubleshooting](#troubleshooting)
- [Disclaimer](#disclaimer)
- [License](#license)

---

## What it does

**Live telemetry**
- Real-time job state, progress %, current/total layers, time remaining, stage
- Both nozzles (current + target temperature, role, active flag), heated bed,
  heated chamber
- All four fans (part cooling, two aux, heat-break) on a normalized 0–100 % scale
- Wi-Fi signal, firmware version, print speed level and magnitude
- AMS: per-slot filament type, brand, colour, remaining % / grams / metres,
  humidity, drying state; active/loading slot; external (non-AMS) spools

**Health**
- HMS health warnings surfaced in the header, each **acknowledgeable** (the X2D's
  HMS codes have no public wiki pages, so ack-and-dismiss is the useful workflow);
  acknowledgements are persisted

**Power & cost accounting** (the reason the app exists — models are sold, so cost
per print must be accurate)
- Live wattage + today/month energy from a **Tapo P110/P110M** smart plug on the
  printer's outlet
- Cost = **electricity + filament**, per print, persisted to the database
- Filament pricing is a **brand × material matrix** (Bambu vs third-party, PLA vs
  PETG, …), because genuine Bambu spools cost roughly double; per-slot / per-type /
  per-filament-id overrides are supported
- Cost tile splits spend into **today / this week / this month** and shows the last
  print, with money always rounded **up** to 2 decimals (a quote must never come
  out under the real cost)

**Print history**
- Automatic per-print rows (start/end, final state, layers, energy, filament used,
  cost breakdown)
- **Editable job names** (the slicer's subtask name is often not descriptive)
- Click-to-override filament grams when the estimate is off
- Finished jobs are **enriched from the Bambu Cloud** (accurate weights, timing,
  completion status)

**Recording modes** (three-state toggle in the header)
- **Auto** — record only while a print is active (+ a cool-down tail), so the NAS
  disks can hibernate the rest of the time
- **On** — always record
- **Off** — stop recording **and fully disconnect** the MQTT stream so the app goes
  completely idle (no disk writes, no network chatter)

**Machine controls & inspection**
- **Chamber LED** on/off toggle in the header (published over MQTT)
- **Raw printer data** browser showing the complete last report from the printer,
  with keys **colour-coded green when the app already consumes them** and white
  when they're still untapped — a live map of what's available to build next

**Live view** (optional)
- The printer's built-in camera in a **Live view** tab — fully local, relayed
  through a bundled **go2rtc** binary (no cloud, no Docker). Hidden unless a
  camera is configured. See [Live view (camera)](#live-view-camera).

**UI**
- Tabbed layout: Overview / Machine / Print history (+ Live view when enabled)
- Live updates via Server-Sent Events (no polling from the browser)
- **German by default with a DE/EN switcher**
- Light/dark theme toggle
- Charts for temperature and power history rendered as inline SVG (no external JS)

---

## Screenshotted at a glance

<img width="1846" height="821" alt="bambuapp" src="https://github.com/user-attachments/assets/dc211da9-0c90-4a82-83dd-ead3afad013e" />

<img width="1829" height="758" alt="bambuapp2" src="https://github.com/user-attachments/assets/61fc2555-5dfe-482c-b412-285961eff1e3" />

<img width="1842" height="516" alt="bambuapp3" src="https://github.com/user-attachments/assets/2acc2708-de20-4674-9870-4ab5cee69df0" />



---

## Architecture

A single Python process. One background thread owns the printer connection and
keeps the latest normalized state in memory; Flask serves the dashboard and pushes
every state change to connected browsers over SSE. Additional daemon threads poll
the smart plug, poll the cloud, and purge old rows.

```
                       ┌──────────────────────── app.py (Flask, one process) ─────────────────────────┐
                       │                                                                               │
   Bambu X2D ──MQTT/TLS│──► mqtt_worker ──► bambu_state.parse_report() ──► _state (in-memory) ──┐      │
   :8883 (local)       │        │                                              │                │      │
                       │        └──────────────── publish LED / pushall ◄──────┘                ▼      │
                       │                                                               _publish_state  │
   Tapo P110M ──local──│──► power_worker ─────────────────────────────────────────────►  │  (SSE fan- │
   API (async)         │                                                                  │   out)     │
                       │                                                                  ▼            │
   Bambu Cloud ──HTTPS─│──► cloud_worker ──► enrich finished prints ──────────►  storage.py           │
   /v1/user-service    │                                                          (sqlite | mariadb)   │
                       │                                                                  │            │
                       │   purge_worker ──► delete rows older than retention_days ────────┘            │
                       │                                                                               │
                       │   Flask routes: / (dashboard.html) · /api/* · /events (SSE) ──► Browser       │
                       └───────────────────────────────────────────────────────────────────────────────┘
```

**Threads**
| Thread          | Job                                                                  |
| --------------- | -------------------------------------------------------------------- |
| `mqtt_worker`   | Owns the TLS MQTT connection; parses reports into `_state`. Parks entirely while recording is **Off**. |
| `power_worker`  | Polls the Tapo plug every `poll_sec` for watts / today / month energy. |
| `cloud_worker`  | Every `poll_min`, and kicked early when a job finishes, enriches finished prints from the Bambu Cloud. |
| `purge_worker`  | Once a day, deletes telemetry older than `retention_days`.           |

The **frontend never polls**: it opens one `EventSource("/events")` and re-renders
whenever the server pushes a new state.

---

## Technology stack

| Layer            | Choice                                                                 |
| ---------------- | --------------------------------------------------------------------- |
| Language         | Python 3 (3.9 on the Synology, 3.13 for local dev)                    |
| Web framework    | **Flask** (threaded), Server-Sent Events for live push               |
| Printer link     | **paho-mqtt 2.x** over TLS (`ssl`), local MQTT — no cloud dependency for telemetry |
| Smart plug       | **`tapo`** library (async; driven from a thread)                     |
| Camera relay     | **go2rtc** (single static binary, no Docker) — RTSPS → WebRTC/MSE, launched & supervised by `app.py` |
| Database         | **SQLite** (local dev) / **MariaDB** via **PyMySQL** (production on NAS) |
| Frontend         | Single hand-written `dashboard.html` — vanilla JS, CSS Grid, inline-SVG charts. **No build step, no framework, no external assets** (matters behind a strict/offline NAS). |
| i18n             | English strings are the keys; a `DE` dictionary provides German; missing entries degrade to English |
| Process mgmt     | POSIX `sh` launcher + pidfile, driven by Synology Task Scheduler (boot + 5-min watchdog) |

Design constraints that shaped these choices:
- **Runs unattended on a NAS** → single self-contained process, watchdog restart,
  no PC involvement.
- **Lets the NAS disks hibernate** → per-request access logging is silenced, and
  the **Off** recording mode drops the connection entirely so nothing wakes the disks.
- **No external frontend assets** → everything inlined in one HTML file.

---

## Where the data comes from

### 1. Local MQTT (primary — telemetry)
- Broker: the printer itself, **`mqtts://<printer-ip>:8883`**
- Auth: username `bblp`, password = the printer's **LAN Access Code**
- TLS is used but the printer's cert is self-signed → verification disabled
  (`CERT_NONE`, `tls_insecure_set(True)`)
- Subscribe: `device/<serial>/report` (JSON, ~1 Hz)
- Publish: `device/<serial>/request` — e.g. a `pushall` to request a full state
  dump, or the `system`/`ledctrl` command to toggle the chamber light
- No browser web UI exists on the printer, and telemetry works even in Cloud mode.

### 2. Tapo smart plug (power)
- A **Tapo P110/P110M** on the printer's outlet, queried on the LAN via the `tapo`
  library using the Tapo account email/password.
- Gives instantaneous watts plus today/month cumulative energy, which is turned
  into € using `cost.price_per_kwh`.

### 3. Bambu Cloud (enrichment, optional)
- `bambu_cloud.py` logs into the Bambu account and reads
  `/v1/user-service/my/tasks` to get authoritative finished-job data (weights,
  timing, completion status 2 = complete / 4 = running).
- Used only to **enrich finished prints** — never to drive the live view, and
  guarded so it can't mark a still-running job as failed.

---

## Project layout

| File                          | Purpose                                                                 |
| ----------------------------- | ----------------------------------------------------------------------- |
| **Core runtime** (root)       | The running app — these stay in root; `app.py` imports the modules and serves the HTML. |
| `app.py`                      | The application: Flask server, MQTT/power/cloud/purge/go2rtc worker threads, all API endpoints, SSE. |
| `bambu_state.py`              | Pure parser: turns a raw Bambu MQTT report into a clean, stable state dict. No I/O. |
| `storage.py`                  | Storage abstraction with two backends (sqlite / mariadb), schema, migrations, per-print upserts. |
| `bambu_cloud.py`              | Bambu Cloud client (login, task list) for finished-print enrichment.    |
| `dashboard.html`             | The entire frontend (HTML + CSS + JS + i18n) in one file.               |
| `printer.config.json`         | **All configuration and secrets** (printer, plug, cost, filament, storage, cloud, camera). Not committed. |
| `requirements.txt`            | Python dependencies.                                                    |
| `go2rtc/`                     | The go2rtc relay binary for the camera Live view (downloaded per-arch; not committed). |
| **`tools/`**                  | Dev & one-time setup helpers — not part of the running app.             |
| `tools/setup_cloud.py`        | Interactive Bambu Cloud login → stores an auth token in the config.     |
| `tools/setup_power.py`        | Verifies Tapo plug connectivity and credentials.                        |
| `tools/test_mqtt_local.py`    | Standalone check that local MQTT works against the printer.             |
| `tools/capture_sample.py`     | Captures a real report to `samples/sample_report.json` for offline parser testing. |
| `tools/explore_ftps.py`       | Explores the printer's FTPS file store (models/thumbnails).            |
| `samples/`                    | Captured payloads used by `bambu_state.py`'s self-test (not committed).  |
| **`deploy/`**                 |                                                                         |
| `deploy/start.sh`             | Idempotent POSIX launcher (pidfile + `kill -0`), supports a `restart` arg. |
| `deploy/DEPLOY.md`            | Step-by-step NAS deployment notes.                                      |
| `deploy/schema_and_user.sql`  | Creates the MariaDB database, tables and app user.                     |
| `deploy/sqlite_to_mariadb.py` | One-shot migration of a local sqlite DB into MariaDB.                  |
| `deploy/recalc_print_energy.py` | Backfills/recomputes per-print energy after pricing changes.         |

---

## Configuration

Everything lives in **`printer.config.json`** (kept out of version control — it holds
secrets). To get started, copy the template and fill it in:

```bash
cp printer.config.example.json printer.config.json
```

Structure, with placeholders:

```jsonc
{
  "ip": "192.168.x.x",            // printer LAN IP
  "access_code": "········",       // printer LAN Access Code (Settings → Network)
  "serial": "········",            // printer serial, used to build MQTT topics
  "model": "X2D",

  "power": {                       // Tapo smart plug (optional)
    "enabled": true,
    "model": "p110",
    "host": "192.168.x.x",         // plug IP
    "email": "tapo-account@…",
    "password": "········",
    "poll_sec": 20
  },

  "cost": {
    "currency": "€",
    "price_per_kwh": 0.30
  },

  "filament": {                    // brand × material price matrix (€/kg)
    "bambu": { "PLA": 24.99, "PETG": 27.99, "default": 24.99 },
    "other": { "PLA": 14.50, "PETG": 16.90, "default": 14.50 },
    "default_per_kg": 20.0,
    "per_slot": {},                // optional overrides, keyed by AMS slot
    "per_filament_id": {},         // …by slicer filament id
    "per_type": {}                 // …by material type
  },

  "storage": {
    "backend": "sqlite",           // "sqlite" for dev, "mariadb" on the NAS
    "sqlite_path": "telemetry.db",
    "sample_interval_sec": 20,     // how often a telemetry row is written
    "retention_days": 30,          // purge_worker deletes older rows
    "auto_tail_min": 10,           // in Auto mode, keep recording this long after a print
    "mariadb": {
      "host": "127.0.0.1", "port": 3306,
      "user": "bambu", "password": "········", "database": "bambu_monitor"
    }
  },

  "cloud": {                       // Bambu Cloud enrichment (optional)
    "enabled": true,
    "email": "bambu-account@…",
    "password": "········",
    "token": "<filled in by setup_cloud.py>",
    "poll_min": 10
  }
}
```

Notes:
- **The listening port** defaults to `8770`; override with the `BAMBU_PORT`
  environment variable.
- `cloud.token` is populated by running `setup_cloud.py` so the password isn't
  re-sent on every start.
- To turn off a subsystem, set its `enabled` to `false` — the corresponding worker
  thread simply isn't started.

---

## Running locally

```bash
# 1. dependencies
pip install -r requirements.txt

# 2. configure
cp printer.config.example.json printer.config.json   # then edit it
#    backend "sqlite" (the default) needs nothing else; power/cloud are off by default

# 3. run
python app.py
#    → serves http://localhost:8770, connects to the printer, creates telemetry.db
```

Handy checks before/while developing:
```bash
python tools/test_mqtt_local.py <ip> <access-code> <serial>   # confirm local MQTT works
python tools/setup_power.py       # confirm the Tapo plug answers
python tools/setup_cloud.py       # log into Bambu Cloud, store token
python bambu_state.py             # run the parser self-test against samples/
```

The parser (`bambu_state.py`) is pure and has a self-test with assertions, so you
can iterate on it against captured JSON without a printer attached.

---

## Deploying to the Synology NAS

Full detail is in [`deploy/DEPLOY.md`](deploy/DEPLOY.md); the essentials:

1. **Code** lives in `/volume1/apps/bambu-monitor/` with a Python virtualenv at
   `venv/` (the launcher runs `venv/bin/python3`).
2. **Database:** run `deploy/schema_and_user.sql` on the NAS MariaDB to create the
   `bambu_monitor` database and app user, then set `storage.backend` to `"mariadb"`
   in the config. To carry local data over, run `deploy/sqlite_to_mariadb.py`.
3. **Autostart:** in *Control Panel → Task Scheduler*, add
   - a **Boot-up** triggered task (user `root`) running `sh /volume1/apps/bambu-monitor/start.sh`
   - a **scheduled** task every 5 minutes running the same line (a watchdog —
     `start.sh` is idempotent and only starts the app if it isn't already running).
4. **Updating:** copy the changed files over, then
   ```sh
   sh /volume1/apps/bambu-monitor/start.sh restart
   ```
   The `restart` arg kills the running instance and **waits for it to exit** before
   starting the new one (a plain kill-then-start races and can silently start nothing).

> **Editing files on Windows:** never round-trip `dashboard.html` / the Python
> sources through PowerShell `Get-Content -Raw` + `Set-Content` — PowerShell 5.1
> mis-encodes UTF-8 and double-encodes every `°`, `·`, `€`, `⚠`, umlaut, etc. Edit
> with a UTF-8-aware editor.

---

## Live view (camera)

An optional **Live view** tab shows the printer's built-in camera, streamed
**entirely locally** — no cloud, no Docker. It's hidden unless a camera is
configured, so it never affects installs that don't use it.

### How it works

The X2D exposes its camera as an **RTSPS (RTSP-over-TLS) stream on port 322**
(`rtsps://bblp:<access-code>@<ip>:322/streaming/live/1`, H.264). Browsers can't
play RTSPS directly, so a tiny relay — **[go2rtc](https://github.com/AlexxIT/go2rtc)**,
a single static binary — converts it to browser-native **WebRTC/MSE** (H.264
passthrough, no transcoding, so it's light on the NAS CPU).

`app.py` owns the relay end-to-end: it generates `go2rtc.yaml` from
`printer.config.json` (so the access code never lives in a second file) and
launches + supervises the go2rtc process. The dashboard's Live view tab embeds
go2rtc's player, and only connects **while the tab is open** so the camera isn't
streamed 24/7.

```
Printer ──RTSPS:322 (H.264)──► go2rtc ──WebRTC/MSE:1984──► Live view tab
         (LAN Mode Live View on)   (relay, on the NAS)      (browser)
```

### Setup

1. **On the printer:** enable **"LAN Mode Live View"** on the touchscreen (on the
   X2D it's under the **LAN Only** section, but it does **not** require LAN-Only
   mode — it works with Cloud mode on). This makes the printer advertise
   `rtsp_url` and serve port 322.
2. **Get the relay binary:** download the `go2rtc` build for your NAS architecture
   into `go2rtc/` (ARM64 Synology → `go2rtc_linux_arm64`; Intel → `_linux_amd64`).
   `app.py` `chmod +x`'s it on start.
3. **Enable it in config** — the `camera` block in `printer.config.json`:
   ```json
   "camera": { "enabled": true, "src": "bambu", "rtsp_port": 322,
               "api_port": 1984, "webrtc_port": 8555, "bin": "go2rtc/go2rtc_linux_arm64" }
   ```
4. **Restart** the app. The Live view tab appears automatically.

The RTSP password is the printer's **LAN access code** (not the serial), reused
from the top-level config. go2rtc uses **UDP transport** (`#transport=udp`) because
the printer's LIVE555 camera only feeds RTP over UDP, not TCP-interleaved.

### Ports & firewall

go2rtc listens on **1984** (player/MSE, TCP) and **8555/UDP** (WebRTC). On a home
LAN with the Synology firewall off, nothing extra is needed. If the firewall is on,
allow those, and allow inbound from the printer's IP (the camera's UDP RTP uses
ephemeral ports).

### Verify from the NAS

No browser or ffmpeg needed — pull a few seconds of H.264 straight from the relay:
```sh
curl -s --max-time 12 -o /tmp/cam.mp4 "http://localhost:1984/api/stream.mp4?src=bambu"; ls -l /tmp/cam.mp4
```
A file of a few hundred KB (or more) = video is flowing.

### ⚠ One camera connection at a time

The printer serves **only one** RTSPS/LAN camera client at once. If the Bambu Handy
app, Bambu Studio, or a second tool is viewing the camera, the app's relay gets a
negotiated-but-silent session (connects, authenticates, then no frames). Symptoms of
a **stuck/contended slot**: the RTSP handshake succeeds (`PLAY → 200 OK`) but no
video arrives. Fixes: close other viewers; if it stays wedged, power-cycle the
printer (and re-enable LAN Mode Live View, which a reboot can revert).

---

## HTTP API

| Method & path              | Purpose                                                             |
| -------------------------- | ------------------------------------------------------------------- |
| `GET /`                    | The dashboard page.                                                 |
| `GET /api/state`           | Latest normalized state as JSON.                                    |
| `GET /events`              | **Server-Sent Events** stream; pushes state on every change.        |
| `GET /api/history`         | Telemetry time-series (`?hours=` window) for the charts.            |
| `GET /api/prints`          | Recent print history (`?limit=`).                                   |
| `GET /api/raw`             | `{ data, covered }` — the full last printer report plus the list of keys the app consumes (drives the green/white highlighting). |
| `GET /api/camera`          | `{ enabled, api_port, src }` — whether the Live view tab shows and how to reach the go2rtc relay (no secrets). |
| `POST /api/recording`      | Set recording mode `{ "mode": "auto"｜"on"｜"off" }` (persisted).    |
| `POST /api/led`            | Chamber light `{ "mode": "on"｜"off" }` (needs a live connection).   |
| `POST /api/prints/label`   | Rename a print (editable job name).                                 |
| `POST /api/prints/filament`| Override the filament grams for a print.                            |
| `POST /api/cloud/refresh`  | Trigger an immediate Bambu Cloud enrichment pass.                   |
| `POST /api/hms/ack`        | Acknowledge / restore an HMS health warning.                        |

---

## Storage & database schema

One `Storage` object; the backend is chosen by config, so identical code runs on
sqlite (dev) and MariaDB (prod). Missing columns are added on startup via a
migration list, so the schema can evolve without manual `ALTER`s.

- **`telemetry`** — flat time-series, one row every `sample_interval_sec`:
  `ts, gcode_state, percent, layer, total_layers, bed_cur, bed_tgt, noz0, noz1,
  noz_tgt, chamber, fan_cooling, speed_mag, wifi_dbm`.
- **`prints`** — one summary row per job: identity (`job_id`, `name`,
  `design_title`), timing (`started_at`, `ended_at`), `final_state`, `total_layers`,
  and the cost fields (energy Wh, filament grams, per-material price, computed cost).
  Some columns are **immutable once set** (start time, label, filament identity,
  error code) to keep enrichment from clobbering user edits or live data.
- **`settings`** — small key/value store (e.g. the persisted recording mode, HMS
  acknowledgements).

`purge_worker` trims `telemetry` beyond `retention_days`.

---

## How the tricky bits work

A few behaviours are non-obvious because the printer's raw data is messy:

- **Packed temperatures.** Once a target is set, the X2D packs current + target into
  one integer as `(target << 16) | current`. `bambu_state._temp_pair()` detects the
  high bits and unpacks it (a raw value like `14418140` is 220 °C now / 220 °C
  target — without unpacking you'd see nonsense like "15073468 °C").

- **Reversed nozzle numbering.** The firmware numbers extruders the opposite way to
  how the X2D is labelled: firmware id **1** is the main direct-drive nozzle that
  prints the part, id **0** is the auxiliary Bowden nozzle. The app presents id 1
  first as "Nozzle 1". This is confined to one mapping table.

- **Fan scale.** Fans are reported 0–15, not 0–100; converted to a percentage.

- **Genuine vs third-party filament.** Real Bambu spools carry an RFID tag; a
  third-party spool reports an all-zero `tag_uid`. That flag — not the cloud, which
  only sees the slicer profile — is the authoritative signal used to pick the right
  price from the brand × material matrix.

- **Cost is monotonic per print.** Energy for a running print only ever increases
  and is seeded from the stored value on restart, so a restart mid-print can't reset
  a job's accumulated cost to zero, and a new job can't inherit the previous one's.

- **Cloud can't fail a live job.** The cloud reports a placeholder end time and
  `status = 4` while a job is still running; enrichment only closes a print on
  `status = 2` and never touches the currently-live job.

- **i18n by degradation.** English strings *are* the translation keys
  (`t(s) = LANG === "de" ? (DE[s] ?? s) : s`), so anything not yet translated shows
  in English rather than as a broken key. Note: `render()`'s local temp variable is
  named `tp`, **not** `t`, because `t` is the translate function — shadowing it
  blanks the whole dashboard.

- **Raw-data coverage is derived, not hand-kept.** The green/white highlighting in
  the Raw printer data view comes from scanning `bambu_state.py` for the keys it
  reads (`.get("…")`), so it can never drift out of sync — add a field to the parser
  and it turns green automatically.

---

## Troubleshooting

| Symptom                                             | Cause / fix                                                                 |
| --------------------------------------------------- | --------------------------------------------------------------------------- |
| Tiles stuck on **"Connecting…"**, no data           | Recording mode is **Off** — that intentionally disconnects the stream. Switch to Auto/On. |
| **"Stream off"** in the header                      | Expected in Off mode; the app is idle so the NAS disks can sleep.           |
| Raw printer data box is empty                       | It needs a *full* report; it fills once the printer sends a pushall/full frame. |
| Dashboard loads but stays empty / no telemetry      | Check `app.log` on the NAS for a Python traceback; confirm the printer IP/Access Code and that only one instance is running. |
| MQTT keeps connecting/disconnecting                 | Two app instances fighting over the printer's single `bblp` login — ensure the watchdog didn't spawn a duplicate (`start.sh` guards against this). |
| `#1054 Unknown column …` (MariaDB)                  | Backend is on MariaDB but the schema is behind; the column-migration runs on startup — restart the app, or check the config didn't get flipped to sqlite. |
| Garbled `°`/`€`/umlauts after editing on Windows    | A file was round-tripped through PowerShell; re-edit with a UTF-8-aware tool. |
| Cloud login fails                                   | Re-run `tools/setup_cloud.py` to refresh the stored token.                  |
| Live view spins / no picture                        | Printer serves **one** camera client at a time — close the Handy app / Bambu Studio / other tabs; if wedged, power-cycle the printer and re-enable LAN Mode Live View. Confirm with the `curl … stream.mp4` test. See [Live view](#live-view-camera). |
| Live view tab missing                               | `camera.enabled` is false, or `/api/camera` reports disabled. Set it in `printer.config.json` and restart. |

---

## Disclaimer

This is an independent, unofficial hobby project. It is **not affiliated with,
authorized, sponsored, or endorsed by Bambu Lab or TP-Link (Tapo)**. "Bambu Lab",
"X2D", "AMS", "Tapo" and other product names are trademarks of their respective
owners and are used here only to describe what this software interoperates with.

The tool talks to your own printer over its **local** MQTT interface and to a smart
plug on your own network. The optional Bambu Cloud enrichment uses an unofficial API
and may be subject to Bambu Lab's terms of service; it is **disabled by default** —
enable it at your own discretion and risk.

Provided **as is**, with no warranty (see [LICENSE](LICENSE)). You are responsible
for your own credentials and deployment. Configuration holds credentials in
plaintext (`printer.config.json`, which is git-ignored); this is fine for a trusted
LAN/NAS deployment but the app should **not be exposed to the public internet**.

## License

Released under the [MIT License](LICENSE).
