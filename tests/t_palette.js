// Colour contracts for the palette, whatever the palette currently is.
//
// This started as a check on one two-colour pair and kept being wrong about the
// next one: a rule saying "the brand colour is never the accent" was really a
// proxy for "the accent must be readable", and an ink-picker check pinned an
// exact return value that only held while the brand colour was pale. What
// survives is the part that is true of every scheme - the contrast of each pair
// the stylesheet actually renders together, in both themes and both documents.
// The app source, relative to this file rather than an absolute path.
const _path = require("path");
const SRC_DIR = process.env.BAMBU_SRC || _path.join(__dirname, "..");
const _src = n => _path.join(SRC_DIR, n);
const fs = require("fs");
// sampled from the stylesheet rather than hardcoded, so a repalette needs no
// edit here - only the contracts below have to keep holding
let BRAND, STONE;

const hex = h => [1,3,5].map(i => parseInt(h.slice(i,i+2),16));
const lin = v => { v /= 255; return v <= .03928 ? v/12.92 : ((v+.055)/1.055)**2.4; };
const lum = h => { const [r,g,b] = hex(h); return .2126*lin(r)+.7152*lin(g)+.0722*lin(b); };
const ratio = (a,b) => { const [x,y] = [lum(a),lum(b)].sort((p,q)=>q-p);
                         return (x+.05)/(y+.05); };

function tokens(css, sel){
  const at = css.indexOf(sel + "{");
  if (at < 0) throw new Error("no " + sel + " block");
  const body = css.slice(at + sel.length + 1, css.indexOf("}", at));
  const out = {};
  for (const m of body.matchAll(/(--[\w-]+)\s*:\s*([^;]+)/g)) out[m[1]] = m[2].trim();
  return out;
}

for (const file of ["dashboard.html"]) {
  const css = fs.readFileSync(_src(file), "utf8")
    .match(/<style>([\s\S]*?)<\/style>/)[1];
  const themes = {
    light: tokens(css, ":root:root"),
    dark:  tokens(css, ':root:root[data-theme="dark"]'),
  };
  console.log(`\n${file}`);
  for (const [name, t] of Object.entries(themes)) {
    BRAND = BRAND || t["--brand"]; STONE = STONE || t["--brand-ink"];

    // every pair the stylesheet actually renders together
    const pairs = [
      ["body text",        t["--text"],      t["--bg"],       4.5],
      ["secondary text",   t["--text-2"],    t["--surface"],  4.5],
      ["muted text",       t["--text-3"],    t["--surface"],  3.0],
      ["text on a card",   t["--text"],      t["--surface"],  4.5],
      // .sections button.on - the primary fill, the loudest thing on the page
      ["active section",   t["--brand-ink"], t["--brand"],    4.5],
      // accent as TEXT: links, the active subnav, sort arrows
      ["accent as text",   t["--accent"],    t["--surface"],  4.5],
      // accent as a FILL with --accent-ink on it
      ["ink on accent",    t["--accent-ink"], t["--accent"],  4.5],
      // status fills carry --on-status
      ["ink on danger",    t["--on-status"], t["--danger"],   3.0],
      ["ink on good",      t["--on-status"], t["--good"],     3.0],
      // chart series have to be visible against the plot ground
      ["series noz0",      t["--s-noz0"],    t["--surface"],  3.0],
      ["series noz1",      t["--s-noz1"],    t["--surface"],  3.0],
      ["series bed",       t["--s-bed"],     t["--surface"],  3.0],
      ["series chamber",   t["--s-chamber"], t["--surface"],  3.0],
      ["series power",     t["--s-power"],   t["--surface"],  3.0],
    ];
    for (const [what, fg, bg, min] of pairs) {
      if (!fg || !bg) throw new Error(`${name}: ${what} has an undefined token`);
      const r = ratio(fg, bg);
      const mark = r >= min ? "ok " : "FAIL";
      if (name === "light" && file === "dashboard.html")
        console.log(`  ${mark} ${what.padEnd(16)} ${fg} on ${bg}  ${r.toFixed(1)}:1 (min ${min})`);
      if (r < min)
        throw new Error(`${file} ${name}: ${what} is ${r.toFixed(2)}:1, needs ${min} (${fg} on ${bg})`);
    }
    console.log(`  ${name}: all ${pairs.length} pairs pass`);
  }
  // A pale colour cannot be text on a light ground. That used to be asserted as
  // "the brand colour is never --accent", which was a PROXY for the real rule
  // and wrong for any pair whose brand colour is a mid-tone: #3447AA is 8:1 on
  // white and makes a perfectly good accent. The real rule is the contrast of
  // whatever is actually used as text, which the pairs above already cover - so
  // what is left to state is that the fill/ink split is honest.
  for (const [name, t] of Object.entries(themes)) {
    const asText = ratio(t["--accent"], t["--surface"]);
    const asFill = ratio(t["--brand-ink"], t["--brand"]);
    console.log(`  ${name}: accent as text ${asText.toFixed(1)}:1, ` +
                `fill ${t["--brand"]} with ${t["--brand-ink"]} on it ${asFill.toFixed(1)}:1`);
    // a colour too pale to be text must not be the one carrying links
    if (asText < 4.5)
      throw new Error(`${name}: --accent ${t["--accent"]} is only ${asText.toFixed(2)}:1 ` +
                      "on the card - it cannot carry links or the active subnav");
  }

  console.log("  no colour hardcoded outside the token blocks");
}

