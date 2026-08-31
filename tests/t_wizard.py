"""The wizard is the only way a fresh install gets configured, so it has to be
the one thing that cannot half-work.

What matters: it never writes anything until all of it validates, it refuses a
connection that has not answered, a blank secret box means "keep what is
stored" rather than "erase it", and an upgrade carries the old file's values
across without the user retyping credentials.
"""
import sys, os, json, tempfile, shutil
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bootstrap, config_store, settings_schema, setup_wizard
from storage import Storage

tmp = tempfile.mkdtemp()
bootstrap.DIR = tmp
bootstrap.PATH = os.path.join(tmp, "db.json")
bootstrap.LEGACY = os.path.join(tmp, "printer.config.json")
bootstrap.LEGACY_DONE = bootstrap.LEGACY + ".imported"
DB = os.path.join(tmp, "t.db")
GOOD_DB = {"backend": "sqlite", "sqlite_path": DB}


def stored():
    cfg = config_store.ConfigStore()
    cfg.attach(Storage(bootstrap.load()))
    return cfg


# --- a fresh install: the wizard offers the schema's defaults, not blanks ----
seed = setup_wizard._seed()
assert seed["source"] == "fresh", seed["source"]
assert seed["values"]["filament.bambu"], "a fresh install would price every print at zero"
assert seed["values"]["storage.sample_interval_sec"] == \
    settings_schema.BY_PATH["storage.sample_interval_sec"]["default"]
assert seed["db"]["backend"] == "mariadb", "the NAS case should be the default offer"
print("fresh install: prefilled from the schema, MariaDB offered first")

# every step's fields come from the schema, so a setting added there appears
# in the wizard without anyone remembering to list it
paths = {f["path"] for f in seed["fields"]}
groups = {g for s in seed["steps"] for g in s["groups"]}
expected = {s["path"] for s in settings_schema.SCHEMA if s["group"] in groups}
assert paths == expected, f"the wizard and the schema disagree: {paths ^ expected}"
assert groups == set(settings_schema.GROUPS), \
    f"a schema group no step asks about: {set(settings_schema.GROUPS) - groups}"
print(f"all {len(paths)} settings in {len(groups)} groups are reachable from some step")

# no secret is ever handed to the page, not even to fill a box in
for p in settings_schema.SECRETS:
    assert seed["values"][p] == "", f"{p} was sent to the wizard"
print("secrets are never prefilled, only flagged as set or not")

# --- nothing is written until everything validates --------------------------
body, code = setup_wizard._apply({"db": GOOD_DB, "values": {
    "ip": "192.168.1.5", "serial": "ABC", "access_code": "12345678",
    "filament.low_pct": 900}})                      # out of range
assert code == 400 and not body["ok"], body
assert not os.path.exists(bootstrap.PATH), "a rejected form still wrote the connection file"
assert not os.path.exists(DB), "a rejected form still created the database"
print("one bad field -> no connection file, no database, nothing stored")

# a connection that has not answered is not written either
bad = {"backend": "mariadb", "mariadb": {"host": "127.0.0.1", "port": 1,
                                         "user": "u", "password": "p", "database": "d"}}
body, code = setup_wizard._apply({"db": bad, "values": {
    "ip": "192.168.1.5", "serial": "ABC", "access_code": "12345678"}})
assert code == 400 and body["where"] == "db", body
assert not os.path.exists(bootstrap.PATH), "an unreachable connection was written to disk"
print("an unreachable database is refused before anything is saved")

# the printer is the whole point of the app; it cannot be skipped
body, code = setup_wizard._apply({"db": GOOD_DB, "values": {"ip": "192.168.1.5"}})
assert code == 400 and any("Serial" in e or "access" in e.lower() for e in body["errors"]), body
print("the printer's serial and access code are required")

# --- a good run writes both halves ------------------------------------------
body, code = setup_wizard._apply({"db": GOOD_DB, "values": {
    "ip": "192.168.1.5", "serial": "ABC123", "access_code": "12345678",
    "cost.price_per_kwh": "0,31", "filament.bambu": {"PLA": "24.99"},
    "cloud.enabled": True, "cloud.email": "a@b.c", "cloud.password": "secret"}})
assert code == 200 and body["ok"], body
assert os.path.exists(bootstrap.PATH), "the connection file was not written"
cfg = stored()
assert cfg.get("ip") == "192.168.1.5"
assert cfg.get("cost.price_per_kwh") == 0.31, "a German decimal comma was not accepted"
assert cfg.get("filament.bambu") == {"PLA": 24.99}, cfg.get("filament.bambu")
assert cfg.get("cloud.password") == "secret"
print(f"a complete run stored {body['settings']} settings and the connection")

# nothing that belongs in the database leaked into the file
onfile = json.load(open(bootstrap.PATH, encoding="utf-8"))
assert "ip" not in json.dumps(onfile), "the printer IP was written to the connection file"
assert set(onfile) <= {"backend", "sqlite_path", "mariadb"}, onfile
print("the connection file holds the connection and nothing else")

# --- re-running it: a blank secret keeps the stored one ---------------------
seed = setup_wizard._seed()
assert seed["source"] == "stored", seed["source"]
assert seed["is_set"]["cloud.password"] is True, "a stored secret is not reported as set"
assert seed["values"]["cloud.password"] == "", "a stored secret was handed back out"
setup_wizard._apply({"db": GOOD_DB, "values": {
    "ip": "192.168.1.5", "serial": "ABC123", "access_code": "",
    "cloud.password": ""}})
cfg = stored()
assert cfg.get("cloud.password") == "secret", "a blank box erased the stored password"
assert cfg.get("access_code") == "12345678", "a blank box erased the access code"
print("blank secret boxes keep what is stored, on a re-run as on the first")

