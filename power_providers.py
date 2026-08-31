"""Where the wattage comes from.

The printer reports no power at all, so it comes from whatever the printer is
plugged into. That used to be one hardcoded TP-Link Tapo loop; it is now a small
interface, because the plug someone owns is not a thing this app gets to choose.

    tapo   TP-Link P110/P115 - WiFi, polled directly over the LAN
    mqtt   anything that publishes readings to a broker: Zigbee2MQTT (so IKEA
           INSPELNING, which is Zigbee and has no address of its own), Shelly,
           Tasmota, Home Assistant

A provider owns its own loop, because the two shapes are genuinely different:
Tapo is polled and MQTT is pushed, and pretending otherwise would mean a poll
loop that mostly waits or a subscription that mostly sleeps.

What a provider must report is `watts`. `today_wh` / `month_wh` are shown on the
dashboard but nothing depends on them - per-print energy is integrated from
wattage over wall-clock time (see app._accumulate_job_energy), so a provider
that can only report watts is a first-class citizen.
"""
from __future__ import annotations

import json
import ssl
import time
from datetime import datetime


class PowerProvider:
    """One source of live wattage.

    report(**fields)  update the shared power state; always pass error=None on
                      a good reading, or error="..." on a bad one
    active()          False while recording is switched off, so a provider can
                      idle instead of polling a plug nobody is watching
    """

    name = "?"

    def __init__(self, cfg, report, active, log=print, state_io=None):
        self.cfg = cfg
        self.report = report
        self.active = active
        self.log = log
        # (load, save) for anything the provider must remember across restarts
        self.load_state, self.save_state = state_io or (lambda: {}, lambda d: None)

    def missing(self) -> list:
        """Setting labels this provider needs and has not been given."""
        return []

    def run(self) -> None:
        raise NotImplementedError


# --------------------------------------------------------------------------
class TapoProvider(PowerProvider):
    """TP-Link Tapo P110 / P110M / P115, polled over the LAN.

    The tapo library is async, so this owns a small event loop in its thread.
    Failures are recorded and retried - never fatal.
    """

    name = "tapo"

    def missing(self):
        return [n for n, k in (("Plug IP", "host"), ("Tapo account", "email"),
                               ("Tapo password", "password"))
                if not self.cfg.get(k)]

    def run(self):
        import asyncio
        try:
            from tapo import ApiClient
        except ImportError:
            self.log("[power] 'tapo' is not installed; Tapo monitoring disabled")
            self.report(error="the tapo package is not installed")
            return

        host = self.cfg.get("host")
        model = self.cfg.get("model", "p110")
        poll = float(self.cfg.get("poll_sec", 20))

        async def loop():
            client = ApiClient(self.cfg.get("email"), self.cfg.get("password"))
            dev = None
            # Log only on state change, never on every poll: a flaky plug must
            # not write a line every 20s, which would wake the NAS disks and
            # defeat the hibernation the whole app is built around.
            last_err, started = None, False
            while True:
                if not self.active():
                    await asyncio.sleep(5)
                    continue
                try:
                    if dev is None:
                        dev = await getattr(client, model)(host)
                    cp = await dev.get_current_power()
                    eu = await dev.get_energy_usage()
                    self.report(watts=cp.current_power, today_wh=eu.today_energy,
                                month_wh=eu.month_energy, ts=time.time(), error=None)
                    if not started:
                        self.log(f"[power] connected to {model} at {host}")
                        started = True
                    elif last_err is not None:
                        self.log(f"[power] {model} reachable again")
                    last_err = None
                except Exception as e:
                    msg = str(e)[:140]
                    self.report(error=msg)
                    dev = None            # force a fresh handshake next time
                    if msg != last_err:   # log the fault once, not every retry
                        self.log(f"[power] error: {e}")
                        last_err = msg
                await asyncio.sleep(poll)

        asyncio.run(loop())


