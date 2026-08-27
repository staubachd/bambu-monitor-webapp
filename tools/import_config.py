#!/usr/bin/env python3
"""Move an old printer.config.json into the database, without the wizard.

The wizard is the normal path. This is the same operation with no browser,
for a headless upgrade over SSH and for the test harness, which needs a
configured app without a human clicking Next four times.

    python tools/import_config.py            # show what would happen
    python tools/import_config.py --write    # do it
    python tools/import_config.py --write --keep   # ...but leave the file in place

Nothing is guessed: only keys settings_schema knows about are imported, and
the storage block becomes instance/db.json unchanged.
"""
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import bootstrap          # noqa: E402
import config_store       # noqa: E402
import settings_schema    # noqa: E402
from storage import Storage  # noqa: E402

WRITE = "--write" in sys.argv
KEEP = "--keep" in sys.argv


def main() -> int:
    legacy = bootstrap.legacy()
    if legacy is None:
        print(f"no {os.path.basename(bootstrap.LEGACY)} to import - nothing to do")
        return 0

    db = bootstrap.clean(legacy.get("storage") or {"backend": "sqlite"})
    values, skipped = {}, []
    for spec in settings_schema.SCHEMA:
        cur, found = legacy, True
        for part in spec["path"].split("."):
            if not isinstance(cur, dict) or part not in cur:
                found = False
                break
            cur = cur[part]
        if not found:
            continue
        try:
            values[spec["path"]] = settings_schema.coerce(spec["path"], cur)
        except settings_schema.Invalid as e:
            skipped.append(f"{spec['path']}: {e}")

    shown = "sqlite " + db.get("sqlite_path", "") if db["backend"] == "sqlite" else \
            f"mariadb {db['mariadb']['user']}@{db['mariadb']['host']}:" \
            f"{db['mariadb']['port']}/{db['mariadb']['database']}"
    print(f"connection -> {bootstrap.PATH}\n             {shown}")
    print(f"settings   -> {len(values)} into the database")
    for p in sorted(values):
        v = "(secret)" if p in settings_schema.SECRETS else values[p]
        print(f"             {p} = {v}")
    for s in skipped:
        print(f"  SKIPPED  {s}")

    left = [k for k in legacy if k not in ("storage",)
            and not any(s["path"].split(".")[0] == k for s in settings_schema.SCHEMA)]
    if left:
        print(f"  not a setting, will be dropped: {', '.join(left)}")

    if not WRITE:
        print("\nnothing written - run again with --write")
        return 0

    ok, msg = bootstrap.test(db)
    if not ok:
        print(f"\nrefusing to write: the connection does not work - {msg}")
        return 1

    bootstrap.save(db)
    cfg = config_store.ConfigStore()
    cfg.attach(Storage(bootstrap.load()))
    cfg.set_many(values)
    print(f"\nwritten. {msg}")
    if KEEP:
        print(f"{os.path.basename(bootstrap.LEGACY)} left in place, but it is no "
              f"longer read by anything")
    else:
        dest = bootstrap.retire_legacy()
        print(f"renamed to {os.path.basename(dest)} - keep it until you are happy, "
              f"then delete it")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
