"""docs/INTERNALS.md's "Adding another backend" section is a map, and a map
that points at the wrong place is worse than no map.

It cites file:line for every dialect-specific spot in storage.py, and it claims
a count of them. Both rot the moment somebody inserts a line. This checks that
each cited line still contains what the section says it does, and - more
importantly - that the section's central claim is still true: that only five
places in storage.py are actually about SQL dialect.
"""
# The app source, relative to this file. These tests used to sit inside the
# source folder and could name it directly; they live beside it now, so that
# they survive a temp-directory clean-out.
import os as _os
SRC_DIR = (_os.environ.get("BAMBU_SRC")
           or _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
def _src(name):
    return _os.path.join(SRC_DIR, name)
import io
import re

SRC = SRC_DIR + _os.sep
readme = io.open(SRC + "docs/INTERNALS.md", encoding="utf-8").read()
store = io.open(SRC + "storage.py", encoding="utf-8").read()
lines = store.split("\n")

start = readme.index("### Adding another backend")
sec = readme[start:readme.index("## How the tricky bits work", start)]

# --- every cited line still says what it is cited for ----------------------
# The table's "used at" column points at where each dialect key is READ, so
# that is what these lines must contain - not the value it resolves to.
CITED = {
    248: "DIALECTS = {",
    276: 'dialect["auto"]',
    277: 'dialect["blob"]',
    278: 'dialect["server"]',
    294: "FOUND_ROWS",
    324: 'dialect["inline_index"]',
    420: 'dialect["columns"]',
    909: "REPLACE INTO settings",
    920: "REPLACE INTO hms_ack",
}
# A link's visible label names a line too, and the two rot independently: a
# bulk update of the hrefs left "[storage.py:717](storage.py#L812)" behind,
# which reads as one line and goes to another.
for label, href in re.findall(r"\[(?:storage\.py)?:?(\d+)\]\(\.\./storage\.py#L(\d+)\)", sec):
    assert label == href, (f"a link is labelled line {label} but points at line "
                           f"{href} - one of them is wrong")
cited = {int(n) for n in re.findall(r"storage\.py#L(\d+)", sec)}
assert cited == set(CITED), f"the section cites {sorted(cited)}, this test knows {sorted(CITED)}"
for line, token in CITED.items():
    actual = lines[line - 1]
    assert token in actual, (f"INTERNALS.md sends the reader to storage.py:{line} for "
                             f"{token!r}, but that line is: {actual.strip()[:60]!r}")
print(f"all {len(CITED)} cited lines still contain what INTERNALS.md says they do")

# --- the claim that only five places are about dialect ---------------------
# Everything else is lifecycle or row shape. If a sixth appears, the section is
# no longer telling the truth about how much work a new backend is.
dialect = [n for n, l in enumerate(lines, 1)
           if any(t in l for t in ("self._auto", "self._blob", "inline_idx",
                                   "PRAGMA table_info", "REPLACE INTO"))]
# _auto and _blob are each set twice (one per backend) and used several times;
# what matters is the number of distinct decisions, which is what is documented
# the five keys the table carries, minus `server`, which is lifecycle
decisions = {"auto": 'auto="', "blob": 'blob="',
             "index": "inline_index=", "columns": 'columns="',
             "upsert": 'upsert="'}
found = {k: store.count(v) for k, v in decisions.items()}
for k, n in found.items():
    assert n, f"the {k} dialect decision has disappeared from storage.py"
assert len(decisions) == 5, "this test no longer matches the table in INTERNALS.md"
assert "**5**" in sec or "| **5** |" in sec, "INTERNALS.md no longer claims five"
print("five dialect decisions, exactly as documented:", ", ".join(sorted(found)))

# --- and the claim that nothing uses a MySQL-only upsert -------------------
# The section rests on prints/filaments being hand-rolled UPDATE-then-INSERT.
assert "ON DUPLICATE KEY" not in store.upper(), \
    "storage.py now uses ON DUPLICATE KEY UPDATE; INTERNALS.md says it does not"
assert "ON CONFLICT" not in store.upper(), \
    "storage.py now uses ON CONFLICT; INTERNALS.md's portability claim is stale"
print("no dialect-locked upsert beyond the two REPLACE INTO that are documented")

# --- the six column types ---------------------------------------------------
# Comments are stripped first: a type named in prose is not a column type, and
# this used to fail on the word JSON appearing in a comment about a TEXT column.
code = "\n".join(l.split("#")[0] for l in lines)
types = set(re.findall(r"\b(FLOAT|DOUBLE|TEXT|INTEGER|LONGBLOB|BLOB|BOOLEAN|"
                       r"TINYINT|DATETIME|TIMESTAMP|DECIMAL|JSON)\b", code))
types -= {"BLOB"}          # the sqlite half of the _blob pair
unexpected = types - {"FLOAT", "DOUBLE", "TEXT", "INTEGER", "LONGBLOB"}
assert not unexpected, (f"the schema gained column types INTERNALS.md does not list: "
                        f"{sorted(unexpected)} - a new backend now has more to answer for")
print(f"schema still uses only {len(types)} column types plus VARCHAR(n)")
print("ok")