# --------------------------------------------------------------------------
def dig(payload, path: str):
    """'a.b.c' out of nested JSON, because Tasmota nests and Zigbee2MQTT does
    not, and neither of them is going to change for us."""
    cur = payload
    for part in str(path).split("."):
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
                continue
            except (ValueError, IndexError):
                return None
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _num(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None


class MqttProvider(PowerProvider):
    """Anything that publishes power readings to an MQTT broker.

    Written for Zigbee2MQTT, which is how an IKEA INSPELNING is reached - it is
    a Zigbee plug with no address of its own, so nothing can poll it directly.
    The same driver serves Shelly, Tasmota and Home Assistant, since the only
    thing that differs between them is which key in the JSON holds the watts.

    Energy is the awkward part. Tapo reports "today" and "this month" directly;
    a Zigbee plug reports one number that only ever goes up. Today and this
    month are therefore differences from a baseline taken at the last rollover,
    and the baselines are persisted - otherwise every restart would show a
    day's usage as zero and then jump.
    """

    name = "mqtt"

    def missing(self):
        m = []
        if not self.cfg.get("host"):
            m.append("Broker address")
        if not self.cfg.get("topic"):
            m.append("Topic")
        return m

    # ---- energy accounting -------------------------------------------------
    def _windows(self, total_wh: float) -> tuple:
        """(today_wh, month_wh) from a counter that only goes up."""
        st = self._energy
        now = datetime.now()
        day, month = now.toordinal(), now.year * 12 + now.month
        changed = False

        # A counter that went backwards means the plug was reset, re-paired, or
        # replaced. Rebasing is the only honest answer: the energy before the
        # reset is not recoverable, and carrying the old baseline would report
        # a negative day.
        if st.get("last") is not None and total_wh < st["last"] - 1e-6:
            self.log(f"[power] the energy counter went backwards "
                     f"({st['last']:.1f} -> {total_wh:.1f} Wh); starting again from here")
            st["day_base"] = st["month_base"] = total_wh
            st["day"], st["month"] = day, month
            changed = True
        if st.get("day") != day:
            st["day"], st["day_base"] = day, total_wh
            changed = True
        if st.get("month") != month:
            st["month"], st["month_base"] = month, total_wh
            changed = True
        st["last"] = total_wh

        # Written only when a baseline moves - twice a day at most. Saving on
        # every reading would put a database write behind a message that
        # arrives every few seconds, which is exactly the kind of constant
        # writing that stops the NAS disks from ever spinning down.
        if changed:
            self.save_state(dict(st))

        return (max(0.0, total_wh - st["day_base"]),
                max(0.0, total_wh - st["month_base"]))

    # ---- one message -------------------------------------------------------
    def handle(self, topic: str, raw: bytes) -> None:
        """Read one message. A method rather than a closure so it can be driven
        with real payloads in a test, which is the only way this gets exercised
        without the hardware."""
        c = self.cfg
        w_field = c.get("watts_field") or "power"
        e_field = c.get("energy_field") or "energy"
        scale = 1000.0 if str(c.get("energy_unit") or "kWh").lower() == "kwh" else 1.0

        try:
            text = raw.decode("utf-8", "replace").strip() if isinstance(raw, bytes) else str(raw)
        except Exception:
            return
        try:
            payload = json.loads(text)
        except ValueError:
            payload = text
        # A bare reading is valid JSON on its own - `77.5` parses to a float, so
        # the "not JSON" branch never sees it. What matters is whether the
        # message has fields at all, not whether it parsed.
        if not isinstance(payload, (dict, list)):
            payload = {w_field: payload}

        watts = _num(dig(payload, w_field))
        if watts is None:
            if not self._seen:
                keys = ", ".join(sorted(payload)[:8]) if isinstance(payload, dict) else "?"
                self.report(error=f"no '{w_field}' in the message on {topic} "
                                  f"(it has: {keys})")
            return

        fields = {"watts": watts, "ts": time.time(), "error": None}
        total = _num(dig(payload, e_field))
        if total is not None:
            today, month = self._windows(total * scale)
            fields["today_wh"], fields["month_wh"] = today, month
        self.report(**fields)
        if not self._seen:
            self._seen = True
            self.log(f"[power] first reading from {topic}: {watts} W")

    # ---- the loop ----------------------------------------------------------
    def run(self):
        import paho.mqtt.client as mqtt

        host = self.cfg.get("host")
        port = int(self.cfg.get("port") or 1883)
        topic = self.cfg.get("topic")

        self._energy = dict(self.load_state() or {})
        self._seen = False

        def on_connect(client, _u, _f, rc, *a):
            if int(rc) == 0:
                client.subscribe(topic)
                self.log(f"[power] mqtt connected to {host}:{port}, listening on {topic}")
                # Connected is not the same as working: a wrong topic is silent,
                # and silence is the single most likely misconfiguration here.
                if not self._seen:
                    self.report(error=f"connected, but nothing has arrived on {topic} yet")
            else:
                self.report(error=f"broker refused the connection (code {rc})")
                self.log(f"[power] mqtt refused: code {rc}")

        def on_message(client, _u, msg):
            self.handle(msg.topic, msg.payload)

        try:
            client = mqtt.Client(client_id=f"bambu-power-{int(time.time())}")
        except TypeError:                              # paho 2.x
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1,
                                 client_id=f"bambu-power-{int(time.time())}")
        if self.cfg.get("user"):
            client.username_pw_set(self.cfg.get("user"), self.cfg.get("password") or None)
        if self.cfg.get("tls"):
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            client.tls_set_context(ctx)
        client.on_connect = on_connect
        client.on_message = on_message
        client.reconnect_delay_set(min_delay=1, max_delay=60)

        last_err = None
        while True:
            try:
                client.connect(host, port, keepalive=60)
                client.loop_forever(retry_first_connection=False)
            except Exception as e:
                msg = str(e)[:140]
                self.report(error=msg)
                if msg != last_err:       # once, not every retry
                    self.log(f"[power] mqtt error: {e}")
                    last_err = msg
                time.sleep(15)


PROVIDERS = {p.name: p for p in (TapoProvider, MqttProvider)}
