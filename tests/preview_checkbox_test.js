// Device Manager's Preview column (the checkbox is the unit's own thumbnail switch, hover shows the
// picture) and Settings' firmware summary. Both are driven for real: the request the click sends is
// inspected, and the checkbox is shown to change only after the unit confirmed.
'use strict';
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const {Node} = require('./_dom.js');

const read = f => fs.readFileSync(path.join(__dirname, '..', f), 'utf8');
const tick = () => new Promise(r => setTimeout(r, 0));
const strip = s => s.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');

// -------------------------------------------------------------- the Preview handlers
const html = read('ui/index.html');
const start = html.indexOf("(function(){\n  // Preview: the hover card");
assert.ok(start > 0, 'preview block present');
const end = html.indexOf('})();', start) + 5;
const block = html.slice(start, end);

function harness(response) {
  const handlers = {};
  const requests = [];
  const toasts = [];
  const doc = {
    addEventListener: (type, fn) => { (handlers[type] = handlers[type] || []).push(fn); },
    getElementById: () => null,
    createElement: tag => new Node(tag),
    body: new Node('body'),
  };
  const unit = {thumbnail_enabled: true};
  const ctx = {document: doc, JSON, Date, Promise, setInterval: () => 1, clearInterval() {},
    window: {innerWidth: 1000, innerHeight: 800, toast: (m, k) => toasts.push([m, k])},
    unitMap: new Map([['192.0.2.7', unit]]),
    fetch: (url, init) => {
      requests.push({url, method: init && init.method, body: init && JSON.parse(init.body)});
      return response instanceof Error ? Promise.reject(response)
        : Promise.resolve({ok: response.status === 200, status: response.status, json: () => Promise.resolve(response.body)});
    }};
  vm.createContext(ctx);
  vm.runInContext(block, ctx);
  return {handlers, requests, toasts, unit, ctx};
}

function checkbox(checked, indeterminate) {
  const cell = new Node('td'); cell.className = 'preview-cell'; cell.dataset = {ip: '192.0.2.7'};
  const cb = new Node('input'); cb.className = 'preview-checkbox';
  cb.dataset = {ip: '192.0.2.7', mac: '02:00:5e:00:aa:01'}; cb.checked = checked; cb.indeterminate = !!indeterminate;
  cell.appendChild(cb);
  cb.closest = sel => (sel === 'input.preview-checkbox' ? cb : null);
  cell.closest = sel => (sel === 'td.preview-cell' ? cell : null);
  cell.contains = n => n === cb || n === cell;
  cell.querySelector = sel => (sel === 'input.preview-checkbox' ? cb : null);
  return {cb, cell};
}
// A real browser flips the box BEFORE the handlers run and puts it back afterwards when one prevents the default.
const click = (h, cb) => {
  const before = {checked: cb.checked, indeterminate: cb.indeterminate};
  cb.checked = !cb.checked; cb.indeterminate = false;
  let prevented = false;
  (h.handlers.click || []).forEach(fn => fn({target: cb, preventDefault() { prevented = true; }}));
  if (prevented) { cb.checked = before.checked; cb.indeterminate = before.indeterminate; }
  return prevented;
};

