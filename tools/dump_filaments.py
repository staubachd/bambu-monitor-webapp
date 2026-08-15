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
unattributed = []          # grams the Filament page cannot place
for p in store.all_prints():
    try:
        entries = json.loads(p.get("filament_detail") or "[]") or []
    except (TypeError, ValueError):
        entries = []
    grams = p.get("filament_g_manual")
    if grams is None:
        grams = p.get("filament_g")
    if grams and not entries:
        # History and Statistics use this per-print total; the Filament page
        # sums the per-slot breakdown, so a print with one and not the other is
        # counted on two pages out of three
        unattributed.append((p.get("job_id"), p.get("started_at"), grams,
                             p.get("label") or p.get("design_title") or p.get("name") or ""))
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

print()
if unattributed:
    tot = sum(u[2] for u in unattributed)
    print(f"{len(unattributed)} print(s) carry grams the Filament page cannot place "
          f"({tot:.1f} g in total):")
    for job, started, grams, name in sorted(unattributed, key=lambda u: -(u[1] or 0)):
        when = datetime.fromtimestamp(started).strftime("%Y-%m-%d") if started else "?"
        print(f"    {job:12} {when}  {grams:8.1f} g  {name[:38]}")
    print("    -> the cloud gave a total weight but no per-slot breakdown, so the")
    print("       History and Statistics tabs count these grams and Filament does not")
else:
    print("every print's grams are attributed to a filament")

# --- what each recent print was credited to, and what the AMS held ------------
n = 6
for a in sys.argv[1:]:
    if a.isdigit():
        n = int(a)
recent = sorted(store.all_prints(), key=lambda p: -(p.get("started_at") or 0))[:n]
print(f"\nlast {len(recent)} prints - what each slot was credited to:")
for p in recent:
    when = (datetime.fromtimestamp(p["started_at"]).strftime("%Y-%m-%d %H:%M")
            if p.get("started_at") else "?")
    name = (p.get("label") or p.get("design_title") or p.get("name") or "")[:34]
    print(f"\n  {p['job_id']}  {when}  {name}")
    try:
        entries = json.loads(p.get("filament_detail") or "[]") or []
    except (TypeError, ValueError):
        entries = []
    try:
        slots = json.loads(p.get("ams_slots") or "{}") or {}
    except (TypeError, ValueError):
        slots = {}
    if not entries:
        print(f"      no per-slot detail (total {p.get('filament_g')} g)")
    for e in entries:
        s = str(e.get("slot"))
        snap = slots.get(s) or {}
        k = fc.key(e.get("filament_id"), e.get("color"), e.get("type"))
        named = cat.get(k, {})
        who = " ".join(x for x in (named.get("vendor"), named.get("product"),
                                   named.get("color_name")) if x) or "unnamed"
        print(f"      slot {s}: {e.get('grams')} g -> {k}  ({who})")
        if snap:
            flag = "" if (snap.get("color") or "") == (e.get("color") or "") \
                   else "   <-- MISMATCH, the AMS held a different colour"
            print(f"              AMS then: {snap.get('sku')}|{snap.get('color')}"
                  f" {snap.get('code') or ''}{flag}")
        elif slots:
            print(f"              AMS then: (no snapshot for slot {s})")
