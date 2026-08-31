"""Assigning a filament to an AMS slot: validation, and the exact MQTT payload."""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app

HERE = os.path.dirname(app.__file__)
c = app.app.test_client()

raw = json.load(open(os.path.join(HERE, "samples", "sample_report.json"), encoding="utf-8"))
s = app.parse_report(raw); app._enrich_ams(s)
with app._state_lock:
    app._state.update(s)
app._observe_filaments(s)

# name the third-party red spool, as the user would
fkey = "GFA00|F72323"
c.post("/api/filaments/identity", json={"fkey": fkey, "vendor": "Sunlu",
                                        "product": "PLA Matte", "color_name": "Red"})

class FakeMqtt:
    def __init__(self): self.sent = []
    def publish(self, topic, payload): self.sent.append(json.loads(payload))

# the feature ships OFF (firmware rejects it, HMS 0500_0500_0001_0007); check
# the gate, then switch it on to keep exercising the command builder
print("off by default ->", c.post("/api/ams/filament",
      json={"slot": 1, "fkey": fkey}).status_code)
# the gate is config now, and read at call time
app.CONFIG.set("filament.allow_slot_assign", True)
app._mqtt_client = None
print("not connected ->", c.post("/api/ams/filament",
      json={"slot": 1, "fkey": fkey}).status_code)

fake = FakeMqtt(); app._mqtt_client = fake
print("bad slot      ->", c.post("/api/ams/filament", json={"slot": 9, "fkey": fkey}).get_json())
print("no such fkey  ->", c.post("/api/ams/filament",
      json={"slot": 1, "fkey": "GFZ99|000000"}).status_code)
print("bad colour    ->", c.post("/api/ams/filament",
      json={"slot": 1, "type": "PLA", "color": "xyz"}).get_json())
print("odd material  ->", c.post("/api/ams/filament",
      json={"slot": 1, "type": "PEEK", "color": "112233"}).get_json())
print("min above max ->", c.post("/api/ams/filament",
      json={"slot": 1, "type": "PLA", "color": "112233",
            "nozzle_temp_min": 260, "nozzle_temp_max": 200}).get_json())
print("out of range  ->", c.post("/api/ams/filament",
      json={"slot": 1, "type": "PLA", "color": "112233",
            "nozzle_temp_min": 10, "nozzle_temp_max": 900}).get_json())

r = c.post("/api/ams/filament", json={"slot": 1, "fkey": fkey}).get_json()
print("\nassign slot 1 ->", r)
print("MQTT payload:")
print(json.dumps(fake.sent[-1], indent=2))

# a PETG identity must pick PETG's window, not PLA's
c.post("/api/filaments/identity", json={"fkey": "GFG00|057748", "vendor": "Sunlu",
                                        "product": "PETG", "color_name": "Green"})
r = c.post("/api/ams/filament", json={"slot": 2, "fkey": "GFG00|057748"}).get_json()
print("\nPETG slot 2 ->", r)
assert (r["nozzle_temp_min"], r["nozzle_temp_max"]) == (230, 270)
p = fake.sent[-1]["print"]
assert p["command"] == "ams_filament_setting" and p["tray_id"] == 1 and p["ams_id"] == 0
assert p["tray_color"].endswith("FF") and len(p["tray_color"]) == 8
print("ok")

app.CONFIG.clear("filament.allow_slot_assign")   # leave no override behind
