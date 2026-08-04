#!/usr/bin/env python3
"""
Normalize a Bambu Lab X2D MQTT 'report' payload into a clean, stable state dict
that the dashboard can consume, insulating the UI from Bambu's messy raw schema.

The X2D emits the standard Bambu schema plus dual-nozzle arrays and a heated
chamber, which this module flattens into predictable fields.

Run standalone to self-test against a captured sample:
    python bambu_state.py sample_report.json
"""
from __future__ import annotations

# gcode_state -> friendly label
GCODE_STATE = {
    "IDLE": "Idle", "PREPARE": "Preparing", "RUNNING": "Printing",
    "PAUSE": "Paused", "FINISH": "Finished", "FAILED": "Failed",
    "SLICING": "Slicing",
}
SPEED_LEVEL = {1: "Silent", 2: "Standard", 3: "Sport", 4: "Ludicrous"}

# Physical nozzle mapping - confirmed by watching the actual machine print.
# The firmware numbers its extruders the OPPOSITE way round from how the X2D is
# labelled: firmware id 1 is the main direct-drive nozzle that prints the part,
# and id 0 is the auxiliary Bowden-fed nozzle used for supports/second material.
# So we present id 1 first, as "Nozzle 1". Change only this block if that flips.
NOZZLE_ROLES = {1: ("Nozzle 1", "direct drive"), 0: ("Nozzle 2", "Bowden")}
NOZZLE_DISPLAY_ORDER = (1, 0)

# stg_cur -> what the machine is doing right now. This is the standard Bambu
# stage enum; unknown ids fall back to "Stage N" rather than lying.
STAGES = {
    -1: "Idle", 0: "Printing", 1: "Auto bed levelling", 2: "Heatbed preheating",
    3: "Sweeping XY mech mode", 4: "Changing filament", 5: "M400 pause",
    6: "Paused: filament runout", 7: "Heating hotend", 8: "Calibrating extrusion",
    9: "Scanning bed surface", 10: "Inspecting first layer",
    11: "Identifying build plate", 12: "Calibrating micro lidar",
    13: "Homing toolhead", 14: "Cleaning nozzle tip", 15: "Checking extruder temp",
    16: "Paused by user", 17: "Paused: front cover falling",
    18: "Calibrating micro lidar", 19: "Calibrating extrusion flow",
    20: "Paused: nozzle temp malfunction", 21: "Paused: bed temp malfunction",
    22: "Filament unloading", 23: "Paused: skipped step", 24: "Filament loading",
    25: "Calibrating motor noise", 26: "Paused: AMS lost",
    27: "Paused: low heat-break fan speed", 28: "Paused: chamber temp error",
    29: "Cooling chamber", 30: "Paused by user gcode", 31: "Motor noise showoff",
    32: "Paused: nozzle covered in filament", 33: "Paused: cutter error",
    34: "Paused: first layer error", 35: "Paused: nozzle clog",
}


def _fan_pct(v):
    """Bambu reports fan speed on a 0-15 scale, not 0-100."""
    n = _num(v)
    if n is None:
        return None
    return max(0, min(100, round(n / 15 * 100)))


def _num(v, default=None):
    """Bambu sends numbers as strings all over the place; coerce safely."""
    if v is None or v == "":
        return default
    try:
        f = float(v)
        return int(f) if f.is_integer() else f
    except (TypeError, ValueError):
        return default


def _temp_pair(v):
    """Decode an X2D temperature field into (current, target).

    Once a target is set the firmware packs both into one int as
    (target << 16) | current  e.g. 14418140 == 0xDC00DC == 220 C now / 220 C target.
    With no target it is just the current temperature (idle captures read 24, 23),
    which is what proves current lives in the LOW half. Real temperatures never
    reach 0xFFFF, so that bit is an unambiguous "is packed" marker.
    """
    n = _num(v)
    if n is None:
        return None, None
    try:
        iv = int(n)
    except (TypeError, ValueError):
        return n, 0
    if iv > 0xFFFF:
        return iv & 0xFFFF, iv >> 16
    return iv, 0


