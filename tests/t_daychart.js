// The per-day chart, and the key it shares with the table it drills into.
//
// The chart and the history table agree on one thing or they agree on nothing:
// the day key. Clicking a bar filters the table by `dayKey(...)` of each print's
// start, and the server buckets by `strftime("%Y-%m-%d")` of the same instant.
// Both must be LOCAL dates. A bare `toISOString().slice(0,10)` is UTC, and in
// Germany that silently files everything printed after 22:00 (23:00 in summer)
// under tomorrow - the bar says 3 prints and the table opens 2.
//
// The other thing that matters is that quiet days stay visible: a window of 30
// days must draw 30 bars, not just the days that happen to have data, or the
// chart compresses a gap out of existence.
const path = require("path");
const SRC_DIR = process.env.BAMBU_SRC || path.join(__dirname, "..");
const fs = require("fs"), vm = require("vm");
const html = fs.readFileSync(path.join(SRC_DIR, "dashboard.html"), "utf8");
const src = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const lines = src.split("\n");
const arrow = name => {
  const l = lines.find(x => x.startsWith("const " + name + " ="));
  if (!l) throw new Error("not found: const " + name);
  return l;
};

const ctx = {};
vm.createContext(ctx);
vm.runInContext(arrow("dayKey"), ctx);
const key = d => vm.runInContext("dayKey(D)", Object.assign(ctx, {D: d}));

// --- the key is a LOCAL date ------------------------------------------------
const late = new Date(2026, 7, 29, 23, 30, 0);      // 29 Aug 2026, 23:30 local
if (key(late) !== "2026-08-29")
  throw new Error(`a print at ${late.getHours()}:30 local was filed as ${key(late)} - `
                  + `that is the UTC date, so late-evening prints land on tomorrow `
                  + `and the bar count stops matching the table`);
const early = new Date(2026, 7, 29, 0, 15, 0);
if (key(early) !== "2026-08-29")
  throw new Error("an early-morning print was filed as " + key(early));
console.log("23:30 and 00:15 on the same local day share one key:", key(late));

// Somewhere in a 24-hour day the UTC date differs from the local one whenever
// the offset is not zero - before midnight if we are west of UTC, after it if
// east. Whichever side it falls, dayKey must give the LOCAL date there.
const day = new Date(2026, 7, 29);
let crossings = 0;
for (let h = 0; h < 24; h++) {
  const at = new Date(2026, 7, 29, h, 30);
  if (key(at) !== "2026-08-29")
    throw new Error(`${h}:30 local was filed as ${key(at)}`);
  if (at.toISOString().slice(0, 10) !== "2026-08-29") crossings++;
}
if (day.getTimezoneOffset() !== 0 && crossings === 0)
  throw new Error("this timezone has an offset but no hour of the day lands on "
                  + "another UTC date, so the test proves nothing here");
console.log(`all 24 hours file under the local date; ${crossings} of them are a `
            + `different date in UTC (offset ${-day.getTimezoneOffset()} min)`);

// --- zero padding, so the keys sort as text --------------------------------
if (key(new Date(2026, 0, 5)) !== "2026-01-05")
  throw new Error("single-digit months and days are not padded: " + key(new Date(2026, 0, 5)));
const keys = [new Date(2026, 0, 5), new Date(2026, 9, 2), new Date(2026, 1, 20)].map(key);
if (JSON.stringify([...keys].sort()) !== JSON.stringify(
    ["2026-01-05", "2026-02-20", "2026-10-02"]))
  throw new Error("the keys do not sort chronologically as text: " + keys);
console.log("keys are padded, so sorting them as text sorts them by date");

// --- the same key the server buckets by -------------------------------------
// The server uses strftime("%Y-%m-%d") on a local datetime; if either side
// changed shape the drill-down would open the wrong day.
const appSrc = fs.readFileSync(path.join(SRC_DIR, "app.py"), "utf8");
if (!/datetime\.fromtimestamp\(s\)\.strftime\("%Y-%m-%d"\)/.test(appSrc))
  throw new Error("the server no longer buckets by a local %Y-%m-%d, so the chart's "
                  + "key and the table's rows are keyed differently");
if (/utcfromtimestamp|datetime\.utcnow/.test(appSrc))
  throw new Error("the server has started using UTC somewhere in the bucketing");
console.log("the server buckets with the same local yyyy-mm-dd");

// --- a window draws every day in it, including the empty ones --------------
const build = lines.slice(lines.findIndex(l => l.includes("const cells = [];")),
                          lines.findIndex(l => l.includes("const active = cells.filter")))
  .join("\n");
const ctx2 = {statDays: [{day: key(new Date()), prints: 3}], dayMetric: "prints",
              dayDays: 30, dayKey: key};
vm.createContext(ctx2);
vm.runInContext("const have = new Map(statDays.map(d=> [d.day, d]));\n" + build, ctx2);
const cells = vm.runInContext("cells", ctx2);
if (cells.length !== 30)
  throw new Error(`a 30-day window drew ${cells.length} bars - quiet days were `
                  + `dropped, so a gap in printing is compressed out of the chart`);
if (cells.filter(c => c.v > 0).length !== 1)
  throw new Error("expected exactly one day with data, got "
                  + cells.filter(c => c.v > 0).length);
if (cells[cells.length - 1].k !== key(new Date()))
  throw new Error("the window does not end today: " + cells[cells.length - 1].k);
const ks = cells.map(c => c.k);
if (JSON.stringify(ks) !== JSON.stringify([...ks].sort()))
  throw new Error("the bars are not in date order");
console.log(`a 30-day window draws ${cells.length} bars, ending today, 1 with data`);

// --- and clicking one filters the table, clicking it again clears it -------
if (!/setDayFilter\(dayFilter === cells\[i\]\.k \? null : cells\[i\]\.k\)/.test(src))
  throw new Error("clicking the selected bar no longer clears the filter, so there "
                  + "is no way back to the whole history except the button");
if (!/function setDayFilter/.test(src)) throw new Error("setDayFilter is gone");
if (!/dayFilterClear/.test(src)) throw new Error("there is no 'show all' escape");
console.log("clicking a bar filters; clicking the same bar again clears it");
console.log("ok");
