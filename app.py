#!/usr/bin/env python3
"""
Bambu X2D Monitor - single-process app: a background MQTT thread keeps the
latest normalized printer state, and Flask serves a live dashboard.

    python app.py            # serves http://localhost:8770
    python app.py --setup    # re-run the setup wizard

Configuration lives in the database, edited from the Settings page. The one
exception is the database connection itself, which is in instance/db.json - see
bootstrap.py. With no connection on file the app serves the setup wizard instead
of the dashboard.

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
import atexit
import signal
import subprocess
import ssl
import sys
import threading
import time

from datetime import datetime, timedelta, timezone

import paho.mqtt.client as mqtt
from flask import Flask, Response, jsonify, request, send_file

import backup
import bootstrap
import filament_catalog
import gcode_meta
import power_providers
import config_store
import settings_schema
from bambu_state import parse_report
from storage import Storage

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("BAMBU_PORT", "8770"))

# Nothing below this point can run without a database, and the wizard is what
# produces one. It serves on the same port, so an unconfigured app is not a
# blank page but the question it needs answered; finishing re-execs this file.
if bootstrap.load() is None or "--setup" in sys.argv:
    import setup_wizard
    setup_wizard.serve(PORT)
    bootstrap.restart()

# the one thing that cannot come from the database: the database
STORE_CFG = bootstrap.load()
store = Storage(STORE_CFG)

# Every setting is a row in that database. Sections handed out below are LIVE
# views, so an edit applies without a restart and without every call site
# changing. Nothing may read CFG before the attach - there is no file layer
# underneath it any more, so before this line the config is only defaults.
# a write to prints is the only thing that can change what printing has cost,
# so that is what drops the cached figure - no call site has to remember
store.on_write = lambda table: _cost_dirty() if table == "prints" else None

CONFIG = config_store.ConfigStore()
CONFIG.attach(store)
CFG = CONFIG.section()

# The serial is restart-only (settings_schema live=False), which is exactly why
# these can be constants.
REPORT_TOPIC = f"device/{CFG.get('serial', '')}/report"
REQUEST_TOPIC = f"device/{CFG.get('serial', '')}/request"
PUSHALL = json.dumps({"pushing": {"sequence_id": "0", "command": "pushall"}})
GET_VERSION = json.dumps({"info": {"sequence_id": "0", "command": "get_version"}})

def SAMPLE_INTERVAL():
    return float(CFG.get("storage", {}).get("sample_interval_sec", 20))
_acked = store.acked_keys()  # set of (code, ts) the user has dismissed

# Recording mode gates DB writes only; the live dashboard updates regardless.
#   on   - always record
#   off  - never record
#   auto - record only while a print is active (+ a tail, so cool-down is kept),
#          leaving the NAS disks idle the rest of the time so they can hibernate
RECORD_MODES = ("auto", "on", "off")
ACTIVE_STATES = {"RUNNING", "PREPARE", "PAUSE", "SLICING"}
def AUTO_TAIL_SEC():
    return float(CFG.get("storage", {}).get("auto_tail_min", 10)) * 60

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
    return bool(last) and (now - last) <= AUTO_TAIL_SEC()


def _annotate_acks(state: dict) -> dict:
    for h in state.get("hms", []) or []:
        h["acked"] = (h["code"], h.get("ts") or "") in _acked
    return state

# ---- shared state ----------------------------------------------------------
# How many reports the printer has pushed since start. The frame rate is the
# denominator for everything else the app does: anything called from on_message
# happens this often, which is easy to forget when writing it.
_frames = {"n": 0}
_started_at = time.time()

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
# live sections, not snapshots: an edit on the Settings page has to reach
# code that bound these at import
PWR_CFG = CONFIG.section("power")
COST_CFG = CONFIG.section("cost")
CAM_CFG = CONFIG.section("camera")
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


def _error_code(state: dict):
    """The failure the printer is reporting right now, if any."""
    errs = state.get("errors") or {}
    return errs.get("print_error") or errs.get("mc_code") or errs.get("fail_reason")


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
        # What the printer is reporting as the model right now belongs to the
        # job that just ENDED. Remember it so this one cannot inherit the link.
        _print_row["carried_design"] = _print_row.get("design_id")
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
        # `print_error` is machine state, not job state: the printer keeps
        # reporting the last failure long after that job is gone, so whatever is
        # showing right now belongs to the PREVIOUS print. Remember it and refuse
        # to blame this one for it until the printer reports something else.
        _print_row["stale_err"] = _error_code(state)
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
    # The MakerWorld reference is machine state, not job state - exactly like
    # print_error. The printer keeps reporting the last model it printed long
    # after that job is gone, so a self-sliced print started afterwards inherits
    # the previous print's link. Two rules:
    #   * incremental frames often omit the field, so only a frame that actually
    #     carries one may overwrite the latch;
    #   * an id identical to the PREVIOUS job's is ambiguous - a repeat print and
    #     a leftover look exactly the same from here - so it is refused. The
    #     cloud pass resolves that case later, by design title.
    reported = job.get("design_id")
    if reported and reported != _print_row.get("carried_design"):
        _print_row["design_id"] = reported
        # the profile identifies a plate WITHIN a design, so it is only ever
        # meaningful paired with the design it arrived with
        _print_row["profile_id"] = job.get("profile_id") or _print_row.get("profile_id")
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
        if changed and st in ("FINISH", "FAILED"):
            if CLOUD_CFG.get("enabled"):
                _cloud_kick.set()
            # the sliced file is still on the drive after the job - proven on
            # this printer - so this reads it at finish rather than at start,
            # when the printer has better things to do
            if SLICER_CFG.get("enabled"):
                _slicer_kick.set()


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
        )
        _print_row["stored"] = {**stored, "energy_wh": energy, "peak_w": peak,
                                "started_at": _print_row["started_at"],
                                "ended_at": ended}
        # Set, never blank: upsert_print treats these as immutable precisely so
        # a job that reports no model cannot wipe one that does.
        did = _print_row.get("design_id")
        if did and did != stored.get("design_id"):
            store.update_print_fields(tid, design_id=did,
                                      profile_id=_print_row.get("profile_id"))
            _print_row["stored"]["design_id"] = did
        # Capture why it failed, while the printer is still reporting it - but
        # not the previous job's failure, which the printer is still showing
        # because nothing clears it when a new print starts.
        code = _error_code(state)
        if not code:
            _print_row["stale_err"] = None   # cleared: anything from now on is real
        elif code == _print_row.get("stale_err"):
            code = None                      # still the old one, not this print's
        if code and code != (stored.get("error_code")):
            store.update_print_fields(tid, error_code=str(code)[:64])
            _print_row["stored"]["error_code"] = str(code)[:64]

        # Record which slots held genuine Bambu spools *while this print ran*.
        # Spools get swapped, so reading it later would price the job wrongly.
        if not _print_row["stored"].get("ams_bambu"):
            snap = _ams_bambu_map(state)
            if snap:
                blob = json.dumps(snap)
                slots = json.dumps(_ams_slot_map(state))
                store.update_print_fields(tid, ams_bambu=blob, ams_slots=slots)
                _print_row["stored"]["ams_bambu"] = blob
                _print_row["stored"]["ams_slots"] = slots
    except Exception as e:
        print(f"[prints] upsert failed: {e}")


def _grams(row):
    m = row.get("filament_g_manual")
    return m if m is not None else row.get("filament_g")


# _cost_block reads the whole prints table and sorts it. It is called from
# on_message, so it ran at the printer's frame rate - several times a second,
# for a figure that changes when a print is written, which is at most once a
# minute. Cached, and invalidated by the writes that can change it.
_cost_cache = {"at": 0.0, "day": None, "block": None}
_COST_TTL = 20.0


def _cost_dirty() -> None:
    """Call after anything that changes what a print cost."""
    _cost_cache["block"] = None


def _cost_block() -> dict:
    # the calendar windows move on their own, so a cached block also expires
    # when the day rolls over, not only when a row changes
    today = datetime.now().toordinal()
    cached = _cost_cache["block"]
    if (cached is not None and _cost_cache["day"] == today
            and (time.time() - _cost_cache["at"]) < _COST_TTL):
        return cached
    block = _cost_block_uncached()
    _cost_cache.update(at=time.time(), day=today, block=block)
    return block


def _cost_block_uncached() -> dict:
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
    months, models, days = {}, {}, {}
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
            # A print is counted on the day it STARTED, so one job never lands
            # in two buckets and the daily numbers still sum to the totals.
            dk = datetime.fromtimestamp(s).strftime("%Y-%m-%d")
            dd = days.setdefault(dk, {"day": dk, "prints": 0, "energy_wh": 0.0,
                                      "cost": 0.0, "filament_g": 0.0, "seconds": 0.0})
            dd["prints"] += 1
            dd["energy_wh"] += r.get("energy_wh") or 0
            dd["cost"] += pc + mc
            dd["filament_g"] += g
            if e and e > s:
                dd["seconds"] += e - s
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
        # a year of days; the page slices the window it wants to show
        "by_day": [{**days[k], "energy_wh": round(days[k]["energy_wh"], 1),
                    "cost": round(days[k]["cost"], 4),
                    "filament_g": round(days[k]["filament_g"], 1),
                    "seconds": round(days[k]["seconds"])}
                   for k in sorted(days)][-370:],
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
    _frames["n"] += 1
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
    # which command families this firmware will accept, so the UI can offer only
    # the controls that actually work
    state["controls"] = {"gcode": CTRL_GCODE()}
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
    if changed or (now - _last_record["ts"]) >= SAMPLE_INTERVAL():
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


def POWER_PROVIDER():
    return str(PWR_CFG.get("provider") or "tapo")


def power_worker():
    """Keep `_power` fed from whichever meter is configured.

    The provider owns its own loop - Tapo is polled and MQTT is pushed, and one
    shape cannot serve both honestly. All this does is choose one, refuse to
    start a half-configured one, and give it somewhere to report to.
    """
    name = POWER_PROVIDER()
    cls = power_providers.PROVIDERS.get(name)
    if cls is None:
        print(f"[power] no such meter {name!r}; expected one of "
              f"{', '.join(sorted(power_providers.PROVIDERS))}")
        _power.update(error=f"unknown meter {name}")
        return

    # The provider's own settings live under its name for anything but tapo,
    # which was here first and keeps the flat keys its install already has.
    cfg = PWR_CFG if name == "tapo" else CONFIG.section(f"power.{name}")

    def report(**fields):
        _power.update(**fields)
        if "watts" in fields:
            _accumulate_job_energy(fields["watts"])

    provider = cls(
        cfg=cfg, report=report, active=_mqtt_enabled.is_set,
        state_io=(lambda: _load_power_state(name),
                  lambda d: _save_power_state(name, d)),
    )

    # Enabled but not filled in is a reachable state: the Settings page can
    # switch a meter on before its details are typed. Say what is missing and
    # stop, rather than raising in a thread nobody is watching.
    missing = provider.missing()
    if missing:
        print(f"[power] {name} is enabled, but {', '.join(missing)} not set - "
              f"fill it in under Settings > Power")
        _power.update(error="not configured")
        return

    print(f"[power] meter: {name}")
    provider.run()


# Where a provider keeps what it must remember across restarts - for MQTT, the
# energy counter's value at the start of today and of this month. In the
# settings table rather than a file, like everything else.
def _load_power_state(name: str) -> dict:
    try:
        return json.loads(store.get_setting(f"power_state_{name}", "") or "{}")
    except (TypeError, ValueError):
        return {}


def _save_power_state(name: str, data: dict) -> None:
    try:
        store.set_setting(f"power_state_{name}", json.dumps(data))
    except Exception as e:
        print(f"[power] could not save the energy baseline: {e}")


FIL_CFG = CONFIG.section("filament")
CLOUD_CFG = CONFIG.section("cloud")

# Reordering: what counts as "nearly used up", and which regional store to link.
def FIL_LOW_PCT():
    return float(FIL_CFG.get("low_pct", 15))
def FIL_STORE_REGION():
    return FIL_CFG.get("store_region", filament_catalog.DEFAULT_REGION)
def FIL_STORE_HOST():
    return FIL_CFG.get("store_host") or None   # full host override, optional
def FIL_COLOR_NAMES():
    return FIL_CFG.get("color_names") or {}   # extends/corrects the built-ins
# Colour names read off imported invoices - Bambu's own wording, so they beat the
# built-in guess table. Config still wins, as the last word is always the user's.
try:    # keys are canonicalised on load, so entries written before norm_code
    _LEARNED_COLORS = {filament_catalog.norm_code(k): v
                       for k, v in store.settings_with_prefix("cname_").items()}
except Exception:
    _LEARNED_COLORS = {}


def _color_overrides() -> dict:
    return {**_LEARNED_COLORS, **FIL_COLOR_NAMES()}


# Per-kg prices taken from your own orders, so the config matrix stops being a
# number you have to maintain by hand. Keyed by Bambu SKU (GFA00).
def PRICES_FROM_ORDERS():
    return bool(FIL_CFG.get("prices_from_orders", True))
_ORDER_PRICES: dict = {}


# {fkey: EUR per kg} typed in by hand on the Filament page. Beats every other
# rule: the config matrix is a guess about a brand and a material, an order is a
# guess that the SKU on the invoice is the spool in the tray, and this is
# neither - it is somebody stating what the spool cost.
_FIL_PRICES: dict = {}
_FIL_ALIAS: dict = {}
# fkey -> (vendor, product, colour name) as typed in on the Filament page. The
# AMS can only name a spool it can read, so a third-party tray arrives with no
# name at all - but the identity it reports is the same one that was named, and
# a name that only shows on one page is half a name.
_FIL_NAMES: dict = {}
# fkey -> (SKU, was the RFID tag genuine). After a merge the folded prints have
# to be priced as the filament they were merged INTO, and that needs the
# survivor's SKU - the entry's own is the one that was wrong.
_FIL_ID: dict = {}


def _rebuild_filament_meta() -> None:
    """Reload what the user has told us about each filament: the hand-set
    prices, the names, and the merge map both have to follow.

    Cached because the AMS enrichment runs on the MQTT frame rate and this is
    edited by hand a few times a month.
    """
    try:
        rows = store.all_filaments()
    except Exception as e:
        print(f"[filament] could not read prices: {e}")
        return
    _FIL_PRICES.clear()
    _FIL_ALIAS.clear()
    _FIL_NAMES.clear()
    _FIL_ID.clear()
    for r in rows:
        if r.get("alias_of"):
            _FIL_ALIAS[r["fkey"]] = r["alias_of"]
        if r.get("price_per_kg") is not None:
            _FIL_PRICES[r["fkey"]] = float(r["price_per_kg"])
        named = (r.get("vendor"), r.get("product"), r.get("color_name"))
        if any(named):
            _FIL_NAMES[r["fkey"]] = named
        _FIL_ID[r["fkey"]] = (r.get("filament_id"),
                              None if r.get("is_bambu") is None else bool(r["is_bambu"]))


def _canon_fkey(fkey: str, hops: int = 6) -> str:
    """Follow merges, so a price set on the surviving row prices the folded ones
    too - otherwise merging two identities would silently split their price."""
    seen = set()
    while hops > 0 and fkey not in seen:
        seen.add(fkey)
        nxt = _FIL_ALIAS.get(fkey)
        if not nxt or nxt == fkey:
            break
        fkey, hops = nxt, hops - 1
    return fkey


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
_rebuild_filament_meta()


def _enrich_ams(state: dict) -> None:
    """Annotate every tray in place with colour name, reorder link and a
    low-stock flag. Purely presentational, so it lives here and not in the
    parser, which stays a pure function of the printer's report."""
    ams = state.get("ams") or {}
    for trays in [u.get("trays") or [] for u in (ams.get("units") or [])] + [ams.get("external") or []]:
        for tr in trays:
            tr.update(filament_catalog.describe(
                tr, overrides=_color_overrides(),
                region=FIL_STORE_REGION(), host=FIL_STORE_HOST()))
            # What this spool was called on the Filament page. For a spool with
            # no RFID that is the ONLY name it can have; for a Bambu one a name
            # typed in by hand beats the catalogue, which is the same rule the
            # Filament page follows.
            vendor, product, cname = _FIL_NAMES.get(_canon_fkey(
                filament_catalog.key(tr.get("filament_id"), tr.get("color"),
                                     tr.get("type"))), (None, None, None))
            if vendor:  tr["vendor"] = vendor
            if product: tr["product"] = product
            if cname:   tr["color_name"] = cname
            pct = tr.get("remain_pct")
            # Only an RFID spool reports a real remaining %: a third-party tray
            # sends -1 and an external spool sends 0, and neither means empty -
            # warning on those would cry wolf on every non-Bambu spool.
            tr["low"] = (bool(tr.get("is_bambu")) and pct is not None
                         and 0 <= pct <= FIL_LOW_PCT())
    if ams:
        ams["low_pct"] = FIL_LOW_PCT()
        ams["can_assign"] = AMS_ASSIGN()   # hides the button unless switched on


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
    done = set()      # one write per identity per frame, not one per tray
    for trays in [u.get("trays") or [] for u in (ams.get("units") or [])] + [ams.get("external") or []]:
        for tr in trays:
            if not tr.get("type"):
                continue          # empty slot
            fkey = filament_catalog.key(tr.get("filament_id"), tr.get("color"), tr.get("type"))
            # Two spools of the same filament are one identity by definition, and
            # the printer does not report them identically: two black PLA trays
            # came back as 'A00-K00' and 'A00-K0', the same code with different
            # padding. Each then looked like a change to the other, and both
            # wrote on every frame - about 100 UPDATEs a minute with the printer
            # sitting idle. norm_code is what the rest of the app compares by.
            if fkey in done:
                continue
            done.add(fkey)
            ident = (tr.get("filament_id"), filament_catalog.norm_code(tr.get("code")),
                     tr.get("brand"),
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


def _match_purchase(p: dict, agg: dict, known: dict, fkey_by_code: dict,
                    fkey_by_match: dict, sku_colours: dict) -> tuple:
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
        want = (p.get("color_name") or "").strip().lower()
        # Every colour of one product line derives the SAME sku - A01-R4, A01-G0
        # and A01-B6 are all GFA01 - so this route must never match on the sku
        # alone. Drop any candidate that positively contradicts this purchase,
        # by colour code or by colour name.
        ok = []
        for k in cands:
            m = known.get(k, {})
            kcode = filament_catalog.norm_code(m.get("code"))
            if kcode and code and kcode != code:
                continue                       # a different colour, same product
            kname = (m.get("color_name") or "").strip().lower()
            if kname and want and kname != want:
                continue
            ok.append(k)
        named = [k for k in ok
                 if (known.get(k, {}).get("color_name") or "").strip().lower() == want]
        if want and len(named) == 1:
            return named[0], "sku+colour"
        if len(ok) == 1:
            # The one survivor may simply be anonymous - a filament the print
            # history knows by sku and hex only. Then it can be claimed by at
            # most one colour: if the purchase log holds several colours of this
            # product, guessing which one it is would be a coin flip.
            m = known.get(ok[0], {})
            if (m.get("code") or m.get("color_name")
                    or len(sku_colours.get(sku, ())) <= 1):
                return ok[0], "sku"
            return None, "ambiguous"
        if ok:
            return None, "ambiguous"
    return None, None


def _left_grams(a: dict, b: dict | None, meta: dict):
    """How much of this filament is left.

    Normally bought minus used, which is only as good as the two logs behind it:
    a deleted print stops counting as used, and a spool bought before the
    invoice importer existed never counted as bought. Either way the figure
    drifts, and no amount of arithmetic can find the truth.

    So it can be anchored: "there were N grams left, as of then". From that
    moment the same arithmetic resumes - prints after it subtract, purchases
    after it add - so a correction typed in once keeps working.
    """
    anchor = meta.get("left_anchor_g")
    if anchor is not None:
        return round(float(anchor)
                     - a.get("grams_since", 0.0)
                     + ((b or {}).get("grams_since", 0.0)), 1)
    return round(b["grams"] - a["grams"], 1) if b else None


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

    # An identity folded into another by hand. Followed with a hop limit so a
    # cycle can't hang the page - a -> b -> a is a user's mistake, not a crash.
    def canon(k, hops=6):
        seen = set()
        while hops > 0 and k not in seen:
            seen.add(k)
            nxt = (known.get(k) or {}).get("alias_of")
            if not nxt or nxt == k:
                break
            k, hops = nxt, hops - 1
        return k

    # "there were N grams left on <date>", per identity. Everything printed or
    # bought after that moment still counts, so the correction ages forward
    # instead of freezing the figure. Read on the canonical key, because an
    # identity folded into another shares its stock.
    anchored = {k: f.get("left_anchor_at") for k, f in known.items()
                if f.get("left_anchor_g") is not None and f.get("left_anchor_at")}

    agg = {}
    for r in prints:
        started = r.get("started_at")
        for e in _detail_entries(r):
            fkey = canon(filament_catalog.key(e.get("filament_id"), e.get("color"),
                                              e.get("type")))
            a = agg.setdefault(fkey, {
                "fkey": fkey, "filament_id": e.get("filament_id"),
                "color": filament_catalog.norm_color(e.get("color")),
                "type": e.get("type"), "grams": 0.0, "cost": 0.0, "prints": 0,
                "first_used": None, "last_used": None,
            })
            a["grams"] += e["grams"]
            a["cost"] += e["cost"]
            a["prints"] += 1
            # A print with no start time cannot be placed either side of the
            # anchor. It is not counted against it rather than guessed at.
            if started and started >= (anchored.get(fkey) or float("inf")):
                a["grams_since"] = a.get("grams_since", 0.0) + e["grams"]
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
        if f.get("alias_of"):
            continue                    # folded into another row
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
    # how many distinct colours of each product line the purchase log knows -
    # used to refuse a coin-flip match onto a single anonymous identity
    sku_colours = {}
    for p in purchases:
        sku = filament_catalog.sku_from_code(p.get("code"))
        nc = filament_catalog.norm_code(p.get("code"))
        if sku and nc:
            sku_colours.setdefault(sku, set()).add(nc)
    for p in purchases:
        fkey, how = _match_purchase(p, agg, known, fkey_by_code, fkey_by_match,
                                    sku_colours)
        p["matched_by"] = how
        if not fkey:
            unmatched.append(p)
            continue
        p["_fkey"] = fkey       # resolved to a display name once `out` is built
        b = buys.setdefault(fkey, {"grams": 0.0, "cost": 0.0, "spools": 0,
                                   "orders": 0, "last": None})
        grams = float(p.get("spools") or 1) * float(p.get("grams_each") or 1000)
        b["grams"] += grams
        b["cost"] += float(p.get("total_price") or 0)
        b["spools"] += int(p.get("spools") or 1)
        b["orders"] += 1
        when = p.get("ordered_at") or p.get("created_at")
        if when:
            b["last"] = max(b["last"] or when, when)
        # a spool bought after the anchor adds to what is left, the same way a
        # print after it takes away
        if when and when >= (anchored.get(fkey) or float("inf")):
            b["grams_since"] = b.get("grams_since", 0.0) + grams

    # what is in the AMS right now -> remaining %, slot and the reorder link
    with _state_lock:
        ams = json.loads(json.dumps((_state.get("ams") or {})))   # cheap deep copy
    loaded = {}
    # setdefault, not assignment, and AMS trays before the external holder: the
    # printer keeps reporting the last filament assigned to the external slot
    # even after that spool has been moved into a tray. Same profile and colour
    # means the same identity, so plain assignment let the stale external entry
    # overwrite the real tray and the spool showed as "external" forever.
    groups = [(u.get("trays") or [], False) for u in (ams.get("units") or [])]
    groups.append((ams.get("external") or [], True))
    for trays, ext in groups:
        for tr in trays:
            if tr.get("type"):
                loaded.setdefault(
                    canon(filament_catalog.key(tr.get("filament_id"), tr.get("color"),
                                               tr.get("type"))), (tr, ext))

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
            "left_g": _left_grams(a, b, meta),
            # so the page can say the figure was pinned, and offer to unpin it
            "left_anchor_g": meta.get("left_anchor_g"),
            "left_anchor_at": meta.get("left_anchor_at"),
            "paid_per_kg": (round(b["cost"] / (b["grams"] / 1000.0), 2)
                            if b and b["grams"] else None),
            # the undiscounted price per kg this filament is costed at
            "list_per_kg": _ORDER_PRICES.get((a.get("filament_id") or "").upper()),
            # what it is actually costed at, and whether that came from a person
            "price_per_kg": meta.get("price_per_kg"),
            **a,
            "grams": round(a["grams"], 1),
            "cost": round(a["cost"], 4),
            "share": round(a["grams"] / total_g, 4) if total_g else 0,
            # naming comes from the AMS observation; the cloud detail never
            # carries the product line or the colour code
            "code": meta.get("code"),
            # a genuine spool needs no one to type "Bambu Lab"; derived rather
            # than stored, so a user edit still wins whenever there is one
            "vendor": meta.get("vendor") or ("Bambu Lab" if is_bambu else None),
            "product": meta.get("product"),
            "color_name": meta.get("color_name"),
            # a real SKU|HEX identity can always be named, even when only the
            # print history knows it; the synthetic purchase rows cannot
            "editable": True,
            "color": a.get("color") or meta.get("color"),
            "is_bambu": is_bambu,
            "first_seen": meta.get("first_seen"),
            # identities folded into this one, so a merge can be undone
            "merged": sorted(k for k, f in known.items() if f.get("alias_of") == fkey),
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
            "vendor": None, "editable": False,   # no identity row to write to
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
    # Likely duplicates: same colour and same material, one side named from an
    # RFID read and the other not. Suggested, never applied - two genuinely
    # different blacks look identical by this test, and merging them silently
    # would be worse than leaving two rows. Naming the stray also clears it.
    suggest = []
    named = [o for o in out if o.get("color") and (o.get("color_name") or o.get("product"))]
    for o in out:
        if o.get("color_name") or o.get("product") or not o.get("color"):
            continue
        for n in named:
            if n["fkey"] == o["fkey"] or n["color"] != o["color"]:
                continue
            if (n.get("type") or "").upper() != (o.get("type") or "").upper():
                continue
            suggest.append({
                "from": o["fkey"], "from_grams": o["grams"],
                "into": n["fkey"], "into_grams": n["grams"],
                "color": o["color"], "type": o.get("type"),
                "name": " ".join(x for x in (n.get("vendor"), n.get("product"),
                                             n.get("color_name")) if x),
            })
            break

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
        "order_prices": dict(_ORDER_PRICES) if PRICES_FROM_ORDERS() else {},
        "filaments": out,
        "suggestions": suggest,
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


def _ams_slot_map(state: dict) -> dict:
    """{slot: what the AMS actually held}, captured while the print ran.

    The cloud reports the slicer PROFILE per slot, not the spool: print a PLA
    Matte reel with a PLA Basic profile and the job says GFA00 while the tag says
    GFA01. Since the filament identity is built from that value, believing the
    cloud splits one spool into two. This snapshot is the printer's own answer,
    taken at the only moment it is true.
    """
    out = {}
    for unit in ((state.get("ams") or {}).get("units") or []):
        for t in (unit.get("trays") or []):
            if t.get("id") is None or not t.get("type"):
                continue
            out[str(int(t["id"]) + 1)] = {
                "sku": t.get("filament_id"), "color": filament_catalog.norm_color(t.get("color")),
                "type": t.get("type"), "code": t.get("code"),
                "bambu": bool(t.get("is_bambu")),
            }
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


def _filament_price_per_kg(entry: dict, bambu_map: dict | None = None,
                           fkey: str | None = None) -> tuple:
    """(price per kg, which rule matched) - most specific rule first.

    Bambu vs third-party is decided by the AMS RFID tag recorded while the print
    ran, NOT by the cloud: the cloud only knows the slicer's filament profile, so
    a third-party spool printed with a Bambu profile still reports e.g. GFA00.
    """
    # A price typed in for this exact filament wins outright. Everything below
    # is inference - a brand x material guess, or an assumption that the SKU on
    # an invoice is the spool that was in the tray - and inference should never
    # overrule someone who went and looked at the receipt.
    if fkey and fkey in _FIL_PRICES:
        return _FIL_PRICES[fkey], "set by hand"
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
    # Genuine or not, decided once. It comes from the RFID tag either way - from
    # the IDENTITY's reading first, then from this print's own snapshot. A print
    # whose filament was not recognised is precisely the one whose snapshot is
    # unreliable, and an identity is what a person merged it into. Both rules
    # below turn on this, and they must not disagree about the same spool.
    ident_sku, ident_bambu = _FIL_ID.get(fkey or "", (None, None))
    genuine = ident_bambu
    if genuine is None:
        genuine = bambu_map.get(slot) if bambu_map else None

    if PRICES_FROM_ORDERS():
        # Which SKU to price by: after a merge, the identity's own - two prints
        # of the same spool that were recorded under different SKUs have to end
        # up at the same price, which is the whole point of merging them.
        sku = (ident_sku or entry.get("filamentId") or "").upper()
        learned = _ORDER_PRICES.get(sku) if genuine else None
        if learned:
            return learned, f"order {sku}"
    # brand x material is more specific than material alone, so it comes first
    if genuine is not None:
        price, rule = _brand_price(genuine, entry.get("filamentType"))
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
    try:
        slot_map = json.loads(row.get("ams_slots") or "{}")
    except (TypeError, ValueError):
        slot_map = {}

    mapping = task.get("amsDetailMapping") or []
    detail, fil_cost = [], 0.0
    for e in mapping:
        grams = float(e.get("weight") or 0)
        slot = (e.get("slotId") or 0) + 1
        # The AMS snapshot exists to correct the SKU: the cloud reports the
        # slicer PROFILE, so one spool otherwise appears once per profile it was
        # printed with. The COLOUR from the cloud has always been right, so it
        # stays authoritative - and it doubles as a check on the snapshot. If the
        # two disagree about the colour, the slot numbering doesn't line up and
        # the snapshot would credit the wrong spool entirely, so it is dropped.
        cloud_color = filament_catalog.norm_color(e.get("targetColor"))
        snap = slot_map.get(str(slot)) or {}
        if snap.get("color") and cloud_color and snap["color"] != cloud_color:
            print(f"[cloud] slot {slot}: AMS had #{snap['color']}, job says "
                  f"#{cloud_color} - ignoring the snapshot for this line")
            snap = {}
        # resolved BEFORE pricing: a price set by hand is per filament identity,
        # so the identity has to exist before the price can be looked up
        fkey = _canon_fkey(filament_catalog.key(
            snap.get("sku") or e.get("filamentId"),
            cloud_color or snap.get("color"),
            e.get("filamentType") or snap.get("type")))
        per_kg, rule = _filament_price_per_kg(e, bambu_map, fkey)
        fil_cost += grams / 1000.0 * per_kg
        detail.append({
            "slot": slot,
            "type": e.get("filamentType") or snap.get("type"),
            "filament_id": snap.get("sku") or e.get("filamentId"),
            "code": snap.get("code"),
            # normalised, not sliced: '#RRGGBB' would come out as '#RRGGB' and
            # key a second, phantom identity for the same filament
            "color": cloud_color or snap.get("color"),
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

    # The live path refuses a model id identical to the previous job's, because
    # a repeat print and a leftover are indistinguishable from the printer. The
    # cloud settles it: the title is per-task and authoritative, and the same
    # title is the same model, so the id can be recovered from an earlier print
    # of it. A job whose title never matched anything simply keeps no link.
    title = task.get("designTitle") or None
    if title and not row.get("design_id"):
        found = store.design_id_for_title(title, job_id)
        if found:
            extra["design_id"], extra["profile_id"] = found
            print(f"[cloud] {job_id}: model id {found[0]} recovered from an "
                  f"earlier print of '{title}'")

    store.update_print_fields(
        job_id,
        **extra,
        design_title=title,
        filament_g=round(grams_total, 2) or None,
        filament_detail=json.dumps(detail) if detail else None,
        filament_cost=round(fil_cost, 4) if fil_cost else None,
    )
    return True


# --------------------------------------------------------------------------
# slicer metadata: what only the sliced file knows
# --------------------------------------------------------------------------
SLICER_CFG = CONFIG.section("slicer")
_slicer_kick = threading.Event()   # a print ended -> go and read its file
_slicer_last = {"at": 0.0, "ok": 0, "failed": 0, "error": None, "read_bytes": 0}


def _slicer_plate(gcode_file: str | None):
    """The plate number out of MQTT's `/data/Metadata/plate_15.gcode`.

    Only used to cross-check the file we picked. It is machine state, so it
    survives the job that set it - which is exactly why it is a check on a file
    chosen by name, and never the thing that chooses one.
    """
    m = re.search(r"plate_(\d+)", str(gcode_file or ""))
    return int(m.group(1)) if m else None


def _live_plate(job_id: str):
    """The plate number for a print, but only from live machine state and only
    while that state is still about this job.

    MQTT keeps reporting `gcode_file` after a job ends - machine state, not job
    state, the same trap as `print_error` and `design_id`. So it is used as a
    cross-check on the file we already picked by name, for the one print it can
    honestly speak for, and never for an older one.
    """
    job = (_state.get("job") or {})
    if job.get("task_id") and str(job["task_id"]) == str(job_id):
        return _slicer_plate(job.get("file"))
    return None


def _slicer_apply(row: dict) -> bool:
    """Read one print's sliced file and store what it says. False if it could
    not be read, which is an ordinary outcome and never raises."""
    job_id = row.get("job_id")
    subtask = row.get("name")
    if not (job_id and subtask):
        return False
    try:
        meta = gcode_meta.fetch(
            CFG.get("ip"), CFG.get("access_code"), subtask=subtask,
            plate=_live_plate(job_id),
            timeout=float(SLICER_CFG.get("timeout_sec", 20) or 20))
    except gcode_meta.SlicerError as e:
        _slicer_last["error"] = str(e)[:200]
        _slicer_tries[job_id] = _slicer_tries.get(job_id, 0) + 1
        return False

    fields = {
        "layer_h": meta.get("layer_h"),
        "nozzle_mm": meta.get("nozzle_mm"),
        "slicer_profile": (meta.get("profile") or "")[:120] or None,
        "est_min": meta.get("est_min"),
        "slice_json": json.dumps(meta, ensure_ascii=False),
    }
    # The slicer's weight and the cloud's have been identical wherever both
    # exist, so this fills the gap rather than competing: an install with no
    # Bambu account gets the figure, one with an account keeps what it has.
    if row.get("filament_g") is None and meta.get("grams"):
        fields["filament_g"] = meta["grams"]
    store.update_print_fields(job_id, **{k: v for k, v in fields.items()
                                         if v is not None})
    _slicer_last["read_bytes"] += int(meta.get("read_bytes") or 0)
    _slicer_last["error"] = None
    _slicer_tries.pop(job_id, None)
    return True


# job_id -> failed attempts. A file that is not on the drive is not going to
# appear later on its own, and retrying every print's file after every print is
# how a background job turns into a poll. Two goes, then leave it alone until
# the app restarts or somebody asks for that print by hand.
_slicer_tries: dict = {}
SLICER_MAX_TRIES = 2


def _slicer_pending(limit: int = 25) -> list[dict]:
    """Prints that have no slicer data yet, newest first.

    A running print is skipped: its file is on the drive already, but the row is
    still being rewritten by the MQTT loop every minute, and there is nothing to
    gain by racing it.
    """
    out = []
    for r in store.recent_prints(limit=200):
        if r.get("slice_json") or not r.get("ended_at"):
            continue
        if _slicer_tries.get(r.get("job_id"), 0) >= SLICER_MAX_TRIES:
            continue
        out.append(r)
        if len(out) >= limit:
            break
    return out


def slicer_worker():
    """Read the sliced file of each finished print, once.

    Not a poll. It waits for a print to end and then does one pass; the only
    repetition is a retry of prints it has not managed to read yet, and that
    stops as soon as they are read or the file is gone. A printer with no drive
    in it therefore says so once per print, not once per minute.
    """
    if SLICER_CFG.get("backfill", True):
        _slicer_kick.set()          # catch up on history at startup, once
    while True:
        _slicer_kick.wait()
        _slicer_kick.clear()
        # a finished print's file is on the drive already, but give the printer
        # a moment to settle before opening a connection to it
        time.sleep(5)
        if not SLICER_CFG.get("enabled"):
            continue
        rows = _slicer_pending()
        if not rows:
            continue
        ok = failed = 0
        for r in rows:
            if _slicer_apply(r):
                ok += 1
            else:
                failed += 1
                # one unreadable file is usually all of them (no drive, wrong
                # code, printer off) - do not hammer the printer to find out
                if failed >= 3 and ok == 0:
                    break
        _slicer_last.update(at=time.time(), ok=ok, failed=failed)
        # one pass reads at most a batch. If that batch went well and there is
        # still a backlog, go round again rather than waiting for the next print
        # to finish - otherwise switching this on with a long history reads 25
        # prints and then looks broken.
        if ok and _slicer_pending(limit=1):
            _slicer_kick.set()
        if ok:
            _cost_dirty()
            print(f"[slicer] read {ok} print(s) from the printer's drive"
                  + (f", {failed} could not be read" if failed else ""))
        elif failed:
            print(f"[slicer] {failed} print(s) could not be read: "
                  f"{_slicer_last['error']}")


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
    # same as the plug: enabled without an account is a reachable state now
    if not (CLOUD_CFG.get("token") or
            (CLOUD_CFG.get("email") and CLOUD_CFG.get("password"))):
        print("[cloud] enabled, but no account is set - fill it in under "
              "Settings > Cloud, or run tools/setup_cloud.py to sign in")
        return

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
    client.username_pw_set("bblp", CFG.get("access_code") or "")
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
        ip = CFG.get("ip")
        if not ip:
            print("[mqtt] no printer address set - Settings > Printer")
            time.sleep(30)
            continue
        client = _build_client()
        try:
            client.connect(ip, 8883, keepalive=60)
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
    """The dashboard, explicitly never cached.

    Deployment here is copying files over, so a browser holding on to a previous
    dashboard.html silently undoes the copy - and the page then looks unchanged
    no matter how many times it is fixed. One local page load is cheap; a stale
    one costs an afternoon.
    """
    resp = send_file(os.path.join(HERE, "dashboard.html"))
    resp.headers["Cache-Control"] = "no-store, must-revalidate"
    return resp


@app.route("/api/version")
def api_version():
    """When the served dashboard.html was last written - so 'is my copy live?'
    is a question with an answer."""
    try:
        ts = os.path.getmtime(os.path.join(HERE, "dashboard.html"))
    except OSError:
        ts = 0
    return jsonify({"dashboard": ts})


@app.route("/api/state")
def api_state():
    with _state_lock:
        return jsonify(dict(_state))


@app.route("/api/history")
def api_history():
    hours = float(request.args.get("hours", 6))
    return jsonify(store.history(hours=hours))


def _slice_block(row: dict):
    """The stored slicer metadata as a dict, or None.

    Kept as JSON in one column so a newly interesting field needs no migration;
    unpacked here so the page never has to parse it.
    """
    raw = row.get("slice_json")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


@app.route("/api/slicer")
def api_slicer_status():
    """What the slicer reader has been doing - for the Settings page."""
    return jsonify({
        "enabled": bool(SLICER_CFG.get("enabled")),
        "last": dict(_slicer_last),
        "pending": len(_slicer_pending(limit=200)) if SLICER_CFG.get("enabled") else 0,
        "gave_up": sum(1 for n in _slicer_tries.values() if n >= SLICER_MAX_TRIES),
    })


@app.route("/api/prints")
def api_prints():
    rows = store.recent_prints(limit=int(request.args.get("limit", 60)))
    for r in rows:
        r["slice"] = _slice_block(r)
        r.pop("slice_json", None)      # the page reads `slice`, not the raw JSON
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


NOTE_MAX = 20000        # generous for pasted links and notes, bounded all the same


IMAGE_MAX = 3 * 1024 * 1024      # the browser downscales first; this is the ceiling
IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


@app.route("/api/notes")
def api_notes():
    notes = store.all_notes()
    try:
        idx = store.note_image_index()
    except Exception:
        idx = {}
    for n in notes:
        n["images"] = idx.get(n["id"], [])
    return jsonify({"notes": notes})


@app.route("/api/notes", methods=["POST"])
def api_note_save():
    """Create a note, or update one when an id comes with it."""
    data = request.get_json(force=True, silent=True) or {}
    title = (data.get("title") or "").strip()[:200]
    body = (data.get("body") or "").strip()[:NOTE_MAX]
    # free text, like a print group: no category table to keep in step, and a
    # category stops existing when its last note leaves
    cat = (data.get("category") or "").strip()[:60] or None
    if not title and not body:
        return jsonify({"ok": False, "error": "an empty note is nothing"}), 400
    nid = _num_or_none(data.get("id"), int)
    try:
        if nid:
            if not store.update_note(nid, title or None, body or None, cat):
                return jsonify({"ok": False, "error": "no such note"}), 404
        else:
            nid = store.add_note(title or None, body or None, cat)
    except Exception as e:
        print(f"[notes] save failed: {e}")
        return jsonify({"ok": False, "error": str(e)[:300]}), 500
    return jsonify({"ok": True, "id": nid})


@app.route("/api/notes/delete", methods=["POST"])
def api_note_delete():
    data = request.get_json(force=True, silent=True) or {}
    nid = _num_or_none(data.get("id"), int)
    if nid is None:
        return jsonify({"ok": False, "error": "missing id"}), 400
    return jsonify({"ok": store.delete_note(nid), "id": nid})


@app.route("/api/notes/image", methods=["POST"])
def api_note_image_add():
    """Attach a picture to a note. Multipart: `file`, plus `note_id`."""
    nid = _num_or_none(request.form.get("note_id"), int)
    f = request.files.get("file")
    if not nid or f is None:
        return jsonify({"ok": False, "error": "need note_id and a file"}), 400
    mime = (f.mimetype or "").lower()
    if mime not in IMAGE_MIMES:
        return jsonify({"ok": False, "error": f"unsupported type {mime or '?'}"}), 400
    blob = f.read(IMAGE_MAX + 1)
    if not blob:
        return jsonify({"ok": False, "error": "empty file"}), 400
    if len(blob) > IMAGE_MAX:
        return jsonify({"ok": False, "error": "image too large"}), 413
    try:
        iid = store.add_note_image(nid, mime, blob,
                                   _num_or_none(request.form.get("w"), int) or 0,
                                   _num_or_none(request.form.get("h"), int) or 0)
    except Exception as e:
        print(f"[notes] image save failed: {e}")
        return jsonify({"ok": False, "error": str(e)[:300]}), 500
    return jsonify({"ok": True, "id": iid, "size": len(blob)})


@app.route("/api/notes/image/<int:iid>")
def api_note_image(iid: int):
    mime, blob = store.get_note_image(iid)
    if not blob:
        return jsonify({"ok": False, "error": "no such image"}), 404
    # the bytes for an id never change, so let the browser keep them
    return Response(blob, mimetype=mime or "application/octet-stream",
                    headers={"Cache-Control": "private, max-age=31536000, immutable"})


@app.route("/api/notes/image/delete", methods=["POST"])
def api_note_image_delete():
    data = request.get_json(force=True, silent=True) or {}
    iid = _num_or_none(data.get("id"), int)
    if iid is None:
        return jsonify({"ok": False, "error": "missing id"}), 400
    return jsonify({"ok": store.delete_note_image(iid), "id": iid})


@app.route("/api/filaments/identity", methods=["POST"])
def api_filament_identity():
    """Name a filament: vendor, product line, colour name.

    The only way a third-party spool gets a name at all - the printer reports a
    borrowed Bambu profile and a colour, never a manufacturer. Naming it is also
    what lets purchases match it, since wording is the sole route available
    without a colour code.
    """
    data = request.get_json(force=True, silent=True) or {}
    fkey = (data.get("fkey") or "").strip()
    if not fkey:
        return jsonify({"ok": False, "error": "missing fkey"}), 400
    fields = {k: ((data.get(k) or "").strip()[:64] or None)
              for k in ("vendor", "product", "color_name") if k in data}
    if not fields:
        return jsonify({"ok": False, "error": "nothing to set"}), 400
    # An identity is 'SKU|RRGGBB' (or a bare material and '?' when the printer
    # gave neither). Checking the shape keeps create-on-demand from minting junk
    # rows for anything an API caller happens to post.
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,24}\|([0-9A-Fa-f]{6}|\?)", fkey):
        return jsonify({"ok": False, "error": "not a filament identity"}), 400
    try:
        ok = store.set_filament_identity(fkey, **fields)
        if not ok:
            # Filament used up before the AMS was ever observed exists only in
            # the print history - and is exactly what most needs a name. Create
            # the identity row from the key itself: SKU|HEX.
            sku, _, hexc = fkey.partition("|")
            store.upsert_filament(
                fkey,
                filament_id=sku if sku.upper().startswith("GF") else None,
                type=None if sku.upper().startswith("GF") else sku,
                color=filament_catalog.norm_color(hexc))
            ok = store.set_filament_identity(fkey, **fields)
    except Exception as e:
        print(f"[filament] identity update failed: {e}")
        return jsonify({"ok": False, "error": str(e)[:300]}), 500
    if not ok:
        return jsonify({"ok": False, "error": "no such filament"}), 404
    # A Bambu spool's colour name is re-derived from the catalogue on every AMS
    # observation, so persist the user's wording as a learned name too -
    # otherwise the next frame would quietly undo the edit.
    if "color_name" in fields and fields["color_name"]:
        row = next((f for f in store.all_filaments() if f["fkey"] == fkey), None)
        if row and row.get("code"):
            _learn_color(row["code"], fields["color_name"])
    # the AMS tiles read these from the cache, so a rename has to reach it now
    # rather than at the next restart
    _rebuild_filament_meta()
    return jsonify({"ok": True, "fkey": fkey, **fields})


def _recost_filament(fkey: str) -> int:
    """Re-price every past print that used this filament, in place.

    A price is only useful if it applies to what you already printed - the
    figures on the Filament and History pages are stored per print, worked out
    when the cloud enriched the job, so a new price that only affected future
    prints would leave the totals disagreeing with the number just typed in.

    Reconstructs the pricing input from the stored per-slot detail rather than
    re-fetching the job: the fields the rules read - slot, SKU, material - are
    all in there, and the cloud may no longer have the task at all.
    """
    try:
        prints = store.all_prints()
    except Exception as e:
        print(f"[filament] could not re-cost {fkey}: {e}")
        return 0
    touched = 0
    for row in prints:
        try:
            entries = json.loads(row.get("filament_detail") or "[]") or []
        except (TypeError, ValueError):
            continue
        if not entries:
            continue
        try:
            bambu_map = json.loads(row.get("ams_bambu") or "{}")
        except (TypeError, ValueError):
            bambu_map = {}
        changed = False
        for e in entries:
            k = _canon_fkey(filament_catalog.key(e.get("filament_id"), e.get("color"),
                                                 e.get("type")))
            if k != fkey:
                continue
            per_kg, rule = _filament_price_per_kg(
                {"slotId": (e.get("slot") or 1) - 1, "filamentId": e.get("filament_id"),
                 "filamentType": e.get("type")}, bambu_map, fkey)
            cost = round(float(e.get("grams") or 0) / 1000.0 * per_kg, 4)
            # the rule too: two rules can arrive at the same figure, and a
            # label describing a rule that no longer applies is worse than no
            # label - it is what the print detail and the diagnostics explain
            # the price with
            if (e.get("per_kg") != per_kg or e.get("cost") != cost
                    or e.get("rule") != rule):
                e["per_kg"], e["rule"], e["cost"] = per_kg, rule, cost
                changed = True
        if not changed:
            continue
        total = round(sum(float(x.get("cost") or 0) for x in entries), 4)
        try:
            store.update_print_fields(row["job_id"],
                                      filament_detail=json.dumps(entries),
                                      filament_cost=total or None)
            touched += 1
        except Exception as e:
            print(f"[filament] re-cost of {row['job_id']} failed: {e}")
    return touched


@app.route("/api/filaments/price", methods=["POST"])
def api_filament_price():
    """Set (or clear) the price per kg of one filament, and re-cost its history.

    The configured brand x material matrix cannot know what a third-party spool
    cost, and the invoice importer only covers spools whose SKU appears on a
    Bambu invoice - which is exactly the filament this is for.
    """
    data = request.get_json(force=True, silent=True) or {}
    fkey = (data.get("fkey") or "").strip()
    if not fkey:
        return jsonify({"ok": False, "error": "missing fkey"}), 400
    raw = data.get("price_per_kg")
    if raw in (None, ""):
        per_kg = None                      # hand it back to the configured rules
    else:
        per_kg = _num_or_none(raw, float)
        if per_kg is None or per_kg < 0:
            return jsonify({"ok": False, "error": "not a price"}), 400
        if per_kg > 100000:
            return jsonify({"ok": False, "error": "that is not a price per kg"}), 400
        per_kg = round(per_kg, 4)
    # The identity shape is checked before anything is created, so an API caller
    # cannot mint junk rows: 'SKU|RRGGBB', or a bare material and '?' when the
    # printer gave neither.
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,24}\|([0-9A-Fa-f]{6}|\?)", fkey):
        return jsonify({"ok": False, "error": "not a filament identity"}), 400
    try:
        if not store.set_filament_price(fkey, per_kg):
            # A filament used up before the AMS ever saw it exists only in the
            # print history and has no row to hang a price on - which is exactly
            # the third-party spool this feature is for. Create it from the key,
            # the same way naming one does.
            sku, _, hexc = fkey.partition("|")
            store.upsert_filament(
                fkey,
                filament_id=sku if sku.upper().startswith("GF") else None,
                type=None if sku.upper().startswith("GF") else sku,
                color=filament_catalog.norm_color(hexc))
            if not store.set_filament_price(fkey, per_kg):
                return jsonify({"ok": False, "error": "no such filament"}), 404
        _rebuild_filament_meta()
        n = _recost_filament(_canon_fkey(fkey))
    except Exception as e:
        print(f"[filament] price update failed: {e}")
        return jsonify({"ok": False, "error": str(e)[:300]}), 500
    print(f"[filament] {fkey} -> {per_kg if per_kg is not None else '(config)'} /kg, "
          f"{n} print(s) re-costed")
    return jsonify({"ok": True, "fkey": fkey, "price_per_kg": per_kg, "recosted": n})


@app.route("/api/filaments/left", methods=["POST"])
def api_filament_left():
    """Pin how much of one filament is left, or unpin it.

    "Left" is bought minus used, and both halves can be wrong: deleting a failed
    print stops it counting as used, and a spool bought before the invoice
    importer existed was never counted as bought. The AMS, meanwhile, weighs an
    RFID spool and is simply right - so the useful thing is to copy that number
    across rather than to reconcile two logs that cannot be reconciled.

    Stored as an anchor, not an override: prints and purchases after this moment
    still move the figure, so it does not have to be typed in again after the
    next print.

        {"fkey": "...", "grams": 480}    pin it to a number
        {"fkey": "...", "from_ams": true} pin it to what the AMS reports now
        {"fkey": "...", "grams": null}   unpin, back to bought minus used
    """
    data = request.get_json(force=True, silent=True) or {}
    fkey = (data.get("fkey") or "").strip()
    if not fkey:
        return jsonify({"ok": False, "error": "missing fkey"}), 400
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,24}\|([0-9A-Fa-f]{6}|\?)", fkey):
        return jsonify({"ok": False, "error": "not a filament identity"}), 400

    canon = _canon_fkey(fkey)
    source = "typed"
    if data.get("from_ams"):
        grams = _ams_grams_left(canon)
        source = "ams"
        if grams is None:
            return jsonify({"ok": False, "error": "the AMS is not reporting a "
                            "remaining amount for this spool - it is not loaded, "
                            "or it has no RFID tag"}), 400
    else:
        raw = data.get("grams")
        if raw in (None, ""):
            grams = None                      # unpin
        else:
            grams = _num_or_none(raw, float)
            if grams is None or grams < 0:
                return jsonify({"ok": False, "error": "not an amount in grams"}), 400
            if grams > 100000:
                return jsonify({"ok": False, "error": "that is not an amount in grams"}), 400
            grams = round(grams, 1)

    at = time.time() if grams is not None else None
    try:
        if not store.set_filament_anchor(canon, grams, at):
            # a filament used up before the AMS ever saw it lives only in the
            # print history and has no row to hang this on - create it, the same
            # way naming or pricing one does
            sku, _, hexc = canon.partition("|")
            store.upsert_filament(
                canon,
                filament_id=sku if sku.upper().startswith("GF") else None,
                type=None if sku.upper().startswith("GF") else sku,
                color=filament_catalog.norm_color(hexc))
            if not store.set_filament_anchor(canon, grams, at):
                return jsonify({"ok": False, "error": "no such filament"}), 404
    except Exception as e:
        print(f"[filament] left-anchor update failed: {e}")
        return jsonify({"ok": False, "error": str(e)[:300]}), 500

    print(f"[filament] {canon} left -> "
          f"{'(computed again)' if grams is None else f'{grams} g from the {source}'}")
    return jsonify({"ok": True, "fkey": canon, "grams": grams, "at": at,
                    "source": None if grams is None else source})


