"""Notes: create, edit, delete - and the rules that stop junk accumulating.

A workshop note is free text somebody typed at 1am with oily hands. What has to
hold: an empty one is not a note, an edit to a note that no longer exists is not
silently a new note, and the long fields are bounded so a paste of an entire
G-code file cannot become a row.
"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# The app source, relative to this file. These tests used to sit inside the
# source folder and could name it directly; they live beside it now, so that
# they survive a temp-directory clean-out.
import os as _os
SRC_DIR = (_os.environ.get("BAMBU_SRC")
           or _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
def _src(name):
    return _os.path.join(SRC_DIR, name)

import app

c = app.app.test_client()


def notes():
    return c.get("/api/notes").get_json()["notes"]


def by_id(nid):
    for n in notes():
        if n["id"] == nid:
            return n
    return None


before = len(notes())

# --- creating ---------------------------------------------------------------
r = c.post("/api/notes", json={"title": "Nozzle swap", "body": "0.4 -> 0.6"}).get_json()
assert r["ok"] and r["id"], r
nid = r["id"]
n = by_id(nid)
assert n["title"] == "Nozzle swap" and n["body"] == "0.4 -> 0.6", n
assert n["created_at"] and n["updated_at"], "a note with no timestamps cannot be sorted"
print("created:", n["title"])

# a note may be title-only or body-only - both are things people actually write
mine = [nid]
for payload in ({"title": "Just a heading"}, {"body": "just a thought"}):
    rr = c.post("/api/notes", json=payload).get_json()
    assert rr["ok"], rr
    mine.append(rr["id"])
print("title-only and body-only notes are both allowed")

# but not empty, and not whitespace pretending not to be empty
for payload in ({}, {"title": "", "body": ""}, {"title": "   ", "body": "\n\t "}):
    rr = c.post("/api/notes", json=payload)
    assert rr.status_code == 400, f"an empty note was accepted: {payload}"
print("an empty note is refused, whitespace included")

# --- editing ----------------------------------------------------------------
r = c.post("/api/notes", json={"id": nid, "title": "Nozzle swap",
                               "body": "0.4 -> 0.6, hardened"}).get_json()
assert r["ok"] and r["id"] == nid, r
n = by_id(nid)
assert n["body"].endswith("hardened"), n
assert len([x for x in notes() if x["id"] == nid]) == 1, "the edit created a second note"
print("edited in place, still one row")

# editing something that is gone must not quietly create it
r = c.post("/api/notes", json={"id": 999999, "title": "ghost"})
assert r.status_code == 404, r.get_json()
assert not any(x["title"] == "ghost" for x in notes()), \
    "editing a missing note created a new one"
print("editing a note that does not exist is a 404, not a new note")

# --- bounds -----------------------------------------------------------------
huge = c.post("/api/notes", json={"title": "T" * 5000,
                                  "body": "B" * (app.NOTE_MAX + 10_000)}).get_json()
assert huge["ok"], huge
n = by_id(huge["id"])
assert len(n["title"]) <= 200, f"title stored at {len(n['title'])} chars"
assert len(n["body"]) <= app.NOTE_MAX, f"body stored at {len(n['body'])} chars"
print(f"a huge paste is trimmed to {len(n['title'])} / {len(n['body'])} chars, not refused")

# --- deleting ---------------------------------------------------------------
assert c.post("/api/notes/delete", json={"id": huge["id"]}).get_json()["ok"]
assert by_id(huge["id"]) is None, "the note survived its own deletion"
# deleting it twice is not an error, it is just already gone
assert c.post("/api/notes/delete", json={"id": huge["id"]}).get_json()["ok"] is False
assert c.post("/api/notes/delete", json={}).status_code == 400
print("deleted; a second delete reports false rather than pretending")

# clean up by id. Matching on text would delete a note somebody actually wrote
# if this were ever pointed at a real database.
for i in mine:
    c.post("/api/notes/delete", json={"id": i})
assert len(notes()) == before, f"{len(notes()) - before} note(s) left behind by this test"
print("ok")
