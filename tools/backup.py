#!/usr/bin/env python3
"""Back the database up to a JSON file, and put it back.

The Settings page can do this by hand. This is the version you point a
scheduler at, so it happens whether or not anyone remembers.

    python tools/backup.py export                      # into ./backups
    python tools/backup.py export --out /volume1/backup --keep 30
    python tools/backup.py export --secrets            # include credentials
    python tools/backup.py show    backups/bambu-monitor-20260831-2137.json
    python tools/backup.py restore backups/....json            # says what it WOULD do
    python tools/backup.py restore backups/....json --apply
    python tools/backup.py restore backups/....json --mode replace --apply

Restore is a dry run until `--apply`, because the interesting case is the one
where you are already upset about something.

On a Synology, Task Scheduler > Create > Scheduled Task > User-defined script,
daily, as root:

    /volume1/apps/bambu-monitor/venv/bin/python3 \\
        /volume1/apps/bambu-monitor/tools/backup.py export \\
        --out /volume1/backup/bambu --keep 30

Put --out somewhere Hyper Backup already covers and the print history is part
of your normal off-site backup.
"""
import glob
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import backup as bk          # noqa: E402
import config_store          # noqa: E402

args = sys.argv[1:]
cmd = args[0] if args else ""


def opt(name, default=None):
    return args[args.index(name) + 1] if name in args and args.index(name) + 1 < len(args) \
        else default


def flag(name):
    return name in args


def die(msg):
    sys.exit(msg)


if cmd == "export":
    out_dir = opt("--out") or os.path.join(HERE, "backups")
    keep = int(opt("--keep") or 0)
    store, _cfg = config_store.open_live()
    data = bk.export(store, include_secrets=flag("--secrets"),
                     include_images=not flag("--no-images"))
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError as e:
        die(f"cannot write to {out_dir}: {e}")

    name = f"bambu-monitor-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path = os.path.join(out_dir, name)
    # written to a temporary name and moved into place, so a backup that is
    # interrupted never leaves a half-file that looks like a good one
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, ensure_ascii=False, default=str)
    os.replace(tmp, path)
    size = os.path.getsize(path)
    print(f"{path}  ({size / 1024:.0f} KB)")
    print("  " + bk.summarise(data))
    if flag("--secrets"):
        print("  this file CONTAINS the printer access code and account passwords")

    if keep > 0:
        old = sorted(glob.glob(os.path.join(out_dir, "bambu-monitor-*.json")))
        for p in old[:-keep]:
            os.unlink(p)
            print(f"  removed {os.path.basename(p)}")
        print(f"  keeping the newest {min(keep, len(old))}")
    sys.exit(0)

if cmd == "show":
    if len(args) < 2:
        die("show which file?")
    with open(args[1], encoding="utf-8") as fh:
        data = json.load(fh)
    why = bk.check(data)
    if why:
        die(f"not usable: {why}")
    print(bk.summarise(data))
    for table, n in (data.get("counts") or {}).items():
        print(f"  {table:<14} {n}")
    for table, why in (data.get("excluded") or {}).items():
        print(f"  (not included: {table} - {why})")
    sys.exit(0)

if cmd == "restore":
    if len(args) < 2 or args[1].startswith("-"):
        die("restore which file?")
    mode = opt("--mode") or "merge"
    if mode not in ("merge", "replace"):
        die(f"--mode is merge or replace, not {mode!r}")
    with open(args[1], encoding="utf-8") as fh:
        data = json.load(fh)
    why = bk.check(data)
    if why:
        die(f"refusing to restore: {why}")

    store, _cfg = config_store.open_live()
    apply_it = flag("--apply")
    print(bk.summarise(data))
    report = bk.restore(store, data, mode=mode, dry_run=not apply_it)
    print(f"\nmode: {mode}" + ("" if apply_it else "   (dry run)"))
    for table, e in report["tables"].items():
        if not any(e.values()):
            continue
        bits = [f"{e['inserted']} in"]
        if e["skipped"]:
            bits.append(f"{e['skipped']} already there")
        if e["deleted"]:
            bits.append(f"{e['deleted']} REPLACED")
        print(f"  {table:<14} {', '.join(bits)}")
    if not apply_it:
        print("\nnothing was written. Re-run with --apply to do it.")
        if mode == "replace" and report["deleted"]:
            print(f"note: --mode replace would DELETE {report['deleted']} existing "
                  f"row(s) before inserting. Use the default merge unless you mean it.")
    else:
        print(f"\ndone: {report['inserted']} row(s) inserted, "
              f"{report['skipped']} already there, {report['deleted']} replaced.")
        print("Restart the app so it picks up restored settings.")
    sys.exit(0)

die(__doc__)
