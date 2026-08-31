// The Settings form is generated from the server's schema. Two pieces carry the
// logic: setField() turns one spec into a control, and markMatrix() reads a
// price table back out. Both are checked here against the real source.
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

const ctx = {
  // the page's OWN esc, not a stand-in: escaping is the thing under test here
  t: s => s,
  setEdits: new Map(),
  document: {querySelectorAll: () => []},
  $: () => ({textContent: "", disabled: false}),
};
vm.createContext(ctx);
vm.runInContext([grab("function esc("), grab("function setField("), grab("function markMatrix(")].join(";\n"), ctx);
const field = spec => vm.runInContext("setField(" + JSON.stringify(spec) + ")", ctx);

// --- one control per kind ---
const cases = [
  ["bool",   {path:"controls.allow_gcode", label:"G-code", kind:"bool", value:true},
             /type="checkbox"[^>]*checked/],
  ["select", {path:"filament.store_region", label:"Region", kind:"select",
              value:"eu", options:["eu","us"]}, /<option value="eu" selected>\s*eu<\/option>/],
  ["int",    {path:"filament.low_pct", label:"Low %", kind:"int", value:15},
             /value="15"/],
  ["matrix", {path:"filament.bambu", label:"Bambu", kind:"matrix",
              value:{PLA:24.99, PETG:27.99}}, /mtx-row[\s\S]*PETG/],
  // a pairs table is the same control with text on both sides: a numeric
  // keypad on a colour name would be wrong, and so would parsing it as a price
  ["pairs",  {path:"filament.color_names", label:"Names", kind:"pairs",
              value:{"7C4B00":"Coffee Brown"}}, /mtx-row[\s\S]*Coffee Brown/],
];
for (const [name, spec, want] of cases) {
  const html = field(spec);
  console.log(`${name.padEnd(7)} -> ${html.replace(/\s+/g," ").slice(0,74)}`);
  if (!want.test(html)) throw new Error(`${name}: unexpected markup`);
}

// --- a secret is never pre-filled, and says whether one is stored ---
const setSecret = field({path:"access_code", label:"Code", kind:"secret",
                         value:null, is_set:true});
const unsetSecret = field({path:"cloud.token", label:"Token", kind:"secret",
                           value:null, is_set:false});
console.log("\nsecret (set)   ->", setSecret.replace(/\s+/g," ").slice(0,88));
if (!/type="password"/.test(setSecret)) throw new Error("a secret is not a password field");
if (/value="[^"]+"/.test(setSecret)) throw new Error("a secret was pre-filled into the form");
if (!/set — type to replace/.test(setSecret)) throw new Error("it does not say one is stored");
if (!/not set/.test(unsetSecret)) throw new Error("an unset secret does not say so");

// a value containing a quote must not be able to close the attribute and start
// an event handler - the values come from a config file a person edits
const nasty = field({path:"cost.currency", label:"Cur", kind:"text",
                     value:'" onfocus="alert(1)'});
if (/value="[^"]*"\s*onfocus/.test(nasty))
  throw new Error("a quoted value broke out of its attribute: " + nasty);
if (!/&quot;/.test(nasty)) throw new Error("the quote was not escaped at all");
console.log("a quote in a value is escaped, not able to close the attribute");

// --- reading a price table back ---
const rows = [["PLA","24.99"], ["PETG","27,99"], ["","19"], ["  ",""]];
const mtx = {
  dataset: {path: "filament.bambu"},
  querySelectorAll: () => rows.map(([k, v]) => ({
    querySelector: sel => ({value: sel === ".mtx-k" ? k : v}),
  })),
};
ctx.setEdits.clear();
vm.runInContext("markMatrix(MTX)", Object.assign(ctx, {MTX: mtx}));
const got = ctx.setEdits.get("filament.bambu");
console.log("\nmatrix read back ->", JSON.stringify(got));
if (got.PLA !== "24.99" || got.PETG !== "27,99")
  throw new Error("a row was lost or mangled: " + JSON.stringify(got));
if ("" in got || "  " in got) throw new Error("a nameless row was kept");
if (Object.keys(got).length !== 2) throw new Error("unexpected rows: " + Object.keys(got));
console.log("nameless rows dropped; the comma decimal is left for the server to parse");

// both kinds of table must be readable by the same code, or one of them silently
// saves nothing
const pairsHtml = field({path:"filament.color_names", label:"Names", kind:"pairs",
                         value:{"7C4B00":"Coffee Brown"}});
if (/class="mtx-v num"/.test(pairsHtml))
  throw new Error("a text table asks for a decimal keypad");
if (!/class="mtx-v"/.test(pairsHtml))
  throw new Error("a pairs row has no .mtx-v, so markMatrix would read nothing back");
if (!/class="mtx-v num"/.test(field({path:"filament.bambu", label:"B", kind:"matrix",
                                     value:{PLA:1}})))
  throw new Error("a price table lost its numeric input");
console.log("pairs and matrix share one reader, and only prices ask for a number pad");

// --- what "reset to X" offers has to be readable, whatever the default is ---
vm.runInContext(grab("function showDefault("), ctx);
const shown = spec => vm.runInContext("showDefault(" + JSON.stringify(spec) + ")", ctx);
const defaults = [
  [{default: 15}, "15"], [{default: true}, "on"], [{default: false}, "off"],
  [{default: ""}, "empty"], [{default: {}}, "empty"],
  [{default: {PLA: 1}}, "1 row"], [{default: {PLA: 1, PETG: 2}}, "2 rows"],
];
for (const [spec, want] of defaults) {
  const got = shown(spec);
  if (got !== want) throw new Error(`showDefault(${JSON.stringify(spec.default)}) = ${got}, expected ${want}`);
}
console.log("reset offers a readable default for every kind, tables included");

// --- a select may show something other than its value ----------------------
// "tapo" and "mqtt" are fine as stored values and poor as things to read, so a
// setting can carry labels. The VALUE must still be what is submitted, or the
// page would save the label.
const labelled = field({path:"power.provider", label:"Meter", kind:"select",
                        value:"mqtt", options:["tapo","mqtt"],
                        option_labels:{tapo:"Tapo (TP-Link P110/P115)",
                                       mqtt:"MQTT (Zigbee2MQTT, Shelly, Tasmota)"}});
if (!/<option value="tapo">Tapo \(TP-Link P110\/P115\)<\/option>/.test(labelled))
  throw new Error("the option label was not used: " + labelled);
if (!/<option value="mqtt" selected>MQTT/.test(labelled))
  throw new Error("the selected option lost its label or its value: " + labelled);
// and a select without labels still works
const plain = field({path:"filament.store_region", label:"Region", kind:"select",
                     value:"us", options:["eu","us"]});
if (!/<option value="us" selected>\s*us<\/option>/.test(plain))
  throw new Error("a select with no labels broke: " + plain);
console.log("a select can show a label while still submitting its value");
console.log("ok");
