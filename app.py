#!/usr/bin/env python3
"""
Bambu X2D Monitor - single-process app: a background MQTT thread keeps the
latest normalized printer state, and Flask serves a live dashboard.

    python app.py            # reads printer.config.json, serves http://localhost:8770

Endpoints:
    /            dashboard page
    /api/state   latest normalized state as JSON
    /events      Server-Sent Events stream (pushes state whenever it changes)
"""
from __future__ import annotations

import json
import logging
import os
import queue
import re
import subprocess
import ssl
import threading
import time

from datetime import datetime, timedelta, timezone

import paho.mqtt.client as mqtt
from flask import Flask, Response, jsonify, request, send_file

import filament_catalog
from bambu_state import parse_report
from storage import Storage

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "printer.config.json")
PORT = int(os.environ.get("BAMBU_PORT", "8770"))

with open(CONFIG_PATH, encoding="utf-8") as fh:
    CFG = json.load(fh)

REPORT_TOPIC = f"device/{CFG['serial']}/report"
REQUEST_TOPIC = f"device/{CFG['serial']}/request"
PUSHALL = json.dumps({"pushing": {"sequence_id": "0", "command": "pushall"}})
GET_VERSION = json.dumps({"info": {"sequence_id": "0", "command": "get_version"}})

STORE_CFG = CFG.get("storage", {"backend": "sqlite"})
# resolve a relative sqlite path against this dir so cwd doesn't matter
if STORE_CFG.get("backend", "sqlite") == "sqlite" and not os.path.isabs(STORE_CFG.get("sqlite_path", "telemetry.db")):
    STORE_CFG = {**STORE_CFG, "sqlite_path": os.path.join(HERE, STORE_CFG.get("sqlite_path", "telemetry.db"))}
SAMPLE_INTERVAL = float(STORE_CFG.get("sample_interval_sec", 20))
store = Storage(STORE_CFG)
_acked = store.acked_keys()  # set of (code, ts) the user has dismissed

# Recording mode gates DB writes only; the live dashboard updates regardless.
#   on   - always record
#   off  - never record
#   auto - record only while a print is active (+ a tail, so cool-down is kept),
#          leaving the NAS disks idle the rest of the time so they can hibernate
RECORD_MODES = ("auto", "on", "off")
ACTIVE_STATES = {"RUNNING", "PREPARE", "PAUSE", "SLICING"}
AUTO_TAIL_SEC = float(STORE_CFG.get("auto_tail_min", 10)) * 60

# Maintenance schedule for the Bambu Lab X2D. Bambu's official intervals are
# calendar-based by usage tier (regular use ~1-5 h/day: X/Y axes every 2 months,
# Z axis every 4 months); converted here to cumulative PRINT hours at ~3 h/day.
# Every interval is editable from the UI, and each task links to its X2D wiki page.
_WIKI = "https://wiki.bambulab.com/en"
MAINT_URL = f"{_WIKI}/x2d/maintenance/periodic-maintenance"
MAINTENANCE_TASKS = [
    {"key": "clean_general",   "hours": 50,  "name": "General clean (dust, debris, build plate)",
     "url": MAINT_URL},
    {"key": "lube_xy",         "hours": 180, "name": "Clean & lubricate the X/Y axes",
     "url": f"{_WIKI}/p2s/maintenance/lubricate-x-y-z-axis"},
    {"key": "lube_idler",      "hours": 270, "name": "Lubricate the X/Y idler pulleys",
     "url": f"{_WIKI}/p2s/maintenance/idler-pulley-lubrication"},
    {"key": "lube_z",          "hours": 360, "name": "Clean & lubricate the Z lead screws",
     "url": f"{_WIKI}/p2s/maintenance/lubricate-x-y-z-axis"},
    {"key": "clean_fans",      "hours": 200, "name": "Clean fans & air filter",
     "url": MAINT_URL},
    {"key": "nozzle_coldpull", "hours": 250, "name": "Hotend cold pull / nozzle clean",
     "url": MAINT_URL},
]
MAINT_KEYS = {t["key"] for t in MAINTENANCE_TASKS}


def _load_mode() -> str:
    raw = store.get_setting("recording", "auto")
    if raw == "1":      # migrate from the older boolean setting
        return "on"
    if raw == "0":
        return "off"
    return raw if raw in RECORD_MODES else "auto"


_rec_mode = _load_mode()
_auto = {"last_active": 0.0}  # when we last saw an active print
# 'off' also tears down the MQTT connection so the app is completely idle.
# 'auto' must stay connected - it can't notice a print starting otherwise.
_mqtt_enabled = threading.Event()
if _rec_mode != "off":
    _mqtt_enabled.set()
# Live handle to the connected MQTT client, so request-topic commands (e.g. the
# chamber LED) can be published from Flask endpoints. None while disconnected.
_mqtt_client = None


def _should_record(state: dict) -> bool:
    """Whether this sample should be written, per the current mode."""
    if _rec_mode == "on":
        return True
    if _rec_mode == "off":
        return False
    st = (state.get("job") or {}).get("state")
    now = time.time()
    if st in ACTIVE_STATES:
        _auto["last_active"] = now
        return True
    last = _auto["last_active"]
    return bool(last) and (now - last) <= AUTO_TAIL_SEC


def _annotate_acks(state: dict) -> dict:
    for h in state.get("hms", []) or []:
        h["acked"] = (h["code"], h.get("ts") or "") in _acked
    return state

# ---- shared state ----------------------------------------------------------
_state_lock = threading.Lock()
_state: dict = {"connected": False, "recording_mode": _rec_mode,
                "recording_active": False, "stream_enabled": _rec_mode != "off",
                "job": {"state_label": "Connecting..."}}
_subscribers: list[queue.Queue] = []
_subs_lock = threading.Lock()
_last_record = {"ts": 0.0, "state": None}
_last_raw: dict = {"data": None}  # last full raw report, served by /api/raw
# Firmware version from the get_version reply. The report's `ver` field is an
# internal number (e.g. 20000), NOT the user-facing firmware - that is the
# 'ota' module's sw_ver (e.g. 01.02.00.00), fetched separately on connect.
_versions: dict = {"firmware": None}


def _covered_raw_keys() -> list:
    """The set of raw-payload keys the parser actually reads, derived by scanning
    bambu_state.py for its `.get("key")` / `["key"]` accesses. Deriving it from
    the source (rather than a hand-kept list) means the dashboard's "used by the
    app" highlighting can't drift out of sync when the parser gains a field.
    The frontend intersects this with the keys actually present in the payload,
    so a little over-inclusion here is harmless."""
    try:
        with open(os.path.join(HERE, "bambu_state.py"), encoding="utf-8") as fh:
            src = fh.read()
    except Exception:
        return []
    # Every raw read in the parser goes through .get("key"); string-literal
    # subscripts appear only in its output-side self-test, so matching .get()
    # (plus the _tray_no helper's literals) yields exactly the consumed keys.
    keys = set(re.findall(r'\.get\(\s*["\']([A-Za-z_][\w]*)["\']', src))
    keys |= set(re.findall(r'_tray_no\(\s*["\']([A-Za-z_][\w]*)["\']', src))
    return sorted(keys)


COVERED_RAW_KEYS = _covered_raw_keys()
# latest reading from the smart plug (Tapo P110/P110M), polled independently
_power: dict = {"watts": None, "today_wh": None, "month_wh": None,
                "ts": 0.0, "error": None}
PWR_CFG = CFG.get("power", {}) or {}
COST_CFG = CFG.get("cost", {}) or {}
CAM_CFG = CFG.get("camera", {}) or {}
# energy consumed by the CURRENT print, integrated from the plug while a job is
# active. Deliberately not derived from the chart range, which the user changes.
_job_energy = {"task_id": None, "wh": 0.0, "last_ts": None}


def _accumulate_job_energy(watts) -> None:
    """Integrate watts over wall-clock time while a print is running.

    Pure integration only. `_track_print` is the SINGLE owner of resetting this
    on a job change - it runs on every MQTT message, whereas this runs on the
    plug's 20s poll. When both owned the reset, a new print inherited the
    previous print's total for up to 20 seconds, and the monotonic guard in
    `_persist_print` then made that wrong value permanent.
    """
    if watts is None:
        return
    with _state_lock:
        job = dict((_state.get("job") or {}))
    tid = job.get("task_id")
    if not tid or tid != _job_energy["task_id"]:
        return  # no job yet, or _track_print hasn't switched over yet
    now = time.time()
    if job.get("state") in ACTIVE_STATES:
        last = _job_energy["last_ts"]
        if last and 0 < (now - last) < 300:    # ignore long gaps (app restart etc.)
            _job_energy["wh"] += watts * (now - last) / 3600.0
        _job_energy["last_ts"] = now
    else:
        _job_energy["last_ts"] = None          # pause the integration when idle


_print_row = {"job_id": None, "started_at": None, "peak_w": 0.0, "seen_active": False,
              "design_id": None, "profile_id": None}
_last_print_write = {"ts": 0.0, "state": None}
# Jobs the user deleted from the history. The printer keeps reporting the last
# job's task_id for as long as it sits idle, so without a tombstone the next
# persist tick would write the row straight back. Only the job currently being
# tracked can be resurrected, so the set is cleared when a new job starts.
_deleted_jobs: set[str] = set()


