"""gcode_line commands are gated by firmware verification, so they ship off."""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app

c = app.app.test_client()

class FakeMqtt:
    def __init__(self): self.sent = []
    def publish(self, topic, payload): self.sent.append(json.loads(payload)["print"])
app._mqtt_client = FakeMqtt()

print("default:", "allow_gcode =", app.CTRL_GCODE())
for body in ({"action": "temp", "target": "bed", "value": 60},
             {"action": "temp", "target": "chamber", "value": 40},
             {"action": "fan", "fan": "cooling", "percent": 50}):
    r = c.post("/api/print/control", json=body)
    print(f"   {str(body):58} -> {r.status_code}")
    assert r.status_code == 403, "a gated command must not be published"
assert not app._mqtt_client.sent, "nothing may reach the printer while it is off"

# the commands that have never raised the warning still work
for body in ({"action": "pause"}, {"action": "speed", "param": "2"}):
    r = c.post("/api/print/control", json=body)
    print(f"   {str(body):58} -> {r.status_code}")
    assert r.status_code == 200
print("   published:", [p.get("command") for p in app._mqtt_client.sent])

print("\nwith controls.allow_gcode = true:")
app.CONFIG.set("controls.allow_gcode", True)
app._mqtt_client = FakeMqtt()
for body in ({"action": "temp", "target": "bed", "value": 60},
             {"action": "temp", "target": "chamber", "value": 40},
             {"action": "fan", "fan": "cooling", "percent": 50}):
    r = c.post("/api/print/control", json=body)
    print(f"   {str(body):58} -> {r.status_code}")
    assert r.status_code == 200
print("   published:", [p.get("param") for p in app._mqtt_client.sent])
app.CONFIG.set("controls.allow_gcode", False)

# the flag reaches the browser so the UI can hide what cannot work
raw = json.load(open(os.path.join(os.path.dirname(app.__file__), "samples",
                                  "sample_report.json"), encoding="utf-8"))
s = app.parse_report(raw)
s["controls"] = {"gcode": app.CTRL_GCODE()}
print("\nstate.controls ->", s["controls"])
print("ok")

app.CONFIG.clear("controls.allow_gcode")   # leave no override behind
