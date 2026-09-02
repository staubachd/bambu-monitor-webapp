"""What is on the printer's USB drive, and how full it is.

Two questions with two different answers, and the whole point is not to blend
them:

  * **how full** comes from MQTT. It is the only source: FTP can list a drive
    but has no command for its capacity - this server advertises neither AVBL
    nor SITE - so adding up file sizes would say how much we can SEE, never how
    much is there. On the real drive those differ by 6 GB.
  * **what is on it** comes from FTP, on demand.

Either half can be missing without taking the other with it: a printer with no
drive still reports its internal flash, and a drive that will not list still has
a size. And a drive is somebody else's filesystem, so the walk is bounded.
"""
import sys, os, json, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import os as _os
SRC_DIR = (_os.environ.get("BAMBU_SRC")
           or _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import app, gcode_meta
from bambu_state import parse_report

c = app.app.test_client()
store = app.store


class FakeFTP:
    """A directory tree, served the way vsFTPd serves one."""

    def __init__(self, tree, fail=None):
        self.tree = tree           # path -> list of (perms, size, name)
        self.fail = fail or set()
        self.listed = []

    def retrlines(self, cmd, cb):
        assert cmd.startswith("LIST -a "), (
            f"{cmd!r}: a plain LIST of an empty directory and a LIST that failed "
            f"look identical, which is why -a is used")
        path = cmd[len("LIST -a "):]
        self.listed.append(path)
        if path in self.fail:
            raise OSError("550 Failed to open directory")
        for perms, size, name in self.tree.get(path, []):
            cb(f"{perms}    2 103      107      {size:>10} Jul 23 04:13 {name}")

    def close(self):
        pass


TREE = {
    "/": [("-rw-r--r--", 3832815, "Herbstliebe_Oberteil.gcode.3mf"),
          ("-rw-r--r--", 4365743, "Fall in Love_Riffel.gcode.3mf"),
          ("-rw-r--r--", 120, "readme.txt"),
          ("drwxr-xr-x", 4096, "timelapse"),
          ("drwxr-xr-x", 4096, ".")],
    "/timelapse": [("-rw-r--r--", 90000000, "video_001.mp4"),
                   ("-rw-r--r--", 80000000, "video_002.mp4"),
                   ("drwxr-xr-x", 4096, "thumb")],
    "/timelapse/thumb": [("-rw-r--r--", 5000, "t1.jpg")],
}

# --- the listing ------------------------------------------------------------
ftp = FakeFTP(TREE)
files, truncated = gcode_meta.list_drive(ftp)
assert not truncated
names = [f["name"] for f in files]
assert len(files) == 6, names
assert names[0] == "video_001.mp4", f"not sorted biggest first: {names}"
assert "." not in names and ".." not in names, "the dot entries were listed as files"
by = {f["name"]: f for f in files}
assert by["t1.jpg"]["dir"] == "/timelapse/thumb", by["t1.jpg"]
assert by["Herbstliebe_Oberteil.gcode.3mf"]["dir"] == "/"
assert by["Herbstliebe_Oberteil.gcode.3mf"]["path"] == "/Herbstliebe_Oberteil.gcode.3mf"
# a sliced job is what somebody is looking for; the rest is housekeeping
assert by["Herbstliebe_Oberteil.gcode.3mf"]["sliced"] is True
assert by["video_001.mp4"]["sliced"] is False and by["readme.txt"]["sliced"] is False
assert by["video_001.mp4"]["when"] == "Jul 23 04:13", by["video_001.mp4"]
print(f"walked {len(files)} files across {len(TREE)} folders, biggest first")

# --- a folder that will not open is not a failed listing -------------------
ftp = FakeFTP(TREE, fail={"/timelapse"})
files, _ = gcode_meta.list_drive(ftp)
assert len(files) == 3, (
    "one unreadable folder took the whole listing with it; the files that could "
    "be read are still worth showing")
print("an unreadable folder is skipped, not fatal")

# --- the walk is bounded, and says when it stopped -------------------------
deep = {"/": [("drwxr-xr-x", 4096, "a")]}
for i, p in enumerate(["/a", "/a/b", "/a/b/c", "/a/b/c/d"]):
    deep[p] = [("drwxr-xr-x", 4096, "bcde"[i]), ("-rw-r--r--", 1, f"f{i}.txt")]
files, truncated = gcode_meta.list_drive(FakeFTP(deep))
assert truncated, "it walked past its depth limit without saying so"
assert all("/a/b/c/d" not in f["dir"] for f in files), \
    f"went deeper than max_depth: {[f['dir'] for f in files]}"
wide = {"/": [("-rw-r--r--", i + 1, f"f{i}.bin") for i in range(50)]}
files, truncated = gcode_meta.list_drive(FakeFTP(wide), max_entries=10)
assert len(files) == 10 and truncated, (
    f"{len(files)} files with a cap of 10, truncated={truncated} - a list cut "
    f"short without saying so reads as a complete one")
print("depth and count are capped, and a shortened list says that it is")

# --- how full: only MQTT knows ----------------------------------------------
with open(os.path.join(SRC_DIR, "samples", "sample_report.json"), encoding="utf-8") as fh:
    sample = json.load(fh)
st = parse_report(sample)["storage"]
assert st["present"] is False, "the sample was captured with no drive in"
assert st["external"] is None, (
    "a drive that is not there reported a size; 0 free and 'no drive' are "
    "different things and the page has to be able to tell them apart")
assert st["internal"]["free_kb"] == 838525, st["internal"]

# the same report, with a drive in it, exactly as the printer sends it
withdrive = json.loads(json.dumps(sample))
p = withdrive.get("print", withdrive)
p["sdcard"] = True
p.setdefault("ipcam", {}).update(tl_external_total_kb=29897088,
                                 tl_external_free_kb=23592640)
st = parse_report(withdrive)["storage"]
assert st["present"] is True
assert st["external"]["total_kb"] == 29897088
assert st["external"]["used_kb"] == 29897088 - 23592640, st["external"]
print(f"printer reports {st['external']['used_kb'] / 1024 / 1024:.1f} GB used of "
      f"{st['external']['total_kb'] / 1024 / 1024:.1f} GB")

# --- the endpoint ties files back to the prints that made them -------------
store.delete_print("drv-1")      # so a re-run starts where the first one did
store.upsert_print(job_id="drv-1", name="Herbstliebe_Oberteil",
                   started_at=time.time() - 3600, ended_at=time.time(),
                   final_state="FINISH", total_layers=100)
store.update_print_fields("drv-1", slice_json=json.dumps(
    {"plate_name": "Oberteile Herz", "v": gcode_meta.FORMAT}))

_connect, _list = gcode_meta.connect, gcode_meta.list_drive
gcode_meta.connect = lambda *a, **k: FakeFTP(TREE)
gcode_meta.list_drive = lambda ftp, *a, **k: (files_fixture, False)
files_fixture, _ = _list(FakeFTP(TREE))
try:
    app._drive_cache.update(at=0.0, data=None)
    with app._state_lock:
        app._state["storage"] = st
    d = c.get("/api/storage").get_json()
    assert (d["storage"].get("external") or {}).get("total_kb") == 29897088, (
        "the capacity did not come from the printer's own report - it is the "
        "only source, and adding up file sizes can only say what we can see")
    assert d["listed_bytes"] == sum(f["size"] for f in files_fixture)
    assert d["sliced_bytes"] == 3832815 + 4365743, d["sliced_bytes"]
    got = {f["name"]: f for f in d["files"]}
    assert got["Herbstliebe_Oberteil.gcode.3mf"]["job_id"] == "drv-1", (
        "a file on the drive was not tied back to the print it made, so the "
        "list is about a filesystem rather than about the workshop")
    # the plate name is the better label when there is no user label
    assert got["Herbstliebe_Oberteil.gcode.3mf"]["print"] == "Oberteile Herz"
    store.set_print_label("drv-1", "the good one")
    app._drive_cache.update(at=0.0, data=None)
    d2 = c.get("/api/storage").get_json()
    assert [f for f in d2["files"]
            if f["name"] == "Herbstliebe_Oberteil.gcode.3mf"][0]["print"] == "the good one", \
        "a label typed by hand lost to the plate name"
    assert "job_id" not in got["video_001.mp4"], "a timelapse was matched to a print"
    assert "job_id" not in got["Fall in Love_Riffel.gcode.3mf"], \
        "a file with no matching print was given one anyway"
    print("files are tied to their prints; a hand-typed label wins over the plate name")

    # --- the listing is cached, the capacity is not ------------------------
    calls = []
    gcode_meta.list_drive = lambda ftp, *a, **k: (calls.append(1), (files_fixture, False))[1]
    app._drive_cache.update(at=0.0, data=None)
    c.get("/api/storage"); c.get("/api/storage"); c.get("/api/storage")
    assert len(calls) == 1, (
        f"{len(calls)} connections for three page loads - switching to the view "
        f"and back should not dial the printer each time")
    c.get("/api/storage?refresh=1")
    assert len(calls) == 2, "the refresh button did not force a new listing"
    print("the listing is cached for a moment; Refresh forces a new one")

    # --- a printer that cannot be reached still answers --------------------
    def _boom(*a, **k):
        raise gcode_meta.SlicerError("cannot reach the printer's file store")
    gcode_meta.connect = _boom
    app._drive_cache.update(at=0.0, data=None)
    d = c.get("/api/storage").get_json()
    assert d["error"] and d["files"] == [], d
    assert d["storage"]["external"]["total_kb"] == 29897088, (
        "the drive could not be listed, and the capacity went with it - they "
        "come from different places and fail separately")
    print("a drive that will not list still reports its size, and says why")
finally:
    gcode_meta.connect, gcode_meta.list_drive = _connect, _list
    app._drive_cache.update(at=0.0, data=None)

store.delete_print("drv-1")
print("ok")
