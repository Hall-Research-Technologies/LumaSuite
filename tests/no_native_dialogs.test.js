// Enforcement test (Hall family spec Part 1.14 / 8.2): production UI must
// never call the native alert()/confirm()/prompt(). This greps every
// ui/*.html and ui/*.js file (except ui/dialogs.js, the one legitimate
// place window.alert is reassigned and lumaDialog.alert/confirm are
// defined) for a literal call to one of those three, and fails if any
// appears.
//
// window.lumaDialog.alert(...)/window.lumaDialog.confirm(...) and
// window.lumaConfirm(...) are the sanctioned replacements and are not
// flagged: the pattern below only matches alert(/confirm(/prompt( when
// NOT immediately preceded by "lumaDialog.".
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const UI_DIR = path.join(__dirname, "..", "ui");
const EXCLUDE_FILES = new Set(["dialogs.js"]);

const BAD_CALL = /(?<!lumaDialog\.)\b(alert|confirm|prompt)\(/g;

function listSourceFiles(dir) {
  return fs.readdirSync(dir)
    .filter(name => name.endsWith(".html") || name.endsWith(".js"))
    .filter(name => !EXCLUDE_FILES.has(name))
    .map(name => path.join(dir, name));
}

let violations = [];

for (const file of listSourceFiles(UI_DIR)) {
  const source = fs.readFileSync(file, "utf8");
  const lines = source.split("\n");
  lines.forEach((line, idx) => {
    const matches = line.match(BAD_CALL);
    if (matches) {
      violations.push(`${path.relative(UI_DIR, file)}:${idx + 1}: ${line.trim()}`);
    }
  });
}

assert.equal(
  violations.length,
  0,
  "Native alert()/confirm()/prompt() found in production UI (use window.toast(...) or " +
  "window.lumaConfirm(...)/window.lumaDialog.confirm(...) instead):\n" + violations.join("\n")
);

console.log("no native dialogs test passed (" + listSourceFiles(UI_DIR).length + " files scanned)");