def _ams_grams_left(fkey: str):
    """What the AMS says is left on this spool right now, in grams.

    None when the spool is not loaded, or has no tag: a third-party tray reports
    -1 and an external spool 0, and neither means empty.
    """
    with _state_lock:
        ams = json.loads(json.dumps(_state.get("ams") or {}))
    groups = [u.get("trays") or [] for u in (ams.get("units") or [])]
    groups.append(ams.get("external") or [])
    for trays in groups:
        for tr in trays:
            if not tr.get("type"):
                continue
            if _canon_fkey(filament_catalog.key(tr.get("filament_id"), tr.get("color"),
                                                tr.get("type"))) != fkey:
                continue
            pct, g = tr.get("remain_pct"), tr.get("grams_left")
            if g is not None and pct is not None and pct >= 0:
                return float(g)
    return None


@app.route("/api/filaments/delete", methods=["POST"])
def api_filament_delete():
    """Forget a filament identity.

    Only ever removes the *identity* - the name, vendor and colour. Grams live
    in the print rows, so an identity that has been printed with would simply
    reappear unnamed, which is why that case is refused and pointed at merge.
    """
    data = request.get_json(force=True, silent=True) or {}
    fkey = (data.get("fkey") or "").strip()
    if not fkey:
        return jsonify({"ok": False, "error": "missing fkey"}), 400
    grams = 0.0
    try:
        for r in store.all_prints():
            for e in _detail_entries(r):
                if filament_catalog.key(e.get("filament_id"), e.get("color"),
                                        e.get("type")) == fkey:
                    grams += e["grams"]
    except Exception:
        pass
    if grams > 0:
        return jsonify({"ok": False, "used": round(grams, 1), "error":
                        f"{grams:.0f} g was printed with this - merge it into the "
                        f"right filament instead, which keeps the grams"}), 409
    try:
        ok = store.delete_filament(fkey)
    except Exception as e:
        print(f"[filament] delete failed: {e}")
        return jsonify({"ok": False, "error": str(e)[:300]}), 500
    _rebuild_filament_meta()   # the name is gone from the AMS tiles too
    print(f"[filament] forgot identity {fkey}")
    return jsonify({"ok": ok, "fkey": fkey})