def _track_print(state: dict) -> None:
    """Keep the in-memory summary of the print currently being watched."""
    job = state.get("job") or {}
    tid = job.get("task_id")
    if not tid:
        return
    if tid != _print_row["job_id"]:
        # A different job started: if the one we were watching was never closed,
        # close it now rather than leaving it "running" forever.
        prev_id = _print_row["job_id"]
        if prev_id and not (_print_row.get("stored") or {}).get("ended_at"):
            try:
                store.update_print_fields(prev_id, ended_at=time.time())
                print(f"[prints] closed previous job {prev_id}")
            except Exception as e:
                print(f"[prints] could not close {prev_id}: {e}")
        # We may be picking a print back up after a restart - the stored row is
        # the authoritative running total, so load it before touching anything.
        try:
            prev = store.get_print(tid)
        except Exception:
            prev = None
        _print_row.update(
            job_id=tid, stored=prev or {},
            started_at=(prev or {}).get("started_at") or time.time(),
            peak_w=float((prev or {}).get("peak_w") or 0.0),
            seen_active=bool(prev),   # already known => don't re-stamp the start
            # seed from the stored row so a resumed print keeps its model link
            # even before a fresh (possibly partial) report re-supplies it
            design_id=(prev or {}).get("design_id"),
            profile_id=(prev or {}).get("profile_id"),
        )
        _deleted_jobs.clear()   # a new job: nothing old can be written back now
        # Single owner of the per-job energy reset, applied in the same tick the
        # job changes. A print we already know resumes from its stored total; a
        # genuinely new print starts at exactly zero.
        _job_energy.update(task_id=tid, last_ts=None,
                           wh=float((prev or {}).get("energy_wh") or 0.0))
        print(f"[cost] job {tid} -> starting from {_job_energy['wh']:.1f} Wh")
    if job.get("state") in ACTIVE_STATES:
        if not _print_row["seen_active"]:
            # first moment we actually see it printing - that's the real start
            _print_row["started_at"] = time.time()
        _print_row["seen_active"] = True
    # Latch the MakerWorld reference: Bambu's incremental reports frequently omit
    # design_id/profile_id, so only overwrite when this frame actually carries them.
    if job.get("design_id"):
        _print_row["design_id"] = job["design_id"]
    if job.get("profile_id"):
        _print_row["profile_id"] = job["profile_id"]
    w = (state.get("power") or {}).get("watts")
    if w:
        _print_row["peak_w"] = max(_print_row["peak_w"], float(w))


def _maybe_persist_print(state: dict) -> None:
    """Write history on any state change, else once a minute.

    Deliberately NOT tied to the telemetry recording gate: a print is one small
    row, and it must still be captured when Auto mode is otherwise idle.
    """
    st = (state.get("job") or {}).get("state")
    now = time.time()
    if st != _last_print_write["state"] or (now - _last_print_write["ts"]) >= 60:
        changed = st != _last_print_write["state"]
        _persist_print(state)
        _last_print_write.update(ts=now, state=st)
        # a print just ended -> fetch its filament data now instead of waiting
        # out the poll interval
        if changed and st in ("FINISH", "FAILED") and CLOUD_CFG.get("enabled"):
            _cloud_kick.set()


def _persist_print(state: dict) -> None:
    """Write the current print to the history table. Called on every stored
    sample, so an in-progress print is already in the table if we crash."""
    job = state.get("job") or {}
    tid = job.get("task_id")
    if not tid or _print_row["job_id"] != tid:
        return
    if tid in _deleted_jobs:
        return  # deleted from the history by hand - don't write it back
    if not _print_row["seen_active"]:
        return  # never saw it printing (e.g. we connected after it finished)
    active = job.get("state") in ACTIVE_STATES

    # Energy and peak are cumulative for a job: they must never go backwards.
    # After a restart the in-memory counters start at 0 and would otherwise
    # overwrite the stored totals before the plug poller has re-bootstrapped.
    stored = _print_row.get("stored") or {}
    # Stamp the end time ONCE. The printer keeps reporting the finished job's
    # task_id while it sits idle, so recomputing this every minute would make
    # the recorded duration creep forward forever.
    ended = None if active else (stored.get("ended_at") or time.time())
    energy = max(round(_job_energy["wh"], 2), float(stored.get("energy_wh") or 0.0))
    peak = max(round(_print_row["peak_w"], 1), float(stored.get("peak_w") or 0.0))
    if energy > _job_energy["wh"]:
        _job_energy["wh"] = energy      # keep the live "this print" figure in step
    _print_row["peak_w"] = peak
    price = float(COST_CFG.get("price_per_kwh", 0) or 0)
    cost = round(energy / 1000.0 * price, 4) if price else None

    try:
        store.upsert_print(
            job_id=tid,
            name=job.get("name"),
            started_at=_print_row["started_at"],
            ended_at=ended,
            final_state=job.get("state"),
            total_layers=job.get("total_layers"),
            energy_wh=energy,
            cost=cost,
            peak_w=peak,
            design_id=_print_row.get("design_id"),
            profile_id=_print_row.get("profile_id"),
        )
        _print_row["stored"] = {**stored, "energy_wh": energy, "peak_w": peak,
                                "started_at": _print_row["started_at"],
                                "ended_at": ended}
        # Capture why it failed, while the printer is still reporting it
        errs = state.get("errors") or {}
        code = errs.get("print_error") or errs.get("mc_code") or errs.get("fail_reason")
        if code and code != (stored.get("error_code")):
            store.update_print_fields(tid, error_code=str(code)[:64])
            _print_row["stored"]["error_code"] = str(code)[:64]

        # Record which slots held genuine Bambu spools *while this print ran*.
        # Spools get swapped, so reading it later would price the job wrongly.
        if not _print_row["stored"].get("ams_bambu"):
            snap = _ams_bambu_map(state)
            if snap:
                blob = json.dumps(snap)
                store.update_print_fields(tid, ams_bambu=blob)
                _print_row["stored"]["ams_bambu"] = blob
    except Exception as e:
        print(f"[prints] upsert failed: {e}")


def _grams(row):
    m = row.get("filament_g_manual")
    return m if m is not None else row.get("filament_g")


def _cost_block() -> dict:
    """Power + material, aggregated per calendar window from the prints table.

    Both are per-print so they pair up: 'what printing cost me today/week/month'.
    A print counts toward every window it was ACTIVE in (running at any point in
    it), so a job that spans midnight - or is still running - shows up under
    'today' instead of leaving it blank. Its full total is attributed to each
    such window; energy_wh is a single cumulative figure we can't split by day.
    """
    price = float(COST_CFG.get("price_per_kwh", 0) or 0)
    cur = COST_CFG.get("currency", "€")

    now = datetime.now()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    t_today = midnight.timestamp()
    t_week = (midnight - timedelta(days=now.weekday())).timestamp()
    t_month = midnight.replace(day=1).timestamp()

    try:
        prints = store.recent_prints(limit=2000)
    except Exception:
        prints = []

    def window(since):
        pw = pc = mg = mc = 0.0
        for r in prints:
            # active-in-window: skip only prints that already finished before the
            # window began. A running print (no ended_at) is active now, so it
            # lands in every current window.
            ended = r.get("ended_at")
            if ended is not None and ended < since:
                continue
            pw += r.get("energy_wh") or 0
            pc += r.get("cost") or 0
            mg += _grams(r) or 0
            mc += r.get("filament_cost") or 0
        return {"power_wh": round(pw, 1), "power_cost": round(pc, 4),
                "material_g": round(mg, 1), "material_cost": round(mc, 4)}

    last = None
    if prints:
        r = prints[0]   # recent_prints is ordered newest-first
        pc, mc = r.get("cost") or 0, r.get("filament_cost") or 0
        last = {
            "name": r.get("label") or r.get("design_title") or r.get("name"),
            "power_cost": r.get("cost"), "material_cost": r.get("filament_cost"),
            "total": round(pc + mc, 4), "grams": _grams(r), "wh": r.get("energy_wh"),
            "running": not r.get("ended_at"),
        }

    return {
        "currency": cur, "price_per_kwh": price,
        "windows": {"today": window(t_today), "week": window(t_week),
                    "month": window(t_month)},
        "last": last,
    }


