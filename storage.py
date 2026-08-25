#!/usr/bin/env python3
"""
Telemetry storage with two interchangeable backends:
  - sqlite  : zero-config single file, used for local development on Windows
  - mariadb : reuses the Synology's existing MariaDB server in production

The app talks to one Storage object; the backend is chosen by config, so the
same code runs locally (sqlite) and on the NAS (mariadb).

Schema (one flat time-series table + a per-print summary table):
  telemetry(ts, gcode_state, percent, layer, total_layers,
            bed_cur, bed_tgt, noz0, noz1, noz_tgt, chamber,
            fan_cooling, speed_mag, wifi_dbm)
  prints(job_id, name, started_at, ended_at, final_state, total_layers)
"""
from __future__ import annotations

import time

TELEMETRY_COLS = [
    "gcode_state", "percent", "layer", "total_layers",
    "bed_cur", "bed_tgt", "noz0", "noz1", "noz_tgt", "chamber",
    "fan_cooling", "speed_mag", "wifi_dbm",
    "power_w", "energy_today_wh",
]

# columns added after the first release - migrated onto existing tables
LATE_COLUMNS = {
    "telemetry": {"power_w": "FLOAT", "energy_today_wh": "FLOAT"},
    "prints": {"energy_wh": "FLOAT", "cost": "FLOAT", "peak_w": "FLOAT",
               "label": "VARCHAR(255)", "design_title": "VARCHAR(255)",
               "filament_g": "FLOAT", "filament_g_manual": "FLOAT",
               "filament_detail": "TEXT", "filament_cost": "FLOAT",
               "ams_bambu": "TEXT", "error_code": "VARCHAR(64)",
               "design_id": "VARCHAR(32)", "profile_id": "VARCHAR(32)",
               "pgroup": "VARCHAR(120)", "ams_slots": "TEXT"},
    # `code` arrived with invoice import, one release after the table itself.
    # CREATE TABLE IF NOT EXISTS silently does nothing to an existing table, so
    # every column added later has to be listed here or the INSERT breaks.
    "purchases": {"code": "VARCHAR(24)", "list_price": "FLOAT"},
    # who made it. The RFID tag only says genuine-or-not, never a manufacturer,
    # so for third-party spools this can only come from the user.
    # a price typed in by hand, per filament identity - the config matrix
    # cannot know what a third-party spool cost
    "filaments": {"vendor": "VARCHAR(64)", "alias_of": "VARCHAR(64)",
                  "price_per_kg": "FLOAT"},
    # notes shipped before they had categories
    "notes": {"category": "VARCHAR(60)"},
}

PRINT_COLS = ["job_id", "name", "started_at", "ended_at", "final_state",
              "total_layers", "energy_wh", "cost", "peak_w", "label",
              "design_title", "filament_g", "filament_g_manual",
              "filament_detail", "filament_cost", "ams_bambu", "error_code",
              "design_id", "profile_id", "pgroup", "ams_slots"]

# Never touched by upsert_print's UPDATE branch (which runs from the MQTT loop):
#   started_at        - so a restart mid-print can't rewrite when the job began
#   label             - user-supplied, must survive the 60s upserts
#   filament_g_manual - user-supplied override
#   design_title / filament_* - owned by the cloud updater; the MQTT path knows
#                       nothing about them and would otherwise null them out
#   pgroup            - user-assigned group, same reasoning as label
#   ams_slots         - what the AMS held while the print ran, written once
#   design_id/profile_id - the MakerWorld link. The printer reports it as machine
#                       state and stops reporting it for a self-sliced job, so a
#                       blanket upsert would first inherit the previous print's
#                       link and then null out a correct one. Written only when
#                       something actually has a value for it.
PRINT_IMMUTABLE = {"job_id", "started_at", "label", "design_title",
                   "filament_g", "filament_g_manual", "filament_detail",
                   "filament_cost", "ams_bambu", "error_code", "pgroup",
                   "ams_slots", "design_id", "profile_id"}