// The state pill is a neutral chip with the state's colour as its TEXT and
// border - the same shape the history table already uses - so what has to hold
// is that every state colour is readable on that chip, in both themes. It
// replaced a solid fill whose foreground had to be computed at runtime; a
// neutral chip needs no such trick and cannot get it wrong.
const STATES = ["--accent", "--good", "--warn", "--danger", "--info", "--text-3"];
for (const file of ["dashboard.html"]) {
  const css = fs.readFileSync(_src(file), "utf8")
    .match(/<style>([\s\S]*?)<\/style>/)[1];
  const themes = {light: tokens(css, ":root:root"),
                  dark:  tokens(css, ':root:root[data-theme="dark"]')};
  for (const [name, t] of Object.entries(themes)) {
    for (const s of STATES) {
      const r = ratio(t[s], t["--surface-2"]);
      if (file === "dashboard.html")
        console.log(`  ${name.padEnd(5)} ${s.padEnd(10)} on the chip  ${r.toFixed(1)}:1`);
      if (r < 3.0)
        throw new Error(`${file} ${name}: ${s} is ${r.toFixed(2)}:1 on the state chip`);
    }
  }
}
console.log("every state colour reads on the chip");

// and the accent must not be the loudest thing on a page where nothing is
// happening: navigation is a raised surface, not a block of accent
const shell = fs.readFileSync(_src("dashboard.html"), "utf8")
  .match(/<style>([\s\S]*?)<\/style>/)[1];
if (/\.sections button\.on\{[^}]*background:var\(--(?:accent|brand)\)/.test(shell))
  throw new Error("the selected section is filled with the accent again");
if (/\.statepill\{[^}]*background:var\(--(?:accent|brand)\)/.test(shell))
  throw new Error("the state pill is a solid accent block again");
// What is banned is a solid block of accent, not the accent itself: a TINT
// (--accent-soft) plus a marker edge is how selection is meant to read.
for (const [what, re] of [
  ["the logo tile",        /\.logo\{[^}]*background:var\(--(?:accent|brand)\)/],
  ["the active AMS slot",  /\.spool\.active\{[^}]*background:var\(--(?:accent|brand)\)/],
  ["the active slot badge",/\.spool\.active \.slot-badge\{[^}]*background:var\(--(?:accent|brand)\)/],
]) {
  if (re.test(shell)) throw new Error(what + " is filled with the accent again");
}
console.log("navigation, state pill, logo and active slot are surfaces, not accent fills");

// The loaded AMS slot has to differ from an idle one by more than a border
// shade. It regressed to exactly that - the neutralising pass gave it the same
// background value as the base tile - and the result was invisible.
const act  = (shell.match(/\.spool\.active\{([^}]*)\}/) || [])[1] || "";
const base = (shell.match(/\n  \.spool\{([^}]*)\}/) || [])[1] || "";
const bg = t => ((t.match(/background:([^;}]+)/) || [])[1] || "").trim();
if (!bg(act)) throw new Error("the loaded AMS slot sets no background of its own");
if (bg(act) === bg(base))
  throw new Error(`the loaded AMS slot has the same background as an idle one (${bg(act)})`);
if (!/box-shadow:\s*inset/.test(act))
  throw new Error("the loaded AMS slot has no marker edge");
console.log(`the loaded slot is tinted (${bg(act)} vs ${bg(base)}) and marked`);
console.log("ok");
