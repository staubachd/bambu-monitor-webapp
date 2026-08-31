// The wizard's database step, run against the real source.
//
// It is the one page that decides which backend the whole install uses, and
// two things there are easy to get subtly wrong: the option list must come from
// the server (so a backend added to storage.DIALECTS appears without editing
// this page), and the connection block must be keyed by the chosen backend (so
// switching MariaDB -> MySQL cannot leave a stale block behind under the old
// key, which would silently connect to the wrong thing).
// The app source, relative to this file rather than an absolute path.
const _path = require("path");
const SRC_DIR = process.env.BAMBU_SRC || _path.join(__dirname, "..");
const _src = n => _path.join(SRC_DIR, n);
const fs = require("fs"), vm = require("vm");
const src = fs.readFileSync(_src("setup.html"), "utf8")
  .match(/<script>([\s\S]*?)<\/script>/)[1];
const lines = src.split("\n");
function grab(sig) {
  const s = lines.findIndex(l => l.startsWith(sig));
  if (s < 0) throw new Error("not found in setup.html: " + sig);
  let e = s; while (lines[e] !== "}") e++;
  return lines.slice(s, e + 1).join("\n");
}
const constant = name => {
  const s = lines.findIndex(l => l.startsWith("const " + name));
  if (s < 0) throw new Error("not found: " + name);
  let e = s; while (!lines[e].startsWith("};")) e++;
  return lines.slice(s, e + 1).join("\n");
};

// the seed the server actually sends, minus everything this step ignores
const seed = {backends: ["sqlite", "mariadb", "mysql"], is_set: {}};
const fields = {};
const ctx = {
  t: s => s,
  esc: s => String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;"),
  seed,
  $: id => ({value: fields[id] === undefined ? "" : fields[id]}),
  db: null,
};
vm.createContext(ctx);
vm.runInContext([constant("BACKEND_LABEL"), grab("function dbStep("),
                 grab("function collectDb(")].join(";\n"), ctx);

// --- every backend the server offers is on the list ------------------------
ctx.db = {backend: "mariadb", mariadb: {host: "127.0.0.1", port: 3306,
                                        user: "bambu", database: "bambu_monitor"}};
let html = vm.runInContext("dbStep()", ctx);
for (const [value, label] of [["sqlite", "SQLite"], ["mariadb", "MariaDB"], ["mysql", "MySQL"]]) {
  const re = new RegExp(`<option value="${value}"[^>]*>${label}`);
  if (!re.test(html)) throw new Error(`${value} is missing from the dropdown:\n${html.slice(0, 400)}`);
}
console.log("all three backends appear, each with its own label");

// and the list is the server's, not this file's
seed.backends = ["sqlite", "mariadb", "mysql", "postgres"];
html = vm.runInContext("dbStep()", ctx);
if (!/<option value="postgres"/.test(html))
  throw new Error("the page ignores a backend the server offers - the list is hardcoded");
seed.backends = ["sqlite", "mariadb", "mysql"];
console.log("a backend added on the server shows up without editing the page");

// exactly one option is selected, and it is the configured one
const selected = [...html.matchAll(/<option value="(\w+)" selected>/g)].map(m => m[1]);
ctx.db = {backend: "mysql", mysql: {host: "h", port: 3306, user: "u", database: "d"}};
const sel2 = [...vm.runInContext("dbStep()", ctx).matchAll(/<option value="(\w+)" selected>/g)]
  .map(m => m[1]);
if (sel2.length !== 1 || sel2[0] !== "mysql")
  throw new Error(`expected only mysql selected, got ${JSON.stringify(sel2)}`);
console.log("the configured backend is the one preselected");

// --- the server fields read whichever block the backend names --------------
if (!/value="h"/.test(vm.runInContext("dbStep()", ctx)))
  throw new Error("a mysql connection's host did not reach the form");
ctx.db = {backend: "mysql", mariadb: {host: "from-the-old-key", port: 3306, user: "u", database: "d"}};
if (!/value="from-the-old-key"/.test(vm.runInContext("dbStep()", ctx)))
  throw new Error("switching an existing MariaDB install to MySQL lost its details");
console.log("the form reads the backend's own block, falling back to the old key");

// --- and writes back under the backend's own name --------------------------
Object.assign(fields, {dbBackend: "mysql", dbHost: "10.0.0.5", dbPort: "3307",
                       dbUser: "bambu", dbPass: "pw", dbName: "bm"});
vm.runInContext("collectDb()", ctx);
let out = ctx.db;
if (out.backend !== "mysql") throw new Error("backend not carried: " + out.backend);
if (!out.mysql) throw new Error("no block under the chosen backend: " + JSON.stringify(out));
if (out.mariadb) throw new Error("a stale mariadb block was left behind: " + JSON.stringify(out));
if (out.mysql.port !== 3307) throw new Error("the port was not a number: " + JSON.stringify(out.mysql));
console.log("collectDb writes one block, keyed by the chosen backend:", JSON.stringify(out.mysql));

fields.dbBackend = "sqlite";
fields.dbPath = "";
vm.runInContext("collectDb()", ctx);
out = ctx.db;
if (out.backend !== "sqlite" || out.mysql || out.mariadb)
  throw new Error("switching to sqlite left a server block behind: " + JSON.stringify(out));
if (out.sqlite_path !== "telemetry.db")
  throw new Error("an empty filename did not fall back to a default: " + out.sqlite_path);
console.log("switching to SQLite drops the server block and defaults the filename");
console.log("ok");
