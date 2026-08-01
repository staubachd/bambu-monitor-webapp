#!/usr/bin/env python3
"""
Bambu Lab X2D - local MQTT telemetry probe.

Goal: find out whether the X2D pushes status to its LOCAL MQTT broker
while the printer stays in Cloud mode (no Developer Mode).

Usage:
    pip install paho-mqtt
    python test_mqtt_local.py <printer-ip> <access-code> <serial>

What it does:
    - Connects to <printer-ip>:8883 over TLS (Bambu uses a self-signed cert,
      so verification is disabled - normal for local Bambu access).
    - Subscribes to device/<serial>/report
    - Sends one "pushall" request to force a full status dump.
    - Prints whatever comes back for ~20 seconds.

Interpreting the result:
    - You see a big JSON blob with temps/print/ams  -> Path A (local) WORKS.
    - Connects but total silence for 20s            -> X2D behaves like H2;
                                                       use Path B (Bambu Cloud MQTT)
                                                       or enable Developer Mode.
"""
import json
import ssl
import sys
import time

try:
    import paho.mqtt.client as mqtt
except ImportError:
    sys.exit("Missing dependency. Run:  pip install paho-mqtt")

if len(sys.argv) != 4:
    sys.exit("Usage: python test_mqtt_local.py <printer-ip> <access-code> <serial>")

HOST, ACCESS_CODE, SERIAL = sys.argv[1], sys.argv[2], sys.argv[3]
REPORT_TOPIC = f"device/{SERIAL}/report"
REQUEST_TOPIC = f"device/{SERIAL}/request"
PUSHALL = {"pushing": {"sequence_id": "0", "command": "pushall"}}

got_data = False


def on_connect(client, userdata, flags, rc, *_):
    if rc == 0:
        print(f"[ok] connected to {HOST}:8883, subscribing to {REPORT_TOPIC}")
        client.subscribe(REPORT_TOPIC)
        client.publish(REQUEST_TOPIC, json.dumps(PUSHALL))
        print("[..] sent pushall request; waiting for telemetry...")
    else:
        print(f"[!!] connect failed, rc={rc} (check IP / access code)")


def on_message(client, userdata, msg):
    global got_data
    got_data = True
    try:
        payload = json.loads(msg.payload)
    except ValueError:
        payload = msg.payload[:200]
    print(f"\n[DATA] {msg.topic}:")
    print(json.dumps(payload, indent=2)[:2000])


client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2) if hasattr(mqtt, "CallbackAPIVersion") else mqtt.Client()
client.username_pw_set("bblp", ACCESS_CODE)
client.tls_set(cert_reqs=ssl.CERT_NONE, tls_version=ssl.PROTOCOL_TLS)
client.tls_insecure_set(True)
client.on_connect = on_connect
client.on_message = on_message

print(f"[..] connecting to {HOST}:8883 as user 'bblp' ...")
client.connect(HOST, 8883, 60)
client.loop_start()
time.sleep(20)
client.loop_stop()

print("\n" + "=" * 50)
if got_data:
    print("RESULT: Path A (LOCAL MQTT) WORKS in Cloud mode.  -> build on this.")
else:
    print("RESULT: no data in 20s. X2D likely needs Path B (Bambu Cloud MQTT)")
    print("        or Developer Mode. Tell Claude and we'll switch approach.")
