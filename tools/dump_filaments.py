#!/usr/bin/env python3
"""Read-only: why does one filament appear twice on the Filament page?

A filament's identity is 'SKU|COLOUR', and it is built from two independent
sources that must agree:

  * the AMS, live      -> tray_info_idx + tray_color   (stored in `filaments`)
  * the cloud, per job -> filamentId + targetColor     (stored in prints.filament_detail)

When those disagree by even one character the same spool becomes two rows: the
AMS one carries the name, the print one carries the grams. This lists every
identity, says where it came from, and flags SKUs that hold several colours
where some are named and some are not - the shape of that split.

Writes nothing.

    python tools/dump_filaments.py
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import filament_catalog as fc           # noqa: E402
from storage import Storage             # noqa: E402

with open(os.path.join(HERE, "printer.config.json"), encoding="utf-8") as fh:
    cfg = json.load(fh)
scfg = cfg.get("storage", {"backend": "sqlite"})
if scfg.get("backend", "sqlite") == "sqlite" and not os.path.isabs(scfg.get("sqlite_path", "telemetry.db")):
    scfg = {**scfg, "sqlite_path": os.path.join(HERE, scfg.get("sqlite_path", "telemetry.db"))}
store = Storage(scfg)

cat = {f["fkey"]: f for f in store.all_filaments()}
used: dict = {}
raw_colours: dict = {}
for p in store.all_prints():
    try:
        entries = json.loads(p.get("filament_detail") or "[]") or []
    except (TypeError, ValueError):
        continue
    for e in entries:
        k = fc.key(e.get("filament_id"), e.get("color"), e.get("type"))
        u = used.setdefault(k, {"grams": 0.0, "prints": 0, "last": None})
        u["grams"] += float(e.get("grams") or 0)
        u["prints"] += 1
        if p.get("started_at"):
            u["last"] = max(u["last"] or 0, p["started_at"])
        raw_colours.setdefault(k, set()).add(repr(e.get("color")))

print(f"catalogue (from the AMS): {len(cat)} identities")
print(f"used (from print detail): {len(used)} identities\n")

keys = sorted(set(cat) | set(used))
print(f"{'identity':22}{'src':6}{'name':26}{'code':10}{'grams':>9}  raw colour in prints")
for k in keys:
    c, u = cat.get(k, {}), used.get(k)
    src = "both" if (c and u) else ("AMS" if c else "print")
    name = " ".join(x for x in (c.get("vendor"), c.get("product"), c.get("color_name")) if x)
    print(f"{k:22}{src:6}{(name or '—')[:25]:26}{str(c.get('code') or '—'):10}"
          f"{(u['grams'] if u else 0):>9.1f}  {','.join(sorted(raw_colours.get(k, ()))) or '—'}")

# the split: one SKU, several colours, some named and some not
print()
by_sku: dict = {}
for k in keys:
    by_sku.setdefault(k.split("|")[0], []).append(k)
flagged = False
for sku, ks in sorted(by_sku.items()):
    if len(ks) < 2:
        continue
    # "named" means anything a human put there, not just a colour name - a
    # vendor-only row (a third-party spool) is named too
    named = [k for k in ks if any((cat.get(k) or {}).get(f)
                                  for f in ("color_name", "product", "vendor"))]
    bare = [k for k in ks if k not in named]
    if named and bare:
        flagged = True
        print(f"SUSPECT {sku}: {len(named)} named, {len(bare)} unnamed")
        for k in named:
            print(f"    named   {k}  {cat[k].get('color_name')}  "
                  f"used {used.get(k, {}).get('grams', 0):.1f} g")
        for k in bare:
            g = used.get(k, {}).get("grams", 0)
            when = used.get(k, {}).get("last")
            print(f"    unnamed {k}  used {g:.1f} g"
                  + (f"  last {datetime.fromtimestamp(when):%Y-%m-%d}" if when else ""))
        print("    -> same product, different colour string on the two sides")
if not flagged:
    print("no split identities found - every SKU's colours are consistently named")
