"""Correcting how much filament is left.

"Left" is bought minus used, and both halves come from logs that can be
incomplete: deleting a failed print stops it counting as used, and a spool
bought before the invoice importer existed never counted as bought. The AMS
weighs an RFID spool and is simply right.

The correction is stored as an ANCHOR - "N grams left, as of then" - not as an
override of the number. That distinction is the whole point: an override is
wrong again after the next print, while an anchor keeps ageing forward. This is
what that has to mean.
"""
import sys, os, json, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app, filament_catalog

c = app.app.test_client()
store = app.store
FKEY = "GFA00|112233"
DAY = 86400.0
now = time.time()


def print_row(job, when, grams):
    store.upsert_print(job_id=job, name=job, started_at=when, ended_at=when + 600,
                       final_state="FINISH", total_layers=1)
    store.update_print_fields(job, filament_detail=json.dumps([{
        "filament_id": "GFA00", "color": "112233", "type": "PLA",
        "grams": grams, "cost": 0.0, "slot": 1}]))


def left_of(fkey=FKEY):
    rows = app._filament_stats()["filaments"]
    for r in rows:
        if r["fkey"] == fkey:
            return r
    raise AssertionError(f"{fkey} is not on the page; got {[r['fkey'] for r in rows]}")


# the identity has to exist with its colour code, or the purchase importer has
# nothing to match the order line against and Left stays unknown
store.upsert_filament(FKEY, filament_id="GFA00", code="A00-B11", color="112233",
                      type="PLA", is_bambu=1, product="PLA Basic", color_name="Blue")

# a spool bought and partly used, with one print in between
store.add_purchase(code="A00-B11", product="PLA Basic", color_name="Blue",
                   spools=1, grams_each=1000, total_price=24.99,
                   ordered_at=now - 30 * DAY, type="PLA")
print_row("old-1", now - 20 * DAY, 200.0)
print_row("old-2", now - 10 * DAY, 150.0)

row = left_of()
base = row["left_g"]
print(f"bought minus used = {base} g  (used {row['grams']} g)")

# --- deleting a print is exactly the problem this is for --------------------
store.delete_print("old-2")
after = left_of()["left_g"]
assert after > base, "deleting a print should have made Left look bigger"
print(f"after deleting a 150 g print -> {after} g, which is {after - base} g too high")

# --- pinning it ------------------------------------------------------------
r = c.post("/api/filaments/left", json={"fkey": FKEY, "grams": 480}).get_json()
assert r["ok"] and r["grams"] == 480.0 and r["at"], r
row = left_of()
assert row["left_g"] == 480.0, f"pinned to 480 but the page says {row['left_g']}"
assert row["left_anchor_g"] == 480.0 and row["left_anchor_at"], row
print("pinned to 480 g ->", row["left_g"], "g")

# --- and it must AGE, which an override would not --------------------------
print_row("new-1", time.time(), 60.0)      # genuinely after, not future-dated
row = left_of()
assert abs(row["left_g"] - 420.0) < 0.5, (
    f"a 60 g print after the anchor left it at {row['left_g']} - an anchor that "
    f"does not age is just a number that goes stale")
print("a 60 g print after it ->", row["left_g"], "g")

# a print BEFORE the anchor must not be subtracted twice: it is already
# accounted for in the number that was typed in
print_row("backdated", now - 25 * DAY, 999.0)
row = left_of()
assert abs(row["left_g"] - 420.0) < 0.5, (
    f"a print from before the anchor changed it to {row['left_g']} - it was "
    f"already included in what the user saw when they typed the number")
print("a print from before it changes nothing ->", row["left_g"], "g")

# a spool bought after the anchor adds
pid = store.add_purchase(code="A00-B11", product="PLA Basic", color_name="Blue",
                         spools=1, grams_each=1000, total_price=24.99,
                         ordered_at=time.time(), type="PLA")
