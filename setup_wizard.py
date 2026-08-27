"""First-run setup: ask once, store everything, restart.

Runs *instead of* the dashboard when instance/db.json is missing, on the same
port, so there is nothing to find: you open the app and it asks. It is a
separate Flask app on purpose - the monitor's own startup opens the database,
subscribes to MQTT and starts three threads at import, none of which can happen
before these questions are answered.

    no instance/db.json      -> wizard, seeded from printer.config.json if one
                                is still lying around from the old layout
    python app.py --setup    -> wizard again, seeded from what is stored now

Finishing writes the connection to instance/db.json, everything else into the
`settings` table, retires the old JSON file, and re-execs the process.
"""
from __future__ import annotations

import json
import os
import ssl
import threading
import time

from flask import Flask, Response, jsonify, request, send_file

import bootstrap
import config_store
import settings_schema
from storage import Storage

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "setup.html")

# What each step asks for. The wizard does not invent its own field list: every
# entry is a path settings_schema already describes, so a setting added there
# with a group listed here shows up in the wizard without any change here.
STEPS = [
    dict(key="db", title="Database", schema=False),
    dict(key="printer", title="Printer", groups=["Printer"]),
    dict(key="extras", title="Plug, cloud & camera", groups=["Power", "Cloud", "Camera"]),
    dict(key="costs", title="Costs & filament", groups=["Cost", "Filament"]),
    dict(key="recording", title="Recording & safety", groups=["Recording", "Controls"]),
]

# Every schema group must appear in some step, or a setting added to a new group
# would be invisible during setup and stay at its default for ever without
# anyone being told it exists. t_wizard checks this.

# A fresh install starts from Bambu's own list prices rather than from zero,
# because a cost of 0.00 on every print looks like a broken feature rather than
# an unanswered question. Anyone can change them on the last page.
SEED_PRICES = {
    "filament.bambu": {"PLA": 24.99, "PETG": 27.99, "default": 24.99},
    "filament.other": {"PLA": 14.50, "PETG": 16.90, "default": 14.50},
}


def _groups_of(step: dict) -> list:
    return step.get("groups") or []


def _fields() -> list:
    """Every schema field the wizard shows, tagged with its step."""
    out = []
    for step in STEPS:
        for g in _groups_of(step):
            for spec in settings_schema.SCHEMA:
                if spec["group"] == g:
                    out.append({**spec, "step": step["key"]})
    return out


def _seed() -> dict:
    """What to fill the form in with.

    Three sources, in order: what is already in the database (re-running the
    wizard), the old printer.config.json (an upgrade), the schema's defaults
    plus the seed prices (a fresh install).
    """
    values, db, source = {}, None, "fresh"

    boot = bootstrap.load()
    if boot is not None:
        db = boot
        source = "stored"
        try:
            cfg = config_store.ConfigStore()
            cfg.attach(Storage(boot))
            values = {p: cfg.get(p) for p in cfg.overrides()}
        except Exception as e:
            print(f"[setup] could not read the stored settings: {e}")

    legacy = bootstrap.legacy()
    if legacy is not None:
        if db is None:
            db = bootstrap.clean(legacy.get("storage") or {"backend": "sqlite"})
            source = "legacy"
        for spec in settings_schema.SCHEMA:
            v = _dig(legacy, spec["path"])
            if v is not None and spec["path"] not in values:
                values[spec["path"]] = v

    for spec in settings_schema.SCHEMA:
        if spec["path"] not in values:
            if spec["path"] in SEED_PRICES and source == "fresh":
                values[spec["path"]] = SEED_PRICES[spec["path"]]
            elif "default" in spec:
                values[spec["path"]] = spec["default"]

    # a stored secret is never handed back out; the page shows only that one exists
    is_set = {p: bool(values.get(p)) for p in settings_schema.SECRETS}
    for p in settings_schema.SECRETS:
        values[p] = ""
    if db and db.get("backend") in bootstrap.SERVER_BACKENDS:
        is_set["db.password"] = bool(_known_db_password())
        b = db["backend"]
        db = {**db, b: {**bootstrap.server_block(db), "password": ""}}

    return dict(source=source, backends=list(bootstrap.BACKENDS),
                db=db or {"backend": "mariadb",
                          "mariadb": {"host": "127.0.0.1", "port": 3306,
                                      "user": "bambu", "password": "",
                                      "database": "bambu_monitor"}},
                values=values, is_set=is_set,
                legacy_path=bootstrap.LEGACY if legacy is not None else None,
                steps=[dict(key=s["key"], title=s["title"], groups=_groups_of(s))
                       for s in STEPS],
                fields=[{k: v for k, v in f.items() if k != "default"} for f in _fields()])


