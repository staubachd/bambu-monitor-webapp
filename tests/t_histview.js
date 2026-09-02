// The expandable detail row under a print, against the real source.
//
// This is where a wrong MakerWorld link showed up as a real bug: the row builds
// its own URL from design_id, so a row with no model of its own must produce no
// link at all rather than the last one it saw. The rest is about a table that
// stays aligned - a full-width row must span exactly as many columns as the
// header has - and about not inventing values it does not have.
const path = require("path");
const SRC_DIR = process.env.BAMBU_SRC || path.join(__dirname, "..");
const fs = require("fs"), vm = require("vm");
const html = fs.readFileSync(path.join(SRC_DIR, "dashboard.html"), "utf8");
const src = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const lines = src.split("\n");
function grab(sig) {
  const s = lines.findIndex(l => l.startsWith(sig));
  if (s < 0) throw new Error("not found: " + sig);
  let e = s; while (lines[e] !== "}") e++;
  return lines.slice(s, e + 1).join("\n");
}

const ctx = {
  t: s => s,
  esc: s => String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;"),
  money: (v, c) => `${c} ${Number(v || 0).toFixed(2)}`,
  kgF: g => (Math.abs(g) >= 1000 ? (g / 1000).toFixed(2) + " kg" : Math.round(g) + " g"),
  fmtDur: (a, b) => (a && b ? Math.round((b - a) / 60) + " min" : "—"),
  swatch: c => `<i data-c="${c}"></i>`,
  filName: f => f.product || f.type || "?",
};
vm.createContext(ctx);
// detailRow leans on these; grabbed from the page rather than stubbed, so the
// panel is exercised with the real precedence and the real formatting
for (const fn of ["function fmtLH(", "function lhOf(", "function fmtMin(",
                  "function sliceBlock(", "function detailRow("]) {
  vm.runInContext(grab(fn), ctx);
}
const detail = r => vm.runInContext("detailRow(" + JSON.stringify(r) + ", '€')", ctx);

const BASE = {job_id: "j1", name: "bracket.3mf", started_at: 1750000000,
              ended_at: 1750003600, total_layers: 120, filament_g: 61.5,
              filament_detail: [], energy_wh: 180, cost: 0.05};

// --- a print of a MakerWorld model links to it -----------------------------
let h = detail({...BASE, design_id: "424242", profile_id: "7"});
if (!/makerworld\.com\/models\/424242/.test(h))
  throw new Error("no link for a print that has a model id: " + h.slice(0, 300));
if (!/profileId-7/.test(h)) throw new Error("the plate is missing from the link");
console.log("a model print links to:", h.match(/https:\/\/makerworld[^"']+/)[0]);

// --- a self-sliced print links to nothing ----------------------------------
h = detail({...BASE});
if (/makerworld/.test(h))
  throw new Error("a print with no model id still produced a MakerWorld link - "
                  + "this is exactly how a row showed the PREVIOUS print's model");
console.log("a self-sliced print produces no link at all");

// --- a model with no plate still links, without a dangling fragment --------
h = detail({...BASE, design_id: "424242"});
if (!/makerworld\.com\/models\/424242/.test(h)) throw new Error("no link: " + h);
if (/profileId-(?:undefined|null|")/.test(h))
  throw new Error("the link has an empty plate fragment: "
                  + h.match(/https:\/\/makerworld[^"']*/)[0]);
console.log("a model with no plate:", h.match(/https:\/\/makerworld[^"']+/)[0]);

// --- the id goes through the URL encoder -----------------------------------
h = detail({...BASE, design_id: 'x"><script>alert(1)</script>'});
if (/<script>alert/.test(h))
  throw new Error("a model id broke out of the href and became markup");
console.log("a hostile model id is encoded, not executed");

// --- what it does not know, it does not invent -----------------------------
h = detail({job_id: "j2", name: "x", filament_detail: []});
if (/NaN|undefined|null/.test(h))
  throw new Error("a row with almost no data rendered NaN/undefined: "
                  + h.replace(/\s+/g, " ").slice(0, 240));
console.log("a nearly empty row renders no NaN and no undefined");

// an error is shown when there is one, and the line is absent when there is not
if (!/Error/.test(detail({...BASE, error_code: "0300_1"})))
  throw new Error("a failed print does not show its error code");
if (/Error/.test(detail({...BASE})))
  throw new Error("a clean print shows an empty Error line");
console.log("the error line appears only when there is an error");

// --- a manual weight override wins over the cloud's estimate ---------------
h = detail({...BASE, filament_g: 61.5, filament_g_manual: 70});
if (!/70/.test(h))
  throw new Error("a hand-corrected weight is not shown: " + h.replace(/\s+/g, " ").slice(0, 300));
console.log("a hand-corrected weight is what the detail shows");

// --- the table stays aligned ------------------------------------------------
// Every full-width row must span exactly as many columns as the header has, or
// it tears the alignment of the whole table.
const head = html.slice(html.indexOf("tb.innerHTML =\n"));
const cols = (head.slice(0, head.indexOf("` +")).match(/<th[ >]/g) || []).length;
if (cols < 5) throw new Error("could not count the history header columns");
for (const [what, re] of Object.entries({
  "detail panel": /<tr class="detail"><td colspan="(\d+)">/,
  "group header": /<td colspan="(\d+)"><div class="grp-row">/,
  "divider": /<tr class="divider"><td colspan="(\d+)">/,
})) {
  const m = html.match(re);
  if (!m) throw new Error(`the ${what} row is gone`);
  if (+m[1] !== cols)
    throw new Error(`the ${what} spans ${m[1]} columns; the header has ${cols}`);
}
console.log(`${cols} columns, and all three full-width rows span them`);

// --- and the day filter drills into this same table ------------------------
if (!/dayFilter\s*\n?\s*\?\s*allRows\.filter/.test(src))
  throw new Error("the table no longer honours the day picked in the chart");
console.log("the table still filters by the day picked in the chart");
console.log("ok");