row = left_of()
assert abs(row["left_g"] - 1420.0) < 0.5, (
    f"buying another kilo after the anchor left it at {row['left_g']}")
print("buying another 1000 g spool after it ->", row["left_g"], "g")

# Those two exist only to prove the anchor ages. Left in place they would also
# be "after" every LATER anchor in this file, which is correct behaviour and a
# confusing fixture.
store.delete_purchase(pid)
store.delete_print("new-1")
store.delete_print("backdated")

# --- unpinning hands it back to the arithmetic ------------------------------
r = c.post("/api/filaments/left", json={"fkey": FKEY, "grams": None}).get_json()
assert r["ok"] and r["grams"] is None, r
row = left_of()
assert row["left_anchor_g"] is None, row
assert row["left_g"] == round(row["bought_g"] - row["grams"], 1), (
    f"after unpinning: {row['left_g']} is not {row['bought_g']} - {row['grams']}")
print("unpinned -> back to bought minus used:", row["left_g"], "g")

# --- taking it from the AMS -------------------------------------------------
# not loaded: it must refuse rather than pin a made-up number
r = c.post("/api/filaments/left", json={"fkey": FKEY, "from_ams": True})
assert r.status_code == 400 and "AMS" in r.get_json()["error"], r.get_json()
assert left_of()["left_anchor_g"] is None, "a refused request still pinned something"
print("with the spool not loaded, 'from the AMS' is refused:",
      r.get_json()["error"][:60])

# now put it in a tray, with a tag that reports a remaining amount
with app._state_lock:
    app._state["ams"] = {"units": [{"trays": [{
        "id": 0, "filament_id": "GFA00", "color": "112233", "type": "PLA",
        "remain_pct": 37, "grams_left": 370, "is_bambu": True}]}], "external": []}
r = c.post("/api/filaments/left", json={"fkey": FKEY, "from_ams": True}).get_json()
assert r["ok"] and r["grams"] == 370.0 and r["source"] == "ams", r
assert left_of()["left_g"] == 370.0, left_of()["left_g"]
print("taken from the AMS ->", left_of()["left_g"], "g")

# a spool with no tag reports -1, which is "unknown", not "empty"
c.post("/api/filaments/left", json={"fkey": FKEY, "grams": None})
with app._state_lock:
    app._state["ams"]["units"][0]["trays"][0].update(remain_pct=-1, grams_left=None)
r = c.post("/api/filaments/left", json={"fkey": FKEY, "from_ams": True})
assert r.status_code == 400, "an untagged spool was pinned to something"
assert left_of()["left_anchor_g"] is None
print("an untagged spool (-1%) is refused, not read as empty")

# --- what may be written ----------------------------------------------------
for bad, why in [({"fkey": FKEY, "grams": -5}, "negative"),
                 ({"fkey": FKEY, "grams": "abc"}, "not a number"),
                 ({"fkey": FKEY, "grams": 999999}, "absurd"),
                 ({"fkey": "../etc", "grams": 1}, "not an identity"),
                 ({"grams": 1}, "no identity at all")]:
    rr = c.post("/api/filaments/left", json=bad)
    assert rr.status_code == 400, f"{why} was accepted: {bad}"
print("refused: negative, non-numeric, absurd, malformed identity, missing identity")

# --- a filament that only exists in the print history can still be pinned ---
HIST = "GFL99|ABCDEF"
print_row("hist-1", now - 5 * DAY, 42.0)
store.update_print_fields("hist-1", filament_detail=json.dumps([{
    "filament_id": "GFL99", "color": "ABCDEF", "type": "PLA",
    "grams": 42.0, "cost": 0.0, "slot": 1}]))
r = c.post("/api/filaments/left", json={"fkey": HIST, "grams": 800}).get_json()
assert r["ok"], r
assert left_of(HIST)["left_g"] == 800.0, left_of(HIST)
print("a filament with no identity row is created on demand and pinned")

print("ok")
