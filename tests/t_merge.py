"""Folding two filament identities into one, and undoing it.

The cloud reports the slicer PROFILE per slot, not what was in the tray, so one
physical spool routinely arrives under several identities. Merging says "these
are the same filament".

The invariant that matters: merging must not rewrite history. Nothing in the
print rows changes - only an `alias_of` pointer - so unmerging has to put
everything back exactly as it was, grams and cost included. Everything else here
is about refusing to build a structure that hides a row: a self-merge, or a
cycle where a -> b -> a leaves neither visible.
"""
import sys, os, json, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import os as _os
SRC_DIR = (_os.environ.get("BAMBU_SRC")
           or _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import app

c = app.app.test_client()
store = app.store
A, B = "GFA00|AA0000", "GFA00|AA0001"     # the same red, two profiles
now = time.time()


def a_print(job, fkey, grams):
    sku, _, hexc = fkey.partition("|")
    store.upsert_print(job_id=job, name=job, started_at=now - 3600, ended_at=now,
                       final_state="FINISH", total_layers=1)
    store.update_print_fields(job, filament_detail=json.dumps([{
        "filament_id": sku, "color": hexc, "type": "PLA",
        "grams": grams, "cost": grams / 1000.0 * 20, "slot": 1}]))


def rows():
    return {r["fkey"]: r for r in app._filament_stats()["filaments"]}


a_print("m-a", A, 300.0)
a_print("m-b", B, 120.0)
before = rows()
assert A in before and B in before, "the fixture did not produce two identities"
assert before[A]["grams"] == 300.0 and before[B]["grams"] == 120.0
print(f"two identities: {before[A]['grams']} g and {before[B]['grams']} g")

# --- merging ----------------------------------------------------------------
r = c.post("/api/filaments/merge", json={"from": B, "into": A}).get_json()
assert r["ok"], r
after = rows()
assert B not in after, "the folded identity is still shown as its own row"
assert after[A]["grams"] == 420.0, f"grams did not follow the merge: {after[A]['grams']}"
assert B in (after[A].get("merged") or []), \
    "the row does not say what was folded into it, so the merge cannot be undone"
print(f"merged: one row of {after[A]['grams']} g, listing {after[A]['merged']}")

# --- and unmerging puts it back exactly ------------------------------------
r = c.post("/api/filaments/merge", json={"from": B, "into": ""}).get_json()
assert r["ok"] and r["into"] is None, r
back = rows()
for k in (A, B):
    assert back[k]["grams"] == before[k]["grams"], (
        f"{k}: {back[k]['grams']} g after unmerging, {before[k]['grams']} g before - "
        f"merging rewrote the history it was only supposed to point at")
    assert round(back[k]["cost"], 4) == round(before[k]["cost"], 4), (
        f"{k}: cost changed across a merge and back")
print("unmerged: both rows back to exactly what they were")

# --- structures that would hide a row --------------------------------------
assert c.post("/api/filaments/merge", json={"from": A, "into": A}).status_code == 400
print("a self-merge is refused")

c.post("/api/filaments/merge", json={"from": B, "into": A})
loop = c.post("/api/filaments/merge", json={"from": A, "into": B})
assert loop.status_code == 400 and "loop" in loop.get_json()["error"], loop.get_json()
assert A in rows(), "the loop was allowed and took the surviving row with it"
print("a -> b -> a is refused:", loop.get_json()["error"])

# a longer cycle is the same mistake with more steps
C = "GFA00|AA0002"
a_print("m-c", C, 50.0)
c.post("/api/filaments/merge", json={"from": A, "into": C})   # b -> a -> c
three = c.post("/api/filaments/merge", json={"from": C, "into": B})
assert three.status_code == 400, "a three-hop cycle was allowed"
print("so is a longer cycle")

c.post("/api/filaments/merge", json={"from": A, "into": ""})
c.post("/api/filaments/merge", json={"from": B, "into": ""})

# --- what may be merged -----------------------------------------------------
for bad, why in [({"from": "../x", "into": A}, "a path"),
                 ({"from": A, "into": "not an identity"}, "free text"),
                 ({"into": A}, "no source"),
                 ({"from": "", "into": A}, "an empty source")]:
    rr = c.post("/api/filaments/merge", json=bad)
    assert rr.status_code == 400, f"{why} was accepted: {bad}"
print("refused: malformed identities, and a merge with no source")

# --- merging an identity that only the print history knows -----------------
HIST = "GFL01|BB2200"          # never seen in the AMS, no identity row
assert HIST not in {f["fkey"] for f in store.all_filaments()}
r = c.post("/api/filaments/merge", json={"from": HIST, "into": A}).get_json()
assert r["ok"], r
assert HIST in {f["fkey"] for f in store.all_filaments()}, \
    "merging a history-only identity did not create the row it needs to hang on"
c.post("/api/filaments/merge", json={"from": HIST, "into": ""})
print("an identity with no row of its own is created so it can be merged")

for job in ("m-a", "m-b", "m-c"):
    store.delete_print(job)
print("ok")
