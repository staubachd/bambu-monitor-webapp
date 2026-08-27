"""What the Settings page may change, and what each thing is.

One table, used three ways: it validates what comes in, it tells the page how to
render each field, and it says whether a change takes effect at once or needs a
restart. Anything not listed here cannot be written through the API at all -
including the database connection itself, which could not come from the
database without a chicken and egg - it lives in instance/db.json, see
bootstrap.py.

  kind    how it is edited and validated
  live    False means the value is read once at startup; the page says so
  default what the CODE falls back to when the database has no value - so the
          page shows what the app actually does, not blank, for a setting
          nobody has ever touched
  secret  never sent to the browser; the page shows only whether one is set
"""

# kinds: bool | int | float | text | secret | select | money | matrix | pairs
SCHEMA = [
    # ---- printer -----------------------------------------------------------
    dict(path="ip", label="Printer IP", kind="text", group="Printer", live=False,
         help="LAN address of the printer. Reconnects on restart."),
    dict(path="serial", label="Serial", kind="text", group="Printer", live=False),
    dict(path="access_code", label="LAN access code", kind="secret", group="Printer",
         live=False, help="From the printer's screen: Settings > Network."),
    dict(path="model", default='X2D', label="Model", kind="text", group="Printer", live=False),

    # ---- cost --------------------------------------------------------------
    dict(path="cost.currency", default="€", label="Currency", kind="text", group="Cost", live=True,
         maxlen=4),
    dict(path="cost.price_per_kwh", default=0, label="Electricity price per kWh", kind="money",
         group="Cost", live=True, min=0, max=100),

    # ---- filament ----------------------------------------------------------
    dict(path="filament.default_per_kg", default=0, label="Default price per kg", kind="money",
         group="Filament", live=True, min=0, max=100000,
         help="Used when no more specific rule matches."),
    dict(path="filament.bambu", default={}, label="Bambu, per material", kind="matrix",
         group="Filament", live=True,
         help="Price per kg for genuine spools, by material. 'default' catches the rest."),
    dict(path="filament.other", default={}, label="Third-party, per material", kind="matrix",
         group="Filament", live=True),
    dict(path="filament.per_type", default={}, label="Override by material", kind="matrix",
         group="Filament", live=True, help="Beats the brand tables. Rarely needed."),
    dict(path="filament.per_slot", default={}, label="Override by AMS slot", kind="matrix",
         group="Filament", live=True),
    dict(path="filament.per_filament_id", default={}, label="Override by SKU", kind="matrix",
         group="Filament", live=True),
    dict(path="filament.prices_from_orders", default=True, label="Price from imported invoices",
         kind="bool", group="Filament", live=True,
         help="Genuine spools are priced from your own orders, list price, ahead of "
              "the tables above."),
    dict(path="filament.low_pct", default=15, label="Low-stock threshold %", kind="int",
         group="Filament", live=True, min=0, max=100),
    dict(path="filament.store_region", default="eu", label="Store region", kind="select",
         group="Filament", live=True, options=["eu", "us", "uk", "de", "jp"],
         help="Which Bambu store the Reorder link points at."),
    dict(path="filament.store_host", default="", label="Store address override", kind="text",
         group="Filament", live=True,
         help="Leave empty unless the region list has the wrong address for you."),
    dict(path="filament.color_names", default={}, label="Extra colour names", kind="pairs",
         group="Filament", live=True,
         help="Hex code to name, for colours the built-in list gets wrong."),

    # ---- controls ----------------------------------------------------------
    dict(path="filament.allow_slot_assign", default=False, label="Allow assigning AMS slots",
         kind="bool", group="Controls", live=True,
         help="Rejected by current firmware (HMS 0500_0500_0001_0007)."),
    dict(path="controls.allow_gcode", default=False, label="Allow sending G-code", kind="bool",
         group="Controls", live=True, danger=True,
         help="Sends raw G-code to the printer. Rejected by current firmware, and this "
              "page has no login - leave off unless you need it."),

    # ---- recording ---------------------------------------------------------
    dict(path="storage.sample_interval_sec", default=20, label="Telemetry sample interval (s)",
         kind="int", group="Recording", live=True, min=1, max=3600),
    dict(path="storage.retention_days", default=30, label="Keep telemetry for (days)", kind="int",
         group="Recording", live=True, min=1, max=3650),
    dict(path="storage.auto_tail_min", default=10, label="Keep recording after a print (min)",
         kind="int", group="Recording", live=True, min=0, max=1440),

    # ---- power -------------------------------------------------------------
    dict(path="power.enabled", default=False, label="Smart plug enabled", kind="bool", group="Power",
         live=False),
    dict(path="power.host", label="Plug IP", kind="text", group="Power", live=False),
    dict(path="power.model", default="p110", label="Plug model", kind="select", group="Power",
         live=False, options=["p110", "p110m", "p115"]),
    dict(path="power.email", label="Tapo account", kind="text", group="Power", live=False),
    dict(path="power.password", label="Tapo password", kind="secret", group="Power",
         live=False),
    dict(path="power.poll_sec", default=20, label="Poll every (s)", kind="int", group="Power",
         live=True, min=5, max=3600),

    # ---- cloud -------------------------------------------------------------
    dict(path="cloud.enabled", default=False, label="Bambu Cloud enabled", kind="bool", group="Cloud",
         live=False),
    dict(path="cloud.email", label="Bambu account", kind="text", group="Cloud", live=False),
    dict(path="cloud.password", label="Bambu password", kind="secret", group="Cloud",
         live=False),
    dict(path="cloud.token", label="Cloud token", kind="secret", group="Cloud", live=False,
         help="Filled in automatically after a successful sign-in."),
    dict(path="cloud.poll_min", default=10, label="Sync every (min)", kind="int", group="Cloud",
         live=True, min=1, max=1440),

    # ---- camera ------------------------------------------------------------
    dict(path="camera.enabled", default=False, label="Camera enabled", kind="bool", group="Camera",
         live=False),
    dict(path="camera.src", default="bambu", label="Stream name", kind="text", group="Camera", live=False),
    dict(path="camera.rtsp_port", default=322, label="RTSP port", kind="int", group="Camera",
         live=False, min=1, max=65535),
    dict(path="camera.api_port", default=1984, label="go2rtc port", kind="int", group="Camera",
         live=False, min=1, max=65535),
    dict(path="camera.webrtc_port", default=8555, label="WebRTC port", kind="int", group="Camera",
         live=False, min=1, max=65535),
    dict(path="camera.bin", default="go2rtc/go2rtc_linux_arm64", label="go2rtc binary",
         kind="text", group="Camera", live=False, maxlen=300,
         help="Relative to the app folder. The default is the Synology's ARM64 build."),
]

