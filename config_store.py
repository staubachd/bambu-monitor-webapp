"""Every setting, in the database.

There used to be two layers - printer.config.json underneath, database
overrides on top - and every field on the Settings page had to explain which
one it was showing. Now there is one place a value can come from, and one
place it can be changed. What the file used to supply is either in the
database (put there by the setup wizard) or is a default declared next to the
setting itself in settings_schema.

The database connection is the exception, and lives in bootstrap.py: it has to
be readable before the database can be read.

Sections handed out by `section()` are LIVE - they look the value up each time -
so code that captured `FIL_CFG` at import still sees an edit made a second ago
without a restart, and without 60 call sites having to change.

    cfg = ConfigStore()
    cfg.attach(store)                          # now it has values
    cfg.section("cost").get("price_per_kwh")   # live
    cfg.set("cost.price_per_kwh", 0.32)        # persisted, visible immediately
    cfg.clear("cost.price_per_kwh")            # back to the declared default
"""
from __future__ import annotations

import json
import threading

import settings_schema

PREFIX = "cfg."          # how a setting is keyed in the settings table


def _deep_merge(base: dict, over: dict) -> dict:
    """Later wins, but a dict merges into a dict rather than replacing it - so
    setting filament.low_pct does not wipe filament.bambu."""
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _explode(path: str, value):
    """'a.b.c', 1 -> {'a': {'b': {'c': 1}}}"""
    for part in reversed(path.split(".")):
        value = {part: value}
    return value


def _dig(d: dict, path: str, default=None):
    if not path:
        return d          # the empty path is the whole config, not a key named ""
    cur = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def defaults() -> dict:
    """The config tree the code falls back to, built from the schema.

    A setting with no `default` is genuinely unknown until someone says - the
    printer's IP, an account name - and is simply absent here rather than
    present as an empty string that looks like an answer.
    """
    out: dict = {}
    for spec in settings_schema.SCHEMA:
        if "default" in spec:
            out = _deep_merge(out, _explode(spec["path"], spec["default"]))
    return out


class LiveSection:
    """A read-through view of one config section.

    Quacks like the dict the call sites were written against, but never holds a
    snapshot: the point of the whole exercise is that nothing captures a value
    at import time any more.
    """

    __slots__ = ("_store", "_path")

    def __init__(self, store: "ConfigStore", path: str):
        self._store, self._path = store, path

    def _d(self) -> dict:
        v = self._store.get(self._path, {})
        return v if isinstance(v, dict) else {}

    def get(self, key, default=None):
        return self._d().get(key, default)

    def __getitem__(self, key):
        return self._d()[key]

    def __contains__(self, key):
        return key in self._d()

    def __iter__(self):
        return iter(self._d())

    def keys(self):
        return self._d().keys()

    def items(self):
        return self._d().items()

    def values(self):
        return self._d().values()

    def __len__(self):
        return len(self._d())

    def __bool__(self):
        return bool(self._d())

    def __eq__(self, other):
        return self._d() == other

    def __repr__(self):
        return f"<live {self._path or 'config'}: {self._d()!r}>"


class ConfigStore:
    def __init__(self):
        self._defaults = defaults()
        self._set: dict = {}
        self._merged: dict = dict(self._defaults)
        self._store = None
        self._lock = threading.Lock()

    # ---- reading -----------------------------------------------------------
    def get(self, path: str, default=None):
        return _dig(self._merged, path, default)

    def section(self, path: str = "") -> LiveSection:
        return LiveSection(self, path)

    @property
    def merged(self) -> dict:
        return self._merged

    def default(self, path: str, fallback=None):
        return _dig(self._defaults, path, fallback)

    def overridden(self, path: str) -> bool:
        """True when someone set this, as opposed to it being the default."""
        return path in self._set

    def overrides(self) -> dict:
        return dict(self._set)

    # ---- writing -----------------------------------------------------------
    def attach(self, store) -> None:
        """Point at the database and pull in whatever it already holds."""
        self._store = store
        self.reload()

    @property
    def attached(self) -> bool:
        return self._store is not None

    def reload(self) -> None:
        stored = {}
        if self._store is not None:
            try:
                raw = self._store.settings_with_prefix(PREFIX)
            except Exception as e:                       # a missing table, say
                print(f"[config] could not read settings: {e}")
                raw = {}
            for k, v in raw.items():
                try:
                    stored[k] = json.loads(v)
                except (TypeError, ValueError):
                    stored[k] = v                        # a bare string
        with self._lock:
            self._set = stored
            merged = dict(self._defaults)
            for path, value in stored.items():
                merged = _deep_merge(merged, _explode(path, value))
            # rebound whole, never mutated in place: the MQTT thread reads this
            # while the web thread writes it
            self._merged = merged

    def set(self, path: str, value) -> None:
        if self._store is None:
            raise RuntimeError("no database attached; cannot persist settings")
        self._store.set_setting(PREFIX + path, json.dumps(value))
        self.reload()

    def set_many(self, values: dict) -> None:
        """One reload for a whole wizard page, rather than one per field."""
        if self._store is None:
            raise RuntimeError("no database attached; cannot persist settings")
        for path, value in values.items():
            self._store.set_setting(PREFIX + path, json.dumps(value))
        self.reload()

    def clear(self, path: str) -> None:
        """Drop a stored value so the declared default applies again."""
        if self._store is None:
            raise RuntimeError("no database attached; cannot persist settings")
        self._store.delete_setting(PREFIX + path)
        self.reload()


def open_live():
    """(store, config) for a script that runs outside app.py.

    The one-line replacement for loading the old config file, which is what
    every tool and deploy script used to do. Raises rather than guessing if the
    app has not been set up.
    """
    import bootstrap
    from storage import Storage

    boot = bootstrap.load()
    if boot is None:
        raise SystemExit("[config] not set up yet - run `python app.py` and answer "
                         "the setup wizard first")
    store = Storage(boot)
    cfg = ConfigStore()
    cfg.attach(store)
    return store, cfg