def _stats_block() -> dict:
    """Lifetime analytics over the whole prints table: totals, success rate,
    per-month trend and the most-printed models."""
    cur = COST_CFG.get("currency", "€")
    try:
        prints = store.all_prints()
    except Exception:
        prints = []

    fin = fail = 0
    dur = eng = pcost = fil = mcost = 0.0
    durations = []
    months, models = {}, {}
    for r in prints:
        st = r.get("final_state")
        s, e = r.get("started_at"), r.get("ended_at")
        g = _grams(r) or 0
        pc = r.get("cost") or 0
        mc = r.get("filament_cost") or 0
        eng += r.get("energy_wh") or 0
        pcost += pc; fil += g; mcost += mc
        if st == "FINISH":
            fin += 1
        elif st == "FAILED":
            fail += 1
        if s and e and e > s:
            d = e - s
            dur += d
            durations.append(d)
        if s:
            mk = datetime.fromtimestamp(s).strftime("%Y-%m")
            mm = months.setdefault(mk, {"month": mk, "prints": 0, "energy_wh": 0.0,
                                        "cost": 0.0, "filament_g": 0.0})
            mm["prints"] += 1
            mm["energy_wh"] += r.get("energy_wh") or 0
            mm["cost"] += pc + mc
            mm["filament_g"] += g
        name = r.get("design_title") or r.get("label") or r.get("name") or "—"
        mkey = r.get("design_id") or name
        md = models.setdefault(mkey, {"name": name, "design_id": r.get("design_id"),
                                      "count": 0, "filament_g": 0.0, "cost": 0.0})
        md["count"] += 1
        md["filament_g"] += g
        md["cost"] += pc + mc

    for mm in months.values():
        mm["energy_wh"] = round(mm["energy_wh"], 1)
        mm["cost"] = round(mm["cost"], 2)
        mm["filament_g"] = round(mm["filament_g"], 1)
    for md in models.values():
        md["filament_g"] = round(md["filament_g"], 1)
        md["cost"] = round(md["cost"], 2)

    completed = fin + fail
    return {
        "currency": cur,
        "totals": {
            "prints": len(prints), "finished": fin, "failed": fail,
            "success_rate": round(fin / completed, 4) if completed else None,
            "print_seconds": round(dur),
            "avg_seconds": round(dur / len(durations)) if durations else 0,
            "longest_seconds": round(max(durations)) if durations else 0,
            "energy_wh": round(eng, 1), "power_cost": round(pcost, 4),
            "filament_g": round(fil, 1), "material_cost": round(mcost, 4),
            "total_cost": round(pcost + mcost, 4),
        },
        "by_month": [months[k] for k in sorted(months)][-12:],
        "top_models": sorted(models.values(), key=lambda x: -x["count"])[:8],
    }


def _recorded_print_hours() -> float:
    """Cumulative printing time from completed prints, in hours."""
    total = 0.0
    try:
        for r in store.all_prints():
            s, e = r.get("started_at"), r.get("ended_at")
            if s and e and e > s:
                total += (e - s)
    except Exception:
        pass
    return total / 3600.0


def _setting_float(key: str, default: float) -> float:
    try:
        return float(store.get_setting(key, default) or default)
    except (TypeError, ValueError):
        return float(default)


def _maintenance_block() -> dict:
    """Per-task maintenance status driven by cumulative print hours. Each task's
    'since' is measured from its last reset; overdue at >=interval, soon at >=80%."""
    recorded = _recorded_print_hours()
    offset = _setting_float("maint_offset_hours", 0.0)
    tasks = []
    for tk in MAINTENANCE_TASKS:
        key = tk["key"]
        interval = _setting_float(f"maint_interval_{key}", tk["hours"])
        reset = _setting_float(f"maint_reset_{key}", 0.0)
        since = max(0.0, recorded - reset)
        if since >= interval:
            status = "overdue"
        elif interval and since >= interval * 0.8:
            status = "soon"
        else:
            status = "ok"
        tasks.append({
            "key": key, "name": tk["name"], "url": tk.get("url", MAINT_URL),
            "interval_hours": round(interval, 1),
            "since_hours": round(since, 1), "due_in_hours": round(interval - since, 1),
            "status": status, "last_reset_hours": round(reset, 1),
        })
    return {"recorded_hours": round(recorded, 1), "offset_hours": round(offset, 1),
            "total_hours": round(recorded + offset, 1), "url": MAINT_URL, "tasks": tasks}


def _publish_state(new_state: dict) -> None:
    """Store latest state and fan it out to all connected SSE clients."""
    with _state_lock:
        _state.clear()
        _state.update(new_state)
    payload = json.dumps(new_state)
    with _subs_lock:
        for q in list(_subscribers):
            try:
                q.put_nowait(payload)
            except queue.Full:
                pass


# ---- MQTT collector thread -------------------------------------------------
def on_connect(client, userdata, flags, rc, *_):
    if rc == 0:
        client.subscribe(REPORT_TOPIC)
        client.publish(REQUEST_TOPIC, PUSHALL)
        client.publish(REQUEST_TOPIC, GET_VERSION)  # for the real firmware version
        print(f"[mqtt] connected, subscribed to {REPORT_TOPIC}")
    else:
        print(f"[mqtt] connect failed rc={rc}")


def on_message(client, userdata, msg):
    try:
        raw = json.loads(msg.payload)
    except ValueError:
        return
    info = raw.get("info")
    if info and "module" in info:   # get_version reply - grab the real firmware
        mods = {m.get("name"): m.get("sw_ver") for m in info.get("module") or []}
        _versions["firmware"] = mods.get("ota") or _versions["firmware"]
        return
    if "print" not in raw:
        return  # ignore non-status frames
    if len(raw.get("print", {})) > 40:   # keep the last *full* report for /api/raw
        _last_raw["data"] = raw
    state = parse_report(raw)
    _enrich_ams(state)
    _observe_filaments(state)
    if _versions["firmware"]:   # override the misleading `ver` with the ota version
        state["printer"]["firmware"] = _versions["firmware"]
    state["connected"] = True
    state["updated_at"] = time.time()
    allowed = _should_record(state)
    state["recording_mode"] = _rec_mode
    state["recording_active"] = allowed
    # derive, don't hardcode: a message can still arrive during teardown after
    # the stream was switched off, and must not resurrect a stale "on" flag
    state["stream_enabled"] = _mqtt_enabled.is_set()
    state["power"] = dict(_power)
    state["cost"] = _cost_block()
    _track_print(state)
    _maybe_persist_print(state)
    # surface the saved label / cloud title on the live tile so it matches history
    _sn = _print_row.get("stored") or {}
    if state.get("job") and state["job"].get("task_id") == _print_row.get("job_id"):
        state["job"]["label"] = _sn.get("label")
        state["job"]["design_title"] = _sn.get("design_title")
    _annotate_acks(state)
    _publish_state(state)
    _maybe_record(state, allowed)


def _maybe_record(state: dict, allowed: bool) -> None:
    """Downsample: store on a fixed interval, but immediately on a state change
    (e.g. print starts/pauses/finishes) so transitions are never missed."""
    if not allowed:
        return
    now = time.time()
    cur_state = (state.get("job") or {}).get("state")
    changed = cur_state != _last_record["state"]
    if changed or (now - _last_record["ts"]) >= SAMPLE_INTERVAL:
        try:
            store.record(state)
            _last_record["ts"] = now
            _last_record["state"] = cur_state
        except Exception as e:
            print(f"[store] record failed: {e}")


def on_disconnect(client, userdata, *args):
    print("[mqtt] disconnected")
    with _state_lock:
        _state["connected"] = False


def power_worker():
    """Poll a TP-Link Tapo P110/P110M smart plug for live wattage.

    The printer itself reports no power data at all, so this comes from the plug
    it is connected to. The tapo library is async, so we own a small event loop
    in this thread. Failures are recorded and retried - never fatal.
    """
    import asyncio
    try:
        from tapo import ApiClient
    except ImportError:
        print("[power] 'tapo' not installed; power monitoring disabled")
        return

    poll = float(PWR_CFG.get("poll_sec", 20))
    model = PWR_CFG.get("model", "p110")

    async def loop():
        client = ApiClient(PWR_CFG["email"], PWR_CFG["password"])
        dev = None
        # Log only on state change, never on every poll: a flaky plug must not
        # write a line every 20s, which would wake the NAS disks and defeat the
        # HDD hibernation the whole app is built around.
        last_err = None
        started = False
        while True:
            if not _mqtt_enabled.is_set():   # 'off' means fully idle
                await asyncio.sleep(5)
                continue
            try:
                if dev is None:
                    dev = await getattr(client, model)(PWR_CFG["host"])
                cp = await dev.get_current_power()
                eu = await dev.get_energy_usage()
                _power.update(watts=cp.current_power, today_wh=eu.today_energy,
                              month_wh=eu.month_energy, ts=time.time(), error=None)
                _accumulate_job_energy(cp.current_power)
                if not started:
                    print(f"[power] connected to {model} at {PWR_CFG['host']}")
                    started = True
                elif last_err is not None:
                    print(f"[power] {model} reachable again")
                last_err = None
            except Exception as e:
                msg = str(e)[:140]
                _power.update(error=msg)
                dev = None  # force a fresh handshake next time
                if msg != last_err:   # log the fault once, not on every retry
                    print(f"[power] error: {e}")
                    last_err = msg
            await asyncio.sleep(poll)

    asyncio.run(loop())


FIL_CFG = CFG.get("filament", {}) or {}
CLOUD_CFG = CFG.get("cloud", {}) or {}

# Reordering: what counts as "nearly used up", and which regional store to link.
FIL_LOW_PCT = float(FIL_CFG.get("low_pct", 15))
FIL_STORE_REGION = FIL_CFG.get("store_region", filament_catalog.DEFAULT_REGION)
FIL_STORE_HOST = FIL_CFG.get("store_host")          # full host override, optional
FIL_COLOR_NAMES = FIL_CFG.get("color_names") or {}  # extends/corrects the built-ins
# Colour names read off imported invoices - Bambu's own wording, so they beat the
# built-in guess table. Config still wins, as the last word is always the user's.
try:    # keys are canonicalised on load, so entries written before norm_code
    _LEARNED_COLORS = {filament_catalog.norm_code(k): v
                       for k, v in store.settings_with_prefix("cname_").items()}
except Exception:
    _LEARNED_COLORS = {}


