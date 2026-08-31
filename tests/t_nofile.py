"""printer.config.json is gone. This is what "gone" has to mean.

A half-removed config file is worse than the old one: some values come from the
database and some from a file nobody remembers editing, and the two disagree
silently. So nothing may read it, nothing may name it as the place a value comes
from, and every setting the code reads must be one the schema knows about -
otherwise it can never be set again now that the file is not there to set it in.
"""
# The app source, relative to this file. These tests used to sit inside the
# source folder and could name it directly; they live beside it now, so that
# they survive a temp-directory clean-out.
import os as _os
SRC_DIR = (_os.environ.get("BAMBU_SRC")
           or _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
def _src(name):
    return _os.path.join(SRC_DIR, name)
import sys, os, re, io
HERE = os.path.dirname(os.path.abspath(__file__))
SRC = SRC_DIR
sys.path.insert(0, HERE)
import settings_schema, config_store   # noqa: E402

# bootstrap.py reads the old file once, to fill the wizard in; import_config.py
# is the headless version of the same thing. Everything else must be blind to it.
ALLOWED = {"bootstrap.py", "setup_wizard.py", "tools/import_config.py"}

offenders = []
for root, dirs, files in os.walk(SRC):
    # `tests` too: these files name the old config file on purpose, to check
    # that nothing in the APP reads it
    dirs[:] = [d for d in dirs if d not in ("venv", "__pycache__", ".git", "instance",
                                            "samples", "go2rtc", "tests")]
    for name in files:
        if not name.endswith((".py", ".html")):
            continue
        rel = os.path.relpath(os.path.join(root, name), SRC).replace("\\", "/")
        if rel in ALLOWED:
            continue
        text = io.open(os.path.join(root, name), encoding="utf-8").read()
        for m in re.finditer(r'(open|load|read|join)\s*\([^)]*printer\.config\.json', text):
            offenders.append(f"{rel}: {m.group(0)[:60]}")
assert not offenders, "these still read the config file:\n  " + "\n  ".join(offenders)
print(f"nothing outside {len(ALLOWED)} files opens printer.config.json")

# the page must not tell the user a value comes from a file that no longer feeds it
page = io.open(os.path.join(SRC, "dashboard.html"), encoding="utf-8").read()
assert "config_file" not in page, \
    "the Settings page still names a config file as the source of a value"
assert "printer.config.json" not in page, \
    "the dashboard still mentions printer.config.json"
print("the Settings page names no config file")

# --- every setting the app reads has to be settable -------------------------
# A key read from the config but missing from the schema used to be fine: you
# put it in the JSON by hand. There is no by hand any more, so such a key is
# unreachable and its value is frozen at whatever the code falls back to.
app_src = io.open(os.path.join(SRC, "app.py"), encoding="utf-8").read()
read = set()
for m in re.finditer(r'\bCFG(?:\.get\("([\w.]+)"|\["([\w.]+)"\])', app_src):
    read.add(m.group(1) or m.group(2))
for var, prefix in [("PWR_CFG", "power"), ("COST_CFG", "cost"), ("CAM_CFG", "camera"),
                    ("FIL_CFG", "filament"), ("CLOUD_CFG", "cloud")]:
    for m in re.finditer(r'\b%s(?:\.get\("(\w+)"|\["(\w+)"\])' % var, app_src):
        read.add(prefix + "." + (m.group(1) or m.group(2)))
# a bare section read (CFG.get("storage")) is the section, not a leaf
read = {p for p in read if "." in p or p in ("ip", "serial", "access_code", "model")}
unreachable = sorted(read - set(settings_schema.BY_PATH))
assert not unreachable, ("the app reads settings nobody can set any more: "
                         + ", ".join(unreachable))
print(f"all {len(read)} settings the app reads are in the schema, so all are settable")

# --- and none of them is a snapshot taken at import --------------------------
# The file used to be loaded once, so a module-level constant was honest. Now
# any value can change while the app runs, and a constant would quietly serve
# what was true at boot.
snapshots = re.findall(r'^([A-Z][A-Z0-9_]*) = (?:CFG|FIL_CFG|COST_CFG|PWR_CFG|CAM_CFG|CLOUD_CFG)\b.*$',
                       app_src, re.M)
snapshots = [s for s in snapshots if s not in ("CFG", "STORE_CFG")]
assert not snapshots, ("these capture a live setting at import and will not follow "
                       "an edit: " + ", ".join(snapshots))
print("no module-level constant captures a setting at import")

# --- the defaults are complete enough to boot on ----------------------------
d = config_store.defaults()
assert d["storage"]["sample_interval_sec"], "an install with nothing set would not record"
assert d["cost"]["currency"], "an install with nothing set would show no currency"
for path in ("ip", "serial", "access_code"):
    assert path not in settings_schema.BY_PATH or "default" not in settings_schema.BY_PATH[path], \
        f"{path} has a made-up default, which would look like a configured printer"
print("defaults cover what can be defaulted, and invent nothing that cannot")
print("ok")
