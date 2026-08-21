# Bambu Monitor

A self-hosted monitoring and cost-accounting dashboard for the **Bambu Lab X2D**
3D printer. It connects to the printer over its **local MQTT** interface, keeps a
live normalized view of everything the machine reports, records telemetry and
per-print history to a database, measures real electricity draw via a **Tapo smart
plug**, enriches finished jobs from the **Bambu Cloud**, offers **live print
controls**, and serves a single-page web dashboard.

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
- Real-time job state, progress %, current/total layers, time remaining **and
  estimated finish time** (wall-clock, with the weekday for overnight jobs), stage
- Both nozzles (current + target temperature, role, active flag), heated bed,
  heated chamber
- All four fans (part cooling, two aux, heat-break) on a normalized 0–100 % scale
- Wi-Fi signal, firmware version, print speed level and magnitude
- AMS: per-slot filament type, brand, colour, remaining % / grams / metres,
  humidity, drying state; active/loading slot; external (non-AMS) spools

**Filament identity & reordering** (genuine Bambu spools only)
- Each RFID spool is named in full: product line + **colour name** (`PLA Basic ·
  Jade White`), resolved from the spool's own colour code (`tray_id_name`, e.g.
  `A00-W01`) — the printer reports the code and the exact hex but never the name
- A **Reorder ↗** link straight to the regional Bambu store, pre-filled with the
  product line and colour, so a nearly-empty spool is one click from being reordered
- Spools at or below `filament.low_pct` (default **15 %**) are flagged **Low** and
  their tile is outlined in amber

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
- Genuine Bambu spools can instead be priced **from your own invoices** — the
  imported orders teach a per-SKU price, so the matrix stops being a number you
  maintain by hand. The **list** price is used, not the discounted one: a one-off
  discount is not what replacing that spool will cost
- Cost tile splits spend into **today / this week / this month** and shows the last
  print, with money always rounded **up** to 2 decimals (a quote must never come
  out under the real cost)

**Print history**
- Automatic per-print rows (start/end, final state, layers, energy, filament used,
  cost breakdown)
