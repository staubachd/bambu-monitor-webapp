"""The one thing that cannot live in the database: the database connection.

Everything else the app knows is a row in the `settings` table, edited from the
Settings page. This is the chicken-and-egg residue - seven keys the app has to
know before it can read anything at all - and it is the whole reason a file
still exists on disk.

    instance/db.json      written by the setup wizard, never edited by hand

It is deliberately boring: no defaults worth arguing about, no layering, no
merge. If it is missing, the app has not been set up yet and serves the wizard
instead of the dashboard.

An environment variable is NOT consulted. That was on the table and rejected:
two places to look is exactly the confusion this change is meant to end.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DIR = os.path.join(HERE, "instance")
PATH = os.path.join(DIR, "db.json")

# what the app ran on before the wizard existed. Read once, to fill the wizard
# in, and renamed away the moment the wizard finishes.
LEGACY = os.path.join(HERE, "printer.config.json")
LEGACY_DONE = LEGACY + ".imported"


def exists() -> bool:
    return os.path.isfile(PATH)


def load() -> dict | None:
    """The connection, or None when the app has not been set up.

    A relative sqlite path is resolved against the app directory, so the
    working directory the service happens to start in cannot decide which
    database file is opened.
    """
    if not exists():
        return None
    try:
        # utf-8-sig, not utf-8: a file opened in Notepad and saved again comes
        # back with a byte-order mark, and refusing to start over three
        # invisible bytes is not a useful thing to do to somebody
        with open(PATH, encoding="utf-8-sig") as fh:
            cfg = json.load(fh)
    except (OSError, ValueError) as e:
        # Refuse to guess. Starting on an empty sqlite file because the real
        # config failed to parse would look like data loss.
        raise SystemExit(f"[bootstrap] {PATH} is unreadable: {e}\n"
                         f"[bootstrap] fix it, or delete it to re-run the setup wizard")
    return resolve(cfg)


def resolve(cfg: dict) -> dict:
    cfg = dict(cfg)
    if cfg.get("backend", "sqlite") != "sqlite":
        return cfg
    path = cfg.get("sqlite_path") or "telemetry.db"
    if not os.path.isabs(path):
        path = os.path.join(HERE, path)
    cfg["sqlite_path"] = path
    return cfg


def save(cfg: dict) -> None:
    """Write it atomically, and keep it to owner-only.

    It holds a database password. On the NAS the app runs as root and the
    folder is visible over SMB, so 0600 is not ceremony.
    """
    os.makedirs(DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(clean(cfg), fh, indent=2)
            fh.write("\n")
        os.replace(tmp, PATH)
    except BaseException:
        os.path.exists(tmp) and os.unlink(tmp)
        raise
    try:
        os.chmod(PATH, 0o600)
    except OSError:
        pass          # Windows during development; harmless


# which backends this file may name. storage.DIALECTS is the authority on what
# the app can actually talk to; importing it here would drag pymysql's import
# path into the bootstrap, so the list is checked against it by a test instead.
BACKENDS = ("sqlite", "mariadb", "mysql")
SERVER_BACKENDS = ("mariadb", "mysql")


def clean(cfg: dict) -> dict:
    """Only the connection belongs here. Anything else is a setting, and a copy
    of a setting sitting in a file is the drift this change exists to remove."""
    backend = cfg.get("backend")
    if backend not in BACKENDS:
        backend = "sqlite"
    if backend == "sqlite":
        return {"backend": "sqlite",
                "sqlite_path": cfg.get("sqlite_path") or "telemetry.db"}
    # the block is keyed by the backend's own name; an install made before
    # MySQL was offered has it under "mariadb" whatever it now says
    m = cfg.get(backend) or cfg.get("mariadb") or {}
    return {"backend": backend, backend: {
        "host": str(m.get("host") or "127.0.0.1"),
        "port": int(m.get("port") or 3306),
        "user": str(m.get("user") or ""),
        "password": str(m.get("password") or ""),
        "database": str(m.get("database") or ""),
    }}


def server_block(cfg: dict) -> dict:
    """The host/user/password block of a server connection, whatever it is
    keyed by. One helper so no caller has to know."""
    b = cfg.get("backend")
    return cfg.get(b) or cfg.get("mariadb") or {}


def redacted() -> dict | None:
    """The connection as the Settings page may see it: no password."""
    cfg = load()
    if cfg is None:
        return None
    if cfg.get("backend") in SERVER_BACKENDS:
        m = dict(server_block(cfg))
        m["password"] = "•" * 8 if m.get("password") else ""
        # Rebuilt, not copied-and-added-to: keeping the original block alongside
        # a masked copy of it sends the real password to the browser, which is
        # exactly what this function exists to prevent.
        cfg = {"backend": cfg["backend"], "server": m}
    cfg["path"] = PATH
    return cfg


def test(cfg: dict) -> tuple[bool, str]:
    """Actually open the connection and report what happened.

    The wizard refuses to save a connection that has not answered, because the
    alternative is an app that writes a config file and then dies on the next
    start with the real error scrolling past in a task-scheduler log.
    """
    cfg = resolve(clean(cfg))
    if cfg["backend"] == "sqlite":
        try:
            import sqlite3
            d = os.path.dirname(cfg["sqlite_path"])
            if d:
                os.makedirs(d, exist_ok=True)
            sqlite3.connect(cfg["sqlite_path"]).close()
            return True, f"sqlite ok: {cfg['sqlite_path']}"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"
    m = server_block(cfg)
    if not m["user"] or not m["database"]:
        return False, "user and database are required"
    try:
        import pymysql
    except ImportError:
        return False, ("pymysql is not installed - run "
                       "`./venv/bin/python3 -m pip install -r requirements.txt`")
    try:
        conn = pymysql.connect(host=m["host"], port=m["port"], user=m["user"],
                               password=m["password"], database=m["database"],
                               connect_timeout=6, charset="utf8mb4")
    except Exception as e:
        # pymysql wraps everything; the useful part is the driver's own message
        return False, _friendly(e, cfg["backend"])
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT VERSION()")
            ver = cur.fetchone()[0]
        # Writing is the thing that actually has to work, and a read-only grant
        # passes a plain connect. Find out now rather than on the first print.
        with conn.cursor() as cur:
            cur.execute("CREATE TABLE IF NOT EXISTS _bambu_probe (x INT)")
            cur.execute("DROP TABLE _bambu_probe")
        conn.commit()
        # Report the server's own name rather than the one that was picked in
        # the dropdown: choosing MySQL and reaching a MariaDB (or the reverse)
        # is an easy mistake on a NAS that has both, and it works either way -
        # but you should be able to see which one answered.
        kind = "MariaDB" if "mariadb" in str(ver).lower() else "MySQL"
        # naming the host back is the point: it is the one value in this form
        # that looks wrong when it is right
        return True, (f"connected to {kind} {ver} at {m['host']}:{m['port']}"
                      f" as {m['user']}, and it accepts writes")
    except Exception as e:
        return False, (f"connected, but cannot create tables: "
                       f"{_friendly(e, cfg['backend'])}")
    finally:
        conn.close()


def _friendly(e: Exception, backend: str = "mariadb") -> str:
    msg = str(e)
    # MySQL 8 defaults to caching_sha2_password, which PyMySQL can only do with
    # the `cryptography` package. Without it the failure is an import error from
    # deep inside the driver and says nothing about what to install.
    if "cryptography" in msg or "caching_sha2_password" in msg:
        return (f"{msg} - MySQL 8 uses the caching_sha2_password plugin. Install "
                f"the extra: `./venv/bin/python3 -m pip install cryptography`, "
                f"or give the user the older plugin with "
                f"`ALTER USER ... IDENTIFIED WITH mysql_native_password BY '...'`")
    # not every driver error carries a code, and some carry no args at all -
    # the reporter must never be the thing that raises
    args = getattr(e, "args", ()) or (None,)
    code = args[0]
    hints = {
        1045: "wrong user or password",
        2059: "the server wants an authentication plugin PyMySQL cannot load - "
              "install `cryptography` (MySQL 8's default is caching_sha2_password)",
        1049: "no such database - create it first (see deploy/schema_and_user.sql)",
        2003: (f"nothing is listening there - is {'MariaDB' if backend == 'mariadb' else 'MySQL'} "
               f"running, and is it accepting TCP connections?"
               + (" On a Synology: Package Center > MariaDB 10 > 'Enable TCP/IP connection'."
                  if backend == "mariadb" else "")),
        1044: "that user has no rights on that database",
    }
    hint = hints.get(code)
    return f"{msg} - {hint}" if hint else msg


def legacy() -> dict | None:
    """The old printer.config.json, if this is an upgrade rather than a fresh
    install. Used only to fill the wizard in, once."""
    if not os.path.isfile(LEGACY):
        return None
    try:
        with open(LEGACY, encoding="utf-8-sig") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def retire_legacy() -> str | None:
    """Rename the old file once its contents are safely in the database.

    Renamed, not deleted: it holds credentials that were typed in once and may
    exist nowhere else, and this is the one moment they could be lost.
    """
    if not os.path.isfile(LEGACY):
        return None
    dest = LEGACY_DONE
    n = 2
    while os.path.exists(dest):
        dest = f"{LEGACY_DONE}.{n}"
        n += 1
    shutil.move(LEGACY, dest)
    return dest


def restart() -> None:
    """Re-exec the app so it starts normally, now that it is configured.

    The alternative is hot-starting the MQTT, cloud and power threads from
    inside a request handler, against a Storage that did not exist when the
    process began. A clean restart of a process that takes two seconds to boot
    is the honest version of that.
    """
    print("[setup] restarting with the new configuration", flush=True)
    sys.stdout.flush()
    os.execv(sys.executable, [sys.executable, os.path.abspath(sys.argv[0])]
             + [a for a in sys.argv[1:] if a != "--setup"])