@app.route("/api/filaments/merge", methods=["POST"])
def api_filament_merge():
    """Fold one filament identity into another, or undo that with a blank
    `into`. Usage, cost and purchases follow; nothing is rewritten in the print
    rows, so unmerging puts everything back exactly as it was."""
    data = request.get_json(force=True, silent=True) or {}
    src = (data.get("from") or "").strip()
    dst = (data.get("into") or "").strip()
    shape = r"[A-Za-z0-9._-]{1,24}\|([0-9A-Fa-f]{6}|\?)"
    if not re.fullmatch(shape, src):
        return jsonify({"ok": False, "error": "not a filament identity"}), 400
    if dst and not re.fullmatch(shape, dst):
        return jsonify({"ok": False, "error": "not a filament identity"}), 400
    if dst == src:
        return jsonify({"ok": False, "error": "cannot merge into itself"}), 400
    try:
        known = {f["fkey"]: f for f in store.all_filaments()}
        # walking the chain first stops a -> b -> a, which would hide both rows
        hop, seen = dst, set()
        while hop and hop not in seen:
            seen.add(hop)
            if hop == src:
                return jsonify({"ok": False, "error": "that would make a loop"}), 400
            hop = (known.get(hop) or {}).get("alias_of")
        if src not in known:
            sku, _, hexc = src.partition("|")
            store.upsert_filament(
                src, filament_id=sku if sku.upper().startswith("GF") else None,
                type=None if sku.upper().startswith("GF") else sku,
                color=filament_catalog.norm_color(hexc))
        store.set_filament_alias(src, dst or None)
    except Exception as e:
        print(f"[filament] merge failed: {e}")
        return jsonify({"ok": False, "error": str(e)[:300]}), 500
    _rebuild_filament_meta()   # canon changed: a price follows the merge
    # Merging says "these are the same filament", so the prints recorded under
    # the folded identity have to be costed as that filament - otherwise two
    # halves of the same batch keep two different prices and the totals look
    # wrong in exactly the way the merge was meant to fix. Both ends are
    # re-costed, so undoing a merge puts the prices back too.
    recosted = _recost_filament(_canon_fkey(src))
    if dst:
        recosted += _recost_filament(_canon_fkey(dst))
    print(f"[filament] {src} -> {dst or '(unmerged)'}, {recosted} print(s) re-costed")
    return jsonify({"ok": True, "from": src, "into": dst or None, "recosted": recosted})


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


