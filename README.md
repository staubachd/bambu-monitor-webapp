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
  - [Backup and restore](#backup-and-restore)
  - [Keeping an idle app quiet](#keeping-an-idle-app-quiet)
  - [Adding another backend](#adding-another-backend)
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
- **Layer height and the rest of the slicer's settings**, read off the printer
  (see [Slicer metadata](#slicer-metadata)). Or typed in by hand: click the
  layers cell, or the line in the detail panel, and type `0.2`, `0,2 mm` or
  `200 µm`. What you type always wins over what was read, and clearing it falls
  back to the file rather than to nothing. Blank means *not recorded*, never a
  guessed default. The detail panel also shows the model's real height, read
  from the file rather than multiplied out — see below for why that matters.
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
- **The colour code, click to copy.** A spool with no RFID gets its colour typed
  in by hand on the printer, and the identity is `SKU|COLOUR` — so a hex one digit
  off mints a *second* filament rather than reusing the one you already named.
  Every row shows its colour next to the name as a chip; clicking it copies.
  A **HEX / RGB** switch in the card head decides which form — Windows' own colour
  dialog has Red/Green/Blue boxes and no hex field, Bambu Studio takes a hex, so
  the useful one depends on where you are typing it. The choice is remembered,
  the chip copies whatever it shows, and its tooltip always spells the channels
  out (`Red 247 · Green 55 · Blue 55`) alongside the hex.
  The copy path matters here: `navigator.clipboard` needs a **secure context**,
  which a NAS on plain `http://` is not, so it falls back to `execCommand("copy")`
  and, if even that is refused, to a prompt — the code always ends up somewhere
  you can take it from
- **Editable price per kg.** Click the `€/kg` cell to type what the spool actually
  cost. A price set by hand **beats every other rule** — the configured brand ×
  material matrix is a guess about a category, and an invoice-learned price is a
  guess that the SKU on the receipt is the spool that was in the tray; neither
  should overrule someone who looked at what they paid. It is the only way to
  price third-party filament properly.
  Setting it **re-costs the prints that already happened**: per-print costs are
  stored (worked out when the cloud enriched the job), so a price that only
  applied to future prints would leave the totals disagreeing with the number just
  typed in. The toast reports how many prints moved. Clearing the field is not
  zero — it hands the filament back to the configured rules and re-costs again.
  A merged identity is priced by whichever row survived — and **merging re-costs
  the prints it moved**. Two halves of one batch recorded under different slicer
  profiles get different prices; saying they are the same filament has to make
  them cost the same, or the merge fixes the Filament page and leaves History
  contradicting it. Pricing therefore resolves the SKU through the identity, not
  through whatever the print happened to report, so a folded print picks up the
  survivor's invoice price. Unmerging re-costs them back. The RFID verdict still
  decides whether an invoice price may be used at all, and it is taken from the
  **identity** first: a print whose filament was not recognised is precisely the
  one whose own snapshot cannot be trusted, and merging it into a known-genuine
  identity is a person saying which spool it was. A third-party spool's identity
  carries `is_bambu = false`, so it is still never priced as a Bambu one.
  [`tools/why_this_price.py`](tools/why_this_price.py) shows, per print and per
  slot, which identity it resolved to, the rate applied, the rule that chose it,
  and what the rules would give today — including prints with no per-slot detail,
  which no merge or price can ever re-cost. The cell shows **bold**
  for a hand-set price, grey for one learned from your invoices, and a dash when
  it falls through to the configured default
- **Editable naming**: click a row's vendor or name to set **vendor / product /
  colour** by hand. This is the only way a third-party spool gets a name at all —
  the printer reports a borrowed Bambu profile and a colour, never a manufacturer
  — and it is what lets purchases match it, wording being the sole route without a
  colour code. Genuine spools show *Bambu Lab* without anyone typing it
- **The names reach the AMS card too.** The AMS can only read a genuine Bambu
  tag, so a third-party tray arrives with no name and its tile used to say "no
  RFID data" even after the vendor, product and colour had been typed in here.
  The identity the tray reports is the same one that was named, so the tile now
  shows it — and a name set by hand beats the catalogue for a Bambu spool too,
  the same rule this page follows. What does *not* change: no store link and no
  low-stock flag for a spool with no tag, because neither can be known
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
- **Responsive header** — below 1150 px the section bar moves onto a full-width
  row of its own and the serial/firmware line steps aside; below 560 px the
  last-update stamp folds away too, so the chips still fit on a phone
- Live updates via Server-Sent Events (no polling from the browser)
- **German by default with a DE/EN switcher**
- Light/dark theme toggle
- **Settings page** (the gear, top right) — prices, the filament matrices,
  thresholds, intervals, feature gates and the printer / Tapo / Bambu Cloud
  credentials, edited in the browser instead of by hand in JSON on the NAS.
  Settings are not a sixth thing to browse alongside Printer / Prints / Filament
  / Workshop, so they get an icon rather than a place in the section bar; the
  section exists in `NAV` but is listed in `NAV_HIDDEN`, which keeps it out of
  the bar while the router, the deep links (`#settings/page`) and the layout
  test still treat it as a normal section.
  - **Grouped into tabs** built from the schema's own `group` field — Printer,
    Cost, Filament, Controls, Recording, Power, Cloud, Camera — so adding a
    setting never means touching the tab strip. A dot on a tab marks unsaved
    edits in that group, so nothing is lost by saving from a different tab. The
    open tab is remembered. A ninth tab, **Database**, shows the connection
    read-only — it is the one thing this page cannot change.
  - Edits **survive a tab switch**: a change is captured when you make it, not
    when the field happens to be on screen at save time.
  - **One source.** Every setting is a row in the database. There is no config
    file underneath it, so a value is either one somebody set or the default
    declared next to the setting in `settings_schema.py`. A field at its default
    says so; a field that was changed offers the way back, and only when that
    would actually change something. Resetting deletes the row rather than
    storing a copy of the default, so a later change to the default still
    reaches an install that never touched it.
  - **Applied immediately** wherever it can be. The sections the code binds at
    import (`FIL_CFG`, `COST_CFG`, …) are live views rather than snapshots, so an
    edited price is in force on the next print without a restart and without 60
    call sites changing. What genuinely cannot be live — ports, the camera, the
    printer connection — is labelled **needs a restart** on the field itself
    rather than quietly doing nothing.
  - **The schema is the gate.** One table (`settings_schema.py`) defines every
    editable setting, its type and its range; it drives the form, the validation
    and the restart labels together. Anything not in it cannot be written through
    the API at all — including the database connection, which the app has to
    read before it can read anything else (see **Setup** below).
  - **Secrets are never sent to the browser.** A password field shows only
    whether one is set; an empty box means "leave it alone", never "erase it".
  - A whole form is validated before anything is stored, so one bad field cannot
    leave half a page applied.

  > **There is no login on this page.** Anyone who can reach the dashboard can
  > change these, including the credentials and the G-code gate. That was a
  > deliberate choice for a LAN-only tool; putting it behind a reverse proxy with
  > basic auth is the obvious next step if the network is not trusted.
- **Palette: Firefox Proton.** The browser's own scheme rather than a two-colour
  pair — a neutral grey scale with one accent, so nothing has to be demoted to a
  tint. Dark is Firefox's dark chrome: in-content `#1C1B22`, cards `#2B2A33`,
  raised `#42414D`, text `#FBFBFE`. The accent is **Photon blue-40 `#45A1FF`**
  rather than Proton's own `#00DDFF`: the cyan is authentic but a saturated neon,
  and on a page made of small chips, bars and table rows it shouted from every
  one of them. Light is
  its counterpart: `#F9F9FB` ground, white cards, `#15141A` text, primary
  `#0060DF`. Greys throughout: `#15141A #2B2A33 #42414D #5B5B66 #8F8F9D #CFCFD8
  #F0F0F4 #FBFBFE`.
  The accent is spent the way Firefox spends it: links, focus, the progress ring
  and the primary action — nothing else. **Selection and identity are surfaces,
  not blocks of accent**: the selected section is a lifted card the way a selected
  Firefox tab is, the live state pill is a neutral chip with the state's colour as
  its text and border (matching the `.st-ok`/`.st-bad` chips the history table
  already used), and the app mark is the same neutral chip as the icon buttons it
  sits beside. The AMS slot that is printing is the one place a **tint** is used:
  a soft accent wash with a marker edge down its side, the pattern the warning
  rows already use — a purely neutral treatment left it differing from an idle
  slot by one pixel of border shade, which could not be seen. `t_palette.js`
  fails if any of these becomes an accent *fill* again, and also if the loaded
  slot ever loses its tint or its marker.
  Everything that carries a colour is a token: `--brand`/`--brand-ink` for the
  primary fill (which may differ per theme — blue in light, cyan in dark),
  `--accent-ink` for whatever reads on an accent fill, `--on-status` because
  status colours are dark in light mode and light in dark mode, and
  `--good-soft`/`--warn-soft`/`--danger-soft` for their tints. The state pill picks
  its own ink from the relative luminance of the colour it is handed.
  Changing the scheme is a swap of the two token blocks; `scratchpad/bm/t_palette.js`
  needs no edit, because it samples the palette rather than hardcoding it. It
  checks every pair the stylesheet actually renders against its contrast floor,
  that whatever is `--accent` is readable as text, that the ink picker returns
  something legible on any fill, and that no colour is hardcoded outside the token
  blocks — a logo gradient and a set of status tints each survived an earlier
  palette change still mixed from a scheme that no longer existed
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
| Database         | **SQLite** (local dev) / **MariaDB** or **MySQL** via **PyMySQL** (production on NAS) |
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

### 2. A smart plug (power)
The printer reports no power at all, so it comes from whatever it is plugged
into — and which plug that is isn't something this app gets to choose. The
source is a **provider**, picked in Settings → Strom:

| provider | what it is | how it's read |
|--|--|--|
| `tapo` | TP-Link **P110 / P110M / P115** | polled on the LAN via the `tapo` library, with the Tapo account login |
| `mqtt` | **Zigbee2MQTT** (so IKEA **INSPELNING** and other Zigbee plugs), **Shelly**, **Tasmota**, **Home Assistant** | subscribes to the plug's topic on a broker |

A provider only has to report **watts**. Per-print energy is integrated from
wattage over wall-clock time (`_accumulate_job_energy`), so `today_wh` /
`month_wh` are display only and a meter that reports just the draw is a
first-class citizen.

**Why MQTT and not "the plug's IP".** An IKEA INSPELNING is Zigbee, not WiFi: it
has no address, and nothing can connect to it directly. It reaches the network
through whatever it is paired to — a DIRIGERA hub, or a Zigbee stick running
Zigbee2MQTT — so the reading is *pushed* to a broker rather than polled from a
plug. That difference is why a provider owns its own loop instead of implementing
a shared `poll()`.

Two things the MQTT provider has to get right, neither of which Tapo has to:
- **Silence is a failure.** Connecting to a broker proves nothing; a wrong topic
  never errors, it just never arrives. So an unheard topic is reported as an
  error (`connected, but nothing has arrived on … yet`) rather than displayed as
  zero consumption, and a message that arrives without the expected field says
  which fields it *does* have.
- **Energy is a counter, not a window.** Tapo reports "today" and "this month";
  a Zigbee plug reports one number that only goes up. Today and this month are
  differences from a baseline captured at the last rollover. The baselines are
  persisted (so a restart doesn't zero the day) but written **only when one
  moves** — twice a day at most, because a plug publishing every few seconds
  behind a database write is exactly the constant writing that stops the NAS
  disks from sleeping. A counter that goes backwards means the plug was reset,
  and rebases rather than reporting a negative day.

Finding the topic and field names is the fiddly part, so:

```bash
python tools/sniff_power_mqtt.py 192.168.1.10          # listens, prints what it hears
```

It marks the fields that look like a power reading and prints the three values
to paste into Settings.

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
| `power_providers.py`          | Where the wattage comes from: one small interface, a Tapo poller and an MQTT subscriber. Adding a meter is a class plus a row in `PROVIDERS`. |
| `filament_catalog.py`         | Colour-code → colour-name table, regional Bambu store search links, and the best-effort order-confirmation parser. Has a self-test (`python filament_catalog.py`). |
| `dashboard.html`             | The entire frontend (HTML + CSS + JS + i18n) in one file.               |
| `bootstrap.py`                | The database connection — the only thing that cannot live in the database. Reads/writes `instance/db.json`, and tests a connection before anything is written to it. |
| `config_store.py`             | Every other setting, read through from the database with live section views. |
| `gcode_meta.py`               | Reads the slicer's own metadata off the printer's USB drive over FTPS, without downloading the job: a ZIP read backwards through FTP `REST`, about 80 KB per print. Has a self-test (`python gcode_meta.py`). |
| `settings_schema.py`          | The one table describing every editable setting: type, range, default, group, and whether it needs a restart. |
| `setup_wizard.py` / `setup.html` | First-run setup. Served instead of the dashboard when there is no connection on file. |
| `instance/db.json`            | Written by the wizard: host, port, user, password, database. Seven keys, chmod 600, never edited by hand. Not committed. |
| `requirements.txt`            | Python dependencies.                                                    |
| `go2rtc/`                     | The go2rtc relay binary for the camera Live view (downloaded per-arch; not committed). |
| **`tools/`**                  | Dev & one-time setup helpers — not part of the running app.             |
| `tools/backup.py`             | Export the database to JSON and restore it. `export --out DIR --keep N` for a scheduled backup; `restore` is a dry run until `--apply`. |
| `tools/why_disk_busy.py`      | Read-only: samples `/api/diag` twice and reports what the app is writing, and how often. For "why are the NAS disks never idle". |
| `tools/import_config.py`      | Headless version of the wizard: moves an old `printer.config.json` into the database. |
| `tools/setup_cloud.py`        | Interactive Bambu Cloud login → stores an auth token in the database.  |
| `tools/setup_power.py`        | Verifies **Tapo** plug connectivity and credentials, then stores them.  |
| `tools/sniff_power_mqtt.py`   | Read-only: listens to an MQTT broker and prints the topics and fields it hears, marking the ones that look like a power meter. For filling in Settings → Power when the meter is `mqtt`. |
| `tools/test_mqtt_local.py`    | Standalone check that local MQTT works against the printer.             |
| `tools/capture_sample.py`     | Captures a real report to `samples/sample_report.json` for offline parser testing. |
| `tools/explore_ftps.py`       | Explores the printer's FTPS file store. Says explicitly when a directory is empty — the printer serves the USB drive there, and an empty root usually means no drive is in the slot. |
| `tools/dump_cloud_tasks.py`   | Read-only: cloud tasks next to the stored prints. Diagnoses why an orphaned print didn't close. |
| `tools/dump_filaments.py`     | Read-only: every filament identity, where it came from, and which look like the same spool split in two. |
| `samples/`                    | Captured payloads used by the offline self-tests: MQTT reports for `bambu_state.py`, and a real `project_settings.config` / `slice_info.config` pair so the slicer parser is tested against the exact bytes Bambu Studio writes. |
| **`tests/`**                  | The test suite: 42 files plus the two module self-tests. Run it with `tests/runall.ps1`, which copies the app into a scratch folder and runs every `t_*.py` / `t_*.js` against a throwaway copy of the database, reporting pass/fail per file - silence is not a pass. Not needed on the NAS. |

| **`deploy/`**                 |                                                                         |
| `deploy/start.sh`             | Idempotent POSIX launcher (pidfile + `kill -0`), supports a `restart` arg. |
| `deploy/DEPLOY.md`            | Step-by-step NAS deployment notes.                                      |
| `deploy/schema_and_user.sql`  | Creates the database and app user. Works as-is on MariaDB and MySQL 8. |
| `deploy/sqlite_to_mariadb.py` | One-shot migration of a local sqlite DB into whichever server is configured. |
| `deploy/recalc_print_energy.py` | Backfills/recomputes per-print energy after pricing changes.         |

---

## Configuration

There is **no config file**. Everything the app knows is a row in the database,
edited from the **Settings page** (the gear, top right) — printer, costs,
filament prices, thresholds, intervals, feature gates, and the Tapo / Bambu
Cloud credentials.

The one exception is the database connection itself: the app has to read it
before it can read anything else. That is seven keys, and they live in
**`instance/db.json`**, which the setup wizard writes and nothing else touches:

```jsonc
{
  "backend": "mariadb",            // or "sqlite"
  "mariadb": {
    "host": "127.0.0.1", "port": 3306,
    "user": "bambu", "password": "········", "database": "bambu_monitor"
  }
}
```

It is written atomically and chmod 600 — it holds a database password. A
relative `sqlite_path` resolves against the app folder, so the directory the
service happens to start in cannot decide which database is opened. Not
committed.

**The location is derived, never assumed.** `instance/db.json` sits beside the
app, wherever the app was installed — `bootstrap.HERE` comes from the module's
own `__file__`, so it is `/volume1/apps/bambu-monitor/instance/db.json` on one
NAS and something else entirely on the next, with nothing to configure and
nothing to edit. Moving the app folder moves the file with it. A test asserts
this: no path is hardcoded, and changing the working directory does not move it.
The wizard shows the full path on its first page, and Settings → Database shows
it afterwards.

The one place a path *is* fixed is `APP_DIR` at the top of
[`deploy/start.sh`](deploy/start.sh), which is the launcher rather than the app —
if you install somewhere other than `/volume1/apps/bambu-monitor`, that line is
the only thing to change.

Before the wizard writes anything it checks the folder will actually take the
file — an app directory owned by another user, or copied read-only over SMB,
otherwise fails at Finish with a traceback in the task-scheduler log after five
pages of typing. It now fails on page one, saying which folder and why.

### First run

Start the app with no `instance/db.json` and it serves the **setup wizard** on
the same port instead of the dashboard, in five steps:

| | asks for | |
|--|--|--|
| 1 | Database | MariaDB or MySQL host/user/password/name, or SQLite. **Test connection** actually opens it — and creates a table, because a read-only grant passes a plain connect — then reports which server answered, since choosing MariaDB and reaching MySQL (or the reverse) works but is worth knowing. |
| 2 | Printer | IP, serial, access code. **Test printer** opens the same MQTT connection the monitor will. |
| 3 | Plug, cloud & camera | All optional, all skippable. |
| 4 | Costs & filament | Seeded with Bambu's list prices rather than zero, so a fresh install does not price every print at 0.00. |
| 5 | Recording & safety | Sample interval, retention, and the two feature gates (off unless you have a reason). |

Nothing is written until the last page: field validation first (it costs
nothing and touches nothing), then the connection test, then both halves at
once. A typo on step 4 cannot leave a half-configured app, and a connection
that has not answered is never saved.

Finishing writes `instance/db.json`, stores everything else in the database,
and re-execs the process so it starts normally. Re-run it later with
`python app.py --setup` — it comes back prefilled, with secrets shown only as
"set".

The wizard's field list is generated from `settings_schema.py`, and a test
fails if any schema group is not reachable from some step. A setting added to
the schema appears in the wizard without anyone remembering to list it.

### Upgrading from printer.config.json

The old file is read once, to fill the wizard in — including the credentials,
so nothing is retyped. On finish its contents move into the database and the
file is renamed to `printer.config.json.imported` (renamed, not deleted: it
holds passwords that may exist nowhere else). Nothing reads it after that.

Headless equivalent, for an upgrade over SSH:

```bash
python tools/import_config.py            # show what would happen
python tools/import_config.py --write    # do it
```

### Notes

- **The listening port** defaults to `8770`; override with the `BAMBU_PORT`
  environment variable. It is not a setting — the wizard has to be served
  somewhere before there are settings.
- `cloud.token` is filled in automatically after a successful sign-in, so the
  password is not re-sent on every start.
- To turn off a subsystem, untick its **enabled** box — the corresponding
  worker thread simply isn't started. Enabled but not yet filled in is a state
  the Settings page can reach, and each worker says what is missing rather than
  dying in a thread nobody is watching.

---

## Running locally

```bash
# 1. dependencies
pip install -r requirements.txt

# 2. run
python app.py
#    → no instance/db.json yet, so this serves the setup wizard on :8770
#    → answer it (SQLite needs nothing but a filename), and the app restarts
#      itself, connects to the printer and creates telemetry.db
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
2. **Database:** run `deploy/schema_and_user.sql` on the NAS MariaDB (or MySQL) to create the
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
the database (so the access code never lives in a second place) and
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
3. **Enable it** — Settings → Camera (or step 3 of the wizard):
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
| `GET /api/backup`          | Download everything worth keeping as one JSON file. `?secrets=1` includes credentials (off by default), `?images=0` leaves note pictures out. |
| `POST /api/backup/restore` | Restore one, as multipart `file` or a JSON body. `mode=merge` (default) inserts only what is missing; `mode=replace` empties each table first. `dry=1` reports what would happen and writes nothing. |
| `POST /api/filaments/left` | Pin how much is left `{ "fkey": …, "grams": 480 }`, take it from the printer `{ "fkey": …, "from_ams": true }`, or unpin with `"grams": null`. Stored as an anchor with a timestamp, so prints and purchases after it still count. |
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
| `POST /api/prints/layerheight` | Set **your** layer height for a print `{ "job_id": …, "mm": "0.2" }`. Accepts `0,2`, `0.2 mm`, `200 µm` and a bare `200` (read as microns); blank clears it, falling back to the slicer's. Refuses anything outside 0.01–3 mm with **400**, an unknown job with **404**. |
| `POST /api/prints/slicer` | Read one print's sliced file from the printer now `{ "job_id": … }`. **400** if the reader is switched off, **404** for an unknown print, **502** if the file could not be read. |
| `GET /api/slicer`          | What the slicer reader has been doing: enabled, last pass, how many prints are still waiting. |
| `GET /api/storage`         | What is on the printer's USB drive and how full it is. `?refresh=1` forces a new listing instead of the 30 s cache. |
| `POST /api/prints/delete`  | Delete one print from the history `{ "job_id": … }`. Refuses a currently-running job with **409**; the telemetry time-series is left untouched. |
| `POST /api/cloud/refresh`  | Trigger an immediate Bambu Cloud enrichment pass.                   |
| `POST /api/hms/ack`        | Acknowledge / restore an HMS health warning.                        |

---

## Storage & database schema

One `Storage` object; the backend is chosen by the connection file, so identical
code runs on **sqlite** (dev), **MariaDB** and **MySQL**. See
[Adding another backend](#adding-another-backend) for what a fourth would cost.
Missing columns are added on startup via a migration list, so the schema can
evolve without manual `ALTER`s.

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

### Backup and restore

The print history is the part nothing else has a copy of: the printer does not
keep it, the cloud keeps a rolling window, and every cost figure on the page is
derived from it. Filament identities are next - the names, colours and prices
somebody typed by hand.

**Settings → Sicherung** downloads one JSON file, and restores one. Or, for a
backup that happens whether or not anyone remembers it:

```bash
python tools/backup.py export --out /volume1/backup/bambu --keep 30
python tools/backup.py show    <file>      # what is in it
python tools/backup.py restore <file>      # says what it WOULD do
python tools/backup.py restore <file> --apply
```

Point `--out` at a folder Hyper Backup already covers and the history becomes
part of the off-site backup. Written to a temporary name and moved into place,
so an interrupted run never leaves a half-file that looks complete.

Three decisions worth knowing, because each can bite:

- **Telemetry is not in it.** A temperature sample every 20 seconds is the bulk
  of the database and the only table nothing is computed from — 3 000 rows for
  five prints. Including it would turn a 200 KB backup into a 100 MB one and
  make it too slow to take often. The file says so, in itself.
- **Credentials are left out by default.** The settings table holds the printer
  access code and the Bambu and Tapo passwords. A backup is a file that gets
  emailed, synced and copied to sticks; those four values are trivially
  retypeable and the rest of the file is not. `?secrets=1` / `--secrets`
  includes them and stamps a warning inside the file.
- **Restore never destroys by default.** `merge` inserts what is missing and
  leaves everything it finds alone, so restoring onto a database that has been
  used since cannot lose the newer work. `replace` empties each table first —
  and both the page and the tool run a **dry pass first** and report exactly how
  many rows would be deleted, because the preview has to be the truth. A test
  asserts the dry run's numbers match what the real one then does.

A backup from a newer version restores into an older one: only columns the
database actually has are written, so a column added later is dropped rather
than failing the restore on its first row.

### Keeping an idle app quiet

The NAS's disks can only hibernate if nothing is writing to them, so **an idle
printer must produce no writes at all**. That is not automatic: `on_message`
runs about once a second, and anything called from it inherits that rate.

Two things enforce it. `_maybe_record` gates telemetry behind the recording
mode — Auto writes only during a print plus a tail — and `_observe_filaments`
writes a tray only when its identity actually changed. `_cost_block` is cached,
because it reads the whole `prints` table and was being called per frame for a
figure that changes at most once a minute.

`GET /api/diag` reports every SQL statement the app has run, by verb and table,
alongside the MQTT frame count — so "what is it doing to the disk" is a
question the app can answer about itself:

```bash
python tools/why_disk_busy.py 60        # samples it twice, prints rates
```

With the printer idle it should say `WRITES in the window: 0`. If it does not,
the offending statement is named, and a rate close to the frame rate means
something is writing once per report, which is always a bug.

> This was worth building: two black PLA spools produce the same filament
> identity, but the printer reports their colour code with different padding
> (`A00-K00` and `A00-K0`). Each looked like a change to the other, so both
> wrote on every frame — about 100 `UPDATE filaments` a minute with nothing
> printing. Invisible from the outside, and obvious in one line of `/api/diag`.

### Adding another backend

`storage.py` has ~42 `self.backend` branches, which makes a third backend look
like a bigger job than it is. Three different questions are hiding behind that
one test, and only the third has anything to do with SQL:

| what the branch decides | how many | matters for a new backend? |
|---|---|---|
| **lifecycle** — sqlite reuses one connection and commits; a server opens one per call and closes it | 25 commits + 38 closes | no. Same for every server backend. |
| **row shape** — sqlite has `row_factory`, so rows are already dicts; PyMySQL returns tuples that get zipped with the column list | 7 | one line per driver |
| **dialect** — the only genuinely database-specific SQL | **5** | yes, and that is the whole list |

The five live in one table, `DIALECTS` at
[storage.py:248](storage.py#L248) — one row per backend, and adding a backend is
filling one in:

| key | sqlite | MariaDB / MySQL | used at |
|--|--|--|--|
| `auto` | `AUTOINCREMENT` | `AUTO_INCREMENT` | [storage.py:276](storage.py#L276) |
| `blob` | `BLOB` | `LONGBLOB` | [storage.py:277](storage.py#L277) |
| `inline_index` | separate `CREATE INDEX` after | inline in `CREATE TABLE` | [storage.py:324](storage.py#L324) |
| `columns` | `PRAGMA table_info` | `information_schema.COLUMNS` | [storage.py:420](storage.py#L420) |
| `upsert` | `REPLACE INTO` | `REPLACE INTO` | [storage.py:909](storage.py#L909), [storage.py:920](storage.py#L920) |

A sixth key, `server`, is not about SQL: it says whether the backend is reached
over TCP with a connection per call (and so needs no explicit commit), which is
the lifecycle question, and it is what the connect branch at
[storage.py:278](storage.py#L278) tests.

Two things make this smaller than it looks. There is **no
`ON DUPLICATE KEY UPDATE` anywhere** — the `prints` and `filaments` upserts are
hand-rolled UPDATE-then-INSERT, which is portable — so only those two
`REPLACE INTO` statements are dialect-locked. And the whole schema uses **six
column types**: `FLOAT`, `DOUBLE`, `TEXT`, `INTEGER`, `VARCHAR(n)`, `LONGBLOB`.
No booleans, no date functions, no `GROUP_CONCAT`, no quoted identifiers, one
`LIMIT`.

So, concretely:

- **MySQL** — **done.** PyMySQL *is* the MySQL driver; MariaDB is the fork, and
  all five dialect answers are identical, so MySQL runs byte-for-byte the same
  SQL MariaDB does. That is what the test asserts: it captures every statement
  `Storage` emits under each backend and requires the two lists to match, since
  "the same SQL as the one that works" is a stronger claim than any amount of
  reasoning about compatibility. MySQL 8's default `caching_sha2_password` needs
  the `cryptography` package, which is in `requirements.txt`, and
  `bootstrap.test()` recognises that failure and says so — including the
  `mysql_native_password` way out for anyone who would rather not install it.
- **PostgreSQL** — bounded, but real. Six changes: the psycopg driver and its
  dict cursor, `SERIAL PRIMARY KEY` in place of `INTEGER PRIMARY KEY
  AUTO_INCREMENT` (so `_auto` alone is not enough — the whole column spec
  differs), `BYTEA`, a separate `CREATE INDEX`, `INSERT … ON CONFLICT DO UPDATE`
  for those two statements, and a slightly different information_schema query.
  `rowcount` is a bonus: Postgres counts matched rows natively, so the
  `CLIENT.FOUND_ROWS` workaround at [storage.py:294](storage.py#L294) is not
  needed. The work that is **not** visible in a grep is type strictness —
  sqlite and MySQL accept `None` or `""` into a `FLOAT`, Postgres does not — and
  finding those needs the test suite run against a live server, not reasoning
  about the code.

Adding one now means a row in `DIALECTS`, a connect function, and a name in
`bootstrap.BACKENDS` — a test fails if those two lists disagree, and another
fails if a schema identifier turns out to be a reserved word.

Half of that tidy-up is now done: `DIALECTS` in
[storage.py](storage.py) holds the five SQL decisions, and the two that are
genuinely about syntax read from it. The other half — a `_finish(conn, cur)` for
the lifecycle and a `_rows(cur)` for the row shape — is **not** done, because
those 37 branches test `== "sqlite"` and so already do the right thing for any
server backend. They are churn, not correctness, and are only worth doing if a
backend ever arrives that is neither.

---

## The printer's USB drive

**Printer → USB drive** lists what is on the stick and how full it is. Two
figures from two sources, kept apart on purpose:

- **How full** comes from MQTT (`tl_external_total_kb` / `tl_external_free_kb`,
  with `sdcard` as the is-it-plugged-in flag). That is the *only* source: FTP can
  list a drive but has no command for its capacity — this server advertises
  neither `AVBL` nor `SITE` — so adding up file sizes could only ever say how
  much is visible, never how much is there.
- **What is on it** comes from FTP, walked on demand and cached for 30 seconds so
  that switching to the view and back does not dial the printer each time.

On the real drive those two disagree by about 6 GB: the printer reports 6.0 GB of
28.5 GB used, and the FTP view shows 12.5 MB. The page **says so** rather than
labelling the difference "other files" — nothing here has measured what that
space is, and a category invented to make a bar add up is a lie with a progress
bar on it.

Sliced jobs are tied back to the print they made (the file is
`<subtask name>.gcode.3mf`), so the list reads as a workshop rather than a
filesystem. The walk is bounded — three levels deep, 2000 entries — and says when
it stopped early, because a drive is somebody else's filesystem and can hold
anything. Nothing here writes to the drive.

## Slicer metadata

Some facts about a print exist only in the file that was sliced. Layer height is
the obvious one — it is in no MQTT frame and in no cloud response. So are the
profile name, the infill, the supports, and the exact gram figure the slicer
computed.

The printer keeps that file on a **USB drive** and serves it over the same FTPS
port MQTT's access code already unlocks, so this needs no account and no extra
credentials. Switch it on under **Settings → Slicer**; it is off by default,
because a printer with no drive in it would otherwise log a failure after every
print.

**What it reads.** The sliced file is a ZIP named `<subtask name>.gcode.3mf`.
Only two members matter, and neither of them is the gcode:

| member | uncompressed | what is read |
|---|---|---|
| `Metadata/project_settings.config` | 94 KB | all of it (13.6 KB in the archive) |
| `Metadata/slice_info.config` | 1.8 KB | all of it |
| `Metadata/model_settings.config` | 10 KB | all of it — the plate names |
| `Metadata/plate_15.gcode` | 11.2 MB | **the first 4 KB**, for the header block |

A ZIP can be read backwards — the index is at the end, and members can be
fetched individually — and FTP has `REST`, which starts a transfer at an offset.
Driving `zipfile` through a seekable FTP-`REST` file therefore reads about
**150 KB whatever the size of the job**. Measured against two real files:
149,429 bytes of 4,365,743 (3.4%) and 156,094 of 3,832,815 (4.1%), in under four
seconds each. On a 150 MB twelve-plate project it would still be about 150 KB.
This matters more than it looks: downloading whole jobs on a timer is how a NAS
disk never sleeps, and this app has been bitten by exactly that once already.

**Why the gcode header is worth a fifth read.** `project_settings` gives the
profile's *nominal* layer height, and layers × that height is the model's height
only while every layer really is that height. A height range modifier breaks it:
one real print here ran 767 layers of a `0.12mm High Quality` profile — 92.04 mm
by multiplication — and was actually **65.96 mm**, with layer steps from 0.012 to
0.12. The gcode header's `max_z_height` is measured rather than assumed, so the
panel shows that and shows nothing at all when the file has not been read. A
number that is wrong by 40% and looks authoritative is worse than no number.

**When it runs.** Once per finished print, on an event — never on a timer. The
file survives the job on this printer, so the read happens at *finish* rather
than at start, when the printer has better things to do. A print whose file
cannot be read is retried twice and then left alone; the detail panel has a
**Read it from the printer** button for trying again after putting the drive
back in. Turning the setting on backfills prints whose files are still there.

**What it will not do.** It refuses rather than guesses, in three places:

- the file is chosen by **name**, matched against the print. Not "the newest
  one" — that would put one print's layer height on another's row
- the plate number the file declares is cross-checked against the one MQTT
  reported, and a disagreement reads nothing
- the slicer's weight **fills a gap and never overwrites** a figure already
  there. Where both exist they have been identical (49.41 g from the cloud,
  `weight="49.41"` in the file), so there is nothing to gain by competing — but
  it does mean an install with no Bambu account now gets exact filament weights.

**What you typed always wins.** The parsed height lives in `layer_h` and yours
in `layer_h_manual`, so re-reading the file can refresh the automatic figure
without undoing a correction — the same split as `filament_g` /
`filament_g_manual`. Upgrading moves any height you typed before this existed
into the manual column and empties the other, so a hand-typed value never ends
up claiming to have come from the sliced file.

**The plate name** comes from `model_settings.config`, which holds the names you
gave each plate in the slicer — "Oberteile Herz", "blanko für Lichterkette". It
is not the object name: one real project has plate 9 called *Oberteile blanko*
holding an object called *Riffel gerade Lichterkette*. For anyone printing a
series it is the name they actually think in, and nothing else in the app has
ever known it.

Everything parsed is also kept as JSON in `prints.slice_json`, so a field worth
showing later needs no migration to reach the page. That block carries a `v`
stamp naming the parser that wrote it, and a print stamped older than the
current `gcode_meta.FORMAT` is read again. Without that, adding a field reaches
prints made *after* the upgrade only, and everything already recorded keeps a
block that silently lacks it — which is what happened when the plate name
arrived. Re-reading is only possible while the file is still on the drive;
prints whose file has been deleted keep what they have, and the detail panel's
**Read it again** button is there for after you put one back.

There is more in the file that is deliberately left alone. The 30-long lists
(`nozzle_temperature` and 42 others) are the whole **filament library**, not this
print, so indexing them by slot would quietly report another spool's numbers;
the 6-long speed lists are ambiguous about what indexes them; and
`plate_N.json`'s `bbox_objects[].layer_height` says `0.2` for a plate sliced at
`0.12`. The lists that *are* safe are the 5-long ones — `filament_cost`,
`filament_density`, `filament_settings_id` — indexed by `slice_info`'s filament
`id` minus one, verified against two independent jobs.

## How the tricky bits work

A few behaviours are non-obvious because the printer's raw data is messy:

- **"Left" can be corrected, and the correction ages.** The Left column is
  bought minus used, and both halves come from logs that can be incomplete: a
  deleted print stops counting as used, and a spool bought before the invoice
  importer existed never counted as bought. Either way the figure drifts, and no
  arithmetic can recover the truth — but the AMS weighs an RFID spool and simply
  knows, which is what the Status column shows.

  So the cell is clickable: type a number, or press **aus dem AMS** to take what
  the printer reports for that spool right now. What is stored is not the number
  but an **anchor** — "N grams left, as of then" — and prints and purchases after
  that moment keep moving it. A plain override would be wrong again after the
  next print; an anchor stays right. A pinned figure is shown in bold, with the
  date in its tooltip, and emptying the box hands it back to the arithmetic.

  The AMS button appears only for a spool that reports a real remaining amount.
  A third-party tray sends `-1` and an external spool `0`, and neither means
  empty, so neither is offered.

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
| `#1054 Unknown column …` (MariaDB/MySQL)            | Backend is on a server but the schema is behind; the column-migration runs on startup — restart the app, or check the connection didn't get flipped to sqlite. |
| `caching_sha2_password cannot be loaded` (MySQL 8)  | PyMySQL needs the `cryptography` package for MySQL 8's default auth plugin: `./venv/bin/python3 -m pip install -r requirements.txt`. Or give the user the older plugin — see the note in `deploy/schema_and_user.sql`. |
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
| Live view tab missing                               | `camera.enabled` is false, or `/api/camera` reports disabled. Tick it under Settings → Camera and restart. |
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
plaintext (in the database, and the database password in `instance/db.json`,
which is chmod 600 and git-ignored); this is fine for a trusted
LAN/NAS deployment but the app should **not be exposed to the public internet**.

## License

Released under the [MIT License](LICENSE).
