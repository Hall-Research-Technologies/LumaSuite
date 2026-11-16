// Remove Selected, driven as a module: a scripted dialog and a recording request helper stand in
// for the browser and the server, so "Cancel sends nothing", "a kept unit is shown with why" and
// "one decision is one request" are measurements of the flow, not of its source text.
'use strict';
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const {Node} = require('./_dom');

const SOURCE = fs.readFileSync(path.join(__dirname, '..', 'ui', 'clear_units.js'), 'utf8');

function world(confirmScript) {
  const byId = {};
  const doc = {
    body: new Node('body'), head: new Node('head'), activeElement: null, handlers: {},
    getElementById: id => byId[id] || null,
    createElement: tag => {
      const n = new Node(tag);
      Object.defineProperty(n, 'id', {get() { return n._id; }, set(v) { n._id = v; if (v) byId[v] = n; }});
      n.removeEventListener = (t, f) => { n.handlers[t] = (n.handlers[t] || []).filter(x => x !== f); };
      return n;
    },
    addEventListener(t, f) { (doc.handlers[t] = doc.handlers[t] || []).push(f); },
    removeEventListener(t, f) { doc.handlers[t] = (doc.handlers[t] || []).filter(x => x !== f); },
  };
  const asked = [];
  const win = {setTimeout: f => { f(); return 0; },
    lumaConfirm: confirmScript === 'missing' ? undefined : (o => { asked.push(o); return Promise.resolve(confirmScript(o, asked.length)); })};
  win.window = win;
  const ctx = {document: doc, window: win, Promise, String, Array, Object};
  vm.createContext(ctx);
  vm.runInContext(SOURCE, ctx);
  return {api: win.lumaClearUnits, asked, doc, byId};
}

function server(view, post) {
  const requests = [];
  const api = (url, options = {}) => {
    const method = options.method || 'GET';
    requests.push({url, method, body: options.body});
    if (url === '/api/clear_units/assignments') return Promise.resolve(view);
    if (url === '/api/remove_units' && method === 'POST') return Promise.resolve(post);
    return Promise.resolve({ok: false, body: {error: 'unexpected ' + url}});
  };
  return {api, requests, posts: () => requests.filter(r => r.method === 'POST')};
}

const ON = {ok: true, body: {ok: true, remote_access: true, protected: 2, clearable: 3, units: []}};
const OFF = {ok: true, body: {ok: true, remote_access: false, protected: 0, clearable: 5, units: []}};
const KEPT = [{id: 'a', name: 'Stage Camera', friendly_name: '', device_name: 'cam-a', type: 'Encoder',
               users: [], direct_access: [], groups: [], walls: ['Stage Wall'],
               wall_reasons: ['Source of wall Stage Wall'], wall_access: ['walker'], count: 1}];

async function flush() { for (let i = 0; i < 6; i++) await new Promise(r => setImmediate(r)); }

