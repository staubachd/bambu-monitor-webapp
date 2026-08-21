#!/usr/bin/env python3
"""Read-only: why is the spool in the AMS not marked as loaded on the Filament page?

The page marks a filament "in slot N" only when the identity of the live tray is
the SAME identity as the row. An identity is 'SKU|RRGGBB', and it is built from
two sources that do not have to agree:

  * the AMS, live      -> tray_info_idx + tray_color
  * the cloud, per job -> filamentId (the SLICER PROFILE) + targetColor

For a third-party spool the AMS has no RFID to read, so the SKU is whatever
profile is set on the printer and the colour is whatever was typed in. Slice with
a different profile or a slightly different colour and the same physical spool
becomes two rows: the one you named, and the one the AMS is holding. Only the
second is "in slot N".

This asks the RUNNING app what is in each tray, works out the identity that makes,
and says whether the page has a row under it - naming the near-misses when it
does not. Writes nothing.

    python tools/why_not_in_ams.py                  # localhost, port from env
    python tools/why_not_in_ams.py http://nas:8770
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import filament_catalog as fc              # noqa: E402

BASE = (sys.argv[1] if len(sys.argv) > 1
        else f"http://127.0.0.1:{os.environ.get('BAMBU_PORT', '8770')}").rstrip("/")


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


try:
    state = get("/api/state")
    page = get("/api/filaments")
except Exception as e:
    print(f"cannot reach the app at {BASE}: {e}")
    print("Pass the base URL as an argument, e.g. python tools/why_not_in_ams.py "
          "http://192.168.1.50:8770")
    sys.exit(2)

rows = {r["fkey"]: r for r in page.get("filaments", []) if r.get("fkey")}
print(f"app      : {BASE}")
print(f"page     : {len(rows)} filament row(s)\n")

trays = []
for unit in (state.get("ams") or {}).get("units") or []:
    for tr in unit.get("trays") or []:
        trays.append((tr, False))
for tr in (state.get("ams") or {}).get("external") or []:
    trays.append((tr, True))

if not trays:
    print("the printer is not reporting any tray at all - nothing to match")
    sys.exit(0)

for tr, ext in trays:
    if not tr.get("type"):
        continue
    slot = "external" if ext else f"slot {(tr.get('id') or 0) + 1}"
    fkey = fc.key(tr.get("filament_id"), tr.get("color"), tr.get("type"))
    hit = rows.get(fkey)
    name = " ".join(x for x in (tr.get("brand"), tr.get("type")) if x)
    print(f"{slot:<10} {name or '?':<28} identity {fkey}")
    if hit:
        shown = hit.get("slot")
        label = " / ".join(x for x in (hit.get("vendor"), hit.get("product"),
                                       hit.get("color_name")) if x) or "(unnamed)"
        if shown or hit.get("external"):
            print(f"           -> matches the row '{label}', shown as loaded  OK")
        else:
            print(f"           -> row '{label}' exists but is NOT shown as loaded;"
                  f" report this, it is a bug rather than a data split")
        continue

    print(f"           -> the page has NO row under this identity")
    sku, _, hexc = fkey.partition("|")
    same_colour = [k for k in rows if k.endswith("|" + hexc) and k != fkey]
    same_sku = [k for k in rows if k.startswith(sku + "|") and k != fkey]
    for k in same_colour:
        r = rows[k]
        lbl = " / ".join(x for x in (r.get("vendor"), r.get("product"),
                                     r.get("color_name")) if x) or "(unnamed)"
        print(f"              same colour, other SKU : {k:<22} {lbl}"
              f"  [{r.get('grams', 0)} g]")
    for k in same_sku:
        r = rows[k]
        lbl = " / ".join(x for x in (r.get("vendor"), r.get("product"),
                                     r.get("color_name")) if x) or "(unnamed)"
        print(f"              same SKU, other colour : {k:<22} {lbl}"
              f"  [{r.get('grams', 0)} g]")
    if same_colour or same_sku:
        print("              -> the same spool under two identities. Merge them on"
              " the Filament page (the merge button on the row) and the slot appears.")
    else:
        print("              -> nothing close. The spool has simply never been"
              " printed with under this identity yet.")
    print()
