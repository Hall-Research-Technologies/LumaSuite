// Static regression guards for the shared application shell (header/nav/
// gear/logo) and Hallway branding, added during the shell-consistency +
// operator-UX top-bar reorganization pass. These are plain file-content
// checks (no headless browser here -- the actual pixel geometry was
// measured live via browser automation, see PROJECT.md/the completion
// report) that guard against the specific regressions found and fixed in
// that pass from silently coming back.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const uiDir = path.join(__dirname, "..", "ui");
const read = name => fs.readFileSync(path.join(uiDir, name), "utf8");

const familyCss = read("family.css");
const indexHtml = read("index.html");
const matrixHtml = read("matrix.html");
const producerHtml = read("producer.html");
const wallHtml = read("wall.html");
const manualHtml = read("manual.html");

// ---- Hallway branding: both assets present, real CSS-driven swap -------

assert.ok(fs.existsSync(path.join(uiDir, "companylogo.png")), "ui/companylogo.png (dark) must exist");
assert.ok(fs.existsSync(path.join(uiDir, "companylogo-light.png")), "ui/companylogo-light.png must exist -- copied from OmniSuite, not referenced at its original path");

// The swap must be theme-only (content: url(), OmniSuite's own mechanism)
// -- never gated by density (Classic/Compact) or a colour preset.
const logoRule = familyCss.match(/html\.light #company_logo,\s*body\.light #company_logo\s*\{[^}]*\}/);
assert.ok(logoRule, "family.css must define the light-mode #company_logo swap rule");
assert.match(logoRule[0], /content:\s*url\(["']\/ui\/companylogo-light\.png["']\)/,
  "the swap must use content: url(), matching OmniSuite's own mechanism exactly");
assert.ok(!/data-density/.test(logoRule[0]) && !/preset/i.test(logoRule[0]),
  "logo selection must depend only on light/dark, never density or preset");

// No page may still carry the old dark-backdrop-chip workaround this
// replaced (it would visually fight with the new light asset).
for (const [name, html] of [["index.html", indexHtml], ["wall.html", wallHtml]]) {
  assert.ok(!/background:\s*#1d232a[^}]*padding:\s*4px 8px/.test(html),
    `${name} must not carry a page-local dark-chip logo workaround`);
}

// ---- Shared nav tabs: single source of truth in family.css -------------

assert.match(familyCss, /\.nav-tab\.active\s*\{/, "family.css must own .nav-tab.active");
for (const [name, html] of [
  ["index.html", indexHtml], ["matrix.html", matrixHtml],
  ["producer.html", producerHtml], ["wall.html", wallHtml], ["manual.html", manualHtml]
]) {
  assert.ok(!/\.nav-tab\.active\s*\{/.test(html),
    `${name} must not redeclare .nav-tab.active locally -- family.css is the single source of truth`);
}

// ---- Device Manager gear: header-row content, not absolutely positioned
// inside the Scan box (the original defect this pass fixed) ------------

assert.ok(!/id="header_gear"[^>]*style="position:absolute/.test(indexHtml),
  "Device Manager's Settings gear must not be absolutely positioned inside page content");
assert.match(indexHtml, /class="nav-tabs"[\s\S]{0,2000}id="header_gear"/,
  "Device Manager's Settings gear must sit in the header row beside the nav tabs, like every other page");

// ---- Production Studio header geometry matched to the other three pages
// (header lives as a flush sibling before .wrap, not inside it -- .wrap's
// own max-width centering previously centered the header itself at wide
// viewports; see PROJECT.md sec. 29 for the measured before/after).

assert.ok(!/<div class="wrap">\s*<div class="row header"/.test(producerHtml),
  "Production Studio's header must not be nested inside .wrap (that re-centers it at wide viewports)");
assert.match(producerHtml, /<div class="row header"[^>]*margin:\s*16px 16px 12px/,
  "Production Studio's header must carry the same flush 16px/16px/12px margins as the other pages' body margin + .header margin-bottom");
assert.match(producerHtml, /\.header\s*\{[^}]*margin-bottom:\s*12px/,
  "Production Studio's .header margin-bottom must match the other pages' shared 12px");

console.log("shell/family consistency tests passed");
