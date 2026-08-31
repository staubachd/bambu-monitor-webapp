"""A spool moved from the external holder into a tray is in the tray.

The printer keeps reporting the last filament assigned to the external slot long
after that spool has been moved into an AMS bay. Same profile and same colour
means the same identity, so both entries resolve to one fkey - and with plain
assignment the stale external entry, which comes last in the scan, overwrote the
real tray. The spool then showed as "external" for ever, with no slot number,
and the AMS panel had a filament nothing was loaded with.

The fix is order plus setdefault: AMS trays are considered before the external
holder, and the first one to claim an identity keeps it.
"""
import sys, os, json, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import os as _os
SRC_DIR = (_os.environ.get("BAMBU_SRC")
           or _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import app

store = app.store
FKEY = "GFA00|1A2B3C"
now = time.time()

store.upsert_print(job_id="x-1", name="x-1", started_at=now - 3600, ended_at=now,
                   final_state="FINISH", total_layers=1)
store.update_print_fields("x-1", filament_detail=json.dumps([{
    "filament_id": "GFA00", "color": "1A2B3C", "type": "PLA",
    "grams": 90.0, "cost": 1.8, "slot": 1}]))


def tray(**kw):
    t = {"filament_id": "GFA00", "color": "1A2B3C", "type": "PLA"}
    t.update(kw)
    return t


def show(ams):
    with app._state_lock:
        app._state["ams"] = ams
    for r in app._filament_stats()["filaments"]:
        if r["fkey"] == FKEY:
            return r
    raise AssertionError("the filament vanished from the page")


# --- in a tray, and nothing in the external holder --------------------------
r = show({"units": [{"trays": [tray(id=0, remain_pct=80, grams_left=800)]}], "external": []})
assert r["loaded"] and r["slot"] == 1 and not r["external"], r
print("in tray 1:", f"slot={r['slot']} external={r['external']}")

# --- the case that was wrong: still listed externally AND in a tray ---------
both = {"units": [{"trays": [tray(id=2, remain_pct=80, grams_left=800)]}],
        "external": [tray()]}          # the printer's stale external assignment
r = show(both)
assert r["slot"] == 3, (
    f"slot={r['slot']} external={r['external']} - the stale external entry "
    f"overwrote the real tray, so a loaded spool shows nowhere in particular")
assert r["external"] is False, "it is in a bay, not on the external holder"
print("listed in both places at once -> the real tray wins:", f"slot={r['slot']}")

# --- genuinely on the external holder --------------------------------------
r = show({"units": [{"trays": []}], "external": [tray()]})
assert r["loaded"] and r["external"] is True, r
assert r["slot"] is None, f"an external spool was given bay number {r['slot']}"
print("genuinely external -> external=True, and no bay number")

# --- and not loaded at all --------------------------------------------------
r = show({"units": [{"trays": []}], "external": []})
assert not r["loaded"] and r["slot"] is None and not r["external"], r
assert r["remain_pct"] is None and r["grams_left"] is None, \
    "a spool that is not loaded still reports a remaining amount"
print("not loaded -> nothing is claimed about it")

# --- the ordering is what makes this work, so say so ------------------------
src = open(os.path.join(SRC_DIR, "app.py"), encoding="utf-8").read()
i = src.index("groups = [(u.get(\"trays\")")
window = src[i:i + 400]
assert "setdefault" in window, (
    "the scan assigns instead of setdefault, so whichever entry comes last wins")
assert window.index("external") > 0, "the external holder is no longer scanned"
assert src.index('groups.append((ams.get("external")') > i, (
    "the external holder is scanned BEFORE the trays, so its stale entry claims "
    "the identity first and the real tray cannot take it back")
print("trays are scanned before the external holder, and the first claim sticks")

store.delete_print("x-1")
print("ok")