(async () => {
  // Turning it off: one request, naming the unit by address AND MAC, and the box changes only afterwards.
  let h = harness({status: 200, body: {ok: true, enabled: false, verified: true}});
  let {cb} = checkbox(true);
  const prevented = click(h, cb);
  assert.ok(prevented, 'the browser does not flip the box by itself');
  assert.strictEqual(cb.checked, true, 'still showing the old state while the unit is being asked');
  assert.strictEqual(cb.disabled, true, 'and cannot be double-clicked');
  assert.deepStrictEqual(h.requests, [{url: '/api/thumbnail', method: 'POST',
    body: {ip: '192.0.2.7', mac: '02:00:5e:00:aa:01', enable: false}}]);
  await tick(); await tick();
  assert.strictEqual(cb.checked, false); assert.strictEqual(cb.disabled, false);
  assert.strictEqual(h.unit.thumbnail_enabled, false, 'the table row agrees with the unit');

  // An unknown state asks for ON.
  h = harness({status: 200, body: {ok: true, enabled: true, verified: true}});
  ({cb} = checkbox(false, true));
  click(h, cb);
  assert.strictEqual(h.requests[0].body.enable, true);
  await tick(); await tick();
  assert.strictEqual(cb.checked, true); assert.strictEqual(cb.indeterminate, false);

  // A unit that did not hold it: the box shows what the unit holds, and the operator is told.
  h = harness({status: 409, body: {ok: false, reason: 'not_held', enabled: true, error: 'The unit did not confirm the change.'}});
  ({cb} = checkbox(true));
  click(h, cb);
  await tick(); await tick();
  assert.strictEqual(cb.checked, true);
  assert.strictEqual(h.toasts.length, 1); assert.strictEqual(h.toasts[0][1], 'error');
  assert.ok(!/not_held|unreadable/.test(h.toasts[0][0]), 'no internal name reaches the operator');

  // A unit that could not be reached leaves the box exactly as it was and says so.
  h = harness(new Error('network'));
  ({cb} = checkbox(true));
  click(h, cb);
  await tick(); await tick();
  assert.strictEqual(cb.checked, true); assert.strictEqual(cb.disabled, false);
  assert.strictEqual(h.toasts.length, 1);

  // The cell is laid out as Omni lays it out: a centred checkbox and the word "Preview" beneath it. The
  // word is a real, keyboard-reachable control with an accessible name; hover and the popup window live in
  // ui/unit-preview.js (unit_preview_test.js drives them).
  const row = html.slice(html.indexOf("const tdPreview = document.createElement('td')"), html.indexOf("tdStatus.innerHTML="));
  assert.ok(/type = 'checkbox'/.test(row) && /aria-label/.test(row), 'the checkbox has an accessible name');
  assert.ok(/createElement\('a'\)/.test(row) && /preview-link/.test(row) && /textContent = 'Preview'/.test(row), 'the word Preview is in the cell');
  assert.ok(/role', 'button'/.test(row) && /tabIndex = 0/.test(row), 'the word is reachable from the keyboard');
  assert.ok(/<script src="\/ui\/unit-preview\.js/.test(html), 'the preview module is loaded by the page');
  const css = strip(html);
  assert.ok(/td\.preview-cell\s*\{\s*text-align:center/.test(css), 'cell centred');
  assert.ok(/preview-checkbox\s*\{\s*display:block;\s*margin:0 auto 4px/.test(css), 'checkbox above the word, centred, Omni geometry');

  // -------------------------------------------------------- firmware summary (Settings)
  const fwSource = read('ui/firmware-summary.js');
  const nodes = {};
  ['cfg_fw_status', 'cfg_fw_detail'].forEach(id => { nodes[id] = new Node('div'); });
  const fwCtx = {document: {getElementById: id => nodes[id] || null, createElement: t => new Node(t),
                            addEventListener() {}},
    fetch: () => Promise.reject(new Error('Settings must not fetch on its own here')),
    window: {}, Date, JSON, Number, Array, String, isFinite, Promise};
  vm.createContext(fwCtx);
  vm.runInContext(fwSource, fwCtx);
  const fw = fwCtx.window.lumaFirmwareSummary;
  const data = {ok: true, hall_lookup: {state: 'ready', fetched_at: 1000, stale: false},
    model_rows: [{model: 'HW-LUMA-E4211', units: 2, installed: ['V1.0.2.3'], latest: 'V1.0.3.0', state: 'update_available', reason: ''},
                 {model: 'HW-LUMA-D4111', units: 1, installed: ['V1.0.2.3'], latest: 'V1.0.2.3', state: 'up_to_date', reason: ''},
                 {model: 'HW-OME-CS31', units: 1, installed: ['V1.0.0.0'], latest: '', state: 'unknown', reason: 'published_version_unavailable'}]};
  fw.render(data);
  assert.ok(/1 model has newer firmware available/.test(nodes.cfg_fw_status.textContent));
  assert.ok(/warn/.test(nodes.cfg_fw_status.className));
  const detail = nodes.cfg_fw_detail.textContent;
  assert.ok(/Last checked/.test(detail), 'last checked is stated');
  const table = nodes.cfg_fw_detail.querySelectorAll('tr');
  assert.strictEqual(table.length, 4, 'header plus one row per model');
  assert.deepStrictEqual(table[0].children.map(c => c.textContent), ['Model', 'Installed', 'Latest', 'Status']);
  assert.deepStrictEqual(table[1].children.map(c => c.textContent).slice(0, 4),
    ['HW-LUMA-E4211 × 2', 'V1.0.2.3', 'V1.0.3.0', 'Available']);
  assert.strictEqual(table[2].children[3].textContent, 'Up to Date');
  assert.ok(/^Unknown/.test(table[3].children[3].textContent));
  assert.ok(/no published version/.test(table[3].children[3].textContent), 'an Unknown says why, in words');
  assert.ok(!/update_available|up_to_date|published_version_unavailable/.test(nodes.cfg_fw_detail.textContent));
  // Not yet checked / unavailable / stale are said, not hidden.
  assert.ok(/Checking Hall Research/.test(fw.summarize({ok: true, hall_lookup: {state: 'pending'}, model_rows: []}).text));
  assert.ok(/Could not reach/.test(fw.summarize({ok: true, hall_lookup: {state: 'unavailable'}, model_rows: []}).text));
  assert.ok(/last known result/.test(fw.summarize({...data, hall_lookup: {state: 'cached', fetched_at: 5, stale: true}}).detail));
  assert.ok(/unavailable/.test(fw.summarize(null).text));
  // The module reads one local endpoint, and its only write is the explicit Check Now (see firmware_check_test.js).
  const calls = [...strip(fwSource).matchAll(/fetch\(\s*'([^']+)'/g)].map(m => m[1]);
  assert.deepStrictEqual([...new Set(calls)].sort(), ['/api/firmware_status', '/api/firmware_status/check']);
  assert.strictEqual((strip(fwSource).match(/method\s*:/g) || []).length, 1, 'exactly one POST, the explicit check');
  const settings = strip(read('ui/settings.js'));
  const loader = settings.slice(settings.indexOf('function loadFirmwareSummary'), settings.indexOf('function open()'));
  assert.ok(/createElement\("script"\)/.test(loader) && !/fetch\(/.test(loader), 'Settings only loads the module');
  console.log('preview checkbox + firmware summary: ok');
})().catch(e => { console.error(e); process.exit(1); });
