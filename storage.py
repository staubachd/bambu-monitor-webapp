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
               "ams_bambu": "TEXT", "error_code": "VARCHAR(64)"},
}

PRINT_COLS = ["job_id", "name", "started_at", "ended_at", "final_state",
              "total_layers", "energy_wh", "cost", "peak_w", "label",
              "design_title", "filament_g", "filament_g_manual",
              "filament_detail", "filament_cost", "ams_bambu", "error_code"]

# Never touched by upsert_print's UPDATE branch (which runs from the MQTT loop):
#   started_at        - so a restart mid-print can't rewrite when the job began
#   label             - user-supplied, must survive the 60s upserts
#   filament_g_manual - user-supplied override
#   design_title / filament_* - owned by the cloud updater; the MQTT path knows
#                       nothing about them and would otherwise null them out
PRINT_IMMUTABLE = {"job_id", "started_at", "label", "design_title",
                   "filament_g", "filament_g_manual", "filament_detail",
                   "filament_cost", "ams_bambu", "error_code"}


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
            m = cfg["mariadb"]
            self._connect = lambda: pymysql.connect(
                host=m.get("host", "127.0.0.1"), port=int(m.get("port", 3306)),
                user=m["user"], password=m["password"], database=m["database"],
                autocommit=True, charset="utf8mb4",
            )
            self.ph = "%s"
            self._auto = "AUTO_INCREMENT"
        else:
            import sqlite3
            path = cfg.get("sqlite_path", "telemetry.db")
            # check_same_thread=False: the MQTT thread writes, Flask threads read
            self._conn = sqlite3.connect(path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._connect = lambda: self._conn
            self.ph = "?"
            self._auto = "AUTOINCREMENT"
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

    # ---- key/value settings (survive restarts) ----
    def get_setting(self, key: str, default=None):
        conn, cur = self._cursor()
        cur.execute(f"SELECT svalue FROM settings WHERE skey={self.ph}", (key,))
        row = cur.fetchone()
        if self.backend != "sqlite":
            cur.close(); conn.close()
        return row[0] if row else default

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
