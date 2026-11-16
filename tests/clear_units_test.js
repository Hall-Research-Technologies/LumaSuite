// Clear Units, driven as a module: a scripted dialog and a recording request helper stand in for the
// browser and the server, so "Cancel makes no mutating request" and "View Assignments returns to the
// question" are measurements of the flow, not of its source text.
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
    if (url === '/api/clear_units' && method === 'POST') return Promise.resolve(post || {ok: true, body: {ok: true, scope: 'all', cleared: 2, kept: 0}});
    return Promise.resolve({ok: false, body: {error: 'unexpected ' + url}});
  };
  return {api, requests, posts: () => requests.filter(r => r.method === 'POST')};
}

const ON = {ok: true, body: {ok: true, remote_access: true, protected: 2, clearable: 3, units: [
  {id: 'a', name: 'Stage Camera', friendly_name: 'Stage Camera', device_name: 'cam-a', type: 'Encoder',
   users: ['alex', 'blair'], direct_access: ['blair'], groups: [], walls: [], count: 2},
  {id: 'b', name: 'Lobby', friendly_name: '', device_name: 'dec-b', type: 'Decoder',
   users: ['alex'], direct_access: [], groups: ['Front'], walls: ['Wall One'], count: 3}]}};
const OFF = {ok: true, body: {ok: true, remote_access: false, protected: 0, clearable: 5, units: []}};
const EMPTY_ON = {ok: true, body: {ok: true, remote_access: true, protected: 0, clearable: 5, units: []}};

async function flush() { for (let i = 0; i < 6; i++) await new Promise(r => setImmediate(r)); }

