// Settings > Local Application Port (LUMA-MA-010): the render path and the request contract.
//
// The real ui/port_settings.js runs against the shared DOM stub with a recording fetch and a
// scripted shared dialog. What is asserted is what a person sees and what each click requests.
'use strict';
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {Node} = require('./_dom');

const ROOT = path.join(__dirname, '..');
const read = rel => fs.readFileSync(path.join(ROOT, rel), 'utf8');
const tick = () => new Promise(r => setTimeout(r, 5));
const settle = async (n = 10) => { for (let i = 0; i < n; i++) await tick(); };

let passed = 0;
const deadline = setTimeout(() => { console.error('TIMEOUT: a scenario never settled'); process.exit(1); }, 60000);
async function scenario(name, fn) {
  try { await fn(); passed++; }
  catch (err) { console.error('FAILED: ' + name + '\n  ' + (err && err.stack || err)); process.exitCode = 1; }
}

function world(stateBody, options) {
  options = options || {};
  const requests = [];
  const routes = options.routes || {};
  function fetch(url, init) {
    init = init || {};
    const rec = {url: String(url), method: init.method || 'GET', mode: init.mode,
                 body: typeof init.body === 'string' ? JSON.parse(init.body) : undefined};
    requests.push(rec);
    const key = rec.method + ' ' + rec.url.split('?')[0];
    let spec = routes[key];
    if (typeof spec === 'function') spec = spec(rec);
    if (!spec && key === 'GET /api/local_port') spec = {status: options.stateStatus || 200, body: stateBody};
    spec = spec || {status: 200, body: {ok: true, url: 'http://127.0.0.1:27001/', message: 'done'}};
    return Promise.resolve({ok: spec.status >= 200 && spec.status < 300, status: spec.status,
                            json: () => Promise.resolve(spec.body === undefined ? {} : spec.body)});
  }
  const host = new Node('div');
  const registry = new Map();
  const doc = {readyState: 'complete', body: new Node('body'), listeners: {},
    getElementById: id => id === 'cfg_extension_point' ? host : (registry.get(id) || null),
    createElement: t => {
      const n = new Node(t);
      Object.defineProperty(n, 'id', {get() { return n._id; }, set(v) { n._id = v; registry.set(v, n); }});
      return n;
    },
    addEventListener: (type, fn) => { (doc.listeners[type] = doc.listeners[type] || []).push(fn); }};
  // Something else is already in the extension point: the Remote Access section.
  const remote = new Node('div'); remote.id = 'cfg_remote_section'; host.appendChild(remote);
  const toasts = [];
  const confirms = [];
  const answers = (options.answers || []).slice();
  const typed = (options.typed || []).slice();
  const win = {
    toast: (m, k) => toasts.push([m, k]),
    location: {href: 'http://127.0.0.1:8094/', origin: 'http://127.0.0.1:8094'},
    lumaConfirm: o => {
      confirms.push(o);
      const yes = answers.length ? answers.shift() : true;
      if (yes && o.field && typeof o.field.onValue === 'function') {
        o.field.onValue(typed.length ? typed.shift() : o.field.value);
      }
      return Promise.resolve(yes);
    }};
  if (options.session) win.lumaSession = () => Promise.resolve(options.session);
  win.window = win;
  const sandbox = {document: doc, window: win, fetch, Promise, JSON, Object, Array, URL, String, parseInt, setTimeout};
  vm.createContext(sandbox);
  vm.runInContext(read('ui/port_settings.js'), sandbox);
  const reg = id => registry.get(id);
  return {host, doc, registry, reg, requests, toasts, confirms, win, sandbox, remote};
}

async function open(w) {
  (w.doc.listeners['luma:settings-open'] || []).forEach(fn => fn());
  await settle();
}

const AUTO = {ok: true, available: true, mode: 'automatic', fixed: false, configured_port: null, current_port: 8094,
              temporary: false, running: true, url: 'http://127.0.0.1:8094/'};
const FIXED = Object.assign({}, AUTO, {mode: 'fixed', fixed: true, configured_port: 8094});
const TEMP = Object.assign({}, FIXED, {configured_port: 8090, temporary: true});