(async () => {
  // ---- Cancel sends nothing, and the question is worded for the Remote Access state
  let w = world(() => false), s = server(ON, {ok: true, body: {}});
  await w.api.removeSelected({api: s.api, ips: ['198.18.0.1', '198.18.0.2']}); await flush();
  assert.strictEqual(s.posts().length, 0, 'Cancel: no mutating request');
  assert.strictEqual(w.asked.length, 1);
  assert.ok(/Remote Access is enabled/.test(w.asked[0].message));
  assert.ok(/will be kept/.test(w.asked[0].message));
  assert.ok(/disable Remote Access in Settings first/.test(w.asked[0].message));
  assert.strictEqual(w.asked[0].confirmText, 'Remove Unassigned Selected');
  assert.strictEqual(w.asked[0].danger, true, 'danger puts Cancel under the cursor');
  w = world(() => false); s = server(OFF, {ok: true, body: {}});
  await w.api.removeSelected({api: s.api, ips: ['198.18.0.1']}); await flush();
  assert.ok(!/Remote Access is enabled/.test(w.asked[0].message));
  assert.strictEqual(w.asked[0].confirmText, 'Remove');

  // ---- confirming sends exactly one request carrying the selection and nothing else
  w = world(() => true); s = server(OFF, {ok: true, body: {ok: true, removed: 2, protected: 0, kept: []}});
  let done = null;
  await w.api.removeSelected({api: s.api, ips: ['198.18.0.1', '198.18.0.2'], onDone: b => { done = b; }}); await flush();
  assert.deepStrictEqual(s.posts().map(r => [r.url, JSON.stringify(r.body)]),
    [['/api/remove_units', JSON.stringify({ips: ['198.18.0.1', '198.18.0.2']})]]);
  assert.strictEqual(done.removed, 2);
  assert.strictEqual(w.byId.lumaAssignmentsBackdrop, undefined, 'nothing kept, nothing to show');

  // ---- a partial removal shows the kept units with why, after reporting what was removed
  w = world(() => true); s = server(ON, {ok: true, body: {ok: true, removed: 1, protected: 1, kept: KEPT}});
  done = null;
  const running = w.api.removeSelected({api: s.api, ips: ['a', 'b'], onDone: b => { done = b; }});
  await flush();
  assert.ok(done, 'the removal is reported before the explanation is read');
  const modal = w.byId.lumaAssignmentsBackdrop;
  assert.ok(modal && modal.hidden === false, 'the kept units are shown');
  assert.ok(/Selected units kept/.test(modal.querySelector('h3').textContent));
  const cells = modal.querySelector('table').querySelectorAll('td').map(c => c.textContent);
  assert.ok(cells.includes('Source of wall Stage Wall'), 'why the wall holds it');
  assert.ok(cells.includes('walker'), 'who reaches the wall');
  assert.ok(cells.includes('1 Wall'));
  modal.querySelector('.assign-close').fire('click');
  await running; await flush();
  assert.strictEqual(modal.hidden, true);
  assert.strictEqual(s.posts().length, 1, 'viewing removes nothing more');
  assert.strictEqual(w.api.describeRemoval(done), 'Removed 1 unit; kept 1 assigned unit.');

  // ---- every selected unit protected: reported as an error, never as done, and still explained
  w = world(() => true);
  s = server(ON, {ok: false, body: {ok: false, reason: 'inventory_protected', removed: 0, protected: 1,
                                    kept: KEPT, error: 'Remote Access is using every selected unit.'}});
  let said = ''; done = null;
  const refused = w.api.removeSelected({api: s.api, ips: ['a'], onError: x => { said = x; }, onDone: b => { done = b; }});
  await flush();
  assert.strictEqual(said, 'Remote Access is using every selected unit.');
  assert.strictEqual(done, null, 'a refusal is not a removal');
  const modal2 = w.byId.lumaAssignmentsBackdrop;
  assert.strictEqual(modal2.hidden, false, 'and the reason is shown');
  modal2.querySelector('.assign-close').fire('click');
  await refused; await flush();

  // ---- the wording of the shared view is the caller's, and does not leak into the next caller
  w = world((o, n) => (n === 1 ? 'assignments' : false)); s = server(
    {ok: true, body: {ok: true, remote_access: true, protected: 1, clearable: 0, units: KEPT}}, {ok: true, body: {}});
  const clearRun = w.api.run({api: s.api}); await flush();
  assert.ok(/Units kept while Remote Access is on/.test(w.byId.lumaAssignmentsBackdrop.querySelector('h3').textContent));
  w.byId.lumaAssignmentsBackdrop.querySelector('.assign-close').fire('click');
  await clearRun; await flush();

  // ---- failure reads: say so, make no removal, do not ask
  w = world(() => true); s = server({ok: false, body: {ok: false, error: 'nope'}}, {ok: true, body: {}});
  said = '';
  await w.api.removeSelected({api: s.api, ips: ['a'], onError: x => { said = x; }}); await flush();
  assert.strictEqual(said, 'nope'); assert.strictEqual(w.asked.length, 0); assert.strictEqual(s.posts().length, 0);

  // ---- nothing selected, and a missing shared dialog, are refused without a request
  w = world(() => true); s = server(OFF, {ok: true, body: {}}); said = '';
  await w.api.removeSelected({api: s.api, ips: [], onError: x => { said = x; }});
  assert.ok(/Select at least one/.test(said)); assert.strictEqual(s.requests.length, 0);
  w = world('missing'); s = server(OFF, {ok: true, body: {}}); said = '';
  await w.api.removeSelected({api: s.api, ips: ['a'], onError: x => { said = x; }});
  assert.ok(/not available/.test(said)); assert.strictEqual(s.requests.length, 0);

  // ---- single flight: the second click while the first dialog is open is the same decision
  let release;
  w = world(() => new Promise(r => { release = r; })); s = server(OFF, {ok: true, body: {}});
  const first = w.api.removeSelected({api: s.api, ips: ['a']});
  await flush();
  const second = await Promise.race([w.api.removeSelected({api: s.api, ips: ['a']}),
                                     new Promise(r => setTimeout(() => r('hung'), 300))]);
  assert.strictEqual(second, null, 'a second click is refused, not queued');
  assert.strictEqual(s.requests.filter(r => r.url === '/api/clear_units/assignments').length, 1);
  release(false); await first;
  // released again afterwards
  w = world(() => false); s = server(OFF, {ok: true, body: {}});
  await w.api.removeSelected({api: s.api, ips: ['a']}); await w.api.removeSelected({api: s.api, ips: ['a']});
  assert.strictEqual(w.asked.length, 2, 'a later click is not swallowed');

  // ---- the page uses the module and carries no native dialog of its own for this action
  const page = fs.readFileSync(path.join(__dirname, '..', 'ui', 'index.html'), 'utf8');
  const start = page.indexOf('async function removeSelected(){');
  const fn = page.slice(start, page.indexOf('async function rebootSelected(){'));
  assert.ok(start > 0 && fn.length > 100);
  const body = fn.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
  assert.ok(/lumaClearUnits\.removeSelected/.test(body));
  assert.ok(!/\/api\/remove_units/.test(body), 'the request is the module\'s, not the page\'s');
  assert.ok(!/(?<![\w.])(alert|confirm|prompt)\s*\(/.test(body));
  console.log('remove selected: ok');
})().catch(e => { console.error(e); process.exit(1); });