def _color_overrides() -> dict:
    return {**_LEARNED_COLORS, **FIL_COLOR_NAMES}


# Per-kg prices taken from your own orders, so the config matrix stops being a
# number you have to maintain by hand. Keyed by Bambu SKU (GFA00).
PRICES_FROM_ORDERS = bool(FIL_CFG.get("prices_from_orders", True))
_ORDER_PRICES: dict = {}


def _rebuild_order_prices() -> None:
    """{SKU: € per kg} from the purchase log; the newest order for a SKU wins.

    Deliberately the LIST price, not what was paid. A one-off discount is not
    what replacing that spool will cost, and a quote built on it would come out
    under the real figure - the same reasoning that makes every total round up.
    """
    prices = {}
    try:
        rows = store.all_purchases()          # newest first
    except Exception as e:
        print(f"[filament] could not read purchases for pricing: {e}")
        return
    for p in reversed(rows):                  # oldest first, newest overwrites
        sku = filament_catalog.sku_from_code(p.get("code"))
        unit, grams = p.get("list_price"), p.get("grams_each")
        if sku and unit and grams:
            prices[sku] = round(float(unit) / (float(grams) / 1000.0), 4)
    _ORDER_PRICES.clear()
    _ORDER_PRICES.update(prices)


_rebuild_order_prices()


def _enrich_ams(state: dict) -> None:
    """Annotate every tray in place with colour name, reorder link and a
    low-stock flag. Purely presentational, so it lives here and not in the
    parser, which stays a pure function of the printer's report."""
    ams = state.get("ams") or {}
    for trays in [u.get("trays") or [] for u in (ams.get("units") or [])] + [ams.get("external") or []]:
        for tr in trays:
            tr.update(filament_catalog.describe(
                tr, overrides=_color_overrides(),
                region=FIL_STORE_REGION, host=FIL_STORE_HOST))
            pct = tr.get("remain_pct")
            # Only an RFID spool reports a real remaining %: a third-party tray
            # sends -1 and an external spool sends 0, and neither means empty -
            # warning on those would cry wolf on every non-Bambu spool.
            tr["low"] = (bool(tr.get("is_bambu")) and pct is not None
                         and 0 <= pct <= FIL_LOW_PCT)
    if ams:
        ams["low_pct"] = FIL_LOW_PCT


_fil_obs = {}   # fkey -> (identity tuple, ts) of what was last written


def _observe_filaments(state: dict) -> None:
    """Remember every filament the AMS shows, so the Filament page can still name
    a spool years after it was used up and thrown away.

    Runs on the MQTT frame rate, so it writes only when an identity actually
    changes (or hourly, to keep last_seen meaningful) - the trays change a few
    times a week, not once a second.
    """
    now = time.time()
    ams = state.get("ams") or {}
    for trays in [u.get("trays") or [] for u in (ams.get("units") or [])] + [ams.get("external") or []]:
        for tr in trays:
            if not tr.get("type"):
                continue          # empty slot
            fkey = filament_catalog.key(tr.get("filament_id"), tr.get("color"), tr.get("type"))
            ident = (tr.get("filament_id"), tr.get("code"), tr.get("brand"),
                     filament_catalog.norm_color(tr.get("color")), tr.get("color_name"),
                     tr.get("type"), bool(tr.get("is_bambu")))
            prev = _fil_obs.get(fkey)
            if prev and prev[0] == ident and (now - prev[1]) < 3600:
                continue
            try:
                store.upsert_filament(
                    fkey, filament_id=ident[0], code=ident[1], product=ident[2],
                    color=ident[3], color_name=ident[4], type=ident[5],
                    is_bambu=int(ident[6]))
                _fil_obs[fkey] = (ident, now)
            except Exception as e:
                print(f"[filament] upsert failed: {e}")


def _detail_entries(row: dict) -> list[dict]:
    """Per-slot filament entries of one print, with a manual grams override
    applied proportionally - so the Filament page adds up to the same total the
    history row shows rather than to the cloud's original estimate."""
    try:
        entries = json.loads(row.get("filament_detail") or "[]") or []
    except (TypeError, ValueError):
        return []
    base = sum(float(e.get("grams") or 0) for e in entries)
    manual = row.get("filament_g_manual")
    scale = (float(manual) / base) if (manual and base) else 1.0
    out = []
    for e in entries:
        g = float(e.get("grams") or 0) * scale
        out.append({**e, "grams": g, "cost": float(e.get("cost") or 0) * scale})
    return out


def _match_purchase(p: dict, agg: dict, known: dict,
                    fkey_by_code: dict, fkey_by_match: dict) -> tuple:
    """Link one purchase to a filament identity. Returns (fkey|None, how).

    Four routes, most certain first. The SKU route is the one that reaches
    filaments used up before the AMS was ever observed: their print rows carry a
    SKU and a colour but no colour code, and an invoice carries a colour code but
    neither SKU nor hex - `sku_from_code` is the bridge between the two.
    """
    if p.get("fkey") and p["fkey"] in agg:
        return p["fkey"], "fkey"
    # canonical, so the store's 'A00-W1' finds the AMS's 'A00-W01'
    code = filament_catalog.norm_code(p.get("code")) or ""
    hit = fkey_by_code.get(code)
    if hit and hit in agg:
        return hit, "code"
    hit = fkey_by_match.get(filament_catalog.match_key(p.get("product"),
                                                       p.get("color_name")))
    if hit and hit in agg:
        return hit, "name"
    sku = filament_catalog.sku_from_code(code)
    if sku:
        cands = [k for k in agg if k.startswith(sku + "|")]
        if len(cands) == 1:
            return cands[0], "sku"
        # several colours of the same product: let the colour name decide, and
        # rather than pick one at random, stay unmatched when it can't
        want = (p.get("color_name") or "").strip().lower()
        if want:
            named = [k for k in cands
                     if (known.get(k, {}).get("color_name") or "").strip().lower() == want]
            if len(named) == 1:
                return named[0], "sku+colour"
        if cands:
            return None, "ambiguous"
    return None, None


