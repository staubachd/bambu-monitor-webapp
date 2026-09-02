# Bambu Monitor — internals

Notes for anyone changing the code. None of this is needed to *run* the app —
see the [README](../README.md) for that. It is kept because each of these was
learned the hard way against a real printer, and every one of them looks like a
bug in the app until you know the reason.

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
[storage.py:248](../storage.py#L248) — one row per backend, and adding a backend is
filling one in:

| key | sqlite | MariaDB / MySQL | used at |
|--|--|--|--|
| `auto` | `AUTOINCREMENT` | `AUTO_INCREMENT` | [storage.py:276](../storage.py#L276) |
| `blob` | `BLOB` | `LONGBLOB` | [storage.py:277](../storage.py#L277) |
| `inline_index` | separate `CREATE INDEX` after | inline in `CREATE TABLE` | [storage.py:324](../storage.py#L324) |
| `columns` | `PRAGMA table_info` | `information_schema.COLUMNS` | [storage.py:420](../storage.py#L420) |
| `upsert` | `REPLACE INTO` | `REPLACE INTO` | [storage.py:909](../storage.py#L909), [storage.py:920](../storage.py#L920) |

A sixth key, `server`, is not about SQL: it says whether the backend is reached
over TCP with a connection per call (and so needs no explicit commit), which is
the lifecycle question, and it is what the connect branch at
[storage.py:278](../storage.py#L278) tests.

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
  `CLIENT.FOUND_ROWS` workaround at [storage.py:294](../storage.py#L294) is not
  needed. The work that is **not** visible in a grep is type strictness —
  sqlite and MySQL accept `None` or `""` into a `FLOAT`, Postgres does not — and
  finding those needs the test suite run against a live server, not reasoning
  about the code.

Adding one now means a row in `DIALECTS`, a connect function, and a name in
`bootstrap.BACKENDS` — a test fails if those two lists disagree, and another
fails if a schema identifier turns out to be a reserved word.

Half of that tidy-up is now done: `DIALECTS` in
[storage.py](../storage.py) holds the five SQL decisions, and the two that are
genuinely about syntax read from it. The other half — a `_finish(conn, cur)` for
the lifecycle and a `_rows(cur)` for the row shape — is **not** done, because
those 37 branches test `== "sqlite"` and so already do the right thing for any
server backend. They are churn, not correctness, and are only worth doing if a
backend ever arrives that is neither.

---

---

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