def _settings_payload() -> dict:
    """The Settings page's whole state: what each field is, what it holds, and
    where that value came from.

    A secret is never sent - only whether one is set. The page has no login, and
    a password that is merely masked in the browser has still been handed to
    anyone who can reach the page.
    """
    out = []
    for spec in settings_schema.SCHEMA:
        path = spec["path"]
        item = {k: v for k, v in spec.items() if k != "help"}
        item["help"] = spec.get("help")
        # "overridden" now means someone set it, as against the default the
        # code declares - there is no longer a file layer between the two
        item["overridden"] = CONFIG.overridden(path)
        item["default"] = None if spec["kind"] == "secret" else spec.get("default")
        if spec["kind"] == "secret":
            item["value"] = None
            item["is_set"] = bool(CONFIG.get(path))
        else:
            # falling back to the schema's default, not to blank: a setting
            # nobody has touched is still in force in the code, and a page that
            # showed it empty would be saying the opposite of what the app does
            item["value"] = CONFIG.get(path, spec.get("default"))
        out.append(item)
    return {"groups": settings_schema.GROUPS, "settings": out,
            "connection": bootstrap.redacted()}


@app.route("/api/backup")
def api_backup():
    """Download everything worth keeping, as one JSON file.

    ?secrets=1 includes the printer access code and account passwords, which is
    off by default: a backup ends up in places a password should not.
    ?images=0 leaves note pictures out, for a small file.
    """
    secrets = request.args.get("secrets") in ("1", "true", "yes")
    images = request.args.get("images") not in ("0", "false", "no")
    data = backup.export(store, include_secrets=secrets, include_images=images)
    name = f"bambu-monitor-{datetime.now().strftime('%Y%m%d-%H%M')}.json"
    body = json.dumps(data, indent=1, ensure_ascii=False, default=str)
    print(f"[backup] exported {sum(data['counts'].values())} row(s)"
          + (" INCLUDING credentials" if secrets else ""))
    return Response(body, mimetype="application/json", headers={
        "Content-Disposition": f'attachment; filename="{name}"'})


