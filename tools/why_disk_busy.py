#!/usr/bin/env python3
"""What is this app actually writing, right now?

Run it while the app is running and the printer is idle. It samples /api/diag
twice and reports the rate of every SQL statement in between, so "the NAS is
writing constantly" becomes a number attached to a cause instead of a guess.

    python tools/why_disk_busy.py           # 60-second window
    python tools/why_disk_busy.py 180       # longer, for slow pollers
    python tools/why_disk_busy.py --url http://192.168.1.5:8770

An idle printer in Auto mode should show close to zero writes per minute. If it
does not, the offending statement is named in the output.
"""
import json
import os
import sys
import time
import urllib.request

PORT = os.environ.get("BAMBU_PORT", "8770")
URL = f"http://127.0.0.1:{PORT}"
WINDOW = 60.0

args = sys.argv[1:]
if "--url" in args:
    URL = args[args.index("--url") + 1].rstrip("/")
    del args[args.index("--url"):args.index("--url") + 2]
for a in args:
    if a.replace(".", "").isdigit():
        WINDOW = float(a)

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS = [os.path.join(HERE, n) for n in ("app.log", "app.err")]


def diag():
    try:
        with urllib.request.urlopen(URL + "/api/diag", timeout=10) as r:
            return json.load(r)
    except Exception as e:
        sys.exit(f"cannot reach {URL}/api/diag - is the app running, and new "
                 f"enough to have that endpoint?\n  {type(e).__name__}: {e}")


def logsize():
    return {p: os.path.getsize(p) for p in LOGS if os.path.exists(p)}


a, la = diag(), logsize()
print(f"backend        : {a['backend']}")
print(f"recording mode : {a['recording_mode']}  (active right now: {a['recording_active']})")
print(f"uptime         : {a['uptime_sec'] / 3600:.1f} h")
print(f"sample interval: {a['sample_interval_sec']}s")
print(f"\nwatching for {WINDOW:.0f}s ...", flush=True)
time.sleep(WINDOW)
b, lb = diag(), logsize()

secs = b["uptime_sec"] - a["uptime_sec"]
if secs <= 0:
    sys.exit("the app restarted during the window - run it again")

frames = b["mqtt_frames"] - a["mqtt_frames"]
print(f"\nprinter pushed {frames} report(s) in {secs:.0f}s "
      f"= {frames / secs * 60:.1f}/min")
if frames:
    print("  (anything below that runs on_message happens this often)")

delta = {}
for k, v in b["statements"].items():
    d = v - a["statements"].get(k, 0)
    if d:
        delta[k] = d

writes = {k: v for k, v in delta.items()
          if k.split()[0] in ("INSERT", "UPDATE", "DELETE", "REPLACE")}
reads = {k: v for k, v in delta.items() if k not in writes}

print(f"\nWRITES in the window: {sum(writes.values())} "
      f"= {sum(writes.values()) / secs * 60:.1f}/min")
if not writes:
    print("  none - the app is not what is writing to the disk")
for k, v in sorted(writes.items(), key=lambda x: -x[1]):
    per = v / secs * 60
    note = ""
    if frames and abs(v / frames - 1) < 0.35:
        note = "  <-- once per printer report; this is the one"
    elif per > 30:
        note = "  <-- high"
    print(f"  {k:<28} {v:>6}   {per:>8.1f}/min{note}")

print(f"\nreads in the window: {sum(reads.values())} "
      f"= {sum(reads.values()) / secs * 60:.1f}/min")
for k, v in sorted(reads.items(), key=lambda x: -x[1])[:8]:
    per = v / secs * 60
    note = "  <-- once per printer report" if frames and abs(v / frames - 1) < 0.35 else ""
    print(f"  {k:<28} {v:>6}   {per:>8.1f}/min{note}")

grew = {p: lb[p] - la.get(p, 0) for p in lb if lb[p] - la.get(p, 0) > 0}
if grew:
    print("\nlog files grew:")
    for p, n in grew.items():
        print(f"  {os.path.basename(p):<28} {n / secs * 60 / 1024:>8.1f} KB/min")
        if n / secs > 1024:
            print("      <-- the log is being written faster than the database; "
                  "look at what is printing")
else:
    print("\nlog files: not growing")

print("\n--- what to make of it ---")
if not writes and not grew:
    print("This app wrote nothing. Whatever is keeping the disk busy is")
    print("something else - check Synology Resource Monitor for the process,")
    print("and remember MariaDB itself writes for other databases too.")
elif a["recording_mode"] == "on":
    print("Recording is set to ON, so a telemetry row is written every")
    print(f"{a['sample_interval_sec']}s whether or not anything is printing.")
    print("Auto records only during a print (plus a tail) and is what lets the")
    print("NAS disks hibernate. Change it with the Auto/An/Aus control.")
elif writes:
    top = max(writes.items(), key=lambda x: x[1])
    print(f"The biggest writer is: {top[0]}")
    if frames and abs(top[1] / frames - 1) < 0.35:
        print("It fires once per printer report, which is not a rate anything")
        print("should be written at. That is a bug - send this output.")
