"""MySQL support, verified as far as it can be without a MySQL server.

There is no server on this machine, so the claim being tested is the one the
implementation actually rests on: **MySQL runs exactly the SQL MariaDB runs.**
Not "similar SQL" - the same bytes, from the same code path, with the same
connect arguments. MariaDB is known to work, so if the two are identical there
is nothing left for MySQL to get wrong except the driver handshake, which is
PyMySQL's job and is covered by the error-message tests below.

What is checked here:
  - every statement Storage emits is identical between the two backends
  - the connect kwargs are identical
  - no identifier in the schema is a MySQL 8 reserved word
  - the connection file, the wizard and the Settings page all handle a backend
    they were written before
"""
# The app source, relative to this file. These tests used to sit inside the
# source folder and could name it directly; they live beside it now, so that
# they survive a temp-directory clean-out.
import os as _os
SRC_DIR = (_os.environ.get("BAMBU_SRC")
           or _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
def _src(name):
    return _os.path.join(SRC_DIR, name)
import sys, os, io, re, json, tempfile, shutil
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bootstrap, storage

SRC = SRC_DIR + _os.sep


# --- a fake server, so the real SQL can be captured without one -------------
class FakeCursor:
    def __init__(self, log): self.log = log; self.rowcount = 0; self.description = ()
    def execute(self, sql, params=None): self.log.append(" ".join(sql.split()))
    def fetchall(self): return []
    def fetchone(self): return None
    def close(self): pass
    def __enter__(self): return self
    def __exit__(self, *a): pass


class FakeConn:
    def __init__(self, log): self.log = log
    def cursor(self): return FakeCursor(self.log)
    def commit(self): pass
    def close(self): pass


def capture(backend):
    """Every statement Storage issues while building its schema, and the
    kwargs it would have connected with."""
    log, kw = [], {}
    import pymysql
    real = pymysql.connect
    pymysql.connect = lambda **k: (kw.update(k), FakeConn(log))[1]
    try:
        storage.Storage({"backend": backend, backend: {
            "host": "h", "port": 3306, "user": "u",
            "password": "p", "database": "d"}})
    finally:
        pymysql.connect = real
    return log, kw


maria_sql, maria_kw = capture("mariadb")
mysql_sql, mysql_kw = capture("mysql")

assert maria_sql, "no statements captured - the fake server did not take"
assert maria_sql == mysql_sql, (
    "MySQL and MariaDB do not run the same SQL:\n  " +
    "\n  ".join(f"{a[:70]}\n  {b[:70]}" for a, b in zip(maria_sql, mysql_sql) if a != b))
print(f"{len(mysql_sql)} statements, byte-identical between MariaDB and MySQL")

assert maria_kw == mysql_kw, f"different connect kwargs:\n  {maria_kw}\n  {mysql_kw}"
assert maria_kw["charset"] == "utf8mb4" and maria_kw["autocommit"] is True
assert maria_kw["client_flag"], "FOUND_ROWS was dropped; every 'did the row exist' breaks"
print("connect kwargs identical, FOUND_ROWS still set")

# and it is genuinely the server path, not a quiet fall-through to sqlite
assert "AUTO_INCREMENT" in " ".join(mysql_sql), "MySQL got sqlite's AUTOINCREMENT"
assert "AUTOINCREMENT" not in " ".join(mysql_sql).replace("AUTO_INCREMENT", "")
assert "LONGBLOB" in " ".join(mysql_sql), "MySQL got sqlite's BLOB type"
assert "INDEX idx_telemetry_ts" in " ".join(mysql_sql), "the inline index was skipped"
assert "PRAGMA" not in " ".join(mysql_sql), "MySQL was asked a sqlite PRAGMA"
print("MySQL takes the server path: AUTO_INCREMENT, LONGBLOB, inline INDEX, no PRAGMA")

# --- the dialect table is the whole of the difference -----------------------
assert set(storage.DIALECTS) == {"sqlite", "mariadb", "mysql"}
assert storage.DIALECTS["mysql"] == storage.DIALECTS["mariadb"], \
    "the two entries have drifted apart; one of them is now untested"
for name, d in storage.DIALECTS.items():
    assert set(d) == {"server", "ph", "auto", "blob", "inline_index",
                      "columns", "upsert"}, f"{name} has a different shape: {sorted(d)}"
print("one dialect entry per backend, all the same shape")

try:
    storage.Storage({"backend": "postgres"})
    raise AssertionError("an unimplemented backend was accepted")
except ValueError as e:
    assert "postgres" in str(e) and "mysql" in str(e), e
print("an unknown backend is refused by name, and says what it could have been")

# --- no identifier needs quoting on MySQL 8 --------------------------------
RESERVED = set("""ACCESSIBLE ADD ALL ALTER ANALYZE AND AS ASC BEFORE BETWEEN BIGINT BINARY
BLOB BOTH BY CALL CASCADE CASE CHANGE CHAR CHARACTER CHECK COLLATE COLUMN CONDITION
CONSTRAINT CONTINUE CONVERT CREATE CROSS CUBE CUME_DIST CURSOR DATABASE DATABASES DEC
DECIMAL DECLARE DEFAULT DELAYED DELETE DENSE_RANK DESC DESCRIBE DETERMINISTIC DISTINCT
DIV DOUBLE DROP DUAL EACH ELSE ELSEIF EMPTY ENCLOSED ESCAPED EXCEPT EXISTS EXIT EXPLAIN
FALSE FETCH FIRST_VALUE FLOAT FOR FORCE FOREIGN FROM FULLTEXT FUNCTION GENERATED GET
GRANT GROUP GROUPING GROUPS HAVING IF IGNORE IN INDEX INFILE INNER INOUT INSENSITIVE
INSERT INT INTEGER INTERVAL INTO IS ITERATE JOIN JSON_TABLE KEY KEYS KILL LAG LAST_VALUE
LATERAL LEAD LEADING LEAVE LEFT LIKE LIMIT LINEAR LINES LOAD LOCK LONG LONGBLOB LONGTEXT
LOOP MATCH MAXVALUE MEDIUMBLOB MEDIUMINT MEDIUMTEXT MOD MODIFIES NATURAL NOT NTH_VALUE
NTILE NULL NUMERIC OF ON OPTIMIZE OPTION OPTIONALLY OR ORDER OUT OUTER OUTFILE OVER
PARTITION PERCENT_RANK PRECISION PRIMARY PROCEDURE PURGE RANGE RANK READ READS REAL
RECURSIVE REFERENCES REGEXP RELEASE RENAME REPEAT REPLACE REQUIRE RESIGNAL RESTRICT
RETURN REVOKE RIGHT RLIKE ROW ROWS ROW_NUMBER SCHEMA SCHEMAS SELECT SENSITIVE SEPARATOR
SET SHOW SIGNAL SMALLINT SPATIAL SPECIFIC SQL SSL STARTING STORED SYSTEM TABLE TERMINATED
THEN TINYBLOB TINYINT TINYTEXT TO TRAILING TRIGGER TRUE UNDO UNION UNIQUE UNLOCK UNSIGNED
UPDATE USAGE USE USING VALUES VARBINARY VARCHAR VARYING VIRTUAL WHEN WHERE WHILE WINDOW
WITH WRITE XOR ZEROFILL""".split())

src = io.open(SRC + "storage.py", encoding="utf-8").read()
ident = set(storage.TELEMETRY_COLS) | set(storage.PRINT_COLS) | set(storage.FILAMENT_COLS)
ident |= set(getattr(storage, "PURCHASE_COLS", []))
for tbl, cols in storage.LATE_COLUMNS.items():
    ident.add(tbl); ident |= set(cols)
for m in re.finditer(r"CREATE TABLE IF NOT EXISTS (\w+) \(([^;]*?)\n\s*\)", src, re.S):
    ident.add(m.group(1))
    ident |= set(re.findall(
        r"(?:^|,)\s*(\w+)\s+(?:INTEGER|INT|BIGINT|FLOAT|DOUBLE|TEXT|VARCHAR|LONGBLOB|BLOB|\{)",
        m.group(2), re.M))
clash = sorted(n for n in ident if n.upper() in RESERVED)
assert not clash, f"these would need backtick quoting on MySQL 8: {clash}"
print(f"{len(ident)} identifiers, none of them a MySQL 8 reserved word")

# --- the connection file ----------------------------------------------------
tmp = tempfile.mkdtemp()
bootstrap.DIR, bootstrap.PATH = tmp, os.path.join(tmp, "db.json")
bootstrap.LEGACY = os.path.join(tmp, "printer.config.json")

assert set(bootstrap.BACKENDS) == set(storage.DIALECTS), (
    f"bootstrap offers {sorted(bootstrap.BACKENDS)} but the app can talk to "
    f"{sorted(storage.DIALECTS)}")
assert set(bootstrap.SERVER_BACKENDS) == {b for b, d in storage.DIALECTS.items() if d["server"]}
print("bootstrap and storage agree on which backends exist")

got = bootstrap.clean({"backend": "mysql", "mysql": {
    "host": "h", "port": "3307", "user": "u", "password": "p", "database": "d"}})
assert got["backend"] == "mysql" and got["mysql"]["port"] == 3307, got
assert "mariadb" not in got, "a mysql connection was written under the mariadb key"

# switching an existing MariaDB install to MySQL keeps the details
switched = bootstrap.clean({"backend": "mysql", "mariadb": {
    "host": "keep-me", "user": "u", "password": "p", "database": "d"}})
assert switched["mysql"]["host"] == "keep-me", switched
assert "mariadb" not in switched, "the old block was left behind as well"
print("the block is keyed by backend, and switching carries the details over")

bootstrap.save({"backend": "mysql", "mysql": {
    "host": "h", "port": 3306, "user": "u", "password": "hunter2", "database": "d"}})
red = bootstrap.redacted()
assert red["backend"] == "mysql"
assert red["server"]["user"] == "u", "the page cannot read a mysql connection"
assert "hunter2" not in json.dumps(red), "the password reached the page"
print("the Settings page sees a mysql connection, without its password")

# --- the error the MySQL 8 default actually produces -----------------------
class Boom(Exception):
    pass


msg = bootstrap._friendly(
    Boom("Authentication plugin 'caching_sha2_password' cannot be loaded"), "mysql")
assert "cryptography" in msg, msg
assert "mysql_native_password" in msg, "no way out is offered for someone who cannot install it"
print("MySQL 8's default auth plugin fails with a message that says what to do")

# the reporter itself must never raise: a driver error with no args at all
# would otherwise turn "cannot connect" into a 500 from the wizard
assert isinstance(bootstrap._friendly(Boom(), "mysql"), str)
assert isinstance(bootstrap._friendly(Boom("plain"), "mysql"), str)
print("an error with no args is still reported, not re-raised")

ok, dead = bootstrap.test({"backend": "mysql", "mysql": {
    "host": "127.0.0.1", "port": 1, "user": "u", "password": "p", "database": "d"}})
assert not ok
assert "MySQL" in dead and "Package Center" not in dead, \
    f"a MySQL failure talks about Synology's MariaDB package: {dead}"
print("a MySQL failure is described as MySQL, not as Synology's MariaDB")

ok, dead = bootstrap.test({"backend": "mariadb", "mariadb": {
    "host": "127.0.0.1", "port": 1, "user": "u", "password": "p", "database": "d"}})
assert "Package Center" in dead, "the Synology hint was lost for MariaDB"
print("and the Synology hint is still there for MariaDB")

shutil.rmtree(tmp, ignore_errors=True)

# --- the wizard and the page offer it --------------------------------------
page = io.open(SRC + "setup.html", encoding="utf-8").read()
assert "mysql: \"MySQL\"" in page, "the wizard has no label for MySQL"
assert "seed.backends" in page, \
    "the wizard hardcodes its backend list instead of reading the server's"
assert "[backend]: {" in page, "the wizard still writes the block under a fixed key"
dash = io.open(SRC + "dashboard.html", encoding="utf-8").read()
assert "mysql: \"MySQL\"" in dash, "the Settings page cannot name a mysql connection"
assert 'c.backend === "mariadb"' not in dash, \
    "the Settings page still branches on mariadb specifically"
print("the wizard lists what the server offers; the Settings page has no per-backend branch")
print("ok")