@app.route("/api/backup/restore", methods=["POST"])
def api_backup_restore():
    """Put a backup back.

    Takes the file as multipart `file`, or the JSON body itself. `mode=merge`
    (the default) only inserts what is missing; `mode=replace` empties each
    table first and says how many rows that would delete. `dry=1` reports what
    would happen and writes nothing - which is how the page shows a confirmation
    that is actually true rather than a guess.
    """
    f = request.files.get("file")
    if f is not None:
        try:
            data = json.loads(f.read().decode("utf-8", "replace"))
        except ValueError as e:
            return jsonify({"ok": False, "error": f"that file is not JSON: {e}"}), 400
        mode = request.form.get("mode", "merge")
        dry = request.form.get("dry") in ("1", "true", "yes")
    else:
        body = request.get_json(force=True, silent=True) or {}
        data = body.get("backup")
        mode = body.get("mode", "merge")
        dry = bool(body.get("dry"))
        if data is None:
            return jsonify({"ok": False, "error": "no backup in the request"}), 400

    why = backup.check(data)
    if why:
        return jsonify({"ok": False, "error": why}), 400
    try:
        report = backup.restore(store, data, mode=mode, dry_run=dry)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        print(f"[backup] restore failed: {e}")
        return jsonify({"ok": False, "error": str(e)[:300]}), 500

    if not dry:
        # everything downstream reads these at import: a restore has just
        # changed what they are
        CONFIG.reload()
        _rebuild_filament_meta()
        _cost_dirty()
        print(f"[backup] restored: {report['inserted']} inserted, "
              f"{report['skipped']} already there, {report['deleted']} replaced")
    return jsonify({"ok": True, "summary": backup.summarise(data), **report})


