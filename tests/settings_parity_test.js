// Settings: the application's version status (green when current, red when an update is available,
// neutral when the check could not say) and the Settings sections that exist in both products.
// renderUpdateState is lifted out of the Settings module and run, so the classes are a measurement.
'use strict';
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const {Node} = require('./_dom.js');

const read = f => fs.readFileSync(path.join(__dirname, '..', f), 'utf8');
const strip = s => s.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
const settingsSource = read('ui/settings.js');

function lift() {
  const a = settingsSource.indexOf('  function renderUpdateState(data) {');
  const b = settingsSource.indexOf('  function runUpdateCheck');
  assert.ok(a > 0 && b > a, 'renderUpdateState found');
  const nodes = {cfg_update_status: new Node('div'), cfg_asset_link: new Node('a'), cfg_release_link: new Node('a')};
  const alerts = [];
  const ctx = {el: id => nodes[id] || null, setGearAlert: on => alerts.push(on),
               localStorage: {getItem: () => null, setItem() {}}};
  vm.createContext(ctx);
  vm.runInContext(settingsSource.slice(a, b) + '\nthis.render = renderUpdateState;', ctx);
  return {nodes, alerts, render: ctx.render, status: () => nodes.cfg_update_status};
}

// ------------------------------------------------------------------ the status and its colour
let t = lift();
t.render({ok: true, state: 'current', installed_version: '1.1.0'});
assert.strictEqual(t.status().className, 'update-status ok', 'current is the green state');
assert.ok(/1\.1\.0 is current/.test(t.status().textContent));

t = lift();
t.render({ok: true, state: 'update_available', latest_version: '1.2.0', installed_version: '1.1.0', asset_url: 'https://example.invalid/x'});
assert.strictEqual(t.status().className, 'update-status update-available', 'an available update is the red state');
assert.ok(/UPDATE AVAILABLE/.test(t.status().textContent) && /1\.2\.0/.test(t.status().textContent));
assert.strictEqual(t.nodes.cfg_asset_link.hidden, false);

// An update with no download for this computer says so, stays the red state and never offers a link.
t = lift();
const sentence = 'There is no supported update package for this computer. LumaSuite releases are built for Windows (x86-64) and macOS on Apple Silicon only.';
t.render({ok: true, state: 'update_available', latest_version: '1.1.1', installed_version: '1.1.0', asset_url: null,
          html_url: 'https://example.invalid/r', package_message: sentence});
assert.strictEqual(t.status().className, 'update-status update-available');
assert.ok(/UPDATE AVAILABLE/.test(t.status().textContent) && /no supported update package/.test(t.status().textContent), t.status().textContent);
assert.strictEqual(t.nodes.cfg_asset_link.hidden, true, 'no download link without a package');
assert.strictEqual(t.nodes.cfg_release_link.href, 'https://example.invalid/r', 'the release page stays reachable');

// A package being offered carries no apology, and a current answer never carries the sentence.
t = lift();
t.render({ok: true, state: 'update_available', latest_version: '1.1.1', asset_url: 'https://example.invalid/a.zip', package_message: null});
assert.ok(!/no supported/.test(t.status().textContent));
assert.strictEqual(t.nodes.cfg_asset_link.hidden, false);
t = lift();
t.render({ok: true, state: 'current', installed_version: '1.1.1', package_message: sentence});
assert.ok(!/no supported/.test(t.status().textContent), 'current is never decorated');

// Anything the check could not establish is neutral: never green on a failed request, never red either.
for (const answer of [null, {ok: false}, {ok: true, state: 'unable_to_check'}, {ok: true, state: 'something_new'}, {ok: true}]) {
  t = lift();
  t.render(answer);
  assert.strictEqual(t.status().className, 'update-status muted', 'neutral for ' + JSON.stringify(answer));
  assert.ok(/Unable to check/.test(t.status().textContent));
  assert.ok(!/ok|update-available/.test(t.status().className.replace('update-status', '')), 'never a colour');
}

// A state change repaints: the colour of an earlier answer cannot survive into the next.
t = lift();
t.render({ok: true, state: 'update_available', latest_version: '1.2.0'});
t.render({ok: true, state: 'current', installed_version: '1.2.0'});
assert.strictEqual(t.status().className, 'update-status ok');
t.render(null);
assert.strictEqual(t.status().className, 'update-status muted');

// ------------------------------------------------------------- the status is coloured, the field is not
const markup = strip(settingsSource);
const versionRow = markup.slice(markup.indexOf('<label>Version</label>') - 80, markup.indexOf('id="cfg_update_check"'));
const versionInput = (versionRow.match(/<input id="cfg_app_version"[^>]*>/) || [''])[0];
assert.ok(versionInput, 'the Version field is present');
assert.ok(!/style=["'][^"']*color|class=/.test(versionInput.replace(/style="width:100%;"/, '')), 'the Version field itself carries no colour or state class');
const css = strip(read('ui/family.css'));
assert.ok(!/#cfg_app_version\s*\{[^}]*color/.test(css) && !/#cfg_app_version[^{]*\{[^}]*color/.test(css), 'no rule colours the Version field');
assert.ok(/\.update-status\.ok\s*\{\s*color:\s*var\(--state-success-text\)/.test(css));
assert.ok(/\.update-status\.update-available\s*\{[^}]*color:\s*var\(--state-error-text\)/.test(css));
assert.ok(!/\.update-status\.update-available\s*\{[^}]*background/.test(css), 'the status is text, not a filled badge');
// Neutral stays neutral: muted is not redefined as a state colour.
assert.ok(/\.update-status\.muted\s*\{\s*color:\s*var\(--text-muted\)/.test(css));

// ---------------------------------------------------- the same sections, in the same order, as Omni
const headings = [...markup.slice(markup.indexOf('function markup()'), markup.indexOf('function injectMarkup'))
  .matchAll(/<h4>([^<]+)<\/h4>/g)].map(m => m[1]);
['Device Communication', 'Firmware', 'Appearance', 'Application Information'].forEach(h => assert.ok(headings.includes(h), h));
const order = ['Device Communication', 'Firmware', 'Appearance', 'Application Information'].map(h => headings.indexOf(h));
assert.deepStrictEqual(order, order.slice().sort((x, y) => x - y), 'the shared sections run in Omni\'s order');
const remote = strip(read('ui/remote_settings.js'));
assert.ok(remote.indexOf("'Remote Access'") < remote.indexOf("'Backup and Restore'"), 'Remote Access, then Backup and Restore');
assert.ok(/<label>Availability<\/label>/.test(markup), 'the firmware row is labelled as Omni labels it');

console.log('settings parity: ok');
