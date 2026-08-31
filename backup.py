"""Export everything worth keeping to one JSON file, and put it back.

The print history is the irreplaceable part: the printer does not remember it,
the cloud keeps only a rolling window, and every cost figure on the page is
derived from it. Filament identities are next - the names, colours and prices
somebody typed in by hand, which nothing else in the world knows.

Three decisions worth stating, because each of them can bite:

**Telemetry is not included.** It is a temperature sample every 20 seconds -
thousands of rows per print, the bulk of the database, and a chart nobody
consults twice. Including it would turn a 200 KB backup into a 100 MB one and
make it too slow to take often. What is lost by leaving it out is the shape of
the temperature curve on old prints; what is kept is everything the app
actually computes anything from.

**Secrets are left out by default.** The settings table holds the printer's
access code and the Bambu and Tapo passwords. A backup is a file that gets
emailed to yourself, dropped in a cloud folder, copied to a stick. Those
credentials are trivially retypeable and the rest of the file is not, so the
default is to omit them and say so in the file itself.

**Restore never destroys by default.** `merge` inserts what is missing and
leaves everything that is already there alone, so a restore onto a database
that has since been used cannot lose the newer work. `replace` is the "the
database is gone and I want exactly this file back" option, and it says how
many rows it is about to delete before it does.
"""
from __future__ import annotations

import base64
import json
import time
from datetime import datetime

import settings_schema

FORMAT = 1

# table -> the columns that identify a row, for deciding what a merge may skip.
# Order matters on restore: a note_image points at a note, so notes go first.
TABLES = [
    ("prints", ("job_id",)),
    ("filaments", ("fkey",)),
    ("purchases", ("id",)),
    ("notes", ("id",)),
    ("note_images", ("id",)),
    ("settings", ("skey",)),
    ("hms_ack", ("code", "ts")),
]

# What is deliberately NOT in a backup, and why - carried in the file so the
# person restoring it three years from now does not have to guess.
EXCLUDED = {
    "telemetry": "a sample every 20s; the bulk of the database and the only "
                 "table nothing is computed from",
    "connection": "instance/db.json is where the app is told which database to "
                  "open; restoring it into a different database would be wrong",
}

# settings rows that are a credential rather than a preference
SECRET_KEYS = {"cfg." + p for p in settings_schema.SECRETS}


def _is_secret(skey: str) -> bool:
    return skey in SECRET_KEYS


def export(store, include_secrets: bool = False, include_images: bool = True) -> dict:
    """Everything worth keeping, as a plain dict ready for json.dump."""
    out, counts, omitted = {}, {}, {"secrets": 0, "images": 0}
    for table, _keys in TABLES:
        rows = store.dump_table(table)
        if table == "settings" and not include_secrets:
            before = len(rows)
            rows = [r for r in rows if not _is_secret(r.get("skey", ""))]
            omitted["secrets"] = before - len(rows)
        if table == "note_images":
            if include_images:
                for r in rows:
                    blob = r.get("data")
                    # bytes cannot go in JSON; base64 is the price of one file
                    r["data"] = (base64.b64encode(blob).decode("ascii")
                                 if isinstance(blob, (bytes, bytearray)) else None)
                    r["_encoding"] = "base64"
            else:
                omitted["images"] = len(rows)
                rows = []
        out[table] = rows
        counts[table] = len(rows)

    return {
        "format": FORMAT,
        "app": "bambu-monitor",
        "created_at": time.time(),
        "created_at_human": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "counts": counts,
        "omitted": omitted,
        "excluded": EXCLUDED,
        "note": ("Restore with the Backup tab in Settings, or "
                 "`python tools/backup.py restore <file>`. "
                 + ("Credentials are NOT in this file - the printer access code "
                    "and any account passwords have to be typed in again."
                    if not include_secrets else
                    "WARNING: this file CONTAINS the printer access code and "
                    "account passwords in clear text.")),
        "tables": out,
    }


def check(data) -> str | None:
    """Why this cannot be restored, or None if it can."""
    if not isinstance(data, dict):
        return "that is not a backup file"
    if data.get("app") != "bambu-monitor":
        return f"this is not a Bambu Monitor backup (app={data.get('app')!r})"
    fmt = data.get("format")
    if not isinstance(fmt, int):
        return "the file has no format version"
    if fmt > FORMAT:
        return (f"this backup is format {fmt} and this app understands {FORMAT} - "
                f"it was written by a newer version")
    if not isinstance(data.get("tables"), dict):
        return "the file has no tables in it"
    known = {t for t, _ in TABLES}
    unknown = set(data["tables"]) - known
    if unknown:
        return f"the file has tables this app does not know: {', '.join(sorted(unknown))}"
    return None


def restore(store, data: dict, mode: str = "merge", dry_run: bool = False) -> dict:
    """Put a backup back.

    merge    insert rows whose key is not already present; touch nothing else
    replace  empty each table first, then insert everything from the file

    Returns a per-table report either way. With dry_run the report is what
    WOULD happen and nothing is written - which is the only honest way to offer
    a destructive option.
    """
    why = check(data)
    if why:
        raise ValueError(why)
    if mode not in ("merge", "replace"):
        raise ValueError(f"unknown restore mode {mode!r}")

    tables = data["tables"]
    report = {"mode": mode, "dry_run": bool(dry_run), "tables": {}}
    for table, keys in TABLES:
        rows = tables.get(table) or []
        existing = store.count_rows(table)
        if table == "note_images":
            rows = [_decode_image(r) for r in rows]
            rows = [r for r in rows if r is not None]

        if mode == "replace":
            entry = {"in_file": len(rows), "deleted": existing, "inserted": len(rows),
                     "skipped": 0}
            if not dry_run:
                store.clear_table(table)
                entry["inserted"] = store.insert_rows(table, rows)
        else:
            have = {_key(r, keys) for r in store.dump_table(table)}
            fresh = [r for r in rows if _key(r, keys) not in have]
            entry = {"in_file": len(rows), "deleted": 0, "inserted": len(fresh),
                     "skipped": len(rows) - len(fresh)}
            if not dry_run:
                entry["inserted"] = store.insert_rows(table, fresh)
        report["tables"][table] = entry

    report["inserted"] = sum(t["inserted"] for t in report["tables"].values())
    report["skipped"] = sum(t["skipped"] for t in report["tables"].values())
    report["deleted"] = sum(t["deleted"] for t in report["tables"].values())
    return report


def _key(row: dict, keys: tuple) -> tuple:
    return tuple(row.get(k) for k in keys)


def _decode_image(row: dict):
    """base64 back to bytes. A row whose picture will not decode is dropped
    rather than restored as a broken image nothing can render."""
    row = dict(row)
    row.pop("_encoding", None)
    blob = row.get("data")
    if blob is None:
        return None
    if isinstance(blob, (bytes, bytearray)):
        return row
    try:
        row["data"] = base64.b64decode(blob, validate=True)
    except Exception:
        return None
    return row


def summarise(data: dict) -> str:
    """One line per table, for a person deciding whether to restore this file."""
    counts = data.get("counts") or {}
    when = data.get("created_at_human") or "?"
    parts = ", ".join(f"{v} {k}" for k, v in counts.items() if v)
    om = data.get("omitted") or {}
    extra = []
    if om.get("secrets"):
        extra.append(f"{om['secrets']} credential(s) omitted")
    if om.get("images"):
        extra.append(f"{om['images']} image(s) omitted")
    return f"backup from {when}: {parts or 'nothing'}" + (
        f" ({'; '.join(extra)})" if extra else "")
