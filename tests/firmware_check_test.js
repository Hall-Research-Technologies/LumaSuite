// Settings > Firmware: the Check Now button. Driven through the real module with a scripted server, so
// "one explicit check", "opening Settings asks nothing of Hall Research", "the table, Last checked and the
// operator's feedback all follow" and "a failed check never reads as success" are measurements.
'use strict';
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const {Node} = require('./_dom.js');

const source = fs.readFileSync(path.join(__dirname, '..', 'ui', 'firmware-summary.js'), 'utf8');
const strip = s => s.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
const tick = () => new Promise(r => setImmediate(r));

const rows = [{model: 'HW-LUMA-D4111', units: 2, installed: ['V1.0.2.6'], latest: '1.0.2', state: 'up_to_date', reason: ''}];
const status = (over) => ({ok: true, model_rows: rows,
  hall_lookup: Object.assign({state: 'ready', fetched_at: 1000, last_attempt: 1000, stale: false, running: false}, over || {})});

// script: an array of {match, respond} consumed in order; anything unexpected fails the test.
function harness(script) {
  const handlers = {};
  const calls = [];
  const toasts = [];
  const nodes = {};
  ['cfg_fw_status', 'cfg_fw_detail'].forEach(id => { nodes[id] = new Node('div'); });
  const button = new Node('button'); button.id = 'cfg_fw_check';
  button.closest = sel => (sel === '#cfg_fw_check' ? button : null);
  nodes.cfg_fw_check = button;
  const ctx = {
    document: {getElementById: id => nodes[id] || null, createElement: t => new Node(t),
               addEventListener: (type, fn) => { (handlers[type] = handlers[type] || []).push(fn); }},
    fetch: (url, init) => {
      const method = (init && init.method) || 'GET';
      calls.push(method + ' ' + url);
      const step = script.shift();
      assert.ok(step, 'unexpected request: ' + method + ' ' + url);
      assert.strictEqual(method + ' ' + url, step.expect, 'the request that was due');
      return step.reject ? Promise.reject(new Error('down'))
        : Promise.resolve({ok: step.status === undefined || step.status < 300, status: step.status || 200,
                           json: () => Promise.resolve(step.body)});
    },
    setTimeout: f => { f(); return 0; },
    window: {toast: (m, k) => toasts.push([m, k])},
    Date, JSON, Number, Array, String, isFinite, Promise, Error,
  };
  vm.createContext(ctx);
  vm.runInContext(source, ctx);
  const fire = (type, e) => (handlers[type] || []).forEach(f => f(e));
  return {ctx, calls, toasts, nodes, button, fire, fw: ctx.window.lumaFirmwareSummary, script};
}

