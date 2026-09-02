"""Read the slicer's own metadata off the printer, without downloading the job.

Some facts about a print exist only in the file that was sliced. Layer height is
the obvious one - it is not in any MQTT frame and not in the cloud API, it is in
the sliced file and nowhere else. So are the profile name, the infill, the
supports, and the exact gram figure the slicer computed.

The X2D writes that file to a USB drive and serves it over FTPS. It is a ZIP
(`<subtask name>.gcode.3mf`), and the interesting parts of it are tiny:

    Metadata/project_settings.config      94 KB  (13.6 KB stored)   581 settings
    Metadata/slice_info.config           1.8 KB  (650 b stored)     this plate
    Metadata/plate_15.gcode             11.2 MB  (2.9 MB stored)    not needed

**The gcode member is never read.** A ZIP is readable backwards - the index is
at the end, and each member can be fetched on its own - and FTP has REST, which
starts a transfer at an offset. Driving zipfile through a seekable FTP-REST file
gets both config members in about 80 KB, whatever the size of the job. Measured
against a 3.8 MB file: 81,146 bytes, five transfers, 1.4 s. On a twelve-plate
project of 150 MB it would still be about 80 KB.

That matters more than it looks. Downloading whole jobs on a timer is how a NAS
disk never sleeps, and this app has been bitten by exactly that once already.

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

# Where the two members live inside the 3MF.
SETTINGS_MEMBER = "Metadata/project_settings.config"
SLICE_MEMBER = "Metadata/slice_info.config"

# A ceiling on what one fetch may pull, as a last line of defence. The ranged
# read makes this unreachable in normal operation (~80 KB); it exists so that a
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


class RemoteZip(io.RawIOBase):
    """A seekable view of a file on the printer, backed by FTP REST.

    zipfile seeks to the index and then to the members it is asked for, so
    handing it one of these reads the few kilobytes that matter and nothing
    else. Every byte that crosses the network is counted in `pulled`, because a
    reader that quietly downloads the whole job would be indistinguishable from
    one that does not, right up until somebody slices a big model.
    """

    WINDOW = 64 * 1024

    def __init__(self, ftp, name: str, size: int, reconnect=None):
        self.ftp, self.name, self.size = ftp, name, size
        self._reconnect = reconnect
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
        # REST is refused in ASCII mode; retrbinary sets this, transfercmd does not
        self.ftp.voidcmd("TYPE I")
        conn = self.ftp.transfercmd(f"RETR {self.name}", rest=start)
        self.transfers += 1
        try:
            while len(got) < want:
                chunk = conn.recv(min(32768, want - len(got)))
                if not chunk:
                    break
                got.extend(chunk)
        finally:
            conn.close()
            # the server is still sending: resynchronise, or reconnect if the
            # control connection cannot be brought back into step
            try:
                self.ftp.voidresp()
            except Exception:
                if self._reconnect is None:
                    raise SlicerError("lost the control connection mid-read") from None
                try:
                    self.ftp.close()
                except Exception:
                    pass
                self.ftp = self._reconnect()
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


def read_members(ftp, name: str, size: int, reconnect=None) -> tuple[dict, int]:
    """The two config members of one sliced file, plus the bytes it cost."""
    rz = RemoteZip(ftp, name, size, reconnect=reconnect)
    try:
        z = zipfile.ZipFile(rz)
        names = set(z.namelist())
        if SETTINGS_MEMBER not in names and SLICE_MEMBER not in names:
            raise SlicerError(f"{name} has no slicer metadata in it")
        out = {}
        for member in (SETTINGS_MEMBER, SLICE_MEMBER):
            if member in names:
                out[member] = z.read(member)
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


def parse(settings_raw: bytes | None, slice_raw: bytes | None) -> dict:
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

    return {k: v for k, v in out.items() if v is not None}


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
          plate: int | None = None, timeout: float = 20.0) -> dict:
    """Everything the slicer knows about one print. Raises SlicerError.

    `plate`, when the caller knows it from MQTT's gcode_file, is checked against
    the plate the file says it holds. They have always agreed on the real
    printer; if they ever do not, that means the file is not this print's, and
    saying nothing is the only safe answer.
    """
    def _dial():
        return connect(ip, access_code, timeout)

    ftp = _dial()
    try:
        files = sliced_files(ftp)
        chosen = pick_file(files, subtask)
        if chosen is None:
            raise SlicerError(
                f"no sliced file on the drive for {subtask!r}"
                if subtask else "no sliced file on the drive")
        raw, pulled = read_members(ftp, chosen["name"], chosen["size"],
                                   reconnect=_dial)
        meta = parse(raw.get(SETTINGS_MEMBER), raw.get(SLICE_MEMBER))
        if plate and meta.get("plate") and int(meta["plate"]) != int(plate):
            raise SlicerError(
                f"{chosen['name']} holds plate {meta['plate']}, but this print "
                f"is plate {plate} - refusing to attribute it")
        meta["source_file"] = chosen["name"]
        meta["source_bytes"] = chosen["size"]
        meta["read_bytes"] = pulled
        return meta
    finally:
        try:
            ftp.close()
        except Exception:
            pass


if __name__ == "__main__":       # a self-test against the captured sample
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "samples", "project_settings.config"), "rb") as fh:
        ps = fh.read()
    with open(os.path.join(here, "samples", "slice_info.config"), "rb") as fh:
        si = fh.read()
    got = parse(ps, si)
    for k, v in sorted(got.items()):
        print(f"  {k:<16} {v}")
    assert got["layer_h"] == 0.12, got
    assert got["grams"] == 49.41, got
    assert got["plate"] == 15, got
    print("ok")
