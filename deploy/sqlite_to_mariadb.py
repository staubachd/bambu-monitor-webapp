#!/usr/bin/env python3
"""
Copy data from the local SQLite file into MariaDB.

Needed if the app ever ran with storage.backend = "sqlite" by accident: the rows
recorded during that time sit in telemetry.db and would otherwise be orphaned.

Run with the app STOPPED, from the app directory:

    kill $(cat app.pid) 2>/dev/null
    ./venv/bin/python3 deploy/sqlite_to_mariadb.py
    # then set backend back to "mariadb" and start the app

Safe to run twice: telemetry is copied only for timestamps newer than what
MariaDB already has, and the other tables are keyed REPLACEs.
"""
import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import bootstrap  # noqa: E402

# This one is special: it copies FROM sqlite TO a server, so it needs both, and
# instance/db.json only ever names one of them. The destination is the
# configured connection; the source is the sqlite file beside the app.
scfg = bootstrap.load() or {}
m = bootstrap.server_block(scfg)
if scfg.get("backend") not in bootstrap.SERVER_BACKENDS or not m:
    raise SystemExit("[migrate] the app is not configured for MariaDB or MySQL - "
                     "run `python app.py --setup` and choose one first")
sqlite_path = os.path.join(HERE, "telemetry.db")

if not os.path.exists(sqlite_path):
    sys.exit(f"no sqlite file at {sqlite_path} - nothing to migrate")

import pymysql  # noqa: E402
import storage as storage_mod  # noqa: E402

# Bring the MariaDB schema up to date FIRST - it may predate columns like
# energy_wh/cost/peak_w/label, and copy() only copies columns that exist there.
print("ensuring MariaDB schema is current ...")
storage_mod.Storage({**scfg, "backend": "mariadb"})

src = sqlite3.connect(sqlite_path)
src.row_factory = sqlite3.Row
dst = pymysql.connect(host=m.get("host", "127.0.0.1"), port=int(m.get("port", 3306)),
                      user=m["user"], password=m["password"], database=m["database"],
                      autocommit=True, charset="utf8mb4")
cur = dst.cursor()


def dst_columns(table):
    cur.execute("SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s", (table,))
    return {r[0] for r in cur.fetchall()}


def src_columns(table):
    try:
        return [r[1] for r in src.execute(f"PRAGMA table_info({table})").fetchall()]
    except sqlite3.Error:
        return []


def copy(table, where="", verb="REPLACE"):
    scols = src_columns(table)
    if not scols:
        print(f"  {table:<10} : not present in sqlite, skipped")
        return
    cols = [c for c in scols if c in dst_columns(table) and c != "id"]
    rows = src.execute(f"SELECT {','.join(cols)} FROM {table} {where}").fetchall()
    if not rows:
        print(f"  {table:<10} : nothing new")
        return
    ph = ",".join(["%s"] * len(cols))
    sql = f"{verb} INTO {table} ({','.join(cols)}) VALUES ({ph})"
    cur.executemany(sql, [tuple(r[c] for c in cols) for r in rows])
    print(f"  {table:<10} : copied {len(rows)} rows")


print(f"sqlite : {sqlite_path}")
print(f"mariadb: {m.get('user')}@{m.get('host')}/{m.get('database')}\n")

cur.execute("SELECT COALESCE(MAX(ts), 0) FROM telemetry")
newest = cur.fetchone()[0] or 0
print(f"newest telemetry already in MariaDB: {newest}")
copy("telemetry", where=f"WHERE ts > {float(newest)}", verb="INSERT")
copy("prints")
copy("hms_ack")
copy("settings")
copy("filaments")
copy("purchases")
copy("notes")
copy("note_images")

cur.execute("SELECT COUNT(*) FROM telemetry"); t = cur.fetchone()[0]
cur.execute("SELECT COUNT(*) FROM prints"); p = cur.fetchone()[0]
print(f"\nMariaDB now holds {t} telemetry rows and {p} prints.")
print("Now set storage.backend back to \"mariadb\" and restart the app.")
src.close(); cur.close(); dst.close()
