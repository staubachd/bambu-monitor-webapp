#!/usr/bin/env python3
"""Read-only: why do two prints of the same thing cost different amounts?

Per-print material cost is STORED, worked out when the cloud enriched the job.
This prints, for each recent print, the per-slot breakdown it was costed from -
the identity each slot resolved to, what that identity canonicalises to after
any merges, the rate applied and which rule chose it - and then re-runs the
pricing rules as they stand NOW, so a row whose stored cost no longer matches
what the rules would give is obvious.

A print with no per-slot detail cannot be re-costed at all: nothing records
which filament it used, so there is nothing for a merge or a price to attach to.
Those are called out separately.

Writes nothing.

    python tools/why_this_price.py            # last 20 prints
    python tools/why_this_price.py 60
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import app                                   # noqa: E402  (loads config + store)
import filament_catalog                      # noqa: E402

LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 20

rows = app.store.recent_prints(limit=LIMIT)
print(f"{len(rows)} print(s), newest first\n")

no_detail = []
for r in rows:
    when = (datetime.fromtimestamp(r["started_at"]).strftime("%Y-%m-%d %H:%M")
            if r.get("started_at") else "?")
    name = r.get("label") or r.get("design_title") or r.get("name") or "-"
    try:
        entries = json.loads(r.get("filament_detail") or "[]") or []
    except (TypeError, ValueError):
        entries = []
    stored = r.get("filament_cost")
    print(f"{when}  {str(name)[:38]:<38} stored material cost: "
          f"{'-' if stored is None else round(stored, 4)}")
    if not entries:
        no_detail.append(r["job_id"])
        g = r.get("filament_g_manual") or r.get("filament_g")
        print(f"      NO per-slot detail ({g or 0} g total) - nothing to re-cost\n")
        continue
    try:
        bambu_map = json.loads(r.get("ams_bambu") or "{}")
    except (TypeError, ValueError):
        bambu_map = {}
    for e in entries:
        raw = filament_catalog.key(e.get("filament_id"), e.get("color"), e.get("type"))
        canon = app._canon_fkey(raw)
        now_kg, now_rule = app._filament_price_per_kg(
            {"slotId": (e.get("slot") or 1) - 1, "filamentId": e.get("filament_id"),
             "filamentType": e.get("type")}, bambu_map, canon)
        was_kg = e.get("per_kg")
        flag = "" if was_kg == now_kg else f"   <-- would now be {now_kg}/kg ({now_rule})"
        print(f"      slot {e.get('slot')}  {raw}"
              + (f"  ->  {canon}" if canon != raw else "")
              + f"  {e.get('grams')} g  @ {was_kg}/kg ({e.get('rule')})"
              + f" = {e.get('cost')}{flag}")
        print(f"              ams_bambu says slot {e.get('slot')}: "
              f"{bambu_map.get(str(e.get('slot')), '(not recorded)')}"
              f" | identity says: {app._FIL_ID.get(canon, (None, None))[1]}")
    print()

if no_detail:
    print(f"{len(no_detail)} print(s) have no per-slot detail and can never be "
          f"re-costed by a merge or a price:")
    print("   " + ", ".join(no_detail))
    print("   (they were never enriched from the cloud, so nothing records which "
          "filament they used)")
