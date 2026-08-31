// Sorting and filtering the filament table, against the real source.
//
// Two rules carry the weight. First: **missing is not zero.** A filament whose
// "Left" is unknown must sink to the bottom whichever way the column is sorted,
// or sorting ascending puts every unknown at the top and the page looks like a
// list of empty spools. Second: the order has to be **total** - ties broken by
// something stable - or rows shuffle between refreshes for no reason.
const path = require("path");
const SRC_DIR = process.env.BAMBU_SRC || path.join(__dirname, "..");
const fs = require("fs"), vm = require("vm");
const src = fs.readFileSync(path.join(SRC_DIR, "dashboard.html"), "utf8")
  .match(/<script>([\s\S]*?)<\/script>/)[1];
const lines = src.split("\n");
function grab(sig) {
  const s = lines.findIndex(l => l.startsWith(sig));
  if (s < 0) throw new Error("not found: " + sig);
  let e = s; while (lines[e] !== "}") e++;
  return lines.slice(s, e + 1).join("\n");
}
function block(sig, endsWith) {
  const s = lines.findIndex(l => l.startsWith(sig));
  if (s < 0) throw new Error("not found: " + sig);
  let e = s; while (!lines[e].startsWith(endsWith)) e++;
  return lines.slice(s, e + 1).join("\n");
}

const ctx = {filSort: {key: "grams", dir: -1}, filScope: "all", filSearch: ""};
vm.createContext(ctx);
vm.runInContext([block("const FIL_KEYS", "};"), grab("function filCompare("),
                 grab("function filMatches(")].join("\n"), ctx);

const sortBy = (rows, key, dir) => {
  ctx.filSort = {key, dir};
  return vm.runInContext("ROWS.slice().sort(filCompare)",
                         Object.assign(ctx, {ROWS: rows})).map(r => r.fkey);
};

const rows = [
  {fkey: "a", product: "PLA Basic", color_name: "Red", type: "PLA",
   grams: 300, left_g: 700, cost: 6, prints: 3, price_per_kg: 20, last_used: 300},
  {fkey: "b", product: "PLA Matte", color_name: "Blue", type: "PLA",
   grams: 100, left_g: null, cost: 2, prints: 1, price_per_kg: null,
   list_per_kg: 25, last_used: 100},
  {fkey: "c", product: "PETG", color_name: "Clear", type: "PETG",
   grams: 200, left_g: 100, cost: 4, prints: 2, price_per_kg: 30, last_used: 200},
];

// --- missing sinks, both ways ----------------------------------------------
let desc = sortBy(rows, "left_g", -1);
let asc = sortBy(rows, "left_g", 1);
if (desc[desc.length - 1] !== "b")
  throw new Error("descending by Left: the unknown is not last: " + desc);
if (asc[asc.length - 1] !== "b")
  throw new Error("ascending by Left: the unknown floated to the top (" + asc + ") - "
                  + "'no data' is not a small number");
console.log("Left, descending:", desc.join(" "), "· ascending:", asc.join(" "));

// --- and the known values are in the right order ---------------------------
if (desc.slice(0, 2).join("") !== "ac")
  throw new Error("descending by Left is not descending: " + desc);
if (asc.slice(0, 2).join("") !== "ca")
  throw new Error("ascending by Left is not ascending: " + asc);
console.log("the known values order correctly both ways");

// --- every column the header offers can actually sort ----------------------
const headers = [...src.matchAll(/\$\{th\("(\w+)",/g)].map(m => m[1]);
if (headers.length < 5) throw new Error("could not find the table headers");
const keys = Object.keys(vm.runInContext("FIL_KEYS", ctx));
for (const h of headers) {
  if (!keys.includes(h))
    throw new Error(`the header "${h}" is clickable but FIL_KEYS has no key for it, `
                    + `so clicking it silently sorts by grams`);
}
console.log(`all ${headers.length} sortable headers have a key:`, headers.join(" "));

// --- the price column sorts by what it displays ----------------------------
// row b has no hand-set price and inherits 25 from the invoices; sorting by
// price must use the 25 it shows, not treat it as unknown
const byPrice = sortBy(rows, "price_per_kg", 1);
if (byPrice.indexOf("b") === byPrice.length - 1)
  throw new Error("a filament priced from invoices sank as if it had no price: " + byPrice);
if (byPrice.join("") !== "abc")
  throw new Error("price order is wrong: " + byPrice + " (expected 20, 25, 30)");
console.log("price sorts by the figure shown, inherited or not:", byPrice.join(" "));

// --- text sorts as text, not by code point ---------------------------------
const named = [
  {fkey: "x", product: "Ölfarbe", type: "PLA", grams: 1},
  {fkey: "y", product: "Zink", type: "PLA", grams: 1},
  {fkey: "z", product: "Apfel", type: "PLA", grams: 1},
];
const byName = sortBy(named, "name", 1);
if (byName[0] !== "z")
  throw new Error("name sort did not start at 'Apfel': " + byName);
if (byName.indexOf("x") > byName.indexOf("y"))
  throw new Error("'Ölfarbe' sorted after 'Zink' - a plain byte comparison, not "
                  + "localeCompare, so every umlaut lands at the end: " + byName);
console.log("names sort with localeCompare, so Ö is not after Z:", byName.join(" "));

// --- the order is total: ties do not shuffle -------------------------------
const tied = [{fkey: "p", grams: 5}, {fkey: "q", grams: 5}, {fkey: "r", grams: 5}];
const first = sortBy(tied, "grams", -1).join("");
for (let i = 0; i < 20; i++) {
  const again = sortBy(tied.slice().reverse(), "grams", -1).join("");
  if (again !== first)
    throw new Error(`rows with equal values came back as ${again} then ${first} - `
                    + `the table reshuffles on every refresh`);
}
console.log("equal values break the tie stably:", first);

// --- the filters ------------------------------------------------------------
const scoped = [
  {fkey: "l", loaded: true, low: false, unused: false},
  {fkey: "m", loaded: false, low: true, unused: false},
  {fkey: "n", loaded: false, low: false, unused: true},
];
const keep = scope => {
  ctx.filScope = scope;
  return scoped.filter(f => vm.runInContext("filMatches(" + JSON.stringify(f) + ")", ctx))
    .map(f => f.fkey);
};
if (keep("all").length !== 3) throw new Error("'all' dropped something: " + keep("all"));
if (keep("ams").join("") !== "l") throw new Error("'in AMS': " + keep("ams"));
if (keep("low").join("") !== "m") throw new Error("'low': " + keep("low"));
if (keep("unused").join("") !== "n") throw new Error("'unused': " + keep("unused"));
console.log("the four scopes each keep exactly what they name");

// --- and the search box looks at every name a spool has --------------------
ctx.filScope = "all";
const spool = {fkey: "s", vendor: "Sunlu", product: "PLA Meta",
               color_name: "Coffee Brown", code: "A00-K0", type: "PLA",
               color: "7C4B00", filament_id: "GFL99"};
const finds = q => {
  ctx.filSearch = q;
  return vm.runInContext("filMatches(" + JSON.stringify(spool) + ")", ctx);
};
for (const q of ["sunlu", "meta", "coffee", "a00-k0", "pla", "7c4b00", "gfl99"])
  if (!finds(q)) throw new Error(`searching "${q}" does not find its own spool`);
if (finds("bambu")) throw new Error("the search matches something that is not there");
console.log("the search matches vendor, product, colour, code, type, hex and SKU");
console.log("ok");
