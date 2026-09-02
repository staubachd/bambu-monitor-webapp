"""Reading the slicer's own metadata off the printer.

Layer height, the profile, the infill, the exact gram figure - these exist only
in the file that was sliced. The printer keeps that file on a USB drive and
serves it over FTPS, so the app can go and read it. What that has to get right:

  * **read a little.** The sliced file is mostly gcode: 11 MB of it in the
    3.8 MB sample, and hundreds of megabytes in a big project. The two config
    members are 14 KB. Downloading whole jobs on a timer is how a NAS disk never
    sleeps, and this app has been bitten by that once already.
  * **the right file.** A wrong layer height is worse than no layer height,
    because it reads as a fact. So the file is matched by name and cross-checked
    by plate, and anything short of that reads nothing.
  * **never lose what somebody typed.** The slicer's figure and the user's are
    different columns, and re-reading the file must not undo a correction.
  * **degrade.** A printer with no drive in it, a member that is missing, a key
    the slicer stopped writing: the print is still recorded, just without this.

The parser runs against a real capture in samples/, taken off the user's own
printer, so these are the actual bytes Bambu Studio 02.08.02.61 writes.
"""
import sys, os, io, json, time, zipfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import os as _os
SRC_DIR = (_os.environ.get("BAMBU_SRC")
           or _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import app, gcode_meta, storage, settings_schema

c = app.app.test_client()
store = app.store

SAMPLES = os.path.join(SRC_DIR, "samples")
PS = open(os.path.join(SAMPLES, "project_settings.config"), "rb").read()
SI = open(os.path.join(SAMPLES, "slice_info.config"), "rb").read()

# --- the parser, against what the printer actually wrote --------------------
meta = gcode_meta.parse(PS, SI)
assert meta["layer_h"] == 0.12, meta
assert meta["first_layer_h"] == 0.2, meta
assert meta["nozzle_mm"] == 0.4, meta
assert meta["profile"] == "0.12mm High Quality @BBL X2D", meta
assert meta["printer"] == "Bambu Lab X2D", meta
assert meta["infill_pct"] == 15.0 and meta["infill_pattern"] == "gyroid", meta
assert meta["walls"] == 2 and isinstance(meta["walls"], int), meta
assert meta["supports"] == "tree(auto)", meta
assert meta["plate"] == 15, meta
assert meta["grams"] == 49.41, meta
assert meta["est_min"] == 162.4, meta
assert meta["filaments"][0]["code"] == "GFA00", meta
assert meta["filaments"][0]["grams"] == 49.41, meta
assert meta["object"] == "Oberteil 'groß` Herz", (
    "the object name did not survive - it is UTF-8 and must not be 'fixed'")
print("real capture parsed:", meta["profile"], "/", meta["layer_h"], "mm /",
      meta["grams"], "g")

# --- supports: two keys, and only one of them is the switch ----------------
d = json.loads(PS.decode())
d["enable_support"] = "0"
off = gcode_meta.parse(json.dumps(d).encode(), SI)
assert "supports" not in off, (
    "support_type is still reported with supports switched off - that claims "
    "supports on a print that has none")
print("supports off is reported as off, not as the type it would have used")

# --- degrading, one failure at a time --------------------------------------
assert gcode_meta.parse(None, SI)["grams"] == 49.41, "no settings member is fatal"
assert gcode_meta.parse(PS, None)["layer_h"] == 0.12, "no slice_info is fatal"
assert gcode_meta.parse(None, None) == {}, "nothing at all should be nothing, not a crash"
for bad, why in [(b"not json", "settings that are not JSON"),
                 (b'"a string"', "settings that are not an object")]:
    try:
        gcode_meta.parse(bad, SI)
        raise AssertionError(f"{why} was accepted")
    except gcode_meta.SlicerError:
        pass
try:
    gcode_meta.parse(PS, b"<config><plate>")
    raise AssertionError("truncated XML was accepted")
except gcode_meta.SlicerError:
    pass
# a slicer that stops writing a key must not take the rest down with it
thin = gcode_meta.parse(json.dumps({"layer_height": "0.2"}).encode(), None)
assert thin == {"layer_h": 0.2}, thin
assert "nil" not in str(gcode_meta.parse(
    json.dumps({"layer_height": "nil", "wall_loops": "0"}).encode(), None))
print("a missing member, junk, or a dropped key degrades to less, never to a crash")

# --- reading a little: the gcode member is never fetched --------------------
# A fake server that serves a real ZIP and counts what is asked of it, so this
# exercises the actual RemoteZip windowing rather than a description of it.
# Incompressible on purpose. Repeated gcode lines deflate to almost nothing, and
# a "big" member that is 300 bytes in the archive would let this test pass while
# the reader downloaded the lot. Seeded, so the numbers below are stable.
import random
BIG = b"; a very large gcode member\n" + random.Random(7).randbytes(2_000_000)


def build_3mf():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("Metadata/plate_15.png", b"\x89PNG" + b"\0" * 20000)
        z.writestr(gcode_meta.SETTINGS_MEMBER, PS)
        z.writestr("Metadata/plate_15.gcode", BIG)
        z.writestr(gcode_meta.SLICE_MEMBER, SI)
    return buf.getvalue()


BLOB = build_3mf()


class FakeConn:
    def __init__(self, data):
        self.data = data
        self.at = 0

    def recv(self, n):
        chunk = self.data[self.at:self.at + n]
        self.at += len(chunk)
        return chunk

    def close(self):
        pass


class FakeFTP:
    """Just enough of ftplib for RemoteZip: TYPE, RETR with REST, and the
    response that follows an aborted transfer."""

    def __init__(self, blob):
        self.blob = blob
        self.served = 0
        self.ranges = []
        self.binary = False

    def voidcmd(self, cmd):
        if cmd == "TYPE I":
            self.binary = True

    def transfercmd(self, cmd, rest=None):
        assert cmd.startswith("RETR "), cmd
        assert self.binary, ("REST was used without TYPE I - the real server "
                             "answers '550 No support for resume of ASCII transfer'")
        start = int(rest or 0)
        self.ranges.append(start)
        data = self.blob[start:]
        self.served += len(data)
        return FakeConn(data)

    def voidresp(self):
        pass


ftp = FakeFTP(BLOB)
members, pulled = gcode_meta.read_members(ftp, "job.gcode.3mf", len(BLOB))
assert gcode_meta.SETTINGS_MEMBER in members and gcode_meta.SLICE_MEMBER in members
assert gcode_meta.parse(members[gcode_meta.SETTINGS_MEMBER],
                        members[gcode_meta.SLICE_MEMBER])["layer_h"] == 0.12
assert pulled < len(BLOB) / 4, (
    f"pulled {pulled:,} of {len(BLOB):,} bytes - the whole point is to read the "
    f"index and two small members, not the job")
assert pulled < 400 * 1024, f"pulled {pulled:,} bytes, which is not 'a little'"
# and specifically: the gcode itself never crossed the wire
assert BIG[:200] not in b"".join(members.values())
print(f"read {pulled:,} bytes of a {len(BLOB):,}-byte file "
      f"({pulled / len(BLOB) * 100:.1f}%) in {len(ftp.ranges)} ranged transfer(s)")

# a file with no metadata in it is refused, not silently reported as empty
empty = io.BytesIO()
with zipfile.ZipFile(empty, "w") as z:
    z.writestr("Metadata/plate_1.gcode", b"G1")
try:
    gcode_meta.read_members(FakeFTP(empty.getvalue()), "x.3mf", len(empty.getvalue()))
    raise AssertionError("a 3MF with no slicer metadata was accepted")
except gcode_meta.SlicerError:
    pass
# ...and so is something that is not a ZIP at all
try:
    gcode_meta.read_members(FakeFTP(b"not a zip" * 100), "x.3mf", 900)
    raise AssertionError("a file that is not a sliced job was accepted")
except gcode_meta.SlicerError:
    pass
print("a file with no metadata, and a file that is not a 3MF, are both refused")

# --- picking the right file --------------------------------------------------
FILES = [
    {"name": "Fall in Love_Riffel.gcode.3mf", "size": 1, "mtime": 200},
    {"name": "Herbstliebe_Oberteil.gcode.3mf", "size": 1, "mtime": 100},
]
assert gcode_meta.pick_file(FILES, "Herbstliebe_Oberteil")["mtime"] == 100, \
    "matched by recency instead of by name"
assert gcode_meta.pick_file(FILES, "Fall in Love_Riffel")["mtime"] == 200
assert gcode_meta.pick_file(FILES, "something else") is None, (
    "no file matches this print's name, and it took the newest one anyway - "
    "that attributes one print's settings to another")
assert gcode_meta.pick_file(FILES, None) is None, (
    "with two candidates and no name to choose by, it guessed")
assert gcode_meta.pick_file(FILES[:1], None)["mtime"] == 200, \
    "one candidate and no name is the one case where there is nothing to get wrong"
assert gcode_meta.pick_file([], "x") is None
print("the file is chosen by name; ambiguity reads nothing rather than guessing")

# --- and the plate is a second opinion, not a formality --------------------
# MQTT says which plate is printing; the file says which plate it holds. They
# have always agreed. If they ever do not, the file is not this print's, and
# reading it would attribute one plate's settings to another.
_conn, _files, _read = gcode_meta.connect, gcode_meta.sliced_files, gcode_meta.read_members
gcode_meta.connect = lambda *a, **k: FakeFTP(BLOB)
gcode_meta.sliced_files = lambda ftp: [{"name": "job.gcode.3mf", "size": len(BLOB),
                                        "mtime": 1}]
gcode_meta.read_members = lambda ftp, n, s, reconnect=None: (
    {gcode_meta.SETTINGS_MEMBER: PS, gcode_meta.SLICE_MEMBER: SI}, 1234)
try:
    ok = gcode_meta.fetch("1.2.3.4", "code", subtask="job")
    assert ok["layer_h"] == 0.12 and ok["read_bytes"] == 1234, ok
    assert gcode_meta.fetch("1.2.3.4", "code", subtask="job", plate=15)["plate"] == 15
    try:
        gcode_meta.fetch("1.2.3.4", "code", subtask="job", plate=9)
        raise AssertionError(
            "the file says plate 15 and the printer said plate 9, and it was "
            "read anyway - that is one print's layer height on another's row")
    except gcode_meta.SlicerError as e:
        assert "plate" in str(e)
finally:
    gcode_meta.connect, gcode_meta.sliced_files, gcode_meta.read_members = _conn, _files, _read
print("a plate that disagrees with the printer is refused, not attributed")

# --- the columns and the split ----------------------------------------------
for col in ("layer_h", "layer_h_manual", "nozzle_mm", "slicer_profile",
            "est_min", "slice_json"):
    assert col in storage.LATE_COLUMNS["prints"], f"{col} would never reach an upgrade"
    assert col in store.table_columns("prints"), f"{col} was not created"
    assert col in storage.PRINT_IMMUTABLE, (
        f"{col} is not immutable, so the next MQTT upsert blanks it")
print("all six columns migrate, and none of them can be blanked by the MQTT loop")

# --- what the user typed always wins, and survives a re-read ----------------
store.upsert_print(job_id="sl-1", name="thing.3mf", started_at=time.time() - 600,
                   ended_at=time.time(), final_state="FINISH", total_layers=100)
store.update_print_fields("sl-1", layer_h=0.2, slice_json=json.dumps({"layer_h": 0.2}))
assert c.post("/api/prints/layerheight",
              json={"job_id": "sl-1", "mm": "0.12"}).get_json()["ok"]
row = store.get_print("sl-1")
assert row["layer_h_manual"] == 0.12 and row["layer_h"] == 0.2, (
    f"typing a layer height wrote over the slicer's: {row['layer_h']}")
# re-reading the file refreshes the slicer's column and leaves the typed one
store.update_print_fields("sl-1", layer_h=0.28)
assert store.get_print("sl-1")["layer_h_manual"] == 0.12, \
    "re-reading the sliced file undid a correction somebody made by hand"
# and clearing the override falls back to the slicer rather than to nothing
back = c.post("/api/prints/layerheight", json={"job_id": "sl-1", "mm": ""}).get_json()
assert back["mm"] is None and back["effective"] == 0.28, back
print("typed beats parsed; a re-read cannot undo it; clearing falls back to the file")

# --- the one-time move of what was typed before the split ------------------
# layer_h used to BE the typed value. On upgrade it has to move, or a hand-typed
# figure sits in the slicer's column claiming to have come from the file.
import sqlite3, tempfile
tmp = os.path.join(tempfile.mkdtemp(), "old.db")
con = sqlite3.connect(tmp)
con.execute("CREATE TABLE prints (job_id VARCHAR(64) PRIMARY KEY, name VARCHAR(255),"
            " started_at DOUBLE, ended_at DOUBLE, final_state VARCHAR(16),"
            " total_layers INT, layer_h FLOAT)")
con.execute("INSERT INTO prints (job_id, layer_h) VALUES ('old-1', 0.12)")
con.execute("INSERT INTO prints (job_id, layer_h) VALUES ('old-2', NULL)")
con.commit(); con.close()
old = storage.Storage({"backend": "sqlite", "sqlite_path": tmp})
moved = old.get_print("old-1")
assert moved["layer_h_manual"] == 0.12, (
    "a layer height typed in before the split was lost by the upgrade")
assert moved["layer_h"] is None, (
    "it was copied but not cleared, so a hand-typed figure now sits in the "
    "slicer's column and the page will say it came from the sliced file")
assert old.get_print("old-2")["layer_h_manual"] is None
assert "prints.layer_h_manual" in storage.LATE_BACKFILL
print("an upgrade moves what was typed into the manual column and empties the other")

# --- the slicer's weight fills a gap, and never overwrites the cloud's ------
store.upsert_print(job_id="sl-2", name="two.3mf", started_at=time.time() - 60,
                   ended_at=time.time(), final_state="FINISH", total_layers=10)
store.update_print_fields("sl-2", filament_g=61.5)
saved = gcode_meta.fetch
gcode_meta.fetch = lambda *a, **k: {"layer_h": 0.16, "grams": 99.9, "read_bytes": 100}
try:
    assert app._slicer_apply(store.get_print("sl-2"))
    assert store.get_print("sl-2")["filament_g"] == 61.5, (
        "the slicer's weight overwrote a figure that was already there - the two "
        "agree where both exist, so this must fill a gap, not compete")
    assert store.get_print("sl-2")["layer_h"] == 0.16
    # ...and it does fill the gap when there is one
    store.update_print_fields("sl-2", filament_g=None, slice_json=None)
    assert app._slicer_apply(store.get_print("sl-2"))
    assert store.get_print("sl-2")["filament_g"] == 99.9, \
        "no cloud figure, and the slicer's was not used either"
finally:
    gcode_meta.fetch = saved
print("the slicer's weight fills a gap and never overwrites one that is there")

# --- a print it cannot read is not retried for ever ------------------------
app._slicer_tries.clear()


def _always_fails(*a, **k):
    raise gcode_meta.SlicerError("no sliced file on the drive")


saved = gcode_meta.fetch
gcode_meta.fetch = _always_fails
try:
    store.upsert_print(job_id="sl-3", name="gone.3mf", started_at=time.time() - 60,
                       ended_at=time.time(), final_state="FINISH", total_layers=5)
    store.update_print_fields("sl-3", slice_json=None)
    for _ in range(5):
        app._slicer_apply(store.get_print("sl-3"))
    assert app._slicer_tries["sl-3"] >= app.SLICER_MAX_TRIES
    assert not any(r["job_id"] == "sl-3" for r in app._slicer_pending(limit=200)), (
        "a print whose file is gone stays in the queue, so every future print "
        "makes the app go and look for it again - that is a poll with extra steps")
finally:
    gcode_meta.fetch = saved
# but asking for it explicitly tries again
app.CONFIG.set("slicer.enabled", True)
r = c.post("/api/prints/slicer", json={"job_id": "sl-3"})
assert r.status_code in (200, 502), r.status_code
assert "sl-3" not in app._slicer_tries or app._slicer_tries["sl-3"] < 5, \
    "asking by hand did not reset the give-up count"
print("an unreadable file is dropped after a couple of goes, and retried on request")

# --- the endpoints -----------------------------------------------------------
assert c.post("/api/prints/slicer", json={"job_id": "nope"}).status_code == 404
app.CONFIG.set("slicer.enabled", False)
r = c.post("/api/prints/slicer", json={"job_id": "sl-1"})
assert r.status_code == 400 and "switched off" in r.get_json()["error"], (
    "with the reader switched off it went and talked to the printer anyway")
app.CONFIG.clear("slicer.enabled")
st = c.get("/api/slicer").get_json()
assert set(st) >= {"enabled", "last", "pending"}, st
print("the endpoints refuse an unknown print, and refuse to run when switched off")

# --- the page gets `slice`, not the raw column -----------------------------
rows = c.get("/api/prints").get_json()["prints"]
row = [r for r in rows if r["job_id"] == "sl-1"][0]
assert "slice_json" not in row, "the page is handed raw JSON to parse itself"
assert isinstance(row["slice"], dict) and row["slice"]["layer_h"] == 0.2, row.get("slice")
print("/api/prints serves the parsed block and hides the column it came from")

# --- and it is in the settings schema, so the wizard and page can show it ---
assert "Slicer" in settings_schema.GROUPS
assert settings_schema.BY_PATH["slicer.enabled"]["default"] is False, (
    "reading from the printer is on by default - a printer with no drive would "
    "log a failure after every print")
print("ok")