@app.route("/api/diag")
def api_diag():
    """What the app has actually been doing, in counts.

    Added because "the NAS is writing constantly" could not be answered from
    the outside: the write paths are all conditional, and the conditions are
    spread across a recording gate, a cloud poller and an MQTT callback.
    """
    up = max(1e-9, time.time() - _started_at)
    counts = store.stats()
    per_min = {k: round(v / up * 60, 1) for k, v in sorted(counts.items())}
    writes = {k: v for k, v in counts.items()
              if k.split()[0] in ("INSERT", "UPDATE", "DELETE", "REPLACE")}
    return jsonify({
        "uptime_sec": round(up, 1),
        "recording_mode": _rec_mode,
        "recording_active": _state.get("recording_active"),
        "mqtt_frames": _frames["n"],
        "mqtt_frames_per_min": round(_frames["n"] / up * 60, 1),
        "statements": counts,
        "statements_per_min": per_min,
        "writes_total": sum(writes.values()),
        "writes_per_min": round(sum(writes.values()) / up * 60, 1),
        "sample_interval_sec": SAMPLE_INTERVAL(),
        "backend": STORE_CFG.get("backend"),
    })


@app.route("/api/settings")
def api_settings():
    return jsonify(_settings_payload())


@app.route("/api/settings", methods=["POST"])
def api_settings_save():
    """Write one or more settings. Everything is validated against the schema
    before anything is stored, so a bad field cannot leave half a form applied."""
    data = request.get_json(force=True, silent=True) or {}
    changes = data.get("changes")
    if not isinstance(changes, dict) or not changes:
        return jsonify({"ok": False, "error": "nothing to change"}), 400

    # validate the whole batch first
    clean, clear = {}, []
    for path, value in changes.items():
        spec = settings_schema.BY_PATH.get(path)
        if spec is None:
            return jsonify({"ok": False, "error": f"{path} is not an editable setting"}), 400
        # a secret left blank means "leave it alone", never "set it to empty" -
        # the page cannot show the current one, so a blank box is not an edit
        if spec["kind"] == "secret" and (value is None or value == ""):
            continue
        if value is None:
            clear.append(path)          # explicit reset to the declared default
            continue
        try:
            clean[path] = settings_schema.coerce(path, value)
        except settings_schema.Invalid as e:
            return jsonify({"ok": False, "error": str(e), "path": path}), 400
    if not clean and not clear:
        return jsonify({"ok": False, "error": "nothing to change"}), 400

    try:
        for path, value in clean.items():
            CONFIG.set(path, value)
        for path in clear:
            CONFIG.clear(path)
    except Exception as e:
        print(f"[config] save failed: {e}")
        return jsonify({"ok": False, "error": str(e)[:300]}), 500

    # anything the pricing rules read is cached; a price change has to reach it
    if any(p.startswith("filament.") or p.startswith("cost.") for p in list(clean) + clear):
        _rebuild_order_prices()
        _rebuild_filament_meta()
    # switching the slicer reader on should do something now, not after the next
    # print finishes - otherwise the setting looks broken for hours
    if clean.get("slicer.enabled"):
        _slicer_tries.clear()
        _slicer_kick.set()
    touched = sorted(set(list(clean) + clear))
    restart = sorted(p for p in touched if not settings_schema.BY_PATH[p]["live"])
    for p in touched:
        shown = "(secret)" if p in settings_schema.SECRETS else CONFIG.get(p)
        print(f"[config] {p} = {shown}")
    return jsonify({"ok": True, "saved": touched, "restart_needed": restart})


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


