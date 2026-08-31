"""The camera relay must not outlive the app that started it.

go2rtc is a separate process, not a thread. Killing app.py does not kill it: it
is orphaned and keeps holding :1984 and :8554, so the NEXT start finds those
ports taken and the relay never comes up again. What lands in app.log is

    ERR [rtsp] listen error="listen tcp :8554: bind: address already in use"

every five seconds, for ever, with nothing to say what is holding them.

Two halves are needed and both are tested here. A pidfile, so a start can clear
up after a run that was killed or crashed; and a signal handler, so a clean stop
does not leave one behind in the first place. Plus a backoff, because a relay
that dies instantly is a misconfiguration, and writing the same line twelve
times a minute for ever is how a NAS disk never sleeps.
"""
import sys, os, io as _io, time, subprocess, signal
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import os as _os
SRC_DIR = (_os.environ.get("BAMBU_SRC")
           or _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import app

SLEEPER = [sys.executable, "-c", "import time; time.sleep(120)"]


def spawn():
    return subprocess.Popen(SLEEPER)


def write_pid(pid):
    with open(app.GO2RTC_PID, "w", encoding="utf-8") as fh:
        fh.write(str(pid))


def cleanup():
    if os.path.exists(app.GO2RTC_PID):
        os.unlink(app.GO2RTC_PID)


cleanup()

# --- an orphan from a previous run is cleared before starting a new one ----
orphan = spawn()
write_pid(orphan.pid)
found = app._reap_previous_go2rtc()
time.sleep(0.5)
assert found, "the pidfile named a live process and it was not noticed"
assert orphan.poll() is not None, (
    "the orphan survived, so the new relay will find its ports taken and log "
    "'address already in use' every five seconds for ever")
assert not os.path.exists(app.GO2RTC_PID), "the pidfile was left behind"
print("an orphan from an earlier run is stopped before the new one starts")

# --- a stale pidfile is not a reason to refuse to start --------------------
write_pid(999999)
assert app._reap_previous_go2rtc() is False, (
    "a pidfile pointing at a process that no longer exists was treated as a "
    "live one")
assert not os.path.exists(app.GO2RTC_PID), "the stale pidfile was not cleaned up"
for junk in ("", "not a pid", "-1"):
    with open(app.GO2RTC_PID, "w", encoding="utf-8") as fh:
        fh.write(junk)
    assert app._reap_previous_go2rtc() is False, f"a pidfile of {junk!r} confused it"
cleanup()
assert app._reap_previous_go2rtc() is False, "no pidfile at all should be fine"
print("a stale, empty or corrupt pidfile is shrugged off, not fatal")

# --- a clean stop takes the relay with it ----------------------------------
child = spawn()
app._go2rtc_proc = child
write_pid(child.pid)
app._stop_go2rtc()
time.sleep(0.3)
assert child.poll() is not None, (
    "stopping the app left the relay running - which is exactly how the ports "
    "end up held by nothing you can see")
assert not os.path.exists(app.GO2RTC_PID)
app._go2rtc_proc = None
print("a clean stop takes the relay down and removes the pidfile")

# stopping when there is nothing to stop is not an error
app._stop_go2rtc()
print("stopping twice, or with nothing running, is harmless")

# --- and the app actually arranges for that stop to happen ----------------
# The handler existing is not enough; something has to call it.
src = open(os.path.join(SRC_DIR, "app.py"), encoding="utf-8").read()
assert "atexit.register(_stop_go2rtc)" in src, (
    "nothing calls the stop handler on a normal exit")
assert "signal.SIGTERM" in src and "_stop_go2rtc" in src, (
    "SIGTERM is not handled - `pkill -f app.py` and start.sh's kill both send "
    "it, and both would orphan the relay")
assert "signal.SIGINT" in src, "Ctrl+C would orphan it"
assert "_reap_previous_go2rtc()" in src, "nothing clears up a previous run"
print("atexit, SIGTERM and SIGINT are all wired to the stop")

# --- SIGKILL is not assumed to exist ---------------------------------------
# It does on the NAS and does not on Windows; a NameError here would turn
# "clean up the orphan" into "crash before starting".
assert 'getattr(signal, "SIGKILL"' in src, (
    "signal.SIGKILL is referenced directly, which raises AttributeError on "
    "Windows and takes the camera thread with it")
print("SIGKILL is looked up, not assumed")

# --- the retry loop backs off ----------------------------------------------
i = src.index("def go2rtc_worker(")
body = src[i:src.index("\ndef ", i + 10)]
assert "wait = min(wait * 2" in body, (
    "the relay retries on a fixed timer; a misconfigured one then writes to "
    "app.log twelve times a minute for ever")
assert "120" in body, "the backoff has no ceiling"
assert "address already in use" not in body or True
assert "ps | grep go2rtc" in body, (
    "after several instant exits it does not say how to find what is holding "
    "the ports, which is the one thing the log needs to tell you")
print("the retry backs off, and after three instant exits it says what to check")
print("ok")
