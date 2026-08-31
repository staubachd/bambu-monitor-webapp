"""A spool you have named must be named on the AMS card too.

The AMS can only read a genuine Bambu tag, so a third-party tray arrives with no
name at all and the tile said "no RFID data" - even after the vendor, product
and colour had been typed in on the Filament page. The identity the tray reports
is the same one that was named, so the name is right there to be used.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app

for f in app.store.all_filaments():
    app.store.delete_filament(f["fkey"])

RED = "F73737"
SUNLU = f"GFL99|{RED}"
app.store.upsert_filament(SUNLU, filament_id="GFL99", type="PLA", color=RED, is_bambu=0)
app.store.set_filament_identity(SUNLU, vendor="Sunlu", product="PLA Meta",
                                color_name="Sunlu Red")
app._rebuild_filament_meta()

def tray(**kw):
    t = {"id": 1, "type": "PLA", "brand": None, "filament_id": "GFL99",
         "code": None, "color": RED, "is_bambu": False, "remain_pct": -1}
    t.update(kw)
    st = {"ams": {"units": [{"id": 0, "trays": [t]}], "external": []}}
    app._enrich_ams(st)
    return st["ams"]["units"][0]["trays"][0]

t = tray()
print("third-party, named :", {k: t.get(k) for k in ("vendor", "product", "color_name")})
assert (t.get("vendor"), t.get("product"), t.get("color_name")) == ("Sunlu", "PLA Meta", "Sunlu Red"), t
# but it is still not a Bambu spool: no store link, no false low-stock warning
assert not t.get("store_url"), "a third-party spool was linked to the Bambu store"
assert t.get("low") is False, "a tray with no readable remaining % was flagged low"
print("no store link, no low-stock guess - it is still not an RFID spool")

# an unnamed third-party spool stays unnamed rather than borrowing someone else's
u = tray(filament_id="GFL00", color="00FF00")
assert not u.get("vendor") and not u.get("color_name"), u
print("an unnamed spool stays unnamed")

# a genuine Bambu spool keeps its catalogue name when nothing was typed in...
b = tray(filament_id="GFA00", color="F4F4F4", code="A00-W01", brand="PLA Basic",
         is_bambu=True, remain_pct=72)
print("bambu, catalogue   :", b.get("color_name"), "| store link:", bool(b.get("store_url")))
assert b.get("color_name"), "a genuine spool lost its catalogue colour name"
assert b.get("store_url"), "a genuine spool lost its reorder link"

# ...and a name typed in by hand beats the catalogue, as it does everywhere else
BAMBU = "GFA00|F4F4F4"
app.store.upsert_filament(BAMBU, filament_id="GFA00", type="PLA", color="F4F4F4",
                          code="A00-W01", is_bambu=1)
app.store.set_filament_identity(BAMBU, color_name="My White")
app._rebuild_filament_meta()
b2 = tray(filament_id="GFA00", color="F4F4F4", code="A00-W01", brand="PLA Basic",
          is_bambu=True, remain_pct=72)
print("bambu, renamed     :", b2.get("color_name"))
assert b2.get("color_name") == "My White", b2.get("color_name")

# a merged identity is named by whichever row survived
OTHER = f"GFL96|{RED}"
app.store.upsert_filament(OTHER, filament_id="GFL96", type="PLA", color=RED, is_bambu=0)
app.store.set_filament_alias(OTHER, SUNLU)
app._rebuild_filament_meta()
m = tray(filament_id="GFL96")
print("folded identity    :", m.get("vendor"), m.get("color_name"))
assert m.get("color_name") == "Sunlu Red", "a merged tray did not inherit the name"

# renaming has to reach the tiles without a restart
c = app.app.test_client()
c.post("/api/filaments/identity", json={"fkey": SUNLU, "color_name": "Ferrari Red"})
assert tray().get("color_name") == "Ferrari Red", "a rename did not reach the AMS tile"
print("a rename reaches the tile immediately")
print("ok")
