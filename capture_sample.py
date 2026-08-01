#!/usr/bin/env python3
"""
Capture one full Bambu X2D MQTT 'report' payload to a file for schema design.

Usage:
    python capture_sample.py <printer-ip> <access-code> <serial>

Writes the largest report it sees within 25s to:  sample_report.json
(The 'pushall' response is the big one containing the complete state.)
"""
import json
import ssl
import sys
import time
import paho.mqtt.client as mqtt

if len(sys.argv) != 4:
    sys.exit("Usage: python capture_sample.py <printer-ip> <access-code> <serial>")

HOST, ACCESS_CODE, SERIAL = sys.argv[1], sys.argv[2], sys.argv[3]
REPORT = f"device/{SERIAL}/report"
REQUEST = f"device/{SERIAL}/request"
OUT = "f:/Claude/bambu-monitor/sample_report.json"

biggest = {"len": 0, "payload": None}


def on_connect(c, u, f, rc, *_):
    c.subscribe(REPORT)
    c.publish(REQUEST, json.dumps({"pushing": {"sequence_id": "0", "command": "pushall"}}))
    print("[..] requested full state; capturing for 25s...")


def on_message(c, u, msg):
    if len(msg.payload) > biggest["len"]:
        biggest["len"] = len(msg.payload)
        biggest["payload"] = msg.payload
        print(f"[..] captured report ({len(msg.payload)} bytes)")


client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
client.username_pw_set("bblp", ACCESS_CODE)
client.tls_set(cert_reqs=ssl.CERT_NONE, tls_version=ssl.PROTOCOL_TLS)
client.tls_insecure_set(True)
client.on_connect = on_connect
client.on_message = on_message
client.connect(HOST, 8883, 60)
client.loop_start()
time.sleep(25)
client.loop_stop()

if biggest["payload"]:
    data = json.loads(biggest["payload"])
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    print(f"\n[ok] wrote {OUT} ({biggest['len']} bytes)")
    print("     Now tell Claude it's ready; it will read the file.")
else:
    print("\n[!!] no report captured - is a print idle/running? try again")
