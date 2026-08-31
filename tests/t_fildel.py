"""Forgetting a filament identity - and refusing to when it would be a lie.

Deleting a filament only ever removes the IDENTITY: the vendor, product line and
colour name somebody typed. The grams live in the print rows and are not touched,
so an identity that has been printed with would simply reappear a moment later,
unnamed, with all its usage intact and its name gone.

That is not deletion, it is amnesia. So it is refused, with a 409 that points at
merge - which is the operation the person actually wanted.
"""
import sys, os, json, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import os as _os
SRC_DIR = (_os.environ.get("BAMBU_SRC")
           or _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import app

c = app.app.test_client()
store = app.store
USED, UNUSED = "GFA00|DD1100", "GFA00|DD2200"
now = time.time()

store.upsert_print(job_id="d-1", name="d-1", started_at=now - 3600, ended_at=now,
                   final_state="FINISH", total_layers=1)
store.update_print_fields("d-1", filament_detail=json.dumps([{
    "filament_id": "GFA00", "color": "DD1100", "type": "PLA",
    "grams": 275.0, "cost": 5.5, "slot": 1}]))
for k in (USED, UNUSED):
    store.upsert_filament(k, filament_id="GFA00", color=k.split("|")[1], type="PLA",
                          vendor="Bambu Lab", product="PLA Basic", color_name="Red")


def rows():
    return {r["fkey"]: r for r in app._filament_stats()["filaments"]}


# --- one that has never been printed with can just go ----------------------
assert UNUSED in {f["fkey"] for f in store.all_filaments()}
r = c.post("/api/filaments/delete", json={"fkey": UNUSED})
assert r.status_code == 200 and r.get_json()["ok"], r.get_json()
assert UNUSED not in {f["fkey"] for f in store.all_filaments()}, "the row survived"
print("an unused identity is forgotten")

# --- one that has, is refused, and says how much -----------------------------
r = c.post("/api/filaments/delete", json={"fkey": USED})
d = r.get_json()
assert r.status_code == 409, f"expected a refusal, got {r.status_code}: {d}"
assert d["used"] == 275.0, f"it does not say how much was printed: {d}"
assert "merge" in d["error"], f"it refuses without pointing anywhere useful: {d['error']}"
print("a used identity is refused:", d["error"][:70])

# and it really is still there, name and all
row = rows()[USED]
assert row["grams"] == 275.0, row["grams"]
assert row["product"] == "PLA Basic" and row["color_name"] == "Red", row
print(f"it is still there, {row['grams']} g and still named")

# --- the point of the refusal: deleting would not remove the usage ---------
# Prove the premise rather than asserting the message. The grams come from the
# print row, so removing the identity cannot remove them.
store.delete_filament(USED)                       # bypass the endpoint
back = rows()
assert USED in back, "the identity did not reappear - the premise has changed"
assert back[USED]["grams"] == 275.0, "the grams did not survive, so the refusal is wrong now"
assert not back[USED].get("product"), "the name came back too"
print("deleted behind the endpoint's back: the row returns unnamed, still 275 g")

# --- what may be deleted ----------------------------------------------------
assert c.post("/api/filaments/delete", json={}).status_code == 400
assert c.post("/api/filaments/delete", json={"fkey": "   "}).status_code == 400
# an identity nobody has ever heard of: nothing to do, and not an error
gone = c.post("/api/filaments/delete", json={"fkey": "GFZZ|999999"})
assert gone.status_code == 200 and gone.get_json()["ok"] is False, gone.get_json()
print("no fkey is a 400; an unknown one reports false rather than pretending")

store.delete_print("d-1")
print("ok")
