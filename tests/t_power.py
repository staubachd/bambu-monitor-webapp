"""The MQTT power meter, and the switch that chooses it.

An IKEA INSPELNING is Zigbee: it has no address, so nothing polls it, and the
reading comes from whatever bridge it is paired to. That makes two things
different from the Tapo path, and both are easy to get wrong:

  - the reading is PUSHED, so "connected" and "working" are not the same thing;
    a wrong topic is silent, and silence must be reported rather than shown as
    an absence of consumption
  - the plug reports one counter that only goes up, where Tapo reports "today"
    and "this month" directly - so those windows are differences from a
    baseline, and the baseline has to survive a restart without being written
    to the database on every message

No IKEA hardware was involved in writing this. The provider is driven here by
handing it the payloads Zigbee2MQTT, Tasmota and Shelly actually publish.
"""
import sys, os, json
from datetime import datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import power_providers as pp
import settings_schema


class Driver:
    """An MqttProvider with its clock and its storage in our hands."""

    def __init__(self, **cfg):
        self.reported = {}
        self.saves = []
        self.logs = []
        self.stored = {}
        base = dict(host="b", topic="zigbee2mqtt/printer",
                    watts_field="power", energy_field="energy", energy_unit="kWh")
        base.update(cfg)
        self.p = pp.MqttProvider(
            cfg=base, report=lambda **f: self.reported.update(f),
            active=lambda: True, log=self.logs.append,
            state_io=(lambda: self.stored,
                      lambda d: (self.saves.append(d), self.stored.update(d))))
        self.p._energy = dict(self.stored)
        self.p._seen = False

    def send(self, payload, topic="zigbee2mqtt/printer"):
        """Hand the provider a message exactly as paho would."""
        if isinstance(payload, bytes):
            raw = payload
        elif isinstance(payload, str):
            raw = payload.encode()
        else:
            raw = json.dumps(payload).encode()
        self.p.handle(topic, raw)      # the real handler, not a copy of it


# --- the payloads these bridges really publish ------------------------------
d = Driver()
d.send({"power": 42.5, "energy": 13.84, "state": "ON", "linkquality": 120})
assert d.reported["watts"] == 42.5, d.reported
assert d.reported["error"] is None
print("Zigbee2MQTT payload ->", d.reported["watts"], "W")

# Tasmota nests its reading several levels down; dotted paths are why
d2 = Driver(watts_field="StatusSNS.ENERGY.Power", energy_field="StatusSNS.ENERGY.Total")
d2.send({"StatusSNS": {"ENERGY": {"Power": 61, "Total": 2.5}}})
assert d2.reported["watts"] == 61.0, d2.reported
assert d2.reported["today_wh"] == 0.0, "the first reading is the baseline, so today is 0"
print("nested Tasmota payload ->", d2.reported["watts"], "W")

# a bare number on its own topic, which some bridges publish
d3 = Driver()
d3.send("77.5")
assert d3.reported["watts"] == 77.5, d3.reported
print("a bare value with no JSON ->", d3.reported["watts"], "W")

# a comma decimal, because some publishers are localised
d4 = Driver()
d4.send({"power": "18,25"})
assert d4.reported["watts"] == 18.25, d4.reported
print("a comma decimal is read as a number")

# --- a wrong field name must say so, not look like zero consumption --------
d5 = Driver(watts_field="watts")
d5.send({"power": 42.5, "energy": 1.0})
assert d5.reported.get("watts") is None, "a missing field was reported as a reading"
assert "no 'watts'" in d5.reported["error"], d5.reported
assert "power" in d5.reported["error"], "it does not say which fields ARE there"
print("a wrong field name ->", d5.reported["error"][:76])

# --- the energy windows -----------------------------------------------------
d6 = Driver()
d6.send({"power": 10, "energy": 5.000})          # 5 kWh on the counter
assert d6.reported["today_wh"] == 0.0, "the first reading should be the baseline"
d6.send({"power": 10, "energy": 5.250})          # +250 Wh
assert abs(d6.reported["today_wh"] - 250.0) < 1e-6, d6.reported
assert abs(d6.reported["month_wh"] - 250.0) < 1e-6, d6.reported
print("a rising counter -> today", d6.reported["today_wh"], "Wh")