# What a layer height can plausibly be, in mm. The floor is below anything an
# FDM printer will actually do; the ceiling exists to catch the one typo that
# matters - 200, meaning microns, typed into a field that counts millimetres.
LAYER_H_MIN, LAYER_H_MAX = 0.01, 3.0


def _parse_layer_h(raw):
    """'0.2', '0,2', '0.2 mm', '200um', '200 µm' -> millimetres.

    Blank returns None, which clears it. Anything unreadable or implausible
    raises, because a layer height nobody can check is worse than none at all.
    """
    s = str(raw if raw is not None else "").strip().lower().replace(",", ".")
    if not s:
        return None
    unit = ""
    for suffix in ("microns", "micron", "µm", "um", "mm"):
        if s.endswith(suffix):
            unit, s = suffix, s[:-len(suffix)].strip()
            break
    # deliberately stricter than float(): the page has to be able to say "that
    # is not a number" before it posts, and it can only do that if the two
    # agree on what a number is. float() would take '1e-1' and '+0.2'.
    if not re.fullmatch(r"\d*\.?\d+", s):
        raise ValueError(f"{raw!r} is not a number")
    v = float(s)
    if unit and unit != "mm":
        v /= 1000.0
    elif not unit and v >= 10:
        # nobody prints a 10 mm layer, so a bare 200 is microns and means 0.2
        v /= 1000.0
    if not (LAYER_H_MIN <= v <= LAYER_H_MAX):
        raise ValueError(f"{v:g} mm is not a plausible layer height")
    return round(v, 4)


@app.route("/api/prints/layerheight", methods=["POST"])
def api_print_layer_height():
    """The user's layer height for one print, in mm. Blank clears it.

    Written to layer_h_manual, never to layer_h: the slicer owns that one, and
    the two are kept apart so reading the sliced file again can refresh the
    automatic figure without quietly overwriting a correction. Clearing this
    falls back to whatever the slicer said, which is the behaviour people
    expect from an override.
    """
    data = request.get_json(force=True, silent=True) or {}
    job_id = (data.get("job_id") or "").strip()
    if not job_id:
        return jsonify({"ok": False, "error": "missing job_id"}), 400
    try:
        mm = _parse_layer_h(data.get("mm"))
    except (TypeError, ValueError):
        return jsonify({"ok": False,
                        "error": f"layer height must be between {LAYER_H_MIN} "
                                 f"and {LAYER_H_MAX} mm"}), 400
    if not store.get_print(job_id):
        return jsonify({"ok": False, "error": "no such print"}), 404
    # not update_print_fields()'s return value: clearing an already-empty
    # override touches no rows, and that is a success, not a failure
    store.update_print_fields(job_id, layer_h_manual=mm)
    row = store.get_print(job_id) or {}
    return jsonify({"ok": True, "job_id": job_id, "mm": mm,
                    # what the cell will now show, which is not always what was
                    # just sent: clearing falls back to the slicer's figure
                    "effective": mm if mm is not None else row.get("layer_h")})


@app.route("/api/prints/slicer", methods=["POST"])
def api_print_slicer():
    """Read one print's sliced file from the printer now.

    The worker does this on its own when a print ends. This is the button for
    the case where it could not - the drive was out, the printer was off - and
    for trying again after fixing whatever it was.
    """
    data = request.get_json(force=True, silent=True) or {}
    job_id = (data.get("job_id") or "").strip()
    row = store.get_print(job_id) if job_id else None
    if not row:
        return jsonify({"ok": False, "error": "no such print"}), 404
    if not SLICER_CFG.get("enabled"):
        return jsonify({"ok": False,
                        "error": "reading slicer data is switched off"}), 400
    _slicer_tries.pop(job_id, None)      # an explicit ask resets the give-up count
    if not _slicer_apply(row):
        return jsonify({"ok": False,
                        "error": _slicer_last.get("error") or "could not read it"}), 502
    _cost_dirty()
    fresh = store.get_print(job_id) or {}
    return jsonify({"ok": True, "job_id": job_id,
                    "slice": _slice_block(fresh)})


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


@app.route("/api/prints/finish", methods=["POST"])
def api_print_finish():
    """Close a print the app never saw end - typically the machine running this
    app lost power mid-job, so no end time was ever recorded.

    The duration has to come from the user: nothing local knows it, precisely
    because nothing was running. The cloud path in _apply_cloud_task remains the
    better route whenever the job is still in the cloud's recent task list.
    """
    data = request.get_json(force=True, silent=True) or {}
    job_id = (data.get("job_id") or "").strip()
    minutes = _num_or_none(data.get("minutes"))
    if not job_id:
        return jsonify({"ok": False, "error": "missing job_id"}), 400
    if not minutes or minutes <= 0:
        return jsonify({"ok": False, "error": "duration must be above zero"}), 400
    row = store.get_print(job_id)
    if not row:
        return jsonify({"ok": False, "error": "no such print"}), 404
    with _state_lock:
        live = dict(_state.get("job") or {})
    if str(live.get("task_id") or "") == job_id and live.get("state") in ACTIVE_STATES:
        return jsonify({"ok": False, "error": "print is still running"}), 409
    if not row.get("started_at"):
        return jsonify({"ok": False, "error": "print has no start time"}), 400
    fields = {"ended_at": float(row["started_at"]) + minutes * 60.0}
    # it only still says RUNNING because we stopped watching, not because it is
    if (row.get("final_state") or "") in ACTIVE_STATES:
        fields["final_state"] = "FINISH"
    ok = store.update_print_fields(job_id, **fields)
    return jsonify({"ok": ok, "job_id": job_id, "ended_at": fields["ended_at"]})


@app.route("/api/prints/error", methods=["POST"])
def api_print_error():
    """Clear (or set) the failure code on a print. Needed because the printer
    reports its last error as machine state, so before this was guarded a fresh
    job could inherit the previous one's code."""
    data = request.get_json(force=True, silent=True) or {}
    job_id = (data.get("job_id") or "").strip()
    code = (data.get("code") or "").strip()[:64]
    if not job_id:
        return jsonify({"ok": False, "error": "missing job_id"}), 400
    was = (store.get_print(job_id) or {}).get("error_code")
    ok = store.update_print_fields(job_id, error_code=code or None)
    if job_id == _print_row.get("job_id"):
        _print_row.setdefault("stored", {})["error_code"] = code or None
        # Suppress the code just rejected, not whatever the printer happens to
        # be showing - otherwise the next tick writes it straight back and the
        # clear looks broken.
        if not code:
            _print_row["stale_err"] = was or _error_code(dict(_state))
    return jsonify({"ok": ok, "job_id": job_id, "code": code or None})


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
    elif action in ("fan", "temp") and not CTRL_GCODE():
        return jsonify({"ok": False, "error":
                        "gcode commands are off - the firmware rejects them "
                        "(HMS 0500_0500_0001_0007). Set controls.allow_gcode "
                        "to try anyway."}), 403
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


# OFF by default, and it should stay off unless you are experimenting.
#
# Firmware 01.08.03.00beta/01.08.05.00 added MQTT command verification: the
# printer rejects commands it does not consider to come from a trusted client
# and raises HMS 0500_0500_0001_0007, "MQTT Command verification failed".
# ams_filament_setting is one of the gated commands - confirmed on an X2D, where
# assigning a slot produced that warning and changed nothing. The simple print
# controls (pause/resume/stop, print_speed, gcode_line, ledctrl) are not gated.
#
# Kept behind a switch rather than deleted, because it may behave differently in
# LAN-only mode or on later firmware. Nothing raises the warning while it is off.
def AMS_ASSIGN():
    return bool(FIL_CFG.get("allow_slot_assign", False))

