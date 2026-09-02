// The page's own layer-height parser and cell, against the real source.
//
// The page parses what is typed so it can say "that is not a layer height"
// without a round trip. The server parses it again, because a browser is not
// an authority. Two parsers means two chances to disagree, and a field that
// accepts something and then rejects it is worse than one that never checked.
// So both are run against the same table of cases, in layerh_cases.json.
const _path = require("path");
const SRC_DIR = process.env.BAMBU_SRC || _path.join(__dirname, "..");
const _src = n => _path.join(SRC_DIR, n);
const fs = require("fs"), vm = require("vm");
const src = fs.readFileSync(_src("dashboard.html"), "utf8")
  .match(/<script>([\s\S]*?)<\/script>/)[1];
const lines = src.split("\n");
function grab(sig) {
  const s = lines.findIndex(l => l.startsWith(sig));
  if (s < 0) throw new Error("not found: " + sig);
  let e = s; while (lines[e] !== "}") e++;
  return lines.slice(s, e + 1).join("\n");
}
const CASES = JSON.parse(
  fs.readFileSync(_path.join(SRC_DIR, "tests", "layerh_cases.json"), "utf8"));

const ctx = {
  t: s => s, lastPrints: [],
  esc: s => String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;"),
};
vm.createContext(ctx);
vm.runInContext(grab("function parseLayerH("), ctx);
vm.runInContext(grab("function fmtLH("), ctx);
vm.runInContext(grab("function lastLayerH("), ctx);
const parse = s => vm.runInContext("parseLayerH(" + JSON.stringify(s) + ")", ctx);
const fmt = v => vm.runInContext("fmtLH(" + JSON.stringify(v) + ")", ctx);

// --- the same answers as the server, case for case -------------------------
for (const [raw, want] of CASES.accepts) {
  const got = parse(raw);
  if (got === null || Math.abs(got - want) > 1e-9) {
    throw new Error(`the page reads ${JSON.stringify(raw)} as ${got}, the server `
      + `stores ${want} - one of them is wrong and the user sees both`);
  }
}
console.log(`${CASES.accepts.length} spellings read the same way as the server`);

for (const [raw, why] of CASES.rejects) {
  if (parse(raw) !== null) {
    throw new Error(`the page accepts ${JSON.stringify(raw)} (${why}) and posts it; `
      + `the server refuses it with a 400, so the edit silently does nothing`);
  }
}
console.log(`${CASES.rejects.length} bad inputs refused before they are posted`);

// blank is not a typo: it is how the override is cleared, and the editor has to
// tell the two apart or clearing a value shows an error toast
if (parse("") !== null) throw new Error("blank is not treated as empty");
const editor = grab("function startEditLH(");
if (!/save && raw && parseLayerH\(raw\) == null/.test(editor)) {
  throw new Error("the editor does not separate 'blank' from 'unreadable' - "
    + "clearing a layer height would report a typo");
}
console.log("blank clears rather than complains");

// --- what it prints back ----------------------------------------------------
for (const [v, want] of [[0.2, "0.20 mm"], [0.08, "0.08 mm"], [0.055, "0.055 mm"],
                         [0.1, "0.10 mm"], [1, "1.00 mm"], [0.28, "0.28 mm"]]) {
  const got = fmt(v);
  if (got !== want) throw new Error(`fmtLH(${v}) is ${JSON.stringify(got)}, expected ${want}`);
}
if (fmt(null) !== "") throw new Error("an unset height formats as something");
console.log("0.2 shows as 0.20 mm, and 0.055 keeps its third decimal");

// --- the placeholder offers the last height that was used ------------------
ctx.lastPrints = [{ job_id: "a" }, { job_id: "b", layer_h: 0.16 }, { job_id: "c", layer_h: 0.2 }];
const last = vm.runInContext("lastLayerH()", ctx);
if (last !== 0.16) throw new Error(`the placeholder offers ${last}, not the most `
  + `recent height (rows arrive newest-first)`);
ctx.lastPrints = [{ job_id: "a" }];
if (vm.runInContext("lastLayerH()", ctx) !== null) {
  throw new Error("with nothing ever typed in, it still offers a number");
}
console.log("the editor offers the last height used, and nothing when there is none");

// --- the cell, in the table and in the detail panel -------------------------
// Both have to carry job and current value, or the click handler opens an
// editor that saves to nowhere.
const rowFn = grab("function printRow(");
if (!/class="num lh-cell"/.test(rowFn)) throw new Error("the layers cell is not editable");
for (const need of ['data-job=', 'data-lh=']) {
  if (!rowFn.includes(need)) throw new Error(`the layers cell has no ${need}`);
}
const detail = grab("function detailRow(");
if (!/class="lh-cell"/.test(detail)) {
  throw new Error("the detail panel shows the layer height but cannot edit it");
}
if (!/Model height/.test(detail)) {
  throw new Error("layers x layer height is the one number this makes possible, "
    + "and the detail panel does not show it");
}
// and the handler reaches both, not just the td
const handler = src.slice(src.indexOf('$("histTable").addEventListener("click"'));
if (!/closest\("\.lh-cell"\)/.test(handler.slice(0, 4000))) {
  throw new Error("the click handler matches a td only, so the line in the "
    + "detail panel looks editable and is not");
}
console.log("both the layers cell and the detail line open the editor");

