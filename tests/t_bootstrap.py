"""The seven keys that stay on disk, and nothing else.

The whole point of the change is that this file is the only one left, so what
matters is that it holds only the connection, that a password never comes back
out of it by accident, and that the app refuses to guess when it is unreadable.
"""
# The app source, relative to this file. These tests used to sit inside the
# source folder and could name it directly; they live beside it now, so that
# they survive a temp-directory clean-out.
import os as _os
SRC_DIR = (_os.environ.get("BAMBU_SRC")
           or _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
def _src(name):
    return _os.path.join(SRC_DIR, name)
import sys, os, json, tempfile, shutil
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bootstrap, settings_schema

tmp = tempfile.mkdtemp()
bootstrap.DIR = tmp
bootstrap.PATH = os.path.join(tmp, "db.json")
bootstrap.LEGACY = os.path.join(tmp, "printer.config.json")
bootstrap.LEGACY_DONE = bootstrap.LEGACY + ".imported"

# --- nothing on disk means "not set up", not "use some default" -------------
assert bootstrap.load() is None, "an absent file was read as a working config"
print("no file -> not configured (rather than a silent sqlite fallback)")

# --- only the connection survives a round trip ------------------------------
mixed = {"backend": "mariadb",
         "mariadb": {"host": "10.0.0.5", "port": "3307", "user": "bambu",
                     "password": "hunter2", "database": "bm"},
         # everything below is a setting, and settings do not live here any more
         "ip": "192.168.1.9", "cost": {"price_per_kwh": 0.3},
         "filament": {"bambu": {"PLA": 24.99}}}
bootstrap.save(mixed)
back = json.load(open(bootstrap.PATH, encoding="utf-8"))
assert set(back) == {"backend", "mariadb"}, f"extra keys were written: {sorted(back)}"
assert set(back["mariadb"]) == {"host", "port", "user", "password", "database"}, back
assert back["mariadb"]["port"] == 3307, "the port was stored as a string"
for stray in ("ip", "cost", "filament"):
    assert stray not in back, f"{stray} is a setting but was written to the connection file"
print("save() keeps the connection and drops everything that is a setting")

# a setting must not be reachable through this file at all
for path in ("ip", "cost.price_per_kwh", "cloud.password"):
    assert path in settings_schema.BY_PATH, f"{path} should be a schema setting"
print("the same keys are settings, and settings are stored in the database")

# --- the password is in the file and never in what the page is handed -------
red = bootstrap.redacted()
# a server connection comes back under "server" whatever the backend is called,
# so the Settings page needs no branch per backend
assert red["server"]["password"] != "hunter2", "redacted() leaked the password"
assert red["server"]["user"] == "bambu", "redacted() hid more than the password"
assert red["path"] == bootstrap.PATH, "the page cannot say where the file is"
assert "hunter2" not in json.dumps(red), "the password is somewhere in the payload"
assert "mariadb" not in red, ("the raw connection block is still in the payload "
                              "alongside the masked copy - with its real password")
print("redacted(): password masked, everything else still legible")

# --- sqlite: a relative name always means "beside the app" ------------------
bootstrap.save({"backend": "sqlite", "sqlite_path": "telemetry.db"})
got = bootstrap.load()["sqlite_path"]
assert os.path.isabs(got), f"a relative sqlite path stayed relative: {got}"
assert os.path.dirname(got) == bootstrap.HERE, got
print("a relative sqlite path resolves against the app folder, not the cwd")

# --- a real connection test, not a syntax check -----------------------------
ok, msg = bootstrap.test({"backend": "sqlite",
                          "sqlite_path": os.path.join(tmp, "probe.db")})
assert ok, msg
assert os.path.exists(os.path.join(tmp, "probe.db")), "test() never opened anything"
print("test() actually opens the database:", msg)

ok, msg = bootstrap.test({"backend": "mariadb",
                          "mariadb": {"host": "127.0.0.1", "port": 1,
                                      "user": "u", "database": "d", "password": ""}})
assert not ok, "a connection to a dead port was reported as working"
# on a machine without pymysql this fails at the import instead, which is also
# the right answer - either way the wizard must not call it a working connection
print("a connection that cannot be made is never reported as working:", msg[:70])

ok, msg = bootstrap.test({"backend": "mariadb",
                          "mariadb": {"host": "127.0.0.1", "user": "", "database": ""}})
assert not ok and "required" in msg, msg
print("an empty user is refused before anything is dialled")

# --- a corrupt file stops the app rather than starting it somewhere else ----
open(bootstrap.PATH, "w", encoding="utf-8").write("{ not json")
try:
    bootstrap.load()
    raise AssertionError("a corrupt connection file was swallowed")
except SystemExit as e:
    assert "unreadable" in str(e), e
print("a corrupt file stops the app with the path in the message")

# --- retiring the old config keeps it, and never overwrites an earlier one --
os.unlink(bootstrap.PATH)
open(bootstrap.LEGACY, "w", encoding="utf-8").write('{"ip": "1.2.3.4"}')
first = bootstrap.retire_legacy()
assert not os.path.exists(bootstrap.LEGACY) and os.path.exists(first)
open(bootstrap.LEGACY, "w", encoding="utf-8").write('{"ip": "5.6.7.8"}')
second = bootstrap.retire_legacy()
assert second != first, "the second retire overwrote the first backup"
assert json.load(open(first, encoding="utf-8"))["ip"] == "1.2.3.4", "the first backup was clobbered"
assert bootstrap.retire_legacy() is None, "retiring nothing should be a no-op"
print("printer.config.json is renamed, kept, and never overwritten")

# --- the path is derived, never assumed ------------------------------------
# The app is installed wherever someone put it - /volume1/apps on one NAS,
# somewhere else on the next - so nothing may hardcode a location. This is what
# "derived" has to mean: it comes from the module's own file, and the working
# directory the service happens to start in cannot move it.
import importlib
fresh = importlib.reload(bootstrap)
assert fresh.HERE == os.path.dirname(os.path.abspath(fresh.__file__)), fresh.HERE
assert fresh.PATH == os.path.join(fresh.HERE, "instance", "db.json"), fresh.PATH
here_before = fresh.PATH
os.chdir(tempfile.gettempdir())
assert importlib.reload(bootstrap).PATH == here_before,     "the connection file moved when the working directory changed"
src = open(_src("bootstrap.py"), encoding="utf-8").read()
for bad in ("/volume1", "/opt/", "C:" + chr(92)):
    assert bad not in src, f"a location is hardcoded in bootstrap.py: {bad}"
print("the connection file follows the app folder, and no path is hardcoded")

# restore what the reload undid
bootstrap.DIR = tmp
bootstrap.PATH = os.path.join(tmp, "db.json")
bootstrap.LEGACY = os.path.join(tmp, "printer.config.json")
bootstrap.LEGACY_DONE = bootstrap.LEGACY + ".imported"

# --- an unwritable folder is an answer, not a traceback --------------------
ok, where = bootstrap.writable()
assert ok and where == tmp, (ok, where)
print("a writable folder reports itself:", os.path.basename(where))

# a folder that cannot even be created: instance/ under a regular file
blocker = os.path.join(tmp, "blocker")
open(blocker, "w").close()
saved_dir, saved_path = bootstrap.DIR, bootstrap.PATH
bootstrap.DIR = os.path.join(blocker, "instance")
bootstrap.PATH = os.path.join(bootstrap.DIR, "db.json")
ok, why = bootstrap.writable()
assert not ok, "a folder that cannot exist was reported as writable"
assert "instance" in why and "cannot create" in why.lower(), why
print("a folder that cannot be created ->", why.split(".")[0][:64])

import setup_wizard
body, code = setup_wizard._apply({"db": {"backend": "sqlite",
                                         "sqlite_path": os.path.join(tmp, "y.db")},
                                  "values": {"ip": "1.2.3.4", "serial": "S",
                                             "access_code": "c"}})
assert code == 400 and body["where"] == "file", (code, body)
assert not os.path.exists(os.path.join(tmp, "y.db")),     "the database was created even though the connection could never be stored"
print("the wizard stops before touching the database, and says which part failed")
bootstrap.DIR, bootstrap.PATH = saved_dir, saved_path

import tempfile as _tf
real = _tf.mkstemp
_tf.mkstemp = lambda *a, **k: (_ for _ in ()).throw(PermissionError(13, "Permission denied"))
try:
    import setup_wizard
    body, code = setup_wizard._apply({"db": {"backend": "sqlite",
                                             "sqlite_path": os.path.join(tmp, "x.db")},
                                      "values": {"ip": "1.2.3.4", "serial": "S",
                                                 "access_code": "c"}})
    assert code == 400 and body["where"] == "file", (code, body)
    assert "write" in body["error"].lower() or "writable" in body["error"].lower(), body
    print("a folder that refuses the file ->", body["error"][:70])
finally:
    _tf.mkstemp = real

shutil.rmtree(tmp, ignore_errors=True)
print("ok")
