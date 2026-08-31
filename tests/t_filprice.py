"""A price typed in by hand must beat every guess, and must apply to prints that
already happened.

The configured brand x material matrix cannot know what a Sunlu spool cost, and
the invoice importer only covers SKUs that appear on a Bambu invoice. Per-print
costs are STORED (worked out when the cloud enriched the job), so a price that
only affected future prints would leave the totals disagreeing with the number
just typed in.
"""
import sys, os, json, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app

c = app.app.test_client()
for f in app.store.all_filaments():
    app.store.delete_filament(f["fkey"])
for p in app.store.all_prints():
    app.store.delete_print(p["job_id"])

RED, FKEY = "F73737", "GFL99|F73737"
app.store.upsert_filament(FKEY, filament_id="GFL99", type="PLA", color=RED, is_bambu=0)
app.store.set_filament_identity(FKEY, vendor="Sunlu", product="PLA Meta", color_name="Sunlu Red")
app._rebuild_filament_meta()

def seed(job, grams, per_kg):
    app.store.upsert_print(job_id=job, name=job, started_at=time.time()-3600,
                           ended_at=time.time(), final_state="FINISH", total_layers=10,
                           energy_wh=100, cost=0.03, peak_w=90)
    detail = [{"slot": 1, "type": "PLA", "filament_id": "GFL99", "code": None,
               "color": RED, "brand": "third-party", "grams": grams,
               "per_kg": per_kg, "rule": "default", "cost": round(grams/1000*per_kg, 4)}]
    app.store.update_print_fields(job, filament_detail=json.dumps(detail),
                                  filament_g=grams,
                                  filament_cost=round(grams/1000*per_kg, 4))

seed("A", 250.0, 20.0)      # costed at the configured default
seed("B", 100.0, 20.0)

def cost_of(job):
    return (app.store.get_print(job) or {}).get("filament_cost")
def entry_of(job):
    return json.loads(app.store.get_print(job)["filament_detail"])[0]

print("before      A:", cost_of("A"), " B:", cost_of("B"))
assert abs(cost_of("A") - 5.0) < 1e-6

# --- set the price ---
r = c.post("/api/filaments/price", json={"fkey": FKEY, "price_per_kg": 12.5})
d = r.get_json()
print("set 12.50   ->", d)
assert d["ok"] and d["recosted"] == 2, d
assert abs(cost_of("A") - 3.125) < 1e-6, f"A not re-costed: {cost_of('A')}"
assert abs(cost_of("B") - 1.25) < 1e-6, f"B not re-costed: {cost_of('B')}"
assert entry_of("A")["per_kg"] == 12.5, "the per-slot detail still shows the old price"
assert entry_of("A")["rule"] == "set by hand", entry_of("A")["rule"]

# it is what new prints get priced at too, ahead of every configured rule
per_kg, rule = app._filament_price_per_kg(
    {"slotId": 0, "filamentId": "GFL99", "filamentType": "PLA"}, {}, FKEY)
print("new print   ->", per_kg, rule)
assert (per_kg, rule) == (12.5, "set by hand"), (per_kg, rule)

# a price beats even an invoice-learned one, which is a guess that the SKU on
# the receipt is the spool that was in the tray
app._ORDER_PRICES["GFL99"] = 29.99
per_kg, _ = app._filament_price_per_kg(
    {"slotId": 0, "filamentId": "GFL99", "filamentType": "PLA"}, {"1": True}, FKEY)
assert per_kg == 12.5, f"an invoice price overruled the one set by hand: {per_kg}"
app._ORDER_PRICES.pop("GFL99")
print("beats the invoice-learned price")

# --- clearing it hands the filament back to the configured rules ---
r = c.post("/api/filaments/price", json={"fkey": FKEY, "price_per_kg": ""})
d = r.get_json()
print("cleared     ->", d)
assert d["ok"] and d["price_per_kg"] is None, d
back = app._filament_price_per_kg(
    {"slotId": 0, "filamentId": "GFL99", "filamentType": "PLA"}, {}, FKEY)
