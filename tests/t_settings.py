"""Every setting comes from the database; only the connection does not.

There used to be a file layer underneath. There is not any more, so a value is
either one somebody set or the default declared beside the setting in the
schema. What matters is that a stored value wins, that clearing one hands the
default back rather than storing a copy of it, that a change reaches running
code without a restart, that nothing outside the schema can be written - the
connection least of all - and that a secret never leaves the server.
"""
# The app source, relative to this file. These tests used to sit inside the
# source folder and could name it directly; they live beside it now, so that
# they survive a temp-directory clean-out.
import os as _os
SRC_DIR = (_os.environ.get("BAMBU_SRC")
           or _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
def _src(name):
    return _os.path.join(SRC_DIR, name)
import sys, os, json, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app, bootstrap, config_store, settings_schema

c = app.app.test_client()
CONFIG = app.CONFIG

# start from a clean slate: nothing set, so everything is at its default
for k in list(CONFIG.overrides()):
    CONFIG.clear(k)

code_kwh = settings_schema.BY_PATH["cost.price_per_kwh"]["default"]
print("with nothing set, price_per_kwh =", CONFIG.get("cost.price_per_kwh"),
      "(the schema's default)")
assert CONFIG.get("cost.price_per_kwh") == code_kwh, "the declared default is not in force"
assert not CONFIG.overridden("cost.price_per_kwh")

# --- an override wins, and reaches code that captured the section at import ---
r = c.post("/api/settings", json={"changes": {"cost.price_per_kwh": "0,42"}})
d = r.get_json()
print("save ->", d)
assert d["ok"] and d["restart_needed"] == [], d
assert CONFIG.get("cost.price_per_kwh") == 0.42, CONFIG.get("cost.price_per_kwh")
# COST_CFG was bound at import; it must still see the new value
assert app.COST_CFG.get("price_per_kwh") == 0.42, "the live section is a snapshot after all"
assert app._cost_block()["price_per_kwh"] == 0.42, "the cost block did not follow"
print("live section and _cost_block both followed the change")

# a deep override must not wipe its siblings
before = dict(CONFIG.get("filament.bambu"))
c.post("/api/settings", json={"changes": {"filament.low_pct": 25}})
assert CONFIG.get("filament.bambu") == before, "overriding low_pct wiped the price table"
assert app.FIL_LOW_PCT() == 25.0, app.FIL_LOW_PCT()
print("a sibling key survived a deep override; FIL_LOW_PCT() is live")

# --- clearing drops the stored value rather than storing a copy of the default,
#     so a later change to the default still reaches an untouched install ---
c.post("/api/settings", json={"changes": {"cost.price_per_kwh": None}})
assert not CONFIG.overridden("cost.price_per_kwh"), "the stored value was not dropped"
assert CONFIG.get("cost.price_per_kwh") == code_kwh, "the default did not come back"
assert "cost.price_per_kwh" not in CONFIG.overrides(),     "reset wrote the default into the database instead of removing the row"
print("cleared -> back to the declared default", code_kwh)

# --- the schema is the gate ---
for bad, why in [({"storage.backend": "sqlite"}, "the database connection"),
                 ({"storage.mariadb.password": "x"}, "a database password"),
                 ({"storage.sqlite_path": "/tmp/x.db"}, "the database file"),
                 ({"nonsense.key": 1}, "an unknown key"),
                 ({"filament.low_pct": 900}, "out of range"),
                 ({"cost.price_per_kwh": "abc"}, "not a number")]:
    rr = c.post("/api/settings", json={"changes": bad})
    assert rr.status_code == 400, f"{why} was accepted: {bad}"
print("refused: db connection, db password, unknown key, out of range, not a number")

# a bad field in a batch must not leave the good ones applied
kwh_before = CONFIG.get("cost.price_per_kwh")
rr = c.post("/api/settings", json={"changes": {"cost.price_per_kwh": 0.9,
                                               "filament.low_pct": 900}})
assert rr.status_code == 400
assert CONFIG.get("cost.price_per_kwh") == kwh_before, "half the batch was applied"
print("a batch with one bad field changed nothing")

# --- secrets ---
payload = c.get("/api/settings").get_json()
by = {s["path"]: s for s in payload["settings"]}
for path in settings_schema.SECRETS:
    assert by[path]["value"] is None, f"{path} was sent to the browser"
    assert "is_set" in by[path], f"{path} does not say whether it is set"
assert json.dumps(payload).find(CONFIG.get("access_code") or "\x00") == -1, \
    "an access code appears somewhere in the payload"
print(f"{len(settings_schema.SECRETS)} secrets: value withheld, only is_set reported")

# a blank secret means "leave it", not "erase it"
was = CONFIG.get("access_code")
c.post("/api/settings", json={"changes": {"access_code": ""}})
assert CONFIG.get("access_code") == was, "a blank box erased the access code"
c.post("/api/settings", json={"changes": {"access_code": "12345678"}})
assert CONFIG.get("access_code") == "12345678", "a secret could not be set"
CONFIG.clear("access_code")
print("a blank secret leaves the stored one alone; a filled one replaces it")

# --- restart-only settings are reported as such, not silently ignored ---
d = c.post("/api/settings", json={"changes": {"camera.rtsp_port": 555}}).get_json()
assert d["restart_needed"] == ["camera.rtsp_port"], d
CONFIG.clear("camera.rtsp_port")
print("a restart-only change says so:", d["restart_needed"])

# --- the storage block is read from the file, whatever the database says ---
app.store.set_setting(config_store.PREFIX + "storage.backend", json.dumps("mariadb"))
CONFIG.reload()
assert app.STORE_CFG.get("backend") != "mariadb", \
    "a database row redirected the database connection"
app.store.delete_setting(config_store.PREFIX + "storage.backend")
CONFIG.reload()
print("the storage block ignores the database, as it must")

# --- the page must show what the app DOES ---------------------------------
# A setting nobody has touched still has a value: the code falls back to one.
# The page used to render that as blank or unticked, which said the opposite of
# the truth for "Price from imported invoices" - never stored, on in the code,
# shown as off.
#
# The exceptions are the handful that genuinely have no answer until somebody
# gives one. They are listed rather than derived, so adding a new blank setting
# is a deliberate act and not something that happens by forgetting a default.
NO_ANSWER_UNTIL_ASKED = {
    "ip", "serial", "access_code",          # which printer this is
    "power.host", "power.email", "power.password",          # a Tapo plug
    "power.mqtt.host", "power.mqtt.topic", "power.mqtt.password",   # a broker
    "cloud.email", "cloud.password", "cloud.token",
}
blank = {s2["path"] for s2 in settings_schema.SCHEMA
         if "default" not in s2 and not CONFIG.get(s2["path"])}
assert blank == NO_ANSWER_UNTIL_ASKED, (
    "settings that show blank with nothing behind them: "
    f"{sorted(blank - NO_ANSWER_UNTIL_ASKED)}; "
    f"listed but no longer blank: {sorted(NO_ANSWER_UNTIL_ASKED - blank)}")
# and every one of those is something the wizard asks for, so a fresh install
# is never left with a blank the user was never prompted about
import setup_wizard
asked = {f["path"] for f in setup_wizard._fields()}
assert NO_ANSWER_UNTIL_ASKED <= asked,     f"the wizard never asks for: {sorted(NO_ANSWER_UNTIL_ASKED - asked)}"
print(f"{len(blank)} settings are blank until asked for, and the wizard asks for all of them")

by = {x["path"]: x for x in c.get("/api/settings").get_json()["settings"]}
checks = [
    ("filament.prices_from_orders", lambda: app.PRICES_FROM_ORDERS()),
    ("filament.low_pct",            lambda: app.FIL_LOW_PCT()),
    ("filament.store_region",       lambda: app.FIL_STORE_REGION()),
    ("filament.allow_slot_assign",  lambda: app.AMS_ASSIGN()),
    ("controls.allow_gcode",        lambda: app.CTRL_GCODE()),
    ("storage.sample_interval_sec", lambda: app.SAMPLE_INTERVAL()),
    ("storage.auto_tail_min",       lambda: app.AUTO_TAIL_SEC() / 60),
]
for path, actual in checks:
    shown, real = by[path]["value"], actual()
    print(f"  {path:<30} page {str(shown):<7} code {real}")
    assert float(shown) == float(real) if isinstance(real, (int, float)) and not isinstance(real, bool)         else shown == real, f"{path}: the page shows {shown!r} but the code does {real!r}"
print("what the page shows is what the code does, for every value with a fallback")

# --- the connection is shown, never edited, and never in full ---------------
payload = c.get("/api/settings").get_json()
conn = payload["connection"]
assert conn, "the page cannot say where the app is storing anything"
assert conn["backend"] in ("sqlite", "mariadb"), conn
assert not any(p.startswith("storage.") and p.split(".")[1] in
               ("backend", "sqlite_path", "mariadb")
               for p in settings_schema.BY_PATH),     "a connection key is editable through the settings API"
if conn["backend"] == "mariadb":
    real = bootstrap.load()["mariadb"]["password"]
    assert real not in json.dumps(payload), "the database password was sent to the browser"
print(f"connection reported as {conn['backend']}, and no part of it is writable here")

# The page is German by default. Every label and help line the schema carries is
# rendered through t(), so a setting added without a translation would show up as
# a lone English row in an otherwise German page.
_html = open(_src("dashboard.html"), encoding="utf-8").read()
_start = _html.index("const DE = {")
_de = _html[_start:_html.index("\n};", _start)]
_untranslated = []
for _s in settings_schema.SCHEMA:
    for _text in (_s["label"], _s.get("help")):
        if _text and '"' + _text + '":' not in _de:
            _untranslated.append(_text)
assert not _untranslated, "no German for: " + "; ".join(_untranslated)
assert _html.count("esc(f.label)") == 0, "a settings label is rendered without t()"
print("all %d setting labels and their help lines have German" % len(settings_schema.SCHEMA))

for _k in list(CONFIG.overrides()):
    CONFIG.clear(_k)
print("ok")
