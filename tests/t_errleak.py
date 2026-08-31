"""A failure belongs to the print that failed, and to no other.

`print_error` is MACHINE state, not job state. The printer keeps reporting the
last failure long after that job is gone - nothing clears it when a new print
starts - so the next print begins its life with someone else's error code
already showing. Recording that would put a red mark on a job that printed
perfectly, permanently, in the history.

The rule: whatever error is showing at the moment a new job appears belongs to
the PREVIOUS one. It is latched as stale and refused, until the printer reports
something else - or nothing, which is what proves the old one was cleared.
"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import os as _os
SRC_DIR = (_os.environ.get("BAMBU_SRC")
           or _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import app

store = app.store
JOBS = []


def tick(tid, state="RUNNING", err=None):
    """One MQTT frame, through the two functions that own attribution."""
    if tid not in JOBS:
        JOBS.append(tid)
    s = {"job": {"task_id": tid, "state": state, "name": tid, "total_layers": 10},
         "errors": ({} if err is None else {"print_error": err}),
         "power": {}, "ams": {}}
    app._track_print(s)
    app._persist_print(s)
    return s


def err_of(tid):
    return (store.get_print(tid) or {}).get("error_code")


def reset():
    app._print_row.update(job_id=None, started_at=None, peak_w=0.0, seen_active=False,
                          design_id=None, profile_id=None, stored={}, stale_err=None,
                          carried_design=None)


# --- a print that really does fail --------------------------------------
reset()
tick("e1", "RUNNING")
tick("e1", "FAILED", err="0300_0100_0002_0001")
assert err_of("e1") == "0300_0100_0002_0001", err_of("e1")
print("a print that fails records its error:", err_of("e1"))

# --- the next print must not inherit it ------------------------------------
# The printer is STILL reporting the same code, because nothing cleared it.
tick("e2", "RUNNING", err="0300_0100_0002_0001")
tick("e2", "RUNNING", err="0300_0100_0002_0001")
assert err_of("e2") is None, (
    f"the next print inherited {err_of('e2')} - that is the previous job's failure, "
    f"still being reported because nothing clears it")
print("the next print does not inherit it, even while it is still being reported")

# --- but a NEW failure on that print is real -------------------------------
tick("e2", "FAILED", err="0700_2000_0002_0003")
assert err_of("e2") == "0700_2000_0002_0003", (
    f"a genuinely different error was swallowed by the stale latch: {err_of('e2')}")
print("a different code on the same print is recorded:", err_of("e2"))

# --- the same code again, after the printer has cleared it, IS real --------
reset()
tick("e3", "RUNNING", err="0300_0100_0002_0001")   # inherited, refused
assert err_of("e3") is None
tick("e3", "RUNNING", err=None)                    # cleared: the latch drops
tick("e3", "FAILED", err="0300_0100_0002_0001")    # and now it happens for real
assert err_of("e3") == "0300_0100_0002_0001", (
    "the same failure happening for real, after the old one cleared, was refused - "
    "the latch is never being released")
print("once the printer clears it, the same code happening for real is recorded")

# --- a clean print stays clean ---------------------------------------------
reset()
tick("e4", "RUNNING")
tick("e4", "FINISH")
assert err_of("e4") is None, err_of("e4")
print("a clean print records no error")

# --- the other two fields count as errors too ------------------------------
for field in ("mc_code", "fail_reason"):
    reset()
    s = {"job": {"task_id": "e5", "state": "RUNNING", "name": "e5"},
         "errors": {field: "123"}, "power": {}, "ams": {}}
    assert app._error_code(s) == "123", f"{field} is not treated as a failure"
print("mc_code and fail_reason are read as failures, not only print_error")

for j in JOBS + ["e5"]:
    store.delete_print(j)
print("ok")
