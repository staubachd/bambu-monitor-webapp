#!/usr/bin/env python3
"""
Configure + test the Tapo smart plug (P110 / P110M / P115) used for power monitoring.

    python setup_power.py

Asks for the plug's IP and your TP-Link account details, verifies it can actually
talk to the plug, and only then stores the settings. Everything it writes can
also be typed on the Settings page - this exists because it proves the plug
answers before storing anything.
The password is typed hidden and never echoed.

Note: the TP-Link *account* credentials are required even though the connection
is entirely local - the plug uses them for its local KLAP handshake. It must be
the account that owns the device in the Tapo app.
"""
import asyncio
import getpass
import json
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # repo root (tools/ is one level down)
sys.path.insert(0, HERE)

try:
    from tapo import ApiClient
except ImportError:
    sys.exit("The 'tapo' package is missing. Install it first:\n"
             "   python -m pip install tapo")

import config_store  # noqa: E402

_store, cfg = config_store.open_live()
p = cfg.section("power")

host = input(f"Tapo plug IP address [{p.get('host', '')}]: ").strip() or p.get("host", "")
email = input(f"TP-Link account email [{p.get('email', '')}]: ").strip() or p.get("email", "")
password = getpass.getpass("TP-Link account password: ")
model = p.get("model", "p110")

if not host or not email or not password:
    sys.exit("host, email and password are all required.")


async def probe():
    client = ApiClient(email, password)
    dev = await getattr(client, model)(host)
    info = await dev.get_device_info()
    cp = await dev.get_current_power()
    eu = await dev.get_energy_usage()
    name = getattr(info, "nickname", None) or getattr(info, "model", None) or "?"
    print("\n  connected to : " + str(name))
    print(f"  power now    : {cp.current_power} W")
    print(f"  today        : {eu.today_energy} Wh")
    print(f"  this month   : {eu.month_energy} Wh")


print(f"\n[..] contacting {model} at {host} ...")
try:
    asyncio.run(probe())
except Exception as e:
    sys.exit(f"\n[!!] FAILED: {e}\n"
             "     Check the IP, that the plug is on this network, and that the\n"
             "     email/password are the TP-Link account that owns the plug.")

cfg.set_many({"power.enabled": True, "power.model": model, "power.host": host,
              "power.email": email, "power.password": password,
              "power.poll_sec": int(p.get("poll_sec", 20))})

print("\n[ok] stored in the database and enabled (Settings > Power).")
print("     Restart the app to start recording power.")