# Same verification gate, confirmed the hard way on an X2D: sending M140/M141
# through `gcode_line` also raises HMS 0500_0500_0001_0007 and changes nothing.
# The fan sliders use the same command, so they go with it. Off by default;
# `controls.allow_gcode` re-enables the lot for testing on other firmware.
def CTRL_GCODE():
    return bool((CFG.get("controls") or {}).get("allow_gcode", False))

# Safe nozzle window per material, used when assigning a filament to a slot.
# Only materials listed here can be assigned without explicit temperatures -
# guessing a window for an unknown material is how you cook a nozzle.
_MATERIAL_TEMPS = {
    "PLA": (190, 230), "PETG": (230, 270), "PET": (230, 270),
    "ABS": (240, 280), "ASA": (240, 280), "TPU": (200, 240),
    "PVA": (190, 230), "PC": (260, 300), "PA": (250, 300),
}


@app.route("/api/ams/filament", methods=["POST"])
def api_ams_filament():
    """Tell the printer what is really loaded in an AMS slot.

    The AMS reader only understands Bambu's own RFID tags, so a third-party
    spool is simply whatever the tray was last told it is. This writes that
    assignment from a filament you have already named - which also stamps a
    distinct colour on the tray, the one thing the identity key needs to keep
    two different green spools apart.

    Strict allowlist, like every other command: slot must exist, material must
    be known, colour must be six hex digits, temperatures are clamped.
    """
    if not AMS_ASSIGN():
        return jsonify({"ok": False, "error":
                        "slot assignment is off - the firmware rejects this "
                        "command (HMS 0500_0500_0001_0007). Set "
                        "filament.allow_slot_assign to try anyway."}), 403
    data = request.get_json(force=True, silent=True) or {}
    try:
        slot = int(data.get("slot"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "slot must be a number"}), 400

    with _state_lock:
        ams = json.loads(json.dumps(_state.get("ams") or {}))
    units = ams.get("units") or []
    tray = unit_id = None
    for u in units:
        for tr in (u.get("trays") or []):
            if tr.get("id") is not None and tr["id"] + 1 == slot:
                tray, unit_id = tr, u.get("id", 0)
    if tray is None:
        return jsonify({"ok": False, "error": f"no AMS slot {slot}"}), 400

    # Values come from a filament you have named; explicit fields override.
    src = {}
    fkey = (data.get("fkey") or "").strip()
    if fkey:
        try:
            src = next((f for f in store.all_filaments() if f["fkey"] == fkey), {}) or {}
        except Exception:
            src = {}
        if not src:
            return jsonify({"ok": False, "error": "unknown filament"}), 404
    ftype = (data.get("type") or src.get("type") or "").strip().upper()
    color = filament_catalog.norm_color(data.get("color") or src.get("color"))
    profile = (data.get("filament_id") or src.get("filament_id") or "").strip().upper()
    if not ftype:
        return jsonify({"ok": False, "error": "missing material"}), 400
    if not color:
        return jsonify({"ok": False, "error": "colour must be six hex digits"}), 400

    base = ftype.split("-")[0]          # PLA-CF shares PLA's window
    lo_hi = _MATERIAL_TEMPS.get(ftype) or _MATERIAL_TEMPS.get(base)
    lo = _num_or_none(data.get("nozzle_temp_min"), int)
    hi = _num_or_none(data.get("nozzle_temp_max"), int)
    if lo is None or hi is None:
        if not lo_hi:
            return jsonify({"ok": False, "error":
                            f"unknown material {ftype} - send nozzle_temp_min/max"}), 400
        lo, hi = lo_hi
    lo, hi = max(150, min(320, lo)), max(150, min(320, hi))
    if lo >= hi:
        return jsonify({"ok": False, "error": "nozzle_temp_min must be below max"}), 400

    cmd = {"print": {
        "sequence_id": "0", "command": "ams_filament_setting",
        "ams_id": int(unit_id or 0), "tray_id": slot - 1,
        "tray_info_idx": profile,       # the slicer profile, e.g. GFA00
        "tray_color": color + "FF",     # the printer reports RRGGBBAA
        "tray_type": ftype,
        "nozzle_temp_min": lo, "nozzle_temp_max": hi,
        "setting_id": "",
    }}
    client = _mqtt_client
    if client is None:
        return jsonify({"ok": False, "error": "printer not connected"}), 409
    try:
        client.publish(REQUEST_TOPIC, json.dumps(cmd))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:120]}), 500
    # logged in full: this is the one command whose acceptance by the X2D has
    # not been confirmed, so the payload needs to be visible when it misbehaves
    print(f"[ams] slot {slot} <- {ftype} #{color} {profile or '(no profile)'} "
          f"{lo}-{hi}C :: {json.dumps(cmd['print'])}")
    return jsonify({"ok": True, "slot": slot, "type": ftype, "color": color,
                    "filament_id": profile or None,
                    "nozzle_temp_min": lo, "nozzle_temp_max": hi})


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
    """Generate go2rtc.yaml from the camera settings. The RTSPS access code is
    injected here from the stored settings so it never has to live in a second
    file. `#transport=udp` is required - the X2D's LIVE555 camera only feeds RTP
    over UDP, not TCP-interleaved (go2rtc's default)."""
    api_port = int(CAM_CFG.get("api_port", 1984))
    webrtc_port = int(CAM_CFG.get("webrtc_port", 8555))
    rtsp_port = int(CAM_CFG.get("rtsp_port", 322))
    src = CAM_CFG.get("src", "bambu")
    url = (f"rtsps://bblp:{CFG.get('access_code') or ''}@{CFG.get('ip') or ''}:{rtsp_port}"
           f"/streaming/live/1#transport=udp#backchannel=0")
    yaml = (
        "# AUTO-GENERATED by app.py from the stored settings - do not edit.\n"
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


# go2rtc is a separate process, not a thread. Killing app.py does not kill it:
# it is orphaned and keeps holding its ports, so the NEXT start finds :1984 and
# :8554 already taken and the relay never comes up - with nothing in the log but
# "address already in use" every five seconds, forever.
#
# Two halves are needed. A pidfile, so a start can clear up after a previous run
# that was killed or crashed; and a signal handler, so a clean stop does not
# leave one behind in the first place.
GO2RTC_PID = os.path.join(HERE, "go2rtc.pid")
_go2rtc_proc = None


def _reap_previous_go2rtc() -> bool:
    """Stop a go2rtc left behind by an earlier run. True if one was there."""
    try:
        with open(GO2RTC_PID, encoding="utf-8") as fh:
            pid = int((fh.read() or "0").strip())
    except (OSError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)                      # still alive?
    except OSError:
        os.path.exists(GO2RTC_PID) and os.unlink(GO2RTC_PID)
        return False
    print(f"[cam] a go2rtc from an earlier run is still holding its ports "
          f"(pid {pid}); stopping it")
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as e:
        print(f"[cam] could not stop pid {pid}: {e}")
        return False
    for _ in range(20):                      # up to 10s for it to let go
        time.sleep(0.5)
        try:
            os.kill(pid, 0)
        except OSError:
            break
    else:
        # SIGKILL on the NAS; Windows has no such signal, and os.kill there
        # terminates whatever it is given, so SIGTERM is the same thing twice
        try:
            os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
            print(f"[cam] pid {pid} ignored SIGTERM; killed")
        except OSError:
            pass
    os.path.exists(GO2RTC_PID) and os.unlink(GO2RTC_PID)
    return True


def _stop_go2rtc(*_a):
    """Take the relay down with us. Registered for a clean exit and for the
    SIGTERM that start.sh and `pkill` send."""
    proc = _go2rtc_proc
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    try:
        if os.path.exists(GO2RTC_PID):
            os.unlink(GO2RTC_PID)
    except OSError:
        pass


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
    _reap_previous_go2rtc()
    global _go2rtc_proc
    # A relay that dies immediately, over and over, is a misconfiguration rather
    # than a blip - back off instead of writing the same line to app.log twelve
    # times a minute for ever.
    wait, quick = 5, 0
    while True:
        try:
            print("[cam] starting go2rtc relay")
            started = time.time()
            _go2rtc_proc = subprocess.Popen([binpath, "-config", cfgpath], cwd=HERE)
            with open(GO2RTC_PID, "w", encoding="utf-8") as fh:
                fh.write(str(_go2rtc_proc.pid))
            _go2rtc_proc.wait()
            ran = time.time() - started
            quick = quick + 1 if ran < 10 else 0
            print(f"[cam] go2rtc exited ({_go2rtc_proc.returncode}) after "
                  f"{ran:.0f}s; restarting in {wait}s")
            if quick == 3:
                print("[cam] it keeps exiting at once. The usual cause is another "
                      "copy still holding the ports - check with "
                      "`ps | grep go2rtc` and `netstat -tlnp | grep 1984`.")
        except Exception as e:
            print(f"[cam] go2rtc error: {e}; retrying in {wait}s")
            quick += 1
        _go2rtc_proc = None
        time.sleep(wait)
        wait = min(wait * 2, 120) if quick else 5


def purge_worker():
    keep = float(CFG.get("storage", {}).get("retention_days", 30))
    while True:
        try:
            n = store.purge(keep_days=keep)
            if n:
                print(f"[store] purged {n} rows older than {keep} days")
        except Exception as e:
            print(f"[store] purge failed: {e}")
        time.sleep(86400)


if __name__ == "__main__":
    # so a stop takes the relay with it rather than orphaning it
    atexit.register(_stop_go2rtc)
    for _sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(_sig, lambda *_a: (_stop_go2rtc(), sys.exit(0)))
        except (ValueError, OSError):
            pass          # not the main thread, or a platform without it

    threading.Thread(target=mqtt_worker, daemon=True).start()
    threading.Thread(target=purge_worker, daemon=True).start()
    if PWR_CFG.get("enabled"):
        threading.Thread(target=power_worker, daemon=True).start()
    if CLOUD_CFG.get("enabled"):
        threading.Thread(target=cloud_worker, daemon=True).start()
    # always started: the setting is read live, so switching it on in Settings
    # takes effect without a restart. It sits on an Event and costs nothing.
    threading.Thread(target=slicer_worker, daemon=True).start()
    if CAM_CFG.get("enabled"):
        threading.Thread(target=go2rtc_worker, daemon=True).start()
    print(f"[web] http://localhost:{PORT}  (printer {CFG.get('ip') or 'not set'}, "
          f"model {CFG.get('model','?')}, storage={STORE_CFG.get('backend')})")
    app.run(host="0.0.0.0", port=PORT, threaded=True)