def _err_code(v):
    """Printer error fields are 0 when healthy; render the rest as hex, which is
    the form Bambu's error-code lookup uses."""
    n = _num(v)
    if n in (None, 0):
        return None
    try:
        return "0x%08X" % int(n)
    except (TypeError, ValueError):
        return str(v)


def _err_str(v):
    return None if v in (None, "", "0") else str(v)


def _hex6(color: str | None) -> str | None:
    """'F72323FF' (RGBA) -> '#F72323' for CSS; drop alpha. Empty -> None."""
    if not color or set(color) <= {"0"}:
        return None
    return "#" + color[:6].upper()


def _hms_code(entry: dict) -> str:
    """Build the canonical HMS_XXXX_XXXX_XXXX_XXXX code used by Bambu's wiki lookup."""
    attr = int(entry.get("attr", 0))
    code = int(entry.get("code", 0))
    return "%04X_%04X_%04X_%04X" % (
        (attr >> 16) & 0xFFFF, attr & 0xFFFF,
        (code >> 16) & 0xFFFF, code & 0xFFFF,
    )


def _trays(ams_unit: dict) -> list[dict]:
    out = []
    for t in ams_unit.get("tray", []):
        color = t.get("tray_color")
        # remaining % is all the printer gives; combine it with the spool's
        # nominal weight/length so it can be shown in grams and metres
        rem = _num(t.get("remain"))
        weight = _num(t.get("tray_weight"), 0) or 0
        length = _num(t.get("total_len"), 0) or 0
        known = rem is not None and rem >= 0
        out.append({
            "spool_weight_g": weight or None,
            "grams_left": round(rem / 100 * weight) if known and weight else None,
            "metres_left": round(rem / 100 * length / 1000, 1) if known and length else None,
            "id": _num(t.get("id")),
            "type": t.get("tray_type") or None,
            "brand": t.get("tray_sub_brands") or None,
            # Spool identity, used to name the colour and build a reorder link.
            # Both are also populated for third-party spools sliced with a Bambu
            # profile, so they are only trustworthy together with is_bambu.
            "filament_id": t.get("tray_info_idx") or None,   # SKU, e.g. GFA00
            "code": t.get("tray_id_name") or None,           # colour code, e.g. A00-W01
            "color": _hex6(color),
            "remain_pct": _num(t.get("remain")),  # -1 = unknown (non-Bambu spool)
            # Genuine Bambu spools carry an RFID tag; third-party ones report an
            # all-zero uid. The cloud can't tell them apart (it only sees the
            # slicer's filament profile), so this is the authoritative signal.
            "is_bambu": bool(t.get("tag_uid")) and set(str(t.get("tag_uid"))) != {"0"},
            "nozzle_min": _num(t.get("nozzle_temp_min")),
            "nozzle_max": _num(t.get("nozzle_temp_max")),
        })
    return out


