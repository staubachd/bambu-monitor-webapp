"""A print whose own RFID snapshot says "not genuine" for a spool the identity
knows IS genuine must still get the invoice price.

Taken from real data: four prints of one model on one Bambu Jade White spool.
Two were recorded under GFA01 with ams_bambu true; two under GFA00 with
ams_bambu FALSE - the snapshot that failed is the reason the filament was not
recognised in the first place. The second pair was costed by the brand x
material fallback at 14.99/kg instead of the 25.99/kg the invoices teach, and a
merge could not fix it while the print's own snapshot outranked the identity.
"""
import sys, os, json, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app

c = app.app.test_client()
for f in app.store.all_filaments():
    app.store.delete_filament(f["fkey"])
for p in app.store.all_prints():
    app.store.delete_print(p["job_id"])

W = "FFFFFF"
GOOD, FOLDED = f"GFA00|{W}", f"GFA01|{W}"
app._ORDER_PRICES["GFA00"] = 25.99
app.store.upsert_filament(GOOD, filament_id="GFA00", type="PLA", color=W,
                          code="A00-W01", color_name="Jade White", is_bambu=1)
app.store.upsert_filament(FOLDED, filament_id="GFA01", type="PLA", color=W, is_bambu=1)
app.store.set_filament_alias(FOLDED, GOOD)
app._rebuild_filament_meta()

def seed(job, sku, grams, per_kg, rule, snapshot):
    app.store.upsert_print(job_id=job, name="Kuerbis Windlicht", started_at=time.time()-3600,
                           ended_at=time.time(), final_state="FINISH", total_layers=200,
                           energy_wh=300, cost=0.09, peak_w=95)
    app.store.update_print_fields(job,
        ams_bambu=json.dumps({"1": snapshot}),
        filament_g=grams, filament_cost=round(grams/1000*per_kg, 4),
        filament_detail=json.dumps([{"slot": 1, "type": "PLA", "filament_id": sku,
            "code": None, "color": W, "brand": "Bambu", "grams": grams,
            "per_kg": per_kg, "rule": rule, "cost": round(grams/1000*per_kg, 4)}]))

seed("ok1", "GFA01", 112.27, 25.99, "order GFA00", True)
seed("ok2", "GFA01", 112.27, 25.99, "order GFA00", True)
seed("bad1", "GFA00", 104.51, 14.99, "other PLA", False)   # the snapshot that lied
seed("bad2", "GFA00", 104.42, 14.99, "other PLA", False)

rate = lambda j: json.loads(app.store.get_print(j)["filament_detail"])[0]["per_kg"]
print("before:", {j: rate(j) for j in ("ok1", "ok2", "bad1", "bad2")})
assert rate("bad1") == 14.99

# the identity's verdict is what decides now, so the rules already disagree with
# what is stored - which is what the diagnostic flags
per_kg, rule = app._filament_price_per_kg(
    {"slotId": 0, "filamentId": "GFA00", "filamentType": "PLA"},
    {"1": False}, GOOD)
print("rules now say:", per_kg, rule)
assert (per_kg, rule) == (25.99, "order GFA00"), (per_kg, rule)

# re-doing the merge is what applies it to the stored costs
r = c.post("/api/filaments/merge", json={"from": FOLDED, "into": GOOD})
d = r.get_json()
print("re-merge ->", d)
after = {j: rate(j) for j in ("ok1", "ok2", "bad1", "bad2")}
print("after :", after)
assert set(after.values()) == {25.99}, after
cost = lambda j: app.store.get_print(j)["filament_cost"]
print("costs :", {j: cost(j) for j in ("ok1", "ok2", "bad1", "bad2")})
assert abs(cost("bad1") - round(104.51/1000*25.99, 4)) < 1e-6

# the grams genuinely differ between the two pairs, so the totals still differ -
# that is the print using less filament, not a pricing fault
assert cost("ok1") != cost("bad1"), "the fixture lost the real weight difference"
print("same rate, different totals - because the grams differ")

# and the protection still holds: a third-party spool whose identity says so is
# refused the Bambu price even when the print's snapshot claims otherwise
app.store.upsert_filament("GFA00|7C4B00", filament_id="GFA00", type="PLA",
                          color="7C4B00", is_bambu=0)
app._rebuild_filament_meta()
per_kg, rule = app._filament_price_per_kg(
    {"slotId": 0, "filamentId": "GFA00", "filamentType": "PLA"},
    {"1": True}, "GFA00|7C4B00")
print("third-party:", per_kg, rule)
assert rule != "order GFA00", "a third-party identity was given the Bambu price"
# ...and the brand x material rule has to agree with that verdict, or the two
# rules contradict each other about the same spool
assert not str(rule).startswith("bambu"),     f"the brand rule priced a third-party identity as Bambu: {rule}"
print("ok")
