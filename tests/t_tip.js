// Placing the chart tooltip, against the real source.
//
// A tooltip that leaves the window is useless, and near the right edge of a
// chart is exactly where you point. So it flips sides rather than being clamped
// flat against the edge, flips below when there is no room above, and is
// clamped only as a last resort.
//
// The subtle part is `tipCal`. The tooltip is positioned in CSS pixels but the
// mouse event and getBoundingClientRect speak layout pixels, and the two differ
// whenever an ancestor is scaled or the page is zoomed. Doing the arithmetic in
// one space and writing the answer in the other puts the tooltip somewhere near
// the pointer on a plain page and nowhere near it on a zoomed one.
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

const VW = 1200, VH = 800, W = 220, H = 90;

function place(clientX, clientY, {sx = 1, sy = 1, ox = 0, oy = 0} = {}) {
  const tip = {
    style: {}, getBoundingClientRect: () => ({width: W, height: H}),
  };
  const ctx = {
    tipCal: {sx, sy, ox, oy},
    calibrateTip: () => ({sx, sy, ox, oy}),
    document: {documentElement: {clientWidth: VW, clientHeight: VH}},
  };
  vm.createContext(ctx);
  vm.runInContext(grab("function placeTip("), ctx);
  vm.runInContext("placeTip(TIP, EV)", Object.assign(ctx, {
    TIP: tip, EV: {clientX, clientY}}));
  // back into the space the assertions think in
  return {x: parseFloat(tip.style.left) * sx + ox,
          y: parseFloat(tip.style.top) * sy + oy,
          opacity: tip.style.opacity};
}

// --- in open space it sits above and to the right of the pointer -----------
let p = place(400, 400);
if (!(p.x > 400)) throw new Error("it is not to the right of the pointer: " + p.x);
if (!(p.y + H < 400)) throw new Error("it is not above the pointer: " + p.y);
if (p.opacity !== 1) throw new Error("it was placed but left invisible");
console.log(`pointer at 400,400 -> tooltip at ${p.x},${p.y} (above right)`);

// --- near the right edge it flips, rather than hanging off ----------------
p = place(VW - 30, 400);
if (p.x + W > VW) throw new Error(`it runs off the right edge: ${p.x} + ${W} > ${VW}`);
if (!(p.x + W < VW - 30)) throw new Error("it did not flip to the left of the pointer, "
                                          + "it was only pushed against the edge: " + p.x);
console.log(`pointer near the right edge -> flipped left, to ${p.x}`);

// --- near the top it flips below -------------------------------------------
p = place(400, 20);
if (p.y < 0) throw new Error("it runs off the top: " + p.y);
if (!(p.y > 20)) throw new Error("it did not flip below the pointer: " + p.y);
console.log(`pointer near the top -> flipped below, to ${p.y}`);

// --- a corner has to satisfy both at once ----------------------------------
p = place(VW - 10, 10);
if (p.x < 0 || p.y < 0 || p.x + W > VW || p.y + H > VH)
  throw new Error(`in the top-right corner it left the window: ${p.x},${p.y}`);
console.log(`top-right corner -> stays inside, at ${p.x},${p.y}`);

// --- and every corner, for good measure ------------------------------------
for (const [x, y] of [[0, 0], [VW, 0], [0, VH], [VW, VH], [1, VH - 1], [VW - 1, 1]]) {
  const q = place(x, y);
  if (q.x < 0 || q.y < 0 || q.x + W > VW || q.y + H > VH)
    throw new Error(`pointer at ${x},${y} put the tooltip at ${q.x},${q.y}, `
                    + `partly outside the ${VW}x${VH} window`);
}
console.log("no pointer position anywhere on the edge puts it outside the window");

// --- the scaled case, which is what tipCal is for --------------------------
// An ancestor scaled by 2: the answer is written in CSS units, so writing the
// layout-space number straight out would land at twice the distance.
const scaled = place(400, 400, {sx: 2, sy: 2, ox: 0, oy: 0});
const plain = place(400, 400);
if (Math.abs(scaled.x - plain.x) > 1 || Math.abs(scaled.y - plain.y) > 1)
  throw new Error(`under a 2x scale the tooltip landed at ${scaled.x},${scaled.y} `
                  + `instead of ${plain.x},${plain.y} - the arithmetic is being done `
                  + `in one space and written in the other`);
const offset = place(400, 400, {sx: 1, sy: 1, ox: 50, oy: 30});
if (Math.abs(offset.x - plain.x) > 1 || Math.abs(offset.y - plain.y) > 1)
  throw new Error("an offset ancestor shifted the tooltip: " + JSON.stringify(offset));
console.log("under a 2x scale, and under an offset, it lands in the same place");

// --- the calibration is measured once, not per mousemove -------------------
// placeTip runs on every mousemove; measuring the page each time forces a
// layout on a pointer move across a chart.
if (!/if\(!tipCal\) tipCal = calibrateTip\(tip\)/.test(src))
  throw new Error("the calibration is no longer cached, so every mousemove "
                  + "measures the page again");
console.log("the calibration is measured once and cached");
console.log("ok");
