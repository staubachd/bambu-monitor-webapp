"""Note categories are free text, and that is a deliberate design.

There is no category table. A category exists because some note says it does,
and stops existing when its last note is edited or deleted. That is cheap and it
never goes out of step - but it means the rules live in how the field is
handled, not in a schema, so they have to be checked here: whitespace is not a
category, clearing one is possible, and nothing is auto-created.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import os as _os
SRC_DIR = (_os.environ.get("BAMBU_SRC")
           or _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import app

c = app.app.test_client()
mine = []


def note(**kw):
    r = c.post("/api/notes", json=kw).get_json()
    assert r["ok"], r
    if r["id"] not in mine:
        mine.append(r["id"])
    return r["id"]


def by_id(nid):
    for n in c.get("/api/notes").get_json()["notes"]:
        if n["id"] == nid:
            return n
    return None


def categories():
    return {n["category"] for n in c.get("/api/notes").get_json()["notes"]
            if n.get("category")}


start = categories()

# --- a category exists because a note says so -------------------------------
a = note(title="Bed levelling", body="every 20 prints", category="Maintenance")
assert by_id(a)["category"] == "Maintenance"
assert "Maintenance" in categories()
print("a category comes into being with the note that names it")

# two notes, one category - not two categories that happen to match
b = note(title="Belt tension", category="Maintenance")
assert len([n for n in c.get("/api/notes").get_json()["notes"]
            if n.get("category") == "Maintenance"]) == 2
print("two notes share one category")

# --- and stops existing when the last note leaves it ------------------------
c.post("/api/notes", json={"id": b, "title": "Belt tension", "category": "Setup"})
assert by_id(b)["category"] == "Setup"
c.post("/api/notes/delete", json={"id": a})
assert "Maintenance" not in categories(), \
    "the category outlived its last note - something is keeping a list"
print("the last note leaving takes the category with it")

# --- clearing one --------------------------------------------------------
c.post("/api/notes", json={"id": b, "title": "Belt tension", "category": ""})
assert by_id(b)["category"] is None, \
    f"an emptied category stored as {by_id(b)['category']!r} rather than nothing"
print("a category can be cleared, and clears to nothing rather than to ''")

# whitespace is not a category, or the filter bar grows an invisible entry
d = note(title="Whitespace", category="   ")
assert by_id(d)["category"] is None, by_id(d)["category"]
assert "   " not in categories()
print("whitespace is not a category")

# --- bounded, like every other free-text field ------------------------------
e = note(title="Long", category="C" * 500)
assert len(by_id(e)["category"]) <= 60, len(by_id(e)["category"])
print(f"a category is trimmed to {len(by_id(e)['category'])} chars")

# --- the page treats them as suggestions, not a fixed list ------------------
page = open(os.path.join(SRC_DIR, "dashboard.html"), encoding="utf-8").read()
assert "<datalist" in page, \
    "the category box offers no suggestions at all"
assert "noteCat" in page
# a <select> would make the suggestions the only possible categories
import re
m = re.search(r'<([a-zA-Z]+)[^>]*\bid="noteCat"', page)
assert m, "there is no category field on the page at all"
assert m.group(1) == "input", (
    f"the category field is a <{m.group(1)}>, which would close the list")
assert 'list="' in page[m.start():m.start() + 300], (
    "the input offers no datalist, so there are no suggestions")
print("the field is an <input> with a datalist: suggestions, not a closed list")

for i in mine:
    c.post("/api/notes/delete", json={"id": i})
assert categories() == start, f"categories left behind: {categories() - start}"
print("ok")
