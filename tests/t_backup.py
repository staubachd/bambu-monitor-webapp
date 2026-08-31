"""Export everything worth keeping, and put it back.

The print history is the part nothing else in the world has a copy of. So the
export has to be complete in the way that matters, and the restore has to be
incapable of making things worse than they already are - which is the state
somebody is in when they reach for it.

Four things carry the weight:
  * a round trip through JSON returns the same data, bytes of a photo included
  * credentials are NOT in the file unless asked for, because a backup gets
    emailed, synced and copied to sticks
  * merge never destroys: restoring onto a database that has been used since
    cannot lose the newer work
  * replace destroys on purpose, and says exactly how much first
"""
import sys, os, json, time, base64, copy
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import os as _os
SRC_DIR = (_os.environ.get("BAMBU_SRC")
           or _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import app, backup, settings_schema

c = app.app.test_client()
store = app.store
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000002000100" "05fe02fe" "0000000049454e44ae426082")

# --- a database with something in every table ------------------------------
store.upsert_print(job_id="bk-1", name="bracket.3mf", started_at=time.time() - 3600,
                   ended_at=time.time(), final_state="FINISH", total_layers=120,
                   energy_wh=180.0, cost=0.05, peak_w=140.0)
store.update_print_fields("bk-1", label="the good one", filament_g=61.5,
                          filament_detail=json.dumps([{"grams": 61.5}]),
                          design_title="Bracket v3", design_id="424242")
store.upsert_filament("GFA00|BB1100", filament_id="GFA00", color="BB1100", type="PLA",
                      vendor="Sunlu", product="PLA Meta", color_name="Coffee")
store.set_filament_price("GFA00|BB1100", 18.99)
pid = store.add_purchase(code="A00-K0", product="PLA Basic", color_name="Black",
                         spools=2, grams_each=1000, total_price=49.98,
                         ordered_at=time.time(), type="PLA")
nid = store.add_note("Bed levelling", "every 20 prints", "Maintenance")
iid = store.add_note_image(nid, "image/png", PNG, 1, 1)
app.CONFIG.set("cost.price_per_kwh", 0.37)
app.CONFIG.set("access_code", "SECRET123")
store.ack_hms("0300_0100", "17500")

# --- the export -------------------------------------------------------------
data = backup.export(store)
raw = json.dumps(data)
for table in ("prints", "filaments", "purchases", "notes", "note_images",
              "settings", "hms_ack"):
    assert data["counts"][table] > 0, f"{table} is empty in the export"
print("exported:", backup.summarise(data))

# --- credentials are not in it ---------------------------------------------
assert "SECRET123" not in raw, (
    "the printer access code is in the backup file - a backup is a thing people "
    "email to themselves and drop in cloud folders")
assert data["omitted"]["secrets"] > 0, "nothing was reported as omitted"
assert "NOT in this file" in data["note"], "the file does not say what is missing from it"
# and every secret the schema knows about, not just that one
for path in settings_schema.SECRETS:
    assert f'"cfg.{path}"' not in raw, f"{path} is in the backup"
print(f"{data['omitted']['secrets']} credential(s) held back, and the file says so")

with_secrets = json.dumps(backup.export(store, include_secrets=True))
assert "SECRET123" in with_secrets, "--secrets did not include them"
assert "WARNING" in json.loads(with_secrets)["note"], \
    "a file that DOES contain passwords does not say so"
print("asking for them includes them, and stamps a warning in the file")

# --- telemetry is deliberately out -----------------------------------------
assert "telemetry" not in data["tables"], "the telemetry table is in the backup"
assert "telemetry" in data["excluded"], \
    "telemetry is missing with no explanation in the file"
print("telemetry is excluded, and the file says why")

# --- the disaster: restore into an empty database --------------------------
before = {t: store.dump_table(t) for t, _ in backup.TABLES}
for t, _ in backup.TABLES:
    store.clear_table(t)
assert store.count_rows("prints") == 0, "the fixture did not actually empty"

report = backup.restore(store, json.loads(raw), mode="merge")
assert report["skipped"] == 0 and report["deleted"] == 0, report
print(f"restored into an empty database: {report['inserted']} row(s)")

# and it is the SAME data, not merely the same shape
after = {t: store.dump_table(t) for t, _ in backup.TABLES}
row = [r for r in after["prints"] if r["job_id"] == "bk-1"][0]
assert row["label"] == "the good one", row
assert row["design_id"] == "424242" and row["filament_g"] == 61.5, row
f = [r for r in after["filaments"] if r["fkey"] == "GFA00|BB1100"][0]
assert f["product"] == "PLA Meta" and f["price_per_kg"] == 18.99, f
p = after["purchases"][0]
assert p["total_price"] == 49.98 and p["spools"] == 2, p
n = after["notes"][0]
assert n["title"] == "Bed levelling" and n["category"] == "Maintenance", n
print("the print's label, the filament's price, the purchase and the note all came back")

# the photo survived base64 both ways
mime, blob = store.get_note_image(after["note_images"][0]["id"])
assert blob == PNG, (f"the picture came back as {len(blob) if blob else 0} bytes "
                     f"instead of {len(PNG)} - base64 round trip is broken")
assert mime == "image/png"
print(f"the {len(blob)}-byte photo is byte-identical after the round trip")

# settings that are not secrets came back, and the secret did not
app.CONFIG.reload()
assert app.CONFIG.get("cost.price_per_kwh") == 0.37, "a setting was lost"
assert not app.CONFIG.get("access_code"), \
    "the access code came back from a backup that was not supposed to contain it"
print("settings restored; the access code is still gone, as promised")

# --- merge never destroys ---------------------------------------------------
store.upsert_print(job_id="bk-new", name="printed since the backup",
                   started_at=time.time(), final_state="RUNNING", total_layers=1)
store.set_print_label("bk-1", "edited since the backup")
r2 = backup.restore(store, json.loads(raw), mode="merge")
assert r2["deleted"] == 0, "merge deleted something"
assert store.get_print("bk-new") is not None, (
    "a print made after the backup was taken did not survive the restore")
assert store.get_print("bk-1")["label"] == "edited since the backup", (
    "merge overwrote a row that was already there - it is supposed to leave "
    "everything it finds alone")
pt = r2["tables"]["prints"]
assert pt["inserted"] == 0 and pt["skipped"] == pt["in_file"], (
    f"merge wrote {pt['inserted']} print(s) that were already there: {pt}")
print("merge onto a used database: newer work kept, existing rows untouched")

# --- replace destroys, on purpose, and says so first -----------------------
dry = backup.restore(store, json.loads(raw), mode="replace", dry_run=True)
assert dry["deleted"] > 0, "a replace that deletes nothing is not being reported"
assert store.get_print("bk-new") is not None, "the dry run deleted something"
print(f"replace, dry run: would delete {dry['deleted']} row(s) and write "
      f"{dry['inserted']} - and wrote nothing")

real = backup.restore(store, json.loads(raw), mode="replace")
assert store.get_print("bk-new") is None, "replace kept a row that is not in the file"
assert store.get_print("bk-1")["label"] == "the good one", \
    "replace did not put the file's version back"
assert real["deleted"] == dry["deleted"], (
    f"the dry run promised {dry['deleted']} deletions and the real one did "
    f"{real['deleted']} - the preview has to be the truth")
print(f"replace for real: {real['deleted']} deleted, {real['inserted']} written, "
      f"exactly as the dry run said")

# --- what it refuses --------------------------------------------------------
for bad, why in [
    ({"app": "something-else", "format": 1, "tables": {}}, "another app's file"),
    ({"app": "bambu-monitor", "tables": {}}, "no format version"),
    ({"app": "bambu-monitor", "format": 999, "tables": {}}, "a newer format"),
    ({"app": "bambu-monitor", "format": 1}, "no tables"),
    ({"app": "bambu-monitor", "format": 1, "tables": {"passwords": []}}, "an unknown table"),
    ("not even a dict", "a string"),
]:
    assert backup.check(bad), f"{why} was accepted as a backup"
print("refused: another app's file, a missing or future format, an unknown table")

# --- and the endpoints agree with the module -------------------------------
# the earlier restore deliberately did NOT bring the access code back, so put
# one there again before checking that ?secrets=1 can reach it
app.CONFIG.set("access_code", "SECRET123")
r = c.get("/api/backup")
assert r.status_code == 200 and "attachment" in r.headers.get("Content-Disposition", "")
assert ".json" in r.headers["Content-Disposition"]
served = json.loads(r.data)
assert served["app"] == "bambu-monitor" and served["counts"]["prints"] > 0
assert "SECRET123" not in r.data.decode(), "the download leaked the access code"
assert "SECRET123" in c.get("/api/backup?secrets=1").data.decode(), \
    "?secrets=1 did not include them"
assert json.loads(c.get("/api/backup?images=0").data)["counts"]["note_images"] == 0
print("GET /api/backup: attachment, no credentials by default, ?secrets=1 and ?images=0 work")

rr = c.post("/api/backup/restore", json={"backup": served, "dry": True}).get_json()
assert rr["ok"] and rr["dry_run"] and rr["inserted"] == 0, rr
assert c.post("/api/backup/restore", json={"backup": {"app": "x"}}).status_code == 400
assert c.post("/api/backup/restore", json={}).status_code == 400
print("POST /api/backup/restore: dry run reports without writing; junk is refused")

# --- and a file that names a column this database does not have ------------
# A backup from a newer version must restore into an older one rather than
# failing on the first row.
future = json.loads(raw)
for r_ in future["tables"]["prints"]:
    r_["a_column_from_the_future"] = "x"
store.clear_table("prints")
rep = backup.restore(store, future, mode="merge")
assert rep["tables"]["prints"]["inserted"] > 0, (
    "a backup carrying an unknown column restored nothing - a newer version's "
    "file has to be readable by an older one")
print("a backup with an unknown column still restores what this version knows")

for t, rows in before.items():
    store.clear_table(t)
    store.insert_rows(t, rows)
app.CONFIG.reload()
print("ok")
