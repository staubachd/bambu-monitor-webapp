"""Saving something that is already what you saved must still report success.

Every `return n > 0` in storage.py means "the row existed" - it is how each
endpoint decides between 200 and 404. On sqlite `cursor.rowcount` after an
UPDATE counts the rows MATCHED, which is that question. On MariaDB it counts the
rows whose values actually CHANGED, which is a different question with the same
name, and it answers 0 for a save that writes what was already there.

That is why re-saving an unchanged filament name reported "no such filament"
on the NAS while the identical code was fine in development. The fix is one
connect flag, CLIENT.FOUND_ROWS, and it is invisible: nothing fails until
somebody saves a value twice, on the backend that is not the one being
developed against.
"""
import sys, os, time, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import os as _os
SRC_DIR = (_os.environ.get("BAMBU_SRC")
           or _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import app, storage

c = app.app.test_client()
store = app.store
FKEY = "GFA00|C0FFEE"
store.upsert_filament(FKEY, filament_id="GFA00", color="C0FFEE", type="PLA")

# --- saving the same value twice --------------------------------------------
first = c.post("/api/filaments/identity",
               json={"fkey": FKEY, "vendor": "Sunlu", "product": "PLA Meta",
                     "color_name": "Coffee"})
assert first.get_json()["ok"], first.get_json()
again = c.post("/api/filaments/identity",
               json={"fkey": FKEY, "vendor": "Sunlu", "product": "PLA Meta",
                     "color_name": "Coffee"})
assert again.status_code == 200 and again.get_json()["ok"], (
    f"re-saving an unchanged name answered {again.status_code}: {again.get_json()} - "
    f"the row plainly exists, so 'did it exist' was answered by 'did it change'")
print("saving an unchanged identity twice: ok both times")

# the same trap, on every other writer that reports existence the same way
store.set_filament_price(FKEY, 18.99)
assert store.set_filament_price(FKEY, 18.99) is True, \
    "setting the same price twice reported the filament as missing"
store.set_filament_anchor(FKEY, 500.0, time.time())
assert store.set_filament_anchor(FKEY, 500.0, time.time()) is True, \
    "pinning the same amount twice reported the filament as missing"

store.upsert_print(job_id="rc-1", name="rc-1", started_at=time.time(),
                   final_state="RUNNING", total_layers=1)
store.set_print_label("rc-1", "same label")
assert store.set_print_label("rc-1", "same label") is True, \
    "setting the same label twice reported the print as missing"
nid = store.add_note("T", "B", None)
store.update_note(nid, "T", "B", None)
assert store.update_note(nid, "T", "B", None) is True, \
    "saving an unchanged note twice reported it as missing"
print("price, anchor, label and note: all still report the row as existing")

# --- and a row that really is missing still says so -------------------------
assert store.set_filament_price("GFZZ|000000", 1.0) is False
assert store.set_print_label("no-such-job", "x") is False
assert store.update_note(999999, "x", "y", None) is False
assert store.delete_note(999999) is False
print("a row that genuinely is not there still reports false")

# --- the flag that makes this true on the NAS -------------------------------
# There is no MariaDB here, so this is the one thing that can be checked: the
# connection is still asked for FOUND_ROWS. Losing that line would break every
# assertion above, in production only.
src = open(os.path.join(SRC_DIR, "storage.py"), encoding="utf-8").read()
assert "CLIENT.FOUND_ROWS" in src, (
    "the FOUND_ROWS client flag is gone - on MariaDB and MySQL every 'did the "
    "row exist' check above starts answering 'did the value change', and saving "
    "an unchanged value reports 'no such filament'")
assert "client_flag=CLIENT.FOUND_ROWS" in src
print("the connection still asks for FOUND_ROWS, which is what makes it true on MariaDB")

# and it is asked for on every server backend, not just the one that was first
for name, d in storage.DIALECTS.items():
    if d["server"]:
        assert "client_flag" in src, f"{name} connects without the flag"
print(f"...for all {sum(1 for d in storage.DIALECTS.values() if d['server'])} server backends")

store.delete_note(nid)
store.delete_print("rc-1")
store.delete_filament(FKEY)
print("ok")
