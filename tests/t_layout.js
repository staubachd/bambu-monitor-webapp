// The page is four sections, each with its own views. What this guards:
//   - the nav map, the markup and the router agree on which views exist
//   - every id the script reaches for exists in the markup
//   - the rebuilt views are wired to real data, not left as empty shells
// The app source, relative to this file rather than an absolute path.
const _path = require("path");
const SRC_DIR = process.env.BAMBU_SRC || _path.join(__dirname, "..");
const _src = n => _path.join(SRC_DIR, n);
const fs = require("fs");
const DIR = SRC_DIR + _path.sep;
const html = fs.readFileSync(DIR + "dashboard.html", "utf8");
const css = html.match(/<style>([\s\S]*?)<\/style>/)[1];
const body = html.match(/<body>([\s\S]*)<\/body>/)[1];

// --- the script must PARSE ------------------------------------------------
// Checked first because it is the one failure that takes everything with it. A
// syntax error anywhere in the page script means nothing is wired at all: no
// nav, no theme toggle, no live updates - the page just sits there saying
// "connecting...". Every other check in this file passes happily on a page that
// is completely dead, which is exactly what shipped once: a join("\n") whose
// backslash was lost became a literal newline inside a string literal, and the
// whole dashboard stopped working.
const vm = require("vm");
for (const [name, file] of [["dashboard.html", html],
                            ["setup.html", fs.readFileSync(_src("setup.html"), "utf8")]]) {
  for (const blk of file.match(/<script>[\s\S]*?<\/script>/g) || []) {
    const body = blk.slice("<script>".length, -"</script>".length);
    try {
      new vm.Script(body);
    } catch (e) {
      const at = (/<anonymous>:(\d+)/.exec(e.stack) || [])[1];
      const line = at ? body.split("\n")[+at - 1] : "";
      throw new Error(`${name} does not parse: ${e.message}`
        + (at ? `\n  line ${at}: ${line.trim().slice(0, 90)}` : "")
        + `\n  nothing on the page works while this is true`);
    }
  }
}
console.log("dashboard.html and setup.html both parse");

// braces balance
let depth = 0, line = 1;
for (const ch of css) {
  if (ch === "\n") line++;
  else if (ch === "{") depth++;
  else if (ch === "}" && --depth < 0) throw new Error(`unbalanced } at line ${line}`);
}
if (depth !== 0) throw new Error(`${depth} unclosed { in the stylesheet`);
console.log("stylesheet braces balance");