def _known_db_password() -> str:
    """The database password the wizard already knows, if any.

    Two sources, because an upgrade has not written instance/db.json yet: the
    connection file if it exists, otherwise the old config file the wizard is
    being seeded from. Looking only at the first is what made the page offer to
    keep a password it then failed to send - "using password: NO".
    """
    boot = bootstrap.load()
    if boot and boot.get("backend") in bootstrap.SERVER_BACKENDS:
        if bootstrap.server_block(boot).get("password"):
            return bootstrap.server_block(boot)["password"]
    legacy = bootstrap.legacy() or {}
    st = legacy.get("storage") or {}
    m = st.get(st.get("backend")) or st.get("mariadb") or {}
    return m.get("password") or ""


def _fill_password(db: dict) -> dict:
    """A blank password box means "keep the one you have", never "there isn't
    one" - the page cannot show the current password, so a blank box is not an
    edit. Same rule as every other secret in the app."""
    b = db["backend"]
    if b in bootstrap.SERVER_BACKENDS and not db[b]["password"]:
        db[b]["password"] = _known_db_password()
    return db


def _dig(d, path):
    cur = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def test_printer(ip: str, serial: str, code: str) -> tuple[bool, str]:
    """Open the same connection the monitor will, and say what happened.

    Worth doing here because all three values are easy to mistype and the
    failure otherwise shows up as a dashboard that simply never fills in.
    """
    if not (ip and serial and code):
        return False, "IP, serial and access code are all required"
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        return False, "paho-mqtt is not installed"

    done, result = threading.Event(), {}

    def on_connect(client, _u, _f, rc, *a):
        result["rc"] = int(rc)
        done.set()

    try:
        cl = mqtt.Client(client_id=f"setup-{int(time.time())}")
    except TypeError:                              # paho 2.x
        cl = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1,
                         client_id=f"setup-{int(time.time())}")
    cl.username_pw_set("bblp", code)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    cl.tls_set_context(ctx)
    cl.on_connect = on_connect
    try:
        cl.connect(ip, 8883, keepalive=20)
    except Exception as e:
        return False, f"cannot reach {ip}:8883 - {type(e).__name__}: {e}"
    cl.loop_start()
    ok = done.wait(8)
    cl.loop_stop()
    try:
        cl.disconnect()
    except Exception:
        pass
    if not ok:
        return False, f"{ip} accepted the connection but never answered"
    rc = result.get("rc", -1)
    if rc == 0:
        return True, f"connected to {ip} as {serial}"
    if rc in (4, 5):
        return False, "the printer refused the access code (Settings > Network on its screen)"
    return False, f"the printer refused the connection (code {rc})"


