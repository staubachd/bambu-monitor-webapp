#!/usr/bin/env python3
"""
Recompute a print's energy/cost/peak from the recorded telemetry.

Useful when a stored figure is wrong - e.g. a print recorded before the
per-job reset fix inherited the previous print's total, and the monotonic
guard in the app will not lower it on its own.

    ./venv/bin/python3 deploy/recalc_print_energy.py            # newest print
    ./venv/bin/python3 deploy/recalc_print_energy.py <job_id>   # a specific one
    ./venv/bin/python3 deploy/recalc_print_energy.py --all      # every print

Stop the app first, then start it again afterwards, so it re-seeds the live
counter from the corrected value:

    kill $(cat app.pid) 2>/dev/null; sleep 3
    ./venv/bin/python3 deploy/recalc_print_energy.py
    sh start.sh
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import storage  # noqa: E402

cfg = json.load(open(os.path.join(HERE, "printer.config.json"), encoding="utf-8"))
scfg = dict(cfg.get("storage", {}))
if scfg.get("backend", "sqlite") == "sqlite" and not os.path.isabs(scfg.get("sqlite_path", "")):
    scfg["sqlite_path"] = os.path.join(HERE, scfg.get("sqlite_path", "telemetry.db"))
price = float((cfg.get("cost") or {}).get("price_per_kwh", 0) or 0)
cur_sym = (cfg.get("cost") or {}).get("currency", "")

s = storage.Storage(scfg)
args = [a for a in sys.argv[1:]]
prints = s.recent_prints(limit=500)
if not prints:
    sys.exit("no prints recorded yet")

if "--all" in args:
    targets = prints
elif args:
    targets = [p for p in prints if p["job_id"] == args[0]]
    if not targets:
        sys.exit(f"no print with job_id {args[0]}")
else:
    targets = prints[:1]

# A drifted ended_at can run past the *next* print's start; without this cap the
# integration would absorb that print's power as well.
starts = sorted(x["started_at"] for x in prints if x["started_at"])

for p in targets:
    start = p["started_at"] or 0
    end = p["ended_at"] or time.time()
    nxt = next((t for t in starts if t > start), None)
    if nxt:
        end = min(end, nxt)
    span_h = max(0.1, (time.time() - start) / 3600 + 1)
    rows = [r for r in s.history(hours=span_h, max_points=10 ** 7)
            if start <= r["ts"] <= end
            and r.get("power_w") is not None
            and r.get("gcode_state") == "RUNNING"]

    wh, peak, prev = 0.0, 0.0, None
    for r in rows:
        peak = max(peak, r["power_w"])
        if prev:
            dt = r["ts"] - prev["ts"]
            if 0 < dt < 300:
                wh += (r["power_w"] + prev["power_w"]) / 2 * dt / 3600.0
        prev = r

    # Repair a creeping ended_at: before the fix, a finished print had its end
    # time rewritten every minute while the printer idled. The true end is the
    # last sample where the printer was still RUNNING.
    new_end = p["ended_at"]
    if rows and p["ended_at"]:
        last_running = rows[-1]["ts"]
        if p["ended_at"] - last_running > 120:      # clearly drifted
            new_end = last_running

    cost = round(wh / 1000.0 * price, 4) if price else None
    conn, cur = s._cursor()
    cur.execute(f"UPDATE prints SET energy_wh={s.ph}, cost={s.ph}, peak_w={s.ph}, "
                f"ended_at={s.ph} WHERE job_id={s.ph}",
                (round(wh, 2), cost, round(peak, 1), new_end, p["job_id"]))
    if s.backend == "sqlite":
        conn.commit()
    else:
        cur.close(); conn.close()

    fixed = "" if new_end == p["ended_at"] else \
            f"   ended_at {(p['ended_at'] - new_end) / 60:.0f} min too late -> corrected"
    print(f"{p['job_id']}  {(p.get('label') or p.get('name') or '')[:34]:<34} "
          f"{len(rows):>4} samples   was {p['energy_wh']} Wh -> now {wh:.2f} Wh "
          f"({cur_sym}{cost})  peak {peak:.0f} W{fixed}")

print("\nDone. Start the app again so it picks up the corrected totals.")