BY_PATH = {s["path"]: s for s in SCHEMA}
SECRETS = {s["path"] for s in SCHEMA if s["kind"] == "secret"}
GROUPS = list(dict.fromkeys(s["group"] for s in SCHEMA))


class Invalid(ValueError):
    pass


def coerce(path: str, value):
    """Validate one incoming value and return it in the form it is stored in."""
    spec = BY_PATH.get(path)
    if spec is None:
        raise Invalid(f"{path} is not an editable setting")
    kind = spec["kind"]

    if kind == "bool":
        if isinstance(value, bool):
            return value
        if str(value).lower() in ("true", "1", "yes", "on"):
            return True
        if str(value).lower() in ("false", "0", "no", "off"):
            return False
        raise Invalid(f"{spec['label']}: expected true or false")

    if kind in ("int", "float", "money"):
        try:
            n = float(str(value).replace(",", "."))
        except (TypeError, ValueError):
            raise Invalid(f"{spec['label']}: not a number")
        if kind == "int":
            if n != int(n):
                raise Invalid(f"{spec['label']}: whole numbers only")
            n = int(n)
        if "min" in spec and n < spec["min"]:
            raise Invalid(f"{spec['label']}: must be at least {spec['min']}")
        if "max" in spec and n > spec["max"]:
            raise Invalid(f"{spec['label']}: must be at most {spec['max']}")
        return n

    if kind == "select":
        if value not in spec["options"]:
            raise Invalid(f"{spec['label']}: must be one of {', '.join(spec['options'])}")
        return value

    if kind in ("text", "secret"):
        s = "" if value is None else str(value)
        if len(s) > spec.get("maxlen", 200):
            raise Invalid(f"{spec['label']}: too long")
        return s

    if kind == "pairs":
        # text to text, where a matrix is text to number
        if not isinstance(value, dict):
            raise Invalid(f"{spec['label']}: expected a table of name to value")
        out = {}
        for k, v in value.items():
            k = str(k).strip()
            if not k:
                raise Invalid(f"{spec['label']}: a row has no name")
            out[k] = str(v).strip()
        return out

    if kind == "matrix":
        # {name: price} - the shape the pricing rules already read
        if not isinstance(value, dict):
            raise Invalid(f"{spec['label']}: expected a table of name to price")
        out = {}
        for k, v in value.items():
            k = str(k).strip()
            if not k:
                raise Invalid(f"{spec['label']}: a row has no name")
            try:
                out[k] = float(str(v).replace(",", "."))
            except (TypeError, ValueError):
                raise Invalid(f"{spec['label']}: '{k}' is not a number")
            if out[k] < 0:
                raise Invalid(f"{spec['label']}: '{k}' cannot be negative")
        return out

    raise Invalid(f"{path}: unknown kind {kind}")
