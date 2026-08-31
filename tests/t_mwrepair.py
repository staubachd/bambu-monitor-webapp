"""Repairing links that leaked before the guard existed, and recovering real ones.

Two halves of the same problem, from opposite directions.

`tools/fix_design_ids.py` looks at history already on disk. Rows that leaked a
model id all carry the SAME id, and the cloud gave each of them its own title -
so a row whose title disagrees with the row that first used that id is an
inheritance, and a row with a matching title is a genuine repeat print. It is
read-only until told otherwise, because a wrong repair loses a real link.

`storage.design_id_for_title` is the other direction: the live path refuses an
ambiguous repeated id, and this is what gives it back once the cloud has
supplied a title that proves the two prints really are the same model.
"""
import sys, os, json, subprocess, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import os as _os
SRC_DIR = (_os.environ.get("BAMBU_SRC")
           or _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import app

# The tool must be the copy sitting next to this test, not the one in the source
# tree: that one reads the real instance/db.json and would report on - and with
# --apply, edit - the actual print history.
HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(HERE, "tools", "fix_design_ids.py")
assert os.path.exists(TOOL), (
    f"{TOOL} is missing - run this through tests/runall.ps1, which assembles a "
    f"scratch copy of the app for the tests to work on")

store = app.store
now = time.time()
JOBS = ["r-owner", "r-repeat", "r-leak", "r-alone"]


def row(job, when, design, title, profile="1"):
    store.upsert_print(job_id=job, name=job, started_at=when, ended_at=when + 600,
                       final_state="FINISH", total_layers=1)
    store.update_print_fields(job, design_id=design, profile_id=profile,
                              design_title=title)


# the model was printed, printed again, and then a self-sliced job inherited it
row("r-owner", now - 3000, "424242", "Cable clip")
row("r-repeat", now - 2000, "424242", "Cable clip")     # genuinely the same model
row("r-leak", now - 1000, "424242", "Bracket v3")       # inherited: different job
row("r-alone", now - 500, "999999", "Something else")   # one of a kind

# --- the tool tells them apart ---------------------------------------------
out = subprocess.run([sys.executable, TOOL], capture_output=True, text=True, cwd=HERE)
report = out.stdout
assert out.returncode == 0, out.stdout + out.stderr
assert "INHERIT" in report and "r-leak" in report, report[-800:]
assert "repeat" in report, "a genuine repeat was not recognised as one"
leaked_lines = [l for l in report.splitlines() if "INHERIT" in l]
assert len(leaked_lines) == 1, f"expected one inheritance, got:\n" + "\n".join(leaked_lines)
assert "r-repeat" not in " ".join(leaked_lines), \
    "a repeat print of the same model was called an inheritance"
print("the tool finds 1 inheritance and leaves the repeat alone")

# --- and changes nothing until told to -------------------------------------
assert (store.get_print("r-leak") or {}).get("design_id") == "424242", \
    "the tool repaired without --apply"
assert "--apply" in report, "it does not say how to actually do it"
print("read-only by default; it says to re-run with --apply")

# --- with --apply, only the wrong one is cleared ---------------------------
out = subprocess.run([sys.executable, TOOL, "--apply"], capture_output=True, text=True,
                     cwd=HERE)
assert out.returncode == 0, out.stdout + out.stderr
assert (store.get_print("r-leak") or {}).get("design_id") is None, \
    "the inherited link was not cleared"
for keep in ("r-owner", "r-repeat", "r-alone"):
    assert (store.get_print(keep) or {}).get("design_id"), \
        f"{keep} lost a link it was entitled to"
print("--apply clears only the inherited row; the other three keep theirs")

# running it again finds nothing left to do
out = subprocess.run([sys.executable, TOOL], capture_output=True, text=True, cwd=HERE)
assert "nothing to repair" in out.stdout, out.stdout[-400:]
print("a second run finds nothing to repair")

# --- the other direction: recovering a link the live path refused ----------
got = store.design_id_for_title("Cable clip", exclude_job="r-repeat")
assert got == ("424242", "1"), got
print("a title that matches an earlier print recovers its model:", got)

# it must not hand a job its own row back as evidence about itself
assert store.design_id_for_title("Something else", exclude_job="r-alone") is None, \
    "a print was offered its own link as if another print had proved it"
print("a print is not offered its own row as evidence")

# an unknown title recovers nothing, rather than the most recent anything
assert store.design_id_for_title("Never printed") is None
print("an unknown title recovers nothing")

for j in JOBS:
    store.delete_print(j)
print("ok")