(async () => {
  await scenario('the section is built once, above Remote Access, and is not part of it', async () => {
    const w = world(AUTO);
    assert.ok(w.reg('cfg_port_section'));
    assert.strictEqual(w.host.children[0], w.reg('cfg_port_section'), 'above Remote Access');
    assert.strictEqual(w.host.children[1], w.remote);
    const n = w.host.children.length;
    vm.runInContext(read('ui/port_settings.js'), w.sandbox);
    assert.strictEqual(w.host.children.length, n, 'a second injection adds nothing');
    assert.ok(!w.reg('cfg_port_section').children.some(c => c === w.remote));
    assert.strictEqual(w.reg('cfg_port_section').children[0].textContent, 'Local Application Port');
  });

  await scenario('automatic: shows the actual port, not editable, says LumaSuite chooses one', async () => {
    const w = world(AUTO); await open(w);
    assert.strictEqual(w.reg('cfg_port_fixed').checked, false);
    assert.strictEqual(w.reg('cfg_port_value').value, '8094');
    assert.strictEqual(w.reg('cfg_port_value').readOnly, true);
    assert.strictEqual(w.reg('cfg_port_address').textContent, 'Current local address: http://127.0.0.1:8094/');
    assert.strictEqual(w.reg('cfg_port_auto').hidden, false);
    assert.ok(/automatically selects an available port/.test(w.reg('cfg_port_auto').textContent));
    assert.ok(/keep LumaSuite.s local browser address consistent after restart/.test(w.reg('cfg_port_helper').textContent));
    assert.strictEqual(w.reg('cfg_port_temp').hidden, true);
    assert.strictEqual(w.reg('cfg_port_change').hidden, true);
  });

  await scenario('checking it asks in the shared dialog, pre-filled with the ACTUAL port (never 8090)', async () => {
    for (const actual of [8094, 27123, 8090]) {
      const w = world(Object.assign({}, AUTO, {current_port: actual}), {answers: [false]}); await open(w);
      const box = w.reg('cfg_port_fixed'); box.checked = true; box.fire('change');
      await settle();
      assert.strictEqual(w.confirms.length, 1);
      const d = w.confirms[0];
      assert.strictEqual(d.title, 'Use a fixed local LumaSuite port');
      assert.strictEqual(d.message, 'A fixed port keeps LumaSuite’s local browser address consistent after restart.');
      assert.strictEqual(JSON.stringify(d.summary), JSON.stringify([{label: 'Current local address:', value: 'http://127.0.0.1:' + actual + '/'}]));
      assert.strictEqual(d.field.label, 'TCP port:');
      assert.strictEqual(d.field.value, String(actual), 'pre-filled with the port in use');
      assert.ok(d.field.help.includes('The current port has been filled in automatically. Keep it or enter another available TCP port.'));
      assert.ok(d.field.help.includes('Changing this setting requires LumaSuite to restart.'));
      assert.strictEqual(d.confirmText, 'Save and Restart');
      assert.strictEqual(d.cancelText, 'Cancel');
    }
  });

  await scenario('Cancel changes nothing, requests nothing and unchecks the box', async () => {
    const w = world(AUTO, {answers: [false]}); await open(w);
    const box = w.reg('cfg_port_fixed'); box.checked = true; box.fire('change');
    await settle();
    assert.strictEqual(box.checked, false);
    assert.deepStrictEqual(w.requests.map(r => r.method + ' ' + r.url), ['GET /api/local_port']);
  });

  await scenario('Save and Restart posts the typed port, says it is restarting and waits for the new address', async () => {
    const w = world(AUTO, {typed: ['27555'], routes: {'POST /api/local_port/enable':
      {status: 200, body: {ok: true, restarting: true, url: 'http://127.0.0.1:27555/'}}}});
    await open(w);
    const box = w.reg('cfg_port_fixed'); box.checked = true; box.fire('change');
    await settle();
    const post = w.requests.find(r => r.method === 'POST');
    assert.strictEqual(post.url, '/api/local_port/enable');
    assert.deepStrictEqual(post.body, {port: 27555});
    assert.ok(/restarting/.test(w.reg('cfg_port_status').textContent));
    const probe = w.requests.find(r => r.url === 'http://127.0.0.1:27555/api/app_version');
    assert.ok(probe, 'it waits for the new address to answer');
    assert.strictEqual(probe.mode, 'no-cors');
    assert.strictEqual(w.win.location.href, 'http://127.0.0.1:27555/');
  });

  await scenario('an invalid port is caught before any request and the dialog is asked again', async () => {
    const w = world(AUTO, {typed: ['abc', '80', '27556'], routes: {'POST /api/local_port/enable':
      {status: 200, body: {ok: true, restarting: true, url: 'http://127.0.0.1:27556/'}}}});
    await open(w);
    const box = w.reg('cfg_port_fixed'); box.checked = true; box.fire('change');
    await settle(20);
    assert.strictEqual(w.confirms.length, 3);
    assert.ok(/whole number between 1024 and 65535/.test(w.confirms[1].message));
    assert.ok(/between 1024 and 65535/.test(w.confirms[2].message));
    assert.strictEqual(w.requests.filter(r => r.method === 'POST').length, 1);
    assert.deepStrictEqual(w.requests.find(r => r.method === 'POST').body, {port: 27556});
  });

  await scenario('a refusal from the server is shown and nothing moves', async () => {
    const w = world(AUTO, {typed: ['27557'], routes: {'POST /api/local_port/enable':
      {status: 409, body: {ok: false, reason: 'in_use', message: 'TCP port 27557 appears to be in use. Choose another.'}}}});
    await open(w);
    const box = w.reg('cfg_port_fixed'); box.checked = true; box.fire('change');
    await settle();
    assert.ok(/appears to be in use/.test(w.reg('cfg_port_status').textContent));
    assert.strictEqual(box.checked, false, 'still automatic');
    assert.strictEqual(w.win.location.href, 'http://127.0.0.1:8094/');
  });

  await scenario('fixed and healthy: shows the fixed port and offers Change Fixed Port', async () => {
    const w = world(FIXED); await open(w);
    assert.strictEqual(w.reg('cfg_port_fixed').checked, true);
    assert.strictEqual(w.reg('cfg_port_change').hidden, false);
    assert.strictEqual(w.reg('cfg_port_temp').hidden, true);
    assert.strictEqual(w.reg('cfg_port_auto').hidden, true);
  });

  await scenario('temporary alternate: the exact warning, both ports, the box checked, three buttons', async () => {
    const w = world(TEMP); await open(w);
    assert.strictEqual(w.reg('cfg_port_temp').hidden, false);
    assert.strictEqual(w.reg('cfg_port_temp_warning').textContent, '⚠ Running temporarily on TCP port 8094');
    assert.strictEqual(w.reg('cfg_port_temp_configured').textContent, 'Configured fixed port: 8090');
    assert.strictEqual(w.reg('cfg_port_temp_current').textContent, 'Current port: 8094');
    assert.strictEqual(w.reg('cfg_port_fixed').checked, true);
    assert.strictEqual(w.reg('cfg_port_retry').textContent, 'Retry Fixed Port');
    assert.strictEqual(w.reg('cfg_port_make').textContent, 'Make 8094 Fixed');
    assert.strictEqual(w.reg('cfg_port_change_temp').textContent, 'Change Fixed Port');
    assert.strictEqual(w.reg('cfg_port_change').hidden, true, 'one set of buttons at a time');
  });

  await scenario('Retry Fixed Port posts retry and moves to the returned address', async () => {
    const w = world(TEMP, {routes: {'POST /api/local_port/retry': {status: 200, body: {ok: true, url: 'http://127.0.0.1:8090/'}}}});
    await open(w);
    w.reg('cfg_port_retry').click(); await settle();
    const post = w.requests.find(r => r.method === 'POST');
    assert.strictEqual(post.url, '/api/local_port/retry');
    assert.strictEqual(w.win.location.href, 'http://127.0.0.1:8090/');
  });

  await scenario('temporary alternate: the Port field shows the CONFIGURED port, the current one is in its own line', async () => {
    const w = world(TEMP); await open(w);
    assert.strictEqual(w.reg('cfg_port_value').value, '8090');
    assert.strictEqual(w.reg('cfg_port_temp_current').textContent, 'Current port: 8094');
    const f = world(FIXED); await open(f);
    assert.strictEqual(f.reg('cfg_port_value').value, '8094');
  });

  await scenario('Make 8094 Fixed confirms first, then posts; Cancel posts nothing', async () => {
    const no = world(TEMP, {answers: [false]}); await open(no);
    no.reg('cfg_port_make').click(); await settle();
    assert.strictEqual(no.requests.filter(r => r.method === 'POST').length, 0);
    const yes = world(TEMP); await open(yes);
    yes.reg('cfg_port_make').click(); await settle();
    assert.strictEqual(yes.confirms[0].title, 'Make 8094 the fixed port?');
    assert.deepStrictEqual(yes.requests.filter(r => r.method === 'POST').map(r => r.url), ['/api/local_port/make_fixed']);
  });

  await scenario('Change Fixed Port asks for a port (pre-filled with the one in use) and posts it', async () => {
    const w = world(TEMP, {typed: ['27600'], routes: {'POST /api/local_port/change': {status: 200, body: {ok: true, url: 'http://127.0.0.1:27600/'}}}});
    await open(w);
    w.reg('cfg_port_change_temp').click(); await settle();
    assert.strictEqual(w.confirms[0].field.value, '8094');
    const post = w.requests.find(r => r.method === 'POST');
    assert.strictEqual(post.url, '/api/local_port/change');
    assert.deepStrictEqual(post.body, {port: 27600});
    assert.strictEqual(w.win.location.href, 'http://127.0.0.1:27600/');
  });

  await scenario('unchecking asks, with the exact sentence, then returns to automatic', async () => {
    const no = world(FIXED, {answers: [false]}); await open(no);
    let box = no.reg('cfg_port_fixed'); box.checked = false; box.fire('change'); await settle();
    assert.strictEqual(box.checked, true, 'Cancel keeps it fixed');
    assert.strictEqual(no.requests.filter(r => r.method === 'POST').length, 0);
    const yes = world(FIXED); await open(yes);
    box = yes.reg('cfg_port_fixed'); box.checked = false; box.fire('change'); await settle();
    assert.ok(yes.confirms[0].message.includes('LumaSuite will return to automatic local port selection. The local browser address may change after restart.'));
    assert.deepStrictEqual(yes.requests.filter(r => r.method === 'POST').map(r => r.url), ['/api/local_port/disable']);
  });

  await scenario('a remote session is never offered the section and makes no request for it', async () => {
    const w = world(AUTO, {session: {local: false}}); await open(w);
    assert.strictEqual(w.reg('cfg_port_section').hidden, true);
    assert.ok(!w.requests.some(r => r.url.indexOf('/api/local_port') === 0));
  });

  await scenario('a refused read (not the local computer) hides the section', async () => {
    const w = world({ok: false}, {stateStatus: 404}); await open(w);
    assert.strictEqual(w.reg('cfg_port_section').hidden, true);
  });

  await scenario('without a launcher the page says so and offers no control', async () => {
    const w = world({ok: true, available: false, message: 'The local application port is managed by the LumaSuite launcher and is not available in this run.'});
    await open(w);
    assert.strictEqual(w.reg('cfg_port_section').hidden, false);
    assert.strictEqual(w.reg('cfg_port_unavailable').hidden, false);
    assert.ok(/not available in this run/.test(w.reg('cfg_port_unavailable').textContent));
    assert.strictEqual(w.reg('cfg_port_fixed').parentNode.hidden, true);
  });

  await scenario('the page never calls a native dialog and only names the local-port endpoints', async () => {
    const code = read('ui/port_settings.js').replace(/^\s*\/\/.*$/gm, '');
    assert.ok(!/\b(alert|confirm|prompt)\s*\(/.test(code.replace(/lumaConfirm/g, '').replace(/function ask/g, '')));
    const endpoints = [...code.matchAll(/['"](\/api\/[A-Za-z0-9_\/]*)['"]/g)].map(m => m[1]);
    endpoints.forEach(e => assert.ok(e.startsWith('/api/local_port'), e));
  });

  await scenario('the dialog module honours a pre-filled field value and help line', async () => {
    const src = read('ui/dialogs.js');
    assert.ok(/fieldInput\.value = field\.value == null \? "" : String\(field\.value\)/.test(src));
    assert.ok(/fieldHelp\.textContent = field\.help/.test(src));
  });

  clearTimeout(deadline);
  console.log(passed + ' scenarios passed' + (process.exitCode ? ' (with failures)' : ''));
})();
