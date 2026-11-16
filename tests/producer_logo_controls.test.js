// LUMA-MA-004: the Production Studio logo controls (position, height, transparency)
// must reach the unit. Device facts encoded here were measured on hardware:
// OsdLogo.Set refuses a partial displaylogo, backtransparency outside 1..255,
// non-integers, and starty < logohight; it applies immediately and answers with
// the state it now holds. Nothing here touches a device.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "..", "ui", "producer.js"), "utf8");
const code = source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");

function extractFunction(src, name) {
  const start = src.indexOf(`function ${name}`);
  assert.notEqual(start, -1, `missing ${name}`);
  const bodyStart = src.indexOf("{", start);
  let depth = 0;
  for (let i = bodyStart; i < src.length; i++) {
    if (src[i] === "{") depth++;
    if (src[i] === "}") depth--;
    if (depth === 0) return src.slice(start, i + 1);
  }
  throw new Error(`unterminated ${name}`);
}
const plain = v => JSON.parse(JSON.stringify(v));

// 1. The payload sent to the unit always satisfies the unit's own limits.
const sandbox = {};
vm.createContext(sandbox);
vm.runInContext(
  [extractFunction(code, "clampNumber"), extractFunction(code, "buildLogoDevicePayload"),
   "globalThis.build = buildLogoDevicePayload;"].join("\n"), sandbox);
const build = (...a) => plain(sandbox.build(...a));

assert.deepEqual(build(true, 128, 12, { startx: 74, starty: 12 }),
  { osdlogoenabled: true, displaylogo: { backtransparency: 128, startpostion: { startx: 74, starty: 12 }, logohight: 12 } });
assert.equal(build(true, 0, 10, { startx: 0, starty: 10 }).displaylogo.backtransparency, 1);
assert.equal(build(true, 999, 10, { startx: 0, starty: 10 }).displaylogo.backtransparency, 255);
assert.equal(build(true, 100.6, 10, { startx: 0, starty: 10 }).displaylogo.backtransparency, 101);
assert.equal(build(true, 255, 5.4, { startx: 0, starty: 10 }).displaylogo.logohight, 5);
assert.equal(build(true, 255, 100, { startx: 0, starty: 100 }).displaylogo.logohight, 100);
// starty is the bottom edge: it may never be above the logo's height.
assert.equal(build(true, 255, 20, { startx: 0, starty: 5 }).displaylogo.startpostion.starty, 20);
assert.equal(build(true, 255, 10, { startx: -2, starty: 500 }).displaylogo.startpostion.starty, 100);
assert.equal(build(true, 255, 10, { startx: -2, starty: 10 }).displaylogo.startpostion.startx, 0);
// The whole displaylogo object is always present (the unit refuses a partial one).
assert.deepEqual(Object.keys(build(false, 255, 10, { startx: 0, starty: 10 }).displaylogo).sort(),
  ["backtransparency", "logohight", "startpostion"]);

// 2. Editing a control applies it. Previously these handlers only recomputed the
//    coordinate inputs, so nothing was ever sent to the unit.
const wire = extractFunction(code, "wireLogoHandlers");
for (const [label, re] of [
  ["transparency", /transInput\.onchange\s*=\s*\(\)\s*=>\s*\{[^}]*applyLogoSettings\(\)/],
  ["height", /heightInput\.onchange\s*=\s*\(\)\s*=>\s*\{[^}]*applyLogoSettings\(\)/],
  ["position", /posSelect\.onchange\s*=\s*async\s*\(\)\s*=>\s*\{[\s\S]*?await applyLogoSettings\(\)/],
  ["horizontal", /horizInput\.onchange\s*=\s*\(\)\s*=>\s*\{[^}]*applyLogoSettings\(\)/],
  ["vertical", /vertInput\.onchange\s*=\s*\(\)\s*=>\s*\{[^}]*applyLogoSettings\(\)/],
]) assert.match(wire, re, `${label} control does not apply its change`);

// 3. The apply path sends the device-shaped payload and reports a refusal.
const apply = wire.slice(wire.indexOf("async function applyLogoSettings"), wire.indexOf("const transInput"));
assert.match(apply, /rpc\("OsdLogo\.Set",\s*payload\)/);
assert.match(apply, /window\.toast/);
assert.match(wire.slice(wire.indexOf("function buildLogoSettingsPayload"), wire.indexOf("async function applyLogoSettings")), /buildLogoDevicePayload\(/);

console.log("producer logo controls tests passed");