# Identity of every filament ever seen in the AMS, so the Filament page can name
# a spool long after it has been used up and removed. Usage figures are NOT here:
# those are aggregated from prints.filament_detail, which already has them for
# every past print (see app._filament_stats).
FILAMENT_COLS = ["fkey", "filament_id", "code", "vendor", "product", "color",
                 "color_name", "type", "is_bambu", "alias_of", "price_per_kg",
                 "first_seen", "last_seen"]

# What was bought, as opposed to what was used. Kept as one row per order LINE
# (not per spool) so an order of 3 spools stays one editable, deletable entry.
# `fkey` links to a filament identity when it is known; when it is not, the free
# text product/colour still identifies the line on screen.
#   total_price - what the line actually cost, after discount (line total)
#   list_price  - the undiscounted price of ONE unit; this is what per-print
#                 costing uses, because a one-off discount is not what replacing
#                 that filament will cost you
PURCHASE_COLS = ["fkey", "code", "product", "color_name", "color", "type",
                 "spools", "grams_each", "total_price", "list_price", "currency",
                 "ordered_at", "order_ref", "note", "created_at"]


def _row_from_state(s: dict) -> dict:
    t = s.get("temps", {}) or {}
    j = s.get("job", {}) or {}
    nz = t.get("nozzles", []) or []
    by_id = {n.get("id"): n for n in nz}
    return {
        "gcode_state": j.get("state"),
        "percent": j.get("percent"),
        "layer": j.get("layer"),
        "total_layers": j.get("total_layers"),
        "bed_cur": (t.get("bed") or {}).get("cur"),
        "bed_tgt": (t.get("bed") or {}).get("target"),
        # Keyed to the FIRMWARE extruder id, not list position, so that changing
        # the display order never reinterprets rows already in the database.
        # noz1 = firmware id 1 = the main direct-drive nozzle (shown as "Nozzle 1").
        "noz0": (by_id.get(0) or {}).get("temp"),
        "noz1": (by_id.get(1) or {}).get("temp"),
        "noz_tgt": t.get("active_nozzle_target"),
        "chamber": (t.get("chamber") or {}).get("cur"),
        "fan_cooling": (s.get("fans") or {}).get("cooling"),
        "speed_mag": (s.get("speed") or {}).get("magnitude_pct"),
        "wifi_dbm": (s.get("printer") or {}).get("wifi_dbm"),
        # from the smart plug, merged into the state by app.py (may be absent)
        "power_w": (s.get("power") or {}).get("watts"),
        "energy_today_wh": (s.get("power") or {}).get("today_wh"),
    }