def _apply(payload: dict) -> tuple[dict, int]:
    """Validate everything, then write. Nothing is written until all of it
    passes, so a typo on the last page cannot leave a half-configured app."""
    db = _fill_password(bootstrap.clean(payload.get("db") or {}))

    values, errors = {}, []
    incoming = payload.get("values") or {}
    for path, raw in incoming.items():
        if path not in settings_schema.BY_PATH:
            errors.append(f"{path} is not a setting")
            continue
        if path in settings_schema.SECRETS and raw in ("", None):
            # Blank means "keep what you have". On an upgrade what you have is
            # in printer.config.json, which is renamed away three lines below
            # the write - so it has to be carried into the database now, or the
            # page's promise turns into a lost access code.
            carried = _known(path)
            if carried:
                try:
                    values[path] = settings_schema.coerce(path, carried)
                except settings_schema.Invalid as e:
                    errors.append(str(e))
            continue
        try:
            values[path] = settings_schema.coerce(path, raw)
        except settings_schema.Invalid as e:
            errors.append(str(e))
    for req in ("ip", "serial", "access_code"):
        if not values.get(req) and not _known(req):
            errors.append(f"{settings_schema.BY_PATH[req]['label']} is required")
    if errors:
        return {"ok": False, "where": "values", "errors": errors}, 400

    # The fields are checked first because that costs nothing and touches
    # nothing. Opening the database creates the sqlite file, which would leave
    # a stray database behind every time someone mistypes a price.
    ok, msg = bootstrap.test(db)
    if not ok:
        return {"ok": False, "where": "db", "error": msg}, 400

    bootstrap.save(db)
    store = Storage(bootstrap.load())
    cfg = config_store.ConfigStore()
    cfg.attach(store)
    cfg.set_many(values)
    retired = bootstrap.retire_legacy()
    print(f"[setup] {len(values)} settings stored, connection written to {bootstrap.PATH}")
    if retired:
        print(f"[setup] printer.config.json is no longer read; kept as {os.path.basename(retired)}")
    return {"ok": True, "settings": len(values), "retired": os.path.basename(retired or "")}, 200


def _known(path: str):
    """What the wizard already knows for one setting, or None.

    Two sources, in the order the seed used them: the database if there is one,
    otherwise the old config file being imported. Both matter, because on an
    upgrade there is no database yet - which is exactly when the page is showing
    "leave empty to keep the stored one" for every secret.

    Same rule as _known_db_password, one layer up.
    """
    boot = bootstrap.load()
    if boot is not None:
        try:
            cfg = config_store.ConfigStore()
            cfg.attach(Storage(boot))
            v = cfg.get(path)
            if v not in (None, ""):
                return v
        except Exception:
            pass
    return _dig(bootstrap.legacy() or {}, path)


def serve(port: int) -> None:
    """Run the wizard until it is finished. Returns once it is."""
    app = Flask(__name__)
    finished = threading.Event()

    @app.get("/")
    @app.get("/<path:_rest>")
    def page(_rest=""):
        return send_file(PAGE)

    @app.get("/setup/seed")
    def seed():
        return jsonify(_seed())

    @app.post("/setup/test-db")
    def tdb():
        d = request.get_json(force=True) or {}
        db = _fill_password(bootstrap.clean(d.get("db") or {}))
        ok, msg = bootstrap.test(db)
        return jsonify(ok=ok, message=msg)

    @app.post("/setup/test-printer")
    def tprn():
        d = request.get_json(force=True) or {}
        code = str(d.get("access_code") or "").strip() or (_known("access_code") or "")
        ok, msg = test_printer(str(d.get("ip") or "").strip(),
                               str(d.get("serial") or "").strip(), code)
        return jsonify(ok=ok, message=msg)

    @app.post("/setup/finish")
    def finish():
        body, code = _apply(request.get_json(force=True) or {})
        if body.get("ok"):
            finished.set()
        return jsonify(body), code

    from werkzeug.serving import make_server
    srv = make_server("0.0.0.0", port, app, threaded=True)
    print(f"[setup] not configured yet - open http://localhost:{port} to set it up",
          flush=True)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    finished.wait()
    # let the browser collect the response before the socket goes away
    time.sleep(1.5)
    srv.shutdown()
    t.join(timeout=5)
