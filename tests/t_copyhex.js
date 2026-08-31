// Copying a colour code has to work on the NAS, which is served over plain
// http:// - not a secure context, so navigator.clipboard is simply absent
// there. The fallbacks are what actually carry it, and a failure must still
// leave the user with something they can copy by hand.
// The app source, relative to this file rather than an absolute path.
const _path = require("path");
const SRC_DIR = process.env.BAMBU_SRC || _path.join(__dirname, "..");
const _src = n => _path.join(SRC_DIR, n);
const fs = require("fs"), vm = require("vm");
const src = fs.readFileSync(_src("dashboard.html"), "utf8")
  .match(/<script>([\s\S]*?)<\/script>/)[1];
const lines = src.split("\n");
function grab(sig){
  const s = lines.findIndex(l => l.startsWith(sig));
  if (s < 0) throw new Error("not found: " + sig);
  let e = s; while (lines[e] !== "}") e++;
  return lines.slice(s, e + 1).join("\n");
}

function env({secure, clipboardWorks, execWorks}){
  const log = {wrote: null, exec: [], prompted: [], appended: 0, removed: 0};
  const ta = {value:"", style:{}, setAttribute(){}, select(){ log.selected = true; },
              remove(){ log.removed++; }};
  const ctx = {
    window: {isSecureContext: secure},
    document: {
      createElement: () => ta,
      body: {appendChild(){ log.appended++; }},
      execCommand: (c) => { log.exec.push(c);
        if (execWorks === "throw") throw new Error("execCommand blocked");
        return !!execWorks; },
    },
    navigator: secure && clipboardWorks !== undefined ? {clipboard: {
      writeText: async (s) => { if (!clipboardWorks) throw new Error("Document is not focused");
                                log.wrote = s; } }} : {},
    prompt: (msg, val) => { log.prompted.push(val); return val; },
    t: s => s,
    log,
  };
  ctx.window.isSecureContext = secure;
  vm.createContext(ctx);
  vm.runInContext("const isSecureContext = window.isSecureContext;\n"
                  + grab("async function copyText("), ctx);
  return {ctx, log, ta};
}

async function run(){
  // 1. a secure context with a working clipboard: the modern path, nothing else touched
  let {ctx, log} = env({secure: true, clipboardWorks: true});
  let ok = await vm.runInContext('copyText("#7C4B00")', ctx);
  console.log(`secure + clipboard   -> ${ok}  wrote=${log.wrote}  exec=${log.exec.length}  prompt=${log.prompted.length}`);
  if (!ok || log.wrote !== "#7C4B00") throw new Error("the clipboard path did not copy");
  if (log.exec.length) throw new Error("it fell through to execCommand needlessly");

  // 2. plain http:// - the NAS. navigator.clipboard is absent, execCommand carries it
  ({ctx, log} = env({secure: false, execWorks: true}));
  ok = await vm.runInContext('copyText("#7C4B00")', ctx);
  console.log(`http:// + execCommand-> ${ok}  exec=${JSON.stringify(log.exec)}  `
            + `textarea appended=${log.appended} removed=${log.removed}  prompt=${log.prompted.length}`);
  if (!ok) throw new Error("the http:// fallback did not copy - this is the NAS case");
  if (!log.exec.includes("copy")) throw new Error("execCommand('copy') was never called");
  if (log.appended !== log.removed) throw new Error("the scratch textarea was left in the document");
  if (log.prompted.length) throw new Error("it prompted even though the copy worked");

  // 3. a secure context whose clipboard rejects (headless, unfocused document):
  //    it must fall through rather than give up
  ({ctx, log} = env({secure: true, clipboardWorks: false, execWorks: true}));
  ok = await vm.runInContext('copyText("#7C4B00")', ctx);
  console.log(`clipboard rejects    -> ${ok}  exec=${JSON.stringify(log.exec)}`);
  if (!ok) throw new Error("a rejected clipboard was not retried with execCommand");

  // 4. everything refused: the value still has to reach the user somehow
  ({ctx, log} = env({secure: false, execWorks: false}));
  ok = await vm.runInContext('copyText("#7C4B00")', ctx);
  console.log(`nothing works        -> ${ok}  prompted=${JSON.stringify(log.prompted)}`);
  if (ok) throw new Error("a failed copy reported success");
  if (log.prompted[0] !== "#7C4B00")
    throw new Error("the user was left with no way to get the code");

  // 5. execCommand throwing must be caught, not escape the handler
  ({ctx, log} = env({secure: false, execWorks: "throw"}));
  ok = await vm.runInContext('copyText("#7C4B00")', ctx);
  console.log(`execCommand throws   -> ${ok}  prompted=${JSON.stringify(log.prompted)}`);
  if (ok || log.prompted[0] !== "#7C4B00") throw new Error("a throwing execCommand was not handled");
  if (log.removed !== 1) throw new Error("the scratch textarea leaked when execCommand threw");
}

// The chip shows whichever form has to be typed somewhere: Windows colour
// dialog has R/G/B boxes and no hex field, Bambu Studio takes a hex.
function fnameIn(fmt, f){
  return new Function("esc", "t", "filColFmt",
    "return " + grab("function filName("))(s => s, s => s, fmt)(f);
}
const SUNLU = {product:"PLA Meta", color_name:"Coffee Brown", color:"7c4b00"};
for (const [fmt, want] of [["hex", "#7C4B00"], ["rgb", "124, 75, 0"]]) {
  const n = fnameIn(fmt, SUNLU);
  console.log(`${fmt} -> chip "${n.shown}"   tooltip "${n.spelt}"`);
  if (n.shown !== want) throw new Error(`${fmt}: expected ${want}, got ${n.shown}`);
  if (n.hex !== "#7C4B00") throw new Error("the hex was lost in " + fmt + " mode");
}
// three bare numbers are no help when they go into three separate boxes
const spelt = fnameIn("rgb", {product:"x", color:"f73737"}).spelt;
console.log(`spelt out            "${spelt}"`);
if (spelt !== "Red 247 · Green 55 · Blue 55")
  throw new Error("the channels are not spelt out in R,G,B order: " + spelt);
const chan = fnameIn("rgb", {product:"x", color:"f73737"}).rgb;
if (chan.join() !== "247,55,55") throw new Error("wrong channel values: " + chan);

// a colour the row has no name for already shows AS the hex, so the chip would
// repeat it - but in rgb mode it says something new and should appear
if (!fnameIn("hex", {product:"PLA", color:"7c4b00"}).dup)
  throw new Error("an unnamed colour would show its hex twice");
if (fnameIn("rgb", {product:"PLA", color:"7c4b00"}).dup)
  throw new Error("rgb was suppressed even though the row only shows the hex");
console.log("no duplicate in hex mode, still shown in rgb mode");

for (const fmt of ["hex", "rgb"]) {
  if (fnameIn(fmt, {product:"PLA", color:null}).shown !== null)
    throw new Error(fmt + ": a colourless row got a chip");
}
console.log("a filament with no known colour gets no chip");

run().then(()=> console.log("\nok"), e => { console.error(e.message); process.exit(1); });
