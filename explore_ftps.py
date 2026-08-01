#!/usr/bin/env python3
"""
Explore the printer's FTPS storage - discovery tool for filament-usage parsing.

The X2D exposes implicit FTPS on port 990 (user 'bblp', password = LAN access
code, self-signed certificate). The sliced 3MF that a print came from lives
somewhere on that storage; inside it, Metadata/slice_info.config and
Metadata/plate_N.gcode carry the exact filament usage in grams.

    python explore_ftps.py            # uses printer.config.json
"""
import ftplib
import json
import os
import ssl
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
cfg = json.load(open(os.path.join(HERE, "printer.config.json"), encoding="utf-8"))
HOST, CODE = cfg["ip"], cfg["access_code"]


class _ReusedSocket(ssl.SSLSocket):
    """Data sockets share the control connection's TLS session; unwrapping one
    would tear that session down, so make unwrap a no-op."""

    def unwrap(self):
        pass


class ImplicitFTP_TLS(ftplib.FTP_TLS):
    """ftplib speaks explicit FTPS (AUTH TLS); Bambu uses implicit TLS on 990,
    where the socket must already be wrapped before the greeting. The printer
    also enforces TLS session reuse on data connections ("522 session reuse
    required"), so ntransfercmd re-uses the control session explicitly."""

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


ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

ftp = ImplicitFTP_TLS(context=ctx)
ftp.encoding = "utf-8"
print(f"[..] connecting to {HOST}:990 ...")
ftp.connect(host=HOST, port=990, timeout=20)
ftp.login("bblp", CODE)
ftp.prot_p()
print("[ok] logged in\n")

INTERESTING = (".3mf", ".gcode", ".gco", ".json", ".config")


def walk(path="/", depth=0, max_depth=2):
    pad = "   " * depth
    try:
        entries = []
        ftp.retrlines(f"LIST {path}", entries.append)
    except Exception as e:
        print(f"{pad}[!] {path}: {e}")
        return
    for line in entries:
        parts = line.split(maxsplit=8)
        if len(parts) < 9:
            continue
        perms, size, name = parts[0], parts[4], parts[8]
        if name in (".", ".."):
            continue
        full = (path.rstrip("/") + "/" + name)
        if perms.startswith("d"):
            print(f"{pad}[dir ] {name}/")
            if depth < max_depth:
                walk(full, depth + 1, max_depth)
        else:
            mark = "  <<<" if name.lower().endswith(INTERESTING) else ""
            print(f"{pad}[file] {name:<52} {int(size):>10,} b{mark}")


if "--raw" in sys.argv:
    for p in ["/", "/cache", "/model", "/timelapse", "/image", "/logger",
              "/ipcam", "/data", "/sdcard", "/Metadata"]:
        print(f"--- LIST {p} ---")
        lines = []
        try:
            ftp.retrlines(f"LIST {p}", lines.append)
            for ln in lines:
                print("    ", repr(ln))
            if not lines:
                print("     (empty)")
        except Exception as e:
            print("     err:", e)
        try:
            n = ftp.nlst(p)
            print(f"     NLST -> {n[:20]}")
        except Exception as e:
            print("     NLST err:", e)
    print(f"\n--- PWD --- {ftp.pwd()}")
else:
    walk("/")
    print("\n[done] '<<<' marks files that may contain slice metadata")
ftp.quit()
