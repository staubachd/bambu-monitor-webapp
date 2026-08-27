#!/usr/bin/env python3
"""Find print rows that inherited the previous print's MakerWorld link.

The printer reports `design_id` as machine state: it keeps naming the last model
it printed long after that job is gone, and it does not clear the field for a
self-sliced print. Until this was guarded, every print that followed a MakerWorld
one was stored with that model's id - so its "View on MakerWorld" link pointed at
somebody else's model.

The inheritance always runs FORWARD in time from the job that really owned the
id, which is what makes it repairable: inside a group of rows sharing one id, the
earliest is the owner, and any later row carrying a DIFFERENT design title (the
cloud's per-task value, which was never wrong) took it by accident.

Rows with the same title are left alone - that is the same model printed twice,
and the id genuinely belongs to both.

Writes nothing unless you pass --apply.

    python tools/fix_design_ids.py            # report
    python tools/fix_design_ids.py --apply    # clear the inherited ones
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from storage import Storage             # noqa: E402

APPLY = "--apply" in sys.argv

import config_store  # noqa: E402

store, cfg = config_store.open_live()

rows = [r for r in store.all_prints() if r.get("design_id")]
rows.sort(key=lambda r: r.get("started_at") or 0)
print(f"{len(rows)} print(s) carry a MakerWorld model id\n")

groups: dict[str, list] = {}
for r in rows:
    groups.setdefault(str(r["design_id"]), []).append(r)


def when(r):
    ts = r.get("started_at")
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "?"


def title(r):
    return (r.get("design_title") or "").strip()


suspect = []
for did, members in sorted(groups.items(), key=lambda kv: kv[1][0].get("started_at") or 0):
    owner = members[0]
    wrong = [m for m in members[1:] if title(m) != title(owner)]
    if not wrong and len(members) == 1:
        continue
    print(f"model {did}")
    print(f"   owner   {when(owner)}  {owner['job_id']:<14}"
          f"{title(owner) or '(no cloud title)'}")
    for m in members[1:]:
        bad = title(m) != title(owner)
        print(f"   {'INHERIT' if bad else 'repeat '} {when(m)}  {m['job_id']:<14}"
              f"{title(m) or '(no cloud title)'}")
        if bad:
            suspect.append(m)
    print()

if not suspect:
    print("nothing to repair: no row carries a link that belongs to another print")
    sys.exit(0)

print(f"{len(suspect)} row(s) inherited a link that is not theirs")
if not APPLY:
    print("re-run with --apply to clear the model id on those rows "
          "(nothing else is touched)")
    sys.exit(0)

for m in suspect:
    store.update_print_fields(m["job_id"], design_id=None, profile_id=None)
    print(f"   cleared {m['job_id']}  {title(m) or '(no cloud title)'}")
print(f"\n{len(suspect)} row(s) repaired. A later cloud sync can still recover a "
      "genuine link by design title.")