def _filament_stats() -> dict:
    """Consumption per filament across the whole print history.

    Aggregated live from prints.filament_detail rather than kept in a counter
    table: the detail is already stored for every past print, so the page is
    complete from day one and can never drift out of step with the history.
    """
    cur = COST_CFG.get("currency", "€")
    try:
        prints = store.all_prints()
    except Exception:
        prints = []
    try:
        known = {f["fkey"]: f for f in store.all_filaments()}
    except Exception:
        known = {}

    agg = {}
    for r in prints:
        started = r.get("started_at")
        for e in _detail_entries(r):
            fkey = filament_catalog.key(e.get("filament_id"), e.get("color"),
                                        e.get("type"))
            a = agg.setdefault(fkey, {
                "fkey": fkey, "filament_id": e.get("filament_id"),
                "color": filament_catalog.norm_color(e.get("color")),
                "type": e.get("type"), "grams": 0.0, "cost": 0.0, "prints": 0,
                "first_used": None, "last_used": None,
            })
            a["grams"] += e["grams"]
            a["cost"] += e["cost"]
            a["prints"] += 1
            # the detail's brand came from the RFID snapshot taken while that
            # print ran, so it still knows genuine-vs-third-party for filaments
            # the AMS has never shown us (used up before this page existed)
            if e.get("brand") in ("Bambu", "third-party"):
                a["brand"] = e["brand"]
            if started:
                a["first_used"] = min(a["first_used"] or started, started)
                a["last_used"] = max(a["last_used"] or started, started)

    # spools seen in the AMS but never printed with yet still belong on the page
    for fkey, f in known.items():
        agg.setdefault(fkey, {
            "fkey": fkey, "filament_id": f.get("filament_id"),
            "color": f.get("color"), "type": f.get("type"),
            "grams": 0.0, "cost": 0.0, "prints": 0,
            "first_used": None, "last_used": None,
        })

    # purchases, matched to a filament by fkey when known and otherwise by
    # 'product line + colour name' - a receipt carries words, never a SKU or a hex
    try:
        purchases = store.all_purchases()
    except Exception:
        purchases = []
    buys, unmatched = {}, []
    fkey_by_match, fkey_by_code = {}, {}
    for fkey, f in known.items():
        mk = filament_catalog.match_key(f.get("product"), f.get("color_name"))
        if mk:
            fkey_by_match.setdefault(mk, fkey)
        nc = filament_catalog.norm_code(f.get("code"))
        if nc:
            fkey_by_code.setdefault(nc, fkey)
    for p in purchases:
        fkey, how = _match_purchase(p, agg, known, fkey_by_code, fkey_by_match)
        p["matched_by"] = how
        if not fkey:
            unmatched.append(p)
            continue
        p["_fkey"] = fkey       # resolved to a display name once `out` is built
        b = buys.setdefault(fkey, {"grams": 0.0, "cost": 0.0, "spools": 0,
                                   "orders": 0, "last": None})
        b["grams"] += float(p.get("spools") or 1) * float(p.get("grams_each") or 1000)
        b["cost"] += float(p.get("total_price") or 0)
        b["spools"] += int(p.get("spools") or 1)
        b["orders"] += 1
        when = p.get("ordered_at") or p.get("created_at")
        if when:
            b["last"] = max(b["last"] or when, when)

    # what is in the AMS right now -> remaining %, slot and the reorder link
    with _state_lock:
        ams = json.loads(json.dumps((_state.get("ams") or {})))   # cheap deep copy
    loaded = {}
    groups = [(u.get("trays") or [], False) for u in (ams.get("units") or [])]
    groups.append((ams.get("external") or [], True))
    for trays, ext in groups:
        for tr in trays:
            if tr.get("type"):
                loaded[filament_catalog.key(tr.get("filament_id"), tr.get("color"),
                                            tr.get("type"))] = (tr, ext)

    total_g = sum(a["grams"] for a in agg.values()) or 0.0
    out = []
    for fkey, a in agg.items():
        meta = known.get(fkey, {})
        tr, ext = loaded.get(fkey, (None, False))
        # RFID truth, whichever source has it: a live tag beats a past snapshot
        is_bambu = (bool(meta["is_bambu"]) if meta.get("is_bambu") is not None
                    else (a["brand"] == "Bambu" if a.get("brand") else None))
        b = buys.get(fkey)
        out.append({
            # what was bought vs what has been used. `left` can go negative when
            # older orders were never logged - shown as-is rather than clamped,
            # because a negative is the signal that the log is incomplete.
            "bought_g": round(b["grams"], 1) if b else None,
            "bought_cost": round(b["cost"], 4) if b else None,
            "spools": b["spools"] if b else None,
            "orders": b["orders"] if b else None,
            "last_order": b["last"] if b else None,
            "left_g": round(b["grams"] - a["grams"], 1) if b else None,
            "paid_per_kg": (round(b["cost"] / (b["grams"] / 1000.0), 2)
                            if b and b["grams"] else None),
            # the undiscounted price per kg this filament is costed at
            "list_per_kg": _ORDER_PRICES.get((a.get("filament_id") or "").upper()),
            **a,
            "grams": round(a["grams"], 1),
            "cost": round(a["cost"], 4),
            "share": round(a["grams"] / total_g, 4) if total_g else 0,
            # naming comes from the AMS observation; the cloud detail never
            # carries the product line or the colour code
            "code": meta.get("code"),
            "product": meta.get("product"),
            "color_name": meta.get("color_name"),
            "color": a.get("color") or meta.get("color"),
            "is_bambu": is_bambu,
            "first_seen": meta.get("first_seen"),
            # ids 254/255 are the virtual external slots, not a real AMS bay
            "slot": None if (ext or not tr or tr.get("id") is None) else tr["id"] + 1,
            "external": bool(tr) and ext,
            "loaded": bool(tr),
            "remain_pct": tr.get("remain_pct") if tr else None,
            "grams_left": tr.get("grams_left") if tr else None,
            "low": bool(tr.get("low")) if tr else False,
            "store_url": tr.get("store_url") if tr else None,
        })
    # Filament that was bought but never printed with belongs on the page too -
    # it is stock on the shelf. Keyed by colour code so four colours of the same
    # product stay four rows. Each folds into the real entry as soon as that
    # spool is used or turns up in the AMS, because only *unmatched* purchases
    # land here.
    owned = {}
    for p in unmatched:
        nc = filament_catalog.norm_code(p.get("code"))
        k = ("code:" + nc) if nc else (
            "name:" + (filament_catalog.match_key(p.get("product"), p.get("color_name"))
                       or str(p.get("id"))))
        o = owned.setdefault(k, {
            "fkey": k, "filament_id": filament_catalog.sku_from_code(p.get("code")),
            "code": p.get("code"), "product": p.get("product"),
            "color_name": p.get("color_name"), "color": p.get("color"),
            "type": p.get("type"), "grams": 0.0, "cost": 0.0, "prints": 0,
            "share": 0, "first_used": None, "last_used": None, "is_bambu": None,
            "first_seen": None, "slot": None, "external": False, "loaded": False,
            "remain_pct": None, "grams_left": None, "low": False, "store_url": None,
            "bought_g": 0.0, "bought_cost": 0.0, "spools": 0, "orders": 0,
            "last_order": None, "left_g": 0.0, "paid_per_kg": None,
            "unused": True,   # nothing printed with it yet
        })
        g = float(p.get("spools") or 1) * float(p.get("grams_each") or 1000)
        o["bought_g"] += g
        o["bought_cost"] += float(p.get("total_price") or 0)
        o["spools"] += int(p.get("spools") or 1)
        o["orders"] += 1
        when = p.get("ordered_at") or p.get("created_at")
        if when:
            o["last_order"] = max(o["last_order"] or when, when)
    for o in owned.values():
        o["bought_g"] = round(o["bought_g"], 1)
        o["bought_cost"] = round(o["bought_cost"], 4)
        o["left_g"] = o["bought_g"]
        o["paid_per_kg"] = (round(o["bought_cost"] / (o["bought_g"] / 1000.0), 2)
                            if o["bought_g"] else None)
        o["list_per_kg"] = _ORDER_PRICES.get((o.get("filament_id") or "").upper())
    out.extend(owned.values())

    out.sort(key=lambda x: (-x["grams"], -(x["bought_g"] or 0), x["fkey"]))
    # tell each purchase which filament it landed on, so the page can show the
    # link instead of leaving the user to infer it
    names = {f["fkey"]: (f.get("product") or f.get("type") or "?")
                        + " · " + (f.get("color_name") or "#" + (f.get("color") or "??????"))
             for f in out}
    for p in purchases:
        fk = p.pop("_fkey", None)
        if fk:
            p["matched_name"] = names.get(fk)
    spent = sum(float(p.get("total_price") or 0) for p in purchases)
    bought_g = sum(float(p.get("spools") or 1) * float(p.get("grams_each") or 1000)
                   for p in purchases)
    return {
        "currency": cur,
        "totals": {"filaments": len(out), "grams": round(total_g, 1),
                   "unused": sum(1 for o in out if o.get("unused")),
                   "cost": round(sum(a["cost"] for a in agg.values()), 4),
                   "prints": sum(1 for r in prints if _detail_entries(r)),
                   "bought_g": round(bought_g, 1), "spent": round(spent, 2),
                   "orders": len(purchases), "priced": len(_ORDER_PRICES)},
        # SKU -> list price per kg, i.e. what per-print costing now uses
        "order_prices": dict(_ORDER_PRICES) if PRICES_FROM_ORDERS else {},
        "filaments": out,
        "purchases": purchases,
        # logged but not attributable to any filament yet: usually a spool that
        # has never been in the AMS, or a wording the match didn't recognise
        "unmatched": unmatched,
    }


def _ams_bambu_map(state: dict) -> dict:
    """{slot number (1-based, as shown in the UI): is a genuine Bambu spool}."""
    out = {}
    for unit in ((state.get("ams") or {}).get("units") or []):
        for t in (unit.get("trays") or []):
            if t.get("id") is not None:
                out[str(int(t["id"]) + 1)] = bool(t.get("is_bambu"))
    return out


def _brand_price(is_bambu: bool, ftype: str | None) -> tuple:
    """Price for a brand x material combination, e.g. Bambu PETG.

    Accepts both the table form  {"bambu": {"PLA": 24.99, "PETG": 27.99}}
    and the older scalar form    {"bambu_per_kg": 24.99}.
    """
    key = "bambu" if is_bambu else "other"
    table = FIL_CFG.get(key)
    if isinstance(table, dict):
        ftype = (ftype or "").strip()
        if ftype in table:
            return float(table[ftype]), f"{key} {ftype}"
        base = ftype.split("-")[0]          # PLA-CF / PETG-CF -> PLA / PETG
        if base and base in table:
            return float(table[base]), f"{key} {base}"
        if "default" in table:
            return float(table["default"]), f"{key} default"
        return None, None
    scalar = FIL_CFG.get(f"{key}_per_kg")   # legacy single price per brand
    if scalar is not None:
        return float(scalar), key
    return None, None


def _filament_price_per_kg(entry: dict, bambu_map: dict | None = None) -> tuple:
    """(price per kg, which rule matched) - most specific rule first.

    Bambu vs third-party is decided by the AMS RFID tag recorded while the print
    ran, NOT by the cloud: the cloud only knows the slicer's filament profile, so
    a third-party spool printed with a Bambu profile still reports e.g. GFA00.
    """
    slot = str((entry.get("slotId") if entry.get("slotId") is not None else -1) + 1)
    by_slot = (FIL_CFG.get("per_slot") or {}).get(slot)
    if by_slot is not None:
        return float(by_slot), f"slot {slot}"
    by_id = (FIL_CFG.get("per_filament_id") or {}).get(entry.get("filamentId") or "")
    if by_id is not None:
        return float(by_id), entry.get("filamentId")
    # What you actually paid for that exact SKU, list price, from your own order
    # history - beats the hand-maintained brand x material guess below. Only for
    # spools the RFID tag confirms are genuine: a third-party spool sliced with a
    # Bambu profile reports a Bambu SKU and must not be priced as one.
    if PRICES_FROM_ORDERS and bambu_map and bambu_map.get(slot):
        sku = (entry.get("filamentId") or "").upper()
        learned = _ORDER_PRICES.get(sku)
        if learned:
            return learned, f"order {sku}"
    # brand x material is more specific than material alone, so it comes first
    if bambu_map and slot in bambu_map:
        price, rule = _brand_price(bambu_map[slot], entry.get("filamentType"))
        if price is not None:
            return price, rule
    by_type = (FIL_CFG.get("per_type") or {}).get(entry.get("filamentType") or "")
    if by_type is not None:
        return float(by_type), entry.get("filamentType")
    return float(FIL_CFG.get("default_per_kg", 0) or 0), "default"


