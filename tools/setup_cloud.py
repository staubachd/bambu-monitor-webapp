#!/usr/bin/env python3
"""
Log in to the Bambu Cloud once, store the token, and show what a print task
actually looks like (that's where per-print filament grams come from).

    python setup_cloud.py

Handles the email verification code and 2FA prompts interactively. The password
is typed hidden; it is stored so the app can re-authenticate when the token
expires. Nothing leaves your machine except the login to Bambu.
"""
import getpass
import json
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # repo root (tools/ is one level down)
sys.path.insert(0, HERE)
from bambu_cloud import BambuCloud, CloudError  # noqa: E402

CFG_PATH = os.path.join(HERE, "printer.config.json")
cfg = json.load(open(CFG_PATH, encoding="utf-8"))
cloud = cfg.setdefault("cloud", {})
serial = cfg.get("serial")

email = input(f"Bambu account email [{cloud.get('email', '')}]: ").strip() or cloud.get("email", "")

# Pasting into a hidden prompt is unreliable over some SSH clients (silently
# truncated), so allow reusing a stored password, or showing what was typed.
stored = cloud.get("password")
if stored:
    print("A password is already stored - press Enter to reuse it.")
if "--show-password" in sys.argv:
    password = input("Bambu account password (visible): ").strip()
else:
    password = getpass.getpass(
        "Bambu account password%s: " % (" [Enter = stored]" if stored else ""))
if not password and stored:
    password = stored
    print("   using the stored password")
elif password:
    print(f"   received {len(password)} characters")

if not email or not password:
    sys.exit("email and password are both required "
             "(tip: re-run with --show-password if pasting is unreliable)")

c = BambuCloud()
try:
    res = c.login(email, password)
except CloudError as e:
    sys.exit(f"[!!] login failed: {e}")

# --- second factor, if Bambu asks for one ---
if not c.token:
    if res.get("tfaKey"):
        code = input("Two-factor code from your authenticator app: ").strip()
        try:
            c.login_tfa(res["tfaKey"], code)
        except CloudError as e:
            sys.exit(f"[!!] 2FA failed: {e}")
    else:
        print("\nBambu wants an emailed verification code - sending one now ...")
        try:
            c.send_code(email)
        except CloudError as e:
            print(f"    (could not trigger the email: {e})")
        code = input("Verification code from your email: ").strip()
        try:
            c.login_code(email, code)
        except CloudError as e:
            sys.exit(f"[!!] code login failed: {e}")

if not c.token:
    sys.exit(f"[!!] no token returned. Raw login response:\n{json.dumps(res, indent=2)[:800]}")

print("\n[ok] authenticated")

try:
    tasks = c.get_tasks(serial=serial, limit=5)
except CloudError as e:
    sys.exit(f"[!!] token obtained but fetching tasks failed: {e}")

print(f"[ok] {len(tasks)} recent task(s) for printer {serial}\n")
if tasks:
    print("=" * 66)
    print("MOST RECENT TASK - full JSON (this defines the fields we can use):")
    print("=" * 66)
    print(json.dumps(tasks[0], indent=2, ensure_ascii=False)[:3000])
    print("=" * 66)
    print("\nSummary of all fetched tasks:")
    for t in tasks:
        print(f"  id={t.get('id')}  '{str(t.get('title'))[:30]}'  "
              f"weight={t.get('weight')}  status={t.get('status')}  "
              f"start={t.get('startTime')}")
else:
    print("(no tasks returned - if you only ever print in LAN mode, the cloud "
          "may have no history for this printer)")

cloud.update(enabled=True, email=email, password=password,
             token=c.token, poll_min=int(cloud.get("poll_min", 10)))
with open(CFG_PATH, "w", encoding="utf-8") as fh:
    json.dump(cfg, fh, indent=2, ensure_ascii=False)
print("\n[ok] saved to printer.config.json (cloud enabled)")
