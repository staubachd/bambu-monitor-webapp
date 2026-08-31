// The "Left" cell and its editor, against the real source.
//
// The cell has to carry three things the click handler then depends on: which
// filament it is, what the anchor currently is (so the box opens with it rather
// than blank), and what the AMS is reporting (so the button can offer it, and
// can be absent when there is nothing to offer).
// The app source, relative to this file rather than an absolute path.
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

const ctx = {
  t: s => s,
  esc: s => String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;"),
  kgF: g => (Math.abs(g) >= 1000 ? (g / 1000).toFixed(2) + " kg" : Math.round(g) + " g"),
};
vm.createContext(ctx);
vm.runInContext(grab("function leftCell("), ctx);
const cell = f => vm.runInContext("leftCell(" + JSON.stringify(f) + ")", ctx);

const base = {fkey: "GFA00|112233", editable: true, left_g: 800};

// --- computed: plain, and clickable ----------------------------------------
let h = cell(base);
if (!/class="num left edit"/.test(h)) throw new Error("not editable: " + h);
if (/<b>/.test(h)) throw new Error("a computed figure is shown as if it were pinned");
if (!/data-left=""/.test(h)) throw new Error("an unpinned cell should open its box empty");
if (!/bought minus used/.test(h)) throw new Error("no explanation of where it comes from");
console.log("computed  ->", h.match(/>([^<]*)<\/td>/)[1].trim(), "· plain, click to correct");

// --- pinned: bold, and the box opens with the anchor, not the shown total ---
h = cell({...base, left_g: 420, left_anchor_g: 480, left_anchor_at: 1750000000});
if (!/<b>/.test(h)) throw new Error("a pinned figure is not marked: " + h);
if (!/data-left="480"/.test(h))
  throw new Error("the editor would open with the aged figure, not the anchor: " + h);
if (!/pinned by hand/.test(h)) throw new Error("it does not say it was pinned");
if (!/\d{2}[./]\d{2}[./]\d{4}|\d{4}-\d{2}-\d{2}/.test(h))
  throw new Error("no date on the pin, so there is no way to know how old it is: " + h);
console.log("pinned    ->", h.match(/<b>([^<]*)<\/b>/)[1], "· bold, box opens at 480");

// --- the AMS offer is present only when there is something to offer --------
h = cell({...base, loaded: true, remain_pct: 37, grams_left: 370});
if (!/data-ams="370"/.test(h)) throw new Error("a tagged spool's reading was not offered: " + h);
for (const [what, f] of [
  ["not loaded", {...base}],
  ["no tag (-1%)", {...base, loaded: true, remain_pct: -1, grams_left: null}],
  ["external (0%)", {...base, loaded: true, remain_pct: 0, grams_left: null}],
]) {
  if (!/data-ams=""/.test(cell(f)))
    throw new Error(`${what}: an amount was offered that the AMS does not know`);
}
console.log("the AMS button is offered only for a spool that reports a real remaining");

// --- a row with no identity to write to must not look clickable ------------
h = cell({...base, editable: false});
if (/ edit"/.test(h)) throw new Error("a non-editable row was made clickable: " + h);
if (/title=/.test(h)) throw new Error("a non-editable row promises an interaction: " + h);
console.log("a purchase-only row is not clickable");

// --- negative is a warning, but not once it has been pinned ----------------
h = cell({...base, left_g: -120});
if (!/var\(--warn\)/.test(h)) throw new Error("a negative computed figure is not flagged");
h = cell({...base, left_g: -120, left_anchor_g: 50, left_anchor_at: 1750000000});
if (/var\(--warn\)/.test(h))
  throw new Error("a pinned figure was flagged as an incomplete log - it is not one");
console.log("negative flags an incomplete log, unless the figure was pinned");

// --- the editor renders its button, and the button cannot be stolen by blur -
const ec = grab("function editCell(");
if (!/actions/.test(ec)) throw new Error("editCell takes no actions");
if (!/mousedown[\s\S]{0,120}preventDefault/.test(ec))
  throw new Error("clicking the button would blur the input first and save an empty box");
if (!/class="wbtn cell-act"/.test(ec)) throw new Error("no button markup in editCell");
console.log("editCell renders action buttons, and blur cannot fire before the click");
console.log("ok");
