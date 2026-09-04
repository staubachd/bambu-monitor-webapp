"""Read the slicer's own metadata off the printer, without downloading the job.

Some facts about a print exist only in the file that was sliced. Layer height is
the obvious one - it is not in any MQTT frame and not in the cloud API, it is in
the sliced file and nowhere else. So are the profile name, the infill, the
supports, and the exact gram figure the slicer computed.

The X2D writes that file to a USB drive and serves it over FTPS. It is a ZIP
(`<subtask name>.gcode.3mf`), and the interesting parts of it are tiny:

    Metadata/project_settings.config      94 KB  (13.6 KB stored)   581 settings
    Metadata/slice_info.config           1.8 KB  (650 b stored)     this plate
    Metadata/model_settings.config        10 KB  (702 b stored)     plate names
    Metadata/plate_15.gcode             11.2 MB  (2.9 MB stored)    first 4 KB

**The bulk of the gcode is never read.** A ZIP is readable backwards - the index
is at the end, and each member can be fetched on its own - and FTP has REST,
which starts a transfer at an offset. Driving zipfile through a seekable
FTP-REST file gets the three config members, plus the gcode's own header block,
in about 150 KB whatever the size of the job. Measured against two real files:
149,429 bytes of 4,365,743 and 156,094 of 3,832,815, under four seconds each. On
a twelve-plate project of 150 MB it would still be about 150 KB.

That matters more than it looks. Downloading whole jobs on a timer is how a NAS
disk never sleeps, and this app has been bitten by exactly that once already.

The gcode header earns its read: `project_settings` gives the profile's nominal
layer height, and multiplying that by the layer count is the model's height only
while every layer is that height. A height range modifier breaks it - one real
print here ran 767 layers of a "0.12mm" profile, 92.04 mm by arithmetic and
65.96 mm in fact. `max_z_height` is measured, so it is the one to believe.

Three things learned the hard way against the real printer, all of them load-
bearing:

  * `REST` is refused in ASCII mode - "550 No support for resume of ASCII
    transfer". `retrbinary` sends `TYPE I` for you; `transfercmd` does not.
  * Aborting a transfer leaves the server still sending. The control connection
    has to be resynchronised afterwards or the next command reads the tail of
    the old response.
  * The printer requires TLS session reuse on data connections, and unwrapping
    one would tear down the control session - hence `_ReusedSocket`.

Nothing here writes to the printer, and nothing here is on a timer: one fetch
per finished print.
"""
from __future__ import annotations

import ftplib
import io
import json
import re
import ssl
import time
import xml.etree.ElementTree as ET
import zipfile

SUFFIX = ".gcode.3mf"

# What this parser knows how to extract. Stamped into every block it produces,
# so that a print read by an older version can be spotted and read again: the
# file is still on the drive, and re-reading it costs one bounded fetch. Without
# this, adding a field only ever reaches prints made after the upgrade, and the
# ones already recorded keep a block that silently lacks it - which is exactly
# what happened when the plate name arrived.
#   1  layer height, profile, infill, supports, weight, estimate
#   2  + plate name, model height, layer count and slot from the gcode header
FORMAT = 2

# Where the members we read live inside the 3MF.
SETTINGS_MEMBER = "Metadata/project_settings.config"
SLICE_MEMBER = "Metadata/slice_info.config"
MODEL_MEMBER = "Metadata/model_settings.config"     # the plate names, 10 KB
# ...plus the first few kilobytes of the plate's gcode, under this key. Not a
# member name, because which member it is depends on the plate.
HEADER_KEY = "#header"
HEADER_BYTES = 4096

# A ceiling on what one fetch may pull, as a last line of defence. The ranged
# read makes this unreachable in normal operation (~150 KB); it exists so that a
# file this code misreads can never turn into an unbounded download.
MAX_PULL = 4 * 1024 * 1024


class SlicerError(Exception):
    """Anything that stopped us reading the metadata. Always non-fatal: the
    print is recorded either way, just without the slicer's half of the story."""


# --------------------------------------------------------------------------
# FTPS
# --------------------------------------------------------------------------

