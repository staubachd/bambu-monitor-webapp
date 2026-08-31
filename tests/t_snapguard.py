"""A print row must only ever gain detail, never lose it.

Two writers touch the same row and they know different things. The MQTT loop
runs every minute and knows the live state; the cloud pass runs every ten
minutes and knows the filament, the weights and the model title. The MQTT loop
knows nothing about the cloud's columns - and a blanket UPDATE writes NULL for
every column it was not given, which is how a job's filament data disappears a
minute after the cloud supplied it.

So `upsert_print` refuses to touch the columns it has no business writing
(PRINT_IMMUTABLE), and the cumulative figures are clamped upward: after a
restart the in-memory counters start at zero, and without the clamp the first
write would replace an hour of accumulated energy with 0.
"""
import sys, os, json, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import os as _os
SRC_DIR = (_os.environ.get("BAMBU_SRC")
           or _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import app, storage

store = app.store
JOB = "sg-1"
now = time.time()

# a print the cloud has enriched: weights, title, model link, a user label
store.upsert_print(job_id=JOB, name="bracket.3mf", started_at=now - 3600,
                   ended_at=None, final_state="RUNNING", total_layers=120,
                   energy_wh=180.0, cost=0.05, peak_w=140.0)
store.update_print_fields(
    JOB, design_title="Bracket v3", design_id="424242", profile_id="7",
    filament_g=61.5, filament_detail=json.dumps([{"grams": 61.5}]),
    filament_cost=1.53, ams_bambu=json.dumps({"1": True}), label="the good one",
    pgroup="Shelf", error_code="0300_1", filament_g_manual=60.0,
    ams_slots=json.dumps({"1": "GFA00"}))
rich = store.get_print(JOB)
assert rich["filament_g"] == 61.5 and rich["design_title"] == "Bracket v3"

# --- the MQTT loop writes again, knowing none of that ----------------------
store.upsert_print(job_id=JOB, name="bracket.3mf", started_at=now - 60,
                   ended_at=None, final_state="RUNNING", total_layers=120,
                   energy_wh=190.0, cost=0.06, peak_w=145.0)
after = store.get_print(JOB)

for col in sorted(storage.PRINT_IMMUTABLE - {"job_id"}):
    assert after[col] == rich[col], (
        f"{col} was {rich[col]!r} and is now {after[col]!r} - the MQTT loop does "
        f"not know this column and has overwritten it")
print(f"{len(storage.PRINT_IMMUTABLE) - 1} cloud- and user-owned columns survived "
      f"a write from the loop that knows nothing about them")

# and the columns it DOES own did move
assert after["energy_wh"] == 190.0 and after["peak_w"] == 145.0, after
print("the columns the loop owns were updated:", after["energy_wh"], "Wh")

# --- started_at is not one of the loop's to move ---------------------------
assert after["started_at"] == rich["started_at"], (
    "a restart mid-print rewrote when the job began, so its duration shrank")
print("started_at survived a write that carried a different one")

# --- the upward clamp, which lives in _persist_print -----------------------
app._print_row.update(job_id=JOB, started_at=rich["started_at"], peak_w=0.0,
                      seen_active=True, stored=dict(after), design_id=None,
                      profile_id=None, stale_err=None, carried_design=None)
app._job_energy.update(task_id=JOB, wh=0.0, last_ts=None)   # as after a restart

app._persist_print({"job": {"task_id": JOB, "state": "RUNNING", "name": "bracket.3mf",
                            "total_layers": 120},
                    "errors": {}, "power": {}, "ams": {}})
row = store.get_print(JOB)
assert row["energy_wh"] == 190.0, (
    f"energy fell to {row['energy_wh']} - the in-memory counter starts at zero "
    f"after a restart and must not replace what is stored")
assert row["peak_w"] == 145.0, f"peak fell to {row['peak_w']}"
assert app._job_energy["wh"] == 190.0, (
    "the live 'this print' figure was not brought back up to the stored total, "
    "so the dashboard and the history would disagree")
print("after a restart the stored totals hold, and the live counter is lifted to them")

# --- the end time is stamped once ------------------------------------------
app._persist_print({"job": {"task_id": JOB, "state": "FINISH", "name": "bracket.3mf"},
                    "errors": {}, "power": {}, "ams": {}})
first_end = store.get_print(JOB)["ended_at"]
assert first_end, "a finished print has no end time"
time.sleep(0.05)
app._persist_print({"job": {"task_id": JOB, "state": "FINISH", "name": "bracket.3mf"},
                    "errors": {}, "power": {}, "ams": {}})
assert store.get_print(JOB)["ended_at"] == first_end, (
    "the end time moved on a second write - the printer keeps reporting a "
    "finished job's id while it sits idle, so the duration would creep forever")
print("ended_at is stamped once and then left alone")

# --- and the immutable list still covers what the cloud owns ---------------
for col in ("filament_detail", "filament_cost", "design_title", "design_id",
            "ams_bambu", "label", "pgroup", "filament_g_manual", "error_code"):
    assert col in storage.PRINT_IMMUTABLE, (
        f"{col} is written by the cloud or by a person, but upsert_print may "
        f"now overwrite it with NULL")
print("every cloud- or user-owned column is still on the immutable list")

store.delete_print(JOB)
print("ok")
