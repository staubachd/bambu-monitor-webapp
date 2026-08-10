#!/usr/bin/env python3
"""Read-only: what the Bambu Cloud knows, next to what the database holds.

Answers "why didn't Refresh close my orphaned print?". The automatic path in
app._apply_cloud_task only closes a print when ALL of these hold:

  * the job is among the tasks the cloud returns (default fetch limit: 20)
  * the cloud reports status == 2  (2 = complete; 4 = still running)
  * its endTime parses as YYYY-MM-DDTHH:MM:SSZ
  * a local print row with that job_id exists and has no ended_at
  * the printer is not currently working on that same job

This prints each of those so the failing one is obvious. It writes nothing.

    python tools/dump_cloud_tasks.py [limit] [--json]
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from bambu_cloud import BambuCloud, CloudError   # noqa: E402
from storage import Storage                      # noqa: E402

limit = 20
show_json = "--json" in sys.argv
for a in sys.argv[1:]:
    if a.isdigit():
        limit = int(a)

with open(os.path.join(HERE, "printer.config.json"), encoding="utf-8") as fh:
    cfg = json.load(fh)
cloud = cfg.get("cloud") or {}
if not cloud.get("enabled"):
    print("cloud.enabled is false in printer.config.json - Refresh can never do "
          "anything. That alone explains a print staying 'running'.")
    sys.exit(1)

scfg = cfg.get("storage", {"backend": "sqlite"})
if scfg.get("backend", "sqlite") == "sqlite" and not os.path.isabs(scfg.get("sqlite_path", "telemetry.db")):
    scfg = {**scfg, "sqlite_path": os.path.join(HERE, scfg.get("sqlite_path", "telemetry.db"))}
store = Storage(scfg)
local = {p["job_id"]: p for p in store.all_prints()}
open_rows = {j: p for j, p in local.items() if not p.get("ended_at")}

print(f"database : {len(local)} prints, {len(open_rows)} with no end time")
for j, p in open_rows.items():
    started = (datetime.fromtimestamp(p["started_at"]).strftime("%Y-%m-%d %H:%M")
               if p.get("started_at") else "?")
    print(f"   OPEN  {j}  started {started}  state={p.get('final_state')}  "
          f"{(p.get('label') or p.get('design_title') or p.get('name') or '')[:40]}")

client = BambuCloud(token=cloud.get("token"))
try:
    tasks = client.get_tasks(serial=cfg.get("serial"), limit=limit)
except CloudError as e:
    print(f"\ncloud request FAILED: {e}")
    print("A 401/403 means the stored token expired - re-run tools/setup_cloud.py. "
          "That is also what a silent Refresh looked like.")
    sys.exit(2)

print(f"\ncloud    : {len(tasks)} tasks returned (limit {limit})\n")
print(f"{'task id':<14}{'status':<8}{'endTime':<22}{'in db':<7}{'db open':<9}title")
for tsk in tasks:
    tid = str(tsk.get("id") or "")
    end = tsk.get("endTime")
    parsed = "?"
    if end:
        try:
            datetime.strptime(str(end), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            parsed = "ok"
        except ValueError:
            parsed = "UNPARSEABLE"
    print(f"{tid:<14}{str(tsk.get('status')):<8}{str(end)[:19]:<22}"
          f"{('yes' if tid in local else 'NO'):<7}"
          f"{('yes' if tid in open_rows else '-'):<9}"
          f"{str(tsk.get('designTitle') or tsk.get('title') or '')[:34]}"
          + ("" if parsed in ("?", "ok") else f"   <- endTime {parsed}"))

missing = [str(t.get('id')) for t in tasks if str(t.get('id')) not in local]
if missing:
    print(f"\ncloud tasks with NO local print row: {', '.join(missing)}")
    print("  (enrichment deliberately never creates rows - it only updates them)")

stuck = [j for j in open_rows if j not in {str(t.get('id')) for t in tasks}]
if stuck:
    print(f"\nopen prints the cloud did NOT return: {', '.join(stuck)}")
    print(f"  -> beyond the fetch limit; try:  python {os.path.basename(__file__)} 100")

if show_json and tasks:
    print("\nfull first task:")
    print(json.dumps(tasks[0], indent=2, ensure_ascii=False)[:4000])
    print("\n(use this to see which fields a backfill could actually rely on)")