(async () => {
  // ----------------------------------------------------------- opening Settings: one local read, no check
  let h = harness([{expect: 'GET /api/firmware_status', body: status()}]);
  h.fire('luma:settings-open', {});
  await tick(); await tick();
  assert.deepStrictEqual(h.calls, ['GET /api/firmware_status'], 'opening Settings only reads what is remembered');
  assert.ok(/up to date/.test(h.nodes.cfg_fw_status.textContent));

  // ------------------------------------------------- Check Now: one POST, then local reads until finished
  h = harness([
    {expect: 'GET /api/firmware_status', body: status({last_attempt: 1000, fetched_at: 1000})},      // before
    {expect: 'POST /api/firmware_status/check', status: 202, body: {ok: true, started: true}},
    {expect: 'GET /api/firmware_status', body: status({running: true})},                              // still going
    {expect: 'GET /api/firmware_status', body: status({fetched_at: 2000, last_attempt: 2000})},       // done
  ]);
  const pending = h.fw.onCheckClick();
  assert.strictEqual(h.button.disabled, true, 'the button is busy while the check runs');
  assert.ok(/Checking Hall Research/.test(h.nodes.cfg_fw_status.textContent), 'and the status says so at once');
  // A second press while it runs starts nothing.
  const second = h.fw.onCheckClick();
  await Promise.all([pending, second]);
  assert.strictEqual(h.script.length, 0, 'every scripted request was made');
  assert.deepStrictEqual(h.calls.filter(c => c.startsWith('POST')), ['POST /api/firmware_status/check'], 'exactly one explicit check');
  assert.strictEqual(h.button.disabled, false, 'free again afterwards');
  assert.deepStrictEqual(h.toasts, [['Firmware check complete.', 'ok']]);
  assert.ok(/Last checked/.test(h.nodes.cfg_fw_detail.textContent), 'Last checked is shown');
  assert.strictEqual(h.nodes.cfg_fw_detail.querySelectorAll('tr').length, 2, 'the table follows the result');
  assert.ok(/up to date/.test(h.nodes.cfg_fw_status.textContent));

  // ---------------------------------------------- the click reaches the same flow from the page's button
  h = harness([
    {expect: 'GET /api/firmware_status', body: status()},
    {expect: 'POST /api/firmware_status/check', status: 202, body: {ok: true}},
    {expect: 'GET /api/firmware_status', body: status({last_attempt: 3000, fetched_at: 3000})},
  ]);
  h.fire('click', {target: h.button});
  await tick(); await tick(); await tick(); await tick(); await tick();
  assert.strictEqual(h.script.length, 0, 'a click on the button runs the check');
  // A click anywhere else does nothing.
  const other = harness([]);
  other.fire('click', {target: {closest: () => null}});
  assert.deepStrictEqual(other.calls, []);

  // -------------------------------------------- a check that could not reach Hall Research is not success
  h = harness([
    {expect: 'GET /api/firmware_status', body: status({last_attempt: 1000})},
    {expect: 'POST /api/firmware_status/check', status: 202, body: {ok: true}},
    {expect: 'GET /api/firmware_status', body: status({state: 'unavailable', last_attempt: 4000, stale: true})},
  ]);
  await h.fw.onCheckClick();
  assert.strictEqual(h.toasts.length, 1);
  assert.strictEqual(h.toasts[0][1], 'error', 'a failed check is reported as one');
  assert.ok(/Could not reach Hall Research/.test(h.toasts[0][0]) && /last known/.test(h.toasts[0][0]));
  assert.ok(/last known result/.test(h.nodes.cfg_fw_detail.textContent), 'the page keeps showing what it knew');

  // ------------------------------------------- the server did not even start it: said, and the page stays honest
  h = harness([
    {expect: 'GET /api/firmware_status', body: status()},
    {expect: 'POST /api/firmware_status/check', status: 403, body: {ok: false}},
    {expect: 'GET /api/firmware_status', body: status()},
  ]);
  await h.fw.onCheckClick();
  assert.deepStrictEqual(h.toasts, [['The firmware check could not start.', 'error']]);
  assert.strictEqual(h.button.disabled, false);

  h = harness([
    {expect: 'GET /api/firmware_status', body: status()},
    {expect: 'POST /api/firmware_status/check', reject: true},
    {expect: 'GET /api/firmware_status', body: status()},
  ]);
  await h.fw.onCheckClick();
  assert.strictEqual(h.toasts[0][1], 'error');
  assert.strictEqual(h.button.disabled, false, 'never left stuck');

  // ------------------------------------- a check that never finishes stops being watched (bounded)
  const long = [{expect: 'GET /api/firmware_status', body: status()},
                {expect: 'POST /api/firmware_status/check', status: 202, body: {ok: true}}];
  for (let i = 0; i < 30; i++) long.push({expect: 'GET /api/firmware_status', body: status({running: true})});
  h = harness(long);
  await h.fw.onCheckClick();
  assert.strictEqual(h.script.length, 0);
  assert.strictEqual(h.calls.length, 32, 'the watch is bounded at thirty reads');
  assert.ok(/still running/.test(h.toasts[0][0]));

  // -------------------------------------------- the pieces: markup, no second implementation, no native dialogs
  const settings = strip(fs.readFileSync(path.join(__dirname, '..', 'ui', 'settings.js'), 'utf8'));
  const fwRow = settings.slice(settings.indexOf('"<label>Availability</label>"'), settings.indexOf('"<h4>Production Studio</h4>"'));
  assert.ok(/id="cfg_fw_check"[^>]*>Check Now</.test(fwRow.replace(/'\s*\+\s*'/g, '')) || /cfg_fw_check/.test(fwRow), 'Check Now sits under the availability text');
  assert.ok(/update-actions/.test(fwRow), 'in the same actions row Omni uses');
  assert.ok(!/fetch\(/.test(settings.slice(settings.indexOf('function loadFirmwareSummary'), settings.indexOf('function open()'))), 'Settings only loads the module');
  assert.ok(!/\b(alert|confirm|prompt)\s*\(/.test(strip(source)));
  console.log('firmware check now: ok');
})().catch(e => { console.error(e); process.exit(1); });
