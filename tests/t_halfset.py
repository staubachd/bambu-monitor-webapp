"""Enabled but not filled in is a state that exists now.

While the config was a file, every feature that was on had its credentials next
to the switch, because you wrote both at once by hand. The Settings page can
tick "Smart plug enabled" and save, with the password box still empty - and the
background thread then dies on a KeyError nobody sees, in a thread nobody is
watching, leaving a dashboard that is merely missing a number.

So: a half-configured feature must decline to start and say what is missing.
"""
# The app source, relative to this file. These tests used to sit inside the
# source folder and could name it directly; they live beside it now, so that
# they survive a temp-directory clean-out.
import os as _os
SRC_DIR = (_os.environ.get("BAMBU_SRC")
           or _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
def _src(name):
    return _os.path.join(SRC_DIR, name)
import sys, os, io, re, threading, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app

CONFIG = app.CONFIG
for k in ("power.password", "power.email", "power.host", "cloud.token",
          "cloud.email", "cloud.password"):
    try:
        CONFIG.clear(k)
    except Exception:
        pass
CONFIG.set("power.enabled", True)
CONFIG.set("cloud.enabled", True)


def run(fn, seconds=2.0):
    """Run a worker briefly and return (crashed_with, printed)."""
    out, err = io.StringIO(), {}
    real = sys.stdout
    sys.stdout = out

    def go():
        try:
            fn()
        except BaseException as e:      # a thread's exception is invisible otherwise
            err["e"] = e
    t = threading.Thread(target=go, daemon=True)
    t.start()
    t.join(seconds)
    sys.stdout = real
    return err.get("e"), out.getvalue()


# --- the plug ---------------------------------------------------------------
crash, said = run(app.power_worker)
assert crash is None, f"the power thread crashed on a missing password: {crash!r}"
assert "power" in said.lower(), f"it started, or said nothing: {said!r}"
assert "Settings" in said, f"it did not say where to fix it: {said!r}"
print("plug enabled with no credentials ->", said.strip().splitlines()[-1][:90])

# and it must not pretend it is working
assert app._power.get("error"), "a plug that never connected reports no error"
print("the dashboard is told the plug is not configured, not that it reads 0 W")

# --- the cloud --------------------------------------------------------------
crash, said = run(app.cloud_worker)
assert crash is None, f"the cloud thread crashed: {crash!r}"
assert "cloud" in said.lower() and "Settings" in said, said
print("cloud enabled with no account ->", said.strip().splitlines()[-1][:90])

# --- nothing reads a required setting with [] -------------------------------
# A live section returns a plain dict, so CFG["x"] on an unset value raises in
# a background thread. Only paths that can never be unset may be subscripted.
src = io.open(_src("app.py"), encoding="utf-8").read()
# both quote styles: inside an f-string the subscript is written with single
# quotes, and that is exactly where the last one was hiding
subs = set(re.findall(r"""\b(?:CFG|PWR_CFG|COST_CFG|CAM_CFG|FIL_CFG|CLOUD_CFG)\[['"](\w+)['"]\]""", src))
import settings_schema
blank_ok = {"ip", "serial", "access_code", "host", "email", "password", "token"}
risky = sorted(subs & blank_ok)
assert not risky, ("these settings are read with [] but can legitimately be "
                   "unset, which raises inside a worker thread: " + ", ".join(risky))
print(f"{len(subs)} settings are subscripted, none of them one that can be blank")

# --- the printer itself: no address is a message, not a KeyError loop -------
assert 'CFG.get("ip")' in src, "mqtt_worker no longer checks for a printer address"
print("a missing printer address is reported rather than retried silently")
print("ok")