(async () => {
  // ---- summary wording
  const w0 = world(() => false);
  const t = w0.api.summaryText;
  assert.strictEqual(t({users: [], groups: [], walls: []}), 'Unassigned');
  assert.strictEqual(t({users: ['a'], groups: [], walls: []}), '1 User');
  assert.strictEqual(t({users: ['a', 'b'], groups: ['g'], walls: []}), '2 Users · 1 Group');
  assert.strictEqual(t({users: ['a'], groups: ['g'], walls: ['w']}), '1 User · 1 Group · 1 Wall');
  assert.strictEqual(t({users: ['a'], groups: ['g', 'h'], walls: ['w', 'x']}), '1 User · 2 Groups · 2 Walls');

  // ---- Remote Access off: the ordinary question, Cancel sends nothing, confirming sends one request
  let w = world(() => false), s = server(OFF);
  await w.api.run({api: s.api}); await flush();
  assert.strictEqual(w.asked.length, 1);
  assert.strictEqual(w.asked[0].secondary, undefined, 'nothing to view when Remote Access is off');
  assert.strictEqual(s.posts().length, 0, 'Cancel: no mutating request');
  w = world(() => true); s = server(OFF);
  let done = null;
  await w.api.run({api: s.api, onDone: b => { done = b; }}); await flush();
  assert.deepStrictEqual(s.posts().map(r => r.url), ['/api/clear_units']);
  assert.strictEqual(done.cleared, 2);

  // ---- Remote Access on: three actions, the destructive one is not the default, there is no disable shortcut
  w = world(() => false); s = server(ON);
  await w.api.run({api: s.api}); await flush();
  const q = w.asked[0];
  assert.strictEqual(q.confirmText, 'Clear Unassigned Units');
  assert.strictEqual(q.cancelText, 'Cancel');
  assert.strictEqual(q.secondary.text, 'View Assignments');
  assert.strictEqual(q.danger, true, 'danger puts Cancel under the cursor');
  assert.ok(/disable Remote Access in Settings first/.test(q.message));
  assert.ok(!Object.keys(q).some(k => /disable|turnoff/i.test(k)));
  assert.strictEqual(s.posts().length, 0);
  assert.strictEqual(s.requests.filter(r => /remote_access/.test(r.url)).length, 0, 'never touches Remote Access itself');

  // ---- with nothing protected there is nothing to view
  w = world(() => false); s = server(EMPTY_ON);
  await w.api.run({api: s.api}); await flush();
  assert.strictEqual(w.asked[0].secondary, undefined);

  // ---- View Assignments: shows why, returns to the question, and only the final answer can clear
  w = world((o, n) => (n === 1 ? 'assignments' : true)); s = server(ON, {ok: true, body: {ok: true, scope: 'unassigned', cleared: 3, kept: 2}});
  let result = null;
  const running = w.api.run({api: s.api, onDone: b => { result = b; }});
  await flush();
  const modal = w.byId.lumaAssignmentsBackdrop;
  assert.ok(modal, 'the assignments view was built');
  assert.strictEqual(modal.hidden, false, 'and shown');
  const table = modal.querySelector('table');
  const cells = table.querySelectorAll('td').map(c => c.textContent);
  assert.ok(cells.includes('Stage Camera') && cells.includes('Encoder') && cells.includes('cam-a'));
  assert.ok(cells.includes('2 Users'), 'assignment count in words');
  assert.ok(cells.includes('alex, blair (Direct Access)'), 'users, with Direct Access named');
  assert.ok(cells.includes('Front') && cells.includes('Wall One'));
  assert.ok(cells.includes('1 User · 1 Group · 1 Wall'));
  assert.strictEqual(s.posts().length, 0, 'viewing clears nothing');
  assert.strictEqual(w.asked.length, 1, 'the question is not asked again until the view is closed');
  modal.querySelector('.assign-close').fire('click');
  await running; await flush();
  assert.strictEqual(modal.hidden, true);
  assert.strictEqual(w.asked.length, 2, 'closing returns to the question');
  assert.deepStrictEqual(s.posts().map(r => r.url), ['/api/clear_units'], 'only the final answer clears');
  assert.strictEqual(result.scope, 'unassigned');

  // ---- Escape closes the view without answering the question behind it
  w = world((o, n) => (n === 1 ? 'assignments' : false)); s = server(ON);
  const second2 = w.api.run({api: s.api});
  await flush();
  assert.strictEqual(w.byId.lumaAssignmentsBackdrop.hidden, false);
  (w.doc.handlers.keydown || []).forEach(f => f({key: 'Escape', preventDefault() {}}));
  await second2; await flush();
  assert.strictEqual(w.byId.lumaAssignmentsBackdrop.hidden, true);
  assert.strictEqual(w.asked.length, 2); assert.strictEqual(s.posts().length, 0);
  assert.strictEqual((w.doc.handlers.keydown || []).length, 0, 'the key handler does not outlive the view');

  // ---- single flight: a second click while the first dialog is open is the same decision
  let release;
  w = world(() => new Promise(r => { release = r; })); s = server(OFF);
  const first = w.api.run({api: s.api});
  await flush();
  // Raced against a timer: without the guard the second run asks a second question that never
  // answers, and a test that waits for it would end silently with exit code 0 instead of failing.
  const second = await Promise.race([w.api.run({api: s.api}), new Promise(r => setTimeout(() => r('hung'), 300))]);
  assert.strictEqual(second, null, 'a second click is refused, not queued behind the first');
  assert.strictEqual(s.requests.filter(r => r.url === '/api/clear_units/assignments').length, 1);
  release(false); await first;

  // ---- failure reads: say so, make no clear request, and do not ask
  w = world(() => true); s = server({ok: false, body: {ok: false, error: 'nope'}});
  let said = '';
  await w.api.run({api: s.api, onError: x => { said = x; }}); await flush();
  assert.strictEqual(said, 'nope'); assert.strictEqual(w.asked.length, 0); assert.strictEqual(s.posts().length, 0);
  // a refusal of the clear itself is reported, never reported as done
  w = world(() => true); s = server(OFF, {ok: false, body: {ok: false, error: 'failed to persist'}});
  said = ''; done = null;
  await w.api.run({api: s.api, onError: x => { said = x; }, onDone: b => { done = b; }}); await flush();
  assert.strictEqual(said, 'failed to persist'); assert.strictEqual(done, null);
  // the flight lock is released after a failure
  w = world(() => false); s = server(OFF);
  await w.api.run({api: s.api}); await w.api.run({api: s.api}); await flush();
  assert.strictEqual(w.asked.length, 2, 'a later click is not swallowed');

  // ---- a missing shared dialog refuses the action: no native fallback, no request
  w = world('missing'); s = server(OFF); said = '';
  await w.api.run({api: s.api, onError: x => { said = x; }});
  assert.ok(/not available/.test(said)); assert.strictEqual(s.requests.length, 0);

  // ---- the past-tense sentence
  assert.strictEqual(w.api.describe({scope: 'unassigned', cleared: 3, kept: 2}), 'Cleared 3 unassigned units; kept 2 assigned units.');
  assert.strictEqual(w.api.describe({scope: 'unassigned', cleared: 1, kept: 0}), 'Cleared 1 unassigned unit.');
  assert.strictEqual(w.api.describe({scope: 'all', cleared: 7}), 'Cleared 7 units.');

  // ---- source rules: no native dialog, no markup sink, never a local named like the global it avoids
  const code = SOURCE.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
  assert.ok(!/(?<![\w.])(alert|confirm|prompt)\s*\(/.test(code));
  assert.ok(!/innerHTML|insertAdjacentHTML|document\.write/.test(code));
  assert.ok(!/\/api\/remote_access/.test(code), 'it never changes Remote Access');
  console.log('clear units: ok');
})().catch(e => { console.error(e); process.exit(1); });