class Storage:
    def __init__(self, cfg: dict):
        self.backend = cfg.get("backend", "sqlite")
        if self.backend == "mariadb":
            import pymysql  # lazy: only needed on the NAS
            from pymysql.constants import CLIENT
            m = cfg["mariadb"]
            # FOUND_ROWS makes cursor.rowcount after an UPDATE count the rows
            # MATCHED, not the rows whose values actually differed. Every
            # `return n > 0` in this file means "the row existed", and without
            # this flag MariaDB answers 0 for a save that writes what was already
            # there - so re-saving an unchanged name reported "no such filament"
            # while the same code was fine on sqlite, which counts matches.
            self._connect = lambda: pymysql.connect(
                host=m.get("host", "127.0.0.1"), port=int(m.get("port", 3306)),
                user=m["user"], password=m["password"], database=m["database"],
                autocommit=True, charset="utf8mb4",
                client_flag=CLIENT.FOUND_ROWS,
            )
            self.ph = "%s"
            self._auto = "AUTO_INCREMENT"
            self._blob = "LONGBLOB"
        else:
            import sqlite3
            path = cfg.get("sqlite_path", "telemetry.db")
            # check_same_thread=False: the MQTT thread writes, Flask threads read
            self._conn = sqlite3.connect(path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._connect = lambda: self._conn
            self.ph = "?"
            self._auto = "AUTOINCREMENT"
            self._blob = "BLOB"
        self._init_schema()

    # sqlite reuses one connection; mariadb opens per-call (thread-safe, cheap on LAN)
    def _cursor(self):
        conn = self._connect()
        return conn, conn.cursor()

    def _init_schema(self):
        conn, cur = self._cursor()
        # MariaDB declares the ts index inline (works on all versions, incl. 10.3
        # where CREATE INDEX IF NOT EXISTS is unsupported); sqlite adds it after.
        inline_idx = ",\n                INDEX idx_telemetry_ts (ts)" if self.backend != "sqlite" else ""
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS telemetry (
                id INTEGER PRIMARY KEY {self._auto},
                ts DOUBLE NOT NULL,
                gcode_state VARCHAR(16), percent INT,
                layer INT, total_layers INT,
                bed_cur FLOAT, bed_tgt FLOAT,
                noz0 FLOAT, noz1 FLOAT, noz_tgt FLOAT, chamber FLOAT,
                fan_cooling INT, speed_mag INT, wifi_dbm INT,
                power_w FLOAT, energy_today_wh FLOAT{inline_idx}
            )""")
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS prints (
                job_id VARCHAR(32) PRIMARY KEY,
                name VARCHAR(255),
                started_at DOUBLE, ended_at DOUBLE,
                final_state VARCHAR(16), total_layers INT
            )""")
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS hms_ack (
                code VARCHAR(32) NOT NULL,
                ts VARCHAR(20) NOT NULL,
                acked_at DOUBLE,
                PRIMARY KEY (code, ts)
            )""")
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS filaments (
                fkey VARCHAR(64) PRIMARY KEY,
                filament_id VARCHAR(16), code VARCHAR(24), vendor VARCHAR(64),
                product VARCHAR(64), color VARCHAR(8), color_name VARCHAR(64),
                type VARCHAR(24), is_bambu INT, alias_of VARCHAR(64),
                first_seen DOUBLE, last_seen DOUBLE
            )""")
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS purchases (
                id INTEGER PRIMARY KEY {self._auto},
                fkey VARCHAR(64), code VARCHAR(24),
                product VARCHAR(64), color_name VARCHAR(64),
                color VARCHAR(8), type VARCHAR(24),
                spools INT, grams_each FLOAT,
                total_price FLOAT, list_price FLOAT, currency VARCHAR(8),
                ordered_at DOUBLE, order_ref VARCHAR(64),
                note VARCHAR(255), created_at DOUBLE
            )""")
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY {self._auto},
                title VARCHAR(200), body TEXT, category VARCHAR(60),
                created_at DOUBLE, updated_at DOUBLE
            )""")
        # Pictures live in the database with the note they belong to, so one
        # backup covers both. The browser downscales before upload, so these are
        # a few hundred KB rather than phone-camera sized.
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS note_images (
                id INTEGER PRIMARY KEY {self._auto},
                note_id INT, mime VARCHAR(40), data {self._blob},
                w INT, h INT, size INT, created_at DOUBLE
            )""")
        # skey/svalue rather than key/value - 'key' is reserved in MySQL/MariaDB
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS settings (
                skey VARCHAR(48) PRIMARY KEY,
                svalue VARCHAR(255)
            )""")
        # migrate columns onto tables created by an earlier version
        for table, cols in LATE_COLUMNS.items():
            existing = self._existing_columns(cur, table)
            for col, ddl in cols.items():
                if col not in existing:
                    cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
                    print(f"[store] migrated: added {table}.{col}")
        if self.backend == "sqlite":
            cur.execute("CREATE INDEX IF NOT EXISTS idx_telemetry_ts ON telemetry(ts)")
            conn.commit()
        else:
            cur.close(); conn.close()

    def _existing_columns(self, cur, table: str) -> set:
        if self.backend == "sqlite":
            cur.execute(f"PRAGMA table_info({table})")
            return {r[1] for r in cur.fetchall()}
        cur.execute(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s", (table,))
        return {r[0] for r in cur.fetchall()}

    def record(self, state: dict) -> None:
        row = _row_from_state(state)
        cols = ["ts"] + TELEMETRY_COLS
        vals = [time.time()] + [row[c] for c in TELEMETRY_COLS]
        placeholders = ",".join([self.ph] * len(cols))
        sql = f"INSERT INTO telemetry ({','.join(cols)}) VALUES ({placeholders})"
        conn, cur = self._cursor()
        cur.execute(sql, vals)
        if self.backend == "sqlite":
            conn.commit()
        else:
            cur.close(); conn.close()

    def history(self, hours: float = 6.0, max_points: int = 720) -> list[dict]:
        since = time.time() - hours * 3600
        conn, cur = self._cursor()
        cur.execute(
            f"SELECT ts,{','.join(TELEMETRY_COLS)} FROM telemetry "
            f"WHERE ts >= {self.ph} ORDER BY ts ASC", (since,))
        rows = cur.fetchall()
        if self.backend == "sqlite":
            out = [dict(r) for r in rows]
        else:
            cur.close(); conn.close()
            cols = ["ts"] + TELEMETRY_COLS
            out = [dict(zip(cols, r)) for r in rows]
        # thin to max_points so the chart stays light over long ranges
        if len(out) > max_points:
            step = len(out) / max_points
            out = [out[int(i * step)] for i in range(max_points)]
        return out

    # ---- print history ----
    def upsert_print(self, **row) -> None:
        """Insert or update one print. started_at is preserved on update so a
        restart mid-print cannot rewrite when the job actually began."""
        conn, cur = self._cursor()
        cur.execute(f"SELECT job_id FROM prints WHERE job_id={self.ph}", (row["job_id"],))
        exists = cur.fetchone()
        if exists:
            cols = [c for c in PRINT_COLS if c not in PRINT_IMMUTABLE]
            sets = ", ".join(f"{c}={self.ph}" for c in cols)
            cur.execute(f"UPDATE prints SET {sets} WHERE job_id={self.ph}",
                        [row.get(c) for c in cols] + [row["job_id"]])
        else:
            ph = ",".join([self.ph] * len(PRINT_COLS))
            cur.execute(f"INSERT INTO prints ({','.join(PRINT_COLS)}) VALUES ({ph})",
                        [row.get(c) for c in PRINT_COLS])
        if self.backend == "sqlite":
            conn.commit()
        else:
            cur.close(); conn.close()

    def update_print_fields(self, job_id: str, **fields) -> bool:
        """Write the cloud-owned columns. Separate from upsert_print because
        those columns are deliberately immutable there - the MQTT loop has no
        filament data and must never blank them."""
        fields = {k: v for k, v in fields.items() if k in PRINT_COLS and k != "job_id"}
        if not fields:
            return False
        sets = ", ".join(f"{k}={self.ph}" for k in fields)
        conn, cur = self._cursor()
        cur.execute(f"UPDATE prints SET {sets} WHERE job_id={self.ph}",
                    list(fields.values()) + [job_id])
        n = cur.rowcount
        if self.backend == "sqlite":
            conn.commit()
        else:
            cur.close(); conn.close()
        return n > 0

    def design_id_for_title(self, title: str, exclude_job: str = "") -> tuple | None:
        """The MakerWorld ids most recently recorded against this exact design
        title. The live path refuses to attribute a repeated model id to a new
        job (it cannot tell a repeat print from a leftover); the cloud gives that
        job an authoritative title, and the same title means the same model."""
        conn, cur = self._cursor()
        cur.execute(
            f"SELECT design_id, profile_id FROM prints "
            f"WHERE design_title={self.ph} AND design_id IS NOT NULL "
            f"AND job_id<>{self.ph} ORDER BY started_at DESC",
            (title, exclude_job))
        row = cur.fetchone()
        if self.backend != "sqlite":
            cur.close(); conn.close()
        return (row[0], row[1]) if row else None

    def get_print(self, job_id: str) -> dict | None:
        conn, cur = self._cursor()
        cur.execute(f"SELECT {','.join(PRINT_COLS)} FROM prints WHERE job_id={self.ph}",
                    (job_id,))
        r = cur.fetchone()
        if self.backend == "sqlite":
            return dict(r) if r else None
        cur.close(); conn.close()
        return dict(zip(PRINT_COLS, r)) if r else None

    def set_print_label(self, job_id: str, label: str | None) -> bool:
        """Set (or clear, when label is empty) the user's own name for a print."""
        conn, cur = self._cursor()
        cur.execute(f"UPDATE prints SET label={self.ph} WHERE job_id={self.ph}",
                    (label or None, job_id))
        n = cur.rowcount
        if self.backend == "sqlite":
            conn.commit()
        else:
            cur.close(); conn.close()
        return n > 0

    def set_print_group(self, job_ids: list, name: str | None) -> int:
        """Put prints into a named group, or out of one when name is empty.

        The group is just a name stored on each print - no second table and no
        ids to keep in step. Renaming is therefore 'set the new name on the same
        prints', and a group stops existing when its last member leaves.
        """
        job_ids = [str(j) for j in (job_ids or []) if j]
        if not job_ids:
            return 0
        marks = ",".join([self.ph] * len(job_ids))
        conn, cur = self._cursor()
        cur.execute(f"UPDATE prints SET pgroup={self.ph} WHERE job_id IN ({marks})",
                    [name or None] + job_ids)
        n = cur.rowcount
        if self.backend == "sqlite":
            conn.commit()
        else:
            cur.close(); conn.close()
        return n

    def delete_print(self, job_id: str) -> bool:
        """Drop one print from the history for good. Telemetry samples are left
        alone: that table is a plain time series, not owned by any single job."""
        conn, cur = self._cursor()
        cur.execute(f"DELETE FROM prints WHERE job_id={self.ph}", (job_id,))
        n = cur.rowcount
        if self.backend == "sqlite":
            conn.commit()
        else:
            cur.close(); conn.close()
        return n > 0

    def recent_prints(self, limit: int = 60) -> list[dict]:
        limit = max(1, min(int(limit), 500))   # cast + clamp: never interpolate raw input
        conn, cur = self._cursor()
        cur.execute(f"SELECT {','.join(PRINT_COLS)} FROM prints "
                    f"ORDER BY started_at DESC LIMIT {limit}")
        rows = cur.fetchall()
        if self.backend == "sqlite":
            out = [dict(r) for r in rows]
        else:
            cur.close(); conn.close()
            out = [dict(zip(PRINT_COLS, r)) for r in rows]
        return out

    def all_prints(self) -> list[dict]:
        """Every print row, newest-first - for the stats/maintenance aggregations
        that need the full history rather than a recent window."""
        conn, cur = self._cursor()
        cur.execute(f"SELECT {','.join(PRINT_COLS)} FROM prints ORDER BY started_at DESC")
        rows = cur.fetchall()
        if self.backend == "sqlite":
            out = [dict(r) for r in rows]
        else:
            cur.close(); conn.close()
            out = [dict(zip(PRINT_COLS, r)) for r in rows]
        return out

    # ---- filament identities seen in the AMS ----
    def upsert_filament(self, fkey: str, **fields) -> None:
        """Record/refresh one filament identity.

        first_seen is stamped once and never rewritten. On update only fields
        that actually carry a value are written, so a later observation with an
        unread RFID tag (product/code blank) cannot erase what an earlier, better
        read already established.
        """
        fields = {k: v for k, v in fields.items()
                  if k in FILAMENT_COLS and k not in ("fkey", "first_seen")}
        now = time.time()
        conn, cur = self._cursor()
        cur.execute(f"SELECT fkey FROM filaments WHERE fkey={self.ph}", (fkey,))
        if cur.fetchone():
            sets = {k: v for k, v in fields.items() if v is not None}
            sets["last_seen"] = now
            clause = ", ".join(f"{k}={self.ph}" for k in sets)
            cur.execute(f"UPDATE filaments SET {clause} WHERE fkey={self.ph}",
                        list(sets.values()) + [fkey])
        else:
            row = {**{c: None for c in FILAMENT_COLS}, **fields,
                   "fkey": fkey, "first_seen": now, "last_seen": now}
            ph = ",".join([self.ph] * len(FILAMENT_COLS))
            cur.execute(f"INSERT INTO filaments ({','.join(FILAMENT_COLS)}) VALUES ({ph})",
                        [row[c] for c in FILAMENT_COLS])
        if self.backend == "sqlite":
            conn.commit()
        else:
            cur.close(); conn.close()

    def set_filament_identity(self, fkey: str, **fields) -> bool:
        """Write the user-owned identity fields of one filament.

        Unlike upsert_filament this writes exactly what it is given, empty
        included - naming something is also being able to un-name it. Only ever
        called from the UI, so the AMS observer can keep its own rules.
        """
        fields = {k: v for k, v in fields.items()
                  if k in ("vendor", "product", "color_name")}
        if not fields:
            return False
        sets = ", ".join(f"{k}={self.ph}" for k in fields)
        conn, cur = self._cursor()
        cur.execute(f"UPDATE filaments SET {sets} WHERE fkey={self.ph}",
                    list(fields.values()) + [fkey])
        n = cur.rowcount
        if self.backend == "sqlite":
            conn.commit()
        else:
            cur.close(); conn.close()
        return n > 0

    def set_filament_price(self, fkey: str, per_kg: float | None) -> bool:
        """The price of one filament, per kg, set by hand.

        Nullable on purpose: clearing it hands the filament back to the
        configured brand x material matrix rather than pinning it at zero.
        """
        conn, cur = self._cursor()
        cur.execute(f"UPDATE filaments SET price_per_kg={self.ph} WHERE fkey={self.ph}",
                    (per_kg, fkey))
        n = cur.rowcount
        if self.backend == "sqlite":
            conn.commit()
        else:
            cur.close(); conn.close()
        return n > 0

    def set_filament_alias(self, fkey: str, target: str | None) -> bool:
        """Fold one identity into another, or (target None) stop folding it.

        Needed because the two sources of an identity can disagree: the cloud
        reports the slicer PROFILE, not what was in the tray, so one spool can
        show up under several SKUs. An alias says "these are one filament".
        """
        conn, cur = self._cursor()
        cur.execute(f"UPDATE filaments SET alias_of={self.ph} WHERE fkey={self.ph}",
                    (target or None, fkey))
        n = cur.rowcount
        if self.backend == "sqlite":
            conn.commit()
        else:
            cur.close(); conn.close()
        return n > 0

    def delete_filament(self, fkey: str) -> bool:
        """Forget an identity. Anything folded into it is unfolded first, so no
        row is left pointing at something that no longer exists."""
        conn, cur = self._cursor()
        cur.execute(f"UPDATE filaments SET alias_of=NULL WHERE alias_of={self.ph}", (fkey,))
        cur.execute(f"DELETE FROM filaments WHERE fkey={self.ph}", (fkey,))
        n = cur.rowcount
        if self.backend == "sqlite":
            conn.commit()
        else:
            cur.close(); conn.close()
        return n > 0

    def set_filament_color(self, fkey: str, color_name: str) -> bool:
        """Rename one identity's colour. Used when an invoice teaches the real
        name - Bambu's own wording outranks the built-in guess table and anything
        observed earlier. Callers match the code themselves, because codes have
        to be compared in canonical form (see filament_catalog.norm_code)."""
        conn, cur = self._cursor()
        cur.execute(f"UPDATE filaments SET color_name={self.ph} WHERE fkey={self.ph}",
                    (color_name, fkey))
        n = cur.rowcount
        if self.backend == "sqlite":
            conn.commit()
        else:
            cur.close(); conn.close()
        return n > 0

    def all_filaments(self) -> list[dict]:
        conn, cur = self._cursor()
        cur.execute(f"SELECT {','.join(FILAMENT_COLS)} FROM filaments")
        rows = cur.fetchall()
        if self.backend == "sqlite":
            return [dict(r) for r in rows]
        cur.close(); conn.close()
        return [dict(zip(FILAMENT_COLS, r)) for r in rows]

    # ---- filament purchases ----
    def add_purchase(self, **row) -> int:
        """Insert one order line. Returns its new id."""
        row = {**{c: None for c in PURCHASE_COLS}, **{k: v for k, v in row.items()
                                                      if k in PURCHASE_COLS}}
        row["created_at"] = time.time()
        ph = ",".join([self.ph] * len(PURCHASE_COLS))
        conn, cur = self._cursor()
        cur.execute(f"INSERT INTO purchases ({','.join(PURCHASE_COLS)}) VALUES ({ph})",
                    [row[c] for c in PURCHASE_COLS])
        pid = cur.lastrowid
        if self.backend == "sqlite":
            conn.commit()
        else:
            cur.close(); conn.close()
        return int(pid or 0)

    def all_purchases(self) -> list[dict]:
        cols = ["id"] + PURCHASE_COLS
        conn, cur = self._cursor()
        cur.execute(f"SELECT {','.join(cols)} FROM purchases "
                    "ORDER BY COALESCE(ordered_at, created_at) DESC")
        rows = cur.fetchall()
        if self.backend == "sqlite":
            return [dict(r) for r in rows]
        cur.close(); conn.close()
        return [dict(zip(cols, r)) for r in rows]

    def delete_purchase(self, pid: int) -> bool:
        conn, cur = self._cursor()
        cur.execute(f"DELETE FROM purchases WHERE id={self.ph}", (int(pid),))
        n = cur.rowcount
        if self.backend == "sqlite":
            conn.commit()
        else:
            cur.close(); conn.close()
        return n > 0

    # ---- notes ----
    def all_notes(self) -> list[dict]:
        cols = ["id", "title", "body", "category", "created_at", "updated_at"]
        conn, cur = self._cursor()
        cur.execute(f"SELECT {','.join(cols)} FROM notes ORDER BY updated_at DESC")
        rows = cur.fetchall()
        if self.backend == "sqlite":
            return [dict(r) for r in rows]
        cur.close(); conn.close()
        return [dict(zip(cols, r)) for r in rows]

    def add_note(self, title: str | None, body: str | None,
                 category: str | None = None) -> int:
        now = time.time()
        conn, cur = self._cursor()
        cur.execute(f"INSERT INTO notes (title, body, category, created_at, updated_at) "
                    f"VALUES ({self.ph},{self.ph},{self.ph},{self.ph},{self.ph})",
                    (title, body, category, now, now))
        nid = cur.lastrowid
        if self.backend == "sqlite":
            conn.commit()
        else:
            cur.close(); conn.close()
        return int(nid or 0)

    def update_note(self, nid: int, title: str | None, body: str | None,
                    category: str | None = None) -> bool:
        """created_at is left alone, so the list can still be ordered by when a
        note was last touched without losing when it was written."""
        conn, cur = self._cursor()
        cur.execute(f"UPDATE notes SET title={self.ph}, body={self.ph}, "
                    f"category={self.ph}, updated_at={self.ph} WHERE id={self.ph}",
                    (title, body, category, time.time(), int(nid)))
        n = cur.rowcount
        if self.backend == "sqlite":
            conn.commit()
        else:
            cur.close(); conn.close()
        return n > 0

    def delete_note(self, nid: int) -> bool:
        """Takes the note's pictures with it - there is no other way to reach
        them once the note is gone, and orphaned blobs would just accumulate."""
        conn, cur = self._cursor()
        cur.execute(f"DELETE FROM note_images WHERE note_id={self.ph}", (int(nid),))
        cur.execute(f"DELETE FROM notes WHERE id={self.ph}", (int(nid),))
        n = cur.rowcount
        if self.backend == "sqlite":
            conn.commit()
        else:
            cur.close(); conn.close()
        return n > 0

    # ---- note pictures ----
    def add_note_image(self, note_id: int, mime: str, data: bytes,
                       w: int = 0, h: int = 0) -> int:
        conn, cur = self._cursor()
        cur.execute(f"INSERT INTO note_images (note_id, mime, data, w, h, size, created_at) "
                    f"VALUES ({self.ph},{self.ph},{self.ph},{self.ph},{self.ph},{self.ph},{self.ph})",
                    (int(note_id), mime, data, int(w), int(h), len(data), time.time()))
        iid = cur.lastrowid
        if self.backend == "sqlite":
            conn.commit()
        else:
            cur.close(); conn.close()
        return int(iid or 0)

    def note_image_index(self) -> dict:
        """{note_id: [{id, w, h, size}, …]} - metadata only. The bytes are
        fetched one at a time by the browser, never bundled into the note list."""
        conn, cur = self._cursor()
        cur.execute("SELECT id, note_id, w, h, size FROM note_images ORDER BY id")
        rows = cur.fetchall()
        if self.backend != "sqlite":
            cur.close(); conn.close()
        out = {}
        for r in rows:
            out.setdefault(int(r[1]), []).append(
                {"id": int(r[0]), "w": r[2], "h": r[3], "size": r[4]})
        return out

    def get_note_image(self, iid: int):
        conn, cur = self._cursor()
        cur.execute(f"SELECT mime, data FROM note_images WHERE id={self.ph}", (int(iid),))
        r = cur.fetchone()
        if self.backend != "sqlite":
            cur.close(); conn.close()
        return (r[0], bytes(r[1])) if r else (None, None)

    def delete_note_image(self, iid: int) -> bool:
        conn, cur = self._cursor()
        cur.execute(f"DELETE FROM note_images WHERE id={self.ph}", (int(iid),))
        n = cur.rowcount
        if self.backend == "sqlite":
            conn.commit()
        else:
            cur.close(); conn.close()
        return n > 0

    # ---- key/value settings (survive restarts) ----
    def get_setting(self, key: str, default=None):
        conn, cur = self._cursor()
        cur.execute(f"SELECT svalue FROM settings WHERE skey={self.ph}", (key,))
        row = cur.fetchone()
        if self.backend != "sqlite":
            cur.close(); conn.close()
        return row[0] if row else default

    def settings_with_prefix(self, prefix: str) -> dict:
        """All settings whose key starts with prefix, keyed without it."""
        conn, cur = self._cursor()
        cur.execute(f"SELECT skey, svalue FROM settings WHERE skey LIKE {self.ph}",
                    (prefix + "%",))
        rows = cur.fetchall()
        if self.backend != "sqlite":
            cur.close(); conn.close()
        return {r[0][len(prefix):]: r[1] for r in rows}

    def set_setting(self, key: str, value) -> None:
        conn, cur = self._cursor()
        cur.execute(f"REPLACE INTO settings (skey, svalue) VALUES ({self.ph},{self.ph})",
                    (key, str(value)))
        if self.backend == "sqlite":
            conn.commit()
        else:
            cur.close(); conn.close()

    # ---- HMS acknowledgements (code+ts identifies one occurrence) ----
    def ack_hms(self, code: str, ts: str) -> None:
        conn, cur = self._cursor()
        cur.execute(
            f"REPLACE INTO hms_ack (code, ts, acked_at) VALUES ({self.ph},{self.ph},{self.ph})",
            (code, ts or "", time.time()))
        if self.backend == "sqlite":
            conn.commit()
        else:
            cur.close(); conn.close()

    def unack_hms(self, code: str, ts: str) -> None:
        conn, cur = self._cursor()
        cur.execute(f"DELETE FROM hms_ack WHERE code={self.ph} AND ts={self.ph}",
                    (code, ts or ""))
        if self.backend == "sqlite":
            conn.commit()
        else:
            cur.close(); conn.close()

    def acked_keys(self) -> set:
        conn, cur = self._cursor()
        cur.execute("SELECT code, ts FROM hms_ack")
        rows = cur.fetchall()
        if self.backend != "sqlite":
            cur.close(); conn.close()
        return {(r[0], r[1]) for r in rows}

    def purge(self, keep_days: float = 30.0) -> int:
        cutoff = time.time() - keep_days * 86400
        conn, cur = self._cursor()
        cur.execute(f"DELETE FROM telemetry WHERE ts < {self.ph}", (cutoff,))
        n = cur.rowcount
        if self.backend == "sqlite":
            conn.commit()
        else:
            cur.close(); conn.close()
        return n