class _ReusedSocket(ssl.SSLSocket):
    """The printer enforces TLS session reuse on data connections. Unwrapping
    one would tear down the control session, so unwrap is a no-op."""

    def unwrap(self):
        pass


class ImplicitFTP_TLS(ftplib.FTP_TLS):
    """ftplib speaks explicit FTPS (AUTH TLS); Bambu uses implicit TLS on 990,
    where the socket must already be wrapped before the greeting."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._sock = None

    @property
    def sock(self):
        return self._sock

    @sock.setter
    def sock(self, value):
        if value is not None and not isinstance(value, ssl.SSLSocket):
            value = self.context.wrap_socket(value, server_hostname=self.host)
        self._sock = value

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(
                conn, server_hostname=self.host, session=self.sock.session)
            conn.__class__ = _ReusedSocket
        return conn, size


def connect(ip: str, access_code: str, timeout: float = 20.0) -> ImplicitFTP_TLS:
    if not ip or not access_code:
        raise SlicerError("no printer address or access code")
    ctx = ssl.create_default_context()
    ctx.check_hostname = False          # the printer's certificate is self-signed
    ctx.verify_mode = ssl.CERT_NONE
    ftp = ImplicitFTP_TLS(context=ctx)
    ftp.encoding = "utf-8"              # names really are UTF-8; do not "fix" them
    try:
        ftp.connect(host=ip, port=990, timeout=timeout)
        ftp.login("bblp", access_code)
        ftp.prot_p()
    except Exception as e:
        raise SlicerError(f"cannot reach the printer's file store: {e}") from None
    return ftp


def sliced_files(ftp) -> list[dict]:
    """Every sliced job on the drive, newest first.

    `LIST -a` rather than `LIST`, because a plain LIST of an empty directory and
    a LIST that failed look identical - which cost an evening once.
    """
    rows: list[str] = []
    try:
        ftp.retrlines("LIST -a /", rows.append)
    except Exception as e:
        raise SlicerError(f"cannot list the printer's drive: {e}") from None
    out = []
    for line in rows:
        parts = line.split(maxsplit=8)
        if len(parts) < 9 or parts[0].startswith("d"):
            continue
        name = parts[8]
        if not name.lower().endswith((".3mf", ".gcode")):
            continue
        try:
            size = int(parts[4])
        except ValueError:
            continue
        out.append({"name": name, "size": size, "mtime": _mtime(ftp, name)})
    out.sort(key=lambda f: f["mtime"] or 0, reverse=True)
    return out


def list_drive(ftp, path: str = "/", max_depth: int = 3,
               max_entries: int = 2000) -> tuple[list[dict], bool]:
    """Everything on the drive, as a flat list of files with their folder.

    Bounded on purpose. A drive is somebody else's filesystem and can hold
    anything - a deep tree, or thousands of timelapse frames - and this runs
    while a print may be going on. Depth and count are capped, and the caller is
    told when the cap was hit rather than being handed a quietly short list.

    `LIST -a`, because a plain LIST of an empty directory and a LIST that failed
    look identical, which cost an evening once.
    """
    out: list[dict] = []
    truncated = [False]

    def walk(where: str, depth: int):
        if len(out) >= max_entries:
            truncated[0] = True
            return
        rows: list[str] = []
        try:
            ftp.retrlines(f"LIST -a {where}", rows.append)
        except Exception:
            return          # an unreadable folder is not a failed listing
        for line in rows:
            if len(out) >= max_entries:
                truncated[0] = True
                return
            parts = line.split(maxsplit=8)
            if len(parts) < 9:
                continue
            perms, name = parts[0], parts[8]
            if name in (".", ".."):
                continue
            full = where.rstrip("/") + "/" + name
            if perms.startswith("d"):
                if depth < max_depth:
                    walk(full, depth + 1)
                else:
                    truncated[0] = True
                continue
            try:
                size = int(parts[4])
            except ValueError:
                continue
            out.append({
                "name": name,
                "dir": where if where != "/" else "/",
                "path": full,
                "size": size,
                # the listing's own date, rather than an MDTM per file: one
                # round trip each would turn a directory of 200 into 200 more
                # commands, on a printer that may be printing
                "when": " ".join(parts[5:8]),
                "sliced": name.lower().endswith((".3mf", ".gcode")),
            })

    walk(path, 0)
    out.sort(key=lambda f: -f["size"])
    return out, truncated[0]


def _mtime(ftp, name: str) -> float | None:
    """MDTM, which this server supports. Only used to break ties."""
    try:
        resp = ftp.sendcmd(f"MDTM {name}")
    except Exception:
        return None
    m = re.search(r"(\d{14})", resp)
    if not m:
        return None
    try:
        return time.mktime(time.strptime(m.group(1), "%Y%m%d%H%M%S"))
    except ValueError:
        return None


class Conn:
    """One FTPS session, which may have to be replaced mid-read.

    A ranged read aborts transfers, and a control connection that cannot be
    resynchronised afterwards has to be thrown away and redialled. Whoever
    opened it therefore has to be able to close whatever it BECAME, not the
    object it started as.

    This class exists because the first version did not, and closed the
    original: every replacement leaked a session to the printer. The printer's
    FTP server has a small pool of them, so a backfill across a hundred prints
    exhausted it and every handshake after that timed out - for every client on
    the network, not just this app. A leaked socket to an embedded server is not
    a tidiness problem.
    """

    def __init__(self, dial=None, ftp=None):
        if dial is None and ftp is None:
            raise ValueError("a Conn needs either a dialler or a connection")
        self._dial = dial
        self.ftp = ftp if ftp is not None else dial()
        self.opened = 1

    def replace(self):
        self.close()
        if self._dial is None:
            raise SlicerError("lost the control connection mid-read")
        self.ftp = self._dial()
        self.opened += 1
        return self.ftp

    def close(self):
        if self.ftp is None:
            return
        try:
            self.ftp.close()
        except Exception:
            pass
        self.ftp = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def _as_conn(what) -> Conn:
    """Accept a Conn or a bare connection. A bare one cannot be replaced, which
    is right for callers that do not do ranged reads."""
    return what if isinstance(what, Conn) else Conn(ftp=what)


class RemoteZip(io.RawIOBase):
    """A seekable view of a file on the printer, backed by FTP REST.

    zipfile seeks to the index and then to the members it is asked for, so
    handing it one of these reads the few kilobytes that matter and nothing
    else. Every byte that crosses the network is counted in `pulled`, because a
    reader that quietly downloads the whole job would be indistinguishable from
    one that does not, right up until somebody slices a big model.
    """

    WINDOW = 64 * 1024

    def __init__(self, conn, name: str, size: int):
        self.conn = _as_conn(conn)
        self.name, self.size = name, size
        self.pos = 0
        self.buf, self.buf_at = b"", -1
        self.pulled = 0
        self.transfers = 0

    def seekable(self):
        return True

    def readable(self):
        return True

    def seek(self, off, whence=0):
        self.pos = (off if whence == 0 else
                    self.pos + off if whence == 1 else self.size + off)
        return self.pos

    def tell(self):
        return self.pos

    def _pull(self, start: int, want: int) -> bytes:
        if self.pulled + want > MAX_PULL:
            raise SlicerError(
                f"reading this file wanted more than {MAX_PULL // 1024} KB - "
                f"refusing, because the point of this is to read a little")
        got = bytearray()
        ftp = self.conn.ftp
        # REST is refused in ASCII mode; retrbinary sets this, transfercmd does not
        ftp.voidcmd("TYPE I")
        data = ftp.transfercmd(f"RETR {self.name}", rest=start)
        self.transfers += 1
        try:
            while len(got) < want:
                chunk = data.recv(min(32768, want - len(got)))
                if not chunk:
                    break
                got.extend(chunk)
        finally:
            data.close()
            # the server is still sending: resynchronise, or replace the session
            # if the control connection cannot be brought back into step.
            # Conn.replace() closes the old one - leaking it here is what
            # exhausted the printer's session pool once already.
            try:
                ftp.voidresp()
            except Exception:
                self.conn.replace()
        self.pulled += len(got)
        return bytes(got)

    def readinto(self, b):
        want = len(b)
        if not want or self.pos >= self.size:
            return 0
        want = min(want, self.size - self.pos)
        cached = (self.buf_at >= 0 and self.buf_at <= self.pos
                  and self.pos + want <= self.buf_at + len(self.buf))
        if not cached:
            start = self.pos
            span = min(max(self.WINDOW, want), self.size - start)
            self.buf = self._pull(start, span)
            self.buf_at = start
        off = self.pos - self.buf_at
        n = min(want, len(self.buf) - off)
        b[:n] = self.buf[off:off + n]
        self.pos += n
        return n


def _gcode_member(names: set, slice_raw: bytes | None) -> str | None:
    """Which plate's gcode belongs to this job.

    The file can carry thumbnails for every plate in the project - eighteen of
    them in one real case - but only the sliced plate's gcode. slice_info names
    the plate, so ask it rather than assuming there is only one.
    """
    plate = None
    if slice_raw:
        m = re.search(rb'key="index" value="(\d+)"', slice_raw)
        if m:
            plate = int(m.group(1))
    if plate is not None and f"Metadata/plate_{plate}.gcode" in names:
        return f"Metadata/plate_{plate}.gcode"
    only = [n for n in names if n.endswith(".gcode")]
    return only[0] if len(only) == 1 else None


def read_members(conn, name: str, size: int) -> tuple[dict, int]:
    """The members of one sliced file worth reading, plus the bytes it cost."""
    rz = RemoteZip(conn, name, size)
    try:
        z = zipfile.ZipFile(rz)
        names = set(z.namelist())
        if SETTINGS_MEMBER not in names and SLICE_MEMBER not in names:
            raise SlicerError(f"{name} has no slicer metadata in it")
        out = {}
        for member in (SETTINGS_MEMBER, SLICE_MEMBER, MODEL_MEMBER):
            if member in names:
                out[member] = z.read(member)
        # The gcode's own header block, which is the resolved truth for this
        # plate rather than the profile's nominal values. Only the first few
        # kilobytes are decompressed: the member itself is 11 MB, and stopping
        # after 4 KB costs one more ranged read of the archive.
        gname = _gcode_member(names, out.get(SLICE_MEMBER))
        if gname:
            try:
                with z.open(gname) as fh:
                    out[HEADER_KEY] = fh.read(HEADER_BYTES)
            except Exception:
                pass       # a bonus; everything else in the file still stands
    except SlicerError:
        raise
    except Exception as e:
        raise SlicerError(f"cannot read {name} as a sliced file: {e}") from None
    return out, rz.pulled


# --------------------------------------------------------------------------
# parsing - pure, and tested against a real capture in samples/
# --------------------------------------------------------------------------

def _f(v):
    """A number, or None. Slicer values are strings, sometimes with a % or a
    unit, sometimes 'nil', sometimes a list of one value per filament."""
    if isinstance(v, list):
        v = v[0] if v else None
    if v is None:
        return None
    s = str(v).strip().rstrip("%").strip()
    if not s or s == "nil":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _s(v):
    if isinstance(v, list):
        v = v[0] if v else None
    s = str(v).strip() if v is not None else ""
    return s or None


def parse_header(raw: bytes | None) -> dict:
    """The gcode's HEADER_BLOCK: what the slicer resolved for THIS plate.

    Worth the extra ranged read for one field. `project_settings` gives the
    profile's *nominal* layer height, and multiplying that by the layer count
    is only the model's height while every layer is that height. The moment a
    height range modifier is used it is not: one real print here ran 767 layers
    of a "0.12mm High Quality" profile - 92.04 mm by multiplication - and was
    actually 65.96 mm, with layer steps from 0.012 to 0.12. `max_z_height` is
    measured rather than assumed, so it is the only honest source.
    """
    if not raw:
        return {}
    text = raw.decode("utf-8", "replace").split("HEADER_BLOCK_END")[0]
    out = {}

    def grab(key, pattern, cast=float):
        m = re.search(pattern, text, re.M)
        if m:
            try:
                out[key] = cast(m.group(1))
            except ValueError:
                pass

    grab("height_mm", r"max_z_height:\s*([\d.]+)")
    grab("layers", r"total layer number:\s*(\d+)", int)
    grab("grams", r"total filament weight \[g\]\s*:\s*([\d.]+)")
    grab("metres", r"total filament length \[mm\]\s*:\s*([\d.]+)",
         lambda s: round(float(s) / 1000.0, 2))
    # "; filament: 5" - the slot that printed it. The trailing colon matters:
    # filament_density and filament_diameter sit two lines above it.
    grab("slot", r"^; filament:\s*(\d+)\s*$", int)
    return out


def parse_plate_name(model_raw: bytes | None, plate: int | None) -> str | None:
    """The name you gave this plate in the slicer.

    A project can hold many plates with names of their own - "Oberteile Herz",
    "blanko für Lichterkette" - which is what somebody printing a series calls
    the job, and nothing else in the app has ever known it.
    """
    if not model_raw or not plate:
        return None
    try:
        root = ET.fromstring(model_raw.decode("utf-8", "replace"))
    except ET.ParseError:
        return None
    for p in root.findall("plate"):
        meta = {m.get("key"): m.get("value") for m in p.findall("metadata")}
        if str(meta.get("plater_id") or "") == str(plate):
            return _s(meta.get("plater_name"))
    return None


def parse(settings_raw: bytes | None, slice_raw: bytes | None,
          model_raw: bytes | None = None, header_raw: bytes | None = None) -> dict:
    """The handful of facts worth keeping, from the two config members.

    Returns a flat dict. Every value is optional: a slicer that stops writing a
    key, or a member that is absent, must degrade to "not known" rather than
    take the whole print down with it.
    """
    out: dict = {}

    if settings_raw:
        try:
            d = json.loads(settings_raw.decode("utf-8", "replace"))
        except ValueError as e:
            raise SlicerError(f"project_settings.config is not JSON: {e}") from None
        if not isinstance(d, dict):
            raise SlicerError("project_settings.config is not a settings object")
        out["layer_h"] = _f(d.get("layer_height"))
        out["first_layer_h"] = _f(d.get("initial_layer_print_height"))
        out["nozzle_mm"] = _f(d.get("nozzle_diameter"))
        out["profile"] = _s(d.get("print_settings_id"))
        out["printer"] = _s(d.get("printer_model"))
        out["infill_pct"] = _f(d.get("sparse_infill_density"))
        out["infill_pattern"] = _s(d.get("sparse_infill_pattern"))
        walls = _f(d.get("wall_loops"))
        out["walls"] = int(walls) if walls else None
        # supports are two keys: a switch and a kind. Reporting the kind while
        # the switch is off would claim supports on a print that has none.
        out["supports"] = (_s(d.get("support_type"))
                           if str(d.get("enable_support") or "0") == "1" else None)

    if slice_raw:
        try:
            root = ET.fromstring(slice_raw.decode("utf-8", "replace"))
        except ET.ParseError as e:
            raise SlicerError(f"slice_info.config is not XML: {e}") from None
        plate = root.find("plate")
        if plate is not None:
            meta = {m.get("key"): m.get("value")
                    for m in plate.findall("metadata") if m.get("key")}
            out["plate"] = int(_f(meta.get("index")) or 0) or None
            out["grams"] = _f(meta.get("weight"))
            secs = _f(meta.get("prediction"))
            out["est_min"] = round(secs / 60.0, 1) if secs else None
            if out.get("nozzle_mm") is None:
                out["nozzle_mm"] = _f((meta.get("nozzle_diameters") or "").split(",")[0])
            fils = []
            for f in plate.findall("filament"):
                fils.append({
                    "slot": int(_f(f.get("id")) or 0) or None,
                    "code": _s(f.get("tray_info_idx")),
                    "type": _s(f.get("type")),
                    "color": _s(f.get("color")),
                    "grams": _f(f.get("used_g")),
                    "metres": _f(f.get("used_m")),
                })
            if fils:
                out["filaments"] = fils
            # the object being printed, as the slicer names it
            obj = plate.find("object")
            if obj is not None:
                out["object"] = _s(obj.get("name"))
        head = root.find("header")
        if head is not None:
            for item in head.findall("header_item"):
                if item.get("key") == "X-BBL-Client-Version":
                    out["slicer"] = _s(item.get("value"))

    # The gcode header last, and only for what nothing else knows. Where both
    # have a figure they have agreed exactly (49.41 g in both), so this fills
    # gaps rather than arguing with slice_info.
    for k, v in parse_header(header_raw).items():
        out.setdefault(k, v)

    out["plate_name"] = parse_plate_name(model_raw, out.get("plate"))

    out = {k: v for k, v in out.items() if v is not None}
    # Only a block that actually says something is stamped. Stamping an empty
    # one would record "read by the current parser" for a print nothing was
    # learned about, and it would never be looked at again.
    if out:
        out["v"] = FORMAT
    return out


# --------------------------------------------------------------------------
# the whole flow
# --------------------------------------------------------------------------

def pick_file(files: list[dict], subtask: str | None) -> dict | None:
    """Which file on the drive belongs to this print.

    The printer names it after the subtask exactly - `<subtask>.gcode.3mf` -
    which is checked against the print we are asking about rather than assumed.
    Falling back to "the newest one" would attribute one print's settings to
    another, and a wrong layer height is worse than a missing one, so the
    fallback only applies when there is exactly one candidate and no name to
    check it against.
    """
    if not files:
        return None
    if subtask:
        want = (subtask + SUFFIX)
        for f in files:
            if f["name"] == want:
                return f
        # some names round-trip through the filesystem with the extension only
        stem = subtask.strip().lower()
        for f in files:
            n = f["name"].lower()
            if n == stem + SUFFIX or n.rsplit(".", 2)[0] == stem:
                return f
        return None
    return files[0] if len(files) == 1 else None


def fetch(ip: str, access_code: str, subtask: str | None = None,
          plate: int | None = None, timeout: float = 20.0,
          conn: "Conn | None" = None) -> dict:
    """Everything the slicer knows about one print. Raises SlicerError.

    Pass `conn` to read several prints over one session. Without it, one is
    opened and closed here.

    `plate`, when the caller knows it from MQTT's gcode_file, is checked against
    the plate the file says it holds. They have always agreed on the real
    printer; if they ever do not, that means the file is not this print's, and
    saying nothing is the only safe answer.
    """
    own = conn is None
    if own:
        conn = Conn(dial=lambda: connect(ip, access_code, timeout))
    try:
        files = sliced_files(conn.ftp)
        chosen = pick_file(files, subtask)
        if chosen is None:
            raise SlicerError(
                f"no sliced file on the drive for {subtask!r}"
                if subtask else "no sliced file on the drive")
        raw, pulled = read_members(conn, chosen["name"], chosen["size"])
        meta = parse(raw.get(SETTINGS_MEMBER), raw.get(SLICE_MEMBER),
                     raw.get(MODEL_MEMBER), raw.get(HEADER_KEY))
        if plate and meta.get("plate") and int(meta["plate"]) != int(plate):
            raise SlicerError(
                f"{chosen['name']} holds plate {meta['plate']}, but this print "
                f"is plate {plate} - refusing to attribute it")
        meta["source_file"] = chosen["name"]
        meta["source_bytes"] = chosen["size"]
        meta["read_bytes"] = pulled
        return meta
    finally:
        # only close what we opened: a caller reading several prints hands the
        # same session in for all of them, which is far kinder to a printer with
        # a small pool of them than one dial per print
        if own:
            conn.close()


if __name__ == "__main__":       # a self-test against the captured sample
    import os
    here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "samples")

    def _read(n):
        with open(os.path.join(here, n), "rb") as fh:
            return fh.read()

    got = parse(_read("project_settings.config"), _read("slice_info.config"),
                _read("model_settings.config"), _read("plate_header.gcode"))
    for k, v in sorted(got.items()):
        print(f"  {k:<16} {v}")
    assert got["layer_h"] == 0.12, got
    assert got["grams"] == 49.41, got
    assert got["plate"] == 15, got
    assert got["height_mm"] == 65.96, got
    assert got["plate_name"] == "Oberteil 'groß` Herz", got
    # the whole reason the header is read: the multiplication is wrong here
    assert abs(got["layers"] * got["layer_h"] - got["height_mm"]) > 25, (
        "this sample no longer demonstrates the variable-layer-height case it "
        "was captured for")
    print("ok")
