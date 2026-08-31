"""Four identical prints of one spool must end up at one price after a merge.

Two of them were recorded under an identity the app did not recognise, so they
were costed by the fallback rule while the other two got the price learned from
the invoices. Merging said "these are the same filament" - but the per-print
costs are STORED, so until now the merge fixed the Filament page and left half
the batch priced differently on History.
"""
import sys, os, json, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app

c = app.app.test_client()
for f in app.store.all_filaments():
    app.store.delete_filament(f["fkey"])
for p in app.store.all_prints():
    app.store.delete_print(p["job_id"])

WHITE = "F4F4F4"
GOOD = f"GFA00|{WHITE}"          # Bambu Jade White, as the AMS read it
STRAY = f"GFA07|{WHITE}"         # the same spool, recorded under another profile

# the invoices taught us what Jade White costs
app._ORDER_PRICES["GFA00"] = 24.99
app.store.upsert_filament(GOOD, filament_id="GFA00", type="PLA", color=WHITE,
                          code="A00-W01", color_name="Jade White", is_bambu=1)
app.store.upsert_filament(STRAY, filament_id="GFA07", type="PLA", color=WHITE, is_bambu=1)
app._rebuild_filament_meta()

def seed(job, sku, per_kg, genuine):
    app.store.upsert_print(job_id=job, name="Cable Winder", started_at=time.time()-3600,
                           ended_at=time.time(), final_state="FINISH", total_layers=150,
                           energy_wh=120, cost=0.04, peak_w=95)
    app.store.update_print_fields(job,
        ams_bambu=json.dumps({"1": genuine}) if genuine is not None else None,
        filament_g=40.0, filament_cost=round(40/1000*per_kg, 4),
        filament_detail=json.dumps([{"slot": 1, "type": "PLA", "filament_id": sku,
            "code": None, "color": WHITE, "brand": "Bambu", "grams": 40.0,
            # the real rule label, not a placeholder: a re-cost rewrites an
            # entry whose RULE changed as well as one whose figure did
            "per_kg": per_kg, "rule": "order GFA00" if genuine else "default",
            "cost": round(40/1000*per_kg, 4)}]))

seed("A", "GFA00", 24.99, True)      # recognised
seed("B", "GFA00", 24.99, True)
# the two strays: the RFID snapshot never reached them, so they fell through to
# the configured default
DEFAULT = float(app.FIL_CFG.get("default_per_kg", 0) or 0) or 20.0
seed("C", "GFA07", DEFAULT, None)
seed("D", "GFA07", DEFAULT, None)

cost = lambda j: (app.store.get_print(j) or {}).get("filament_cost")
print("before merge:", {j: cost(j) for j in "ABCD"})
assert cost("A") != cost("C"), "the fixture does not reproduce the split"

# --- the merge the user actually did ---
r = c.post("/api/filaments/merge", json={"from": STRAY, "into": GOOD})
d = r.get_json()
print("merge        ->", d)
assert d["ok"], d
after = {j: cost(j) for j in "ABCD"}
print("after merge :", after)
assert len(set(after.values())) == 1, f"the batch still has two prices: {after}"
assert abs(after["C"] - 40/1000*24.99) < 1e-6, "the folded prints were not priced as Jade White"
assert d["recosted"] == 2, d
print("all four now cost", after["A"], "- the invoice price for Jade White")

# the rule that got them there is the identity's, not the entry's own SKU
per_kg, rule = app._filament_price_per_kg(
    {"slotId": 0, "filamentId": "GFA07", "filamentType": "PLA"}, {}, GOOD)
print("priced by identity:", per_kg, rule)
assert (per_kg, rule) == (24.99, "order GFA00"), (per_kg, rule)

# --- undoing the merge puts the prices back ---
r = c.post("/api/filaments/merge", json={"from": STRAY, "into": ""})
d = r.get_json()
back = {j: cost(j) for j in "ABCD"}
rule = lambda j: json.loads(app.store.get_print(j)["filament_detail"])[0]["rule"]
print("unmerged    ->", d, back, "| C rule:", rule("C"))
assert d["ok"], d
# Compare the RULE, not the number. Unmerged, the stray identity still carries a
# genuine RFID reading of its own, so it prices from the Bambu matrix - which in
# this fixture happens to be the same figure as the invoice. Asserting on the
# amount would pass or fail on that coincidence rather than on the behaviour.
assert rule("C") != "order GFA00", "the folded identity kept the survivor's invoice price"
assert back["A"] == after["A"], "unmerging disturbed the prints that were never folded"

# --- a third-party spool must NOT inherit a Bambu invoice price ---
app.store.upsert_filament("GFA00|7C4B00", filament_id="GFA00", type="PLA",
                          color="7C4B00", is_bambu=0)      # sliced with a Bambu profile
app._rebuild_filament_meta()
per_kg, rule = app._filament_price_per_kg(
    {"slotId": 0, "filamentId": "GFA00", "filamentType": "PLA"}, {}, "GFA00|7C4B00")
print("third-party :", per_kg, rule)
assert rule != "order GFA00", "a third-party spool was priced as a genuine Bambu one"
print("a third-party spool is still refused the Bambu price")
print("ok")
