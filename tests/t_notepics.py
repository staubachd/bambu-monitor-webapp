"""Pictures attached to notes.

A photo of a failed print is the most useful thing in a workshop note, and also
the easiest way to fill a NAS disk. The bytes go in the database, so what has to
hold is: only real image types, a hard ceiling on size, the bytes come back
exactly as they went in, and deleting a picture actually reclaims it.
"""
import sys, os, io as _io
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import os as _os
SRC_DIR = (_os.environ.get("BAMBU_SRC")
           or _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import app

c = app.app.test_client()

# the smallest real PNG there is: 1x1, transparent
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000002000100" "05fe02fe" "0000000049454e44ae426082")

note = c.post("/api/notes", json={"title": "Warping", "body": "corner lifted"}).get_json()
nid = note["id"]


def add(data=PNG, mime="image/png", name="shot.png", note_id=None):
    return c.post("/api/notes/image", data={
        "note_id": str(nid if note_id is None else note_id),
        "file": (_io.BytesIO(data), name, mime)},
        content_type="multipart/form-data")


def images():
    for n in c.get("/api/notes").get_json()["notes"]:
        if n["id"] == nid:
            return n.get("images") or []
    return []


# --- attaching --------------------------------------------------------------
r = add().get_json()
assert r["ok"] and r["id"] and r["size"] == len(PNG), r
iid = r["id"]
assert len(images()) == 1, images()
print(f"attached {r['size']} bytes; the note lists {len(images())} image")

# --- and getting the same bytes back ---------------------------------------
got = c.get(f"/api/notes/image/{iid}")
assert got.status_code == 200, got.status_code
assert got.data == PNG, (f"{len(got.data)} bytes came back, {len(PNG)} went in - "
                         f"a blob column that mangles bytes ruins every photo")
assert got.mimetype == "image/png", got.mimetype
assert "immutable" in got.headers.get("Cache-Control", ""), \
    "the bytes for an id never change; the browser should be told so"
print("the exact bytes come back, with the right type and a cacheable header")

# --- what may be attached ---------------------------------------------------
for mime in ("image/jpeg", "image/webp", "image/gif"):
    rr = add(mime=mime, name="x")
    assert rr.get_json()["ok"], f"{mime} was refused: {rr.get_json()}"
    c.post("/api/notes/image/delete", json={"id": rr.get_json()["id"]})
print("jpeg, webp and gif are accepted alongside png")

for mime, why in [("application/pdf", "a document"), ("text/html", "markup"),
                  ("application/octet-stream", "anything at all"), ("", "no type")]:
    rr = add(mime=mime, name="x")
    assert rr.status_code == 400, f"{why} ({mime!r}) was stored as a picture"
print("a pdf, html or an unnamed type is refused")

assert add(data=b"").status_code == 400, "an empty upload became an image"
big = add(data=b"\x89PNG" + b"\0" * (app.IMAGE_MAX + 1))
assert big.status_code == 413, f"a {app.IMAGE_MAX + 5} byte upload returned {big.status_code}"
print(f"empty is refused; over {app.IMAGE_MAX // (1024*1024)} MB is 413, not a silent truncation")

# an upload with no note, or for a note that is not there
assert c.post("/api/notes/image", data={"file": (_io.BytesIO(PNG), "x.png", "image/png")},
              content_type="multipart/form-data").status_code == 400
assert c.post("/api/notes/image", data={"note_id": str(nid)},
              content_type="multipart/form-data").status_code == 400
print("an upload with no note, or no file, is refused")

# --- deleting ---------------------------------------------------------------
assert c.post("/api/notes/image/delete", json={"id": iid}).get_json()["ok"]
assert images() == [], f"the note still lists {len(images())} image(s)"
assert c.get(f"/api/notes/image/{iid}").status_code == 404, \
    "the bytes are still served after the image was deleted"
assert c.post("/api/notes/image/delete", json={}).status_code == 400
print("deleted: gone from the note, and no longer served")

# --- and a deleted note must not leave its pictures behind ------------------
iid2 = add().get_json()["id"]
assert c.get(f"/api/notes/image/{iid2}").status_code == 200
c.post("/api/notes/delete", json={"id": nid})
orphan = c.get(f"/api/notes/image/{iid2}")
assert orphan.status_code == 404, (
    "a picture outlived the note it belonged to - nothing will ever reference "
    "it again and nothing will ever delete it")
print("deleting a note takes its pictures with it")
print("ok")
