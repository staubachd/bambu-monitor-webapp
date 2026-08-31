"""The diagnostic for "the spool is in the AMS but the page says it is not".

The page marks a filament "in slot N" only when the live tray's identity is the
SAME identity as the row - and for a third-party spool the identity is built
from a borrowed slicer profile and a typed-in colour, so the same physical spool
easily becomes two rows. This tool asks the running app what is in each tray,
works out the identity that makes, and says whether a row exists under it.

Its whole value is naming the near-miss: "same colour, other SKU" is the sentence
that turns a mystery into a merge. It writes nothing, and that has to stay true -
a diagnostic that repairs things is one you cannot run while confused.
"""
import sys, os, json, subprocess, threading, http.server, socketserver
NL = chr(10)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import os as _os
SRC_DIR = (_os.environ.get("BAMBU_SRC")
           or _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(HERE, "tools", "why_not_in_ams.py")
assert os.path.exists(TOOL), (
    f"{TOOL} is missing - run this through tests/runall.ps1")

STATE = {"ams": {"units": [{"trays": [
    {"id": 0, "filament_id": "GFL99", "color": "7C4B00", "type": "PLA",
     "remain_pct": -1, "is_bambu": False},
    {"id": 1, "filament_id": "GFA00", "color": "FFFFFF", "type": "PLA",
     "remain_pct": 80, "is_bambu": True},
]}], "external": []}}

FILAMENTS = {"filaments": [
    # the tray in slot 2 has a row, and the page shows it as loaded
    {"fkey": "GFA00|FFFFFF", "filament_id": "GFA00", "color": "FFFFFF", "type": "PLA",
     "product": "PLA Basic", "color_name": "Jade White", "loaded": True, "slot": 2},
    # slot 1's spool: named under the same colour but a DIFFERENT profile, so the
    # page has no row under the identity the AMS is making
    {"fkey": "GFL96|7C4B00", "filament_id": "GFL96", "color": "7C4B00", "type": "PLA",
     "product": "PLA Meta", "color_name": "Coffee Brown", "loaded": False, "slot": None},
]}


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps(STATE if self.path.startswith("/api/state") else FILAMENTS)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *a):
        pass


with socketserver.TCPServer(("127.0.0.1", 0), Handler) as srv:
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    out = subprocess.run([sys.executable, TOOL, f"http://127.0.0.1:{port}"],
                         capture_output=True, text=True, cwd=HERE)
    srv.shutdown()

report = out.stdout
assert out.returncode == 0, out.stdout + out.stderr
assert report.strip(), "the tool said nothing at all"

# --- the tray that IS matched is reported as fine ---------------------------
# the tool prints the identity and its verdict on consecutive lines
lines = report.splitlines()
at = [i for i, l in enumerate(lines) if "GFA00|FFFFFF" in l]
assert at, "the matched tray is not in the report:" + NL + report
verdict = NL.join(lines[at[0]:at[0] + 3])
assert "OK" in verdict and "matches the row" in verdict, (
    "the correctly-loaded tray was not reported as OK:" + NL + verdict)
print("the matched tray:", lines[at[0] + 1].strip()[:74])

# --- and the one that is not gets the sentence that explains it ------------
assert "GFL99|7C4B00" in report, "the unmatched tray's identity is not shown"
assert "NO row" in report, "it does not say the page has no row for that identity"
assert "same colour, other SKU" in report, (
    "the near-miss is not named - that line is the entire point of the tool, "
    "because it is what tells you to merge rather than to rename")
assert "GFL96|7C4B00" in report, "the near-miss identity itself is not shown"
assert "Merge" in report, "it diagnoses without saying what to do about it"
near = [l for l in report.splitlines() if "same colour, other SKU" in l]
print("the unmatched tray:", near[0].strip()[:78])

# --- read-only, and that is checked in the source, not just observed -------
src = open(TOOL, encoding="utf-8").read()
for danger in ("update_print_fields", "upsert_filament", "set_filament",
               "delete_", "INSERT", "UPDATE ", "requests.post", "method=\"POST\""):
    assert danger not in src, (
        f"the tool contains {danger!r} - it is documented as read-only, and a "
        f"diagnostic that changes things is one you cannot safely run while lost")
assert "Writes nothing" in src or "writes nothing" in src.lower()
print("read-only: nothing in it writes, and it says so")

# --- it fails politely when the app is not running -------------------------
dead = subprocess.run([sys.executable, TOOL, "http://127.0.0.1:1"],
                      capture_output=True, text=True, cwd=HERE)
msg = (dead.stdout + dead.stderr).lower()
assert dead.returncode != 0, "an unreachable app was reported as a successful run"
assert "http://127.0.0.1:1" in dead.stdout + dead.stderr, \
    "it does not say which address it could not reach"
print("with no app running it names the address it tried")
print("ok")