def parse_report(raw: dict) -> dict:
    """raw = full JSON from device/<serial>/report. Returns normalized state."""
    p = raw.get("print", raw)  # some payloads nest under 'print'
    dev = p.get("device", {})

    # --- dual nozzle: per-extruder current + target temperature ---
    ext_info = (dev.get("extruder", {}) or {}).get("info", []) or []
    noz_info = (dev.get("nozzle", {}) or {}).get("info", []) or []
    by_id = {}
    for i, ext in enumerate(ext_info):
        meta = noz_info[i] if i < len(noz_info) else {}
        cur, tgt = _temp_pair(ext.get("temp"))
        fid = _num(ext.get("id"), i)
        by_id[fid] = {
            "id": fid,  # firmware extruder id - storage columns are keyed to this
            "temp": cur,
            "target": tgt,
            "type": meta.get("type") or p.get("nozzle_type"),
            "diameter": _num(meta.get("diameter")),
            "wear": _num(meta.get("wear")),
            # "heated" is the reliable in-use signal: nozzle.tar_id stays 0 even
            # when the *second* nozzle is the one printing, so it can't be used.
            "active": bool(tgt),
        }
    # emit in physical order (main nozzle first), labelled as the machine is
    nozzles = []
    for fid in NOZZLE_DISPLAY_ORDER:
        n = by_id.pop(fid, None)
        if n:
            n["label"], n["role"] = NOZZLE_ROLES[fid]
            nozzles.append(n)
    for fid, n in sorted(by_id.items()):  # any unexpected extras keep firmware order
        n.setdefault("label", f"Nozzle {fid + 1}")
        n.setdefault("role", "")
        nozzles.append(n)
    # kept for compatibility; refers to whichever nozzle the firmware reports at top level
    active_target = _num(p.get("nozzle_target_temper"), 0)

    # --- chamber (heated, X2D) - can be packed the same way once it has a target ---
    chamber_cur, chamber_tgt = _temp_pair((dev.get("ctc", {}).get("info", {}) or {}).get("temp"))

    # --- AMS ---
    ams_units = []
    for unit in (p.get("ams", {}).get("ams", []) or []):
        dry = unit.get("dry_setting", {}) or {}
        ams_units.append({
            "id": _num(unit.get("id")),
            "humidity_pct": _num(unit.get("humidity_raw")),
            "temp": _num(unit.get("temp")),
            "trays": _trays(unit),
            "drying": {
                "active": bool(_num(unit.get("dry_time"), 0)),
                "minutes_left": _num(unit.get("dry_time"), 0),
                "target_temp": _num(dry.get("dry_temperature")),
                "duration_h": _num(dry.get("dry_duration")),
            },
        })
    ams_root = p.get("ams", {}) or {}

    def _tray_no(key):
        v = _num(ams_root.get(key))
        return None if v in (None, 255) else v

    active_tray = _tray_no("tray_now")
    tray_target = _tray_no("tray_tar")
    tray_prev = _tray_no("tray_pre")
    # tray_reading_bits is a hex mask of trays whose RFID is being read
    reading = str(ams_root.get("tray_reading_bits") or "0").strip("0") != ""
    changing = tray_target is not None and tray_target != active_tray

    # --- lights ---
    lights = {l.get("node"): l.get("mode") for l in p.get("lights_report", [])}

    # --- health / errors ---
    hms = [{"code": _hms_code(h), "raw_code": h.get("code"), "ts": h.get("ts_unix")}
           for h in p.get("hms", [])]

    # --- external (non-AMS) spool slots ---
    ext_spools = [t for t in _trays({"tray": p.get("vir_slot", [])}) if t.get("type")]

    # --- machine internals ---
    cam = (dev.get("cam", {}) or {})
    free_kb = _num(cam.get("tl_internal_free_kb"))
    total_kb = _num(cam.get("tl_internal_total_kb"))
    upg = p.get("upgrade_state", {}) or {}
    vent = ((p.get("3D", {}) or {}).get("ventobox", {}) or {})
    xcam = p.get("xcam", {}) or {}

    stage_id = _num(p.get("stg_cur"), -1)
    state = p.get("gcode_state", "")
    # MakerWorld reference: design_id maps to makerworld.com/models/<id>.
    # Self-sliced / local prints report "0" (or empty) — treat those as none.
    design_id = str(p.get("design_id") or "").strip()
    profile_id = str(p.get("profile_id") or "").strip()
    return {
        "printer": {
            "serial": raw.get("sn") or (p.get("upgrade_state", {}) or {}).get("sn"),
            "firmware": p.get("ver"),
            "wifi_dbm": _num((p.get("wifi_signal") or "").replace("dBm", "")),
            "print_type": p.get("print_type"),
        },
        "job": {
            "name": p.get("subtask_name") or None,
            "state": state,
            "state_label": GCODE_STATE.get(state, state.title() if state else "Unknown"),
            "percent": _num(p.get("mc_percent", p.get("percent")), 0),
            "layer": _num(p.get("layer_num")),
            "total_layers": _num(p.get("total_layer_num")),
            "remaining_min": _num(p.get("mc_remaining_time", p.get("remain_time"))),
            "stage_id": stage_id,
            "stage": STAGES.get(stage_id, f"Stage {stage_id}"),
            "file": p.get("gcode_file") or None,
            "task_id": p.get("subtask_id") or p.get("task_id"),
            "design_id": design_id if design_id and design_id != "0" else None,
            "profile_id": profile_id or None,
        },
        "temps": {
            "bed": {"cur": _num(p.get("bed_temper")), "target": _num(p.get("bed_target_temper"))},
            "chamber": {"cur": chamber_cur, "target": chamber_tgt},
            "nozzles": nozzles,
            "active_nozzle_target": active_target,
        },
        "speed": {
            "level": SPEED_LEVEL.get(_num(p.get("spd_lvl")), "?"),
            "magnitude_pct": _num(p.get("spd_mag")),
        },
        # all four fans, converted from Bambu's 0-15 scale to a percentage
        "fans": {
            "cooling": _fan_pct(p.get("cooling_fan_speed")),
            "aux1": _fan_pct(p.get("big_fan1_speed")),
            "aux2": _fan_pct(p.get("big_fan2_speed")),
            "heatbreak": _fan_pct(p.get("heatbreak_fan_speed")),
        },
        "lights": lights,
        "ams": {
            "units": ams_units, "active_tray": active_tray,
            "external": ext_spools,
            "tray_target": tray_target, "tray_prev": tray_prev,
            "changing": changing, "reading": reading,
            "status_raw": _num(p.get("ams_status")),
            # only the reliable signals get a label; ams_status is an
            # undocumented state machine, so it is passed through raw
            "activity": ("changing filament" if changing else
                         "reading RFID" if reading else "idle"),
        },
        "job_mapping": p.get("mapping") or [],
        "errors": {
            "print_error": _err_code(p.get("print_error")),
            "mc_code": _err_str(p.get("mc_print_error_code")),
            "fail_reason": _err_str(p.get("fail_reason")),
            "err": _err_str(p.get("err")),
        },
        "hms": hms,
        "machine": {
            "airduct_mode": _num((dev.get("airduct", {}) or {}).get("modeCur")),
            "vent_enabled": bool(vent.get("enable")),
            "vent_speed": _num(vent.get("speed")),
            "storage_free_kb": free_kb,
            "storage_total_kb": total_kb,
            "storage_used_pct": (round((total_kb - free_kb) / total_kb * 100)
                                 if free_kb is not None and total_kb else None),
            "firmware_update": upg.get("ota_new_version_number") or None,
            "firmware_update_ams": upg.get("ams_new_version_number") or None,
            "firmware_update_ext": upg.get("ext_new_version_number") or None,
            "upgrade_status": upg.get("status"),
            "plate_id": (dev.get("plate", {}) or {}).get("cur_id") or None,
            "plate_base": _num((dev.get("plate", {}) or {}).get("base")),
        },
        "ai": {
            "spaghetti": bool(xcam.get("spaghetti_detector")),
            "first_layer": bool(xcam.get("first_layer_inspector")),
            "buildplate_marker": bool(xcam.get("buildplate_marker_detector")),
            "monitoring": bool(xcam.get("printing_monitor")),
            "auto_halt": bool(xcam.get("print_halt")),
            "sensitivity": xcam.get("halt_print_sensitivity"),
        },
        "camera": {
            "resolution": p.get("ipcam", {}).get("resolution"),
            "rtsp_enabled": p.get("ipcam", {}).get("rtsp_url") not in (None, "disable"),
        },
    }


if __name__ == "__main__":
    import json
    import sys

    path = sys.argv[1] if len(sys.argv) > 1 else "samples/sample_report.json"
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)

    s = parse_report(raw)
    print(json.dumps(s, indent=2, ensure_ascii=False))

    # sanity checks against the known sample
    j = s["job"]
    print("\n--- self-check ---")
    print(f"state:        {j['state_label']}  ({j['percent']}%)")
    print(f"nozzles:      {[ (n['id'], n['temp'], 'ACTIVE' if n['active'] else '') for n in s['temps']['nozzles'] ]}")
    print(f"chamber:      {s['temps']['chamber']['cur']} C")
    print(f"ams trays:    {[ (t['type'], t['color'], t['remain_pct']) for t in s['ams']['units'][0]['trays'] ]}")
    print(f"hms warnings: {[ h['code'] for h in s['hms'] ]}")
    assert len(s['temps']['nozzles']) == 2, "X2D should have 2 nozzles"
    print("OK: dual-nozzle parse verified")