assert back[1] != "set by hand", "clearing did not release the filament"
assert abs(cost_of("A") - 250/1000*back[0]) < 1e-6, "history was not re-costed on clear"
print("history followed the clear:", cost_of("A"), "at", back[0], "/kg")

# --- a merged identity is priced by whichever row survived ---
OTHER = "GFL96|" + RED
app.store.upsert_filament(OTHER, filament_id="GFL96", type="PLA", color=RED, is_bambu=0)
app.store.set_filament_alias(OTHER, FKEY)
c.post("/api/filaments/price", json={"fkey": FKEY, "price_per_kg": 15})
app._rebuild_filament_meta()
per_kg, rule = app._filament_price_per_kg(
    {"slotId": 0, "filamentId": "GFL96", "filamentType": "PLA"}, {},
    app._canon_fkey(OTHER))
print("folded row  ->", per_kg, rule)
assert (per_kg, rule) == (15.0, "set by hand"), "a merged identity lost the price"

# --- rubbish is refused rather than stored ---
for bad in ("abc", -5, 10**9):
    rr = c.post("/api/filaments/price", json={"fkey": FKEY, "price_per_kg": bad})
    assert rr.status_code == 400, f"{bad!r} was accepted"
# a well-formed identity nothing has recorded yet is CREATED, the same way
# naming one is - see the history-only case below for why that matters
assert c.post("/api/filaments/price", json={"fkey": "NOPE|000000", "price_per_kg": 5}
              ).status_code == 200, "a well-formed identity was refused"
print("rubbish refused, a well-formed identity is created")

# and the page reports the price so the cell can show it
row = next(x for x in app._filament_stats()["filaments"] if x["fkey"] == FKEY)
assert row["price_per_kg"] == 15.0, row.get("price_per_kg")
print("shown on the page:", row["price_per_kg"])

# --- a filament that exists only in the print history ---
# Nothing has ever created a `filaments` row for it: the AMS never saw it (used
# up before this page existed, or the identity came from the cloud's slicer
# profile). It still shows on the Filament page, and it is exactly the kind of
# third-party spool whose price has to be typed in - so pricing it must create
# the row rather than answer "no such filament".
HIST = "GFL55|00AA55"
app.store.upsert_print(job_id="C", name="C", started_at=time.time()-60,
                       ended_at=time.time(), final_state="FINISH", total_layers=5,
                       energy_wh=10, cost=0.01, peak_w=50)
app.store.update_print_fields("C", filament_g=100.0, filament_cost=2.0,
    filament_detail=json.dumps([{"slot": 1, "type": "PLA", "filament_id": "GFL55",
        "code": None, "color": "00AA55", "brand": "third-party", "grams": 100.0,
        "per_kg": 20.0, "rule": "default", "cost": 2.0}]))
assert not any(f["fkey"] == HIST for f in app.store.all_filaments()), "row should not exist yet"

r = c.post("/api/filaments/price", json={"fkey": HIST, "price_per_kg": 9.5})
d = r.get_json()
print("history-only ->", r.status_code, d)
assert r.status_code == 200 and d["ok"], f"pricing a history-only filament failed: {d}"
assert any(f["fkey"] == HIST for f in app.store.all_filaments()), "the row was not created"
assert d["recosted"] == 1, d
assert abs(cost_of("C") - 0.95) < 1e-6, f"the print was not re-costed: {cost_of('C')}"
print("row created on demand, print re-costed:", cost_of("C"))

# junk still cannot mint a row
before = len(app.store.all_filaments())
rr = c.post("/api/filaments/price", json={"fkey": "not an identity", "price_per_kg": 5})
assert rr.status_code == 400, rr.status_code
assert len(app.store.all_filaments()) == before, "a junk key created a filament row"
print("a junk identity is refused and creates nothing")
print("ok")
