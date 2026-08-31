"""A merge is permanent: the folded identity must never come back.

The identity is SKU|COLOUR, and the SKU is the printer's filament PROFILE - so
the same spool set up under a different profile mints a second row. Merging is
what tells the app they are one filament, and the question that matters is
whether that has to be repeated every time the spool is loaded again.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app

for f in app.store.all_filaments():
    app.store.delete_filament(f["fkey"])
for p in app.store.all_prints():
    app.store.delete_print(p["job_id"])

RED, OLD, NEW = "F73737", "GFL99|F73737", "GFL96|F73737"
app.store.upsert_filament(OLD, filament_id="GFL99", type="PLA", color=RED, is_bambu=0)
app.store.set_filament_identity(OLD, vendor="Sunlu", product="PLA Meta", color_name="Sunlu Red")
app.store.upsert_filament(NEW, filament_id="GFL96", type="PLA", color=RED, is_bambu=0)

keys = lambda: {r["fkey"] for r in app._filament_stats()["filaments"]}
print("before merge :", sorted(keys()))
assert NEW in keys() and OLD in keys(), "two rows expected before the merge"

app.store.set_filament_alias(NEW, OLD)          # what the merge button does
print("after merge  :", sorted(keys()))
assert NEW not in keys(), "the folded row is still listed separately"
assert OLD in keys(), "the named row disappeared"

# Loading that spool again re-observes the SAME identity. The observer upserts
# it on every frame - the fold must survive that, or the merge would silently
# come undone the next time the spool is in the AMS.
state = {"ams": {"units": [{"trays": [
    {"id": 1, "type": "PLA", "brand": "PLA Basic", "filament_id": "GFL96",
     "color": RED, "code": None, "is_bambu": False}]}], "external": []}}
app._fil_obs.clear()
app._observe_filaments(state)
row = next(r for r in app.store.all_filaments() if r["fkey"] == NEW)
print("re-observed  : alias_of =", row.get("alias_of"))
assert row.get("alias_of") == OLD, "re-observing the spool cleared the merge"
assert NEW not in keys(), "the folded row came back after being seen again"

# ...and the grams and the slot land on the named row, not on a new one
with app._state_lock:
    app._state.clear(); app._state.update(state)
merged = next(r for r in app._filament_stats()["filaments"] if r["fkey"] == OLD)
print("slot shown on:", merged["fkey"], "->", "slot", merged["slot"])
assert merged["slot"] == 2, f"the AMS slot did not follow the fold: {merged['slot']}"

# a THIRD profile is a third identity - the fold is per SKU|COLOUR, not per spool
app.store.upsert_filament("GFL77|" + RED, filament_id="GFL77", type="PLA",
                          color=RED, is_bambu=0)
assert "GFL77|" + RED in keys(), "a new profile should appear as its own row"
print("a third profile:", "new row, needs its own merge (as expected)")
print("ok")
