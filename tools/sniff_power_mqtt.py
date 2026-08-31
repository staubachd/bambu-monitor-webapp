#!/usr/bin/env python3
"""Find the topic and field names a smart plug is publishing.

Settings > Power asks for a topic and two field names. Nobody knows those by
heart, and a wrong one fails silently - the app connects, subscribes, and simply
never hears anything. So: listen to a broker for a while and print what is
actually there, with the plausible power fields marked.

    python tools/sniff_power_mqtt.py 192.168.1.10
    python tools/sniff_power_mqtt.py 192.168.1.10 --topic 'zigbee2mqtt/#' --secs 60
    python tools/sniff_power_mqtt.py 192.168.1.10 --user me --password secret

Read-only: it subscribes and prints. Nothing is stored and nothing is published.
"""
import json
import sys
import time

HOST = None
PORT = 1883
TOPIC = "#"
SECS = 30.0
USER = PASSWORD = None

args = sys.argv[1:]
i = 0
while i < len(args):
    a = args[i]
    if a in ("--topic", "--secs", "--user", "--password", "--port") and i + 1 < len(args):
        v = args[i + 1]
        if a == "--topic":
            TOPIC = v
        elif a == "--secs":
            SECS = float(v)
        elif a == "--user":
            USER = v
        elif a == "--password":
            PASSWORD = v
        else:
            PORT = int(v)
        i += 2
        continue
    if a.startswith("-"):
        sys.exit(__doc__)
    HOST = a
    i += 1

if not HOST:
    sys.exit(__doc__)

try:
    import paho.mqtt.client as mqtt
except ImportError:
    sys.exit("paho-mqtt is not installed - run `python -m pip install -r requirements.txt`")

# what a power reading tends to be called, across the bridges people run
WATT_HINTS = ("power", "apparentpower", "activepower", "watts", "w",
              "current_power", "load", "power_w")
ENERGY_HINTS = ("energy", "total", "energy_total", "today", "consumption",
                "totalenergy", "energy_kwh", "summation")

seen = {}          # topic -> (payload, count)


def leaves(payload, prefix=""):
    """Every scalar in the message, as dotted paths - the form the setting takes."""
    if isinstance(payload, dict):
        for k, v in payload.items():
            yield from leaves(v, f"{prefix}{k}.")
    elif isinstance(payload, list):
        for n, v in enumerate(payload):
            yield from leaves(v, f"{prefix}{n}.")
    else:
        yield prefix[:-1], payload


def on_connect(client, _u, _f, rc, *a):
    if int(rc) != 0:
        print(f"the broker refused the connection (code {rc})")
        return
    client.subscribe(TOPIC)
    print(f"connected to {HOST}:{PORT}, listening on '{TOPIC}' for {SECS:.0f}s ...\n")


def on_message(client, _u, msg):
    try:
        raw = msg.payload.decode("utf-8", "replace").strip()
    except Exception:
        return
    try:
        payload = json.loads(raw)
    except ValueError:
        payload = raw
    prev = seen.get(msg.topic)
    seen[msg.topic] = (payload, (prev[1] + 1) if prev else 1)


try:
    client = mqtt.Client(client_id=f"bambu-sniff-{int(time.time())}")
except TypeError:                                  # paho 2.x
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1,
                         client_id=f"bambu-sniff-{int(time.time())}")
if USER:
    client.username_pw_set(USER, PASSWORD)
client.on_connect = on_connect
client.on_message = on_message
try:
    client.connect(HOST, PORT, keepalive=30)
except Exception as e:
    sys.exit(f"cannot reach {HOST}:{PORT} - {type(e).__name__}: {e}")

client.loop_start()
time.sleep(SECS)
client.loop_stop()
try:
    client.disconnect()
except Exception:
    pass

if not seen:
    print("nothing arrived.\n"
          "  - is the topic right? '#' listens to everything\n"
          "  - does the broker need a user and password?\n"
          "  - a plug that only reports on change may need switching on and off")
    raise SystemExit(1)

print(f"{len(seen)} topic(s) heard\n")
candidates = []
for topic in sorted(seen):
    payload, count = seen[topic]
    fields = list(leaves(payload)) if isinstance(payload, (dict, list)) else [("", payload)]
    numeric = [(k, v) for k, v in fields if isinstance(v, (int, float))
               and not isinstance(v, bool)]
    watt = [k for k, _ in numeric if k.split(".")[-1].lower() in WATT_HINTS]
    energy = [k for k, _ in numeric if any(h in k.split(".")[-1].lower() for h in ENERGY_HINTS)]
    mark = "  <-- looks like a power meter" if watt else ""
    print(f"{topic}   ({count} message(s)){mark}")
    for k, v in fields[:14]:
        tag = ""
        if k in watt:
            tag = "   <-- Field: watts"
        elif k in energy:
            tag = "   <-- Field: energy total"
        print(f"    {k or '(value)':<28} {v!r}{tag}")
    if len(fields) > 14:
        print(f"    ... and {len(fields) - 14} more")
    print()
    if watt:
        candidates.append((topic, watt[0], energy[0] if energy else ""))

if candidates:
    print("--- what to put in Settings > Power ---")
    for topic, w, e in candidates:
        print(f"  Topic              {topic}")
        print(f"  Field: watts       {w}")
        print(f"  Field: energy total{'':1}{e or '(none found - leave the default)'}")
        print(f"  Energy unit        kWh if the number looks like a running total in "
              f"kWh, else Wh")
        print()
else:
    print("--- nothing here looks like a power reading ---")
    print("A plug with energy monitoring usually publishes a 'power' field in watts.")
    print("If yours is not listed, check it is paired and reporting in Zigbee2MQTT,")
    print("and try a longer --secs: some plugs only publish when the load changes.")