def _iso_ts(s):
    """'2026-07-23T11:43:58Z' -> epoch seconds (3.9's fromisoformat rejects Z)."""
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def _apply_cloud_task(task: dict) -> bool:
    """Fold one cloud task into its print row (grams, per-slot detail, cost)."""
    job_id = str(task.get("id") or "")
    if not job_id:
        return False
    row = store.get_print(job_id)
    if not row:
        return False  # a print this instance never watched - leave it alone

    try:
        bambu_map = json.loads(row.get("ams_bambu") or "{}")
    except (TypeError, ValueError):
        bambu_map = {}

    mapping = task.get("amsDetailMapping") or []
    detail, fil_cost = [], 0.0
    for e in mapping:
        grams = float(e.get("weight") or 0)
        per_kg, rule = _filament_price_per_kg(e, bambu_map)
        fil_cost += grams / 1000.0 * per_kg
        slot = (e.get("slotId") or 0) + 1
        detail.append({
            "slot": slot,
            "type": e.get("filamentType"),
            "filament_id": e.get("filamentId"),
            "color": (e.get("targetColor") or "")[:6] or None,
            "brand": ("Bambu" if bambu_map.get(str(slot)) else "third-party")
                     if str(slot) in bambu_map else "unknown",
            "grams": round(grams, 2),
            "per_kg": per_kg,
            "rule": rule,
            "cost": round(grams / 1000.0 * per_kg, 4),
        })

    grams_total = float(task.get("weight") or 0)
    # a manual override wins for the total, scaled against the reported grams
    manual = row.get("filament_g_manual")
    if manual and grams_total:
        fil_cost *= float(manual) / grams_total
    elif manual and not mapping:
        fil_cost = float(manual) / 1000.0 * float(FIL_CFG.get("default_per_kg", 0) or 0)

    # Close out a row we never saw finish - e.g. the app was restarted after the
    # print ended, so it would otherwise stay "running" with a growing duration.
    #
    # Two guards, both learned the hard way:
    #  1. A RUNNING job reports status 4 with a placeholder endTime a few seconds
    #     after startTime. Only status 2 means genuinely complete, so anything
    #     else is ignored - otherwise a live print gets stamped as ended+FAILED.
    #  2. Never touch the job the printer says it is currently working on.
    extra = {}
    cloud_end = _iso_ts(task.get("endTime")) if task.get("status") == 2 else None
    with _state_lock:
        live = dict(_state.get("job") or {})
    live_active = (str(live.get("task_id") or "") == job_id
                   and live.get("state") in ACTIVE_STATES)
    if cloud_end and not row.get("ended_at") and not live_active:
        extra["ended_at"] = cloud_end
        if (row.get("final_state") or "") in ACTIVE_STATES:
            extra["final_state"] = "FINISH"
        print(f"[cloud] closed orphaned print {job_id}")

    store.update_print_fields(
        job_id,
        **extra,
        design_title=task.get("designTitle") or None,
        filament_g=round(grams_total, 2) or None,
        filament_detail=json.dumps(detail) if detail else None,
        filament_cost=round(fil_cost, 4) if fil_cost else None,
    )
    return True


_cloud_client = None
_cloud_kick = threading.Event()   # set when a print ends -> sync without waiting


def _cloud_get_client():
    global _cloud_client
    if _cloud_client is None:
        from bambu_cloud import BambuCloud
        _cloud_client = BambuCloud(token=CLOUD_CFG.get("token"))
    return _cloud_client


def cloud_sync_once() -> int:
    """One pass: fetch recent tasks and enrich the matching print rows."""
    from bambu_cloud import CloudError
    c = _cloud_get_client()
    serial = CFG.get("serial")
    try:
        tasks = c.get_tasks(serial=serial, limit=20)
    except CloudError as e:
        if "401" not in str(e) and "403" not in str(e):
            raise
        res = c.login(CLOUD_CFG.get("email"), CLOUD_CFG.get("password"))
        if not c.token:
            raise CloudError(
                f"re-login needs a verification code or 2FA - run setup_cloud.py "
                f"again (loginType={res.get('loginType')})") from None
        print("[cloud] re-authenticated")
        tasks = c.get_tasks(serial=serial, limit=20)
    return sum(1 for t in tasks if _apply_cloud_task(t))


def cloud_worker():
    """Enrich print history with filament data from the Bambu cloud.

    The printer never reports how much filament a job used - the slicer computed
    it and only the cloud knows. This is history enrichment, not live telemetry,
    so it runs slowly; a finished print kicks it early via _cloud_kick.
    """
    poll = max(60.0, float(CLOUD_CFG.get("poll_min", 10)) * 60)
    while True:
        try:
            n = cloud_sync_once()
            if n:
                print(f"[cloud] enriched {n} print(s)")
        except Exception as e:
            print(f"[cloud] {e}")
        if _cloud_kick.wait(timeout=poll):
            _cloud_kick.clear()
            time.sleep(45)   # give the cloud a moment to register the finished job


def _build_client():
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.username_pw_set("bblp", CFG["access_code"])
    client.tls_set(cert_reqs=ssl.CERT_NONE, tls_version=ssl.PROTOCOL_TLS)
    client.tls_insecure_set(True)
    client.on_connect = on_connect
    client.on_message = on_message
    client.on_disconnect = on_disconnect
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    return client


def mqtt_worker():
    """Supervises the printer connection. Parks entirely while mode is 'off',
    so nothing is connected, polled or processed - the app goes fully idle."""
    global _mqtt_client
    while True:
        _mqtt_enabled.wait()  # blocks (no CPU) while the stream is switched off
        client = _build_client()
        try:
            client.connect(CFG["ip"], 8883, keepalive=60)
            client.loop_start()
            _mqtt_client = client
            print("[mqtt] stream started")
            while _mqtt_enabled.is_set():
                time.sleep(1)
        except Exception as e:  # network blip -> retry
            print(f"[mqtt] error: {e}; retrying in 5s")
            time.sleep(5)
        finally:
            _mqtt_client = None
            try:
                client.loop_stop()
                client.disconnect()
            except Exception:
                pass
            if not _mqtt_enabled.is_set():
                print("[mqtt] stream stopped (recording off)")


# ---- Flask -----------------------------------------------------------------
# Silence per-request access logging: every log line is a write to app.log on the
# NAS volume, which wakes the disks and defeats Synology HDD hibernation.
# Warnings and errors still come through.
logging.getLogger("werkzeug").setLevel(logging.WARNING)

app = Flask(__name__)


@app.route("/")
def index():
    return send_file(os.path.join(HERE, "dashboard.html"))


@app.route("/api/state")
def api_state():
    with _state_lock:
        return jsonify(dict(_state))


@app.route("/api/history")
def api_history():
    hours = float(request.args.get("hours", 6))
    return jsonify(store.history(hours=hours))


@app.route("/api/prints")
def api_prints():
    rows = store.recent_prints(limit=int(request.args.get("limit", 60)))
    for r in rows:
        # total is derived, never stored, so it can't go stale when either half
        # is recalculated
        r["total_cost"] = round((r.get("cost") or 0) + (r.get("filament_cost") or 0), 4)
        if r.get("filament_detail"):
            try:
                r["filament_detail"] = json.loads(r["filament_detail"])
            except (TypeError, ValueError):
                r["filament_detail"] = None
    cost = _cost_block()
    return jsonify({"currency": cost["currency"], "prints": rows})


@app.route("/api/stats")
def api_stats():
    return jsonify(_stats_block())


@app.route("/api/filaments")
def api_filaments():
    return jsonify(_filament_stats())


def _num_or_none(v, cast=float):
    try:
        s = str(v).strip().replace(",", ".")
        return cast(float(s)) if s else None
    except (TypeError, ValueError):
        return None


def _pdf_text(data: bytes) -> str:
    """Text of a PDF. pypdf is an OPTIONAL dependency: it is pure Python, so it
    installs on the NAS without a compiler, but the paste box has to keep working
    on installs that never added it."""
    try:
        from pypdf import PdfReader
    except ImportError:
        raise RuntimeError("pypdf is not installed on the server "
                           "- run: pip install pypdf (or paste the text instead)")
    from io import BytesIO
    return "\n".join((p.extract_text() or "") for p in PdfReader(BytesIO(data)).pages)


def _learn_color(code: str | None, name: str | None) -> None:
    """Remember a colour name an invoice taught us, for this code, for good.

    Stored against the canonical code so the store's SKU spelling ('A00-W1') and
    the AMS's ('A00-W01') resolve to the same entry - otherwise a German store
    name would never reach the spool it belongs to.
    """
    code = filament_catalog.norm_code(code)
    name = (name or "").strip()
    if not code or not name or _LEARNED_COLORS.get(code) == name:
        return
    try:
        store.set_setting("cname_" + code, name)
        _LEARNED_COLORS[code] = name
        # apply it to identities already on record, so the Filament page and the
        # AMS tiles show the real name at once rather than after the next frame
        for f in store.all_filaments():
            if filament_catalog.norm_code(f.get("code")) == code:
                store.set_filament_color(f["fkey"], name)
        print(f"[filament] learned colour {code} = {name}")
    except Exception as e:
        print(f"[filament] could not store colour name: {e}")


