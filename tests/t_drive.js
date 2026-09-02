// The USB drive view, against the real source.
//
// The view carries two numbers from two different places: how full the drive is
// (from the printer) and what is visible on it (from FTP). On the real drive
// those differ by 6 GB, because the FTP view does not show everything the drive
// holds. Presenting either as "the" answer, or silently subtracting one from the
// other to invent a category, would be a lie with a progress bar on it.
const _path = require("path");
const SRC_DIR = process.env.BAMBU_SRC || _path.join(__dirname, "..");
const fs = require("fs"), vm = require("vm");
const src = fs.readFileSync(_path.join(SRC_DIR, "dashboard.html"), "utf8")
  .match(/<script>([\s\S]*?)<\/script>/)[1];
const lines = src.split("\n");
function grab(sig) {
  const s = lines.findIndex(l => l.startsWith(sig));
  if (s < 0) throw new Error("not found: " + sig);
  let e = s; while (lines[e] !== "}") e++;
  return lines.slice(s, e + 1).join("\n");
}

const el = { driveBody: { innerHTML: "", dataset: {} }, driveMeta: { textContent: "" } };
const ctx = {
  t: s => s,
  esc: s => String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;"),
  $: id => el[id] || (el[id] = { innerHTML: "", dataset: {}, textContent: "" }),
};
vm.createContext(ctx);
for (const fn of ["function kb(", "function bytes(", "function renderDrive("]) {
  vm.runInContext(grab(fn), ctx);
}
const render = d => {
  el.driveBody.innerHTML = ""; el.driveMeta.textContent = "";
  vm.runInContext("renderDrive(" + JSON.stringify(d) + ")", ctx);
  return el.driveBody.innerHTML;
};
const kb = v => vm.runInContext(`kb(${JSON.stringify(v)})`, ctx);
const bytes = v => vm.runInContext(`bytes(${JSON.stringify(v)})`, ctx);

// --- sizes read as sizes ----------------------------------------------------
for (const [v, want] of [[29897088, "28.5 GB"], [23592640, "22.5 GB"],
                         [186081, "181.7 MB"], [120, "120 KB"]]) {
  if (kb(v) !== want) throw new Error(`kb(${v}) is ${kb(v)}, expected ${want}`);
}
for (const [v, want] of [[3832815, "3.7 MB"], [120, "120 B"], [90000000, "85.8 MB"],
                         [6304448 * 1024, "6.0 GB"]]) {
  if (bytes(v) !== want) throw new Error(`bytes(${v}) is ${bytes(v)}, expected ${want}`);
}
if (kb(null) !== "—" || bytes(null) !== "—") throw new Error("a missing size showed as a number");
// the same quantity has to print the same way whichever unit it arrives in
if (kb(6304448) !== bytes(6304448 * 1024)) {
  throw new Error(`the same 6 GB prints as ${kb(6304448)} on the capacity bar and `
    + `${bytes(6304448 * 1024)} in the note under it`);
}
console.log("sizes read as sizes, in one precision, and a missing one as a dash");

// --- the real numbers, which disagree ---------------------------------------
const REAL = {
  storage: { present: true,
             external: { total_kb: 29897088, free_kb: 23592640, used_kb: 6304448 } },
  files: [
    { name: "Herbstliebe_Oberteil.gcode.3mf", dir: "/", path: "/x", size: 3832815,
      when: "Jul 23 04:13", sliced: true, job_id: "j1", print: "Oberteile Herz" },
    { name: "video_001.mp4", dir: "/timelapse", path: "/timelapse/video_001.mp4",
      size: 90000000, when: "Jul 23 04:13", sliced: false },
  ],
  listed_bytes: 93832815, sliced_bytes: 3832815, truncated: false, error: null,
};
let h = render(REAL);
for (const need of ["6.0 GB", "22.5 GB", "28.5 GB", "as reported by the printer"]) {
  if (!h.includes(need)) throw new Error(`the capacity line is missing ${need}: ` + h.slice(0, 400));
}
if (!h.includes("Oberteile Herz")) throw new Error("a file is not tied to its print");
if (!h.includes("85.8 MB")) throw new Error("the timelapse size is not shown");
// the gap is stated, not explained away as a category nobody measured
if (!/does not share/.test(h)) {
  throw new Error("the printer says 6 GB is used and only 89 MB is visible, and "
    + "the page says nothing about the difference");
}
if (/other files|Other/.test(h)) {
  throw new Error("the page invented a category for space it cannot see");
}
console.log("both figures shown, each labelled with where it came from");

// --- and no gap note when the two roughly agree -----------------------------
h = render({ ...REAL, listed_bytes: 6304448 * 1024 - 1000 });
if (/does not share/.test(h)) throw new Error("a note about a gap that is not there");
console.log("...and no note when they agree");

// --- the halves fail apart --------------------------------------------------
h = render({ storage: REAL.storage, files: [], listed_bytes: 0,
             error: "cannot reach the printer's file store" });
if (!h.includes("28.5 GB")) {
  throw new Error("the listing failed and took the capacity with it - they come "
    + "from different places and one must survive the other");
}
if (!/cannot reach/.test(h)) throw new Error("it does not say why the list is empty");

h = render({ storage: { present: false, external: null }, files: [], listed_bytes: 0 });
if (/NaN|undefined|—\s*used/.test(h)) throw new Error("a missing drive rendered as junk: " + h);
if (!/no drive/i.test(h)) throw new Error("with no drive in, the page does not say so");
console.log("no drive, or no listing: each is said plainly and neither breaks the other");

// --- a shortened list says so -----------------------------------------------
h = render({ ...REAL, truncated: true });
if (!/shortened/.test(h)) {
  throw new Error("a list that was cut short is presented as the whole drive");
}
console.log("a shortened list says that it is");

// --- German ------------------------------------------------------------------
const de = src.slice(src.indexOf("const DE = {"), src.indexOf("\n};", src.indexOf("const DE = {")));
for (const s of ["USB drive", "used", "free", "total", "as reported by the printer",
                 "no drive in the printer", "File", "Folder", "Size"]) {
  if (!de.includes('"' + s + '"')) throw new Error(`no German for ${JSON.stringify(s)}`);
}
console.log("the drive view has German");
console.log("ok");