// ---- the nav map is the single source of truth ----------------------------
const navSrc = html.match(/const NAV = \{([\s\S]*?)\n\};/);
if (!navSrc) throw new Error("no NAV map in the page script");
const NAV = {};
for (const m of (navSrc[1] + "\n").matchAll(/(\w+):\s*\[(.*?)\],?\n/g))
  NAV[m[1]] = [...m[2].matchAll(/\["(\w+)","([^"]+)"\]/g)].map(x => [x[1], x[2]]);
const views = Object.entries(NAV).flatMap(([s, vs]) => vs.map(([v]) => `${s}/${v}`));
if (views.length < 8) throw new Error(`only ${views.length} views parsed from NAV`);

// a hidden section is reached by its own control rather than the section bar,
// so it is the one kind that may have no button
const hidden = new Set([...(html.match(/const NAV_HIDDEN = new Set\(\[([^\]]*)\]/) || ["", ""])[1]
  .matchAll(/"(\w+)"/g)].map(m => m[1]));
for (const sec of Object.keys(NAV)) {
  const hasButton = new RegExp(`data-sec="${sec}"`).test(body);
  if (!hasButton && !hidden.has(sec))
    throw new Error(`NAV has a "${sec}" section with no button in the masthead`);
  if (hasButton && hidden.has(sec))
    throw new Error(`"${sec}" is marked hidden but still has a button`);
}
// ...and it still needs a way in, or it is unreachable
for (const sec of hidden) {
  if (!html.includes(`go("${sec}"`))
    throw new Error(`nothing navigates to the hidden "${sec}" section`);
}
console.log(`${hidden.size} hidden section(s), each reachable by its own control`);
for (const b of body.matchAll(/data-sec="(\w+)"/g)) {
  if (!NAV[b[1]]) throw new Error(`a "${b[1]}" button exists but NAV has no such section`);
}
for (const key of views) {
  const id = "v-" + key.replace("/", "-");
  if (!new RegExp(`id="${id}"`).test(body))
    throw new Error(`NAV lists ${key} but there is no #${id} in the markup`);
}
for (const m of body.matchAll(/id="v-([\w-]+)"/g)) {
  const [sec, ...rest] = m[1].split("-");
  if (!views.includes(`${sec}/${rest.join("-")}`))
    throw new Error(`#v-${m[1]} exists but NAV does not list it`);
}
console.log(`${Object.keys(NAV).length} sections, ${views.length} views, markup and NAV agree`);

// exactly one view starts visible, and the router can reach every one of them
const on = [...body.matchAll(/class="dash view on[^"]*" id="(v-[\w-]+)"/g)].map(m => m[1]);
if (on.length !== 1) throw new Error(`${on.length} views are marked visible at load, expected 1`);
console.log(`one view (${on[0]}) is visible at load`);

// Each card belongs to exactly one view. A card left in two places renders
// twice and its ids stop being unique, which breaks every $() that reads it.
const dupes = [...body.matchAll(/id="([\w-]+)"/g)].map(m => m[1])
  .filter((id, i, a) => a.indexOf(id) !== i);
if (dupes.length) throw new Error("duplicated ids after the move: " + [...new Set(dupes)].join(" "));
console.log("every id appears exactly once");

// Every element the script reaches for must exist. `$("x").addEventListener`
// on a missing id throws at load and takes the rest of the page with it - the
// nav stops responding and the cause is nowhere near the symptom.
const declared = new Set([...body.matchAll(/id="([\w-]+)"/g)].map(m => m[1]));
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const wired = new Set([...script.matchAll(/\$\("([\w-]+)"\)\s*\.\s*(?:addEventListener|classList|style|textContent|innerHTML|value|src|href)/g)]
  .map(m => m[1]));
const phantom = [...wired].filter(id => !declared.has(id));
if (phantom.length)
  throw new Error("the script wires ids that are not in the markup: " + phantom.join(" "));
console.log(`${wired.size} ids the script touches all exist in the markup`);

// An unclosed tag is invisible to every other check here: the id it swallows is
// still THERE in the text, so the phantom-id scan above is satisfied while the
// element does not exist in the DOM at all. `<div id="x"` followed by
// `<span id="y">` parses as one div with a stray attribute, and y is gone.
// script bodies are not markup - `d < bd` in the chart code is not a tag
const markup = body.replace(/<script>[\s\S]*?<\/script>/g, "")
                   .replace(/<style>[\s\S]*?<\/style>/g, "");
for (const m of markup.matchAll(/<(?!\/)([a-zA-Z][\w-]*)((?:"[^"]*"|'[^']*'|[^><"'])*)/g)) {
  const rest = markup.slice(m.index + m[0].length, m.index + m[0].length + 1);
  if (rest !== ">") {
    const where = markup.slice(m.index, m.index + 70).replace(/\s+/g, " ");
    throw new Error(`unclosed <${m[1]}> tag - it swallows what follows: ${where}`);
  }
}
console.log("every tag in the markup closes before the next one opens");



// ---- the rebuilt views are wired, not empty shells -------------------------
const wiring = [
  // the camera has a section of its own, revealed only when one is configured
  ["live section", /id="v-live-camera"/.test(body) && /id="liveSec"/.test(body)],
  ["live section hidden until configured", /id="liveSec"[^>]*style="display:none"/.test(body)],
  ["camera streams only while shown", /setLive\(sec === "live"\)/.test(html)],
  ["live is unreachable without a camera", /sec === "live" && !\(CAM && CAM\.enabled\)/.test(html)],
  // a one-view section gets no second-level bar
  ["single-view sections skip the subnav", /NAV\[sec\]\.length < 2/.test(html)],
  // an idle machine reports its state instead of an empty ring
  ["idle panel", /function renderIdle/.test(html) && /id="idleWrap"/.test(body)],
  ["idle panel swaps with the ring", /\$\("ringWrap"\)\.style\.display/.test(html)],
  // the print detail that did not exist
  ["print detail", /function detailRow/.test(html) && /\n\s*\.det\{/.test(css)],
  ["detail reads the per-slot breakdown", /r\.filament_detail/.test(html)],
  ["detail toggles per row", /histDet/.test(html)],
  // chart and table are one page, and one filters the other
  ["day chart drills into the table", /function setDayFilter/.test(html)],
  ["the table honours the filter", /dayFilter\s*\n?\s*\?\s*allRows\.filter/.test(html)],
  // the importer is folded away
  ["importer folded away", /id="buyImportBtn"/.test(body) && /#buyImport\{display:none\}/.test(css)],
  // deep links
  ["views are linkable", /addEventListener\("hashchange"/.test(html)],
];
for (const [what, ok] of wiring) if (!ok) throw new Error("not wired: " + what);
console.log(`${wiring.length} rebuilt behaviours wired`);

// The history table gained a column for the expander. Its full-width rows -
// group headers, dividers, the detail panel - must span exactly as many columns
// as the header has, or every one of them tears the table's alignment.
const head = html.slice(html.indexOf('tb.innerHTML =\n'));
const cols = (head.slice(0, head.indexOf("` +")).match(/<th[ >]/g) || []).length;
const wide = {
  "group header": /<td colspan="(\d+)"><div class="grp-row">/,
  "divider":      /<tr class="divider"><td colspan="(\d+)">/,
  "detail panel": /<tr class="detail"><td colspan="(\d+)">/,
};
for (const [what, re] of Object.entries(wide)) {
  const m = html.match(re);
  if (!m) throw new Error(`the ${what} row is gone from the history table`);
  if (+m[1] !== cols)
    throw new Error(`the ${what} spans ${m[1]} columns, the header has ${cols}`);
}
console.log(`history table: ${cols} columns, and all ${Object.keys(wide).length} full-width rows span them`);

// ---- translations ----------------------------------------------------------
const de = html.match(/const DE = \{([\s\S]*?)\n\};/)[1];
const missing = [];
for (const [, label] of Object.values(NAV).flat())
  if (!new RegExp(`"${label.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}":`).test(de)) missing.push(label);
if (missing.length) throw new Error("no German for: " + missing.join(", "));
// two views must not end up with the same German name
const seen = new Map();
for (const [, label] of Object.values(NAV).flat()) {
  const g = (de.match(new RegExp(`"${label}":"([^"]*)"`)) || [])[1];
  if (g && seen.has(g)) throw new Error(`"${label}" and "${seen.get(g)}" are both "${g}" in German`);
  if (g) seen.set(g, label);
}
console.log("every view has a German name, and no two share one");
console.log("ok");
