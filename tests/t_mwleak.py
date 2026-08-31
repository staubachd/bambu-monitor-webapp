"""The MakerWorld link belongs to the print it came from.

`design_id` is machine state, exactly like `print_error`: the printer keeps
reporting the last model it printed long after that job is gone. A self-sliced
print started afterwards therefore inherits the previous print's link, and the
history shows "View on MakerWorld" pointing at somebody else's model.

Two rules, and both are needed:

  * incremental frames often omit the field entirely, so only a frame that
    actually carries one may overwrite what is latched - otherwise a correct
    link is nulled out by the next partial report;
  * an id identical to the PREVIOUS job's is refused, because from here a repeat
    print and a leftover look exactly the same. The cloud pass resolves that
    case later, by design title, where there is enough information to tell.
"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import os as _os
SRC_DIR = (_os.environ.get("BAMBU_SRC")
           or _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import app

store = app.store
JOBS = []


def tick(tid, state="RUNNING", design=..., profile=None):
    if tid not in JOBS:
        JOBS.append(tid)
    job = {"task_id": tid, "state": state, "name": tid, "total_layers": 10}
    # `...` means the frame does not carry the field at all, which is different
    # from carrying it as empty - that difference is half of what is tested here
    if design is not ...:
        job["design_id"] = design
    if profile is not None:
        job["profile_id"] = profile
    s = {"job": job, "errors": {}, "power": {}, "ams": {}}
    app._track_print(s)
    app._persist_print(s)


def link(tid):
    r = store.get_print(tid) or {}
    return r.get("design_id"), r.get("profile_id")


def reset():
    app._print_row.update(job_id=None, started_at=None, peak_w=0.0, seen_active=False,
                          design_id=None, profile_id=None, stored={}, stale_err=None,
                          carried_design=None)


# --- a print of a real model -----------------------------------------------
reset()
tick("w1", "RUNNING", design="123456", profile="99")
tick("w1", "FINISH", design="123456", profile="99")
assert link("w1") == ("123456", "99"), link("w1")
print("a MakerWorld print records its model and plate:", link("w1"))

# --- a partial frame must not wipe it --------------------------------------
tick("w1", "RUNNING")                      # no design_id in this frame at all
assert link("w1") == ("123456", "99"), (
    f"an incremental frame with no design_id blanked the link: {link('w1')}")
tick("w1", "RUNNING", design="")           # present but empty
assert link("w1") == ("123456", "99"), f"an empty design_id blanked the link: {link('w1')}"
print("a frame that omits the field, or sends it empty, leaves it alone")

# --- the next print must not inherit it ------------------------------------
# The printer is still reporting 123456 because that is the last thing it printed.
tick("w2", "RUNNING", design="123456", profile="99")
tick("w2", "FINISH", design="123456", profile="99")
assert link("w2") == (None, None), (
    f"a self-sliced print inherited {link('w2')} from the job before it")
print("a self-sliced print started afterwards inherits nothing")

# --- a genuinely different model IS taken ----------------------------------
reset()
tick("w3", "RUNNING", design="123456")     # inherited-looking, refused
tick("w3", "RUNNING", design="777777")     # a real, different model
assert link("w3")[0] == "777777", link("w3")
print("a different model on the same job is taken:", link("w3")[0])

# --- the profile only travels with its design ------------------------------
reset()
tick("w4", "RUNNING", design="555", profile="1")
tick("w4", "RUNNING", profile="2")         # a plate with no design: meaningless
assert link("w4") == ("555", "1"), (
    f"the plate changed without its model: {link('w4')} - a profile identifies a "
    f"plate WITHIN a design and means nothing on its own")
print("a profile with no design of its own is ignored")

# --- resuming after a restart keeps the link -------------------------------
reset()                                     # as if the app had just started
tick("w1", "RUNNING")                       # no design in the frame
assert link("w1") == ("123456", "99"), (
    f"picking a print back up after a restart lost its link: {link('w1')}")
print("a print picked back up after a restart keeps its link")

# --- and the repeat-print case is knowingly left to the cloud --------------
src = open(os.path.join(SRC_DIR, "app.py"), encoding="utf-8").read()
assert "design_id_for_title" in src, (
    "nothing resolves the ambiguous case any more - refusing an identical id is "
    "only acceptable because the cloud pass recovers a genuine repeat print")
print("the ambiguous repeat is recovered later by title, not guessed at here")

for j in JOBS:
    store.delete_print(j)
print("ok")
