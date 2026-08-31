"""An idle printer must not write to the disk.

`_observe_filaments` runs on every MQTT frame - about once a second - and is
supposed to write only when a tray's identity actually changes. It did not:
two black PLA spools produce the same filament identity, but the printer
reports their colour code with different zero-padding ('A00-K00' and
'A00-K0'). Each tray then looked like a change to the other, and both wrote on
every frame: ~100 UPDATEs a minute with nothing printing, which is exactly the
kind of constant write that stops a NAS's disks from ever spinning down.

The contract is about the RATE, not the SQL: repeated identical frames write
once, whatever the printer's spelling.
"""
# The app source, relative to this file. These tests used to sit inside the
# source folder and could name it directly; they live beside it now, so that
# they survive a temp-directory clean-out.
import os as _os
SRC_DIR = (_os.environ.get("BAMBU_SRC")
           or _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
def _src(name):
    return _os.path.join(SRC_DIR, name)
import sys, os, copy
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app


def frame(*trays):
    return {"ams": {"units": [{"trays": list(trays)}], "external": []}}


def tray(fid, color, typ, code, **extra):
    t = dict(filament_id=fid, color=color, type=typ, code=code,
             brand="Bambu", color_name=None, is_bambu=True)
    t.update(extra)
    return t


writes = []
app.store.upsert_filament = lambda fkey, **f: writes.append((fkey, f))


def observe(state, n=1):
    del writes[:]
    for _ in range(n):
        app._observe_filaments(copy.deepcopy(state))
    return len(writes)


# --- the actual bug: one identity, two spellings, in the SAME frame ---------
two_blacks = frame(tray("GFA00", "000000", "PLA", "A00-K00"),
                   tray("GFA00", "000000", "PLA", "A00-K0"))
app._fil_obs.clear()
first = observe(two_blacks)
assert first == 1, f"two trays of one identity wrote {first} times in a single frame"
print("two spools of the same filament are one identity, and write once")

n = observe(two_blacks, 60)
assert n == 0, (f"{n} writes over 60 identical frames - at ~1 frame/s that is "
                f"{n} writes a minute with the printer doing nothing")
print("60 more identical frames ->", n, "writes")

# --- and the same when the padding alternates between frames ----------------
app._fil_obs.clear()
a = frame(tray("GFA00", "000000", "PLA", "A00-K00"))
b = frame(tray("GFA00", "000000", "PLA", "A00-K0"))
observe(a)
del writes[:]
for i in range(60):
    app._observe_filaments(copy.deepcopy(b if i % 2 else a))
assert not writes, f"an alternating code spelling wrote {len(writes)} times"
print("a code that alternates spelling between frames writes nothing")

# --- a real change still gets through --------------------------------------
app._fil_obs.clear()
observe(a)
changed = observe(frame(tray("GFA00", "000000", "PETG", "A00-K00")))
assert changed == 1, "a genuinely different filament was not written"
print("a real identity change is still written")

app._fil_obs.clear()
observe(a)
named = observe(frame(tray("GFA00", "000000", "PLA", "A00-K00",
                           color_name="Black")))
# color_name is part of the identity, so learning one is a real change
assert named == 1, "learning a colour name was swallowed"
print("learning a colour name is still written")

# --- what is stored is the canonical spelling ------------------------------
app._fil_obs.clear()
observe(frame(tray("GFA00", "000000", "PLA", "A00-K00")))
assert writes[0][1]["code"] == "A00-K0", (
    f"stored code is {writes[0][1]['code']!r}; the rest of the app compares "
    f"codes through norm_code, so the database should hold that form")
print("the code is stored in the form everything else compares by")

# --- and nothing else writes on a frame ------------------------------------
# The whole point is that an idle printer is quiet. If a new per-frame writer
# appears, this catches it as a rate rather than as a mystery on the NAS.
src = open(_src("app.py"), encoding="utf-8").read()
start = src.index("def on_message(")
body = src[start:src.index("\ndef ", start + 10)]
for writer in ("store.record(", "store.upsert_print(", "store.upsert_filament(",
               "store.set_setting(", "store.update_print_fields("):
    assert writer not in body, (
        f"{writer} was added directly to on_message, which runs about once a "
        f"second - it must go behind a gate like _maybe_record or _fil_obs")
print("on_message itself still calls no store writer directly")
print("ok")
