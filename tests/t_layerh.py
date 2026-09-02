"""Layer height, typed in by hand, per print.

Nothing on the network knows it. It is in the sliced file; MQTT never mentions
it and neither does the cloud API. So the only source is a person typing it,
which puts the whole weight on three things:

  * it is optional, and "not set" stays not set - a plausible-looking 0.2 that
    nobody typed is worse than an empty cell, because it reads as a fact
  * the MQTT loop, which upserts the running print every minute and has never
    heard of this column, must not blank it
  * a typo is refused rather than stored, and the page and the server agree on
    what a typo is - otherwise the field accepts something and then rejects it

The parser cases live in layerh_cases.json and are read by the JavaScript test
too, so the two implementations cannot drift apart.
"""
import sys, os, json, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import os as _os
SRC_DIR = (_os.environ.get("BAMBU_SRC")
           or _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import app, backup, storage

c = app.app.test_client()
store = app.store

with open(os.path.join(SRC_DIR, "tests", "layerh_cases.json"), encoding="utf-8") as fh:
    CASES = json.load(fh)


def post(job, mm):
    return c.post("/api/prints/layerheight", json={"job_id": job, "mm": mm})


def stored(job):
    """What the user typed. The slicer's own figure lives in layer_h and is
    deliberately a different column - see t_slicer.py for the split."""
    return (store.get_print(job) or {}).get("layer_h_manual")


# --- the column survived the migration --------------------------------------
# CREATE TABLE IF NOT EXISTS does nothing to a table that already exists, so a
# column added after the first release reaches an upgraded install only through
# LATE_COLUMNS. Forgetting that entry is invisible on a fresh database and
# breaks every INSERT on an old one.
assert "layer_h" in storage.LATE_COLUMNS["prints"], \
    "layer_h is not in LATE_COLUMNS - an existing install would never get the column"
assert "layer_h" in store.table_columns("prints"), "the column was not created"
assert "layer_h" in storage.PRINT_COLS, "the column is never selected or written"
print("layer_h exists, is migrated onto existing installs, and is read back")

# --- a print starts with no layer height, and nothing invents one -----------
store.upsert_print(job_id="lh-1", name="plate.3mf", started_at=time.time() - 1800,
                   ended_at=time.time(), final_state="FINISH", total_layers=150)
assert stored("lh-1") is None, "a fresh print already has a layer height"
row = [r for r in c.get("/api/prints").get_json()["prints"] if r["job_id"] == "lh-1"][0]
assert row["layer_h"] is None and row["layer_h_manual"] is None, "the API made one up"
print("a new print has no layer height, and nothing supplies a default")

# --- setting one, in every spelling the page offers -------------------------
for raw, want in CASES["accepts"]:
    r = post("lh-1", raw)
    assert r.status_code == 200 and r.get_json()["ok"], (raw, r.status_code, r.data)
    got = stored("lh-1")
    assert abs(got - want) < 1e-9, f"{raw!r} became {got} mm, expected {want}"
print(f"{len(CASES['accepts'])} spellings all land on the right millimetres, "
      f"microns and commas included")

# --- and a typo is refused, leaving what was there alone --------------------
post("lh-1", "0.2")
for raw, why in CASES["rejects"]:
    r = post("lh-1", raw)
    assert r.status_code == 400, f"{raw!r} ({why}) was accepted with {r.status_code}"
    assert stored("lh-1") == 0.2, (
        f"{raw!r} was refused but the stored value changed to {stored('lh-1')} - "
        f"a rejected edit must not touch what is already recorded")
print(f"{len(CASES['rejects'])} bad inputs refused, and none of them disturbed "
      f"the value already stored")

# --- blank clears it, and clearing twice is not an error --------------------
assert post("lh-1", "").get_json()["mm"] is None
assert stored("lh-1") is None, "blank did not clear the layer height"
r = post("lh-1", "")
assert r.status_code == 200 and r.get_json()["ok"], (
    "clearing an already-empty layer height reported failure - the UPDATE "
    "touches no rows, and that is a success")
print("blank clears it; clearing an empty one is still a success")

# --- the MQTT loop must not wipe it -----------------------------------------
# upsert_print runs about once a minute for the whole of a print and knows
# nothing about this column. If it is not immutable there, the value survives
# exactly until the next frame.
for _c in ("layer_h", "layer_h_manual"):
    assert _c in storage.PRINT_IMMUTABLE, f"upsert_print would overwrite {_c}"
post("lh-1", "0.16")
store.upsert_print(job_id="lh-1", name="plate.3mf", started_at=time.time() - 1800,
                   ended_at=time.time(), final_state="FINISH", total_layers=151,
                   energy_wh=210.0, cost=0.06, peak_w=150.0)
assert stored("lh-1") == 0.16, (
    f"the next MQTT upsert blanked the layer height (now {stored('lh-1')}) - "
    f"it would survive about sixty seconds on a running print")
assert store.get_print("lh-1")["total_layers"] == 151, \
    "the upsert stopped writing the columns it does own"
print("a minute of MQTT upserts leaves it alone, and still updates the rest")

# --- a print that does not exist is a 404, not a silent no-op ---------------
assert post("lh-nope", "0.2").status_code == 404, \
    "setting a layer height on a print that is not there reported success"
assert c.post("/api/prints/layerheight", json={"mm": "0.2"}).status_code == 400
print("an unknown job is a 404; a missing job_id is a 400")

# --- and it is in the backup -------------------------------------------------
# The whole point of typing it in is that it is the one thing no machine can
# tell you again afterwards.
data = backup.export(store)
assert [r for r in data["tables"]["prints"]
        if r["job_id"] == "lh-1"][0]["layer_h_manual"] == 0.16, \
    "the layer height is not in the backup - the one field nothing could recover"
store.clear_table("prints")
backup.restore(store, data, mode="merge")
assert stored("lh-1") == 0.16, "it did not come back from the backup"
print("it is exported and restored, like the rest of what was typed by hand")

# --- the page and the server read the same strings --------------------------
# Only checks that the shared table is actually reaching both sides; the
# JavaScript half runs the page's own parser against it.
assert len(CASES["accepts"]) > 10 and len(CASES["rejects"]) > 5, \
    "the shared parser table has been emptied out"
print("ok")