// --- typed beats parsed, and the cell says which it is ---------------------
// A hand-typed 0.2 and one read off the sliced file look identical on screen,
// and only one of them is evidence. The cell has to carry both values (the
// editor opens on the typed one and offers the parsed one as its placeholder)
// and the title has to say where the number came from.
vm.runInContext(grab("function lhOf("), ctx);
vm.runInContext(grab("function lhTitle("), ctx);
const lhOf = r => vm.runInContext("lhOf(" + JSON.stringify(r) + ")", ctx);
const lhTitle = r => vm.runInContext("lhTitle(" + JSON.stringify(r) + ")", ctx);

if (lhOf({ layer_h: 0.2, layer_h_manual: 0.12 }) !== 0.12) {
  throw new Error("the slicer's figure won over the one somebody typed");
}
if (lhOf({ layer_h: 0.2, layer_h_manual: null }) !== 0.2) {
  throw new Error("with nothing typed, the slicer's figure is not shown");
}
if (lhOf({}) != null) throw new Error("a height was shown for a print that has none");
// zero is not "nothing": ?? not ||, or a 0 would fall through to the other value
if (lhOf({ layer_h: 0.2, layer_h_manual: 0 }) !== 0) {
  throw new Error("layer_h_manual of 0 fell through to the slicer's value");
}
{
  const typed = lhTitle({ layer_h: 0.2, layer_h_manual: 0.12 });
  if (!/typed in by hand/.test(typed) || !/0.20 mm/.test(typed)) {
    throw new Error("a typed height does not say so, or does not show what the "
      + "sliced file said it would be: " + typed);
  }
  const auto = lhTitle({ layer_h: 0.2 });
  if (!/from the sliced file/.test(auto)) {
    throw new Error("a parsed height is not marked as coming from the file: " + auto);
  }
  if (/typed in by hand/.test(auto)) throw new Error("a parsed height claims to be typed");
  if (!/click to note/.test(lhTitle({}))) throw new Error("an empty cell does not invite one");
}
console.log("typed beats parsed, 0 is a value, and the cell says which it is");

// --- the slicer panel ---------------------------------------------------------
vm.runInContext(grab("function fmtMin("), ctx);
vm.runInContext(grab("function sliceBlock("), ctx);
const slice = r => vm.runInContext("sliceBlock(" + JSON.stringify(r) + ")", ctx);
{
  const none = slice({ job_id: "j1" });
  if (!/sl-read/.test(none) || !/data-job="j1"/.test(none)) {
    throw new Error("a print with nothing read offers no way to go and read it");
  }
  const full = slice({
    job_id: "j1", started_at: 1750000000, ended_at: 1750000000 + 3600 * 3,
    slice: { profile: "0.12mm High Quality @BBL X2D", nozzle_mm: 0.4, walls: 2,
             infill_pct: 15, infill_pattern: "gyroid", supports: "tree(auto)",
             grams: 49.41, est_min: 162.4, first_layer_h: 0.2 },
  });
  for (const need of ["0.12mm High Quality", "0.4 mm", "15% gyroid", "tree(auto)",
                      "49.4 g", "2h 42m"]) {
    if (!full.includes(need)) throw new Error(`the panel does not show ${need}: ` + full);
  }
  // 3h actual against a 2h42 estimate: the difference is the interesting part
  if (!/\+18 min/.test(full)) {
    throw new Error("the estimate is shown without how far out it was: " + full);
  }
  if (/sl-read/.test(full)) throw new Error("it still offers to read what it has");
  // supports off must read as off, not vanish - absent and none are different
  if (!/none/.test(slice({ job_id: "j", slice: { grams: 1 } }))) {
    throw new Error("a print with no supports does not say so");
  }
}
console.log("the slicer panel shows the profile, the settings and the estimate delta");

// --- German ------------------------------------------------------------------
const de = src.slice(src.indexOf("const DE = {"), src.indexOf("\n};", src.indexOf("const DE = {")));
for (const s of ["Layer height", "Model height", "not noted",
                 "click to note the layer height"]) {
  if (!de.includes('"' + s + '"')) {
    throw new Error(`no German for ${JSON.stringify(s)} - it would show up as a `
      + `lone English label in an otherwise German page`);
  }
}
console.log("the new labels have German");
console.log("ok");