# --- an upgrade: the old file fills the form in, then stops being read ------
shutil.rmtree(tmp, ignore_errors=True)
os.makedirs(tmp, exist_ok=True)
json.dump({"ip": "10.0.0.9", "serial": "OLD1", "access_code": "abcdefgh",
           "cost": {"price_per_kwh": 0.42, "currency": "EUR"},
           "filament": {"bambu": {"PLA": 19.99}},
           "cloud": {"enabled": True, "email": "a@b.c", "password": "cloud-pw"},
           "power": {"enabled": True, "host": "10.0.0.3", "email": "t@b.c",
                     "password": "tapo-pw"},
           "storage": {"backend": "sqlite", "sqlite_path": DB,
                       "sample_interval_sec": 45}},
          open(bootstrap.LEGACY, "w", encoding="utf-8"))

seed = setup_wizard._seed()
assert seed["source"] == "legacy", seed["source"]
assert seed["values"]["ip"] == "10.0.0.9", "the old file did not fill the form in"
assert seed["values"]["cost.price_per_kwh"] == 0.42
assert seed["values"]["storage.sample_interval_sec"] == 45, \
    "a value from the old storage block was lost"
assert seed["is_set"]["access_code"] is True, "the old access code was not carried over"
assert seed["values"]["access_code"] == "", "the old access code was sent to the browser"
assert seed["db"]["backend"] == "sqlite", "the old storage block did not become the connection"
assert seed["legacy_path"], "the page cannot tell the user which file it read"
print("an upgrade prefills every step from printer.config.json, secrets included")

# Exactly what the page posts when the user clicks straight through: every
# secret box blank, because the page told them blank means "keep it". The old
# file is renamed away moments later, so anything not carried over now is gone -
# this used to lose the access code and both account passwords.
body, code = setup_wizard._apply({"db": GOOD_DB, "values": seed["values"]})
assert code == 200, body
assert not os.path.exists(bootstrap.LEGACY), "the old file is still where the app looks"
assert os.path.exists(bootstrap.LEGACY_DONE), "the old file was deleted rather than kept"
assert body["retired"], "the page cannot tell the user what happened to the file"
cfg = stored()
assert cfg.get("cost.price_per_kwh") == 0.42, "the old price did not reach the database"
assert cfg.get("storage.sample_interval_sec") == 45
for path, want in [("access_code", "abcdefgh"), ("cloud.password", "cloud-pw"),
                   ("power.password", "tapo-pw")]:
    got = cfg.get(path)
    assert got == want, (f"{path} was lost with the retired file: {got!r} - the page "
                         f"said a blank box would keep it")
print("finishing moves it into the database and renames the file:", body["retired"])
print("every secret survived, without the user retyping one")

# --- "leave empty to keep the stored one" has to be true -------------------
# On an upgrade there is no instance/db.json yet, so the password the page
# offers to keep lives in the old config file. Looking only at the connection
# file sent no password at all and MariaDB answered "using password: NO".
shutil.rmtree(tmp, ignore_errors=True)
os.makedirs(tmp, exist_ok=True)
json.dump({"ip": "10.0.0.9", "serial": "OLD1", "access_code": "abcdefgh",
           "storage": {"backend": "mariadb",
                       "mariadb": {"host": "127.0.0.1", "port": 3306, "user": "bambu",
                                   "password": "from-the-old-file",
                                   "database": "bambu_monitor"}}},
          open(bootstrap.LEGACY, "w", encoding="utf-8"))

assert not bootstrap.exists(), "this case is about there being no connection file yet"
seed = setup_wizard._seed()
assert seed["is_set"]["db.password"] is True,     "the page would not offer to keep a password it can see"
assert seed["db"]["mariadb"]["password"] == "", "the password was sent to the browser"

# what the page posts when the box is left blank
seen = {}
real_test = bootstrap.test
bootstrap.test = lambda cfg: (seen.update(cfg) or (False, "stopped here"))
try:
    setup_wizard._apply({"db": {**seed["db"], "mariadb":
                                {**seed["db"]["mariadb"], "password": ""}},
                         "values": {"ip": "10.0.0.9", "serial": "OLD1",
                                    "access_code": "abcdefgh"}})
finally:
    bootstrap.test = real_test
assert seen.get("mariadb", {}).get("password") == "from-the-old-file", (
    "a blank password box sent no password at all: "
    f"{seen.get('mariadb', {}).get('password')!r}")
print("a blank password box reuses the one the page said it had")

# and with nothing to reuse, the page must not claim otherwise
os.unlink(bootstrap.LEGACY)
assert setup_wizard._known_db_password() == "", "a password appeared from nowhere"
assert not setup_wizard._seed()["is_set"].get("db.password"),     "the page offers to keep a password that does not exist"
print("with nothing stored, it does not offer to keep anything")

shutil.rmtree(tmp, ignore_errors=True)
os.makedirs(tmp, exist_ok=True)
json.dump({"ip": "10.0.0.9", "serial": "OLD1", "access_code": "abcdefgh",
           "cost": {"price_per_kwh": 0.42, "currency": "EUR"},
           "filament": {"bambu": {"PLA": 19.99}},
           "storage": {"backend": "sqlite", "sqlite_path": DB,
                       "sample_interval_sec": 45}},
          open(bootstrap.LEGACY, "w", encoding="utf-8"))
setup_wizard._apply({"db": GOOD_DB, "values": {
    "ip": "10.0.0.9", "serial": "OLD1", "access_code": "abcdefgh"}})

# and a second run no longer thinks this is an upgrade
assert setup_wizard._seed()["source"] == "stored", "the retired file is still being read"
print("the retired file is not read again")

shutil.rmtree(tmp, ignore_errors=True)
print("ok")