@app.route("/api/purchases", methods=["POST"])
def api_purchase_add():
    """Log one order line. Everything is optional except a product or colour to
    call it by - a receipt the parser only half-read is still worth keeping."""
    data = request.get_json(force=True, silent=True) or {}
    rows = data.get("lines") if isinstance(data.get("lines"), list) else [data]
    added = []
    try:
        added = _store_purchases(rows)
    except Exception as e:      # surface the real reason - a bare 500 leaves the
        print(f"[purchases] save failed: {e}")   # browser guessing
        return jsonify({"ok": False, "error": str(e)[:300]}), 500
    if not added:
        return jsonify({"ok": False, "error": "nothing to save"}), 400
    _rebuild_order_prices()     # new list prices may change what a print costs
    return jsonify({"ok": True, "added": added, "prices": len(_ORDER_PRICES)})


def _store_purchases(rows: list) -> list:
    added = []
    for r in rows:
        product = (r.get("product") or "").strip()[:64]
        color_name = (r.get("color_name") or "").strip()[:64]
        if not product and not color_name:
            continue
        spools = _num_or_none(r.get("spools"), int) or 1
        code = (r.get("code") or "").strip().upper()[:24] or None
        _learn_color(code, color_name)
        added.append(store.add_purchase(
            fkey=(r.get("fkey") or "").strip()[:64] or None, code=code,
            product=product or None, color_name=color_name or None,
            color=filament_catalog.norm_color(r.get("color")),
            type=(r.get("type") or "").strip()[:24] or None,
            spools=max(1, spools),
            grams_each=_num_or_none(r.get("grams_each")) or 1000.0,
            total_price=_num_or_none(r.get("total_price")),
            list_price=_num_or_none(r.get("list_price")),
            currency=(r.get("currency") or COST_CFG.get("currency", "€"))[:8],
            ordered_at=_num_or_none(r.get("ordered_at")),
            order_ref=(r.get("order_ref") or "").strip()[:64] or None,
            note=(r.get("note") or "").strip()[:255] or None))
    return added


@app.route("/api/purchases/delete", methods=["POST"])
def api_purchase_delete():
    data = request.get_json(force=True, silent=True) or {}
    pid = _num_or_none(data.get("id"), int)
    if pid is None:
        return jsonify({"ok": False, "error": "missing id"}), 400
    ok = store.delete_purchase(pid)
    _rebuild_order_prices()
    return jsonify({"ok": ok, "id": pid})


@app.route("/api/purchases/parse", methods=["POST"])
def api_purchase_parse():
    """Read an order: an uploaded invoice PDF, or pasted text.

    Stores nothing - the result is a suggestion the user corrects and then saves.
    A Bambu invoice is recognised by its layout and parsed precisely (colour code,
    real price paid); anything else falls back to loose text heuristics.
    """
    f = request.files.get("file")
    if f is not None:
        blob = f.read(8 * 1024 * 1024)      # an invoice is tens of KB; cap anyway
        if not blob:
            return jsonify({"ok": False, "error": "empty file"}), 400
        try:
            text = (_pdf_text(blob) if (f.filename or "").lower().endswith(".pdf")
                    else blob.decode("utf-8", "replace"))
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)[:300]}), 400
    else:
        data = request.get_json(force=True, silent=True) or {}
        text = (data.get("text") or "")[:40000]
    if not text.strip():
        return jsonify({"ok": False, "error": "nothing to read"}), 400
    return jsonify({"ok": True, **filament_catalog.parse_order(text)})


@app.route("/api/maintenance")
def api_maintenance():
    return jsonify(_maintenance_block())


@app.route("/api/maintenance/reset", methods=["POST"])
def api_maintenance_reset():
    data = request.get_json(force=True, silent=True) or {}
    key = data.get("key")
    if key not in MAINT_KEYS:
        return jsonify({"ok": False, "error": "unknown task"}), 400
    # stamp this task as just-done at the current cumulative print hours
    store.set_setting(f"maint_reset_{key}", _recorded_print_hours())
    return jsonify({"ok": True, **_maintenance_block()})


@app.route("/api/maintenance/config", methods=["POST"])
def api_maintenance_config():
    data = request.get_json(force=True, silent=True) or {}
    if "offset_hours" in data:
        try:
            store.set_setting("maint_offset_hours", max(0.0, float(data["offset_hours"])))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "bad offset"}), 400
    key = data.get("key")
    if key is not None:
        if key not in MAINT_KEYS:
            return jsonify({"ok": False, "error": "unknown task"}), 400
        try:
            store.set_setting(f"maint_interval_{key}", max(1.0, float(data.get("interval_hours"))))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "bad interval"}), 400
    return jsonify({"ok": True, **_maintenance_block()})


@app.route("/api/cloud/refresh", methods=["POST"])
def api_cloud_refresh():
    if not CLOUD_CFG.get("enabled"):
        return jsonify({"ok": False, "error": "cloud not configured"}), 400
    try:
        n = cloud_sync_once()
        return jsonify({"ok": True, "updated": n})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:200]}), 502


@app.route("/api/prints/filament", methods=["POST"])
def api_print_filament():
    """Manual grams override for one print (blank clears it)."""
    data = request.get_json(force=True, silent=True) or {}
    job_id = (data.get("job_id") or "").strip()
    if not job_id:
        return jsonify({"ok": False, "error": "missing job_id"}), 400
    raw = str(data.get("grams") or "").strip()
    grams = None
    if raw:
        try:
            grams = max(0.0, float(raw.replace(",", ".")))
        except ValueError:
            return jsonify({"ok": False, "error": "grams must be a number"}), 400
    ok = store.update_print_fields(job_id, filament_g_manual=grams)
    # recost immediately using the stored per-slot detail
    row = store.get_print(job_id) or {}
    detail = row.get("filament_detail")
    if detail:
        try:
            entries = json.loads(detail)
            base = sum(float(d.get("grams") or 0) for d in entries)
            cost = sum(float(d.get("cost") or 0) for d in entries)
            if grams and base:
                cost *= grams / base
            store.update_print_fields(job_id, filament_cost=round(cost, 4))
        except (TypeError, ValueError):
            pass
    return jsonify({"ok": ok, "job_id": job_id, "grams": grams})


@app.route("/api/prints/label", methods=["POST"])
def api_print_label():
    data = request.get_json(force=True, silent=True) or {}
    job_id = (data.get("job_id") or "").strip()
    label = (data.get("label") or "").strip()[:255]
    if not job_id:
        return jsonify({"ok": False, "error": "missing job_id"}), 400
    ok = store.set_print_label(job_id, label)
    # if this is the print currently on screen, reflect the rename right away
    if job_id == _print_row.get("job_id"):
        _print_row.setdefault("stored", {})["label"] = label or None
    return jsonify({"ok": ok, "job_id": job_id, "label": label or None})


@app.route("/api/prints/group", methods=["POST"])
def api_print_group():
    """Group prints under a name, or ungroup them when the name is blank.

    One model often takes several prints; grouping is how the history stops
    reading as a flat list of unrelated jobs. Renaming a group is the same call
    with the same jobs and a different name.
    """
    data = request.get_json(force=True, silent=True) or {}
    jobs = [str(j).strip() for j in (data.get("job_ids") or []) if str(j).strip()]
    name = (data.get("name") or "").strip()[:120]
    if not jobs:
        return jsonify({"ok": False, "error": "no prints selected"}), 400
    try:
        n = store.set_print_group(jobs, name or None)
    except Exception as e:
        print(f"[prints] group failed: {e}")
        return jsonify({"ok": False, "error": str(e)[:300]}), 500
    # keep the live tile in step if the running print was part of it
    if _print_row.get("job_id") in jobs:
        _print_row.setdefault("stored", {})["pgroup"] = name or None
    return jsonify({"ok": True, "updated": n, "name": name or None})


@app.route("/api/prints/delete", methods=["POST"])
def api_print_delete():
    """Remove one print from the history (e.g. test prints started at the
    printer itself). A running print is refused: it would only be re-created by
    the next persist tick, and its energy totals are still being accumulated."""
    data = request.get_json(force=True, silent=True) or {}
    job_id = (data.get("job_id") or "").strip()
    if not job_id:
        return jsonify({"ok": False, "error": "missing job_id"}), 400
    with _state_lock:
        live = dict(_state.get("job") or {})
    if str(live.get("task_id") or "") == job_id and live.get("state") in ACTIVE_STATES:
        return jsonify({"ok": False, "error": "print is still running"}), 409
    ok = store.delete_print(job_id)
    # Only the job still being reported can be written back - see _deleted_jobs
    if job_id == _print_row.get("job_id"):
        _deleted_jobs.add(job_id)
    return jsonify({"ok": ok, "job_id": job_id})


@app.route("/api/raw")
def api_raw():
    """The complete last report from the printer - everything it exposes.
    `covered` lists the keys the app consumes, so the dashboard can highlight
    which parts of the raw payload are already surfaced and which are untapped."""
    return jsonify({"data": _last_raw["data"] or {}, "covered": COVERED_RAW_KEYS})