- **Editable job names** (the slicer's subtask name is often not descriptive)
- Click-to-override filament grams when the estimate is off
- **Delete a row** (✕ on row hover, behind a confirm) — for test/showcase prints
  started at the printer itself that shouldn't count toward stats or cost; a
  running print is refused, and the deleted row can't be re-created by the next
  telemetry tick (see [How the tricky bits work](#how-the-tricky-bits-work))
- Finished jobs are **enriched from the Bambu Cloud** (accurate weights, timing,
  completion status)
- **Manual grouping** — one model is often several prints, so tick the rows and
  give them a group name. Grouped prints fold under a collapsible header carrying
  the group's own subtotals (prints, failures, time, filament, energy, cost). The
  header sits at the position of its newest member, so the history still reads
  chronologically. Renaming is a click on the ✎; clearing the name dissolves the
  group
- Adding to an existing group is a **drag**: grab a row by its handle and drop it
  on the group (header or any member). Dragging one of several selected prints
  carries the whole selection, so a batch is still one gesture
- Four **views**, remembered: *Timeline* (everything by date), *Groups first*
  (projects, a divider, then single prints), *Only groups* — the collapsed headers
  read as a project list — and *Only singles*. The summary line counts what is on
  screen, not what exists
- **MakerWorld links** — jobs sliced from MakerWorld show a link straight to the
  source model (deep-linked to the exact print profile), on the live job tile,
  each history row and the expanded print; the `design_id`/`profile_id` are
  captured to the database.
  `design_id` is **machine state, not job state**: the printer keeps reporting
  the last model it printed and never clears it for a self-sliced job, so a print
  started afterwards used to inherit the previous print's link. Only an id that
  *changed* since the job began is attributed to it. An id identical to the
  previous job's is ambiguous — a repeat print and a leftover look the same from
  the printer — so it is refused live and recovered afterwards from the cloud's
  per-task **design title**, which was never wrong. Rows already stored with an
  inherited link are repairable with
  [`tools/fix_design_ids.py`](tools/fix_design_ids.py) (reports by default,
  `--apply` clears)

**Filament section**
- A historical overview of **every filament ever used**: grams, material cost,
  number of prints, share of total consumption, and when it was last used
- Populated **retroactively** — usage is summed from the per-slot detail already
  stored on every past print, so the page is complete the moment it ships and can
  never drift out of step with the print history
- Identities are **remembered in the database** (`filaments` table), so a spool
  keeps its product line, colour code and colour name long after it has been used
  up and thrown away
- **Editable naming**: click a row's vendor or name to set **vendor / product /
  colour** by hand. This is the only way a third-party spool gets a name at all —
  the printer reports a borrowed Bambu profile and a colour, never a manufacturer
  — and it is what lets purchases match it, wording being the sole route without a
  colour code. Genuine spools show *Bambu Lab* without anyone typing it
- Shows which filaments are **in the AMS right now** (slot, remaining %, Low flag)
  with the same **Reorder ↗** link, so the page doubles as a shopping list.
  A spool only shows a slot when the identity of the live tray is the *same*
  identity as the row, and for a third-party spool the two sources can disagree
  (the AMS reports whatever profile is set on the printer, the cloud reports the
  one you sliced with). [`tools/why_not_in_ams.py`](tools/why_not_in_ams.py) asks
  the running app what is in each tray, works out the identity that makes, and
  names the near-miss row when it differs — that split is what **merge** is for
- **Search and sort**: type to match vendor, product, colour name, colour code,
  hex or material; narrow to *In AMS · Low · unused*; click any column header to
  sort by it. Both are applied to the data already loaded — no refetch — and the
  scope and sort column are remembered
- A **purchase log**: drop a Bambu **invoice PDF** on the page and it reads the
  line items straight out of the invoice table — product, colour code, colour
  name, quantity, weight and the price *actually paid* after discounts. Pasting
  the text of an order confirmation and adding lines by hand both work too.
  Nothing is stored until you confirm the parsed lines
- Because the invoice's SKU prefix (`A19-K00`) **is** the code the AMS reports as
  `tray_id_name`, an imported order matches your spools exactly rather than by
  wording — and **teaches the app colour names** it had no way to know
  (`A19-K00` → *Absolute Black*), which then show up on the AMS tiles
- Purchases are **grouped by your choice** of product line, material, colour or
  order (or flat). Groups start **folded in** — the headers alone are the overview,
  each carrying its subtotal (lines, spools, weight, spend, average €/kg) — and you
  open the one you want to inspect. The choice of grouping is remembered
- With purchases logged the page shows **bought vs used vs left** per filament,
  and the **price actually paid per kg** — which is the real number, rather than
  the nominal one from the config price matrix

**Statistics** (own tab)
- Lifetime analytics over the whole print history: total prints, **success rate**,
  total print time (+ average), filament used (kg + material €), energy (kWh +
  power €) and total cost
- A **per-day chart** over 30 days / 90 days / a year, switchable between prints,
  filament, print time, energy and cost. Days with nothing on them are drawn as
  gaps rather than skipped, so a quiet fortnight looks quiet
- A **per-month** trend (prints + cost) and the **most-printed models** (grouped by
  MakerWorld model, with count / filament / cost)

**Maintenance reminders** (own tab)
- Tracks **cumulative print hours** and flags upkeep tasks — general clean, clean &
  lubricate the X/Y axes, lubricate the X/Y idler pulleys, clean & lubricate the Z
  lead screws, clean fans/filter, hotend cold pull — as OK / due-soon / overdue,
  each **linking to its X2D wiki page**
- Bambu's X2D intervals are **calendar-based** (X/Y ~2 months, Z ~4 months at
  regular use), converted to print-hours here (~3 h/day); intervals are **editable**
  and a **Done** button resets a task's clock at the current hour-mark

**Notes** (own tab)
- A scratchpad kept in the database: plain text, links and YouTube links, with an
  optional title per note. Edit and delete in place, newest-touched first
- **Categories** — free text with autocomplete, suggested as *General · Tips &
  Tricks · Settings · Filament · Troubleshooting · Models & Ideas* but not limited
  to them. Filter chips with counts sit above the list (plus **Other** for
  uncategorised), and the chosen filter is remembered
- URLs become links automatically; YouTube ones are marked with a ▶. Nothing is
  embedded and no thumbnails are fetched — the dashboard stays free of external
  assets, which is what lets it work on an offline NAS
- **Pictures**, stored in the database with the note, so one backup covers both.
  The browser downscales before uploading (max 1400 px, JPEG ~82 %), which turns
  a 6 MB phone photo into a few hundred KB; small PNG/WebP/GIF pass through
  untouched so transparency and animation survive. Click a thumbnail for full size

**Recording modes** (three-state toggle in the header)
- **Auto** — record only while a print is active (+ a cool-down tail), so the NAS
  disks can hibernate the rest of the time
- **On** — always record
- **Off** — stop recording **and fully disconnect** the MQTT stream so the app goes
  completely idle (no disk writes, no network chatter)

**Machine controls & inspection** (all over local MQTT, behind a **strict
server-side allowlist** — never a free-form gcode passthrough)
- **Print flow:** Pause / Resume / Stop, in the job tile only while a print is
  active (Stop behind a confirm; Pause↔Resume is one context-aware button)
- **Speed profile:** Silent / Standard / Sport / Ludicrous
- **Fans:** part-cooling / aux / chamber **sliders** (heat-break stays
  firmware-managed); the live refresh won't yank a slider mid-drag. Same gate as
  the setpoints — with `controls.allow_gcode` off the readings show without
  sliders
- **Temperature setpoints:** heated **bed** (`M140`) and **chamber** (`M141`),
  clamped to safe ranges (bed 0–120, chamber 0–60 °C; 0 = off) — **off by
  default**, rejected by current firmware, see
  [MQTT command verification](#mqtt-command-verification)
- **Assign a filament to an AMS slot** — built, but **off by default and it does
  not work on current firmware**. See
  [MQTT command verification](#mqtt-command-verification) below
- **Chamber LED** on/off toggle in the header
- **Raw printer data** browser showing the complete last report from the printer,
  with keys **colour-coded green when the app already consumes them** and white
  when they're still untapped — a live map of what's available to build next

**Live view** (optional)
- The printer's built-in camera, relayed through a bundled **go2rtc** binary
  (fully local: no cloud, no Docker). Hidden unless a camera is configured. See
  [Live view (camera)](#live-view-camera).
- It has a **Live** section of its own — the thing you leave open on a second
  screen, one click away rather than two levels down. The section only appears
  when a camera is configured, and the stream is connected only while it is on
  screen, never in the background
- **Remaining time, end time and layer** sit on its caption line, so the camera
  can be watched without switching away

**UI**
- **Sections, each with its own views** — the page is organised by the thing you
  are looking at rather than by one flat row of tabs:

  | Section | Views |
  |---|---|
  | **Printer** | Now · Hardware · Raw data |
  | **Prints** | History · Statistics |
  | **Filament** | Inventory · Purchases |
  | **Workshop** | Maintenance · Notes |
  | **Live** | the camera, when one is configured |

  One `NAV` map in the page script drives the second-level bar, the routing and
  the address bar, so a view cannot exist in one and be missing from another.
  Every view is linkable (`#prints/history`) and the back button works. A section
  with a single view gets no second-level bar at all.
- **Printer · Now** puts the job, the temperatures, the AMS and both charts
  (temperature and power) on one screen. With nothing printing it reports what
  the machine **is** and what it printed last, instead of an empty progress ring.
- **Printer · Hardware** holds the standing facts about the machine — identity,
  AI monitoring, and the fans.
- **The browser tab is a status light.** This page spends most of its life in the
  background, so the print's state is readable from the tab strip itself:
  - the **title** carries it in words — `68% · Dragon_body`, then
    `✓ Finished · Dragon_body` (translated, so `✓ Fertig …` in German)
  - the **favicon** is drawn on a canvas at runtime: a ring that fills while
    printing, closing into a green tick when the job is done or a red ✕ when it
    failed. Idle restores the normal printer glyph. No asset, no request.
  - a finish that happens **while the tab is hidden is latched**: the printer
    drops back to `Idle` a while after a print ends, and without the latch the
    one thing you left the tab open for would vanish before you looked. It clears
    on the next `visibilitychange`, i.e. the moment you look at the tab.

  A desktop notification would be the obvious alternative, but the Notification
  API needs a **secure context** and the NAS is served over plain `http://`, so
  the tab is the whole channel. `scratchpad/bm/t_tabflag.js` drives the real
  function through a print's life to check the latch survives the drop to `Idle`.
- **Prints · History** is the per-day chart and the table together: **clicking a
  bar filters the table to that day**, and clicking it again clears it.
- **Every print expands** into one panel holding its run, its per-slot filament
  with colours, its energy and cost breakdown, its group, its slicer profile and
  its MakerWorld link — facts that used to be spread over five tabs and a
  tooltip.
- **Filament · Purchases** keeps the invoice importer folded away behind a
  button: the list is what you come to read, the importer is what you
  occasionally come to use.
- **Two layouts**, swapped with the ✦ button. They differ in structure, not just
  styling, so they are two documents rather than one document with a CSS layer:
  - `dashboard.html` — the four sections above.
  - `classic.html` — the previous eight-tab page, **frozen**. It is the way back,
    not a second thing to maintain; fixes land in `dashboard.html`.

  The button sets a `bambu_page` cookie and reloads; `/` reads that cookie and
  serves whichever was chosen last, so the choice survives a restart and needs no
  server-side state. `scratchpad/bm/t_layout.js` checks that both documents exist,
  reach each other, and that no card was lost in the move.
- **Responsive header** — below 1150 px the section bar moves onto a full-width
  row of its own and the serial/firmware line steps aside; below 560 px the
  last-update stamp folds away too, so the chips still fit on a phone
- Live updates via Server-Sent Events (no polling from the browser)
- **German by default with a DE/EN switcher**
- Light/dark theme toggle
- Non-blocking **toast** confirmations for actions that change stored data (the
  deleted row animates out before the table reloads), with errors shown in the
  same strip instead of a browser `alert()`
- Charts for temperature and power history rendered as inline SVG (no external JS)

---

## Screenshotted at a glance

Overview:
<img width="1999" height="1121" alt="overview" src="https://github.com/user-attachments/assets/ea4019c7-f876-4b81-9d98-b8095be4118b" />

Machine page:
<img width="2023" height="824" alt="machine" src="https://github.com/user-attachments/assets/256139ec-9f66-4930-9732-73da9322a894" />

Maintenance:
<img width="2014" height="642" alt="maintenance" src="https://github.com/user-attachments/assets/16aa5446-d695-454d-b8bb-97366646067d" />

Print history:
<img width="2019" height="567" alt="print_history" src="https://github.com/user-attachments/assets/f88c826f-99f5-4c5a-abda-04c1a296c749" />

Statistics:
<img width="2025" height="609" alt="statistics" src="https://github.com/user-attachments/assets/cfa53b64-40b0-4d30-85d3-6522b75d2842" />

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
| Invoice import   | **pypdf** — optional, pure Python (no compiler needed on the NAS); absent it, the paste path still works |
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
- Publish: `device/<serial>/request` — a `pushall` to request a full state dump,
  the `system`/`ledctrl` command to toggle the chamber light, and **print controls**
  (`pause` / `resume` / `stop`, `print_speed`, and `gcode_line` for fan speeds
  `M106` and bed/chamber temperatures `M140` / `M141`)
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
| `storage.py`                  | Storage abstraction with two backends (sqlite / mariadb), schema, migrations, per-print upserts/deletes, filament identities. |
| `bambu_cloud.py`              | Bambu Cloud client (login, task list) for finished-print enrichment.    |
| `filament_catalog.py`         | Colour-code → colour-name table, regional Bambu store search links, and the best-effort order-confirmation parser. Has a self-test (`python filament_catalog.py`). |
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
| `tools/dump_cloud_tasks.py`   | Read-only: cloud tasks next to the stored prints. Diagnoses why an orphaned print didn't close. |
| `tools/dump_filaments.py`     | Read-only: every filament identity, where it came from, and which look like the same spool split in two. |
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
    "per_type": {},                // …by material type

    "prices_from_orders": true,    // price Bambu spools from your own invoices
                                   //   (list price per SKU) instead of the
                                   //   bambu/other matrix above; false = off
    "low_pct": 15,                 // at/below this %, a spool is flagged "Low"
    "store_region": "eu",          // eu | us | uk | jp | global (reorder links)
    "color_names": {               // adds to / corrects the built-in table
      "A00-N04": "Cocoa Brown"     //   key = the spool's tray_id_name code
    }
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

The tab's caption line also carries **Remaining** and **Ends** — the same two
values as the overview hero, fed by the same SSE state, so watching the print
doesn't mean tabbing back and forth.

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
| `POST /api/ams/filament`   | Tell the printer what is loaded in a slot `{ "slot": 1, "fkey": … }`. **Disabled by default** (`filament.allow_slot_assign`) — current firmware rejects it, see [MQTT command verification](#mqtt-command-verification). |
| `POST /api/print/control`  | Print controls `{ "action": "pause｜resume｜stop｜speed｜fan｜temp", … }` — strict allowlist (speed 1–4, fan `cooling｜aux1｜aux2` %, temp `bed｜chamber` °C). Needs a live connection. |
| `GET /api/stats`           | Lifetime analytics from the prints table (totals, success rate, per-day, per-month, top models). |
| `GET /api/notes`           | All notes, newest-touched first.                                    |
| `POST /api/notes`          | Create a note, or update one when an `id` is included.              |
| `POST /api/notes/delete`   | Delete a note `{ "id": … }`, and its pictures with it.              |
| `POST /api/notes/image`    | Attach a picture (`multipart`: `file`, `note_id`). JPEG/PNG/WebP/GIF, 3 MB ceiling. |
| `GET /api/notes/image/<id>`| The picture bytes, cached immutably.                                |
| `POST /api/notes/image/delete` | Remove one picture `{ "id": … }`.                               |
| `GET /api/filaments`       | Per-filament consumption (grams, cost, prints, share, last used) joined with the stored identities, the purchase log and what is loaded in the AMS right now. |
| `POST /api/filaments/merge` | Fold one identity into another `{ "from": …, "into": … }`; a blank `into` unmerges. Refuses self-merges and alias loops. |
| `POST /api/filaments/identity` | Name a filament `{ "fkey": …, "vendor": …, "product": …, "color_name": … }`. Creates the identity row if only the print history knew it; empty values clear a field. |
| `POST /api/purchases`      | Log one or more order lines `{ "lines": [ … ] }` (or a single line object). |
| `POST /api/purchases/parse`| Read an order: an uploaded invoice PDF (`multipart`, field `file`) or pasted text (`{ "text": … }`). **Stores nothing** — returns suggested lines for the user to confirm. |
| `POST /api/purchases/delete` | Remove one order line `{ "id": … }`.                            |
| `GET /api/maintenance`     | Maintenance tasks with hours-since / due status, driven by cumulative print hours. |
| `POST /api/maintenance/reset` | Mark a maintenance task done (resets its clock to the current hours). |
| `POST /api/maintenance/config`| Edit a task's interval (or the baseline hour offset).             |
| `POST /api/prints/label`   | Rename a print (editable job name).                                 |
| `POST /api/prints/group`   | Put prints in a named group `{ "job_ids": […], "name": "…" }`; a blank name ungroups them. |
| `POST /api/prints/error`   | Set or clear a print's failure code `{ "job_id": …, "code": "" }`. |
| `POST /api/prints/finish`  | Close a print that never got an end time `{ "job_id": …, "minutes": … }` — for when the app itself was down mid-job. Refuses a genuinely running print. |
| `POST /api/prints/filament`| Override the filament grams for a print.                            |
| `POST /api/prints/delete`  | Delete one print from the history `{ "job_id": … }`. Refuses a currently-running job with **409**; the telemetry time-series is left untouched. |
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
  `design_title`, and the MakerWorld `design_id` / `profile_id`), timing
  (`started_at`, `ended_at`), `final_state`, `total_layers`, the user's group name
  (`pgroup`), and the cost fields
  (energy Wh, filament grams, per-material price, computed cost). Some columns are
  **immutable once set** (start time, label, filament identity, error code) to keep
  enrichment from clobbering user edits or live data. Rows can be deleted from the
  history page; that removes only the summary row — `telemetry` is a plain time
  series and isn't owned by any single job.
- **`filaments`** — one row per filament identity: `fkey` (SKU + colour, the
  aggregation key), SKU, colour code, **vendor**, product line, hex, colour name,
  material, `is_bambu`, `first_seen` / `last_seen`. Written by the AMS observer,
  and by hand from the Filament page — the user's fields (`vendor`, `product`,
  `color_name`) are never blanked by an observation, and a row is created on
  demand when only the print history knew that filament. Identity only — **no
  usage counters**, which are aggregated from `prints.filament_detail` instead so
  the two can never disagree. Updates never overwrite a populated field with a
  blank one, so a frame where the RFID tag wasn't read can't erase a good read.
- **`purchases`** — one row per order *line* (not per spool): `fkey` when the
  filament is known, free-text product/colour otherwise, spool count, grams each,
  price paid, currency, order date and reference. Deliberately not linked by a
  foreign key — a purchase is matched to a filament at read time on "product line
  + colour name", so a spool bought today links itself up the first time the AMS
  actually sees it.
- **`notes`** — free-form notes: `title`, `body`, `category`, `created_at`,
  `updated_at`. The category is the name itself, like `prints.pgroup` — no
  category table, renaming is writing a new name on those notes, and a category
  stops existing when its last note leaves.
  Rendered escape-first, then linkified — the reverse order would let a note
  inject markup into the page.
- **`note_images`** — a picture per row (`note_id`, `mime`, `data` BLOB, size),
  deleted along with its note. The note listing carries only metadata; the bytes
  are fetched one URL at a time and cached immutably, since an id's content never
  changes.
- **`settings`** — small key/value store (e.g. the persisted recording mode, HMS
  acknowledgements, and per-task maintenance intervals / reset marks).

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

- **Filament usage is derived, never counted.** The Filament page sums
  `prints.filament_detail` on every request instead of incrementing a stored
  counter. That makes it retroactive (every past print already carries per-slot
  grams), immune to double-counting on a restart, and self-correcting when a print
  is deleted or its grams are overridden — a manual override is applied
  proportionally to the per-slot entries, so the page's total always matches the
  history page's. Verified against the real database: 646.2 g / 9.3707 € both ways.

- **The invoice knows what the printer doesn't.** A Bambu invoice line reads
  `SKU: A19-K00-1.75-1000-SPL` / `Variant: Absolute Black (17101)`, and that SKU
  prefix is character-for-character the `tray_id_name` the AMS reports. So an
  imported invoice joins to a spool **by code**, carries the colour name the
  printer never sends, and states the price actually charged (the *Items
  SubTotal* column, after discount) rather than the list price — €15.39/kg on
  that line, not the €24.99 the config matrix assumes. Learned names are stored
  under `cname_<CODE>` in `settings` and outrank the built-in guess table;
  `filament.color_names` in the config still outranks everything.

- **Accessories are not filament.** Build plates and spools appear in the same
  table (`SKU: FAP017-N`, `RSP001`); only SKUs carrying a `-1.75-` diameter field
  are imported. On the test invoice: 6 filament lines totalling €80.24, with the
  plate (€19.99) and three spools (€27.28) skipped — €127.51 grand total.

- **Order parsing suggests, it never saves.** Bambu's receipt layout can't be
  verified from inside this project and will change, so `parse_order()` is
  explicitly best-effort: it returns `None` for anything it can't read rather than
  inventing a price or a quantity, and every line lands in an editable draft that
  the user confirms. Two bugs that shaped it, both covered by the self-test: a
  trailing-symbol price pattern reads `×1  € 27,99` as *one euro* unless
  symbol-before-number is tried first, and `\b` after `€` never matches at
  end-of-line because `€` isn't a word character.

- **Prints are costed at list price, never at what you paid.** An imported
  invoice teaches `{SKU: € per kg}` from the *Price* column, not the discounted
  *Items SubTotal*. Costing a print at a promotional price makes it look cheaper
  than replacing that filament will be, and a quote built on it comes out under
  the real figure — the same reasoning that makes every total round up. The
  Filament page shows both numbers, so the saving stays visible where it belongs.

- **A learned price only applies to a spool the RFID tag vouches for.** A
  third-party spool sliced with a Bambu profile reports a Bambu SKU, so pricing it
  from a Bambu invoice would silently inflate it. The rule needs `ams_bambu` to say
  genuine; otherwise it falls through to the brand × material matrix. Rule order:
  `per_slot` → `per_filament_id` → **order price** → brand × material → `per_type`
  → default, so an explicit config override still wins and
  `prices_from_orders: false` turns the whole thing off.

- **Colour codes are compared canonically, names never are.** The AMS pads the
  colour part of `tray_id_name` to two digits, the store's SKU does not always —
  one real invoice carries both `A19-K00` and `A01-R4`. So codes are normalised
  (`A00-W01` ≡ `A00-W1`, while `W1` and `W2` stay distinct) before any comparison.
  This matters more than it looks: a regional store returns *localised* colour
  names, so the same spool is "Jade Weiß" on a German invoice and "Jade White" in
  the built-in table. Matching on the code sidesteps the language entirely, and
  the invoice's wording then replaces the guess everywhere — Filament page, AMS
  tile and reorder link.

- **Four ways a purchase finds its filament.** Tried in order, most certain
  first: an explicit `fkey`; the colour **code** against an AMS identity; the
  **wording** (product + colour name); and finally the **SKU derived from the
  code** — `A19-K00` → `GFA19`, because `GF` + the code's letter + its two digits
  is the `tray_info_idx` the printer reports (verified on PLA Pure, PLA Basic and
  PETG Basic). That last route is what reaches filament used up long ago: a print
  row carries the SKU and a colour hex but no colour code, while an invoice
  carries a colour code but neither SKU nor hex, so nothing else bridges them.
  Every purchase shows which route matched it, so a miss is diagnosable instead
  of mysterious.

- **The SKU route must never match on the SKU alone.** Every colour of a product
  line derives the *same* SKU — `A01-R4`, `A01-G0` and `A01-B6` are all `GFA01` —
  so "exactly one candidate for this SKU" is not evidence. It used to be, and the
  result was every PLA Matte order attaching itself to the one PLA Matte identity
  on record, which then claimed to have been bought eleven times. Candidates are
  now dropped when their colour code or colour name contradicts the purchase, and
  a lone *anonymous* candidate (a filament the print history knows by SKU and hex
  only) is refused when the purchase log holds more than one colour of that
  product — that would be a coin flip. Unmatched lines become their own stock
  rows, which is the honest outcome.

- **Bought-but-unused filament is still filament.** A purchase that matches
  nothing gets its own row — keyed by colour code, so four colours of one product
  stay four rows — marked **unused** with 0 g used. It folds into the real entry
  automatically the first time that spool is printed with or seen in the AMS,
  because only *unmatched* purchases generate these rows.

- **"Left" can go negative.** Bought minus used, shown as-is rather than clamped:
  a negative simply means orders from before the log existed are missing, and
  hiding that would make the number look trustworthy when it isn't.

- **The cloud reports the slicer profile, not what was in the tray.** Print a PLA
  Matte reel with a PLA Basic profile and the job says `GFA00` while the RFID tag
  says `GFA01` — one spool, two identities, the tag's carrying the name and the
  job's carrying the grams. Two answers: `prints.ams_slots` now snapshots what
  the AMS actually held (SKU, colour, code, material per slot) while the print
  ran, and enrichment takes the **SKU** from that in preference to the profile —
  but never the colour. The cloud's colour has always been right, so it stays
  authoritative *and* validates the snapshot: if the two disagree the slot
  numbering doesn't line up, and the snapshot is dropped for that line rather
  than crediting another spool. That guard matters because one real job reported
  two different filaments both on slot 1;
  and identities can be **merged** by hand, folding usage, cost and purchases into
  one row. The page suggests likely pairs — same colour, same material, one side
  unnamed — but never merges on its own, because two different blacks look
  identical to that test. Merging is an alias, so unmerging restores both rows
  exactly; nothing in the print rows is rewritten.

- **One filament = SKU + colour, not one spool.** `GFA00|FFFFFF` is the only key
  the live AMS and the cloud's per-print detail both carry. Two spools of the same
  Jade White are deliberately one entry; per-physical-spool tracking would need
  `tray_uuid`, which no past print recorded.

- **Colour names are looked up, never guessed.** An RFID spool reports its product
  line (`PLA Basic`), SKU (`GFA00`), colour code (`A00-W01`) and exact hex — but not
  the marketing colour name. `filament_catalog.COLOR_NAMES` maps only codes
  confirmed against real reports; anything unknown shows the raw code plus the hex
  swatch, because a confidently wrong colour name is worse than none when the point
  is to reorder the same spool. Add your own in `filament.color_names`.

- **Reorder links are searches, not deep links.** `/search?q=PLA+Basic+Jade+White`
  rather than `/products/<handle>`: product handles can't be verified from here and
  a 404 at the moment you want to reorder is worse than one extra click.

- **The external holder keeps reporting a spool that has been moved.** `vir_slot`
  holds the last filament assigned to it indefinitely, so after moving that spool
  into a tray the printer reports it in *both* places. With the same profile and
  colour both are the same identity, and merging them with plain assignment let
  the stale external entry overwrite the real tray — the spool showed as
  "external" forever. AMS trays are now merged first and win.

- **"Low" only applies to RFID spools.** Remaining % comes from the tag, so a
  third-party tray reports `-1` and an external spool reports `0` — neither means
  empty, and warning on them would cry wolf on every non-Bambu spool.

- **Cost is monotonic per print.** Energy for a running print only ever increases
  and is seeded from the stored value on restart, so a restart mid-print can't reset
  a job's accumulated cost to zero, and a new job can't inherit the previous one's.

- **Cloud can't fail a live job.** The cloud reports a placeholder end time and
  `status = 4` while a job is still running; enrichment only closes a print on
  `status = 2` and never touches the currently-live job.

- **Cost windows are by activity, not start time.** The today/week/month tiles sum
  every print that was *running* during the window — not just those that started in
  it — so a job that spans midnight, or is still printing, counts toward "today"
  instead of leaving it blank.

- **MakerWorld id survives partial reports.** Bambu's incremental MQTT frames often
  omit `design_id`/`profile_id`, so they're **latched in memory** while a job prints
  and persisted from there — a partial update can't null out the stored model link.

- <a id="mqtt-command-verification"></a>**MQTT command verification blocks
  `ams_filament_setting`.** Firmware 01.08.03.00beta/01.08.05.00 added a check on
  the local MQTT request topic: commands the printer does not consider to come
  from a trusted client are rejected with HMS **0500_0500_0001_0007**, *"MQTT
  Command verification failed, please update Studio or Handy"*. Confirmed on an
  X2D, twice: assigning a filament to an AMS slot, and setting the bed or chamber
  temperature. Both raised the warning and changed nothing.

  Two command families are therefore **off by default**, each behind a switch,
  and the UI hides what it cannot use:

  | Config | Covers | Default |
  | --- | --- | --- |
  | `filament.allow_slot_assign` | `ams_filament_setting` — the Assign button | `false` |
  | `controls.allow_gcode` | `gcode_line` — bed/chamber setpoints (`M140`/`M141`) and the fan sliders (`M106`) | `false` |

  With gcode off the fans still show their readings, just without sliders, and
  the temperature **set** buttons are hidden. `pause`/`resume`/`stop`,
  `print_speed` and `ledctrl` are **believed** to be ungated — they use different
  commands and have not raised the warning — but that is now an observation, not
  a guarantee. I previously asserted `gcode_line` was ungated and it was not.

- **A print group is a name, not a table.** `prints.pgroup` holds the group's
  name directly: no ids to keep in step, renaming is "write the new name on the
  same rows", and a group stops existing when its last member leaves. It sits in
  `PRINT_IMMUTABLE` alongside `label` — without that the 60-second persist tick
  would blank it on the running print, which is covered by a regression test.

- **An error code belongs to a job, but the printer reports it as machine
  state.** `print_error` keeps showing the last failure long after that job
  ended, so cancelling one print and starting another stamped the old code onto
  the new one. Whatever is being reported the moment a job begins is therefore
  remembered as *stale* and ignored until the printer reports something else —
  including nothing. The same code recurring after a genuine clear is recorded
  normally, and a wrong code already on a row can be cleared by clicking it.

- **A deleted print has to be tombstoned.** The printer keeps reporting the *last*
  job's `task_id` for as long as it sits idle, so simply deleting the row would let
  the next persist tick write it straight back within 60 s. Deleting the currently
  tracked job therefore records it in an in-memory `_deleted_jobs` set that
  `_persist_print()` checks; the set is cleared as soon as a new job starts, since
  nothing older can be resurrected then. Cloud enrichment is safe by construction —
  it only *updates* rows that already exist.

- **Maintenance runs on tracked hours.** Upkeep due-dates are measured from
  cumulative *recorded* print time (summed from completed prints), counting from when
  tracking began or a task's last **Done** reset — independent of the printer's clock.

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
| A dashboard change doesn't appear after copying it over | The page is served `no-store`, so this should not happen — but to be sure, hover the SN line in the header or check the console: both report when the served `dashboard.html` was last written. `GET /api/version` returns the same timestamp. |
| Cloud login fails                                   | Re-run `tools/setup_cloud.py` to refresh the stored token.                  |
| A print is stuck on **"running"** with a runaway duration | The app was down when it finished, so no end time was recorded. Press **Refresh** — cloud enrichment closes it if the job is still among the last 20 cloud tasks and reports `status == 2`. Otherwise click its **Duration** cell and type how long it took (`5h 20m`). `tools/dump_cloud_tasks.py` shows which condition failed. |
| A spool shows a **code** (`A00-N04`) instead of a colour name | That code isn't in the built-in table. Add `"A00-N04": "Cocoa Brown"` under `filament.color_names` and restart. |
| A purchase says **"not matched to a filament"** | Nothing on record matches it yet — that filament has neither been printed with nor seen in the AMS. It still appears on the page as **unused** stock, and links itself the first time the spool is loaded or used. |
| A purchase says **"ambiguous — colour unclear"** | Several colours of that product are on record and the colour name doesn't single one out — commonly a localised name ("Jade Weiß") against an English one. Fix the line's **colour code** rather than its name: the code match ignores language. |
| Uploading a PDF says **"pypdf is not installed"** | The PDF reader is an optional dependency, and it must go into the **app's venv**, not the system Python: `cd /volume1/apps/bambu-monitor && ./venv/bin/python3 -m pip install pypdf`, then restart. Or paste the invoice text instead. |
| Saving a purchase fails with **"Unknown column …"** | A column added after that table first shipped. Every such column is listed in `storage.LATE_COLUMNS` and applied on startup — restart the app; the log shows `[store] migrated: added …`. |
| **Bought/Left** columns are empty                   | No purchases logged for that filament yet. Paste an order, or add a line by hand on the Filament tab. |
| Filament page shows a spool as **external** after moving it into a tray | Fixed — the printer keeps reporting the last external assignment after the spool has gone, and the stale entry used to win. AMS trays now take priority over the external holder for the same identity. |
| Filament page shows a spool in the **wrong slot**, or twice | Identity is SKU + colour. If a spool reports a different profile or colour in a tray than it did on the external holder, that is a **different identity** and appears as a second row rather than moving. |
| Filament page shows only **material + hex** for an old filament | It was used up before the page existed, so the AMS never recorded its identity. Only prints carry it, and they store just SKU + colour. It names itself properly if a spool of it is loaded again. |
| A spool has **no Reorder link / no "Low" flag**     | It isn't a genuine Bambu spool (`tag_uid` is all-zero), so the printer reports neither an identity nor a real remaining %. Expected for third-party and external spools. |
| Deleting a print says **"print is still running"**  | Intentional: the job is active, its energy total is still accumulating and it would be re-created anyway. Delete it once it has finished. |
| Deleted prints changed the **Statistics/Maintenance** figures | Expected — both aggregate over the `prints` table, so removing a row also removes its hours, energy and filament from the totals. |
| Live view spins / no picture                        | Printer serves **one** camera client at a time — close the Handy app / Bambu Studio / other tabs; if wedged, power-cycle the printer and re-enable LAN Mode Live View. Confirm with the `curl … stream.mp4` test. See [Live view](#live-view-camera). |
| Live view tab missing                               | `camera.enabled` is false, or `/api/camera` reports disabled. Set it in `printer.config.json` and restart. |
| Print controls do nothing / "printer not connected" | Recording mode is **Off**, which drops the MQTT stream — controls need a live connection. Switch to Auto/On. |
| A fan slider moves the **wrong** fan                | The `M106` fan mapping (`P1/P2/P3`) can differ per model. Adjust `_FAN_GCODE` in `app.py`. |
| **HMS 0500_0500_0001_0007** after a control      | *"MQTT Command verification failed"* — the firmware rejected the command; nothing was written and nothing is damaged. Acknowledge the warning in the header. Slot assignment and the gcode-based controls (bed/chamber setpoints, fan sliders) ship **off** for this reason; see [MQTT command verification](#mqtt-command-verification). |
| Bed/chamber **set** buttons and fan sliders are missing | Expected — `controls.allow_gcode` is `false` because the firmware rejects `gcode_line`. Set it to `true` to try on other firmware; the readings show either way. |

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