# Wh, not kWh
d7 = Driver(energy_unit="Wh")
d7.send({"power": 10, "energy": 5000})
d7.send({"power": 10, "energy": 5250})
assert abs(d7.reported["today_wh"] - 250.0) < 1e-6, d7.reported
print("the unit is honoured: Wh gives the same 250 Wh as kWh did")

# a new day rebases today but not the month
d8 = Driver()
d8.send({"power": 10, "energy": 5.0})
d8.send({"power": 10, "energy": 5.4})
d8.p._energy["day"] -= 1                          # pretend midnight passed
d8.send({"power": 10, "energy": 5.5})
assert abs(d8.reported["today_wh"] - 0.0) < 1e-6, "today did not restart"
assert abs(d8.reported["month_wh"] - 500.0) < 1e-6, \
    f"the month restarted with the day: {d8.reported['month_wh']}"
print("midnight restarts today and leaves the month running")

# --- a counter that goes backwards ------------------------------------------
d9 = Driver()
d9.send({"power": 10, "energy": 9.0})
d9.send({"power": 10, "energy": 9.5})
d9.send({"power": 10, "energy": 0.2})             # plug reset / re-paired
assert d9.reported["today_wh"] >= 0, f"a reset produced a negative day: {d9.reported}"
assert d9.reported["today_wh"] == 0.0, d9.reported
d9.send({"power": 10, "energy": 0.3})
assert abs(d9.reported["today_wh"] - 100.0) < 1e-6, "it did not start counting again"
assert any("backwards" in m for m in d9.logs), "the reset was not mentioned once"
print("a counter reset rebases instead of reporting a negative day")

# --- the baseline is persisted, but not on every message -------------------
d10 = Driver()
for i in range(200):
    d10.send({"power": 10, "energy": 1.0 + i / 1000.0})
assert len(d10.saves) <= 3, (
    f"{len(d10.saves)} database writes for 200 readings - a plug publishing every "
    f"few seconds would then write constantly, which is what stops the NAS disks "
    f"from sleeping")
print(f"200 readings -> {len(d10.saves)} state write(s)")

# and it survives a restart
before = dict(d10.stored)
resumed = Driver()
resumed.stored = dict(before)
resumed.p._energy = dict(before)
resumed.send({"power": 10, "energy": 1.0 + 199 / 1000.0 + 0.05})
assert abs(resumed.reported["today_wh"] - d10.reported["today_wh"] - 50.0) < 1e-3, (
    f"after a restart today jumped: {resumed.reported['today_wh']} vs "
    f"{d10.reported['today_wh']}")
print("after a restart today continues from where it was, not from zero")

# --- the provider registry and the switch ----------------------------------
assert set(pp.PROVIDERS) == {"tapo", "mqtt"}, sorted(pp.PROVIDERS)
opts = settings_schema.BY_PATH["power.provider"]["options"]
assert set(opts) == set(pp.PROVIDERS), (
    f"the Settings page offers {opts} but the code has {sorted(pp.PROVIDERS)}")
print("every provider in the code is offered in Settings, and vice versa")

# a half-configured meter names what is missing rather than raising
m = pp.MqttProvider(cfg={"host": "b"}, report=lambda **k: None, active=lambda: True)
assert m.missing() == ["Topic"], m.missing()
m2 = pp.MqttProvider(cfg={}, report=lambda **k: None, active=lambda: True)
assert set(m2.missing()) == {"Broker address", "Topic"}, m2.missing()
t = pp.TapoProvider(cfg={"host": "1.2.3.4"}, report=lambda **k: None, active=lambda: True)
assert set(t.missing()) == {"Tapo account", "Tapo password"}, t.missing()
print("each meter says which of ITS settings are missing")

# --- every provider's fields are in the schema and hide with the switch -----
for name in pp.PROVIDERS:
    if name == "tapo":
        paths = [p for p in settings_schema.BY_PATH if p.startswith("power.")
                 and not p.startswith("power.mqtt") and p not in ("power.enabled",
                                                                  "power.provider")]
    else:
        paths = [p for p in settings_schema.BY_PATH if p.startswith(f"power.{name}.")]
    assert paths, f"{name} has no settings at all"
    for p in paths:
        cond = settings_schema.BY_PATH[p].get("show_if")
        assert cond == ("power.provider", name), (
            f"{p} belongs to {name} but is shown when the meter is "
            f"{cond[1] if cond else 'anything'}")
print("each meter's settings are hidden unless that meter is chosen")
print("ok")
