"""Per-day and per-month aggregation, and the rule that keeps them honest.

The one that matters: **a print is counted on the day it STARTED.** A job that
runs past midnight is one job, not two halves, and putting it in both buckets
would make the daily figures add up to more than the totals they are derived
from. The day chart drills into the history table, so a day whose bar says three
prints has to open three rows.

Everything else here is arithmetic that has to agree with itself: the days sum
to the totals, the months sum to the totals, and a failed print still counts as
having happened even though it produced nothing.
"""
import sys, os, json, time
from datetime import datetime, timedelta
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import os as _os
SRC_DIR = (_os.environ.get("BAMBU_SRC")
           or _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import app

c = app.app.test_client()
store = app.store

for r in store.all_prints():          # a clean table: this test is about sums
    store.delete_print(r["job_id"])

DAY = 86400.0
base = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
JOBS = []


def add(job, start, hours, state="FINISH", wh=100.0, grams=20.0, cost=0.03,
        fcost=0.40):
    JOBS.append(job)
    store.upsert_print(job_id=job, name=job, started_at=start.timestamp(),
                       ended_at=(start + timedelta(hours=hours)).timestamp(),
                       final_state=state, total_layers=10, energy_wh=wh, cost=cost,
                       peak_w=120.0)
    store.update_print_fields(job, filament_g=grams, filament_cost=fcost,
                              filament_detail=json.dumps([{
                                  "filament_id": "GFA00", "color": "000000",
                                  "type": "PLA", "grams": grams, "cost": fcost,
                                  "slot": 1}]))


today = base
yesterday = base - timedelta(days=1)
add("b-1", today, 2)
add("b-2", today, 1)
add("b-3", yesterday, 3, state="FAILED")
# The one that decides the rule: started at 23:00, finished at 02:00 the next
# day. Placed well away from the others so the day it spills into is empty -
# otherwise another print would be there to explain the count away.
midnight = base.replace(hour=23) - timedelta(days=6)
add("b-4", midnight, 3)

s = c.get("/api/stats").get_json()
days = {d["day"]: d for d in s["by_day"]}
months = {m["month"]: m for m in s["by_month"]}

# --- the midnight-spanning print is in exactly one bucket ------------------
started = midnight.strftime("%Y-%m-%d")
crossed = (midnight + timedelta(hours=3)).strftime("%Y-%m-%d")
assert started != crossed, "the fixture does not actually cross midnight"
assert days[started]["prints"] == 1, days.get(started)
assert crossed not in days or days[crossed]["prints"] == 0, (
    f"the job appears on {crossed} as well - counted twice, and every daily "
    f"figure is now larger than the total it came from")
print(f"a job from {midnight:%H:%M} to {midnight + timedelta(hours=3):%H:%M} "
      f"is counted once, on {started}")

# --- the days sum to the totals --------------------------------------------
assert sum(d["prints"] for d in s["by_day"]) == len(JOBS), (
    f"{sum(d['prints'] for d in s['by_day'])} prints across the days, {len(JOBS)} in total")
assert abs(sum(d["energy_wh"] for d in s["by_day"]) - s["totals"]["energy_wh"]) < 0.5
assert abs(sum(d["filament_g"] for d in s["by_day"]) - s["totals"]["filament_g"]) < 0.5
print("the daily buckets sum to the totals: prints, energy and filament")

# --- and so do the months ---------------------------------------------------
assert sum(m["prints"] for m in s["by_month"]) == len(JOBS)
assert abs(sum(m["energy_wh"] for m in s["by_month"]) - s["totals"]["energy_wh"]) < 0.5
print("so do the monthly buckets")

# --- a failed print still happened -----------------------------------------
yk = yesterday.strftime("%Y-%m-%d")
assert days[yk]["prints"] == 1, (
    f"the failed print is missing from {yk} - it consumed filament and "
    f"electricity whether or not it produced anything")
assert s["totals"]["failed"] == 1 and s["totals"]["finished"] == 3, s["totals"]
print("a failed print is counted, and shows in the success rate:",
      f"{s['totals']['finished']} ok / {s['totals']['failed']} failed")

# --- a print in the hours where UTC and local disagree ---------------------
# The bucket key must be the LOCAL date. East of UTC that means the small hours:
# 00:30 local is the previous day in UTC, and bucketing in UTC would file the
# print under yesterday while the history table - which filters on the local
# date - shows it under today. The bar would say 1 and the table open 0.
from datetime import timezone
small = base.replace(hour=0, minute=30) - timedelta(days=3)
utc_date = datetime.fromtimestamp(small.timestamp(), timezone.utc).strftime("%Y-%m-%d")
local_date = small.strftime("%Y-%m-%d")
add("b-tz", small, 1)
days_tz = {d["day"]: d for d in c.get("/api/stats").get_json()["by_day"]}
assert local_date in days_tz, (
    f"a print at 00:30 local is not filed under {local_date}; the buckets are "
    f"{sorted(days_tz)}")
if utc_date != local_date:
    assert utc_date not in days_tz, (
        f"the print at 00:30 local was filed under {utc_date}, which is its UTC "
        f"date - the table filters on the local date and would show that day empty")
    print(f"00:30 local ({local_date}) is not filed under its UTC date ({utc_date})")
else:
    print(f"00:30 local files under {local_date} "
          f"(this machine is on UTC, so the two cannot differ here)")

# --- the days are sorted, and dated in local time ---------------------------
s = c.get("/api/stats").get_json()          # refreshed: b-tz is in it now
order = [d["day"] for d in s["by_day"]]
assert order == sorted(order), f"the days are not in order: {order}"
assert today.strftime("%Y-%m-%d") in days, (
    "a print started at noon today is not filed under today - the bucket key is "
    "being built in UTC rather than in the clock the user reads")
print("days are ordered, and keyed by the local date:", order)

# --- a print with no start time cannot be filed anywhere -------------------
store.upsert_print(job_id="b-5", name="b-5", started_at=None, ended_at=None,
                   final_state="FINISH", total_layers=1)
JOBS.append("b-5")
s2 = c.get("/api/stats").get_json()
assert sum(d["prints"] for d in s2["by_day"]) == len(JOBS) - 1, (
    "a print with no start time was given a day anyway - which day would that be?")
print("a print with no start time is left out of the buckets rather than guessed at")

for j in JOBS:
    store.delete_print(j)
print("ok")