@app.route("/api/recording", methods=["POST"])
def api_recording():
    """Set recording mode (auto|on|off). Persisted, so it survives restarts.
    Live telemetry/dashboard is unaffected - only DB writes are gated."""
    global _rec_mode
    data = request.get_json(force=True, silent=True) or {}
    mode = data.get("mode")
    if mode not in RECORD_MODES:
        return jsonify({"ok": False, "error": "mode must be auto|on|off"}), 400
    _rec_mode = mode
    store.set_setting("recording", mode)
    print(f"[store] recording mode -> {mode}")
    # 'off' also drops the printer connection entirely
    if mode == "off":
        _mqtt_enabled.clear()
    else:
        _mqtt_enabled.set()
    with _state_lock:
        cur = dict(_state)
    cur["recording_mode"] = mode
    cur["recording_active"] = _should_record(cur)
    cur["stream_enabled"] = mode != "off"
    if mode == "off":
        cur["connected"] = False
    _publish_state(cur)
    return jsonify({"ok": True, "mode": mode, "active": cur["recording_active"],
                    "stream": cur["stream_enabled"]})


_SPEED_PARAMS = {"1", "2", "3", "4"}                 # Silent / Standard / Sport / Ludicrous
_FAN_GCODE = {"cooling": "P1", "aux1": "P2", "aux2": "P3"}   # M106 P<n> S<0-255>


@app.route("/api/print/control", methods=["POST"])
def api_print_control():
    """Print-flow controls over the local MQTT request topic. Strict allowlist -
    never a free-form gcode passthrough - so the command surface stays tight."""
    data = request.get_json(force=True, silent=True) or {}
    action = data.get("action")
    if action in ("pause", "resume", "stop"):
        cmd = {"print": {"sequence_id": "0", "command": action}}
    elif action == "speed":
        param = str(data.get("param", ""))
        if param not in _SPEED_PARAMS:
            return jsonify({"ok": False, "error": "speed must be 1-4"}), 400
        cmd = {"print": {"sequence_id": "0", "command": "print_speed", "param": param}}
    elif action == "fan":
        p = _FAN_GCODE.get(data.get("fan"))
        if not p:
            return jsonify({"ok": False, "error": "unknown fan"}), 400
        try:
            pct = max(0, min(100, int(data.get("percent"))))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "percent must be 0-100"}), 400
        cmd = {"print": {"sequence_id": "0", "command": "gcode_line",
                         "param": f"M106 {p} S{round(pct * 255 / 100)}"}}
    elif action == "temp":
        # bed = M140, chamber = M141 (S0 = off). Clamped to safe ranges.
        spec = {"bed": (120, "M140"), "chamber": (60, "M141")}.get(data.get("target"))
        if not spec:
            return jsonify({"ok": False, "error": "unknown temp target"}), 400
        hi, mcode = spec
        try:
            val = max(0, min(hi, int(data.get("value"))))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "bad temperature"}), 400
        cmd = {"print": {"sequence_id": "0", "command": "gcode_line", "param": f"{mcode} S{val}"}}
    else:
        return jsonify({"ok": False, "error": "unknown action"}), 400
    client = _mqtt_client
    if client is None:
        return jsonify({"ok": False, "error": "printer not connected"}), 409
    try:
        client.publish(REQUEST_TOPIC, json.dumps(cmd))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:120]}), 500
    return jsonify({"ok": True})


@app.route("/api/led", methods=["POST"])
def api_led():
    """Turn the chamber LED on or off via the printer's request topic.
    Needs a live MQTT connection (recording mode must not be 'off')."""
    data = request.get_json(force=True, silent=True) or {}
    mode = data.get("mode")
    if mode not in ("on", "off"):
        return jsonify({"ok": False, "error": "mode must be on|off"}), 400
    client = _mqtt_client
    if client is None:
        return jsonify({"ok": False, "error": "printer not connected"}), 409
    cmd = json.dumps({"system": {
        "sequence_id": "0", "command": "ledctrl",
        "led_node": data.get("node", "chamber_light"), "led_mode": mode,
        "led_on_time": 500, "led_off_time": 500,
        "loop_times": 0, "interval_time": 0,
    }})
    try:
        client.publish(REQUEST_TOPIC, cmd)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    # Optimistically reflect the new state so the button flips immediately;
    # the printer's next report confirms it.
    with _state_lock:
        cur = dict(_state)
    lights = dict(cur.get("lights") or {})
    lights[data.get("node", "chamber_light")] = mode
    cur["lights"] = lights
    _publish_state(cur)
    return jsonify({"ok": True, "mode": mode})


@app.route("/api/camera")
def api_camera():
    """Camera config the frontend needs to decide whether to show the Live view
    tab and how to reach the go2rtc relay. No secrets - the RTSPS URL/access code
    stays server-side in go2rtc.yaml."""
    return jsonify({
        "enabled": bool(CAM_CFG.get("enabled")),
        "api_port": int(CAM_CFG.get("api_port", 1984)),
        "src": CAM_CFG.get("src", "bambu"),
    })


@app.route("/api/hms/ack", methods=["POST"])
def api_hms_ack():
    data = request.get_json(force=True, silent=True) or {}
    code = data.get("code")
    ts = data.get("ts") or ""
    if not code:
        return jsonify({"ok": False, "error": "missing code"}), 400
    if data.get("acked", True):
        store.ack_hms(code, ts)
        _acked.add((code, ts))
    else:
        store.unack_hms(code, ts)
        _acked.discard((code, ts))
    # re-annotate current state and push so every open dashboard updates at once
    with _state_lock:
        cur = dict(_state)
    _annotate_acks(cur)
    _publish_state(cur)
    return jsonify({"ok": True})


@app.route("/events")
def events():
    q: queue.Queue = queue.Queue(maxsize=10)
    with _subs_lock:
        _subscribers.append(q)
    # send current state immediately on connect
    with _state_lock:
        initial = json.dumps(dict(_state))

    def stream():
        yield f"data: {initial}\n\n"
        try:
            while True:
                try:
                    payload = q.get(timeout=15)
                    yield f"data: {payload}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"  # keep proxies from closing idle conn
        finally:
            with _subs_lock:
                if q in _subscribers:
                    _subscribers.remove(q)

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _write_go2rtc_config() -> str:
    """Generate go2rtc.yaml from the camera config. The RTSPS access code is
    injected here from printer.config.json so it never has to live in a second
    file. `#transport=udp` is required - the X2D's LIVE555 camera only feeds RTP
    over UDP, not TCP-interleaved (go2rtc's default)."""
    api_port = int(CAM_CFG.get("api_port", 1984))
    webrtc_port = int(CAM_CFG.get("webrtc_port", 8555))
    rtsp_port = int(CAM_CFG.get("rtsp_port", 322))
    src = CAM_CFG.get("src", "bambu")
    url = (f"rtsps://bblp:{CFG['access_code']}@{CFG['ip']}:{rtsp_port}"
           f"/streaming/live/1#transport=udp#backchannel=0")
    yaml = (
        "# AUTO-GENERATED by app.py from printer.config.json - do not edit.\n"
        "api:\n"
        f'  listen: ":{api_port}"\n'
        "webrtc:\n"
        f'  listen: ":{webrtc_port}"\n'
        "log:\n"
        "  level: info\n"
        "streams:\n"
        f"  {src}: {url}\n"
    )
    path = os.path.join(HERE, "go2rtc.yaml")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(yaml)
    return path


def go2rtc_worker():
    """Launch and supervise the go2rtc relay - a single static binary, no Docker.
    Converts the printer's RTSPS/H.264 into browser-playable WebRTC/MSE. Restarts
    it if it dies. Only started when camera.enabled is set."""
    binpath = os.path.join(HERE, CAM_CFG.get("bin", "go2rtc/go2rtc_linux_arm64"))
    if not os.path.exists(binpath):
        print(f"[cam] go2rtc binary not found at {binpath}; live view disabled")
        return
    try:  # a binary copied over SMB often loses the executable bit
        os.chmod(binpath, 0o755)
    except OSError:
        pass
    cfgpath = _write_go2rtc_config()
    while True:
        try:
            print("[cam] starting go2rtc relay")
            proc = subprocess.Popen([binpath, "-config", cfgpath], cwd=HERE)
            proc.wait()
            print(f"[cam] go2rtc exited ({proc.returncode}); restarting in 5s")
        except Exception as e:
            print(f"[cam] go2rtc error: {e}; retrying in 5s")
        time.sleep(5)


def purge_worker():
    keep = float(STORE_CFG.get("retention_days", 30))
    while True:
        try:
            n = store.purge(keep_days=keep)
            if n:
                print(f"[store] purged {n} rows older than {keep} days")
        except Exception as e:
            print(f"[store] purge failed: {e}")
        time.sleep(86400)


if __name__ == "__main__":
    threading.Thread(target=mqtt_worker, daemon=True).start()
    threading.Thread(target=purge_worker, daemon=True).start()
    if PWR_CFG.get("enabled"):
        threading.Thread(target=power_worker, daemon=True).start()
    if CLOUD_CFG.get("enabled"):
        threading.Thread(target=cloud_worker, daemon=True).start()
    if CAM_CFG.get("enabled"):
        threading.Thread(target=go2rtc_worker, daemon=True).start()
    print(f"[web] http://localhost:{PORT}  (printer {CFG['ip']}, model {CFG.get('model','?')}, storage={STORE_CFG.get('backend')})")
    app.run(host="0.0.0.0", port=PORT, threaded=True)
