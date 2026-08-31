// The browser-tab indicator, against the real source.
//
// The point of it is to tell you a print finished while you were looking at
// something else. That makes the EVENT the thing to catch, not the state: the
// printer keeps reporting "Finished" for hours afterwards, so a page opened the
// next morning must not greet you with a tick as though it had just happened.
//
// Hence a latch, and three rules that are easy to get wrong:
//   * only a transition from busy to done, and only while the tab is hidden
//   * a new job supersedes a latched finish
//   * looking at the tab is what clears it
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
const constant = name => {
  const l = lines.find(x => x.startsWith("const " + name));
  if (!l) throw new Error("not found: const " + name);
  return l;
};

const ctx = {
  t: s => s,
  document: {hidden: false, title: ""},
  BASE_TITLE: "Bambu X2D",
  FAV_DEFAULT: "default-icon",
  setFavicon: v => { ctx.favicon = v; },
  drawFavicon: (state, pct) => `${state}:${Math.round(pct)}`,
  prevBusy: false, doneLatch: null, tabKey: null, lastState: null,
  favicon: null,
};
vm.createContext(ctx);
vm.runInContext([constant("BUSY_STATES"), constant("DONE_STATES"),
                 grab("function updateTab(")].join("\n"), ctx);

const tab = job => vm.runInContext("updateTab(" + JSON.stringify(job) + ")", ctx);
const title = () => ctx.document.title;

// --- while you are watching, it just tracks the print ----------------------
ctx.document.hidden = false;
tab({state_label: "Printing", percent: 42, name: "bracket.3mf"});
if (!/42%/.test(title())) throw new Error("no progress in the title: " + title());
if (!/bracket\.3mf/.test(title())) throw new Error("no job name in the title: " + title());
console.log("printing, tab visible ->", title());

// While the printer says Finished, the tab says so either way - the latch is
// not about that moment. It is about what happens NEXT: the printer goes idle,
// and a finish you watched should leave with it.
tab({state_label: "Finished", percent: 100, name: "bracket.3mf"});
tab({state_label: "Idle", percent: 0});
if (/✓/.test(title()))
  throw new Error("a finish you watched happen was still flagged after the "
                  + "printer went idle: " + title());
console.log("finished while watching, then idle -> nothing left over:", title());

// --- the case it exists for -------------------------------------------------
ctx.prevBusy = false; ctx.doneLatch = null; ctx.tabKey = null;
ctx.document.hidden = true;
tab({state_label: "Printing", percent: 90, name: "bracket.3mf"});
tab({state_label: "Finished", percent: 100, name: "bracket.3mf"});
if (!/✓/.test(title()))
  throw new Error("a print finished in a hidden tab raised no flag: " + title());
if (!/bracket\.3mf/.test(title())) throw new Error("the flag does not say what finished");
console.log("finished while hidden ->", title());

// and it survives the printer moving on, which is the whole point: you were
// not there, so the tab has to keep the news until you are
tab({state_label: "Idle", percent: 0});
if (!/✓/.test(title()))
  throw new Error("the printer went idle and took the news with it: " + title());
console.log("...and survives the printer going idle:", title());

// looking at the tab is what clears it
ctx.document.hidden = false;
ctx.doneLatch = null; ctx.tabKey = null;      // what the visibilitychange handler does
tab({state_label: "Idle", percent: 0});
if (/✓/.test(title())) throw new Error("looking at the tab did not clear it");
console.log("looking at the tab clears it");

// --- a page opened the next morning must not invent one --------------------
ctx.prevBusy = false; ctx.doneLatch = null; ctx.tabKey = null;
ctx.document.hidden = true;
tab({state_label: "Finished", percent: 100, name: "yesterday.3mf"});
tab({state_label: "Idle", percent: 0});
if (/✓/.test(title()))
  throw new Error("a page loaded onto an already-finished printer claimed it had "
                  + "just finished: " + title());
console.log("opening onto an already-finished printer ->", title());

// --- a new job supersedes a latched finish ---------------------------------
ctx.prevBusy = false; ctx.doneLatch = null; ctx.tabKey = null;
ctx.document.hidden = true;
tab({state_label: "Printing", percent: 99, name: "a.3mf"});
tab({state_label: "Finished", percent: 100, name: "a.3mf"});
if (!/✓/.test(title())) throw new Error("setup failed");
tab({state_label: "Printing", percent: 3, name: "b.3mf"});
if (/✓/.test(title()))
  throw new Error("the next print started and the old finish was still showing: "
                  + title());
if (!/3%/.test(title())) throw new Error("the new job is not being reported: " + title());
console.log("a new job supersedes it ->", title());

// --- a failure is flagged too, and distinguishably -------------------------
ctx.prevBusy = false; ctx.doneLatch = null; ctx.tabKey = null;
ctx.document.hidden = true;
tab({state_label: "Printing", percent: 50, name: "c.3mf"});
tab({state_label: "Failed", percent: 50, name: "c.3mf"});
if (!/✕/.test(title())) throw new Error("a failure raised no flag: " + title());
if (/✓/.test(title())) throw new Error("a failure was flagged as a success: " + title());
console.log("a failure is flagged, and differently ->", title());

// --- the favicon follows, and idle has no badge ----------------------------
ctx.prevBusy = false; ctx.doneLatch = null; ctx.tabKey = null;
ctx.document.hidden = false;
tab({state_label: "Printing", percent: 55});
if (ctx.favicon === "default-icon") throw new Error("a running print has the plain icon");
tab({state_label: "Idle"});
if (ctx.favicon !== "default-icon")
  throw new Error("an idle printer keeps a badged icon: " + ctx.favicon);
if (title() !== "Bambu X2D") throw new Error("an idle tab is not plain: " + title());
console.log("idle -> plain title and plain icon");

// --- and the work is skipped when nothing changed --------------------------
// updateTab runs on every state push, several times a second. Rewriting the
// title and redrawing a canvas favicon that often is visible as flicker.
ctx.prevBusy = false; ctx.doneLatch = null; ctx.tabKey = null;
let draws = 0;
ctx.drawFavicon = (s, p) => { draws++; return `${s}:${p}`; };
for (let i = 0; i < 20; i++) tab({state_label: "Printing", percent: 42});
if (draws !== 1) throw new Error(`${draws} favicon redraws for 20 identical states`);
tab({state_label: "Printing", percent: 43});
if (draws !== 2) throw new Error("a real change did not redraw");
console.log("20 identical pushes redraw once; a changed percent redraws again");
console.log("ok");
