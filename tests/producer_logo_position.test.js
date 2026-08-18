const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const producerPath = path.join(__dirname, "..", "ui", "producer.js");
const source = fs.readFileSync(producerPath, "utf8");

function extractFunction(name) {
  const start = source.indexOf(`function ${name}`);
  assert.notEqual(start, -1, `missing ${name}`);
  const bodyStart = source.indexOf("{", start);
  let depth = 0;
  for (let i = bodyStart; i < source.length; i++) {
    if (source[i] === "{") depth++;
    if (source[i] === "}") depth--;
    if (depth === 0) return source.slice(start, i + 1);
  }
  throw new Error(`unterminated ${name}`);
}

const sandbox = {};
vm.createContext(sandbox);
vm.runInContext(
  [
    extractFunction("clampNumber"),
    extractFunction("parseResolution"),
    extractFunction("roundLogoPercent"),
    extractFunction("calculateLogoPresetPosition"),
    "globalThis.parseResolution = parseResolution;",
    "globalThis.calculateLogoPresetPosition = calculateLogoPresetPosition;"
  ].join("\n"),
  sandbox
);

const pos = sandbox.calculateLogoPresetPosition;
const parseResolution = sandbox.parseResolution;
const output4k = { width: 3840, height: 2160 };
const logoWide = { width: 400, height: 199 };
const plain = value => JSON.parse(JSON.stringify(value));

assert.deepEqual(plain(pos("upper-left", 20, output4k, logoWide)), { startx: 0, starty: 20 });
assert.deepEqual(plain(pos("upper-right", 20, output4k, logoWide)), { startx: 78, starty: 20 });
assert.deepEqual(plain(pos("lower-left", 20, output4k, logoWide)), { startx: 0, starty: 100 });
assert.deepEqual(plain(pos("lower-right", 20, output4k, logoWide)), { startx: 78, starty: 100 });

assert.deepEqual(plain(pos("upper-left", 10, output4k, logoWide)), { startx: 0, starty: 10 });
assert.deepEqual(plain(pos("upper-right", 10, output4k, logoWide)), { startx: 89, starty: 10 });
assert.deepEqual(plain(pos("lower-left", 10, output4k, logoWide)), { startx: 0, starty: 100 });
assert.deepEqual(plain(pos("lower-right", 10, output4k, logoWide)), { startx: 89, starty: 100 });

assert.deepEqual(plain(pos("upper-right", 10, { width: 1920, height: 1080 }, { width: 100, height: 100 })), { startx: 95, starty: 10 });
assert.deepEqual(plain(pos("upper-right", 10, { width: 1280, height: 720 }, { width: 300, height: 100 })), { startx: 84, starty: 10 });
assert.deepEqual(plain(pos("lower-right", 25, { width: 1920, height: 1200 }, { width: 400, height: 200 })), { startx: 69, starty: 100 });

assert.deepEqual(plain(pos("upper-right", 10, null, logoWide, 88)), { startx: 88, starty: 10 });
assert.deepEqual(plain(pos("lower-right", 10, output4k, null, 88)), { startx: 88, starty: 90 });
assert.deepEqual(plain(pos("upper-right", 20, null, null, 78)), { startx: 78, starty: 20 });
assert.notEqual(plain(pos("upper-right", 20, null, null, 78)).startx, 0);

assert.deepEqual(plain(parseResolution("3840x2160")), output4k);
assert.deepEqual(plain(parseResolution("1920 x 1080")), { width: 1920, height: 1080 });
assert.equal(parseResolution("auto"), null);

console.log("producer logo position tests passed");
