// The browser scripts around remote access: sign-in, the CSRF wrapper, the accounts
// page and the Remote Access section of Settings.
//
// Each runs for real against the shared DOM stub and a recording fetch. What is
// asserted is behaviour (what request a click makes, what a refusal looks like),
// not that some text exists in a file.
'use strict';
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {Node, pageFor} = require('./_dom');

const ROOT = path.join(__dirname, '..');
const read = rel => fs.readFileSync(path.join(ROOT, rel), 'utf8');
const tick = () => new Promise(r => setTimeout(r, 5));
const settle = async (n = 8) => { for (let i = 0; i < n; i++) await tick(); };
const plain = x => JSON.parse(JSON.stringify(x));

function recorder(routes = {}) {
  const requests = [];
  function fetch(url, init) {
    init = init || {};
    let body, form;
    if (typeof init.body === 'string') body = JSON.parse(init.body);
    else if (init.body && init.body.entries) form = init.body.entries;
    const rec = {url: String(url), method: init.method || 'GET',
                 headers: init.headers instanceof Map ? Object.fromEntries(init.headers) : (init.headers || {}),
                 body, form};
    requests.push(rec);
    const key = rec.method + ' ' + rec.url.split('?')[0];
    let spec = routes[key];
    if (typeof spec === 'function') spec = spec(rec);
    spec = spec || {status: 200, body: {ok: true}};
    return Promise.resolve({ok: spec.status >= 200 && spec.status < 300, status: spec.status,
                            headers: {get: h => (spec.headers || {})[h] || null},
                            json: () => Promise.resolve(spec.body === undefined ? {} : spec.body),
                            blob: () => Promise.resolve({size: 8})});
  }
  return {requests, fetch};
}

let passed = 0;
async function scenario(name, fn) {
  try { await fn(); passed++; }
  catch (err) { console.error('FAILED: ' + name + '\n  ' + (err && err.stack || err)); process.exitCode = 1; }
}

// ----------------------------------------------------------------- sign-in
function login(search, role, protocol = 'https:') {
  const html = read('ui/login.html');
  const {byId} = pageFor(html);
  const store = {};
  const out = {replaced: null};
  const {fetch} = recorder({'POST /api/auth/login': {status: 200, body: {ok: true, csrf_token: 'tok', user: {role}}}});
  const sandbox = {document: {getElementById: id => byId[id]},
    window: {location: {protocol}}, location: {search, protocol, replace: u => { out.replaced = u; }},
    URLSearchParams, sessionStorage: {setItem: (k, v) => { store[k] = v; }}, fetch, JSON, Promise};
  vm.createContext(sandbox);
  vm.runInContext(read('ui/login.js'), sandbox);
  byId.username.value = 'alex'; byId.password.value = 'x';
  (byId.login_form.handlers.submit || []).forEach(fn => fn({preventDefault() {}}));
  return settle().then(() => ({out, store, byId}));
}

(async () => {
  await scenario('sign-in sends an administrator home and a user to the routing page', async () => {
    assert.strictEqual((await login('', 'admin')).out.replaced, '/');
    assert.strictEqual((await login('', 'user')).out.replaced, '/user');
  });

  await scenario('sign-in keeps the CSRF token for this tab only', async () => {
    const r = await login('', 'user');
    assert.strictEqual(r.store.luma_csrf, 'tok');
  });

  await scenario('sign-in never follows a redirect that leaves the site', async () => {
    const evil = ['//evil.test/user', 'https://evil.test/', '/\\evil.test', 'javascript:alert(1)',
                  '/user\\@evil.test', '///evil.test', '/%2F%2Fevil.test', 'user', 'http://127.0.0.1/user',
                  '/user?x=<script>', '/login'];
    for (const next of evil) {
      const r = await login('?next=' + encodeURIComponent(next), 'user');
      assert.strictEqual(r.out.replaced, '/user', 'followed: ' + next);
      const a = await login('?next=' + encodeURIComponent(next), 'admin');
      assert.strictEqual(a.out.replaced, '/', 'admin followed: ' + next);
    }
  });

  await scenario('sign-in follows a safe same-site path, and a restricted user only to their own page', async () => {
    assert.strictEqual((await login('?next=%2Fui%2Fmatrix.html%3Fa%3D1', 'admin')).out.replaced, '/ui/matrix.html?a=1');
    assert.strictEqual((await login('?next=%2Fuser', 'user')).out.replaced, '/user');
    assert.strictEqual((await login('?next=%2Fui%2Fmatrix.html', 'user')).out.replaced, '/user');
  });

  await scenario('sign-in says which kind of connection this is, from the page scheme', async () => {
    const secure = await login('', 'user', 'https:');
    assert.strictEqual(secure.byId.transport_note.textContent, 'This connection is encrypted.');
    const plainHttp = await login('', 'user', 'http:');
    assert.ok(plainHttp.byId.transport_note.textContent.includes('not encrypted'));
    assert.strictEqual(plainHttp.byId.transport_note.hidden, false);
  });

  await scenario('a failed sign-in shows the server sentence and lets the person try again', async () => {
    const html = read('ui/login.html');
    const {byId} = pageFor(html);
    const {fetch} = recorder({'POST /api/auth/login': {status: 401, body: {ok: false, error: 'invalid username or password'}}});
    const sandbox = {document: {getElementById: id => byId[id]}, window: {location: {protocol: 'https:'}}, location: {search: '', protocol: 'https:', replace() {}},
                     URLSearchParams, sessionStorage: {setItem() {}}, fetch, JSON, Promise};
    vm.createContext(sandbox);
    vm.runInContext(read('ui/login.js'), sandbox);
    (byId.login_form.handlers.submit || []).forEach(fn => fn({preventDefault() {}}));
    await settle();
    assert.strictEqual(byId.error.textContent, 'invalid username or password');
    assert.strictEqual(byId.submit.disabled, false);
    assert.strictEqual(byId.submit.textContent, 'Sign in');
  });

  // ------------------------------------------------------------------- csrf
  function csrfWorld(routes, stored = {}) {
    const rec = recorder(routes);
    const store = Object.assign({}, stored);
    const win = {fetch: rec.fetch, location: {href: 'https://host.test:8090/user', origin: 'https://host.test:8090'}};
    const sandbox = {window: win, sessionStorage: {getItem: k => store[k] || null, setItem: (k, v) => { store[k] = v; },
                     removeItem: k => { delete store[k]; }},
      URL, Headers: class Headers extends Map { constructor(init) { super(Object.entries(init || {})); }
        get(k) { for (const [kk, v] of this) if (kk.toLowerCase() === k.toLowerCase()) return v; return null; } },
      Promise, Object, JSON};
    sandbox.window.window = sandbox.window;
    vm.createContext(sandbox);
    vm.runInContext(read('ui/csrf.js'), sandbox);
    return {win, rec, store, sandbox};
  }
  const session = {'GET /api/auth/session': {status: 200, body: {csrf_token: 'T-1', method: 'password', local: false}}};

  await scenario('a state-changing same-origin request carries the token, a read does not', async () => {
    const w = csrfWorld(session);
    await w.win.fetch('/api/x', {method: 'POST', body: '{}'});
    await w.win.fetch('/api/y');
    const post = w.rec.requests.find(r => r.url === '/api/x');
    const get = w.rec.requests.find(r => r.url === '/api/y');
    assert.strictEqual(post.headers['X-LumaSuite-CSRF'], 'T-1');
    assert.ok(!('X-LumaSuite-CSRF' in (get.headers || {})));
  });

  await scenario('a request to another origin never receives the token', async () => {
    const w = csrfWorld(session);
    await w.win.fetch('http://198.51.100.9/api/set', {method: 'POST', body: '{}'});
    const sent = w.rec.requests.find(r => r.url.startsWith('http://198.51.100.9'));
    assert.ok(!('X-LumaSuite-CSRF' in sent.headers));
  });

  await scenario('the wrapper is installed once however many pages load it', async () => {
    const w = csrfWorld(session);
    const first = w.win.fetch;
    vm.runInContext(read('ui/csrf.js'), w.sandbox);
    assert.strictEqual(w.win.fetch, first);
  });

  await scenario('the session endpoint decides the token, not whatever a previous session left in storage', async () => {
    const w = csrfWorld(session, {luma_csrf: 'STALE-FROM-ANOTHER-SESSION'});
    await w.win.fetch('/api/x', {method: 'POST', body: '{}'});
    assert.strictEqual(w.rec.requests.find(r => r.url === '/api/x').headers['X-LumaSuite-CSRF'], 'T-1');
  });

  await scenario('the local operator has no session, so no token is sent and none is asked for twice', async () => {
    const w = csrfWorld({'GET /api/auth/session': {status: 200, body: {ok: true, local: true, role: 'admin'}}});
    await w.win.fetch('/api/x', {method: 'POST', body: '{}'});
    await w.win.fetch('/api/x', {method: 'POST', body: '{}'});
    assert.ok(!('X-LumaSuite-CSRF' in w.rec.requests.filter(r => r.url === '/api/x')[0].headers));
    assert.strictEqual(w.rec.requests.filter(r => r.url === '/api/auth/session').length, 1);
  });

  await scenario('the token is never put in persistent storage', () => {
    const code = read('ui/csrf.js').replace(/\/\/.*$/gm, '');
    assert.ok(!/localStorage/.test(code));
  });

  // ------------------------------------------------------------- accounts page
  function accounts(extra = {}, confirmAnswer = true) {
    const html = read('ui/admin_users.html');
    const {byId, root} = pageFor(html);
    const confirms = [];
    const toasts = [];
    const users = [{id: 'u1', username: 'root', role: 'admin', enabled: true, permissions: {}, direct_access: {enabled: false}},
                   {id: 'u2', username: 'alex<b>', role: 'user', enabled: true,
                    permissions: {sources: [], destinations: [], groups: ['grp_a']}, direct_access: {enabled: false}}];
    const groups = [{id: 'grp_a', name: 'Lobby', members: [{id: '02:00:5e:20:00:01', name: 'Lobby One', known: true}],
                     granted_to: ['alex<b>']}];
    const routes = Object.assign({
      'GET /api/admin/users': {status: 200, body: {ok: true, users}},
      'GET /api/admin/devices': {status: 200, body: {ok: true, devices: [], duplicate_aliases: []}},
      'GET /api/admin/groups': {status: 200, body: {ok: true, groups}},
      'POST /api/admin/users/u2': {status: 200, body: {ok: true}},
      'POST /api/admin/groups/grp_a': {status: 200, body: {ok: true, accounts_affected: ['alex<b>']}},
    }, extra);
    const rec = recorder(routes);
    const pageHandlers = {};
    const on = (type, fn) => { (pageHandlers[type] = pageHandlers[type] || []).push(fn); };
    const doc = {getElementById: id => byId[id], createElement: t => new Node(t), hidden: false,
                 addEventListener: on, querySelector: () => null, querySelectorAll: () => []};
    const sandbox = {document: doc, window: {toast: (m, k) => toasts.push([m, k]), addEventListener: on,
                       lumaConfirm: confirmAnswer === 'missing' ? undefined
                         : (o => { confirms.push(o); return Promise.resolve(typeof confirmAnswer === 'function' ? confirmAnswer(o) : confirmAnswer); })},
      fetch: rec.fetch, location: {replace() {}}, navigator: {}, URLSearchParams, Set, Promise, Date, JSON, Object, Array, console,
      setTimeout, clearTimeout, encodeURIComponent};
    sandbox.window.window = sandbox.window;
    vm.createContext(sandbox);
    vm.runInContext(read('ui/clear_units.js'), sandbox);
    vm.runInContext(read('ui/admin_users.js'), sandbox);
    return {byId, root, rec, confirms, toasts, pageHandlers, doc};
  }
  const rowButtons = (table, label) => table.querySelectorAll('tbody').length ? [] : [];
  const allButtons = node => { const out = []; const walk = n => n.children.forEach(c => { if (c.tagName === 'button') out.push(c); walk(c); }); walk(node); return out; };
  const byLabel = (node, label) => allButtons(node).filter(b => b.textContent === label);

  await scenario('the accounts page shows an account name as text, never as markup', async () => {
    const a = accounts();
    await settle();
    assert.ok(a.byId.user_table.textContent.includes('alex<b>'));
  });

  await scenario('deleting an account asks first, and Cancel makes no request', async () => {
    const a = accounts({}, false);
    await settle();
    const before = a.rec.requests.length;
    byLabel(a.byId.user_table, 'Delete')[1].click();
    await settle();
    assert.strictEqual(a.confirms.length, 1);
    assert.strictEqual(a.rec.requests.length, before, 'Cancel must not send anything');
    assert.strictEqual(a.confirms[0].danger, true);
  });

  await scenario('confirming a delete sends exactly one named action', async () => {
    const a = accounts({}, true);
    await settle();
    const before = a.rec.requests.length;
    byLabel(a.byId.user_table, 'Delete')[1].click();
    await settle();
    const posts = a.rec.requests.slice(before).filter(r => r.method === 'POST');
    assert.strictEqual(posts.length, 1);
    assert.strictEqual(posts[0].url, '/api/admin/users/u2');
    assert.deepStrictEqual(plain(posts[0].body), {action: 'delete'});
  });

  const OFF = {'GET /api/clear_units/assignments': {status: 200, body: {ok: true, remote_access: false, protected: 0, clearable: 3, units: []}}};
  const ON = {'GET /api/clear_units/assignments': {status: 200, body: {ok: true, remote_access: true, protected: 1, clearable: 2,
    units: [{id: '02:00:5e:10:00:0a', name: 'Stage Camera', friendly_name: 'Stage Camera', device_name: 'cam-a', type: 'Encoder',
             users: ['alex'], direct_access: [], groups: [], walls: [], count: 1}]}}};
  const posts = (w, before) => w.rec.requests.slice(before).filter(r => r.method === 'POST').map(r => r.url);

  await scenario('Clear Units, Remote Access off: one direct question; Cancel clears nothing, confirming sends one request', async () => {
    const no = accounts(OFF, false);
    await settle();
    let before = no.rec.requests.length;
    no.byId.clear_open.click();
    await settle();
    assert.strictEqual(no.confirms.length, 1);
    assert.strictEqual(no.confirms[0].title, 'Clear discovered units?');
    assert.strictEqual(no.confirms[0].confirmText, 'Clear Units');
    assert.strictEqual(no.confirms[0].cancelText, 'Cancel');
    assert.strictEqual(no.confirms[0].danger, true);
    assert.ok(!no.confirms[0].secondary, 'no assignments to view when Remote Access is off');
    assert.ok(/removes their assignments from users and display groups/.test(no.confirms[0].message));
    assert.deepStrictEqual(posts(no, before), [], 'Cancel makes no mutating request');

    const yes = accounts(Object.assign({'POST /api/clear_units': {status: 200, body: {ok: true, scope: 'all', cleared: 3, kept: 0}}}, OFF), true);
    await settle();
    before = yes.rec.requests.length;
    yes.byId.clear_open.click();
    await settle();
    assert.deepStrictEqual(posts(yes, before), ['/api/clear_units']);
  });

  await scenario('Clear Units, Remote Access on: Cancel / View Assignments / Clear Unassigned Units, and no shortcut to switch Remote Access off', async () => {
    const no = accounts(ON, false);
    await settle();
    let before = no.rec.requests.length;
    no.byId.clear_open.click();
    await settle();
    const q = no.confirms[0];
    assert.strictEqual(q.title, 'Clear discovered units?');
    assert.ok(q.message.startsWith('Remote Access is enabled.'));
    assert.ok(q.message.includes('To remove assigned units, disable Remote Access in Settings first.'));
    assert.strictEqual(q.confirmText, 'Clear Unassigned Units');
    assert.strictEqual(q.cancelText, 'Cancel');
    assert.strictEqual(JSON.stringify(q.secondary), JSON.stringify({text: 'View Assignments', value: 'assignments'}));
    assert.strictEqual(q.danger, true);
    assert.ok(!/turn off|disable and clear/i.test(JSON.stringify(Object.assign({}, q, {message: ''}))), 'no disable-and-clear shortcut');
    assert.strictEqual(JSON.stringify(q.summary.map(r => r.value)), JSON.stringify(['2 units', '1 assigned unit']));
    assert.deepStrictEqual(posts(no, before), [], 'Cancel makes no mutating request');
    assert.ok(!no.rec.requests.some(r => /remote_access/.test(r.url) && r.method === 'POST'));

    const yes = accounts(Object.assign({'POST /api/clear_units': {status: 200, body: {ok: true, scope: 'unassigned', cleared: 2, kept: 1}}}, ON), true);
    await settle();
    before = yes.rec.requests.length;
    yes.byId.clear_open.click();
    await settle();
    assert.deepStrictEqual(posts(yes, before), ['/api/clear_units']);
    assert.ok(yes.toasts.some(t => /Cleared 2 unassigned units; kept 1 assigned unit/.test(t[0])), JSON.stringify(yes.toasts));
  });

  await scenario('Clear Units: a refused read of the assignments makes no clear request and says so', async () => {
    const w = accounts({'GET /api/clear_units/assignments': {status: 500, body: {ok: false, error: 'Could not read what Remote Access is using.'}}}, true);
    await settle();
    const before = w.rec.requests.length;
    w.byId.clear_open.click();
    await settle();
    assert.deepStrictEqual(posts(w, before), []);
    assert.strictEqual(w.confirms.length, 0);
    assert.ok(w.toasts.some(t => /Could not read/.test(t[0])));
  });

  const device = (id, name, assignments) => ({id, name, device_name: name, role: 'decoder', alias: '', location: '', model: 'D',
    assigned_to: [], in_groups: [], assignments});
  const none = {users: [], direct_access: [], groups: [], walls: [], count: 0};

  await scenario('the Device names table shows one compact assignment summary per device, expandable to the names', async () => {
    const a = accounts({'GET /api/admin/devices': {status: 200, body: {ok: true, duplicate_aliases: [], devices: [
      device('d1', 'Lobby', none),
      device('d2', 'Stage', {users: ['alex'], direct_access: [], groups: [], walls: [], count: 1}),
      device('d3', 'Hall', {users: ['alex', 'blair'], direct_access: ['blair'], groups: ['Front'], walls: ['Wall One'], count: 4})]}}}, true);
    await settle();
    const rows = a.byId.device_table.querySelectorAll('tbody')[0].children;
    const last = r => r.children[r.children.length - 1];
    assert.strictEqual(last(rows[0]).textContent, 'Unassigned');
    assert.strictEqual(last(rows[1]).textContent, '1 User');
    assert.strictEqual(last(rows[2]).textContent, '2 Users · 1 Group · 1 Wall');
    // Collapsed: only the summary. Expanded: the names behind it, Direct Access marked.
    assert.ok(!/blair/.test(last(rows[2]).textContent));
    byLabel(last(rows[2]), '2 Users · 1 Group · 1 Wall')[0].click();
    await settle();
    const expanded = last(a.byId.device_table.querySelectorAll('tbody')[0].children[2]).textContent;
    assert.ok(/Users: alex, blair \(Direct Access\)/.test(expanded), expanded);
    assert.ok(/Display Groups: Front/.test(expanded) && /Saved Video Walls: Wall One/.test(expanded), expanded);
    // The summary is the server's: there is no second derivation in the page.
    assert.ok(!/assigned_to|in_groups/.test(read('ui/admin_users.js').replace(/\/\/.*$/gm, '').split('function assignmentCell')[1].split('function renderDevices')[0]));
  });

  await scenario('returning to the Device names panel re-reads what depends on each device, at most every two seconds', async () => {
    const a = accounts({}, true);
    await settle();
    const reads = () => a.rec.requests.filter(r => r.url === '/api/admin/devices').length;
    const focus = () => (a.pageHandlers.focus || []).forEach(fn => fn({}));
    const start = reads();
    a.byId.panel_devices.hidden = true;
    focus(); await settle();
    assert.strictEqual(reads(), start, 'not while another panel is the one being looked at');
    a.byId.panel_devices.hidden = false;
    a.doc.hidden = true;
    focus(); await settle();
    assert.strictEqual(reads(), start, 'not while the page is hidden');
    a.doc.hidden = false;
    focus(); await settle();
    assert.strictEqual(reads(), start + 1, 'a focus re-reads');
    focus(); focus(); await settle();
    assert.strictEqual(reads(), start + 1, 'and is throttled');
  });

  await scenario('regenerating a direct link asks first and shows the NEW link, never the old one', async () => {
    let n = 0;
    const links = ['https://192.0.2.9:8443/access/AAA', 'https://192.0.2.9:8443/access/BBB'];
    const a = accounts({
      'GET /api/admin/users/u2/direct_access': () => ({status: 200, body: {ok: true, direct_access: {enabled: true}, link: links[0], recoverable: true}}),
      'POST /api/admin/users/u2/direct_access': r => ({status: 200, body: {ok: true, direct_access: {enabled: true}, link: links[++n]}}),
    }, true);
    await settle();
    byLabel(a.byId.user_table, 'Edit')[1].click();
    await settle();
    assert.strictEqual(a.byId.direct_link.value, links[0]);
    const before = a.rec.requests.length;
    a.byId.direct_regen.click();
    await settle();
    const posts = a.rec.requests.slice(before).filter(r => r.method === 'POST');
    assert.strictEqual(posts.length, 1);
    assert.deepStrictEqual(plain(posts[0].body), {action: 'regenerate'});
    assert.strictEqual(a.byId.direct_link.value, links[1]);
    assert.ok(/regenerate/i.test(a.confirms[0].title));
  });

  await scenario('the only administrator cannot be disabled or deleted from the page', async () => {
    const a = accounts();
    await settle();
    assert.strictEqual(byLabel(a.byId.user_table, 'Delete')[0].disabled, true);
    assert.strictEqual(byLabel(a.byId.user_table, 'Disable')[0].disabled, true);
  });

  await scenario('deleting a display group names who loses it, and Cancel changes nothing', async () => {
    const a = accounts({}, false);
    await settle();
    const before = a.rec.requests.length;
    byLabel(a.byId.group_table, 'Delete')[0].click();
    await settle();
    assert.ok(a.confirms[0].message.includes('alex<b>'));
    assert.strictEqual(a.rec.requests.length, before);
  });

  await scenario('a confirmation that cannot be shown refuses the action instead of using a native dialog', async () => {
    const a = accounts({}, 'missing');
    await settle();
    const before = a.rec.requests.length;
    byLabel(a.byId.user_table, 'Delete')[1].click();
    await settle();
    assert.strictEqual(a.rec.requests.length, before);
  });

  await scenario('the account list is read-only from the page: it only names accounts the server returned', async () => {
    const a = accounts();
    await settle();
    const urls = a.rec.requests.map(r => r.url);
    assert.deepStrictEqual(urls.slice(0, 3).sort(), ['/api/admin/devices', '/api/admin/groups', '/api/admin/users']);
  });

  // --------------------------------------------------- remote access settings
  function settings(state, session) {
    const host = new Node('div');
    host.id = 'cfg_extension_point';
    const els = {cfg_extension_point: host};
    ['cfg_pw_toggle', 'cfg_fallback_pw_toggle', 'cfg_firmware_browse', 'cfg_imagestream_browse', 'cfg_logos_browse']
      .forEach(id => { els[id] = new Node('button'); });
    const registry = new Map();
    const doc = {
      readyState: 'complete', body: new Node('body'),
      getElementById: id => els[id] || registry.get(id) || findIn(host, id),
      createElement: t => { const n = new Node(t); Object.defineProperty(n, 'id', {get() { return n._id; }, set(v) { n._id = v; registry.set(v, n); }}); return n; },
      addEventListener: (type, fn) => { (doc.listeners[type] = doc.listeners[type] || []).push(fn); }, listeners: {},
    };
    function findIn(node, id) { return registry.get(id) || null; }
    // A fresh copy per page: the page keeps and edits what it is given, as it would a response.
    const rec = recorder({'GET /api/remote_access': () => ({status: 200, body: plain(state)})});
    const toasts = [];
    const downloads = [];
    const confirms = [];
    doc.createElement = t => {
      const n = new Node(t);
      Object.defineProperty(n, 'id', {get() { return n._id; }, set(v) { n._id = v; registry.set(v, n); }});
      if (t === 'a') n.click = () => downloads.push({download: n.download, href: n.href});
      if (t === 'input') {
        n.files = [];
        // A file chooser answers with the file the scenario chose; with none chosen it never answers.
        n.click = () => {
          if (n.type === 'file' && session && session.chosenFile) {
            n.files = [session.chosenFile];
            (n.handlers.change || []).forEach(fn => fn({target: n}));
          }
        };
      }
      return n;
    };
    // A dialog with a field takes its text from the scenario, one passphrase per question; Cancel
    // (an answer of false) delivers nothing, as the real dialog does.
    const typed = ((session && session.passphrases) || []).slice();
    const answers = ((session && session.confirmAnswers) || []).slice();
    const answer = (session && session.confirmAnswer !== undefined) ? session.confirmAnswer : true;
    const sandbox = {document: doc, fetch: rec.fetch, Promise, JSON, Object, Array, setTimeout,
      URL: {createObjectURL: () => 'blob:x', revokeObjectURL() {}},
      FormData: class { constructor() { this.entries = []; } append(k, v) { this.entries.push([k, v && v.name ? v.name : v]); } },
      window: {lumaSession: () => Promise.resolve(session), toast: (m, k) => toasts.push([m, k]),
               lumaConfirm: o => {
                 confirms.push(o);
                 const yes = answers.length ? answers.shift() : answer;
                 if (yes && o.field && typeof o.field.onValue === 'function') o.field.onValue(typed.length ? typed.shift() : '');
                 return Promise.resolve(yes);
               }}};
    sandbox.window.window = sandbox.window;
    vm.createContext(sandbox);
    vm.runInContext(read('ui/remote_settings.js'), sandbox);
    return {els, registry, doc, rec, toasts, host, sandbox, downloads, confirms};
  }
  const reg = (w, id) => w.registry.get(id);
  const BASE = {ok: true, enabled: false, has_admin: true, configured: false, adapters: [], transport: 'https',
                certificate: {present: false, can_generate: true}, sessions: 0};

  await scenario('the Remote Access section is built once into the extension point', async () => {
    const w = settings(BASE, {local: true});
    assert.ok(reg(w, 'cfg_remote_section'));
    const built = w.host.children.length;
    vm.runInContext(read('ui/remote_settings.js'), w.sandbox);      // a second injection
    assert.strictEqual(w.host.children.length, built, 'the sections must not be duplicated');
  });

  // LUMA-MA-009: the Security option and the certificate line describe the certificate that exists,
  // not which option is selected. 'Active' never says 'trusted'.
  async function certificatePage(transport, certificate, extra) {
    const state = Object.assign({}, BASE, {transport, certificate}, extra || {});
    const w = settings(state, {local: true});
    (w.doc.listeners['luma:settings-open'] || []).forEach(fn => fn());
    await settle();
    return {
      option: reg(w, 'cfg_remote_transport').children[1].textContent,
      detail: reg(w, 'cfg_remote_cert_status').textContent,
    };
  }
  const SELF = {present: true, readable: true, self_signed: true, generated: true, subject: '192.0.2.50',
    for_address: '192.0.2.50', matches_address: true, days_remaining: 364, fingerprint: 'AABBCCDDEEFF00112233445566778899', can_generate: true};

  await scenario('MA-009: a working generated certificate reads as active, with the browser warning and the fingerprint', async () => {
    const r = await certificatePage('https', SELF);
    assert.strictEqual(r.option, 'HTTPS — encrypted · certificate active');
    assert.ok(r.detail.startsWith('Self-signed certificate active for 192.0.2.50 (364 days left).'), r.detail);
    assert.ok(r.detail.includes('Your browser may warn because this certificate is self-signed.'), r.detail);
    assert.ok(r.detail.includes('Fingerprint: AABBCCDDEEFF001122334'), r.detail);
    assert.ok(!/trusted/i.test(r.option + r.detail), 'a self-signed certificate is never called trusted');
    assert.ok(!r.detail.includes('missing or invalid'));
  });

  await scenario('MA-009: a supplied certificate reads as a custom certificate, with no self-signed warning', async () => {
    const custom = Object.assign({}, SELF, {self_signed: false, generated: false, subject: 'luma.example.test', days_remaining: 90});
    const r = await certificatePage('https', custom);
    assert.strictEqual(r.option, 'HTTPS — encrypted · custom certificate active');
    assert.ok(r.detail.startsWith('Custom certificate active for 192.0.2.50 (90 days left).'), r.detail);
    assert.ok(!r.detail.includes('self-signed'), r.detail);
    assert.ok(!/trusted/i.test(r.option + r.detail));
  });

  await scenario('MA-009: a missing certificate says a certificate is required, and what to do', async () => {
    const r = await certificatePage('https', {present: false, can_generate: true});
    assert.strictEqual(r.option, 'HTTPS — certificate required');
    assert.strictEqual(r.detail, 'HTTPS certificate is missing or invalid. Turn off Remote Access, then create ' +
      'or install a certificate for this network before enabling HTTPS.');
  });

  await scenario('MA-009: an unreadable, expired or wrong-network certificate is not called active', async () => {
    const cases = [
      [{present: true, readable: false}, 'could not be read'],
      [Object.assign({}, SELF, {expired: true, days_remaining: -3}), 'has expired'],
      [Object.assign({}, SELF, {matches_address: false, for_address: '192.0.2.99'}), 'does not cover 192.0.2.99'],
    ];
    for (const [certificate, reason] of cases) {
      const r = await certificatePage('https', certificate);
      assert.strictEqual(r.option, 'HTTPS — certificate required', JSON.stringify(certificate));
      assert.ok(r.detail.includes(reason), r.detail);
      assert.ok(r.detail.endsWith('before enabling HTTPS.'), r.detail);
      assert.ok(!/certificate active/.test(r.option + r.detail), 'must not claim an active certificate');
    }
  });

  await scenario('MA-009: in HTTP mode the HTTPS option still describes the certificate that exists', async () => {
    const withCert = await certificatePage('http', SELF);
    assert.strictEqual(withCert.option, 'HTTPS — encrypted · certificate active');
    const without = await certificatePage('http', {present: false, can_generate: true});
    assert.strictEqual(without.option, 'HTTPS — certificate required');
  });

  await scenario('MA-009: the label follows the certificate, not the selected option', async () => {
    const a = await certificatePage('https', {present: false, can_generate: true});
    const b = await certificatePage('http', {present: false, can_generate: true});
    assert.strictEqual(a.option, b.option, 'selecting HTTPS must not change what the certificate is');
    const c = await certificatePage('https', SELF);
    const d = await certificatePage('http', SELF);
    assert.strictEqual(c.option, d.option);
  });

  // Certificate identity: the button replaces only a LumaSuite-made certificate, and only after the
  // shared dialog's Regenerate; Cancel makes no request.
  async function generateClick(certificate, confirmAnswer) {
    const state = Object.assign({}, BASE, {adapters: [{id: 'a1', name: 'Ethernet', address: '192.0.2.50'}], certificate});
    const w = settings(state, {local: true, confirmAnswer});
    w.rec.requests.length = 0;
    (w.doc.listeners['luma:settings-open'] || []).forEach(fn => fn());
    await settle();
    const button = reg(w, 'cfg_remote_cert_generate');
    const label = button.textContent;
    button.click();
    await settle();
    return {w, label, posts: w.rec.requests.filter(r => r.method === 'POST' && r.url === '/api/admin/certificate')};
  }

  await scenario('certificate identity: with no certificate the button creates one, with no confirmation', async () => {
    const r = await generateClick({present: false, can_generate: true}, true);
    assert.strictEqual(r.label, 'Create one for this network');
    assert.strictEqual(r.w.confirms.length, 0);
    assert.strictEqual(r.posts.length, 1);
    assert.strictEqual(r.posts[0].body.action, 'generate');
  });

  await scenario('certificate identity: an existing generated certificate is regenerated only after an explicit confirmation naming the fingerprint', async () => {
    const r = await generateClick(SELF, true);
    assert.strictEqual(r.label, 'Regenerate certificate');
    assert.strictEqual(r.w.confirms.length, 1);
    assert.strictEqual(r.w.confirms[0].title, 'Regenerate certificate');
    assert.ok(r.w.confirms[0].message.includes('AABBCCDDEEFF001122334'), r.w.confirms[0].message);
    assert.ok(r.w.confirms[0].message.includes('fingerprint will change'));
    assert.ok(!/trusted/i.test(r.w.confirms[0].message));
    assert.strictEqual(r.posts.length, 1);
    assert.deepStrictEqual(plain(r.posts[0].body), {action: 'generate', address: '192.0.2.50'});
  });

  await scenario('certificate identity: Cancel regenerates nothing and makes no request', async () => {
    const r = await generateClick(SELF, false);
    assert.strictEqual(r.w.confirms.length, 1);
    assert.strictEqual(r.posts.length, 0);
  });

  await scenario('certificate identity: the success message shows the fingerprint change', async () => {
    const state = Object.assign({}, BASE, {adapters: [{id: 'a1', name: 'Ethernet', address: '192.0.2.50'}], certificate: SELF});
    const w = settings(state, {local: true, confirmAnswer: true});
    w.rec.requests.length = 0;
    const generated = {ok: true, previous_fingerprint: 'AABBCCDDEEFF00112233445566778899',
      certificate: {fingerprint: '11223344556677889900AABBCCDDEEFF'}};
    const original = w.rec.fetch;
    w.sandbox.fetch = (url, init) => (String(url) === '/api/admin/certificate'
      ? Promise.resolve({ok: true, status: 200, headers: {get: () => null}, json: () => Promise.resolve(generated)})
      : original(url, init));
    (w.doc.listeners['luma:settings-open'] || []).forEach(fn => fn());
    await settle();
    reg(w, 'cfg_remote_cert_generate').click();
    await settle();
    const message = w.toasts.map(t => t[0]).join(' | ');
    assert.ok(message.includes('Fingerprint changed from AABBCCDDEEFF001122334') && message.includes('to 11223344556677889900AAB'), message);
  });

  await scenario('certificate identity: a supplied certificate keeps the plain label and the status never says trusted', async () => {
    const custom = Object.assign({}, SELF, {self_signed: false, generated: false});
    const r = await generateClick(custom, true);
    assert.strictEqual(r.label, 'Create one for this network');
    const detail = reg(r.w, 'cfg_remote_cert_status').textContent;
    assert.ok(detail.startsWith('Custom certificate active'), detail);
    assert.ok(!/trusted/i.test(detail));
  });

  await scenario('a dialog opened before this script loaded still gets its section filled in', async () => {
    // settings(...) builds the page; here the host sits inside a dialog that is already open.
    const html = read('ui/remote_settings.js');
    const host = new Node('div'); host.id = 'cfg_extension_point';
    const dialog = new Node('div'); dialog.classList.add('open'); dialog.appendChild(host);
    const registry = new Map();
    const rec = recorder({'GET /api/remote_access': () => ({status: 200, body: plain(BASE)})});
    const doc = {readyState: 'complete', getElementById: id => id === 'cfg_extension_point' ? host : (registry.get(id) || null),
      createElement: t => { const n = new Node(t); Object.defineProperty(n, 'id', {get() { return n._id; }, set(v) { n._id = v; registry.set(v, n); }}); return n; },
      addEventListener() {}};
    const sandbox = {document: doc, fetch: rec.fetch, Promise, JSON, Object, Array, setTimeout,
      URL: {}, FormData: class {}, window: {lumaSession: () => Promise.resolve({local: true}), toast() {}, lumaConfirm: () => Promise.resolve(true)}};
    sandbox.window.window = sandbox.window;
    vm.createContext(sandbox);
    vm.runInContext(html, sandbox);
    await settle();
    assert.ok(rec.requests.some(r => r.url === '/api/remote_access'), 'the section loaded its state on its own');
    assert.ok(registry.get('cfg_remote_status').textContent.length > 0);
    const closed = settings(BASE, {local: true});
    await settle();
    assert.ok(!closed.rec.requests.some(r => r.url === '/api/remote_access'), 'a closed dialog makes no request');
  });

  await scenario('exactly one transport statement shows, and plaintext is never described as encrypted', async () => {
    const w = settings(Object.assign({}, BASE, {transport: 'http'}), {local: true});
    (w.doc.listeners['luma:settings-open'] || []).forEach(fn => fn());
    await settle();
    assert.strictEqual(reg(w, 'cfg_remote_warning').hidden, false);
    assert.strictEqual(reg(w, 'cfg_remote_note').hidden, true);
    const v = settings(Object.assign({}, BASE, {transport: 'https'}), {local: true});
    (v.doc.listeners['luma:settings-open'] || []).forEach(fn => fn());
    await settle();
    assert.strictEqual(reg(v, 'cfg_remote_warning').hidden, true);
    assert.strictEqual(reg(v, 'cfg_remote_note').hidden, false);
  });

  await scenario('a remote administrator is not offered the password eye or the host folder browser', async () => {
    const w = settings(BASE, {local: false, username: 'root'});
    await settle();
    ['cfg_pw_toggle', 'cfg_fallback_pw_toggle', 'cfg_firmware_browse', 'cfg_imagestream_browse', 'cfg_logos_browse']
      .forEach(id => assert.strictEqual(w.els[id].hidden, true, id));
    assert.strictEqual(reg(w, 'cfg_remote_local_note').hidden, false);
  });

  await scenario('the local computer keeps every control and sees no explanatory note', async () => {
    const w = settings(BASE, {local: true});
    await settle();
    ['cfg_pw_toggle', 'cfg_firmware_browse'].forEach(id => assert.strictEqual(w.els[id].hidden, false, id));
    assert.strictEqual(reg(w, 'cfg_remote_local_note').hidden, true);
  });

  await scenario('the first administrator can be created only on the local computer', async () => {
    const noAdmin = Object.assign({}, BASE, {has_admin: false});
    const local = settings(noAdmin, {local: true});
    (local.doc.listeners['luma:settings-open'] || []).forEach(fn => fn());
    await settle();
    assert.strictEqual(reg(local, 'cfg_remote_bootstrap').hidden, false);
    const remote = settings(noAdmin, {local: false});
    (remote.doc.listeners['luma:settings-open'] || []).forEach(fn => fn());
    await settle();
    assert.strictEqual(reg(remote, 'cfg_remote_bootstrap').hidden, true);
  });

  await scenario('turning remote access on needs a confirmation, and plaintext needs the explicit acknowledgement', async () => {
    const state = Object.assign({}, BASE, {adapters: [{id: 'a1', name: 'Ethernet', address: '192.0.2.50'}]});
    const w = settings(state, {local: true});
    (w.doc.listeners['luma:settings-open'] || []).forEach(fn => fn());
    await settle();
    reg(w, 'cfg_remote_transport').value = 'http';
    reg(w, 'cfg_remote_transport').handlers.change.forEach(fn => fn());
    reg(w, 'cfg_remote_toggle').click();
    await settle();
    const post = w.rec.requests.find(r => r.method === 'POST' && r.url === '/api/remote_access');
    assert.strictEqual(post.body.accept_insecure, true);
    assert.strictEqual(post.body.transport, 'http');
    const secure = settings(Object.assign({}, state, {certificate: SELF}), {local: true});
    (secure.doc.listeners['luma:settings-open'] || []).forEach(fn => fn());
    await settle();
    reg(secure, 'cfg_remote_toggle').click();
    await settle();
    const p2 = secure.rec.requests.find(r => r.method === 'POST' && r.url === '/api/remote_access');
    assert.strictEqual(p2.body.accept_insecure, false);
    assert.strictEqual(p2.body.transport, 'https');
  });

  // ---- HTTPS selected with no usable certificate is a decision, never a silent outcome ----
  const NO_CERT_STATE = Object.assign({}, BASE, {transport: 'https', certificate: {present: false, can_generate: true},
    adapters: [{id: 'a1', name: 'Ethernet', address: '192.0.2.50'}]});
  async function noCertificatePage(choice) {
    const w = settings(NO_CERT_STATE, {local: true, confirmAnswers: choice === 'then-confirm' ? [true, true] : [choice, true]});
    (w.doc.listeners['luma:settings-open'] || []).forEach(fn => fn());
    await settle();
    reg(w, 'cfg_remote_toggle').click();
    await settle();
    return w;
  }
  const postsTo = (w, url) => w.rec.requests.filter(r => r.method === 'POST' && r.url === url);

  await scenario('DEFECT 2: HTTPS with no usable certificate asks first, offering exactly three answers', async () => {
    const w = await noCertificatePage(false);
    assert.strictEqual(w.confirms.length, 1, 'one question, nothing else');
    const q = w.confirms[0];
    assert.strictEqual(q.title, 'HTTPS needs a certificate');
    assert.strictEqual(q.confirmText, 'Create Certificate');
    assert.strictEqual(q.secondary.text, 'Change to HTTP');
    assert.strictEqual(q.secondary.value, 'http');
    assert.strictEqual(q.cancelText, 'Cancel');
    assert.ok(/nothing has been turned on/i.test(q.message));
  });

  await scenario('DEFECT 2: Cancel changes nothing at all', async () => {
    const w = await noCertificatePage(false);
    assert.strictEqual(postsTo(w, '/api/remote_access').length, 0);
    assert.strictEqual(postsTo(w, '/api/admin/certificate').length, 0);
    assert.strictEqual(reg(w, 'cfg_remote_transport').value, 'https', 'the selection is left as it was');
  });

  await scenario('DEFECT 2: Create Certificate enters the existing creation flow and does not turn anything on', async () => {
    const w = await noCertificatePage(true);
    const created = postsTo(w, '/api/admin/certificate');
    assert.strictEqual(created.length, 1);
    assert.strictEqual(created[0].body.action, 'generate');
    assert.strictEqual(created[0].body.address, '192.0.2.50');
    assert.strictEqual(postsTo(w, '/api/remote_access').length, 0, 'creating a certificate is not turning access on');
  });

  await scenario('DEFECT 2: Change to HTTP selects HTTP and continues into the ordinary plaintext confirmation', async () => {
    const w = await noCertificatePage('http');
    assert.strictEqual(w.confirms.length, 2, 'the choice, then the plaintext confirmation');
    assert.strictEqual(w.confirms[1].title, 'Turn on remote access');
    assert.strictEqual(w.confirms[1].danger, true);
    const turnOn = postsTo(w, '/api/remote_access');
    assert.strictEqual(turnOn.length, 1);
    assert.strictEqual(turnOn[0].body.transport, 'http');
    assert.strictEqual(turnOn[0].body.accept_insecure, true);
    assert.strictEqual(postsTo(w, '/api/admin/certificate').length, 0, 'no certificate was created on the way');
  });

  await scenario('DEFECT 2: a usable certificate goes straight to the ordinary confirmation, with no extra question', async () => {
    const w = settings(Object.assign({}, NO_CERT_STATE, {certificate: SELF}), {local: true});
    (w.doc.listeners['luma:settings-open'] || []).forEach(fn => fn());
    await settle();
    reg(w, 'cfg_remote_toggle').click();
    await settle();
    assert.strictEqual(w.confirms.length, 1);
    assert.strictEqual(w.confirms[0].title, 'Turn on remote access');
    assert.strictEqual(postsTo(w, '/api/remote_access')[0].body.transport, 'https');
  });

  await scenario('DEFECT 2: a certificate made for another network is also a decision, and a custom one is never replaced from here', async () => {
    const wrongNetwork = Object.assign({}, SELF, {for_address: '192.0.2.99', matches_address: false});
    const w = settings(Object.assign({}, NO_CERT_STATE, {certificate: wrongNetwork}), {local: true, confirmAnswers: [false]});
    (w.doc.listeners['luma:settings-open'] || []).forEach(fn => fn());
    await settle();
    reg(w, 'cfg_remote_toggle').click();
    await settle();
    assert.strictEqual(w.confirms[0].title, 'HTTPS needs a certificate');
    assert.strictEqual(postsTo(w, '/api/remote_access').length, 0);
  });

  // ------------------------------------------------------------------ sign-out
  function adminnav(session, gearId) {
    const header = new Node('div');
    const gear = new Node('button'); gear.id = gearId;
    header.appendChild(gear);
    const added = [];
    const rec = recorder({'POST /api/auth/logout': {status: 200, body: {ok: true}}});
    let replaced = null, forgot = 0;
    const doc = {readyState: 'complete', addEventListener() {},
      querySelector: sel => sel.split(',').map(s => s.trim()).some(s => s === '#' + gearId) ? gear : null,
      getElementById: id => (id === 'luma_signed_in' ? added.find(n => n.id === 'luma_signed_in') || null : null),
      createElement: t => { const n = new Node(t); return n; }};
    const sandbox = {document: doc, fetch: rec.fetch, Promise, JSON, location: {replace: u => { replaced = u; }},
      window: {lumaSession: () => Promise.resolve(session), lumaForgetCsrf: () => { forgot++; },
               addEventListener() {}}};
    sandbox.window.window = sandbox.window;
    vm.createContext(sandbox);
    vm.runInContext(read('ui/adminnav.js'), sandbox);
    return {header, gear, rec, get replaced() { return replaced; }, get forgot() { return forgot; }};
  }
  const labels = n => n.header.children.map(c => c.textContent);

  await scenario('a remote administrator gets their name and Sign out beside the gear on every page', async () => {
    for (const id of ['header_gear', 'gear']) {          // Production Studio's gear is #gear
      const n = adminnav({local: false, username: 'root'}, id);
      await settle();
      assert.deepStrictEqual(labels(n), ['root', 'Sign out', ''], id);
    }
  });

  await scenario('the local computer gets nothing added: there is no account to sign out of', async () => {
    const n = adminnav({local: true, role: 'admin'}, 'header_gear');
    await settle();
    assert.strictEqual(n.header.children.length, 1);
  });

  await scenario('Sign out ends the session, forgets the token and goes to the sign-in page', async () => {
    const n = adminnav({local: false, username: 'root'}, 'header_gear');
    await settle();
    n.header.children[1].click();
    await settle();
    assert.ok(n.rec.requests.some(r => r.url === '/api/auth/logout' && r.method === 'POST'));
    assert.strictEqual(n.forgot, 1);
    assert.strictEqual(n.replaced, '/login');
  });

  // ------------------------------------------------------------- backup and restore
  const opened = async (w) => { (w.doc.listeners['luma:settings-open'] || []).forEach(fn => fn()); await settle(); };

  await scenario('backup and restore controls are offered on the local computer only; remotely the reason is shown', async () => {
    const local = settings(BASE, {local: true});
    await opened(local);
    assert.strictEqual(reg(local, 'cfg_backup_section').hidden, false);
    assert.strictEqual(reg(local, 'cfg_backup_export').hidden, false);
    assert.strictEqual(reg(local, 'cfg_backup_export').disabled, false);
    assert.strictEqual(reg(local, 'cfg_backup_restore').hidden, false);
    assert.strictEqual(reg(local, 'cfg_backup_blurb').hidden, false);
    assert.strictEqual(reg(local, 'cfg_backup_remote').hidden, true);
    const remote = settings(BASE, {local: false});
    await opened(remote);
    assert.strictEqual(reg(remote, 'cfg_backup_export').hidden, true);
    assert.strictEqual(reg(remote, 'cfg_backup_export').disabled, true);
    assert.strictEqual(reg(remote, 'cfg_backup_restore').hidden, true);
    assert.strictEqual(reg(remote, 'cfg_backup_blurb').hidden, true, 'a remote page does not describe what a backup holds');
    assert.strictEqual(reg(remote, 'cfg_backup_remote').hidden, false);
    assert.ok(/local access/.test(reg(remote, 'cfg_backup_remote').textContent));
  });

  await scenario('before the server has said where this browser is, the backup controls are hidden and disabled', async () => {
    const w = settings(BASE, {local: true});
    // No session answer has been applied yet: the safe direction is withdrawn.
    const fresh = settings(BASE, undefined);
    assert.strictEqual(reg(fresh, 'cfg_backup_export').hidden, true);
    assert.strictEqual(reg(fresh, 'cfg_backup_export').disabled, true);
    assert.strictEqual(reg(fresh, 'cfg_backup_restore').hidden, true);
    assert.ok(w);
  });

  await scenario('the Settings line about Clear Units shows only while Remote Access is on', async () => {
    const off = settings(BASE, {local: true});
    await opened(off);
    const note = reg(off, 'cfg_remote_protect_note');
    assert.ok(note, 'the line exists');
    assert.strictEqual(note.hidden, true, 'hidden while Remote Access is off');
    assert.strictEqual(note.textContent, 'Assigned devices are protected from Clear Units while Remote Access is enabled.');
    const on = settings(Object.assign({}, BASE, {enabled: true, url: 'https://192.0.2.9:8443', encrypted: true}), {local: true});
    await opened(on);
    assert.strictEqual(reg(on, 'cfg_remote_protect_note').hidden, false, 'shown while it is on');
  });

  await scenario('the backup text describes what Luma backs up and never Multiview', async () => {
    const w = settings(BASE, {local: true});
    const text = reg(w, 'cfg_backup_blurb').textContent;
    ['accounts', 'permissions', 'device names', 'display groups', 'saved video walls', 'Production Studio',
     'remote access', 'device sign-in', 'encrypted'].forEach(t => assert.ok(text.includes(t), 'mentions ' + t));
    assert.ok(/cannot be shown again/.test(text.replace(/\s+/g, ' ')), 'says what does not travel');
    assert.ok(!/multiview/i.test(read('ui/remote_settings.js').replace(/\/\/.*$/gm, '')), 'Luma has no Multiview and the page never says so');
  });

  await scenario('an export is a dialog with a passphrase field, asked once, of eight characters or more', async () => {
    const before = (w) => w.rec.requests.length;
    // Too short: nothing is sent, and the page says why in its status line.
    let w = settings(BASE, {local: true, passphrases: ['short']});
    await opened(w);
    let n = before(w);
    reg(w, 'cfg_backup_export').click();
    await settle();
    assert.strictEqual(before(w), n, 'nothing is sent for a weak passphrase');
    assert.strictEqual(w.confirms.length, 1);
    assert.strictEqual(w.confirms[0].field.type, 'password', 'a password field in the shared dialog, never a prompt');
    assert.ok(/At least 8 characters/.test(w.confirms[0].message));
    assert.ok(/too short/.test(reg(w, 'cfg_backup_status').textContent));
    // A valid passphrase is asked for once: there is no repeat question to answer.
    w = settings(BASE, {local: true, passphrases: ['a long enough one']});
    await opened(w);
    w.rec.requests.length = 0;
    const original = w.sandbox.fetch;
    w.sandbox.fetch = (u, i) => u === '/api/backup/export'
      ? (w.rec.requests.push({url: u, method: i.method, body: JSON.parse(i.body)}),
         Promise.resolve({ok: true, status: 200, headers: {get: () => 'attachment; filename="x.lumabak"'},
                          blob: () => Promise.resolve({size: 8}), json: () => Promise.resolve({})}))
      : original(u, i);
    reg(w, 'cfg_backup_export').click();
    await settle();
    assert.strictEqual(w.confirms.length, 1, 'asked once, never twice');
    assert.ok(!w.confirms.some(c => /repeat/i.test(c.title)), 'no repeat question');
    assert.strictEqual(w.rec.requests.filter(r => r.url === '/api/backup/export').length, 1);
    // Cancelling the first question sends nothing and says nothing.
    w = settings(BASE, {local: true, confirmAnswers: [false]});
    await opened(w);
    n = before(w);
    reg(w, 'cfg_backup_export').click();
    await settle();
    assert.strictEqual(before(w), n);
    assert.strictEqual(reg(w, 'cfg_backup_status').textContent, '');
  });

  await scenario('a valid export sends the passphrase once and saves the file under the server name', async () => {
    const w = settings(BASE, {local: true, passphrases: ['a long enough one']});
    w.rec.requests.length = 0;
    await opened(w);
    const original = w.sandbox.fetch;
    w.sandbox.fetch = (u, i) => u === '/api/backup/export'
      ? (w.rec.requests.push({url: u, method: i.method, body: JSON.parse(i.body)}),
         Promise.resolve({ok: true, status: 200, headers: {get: () => 'attachment; filename="LumaSuite-1.1.0-backup-x.lumabak"'},
                          blob: () => Promise.resolve({size: 8}), json: () => Promise.resolve({})}))
      : original(u, i);
    reg(w, 'cfg_backup_export').click();
    await settle();
    const sent = w.rec.requests.filter(r => r.url === '/api/backup/export');
    assert.strictEqual(sent.length, 1);
    assert.deepStrictEqual(plain(sent[0].body), {passphrase: 'a long enough one'});
    assert.strictEqual(w.downloads.length, 1);
    assert.strictEqual(w.downloads[0].download, 'LumaSuite-1.1.0-backup-x.lumabak');
    assert.ok(/Saved LumaSuite-1\.1\.0-backup-x\.lumabak/.test(reg(w, 'cfg_backup_status').textContent));
    assert.strictEqual(reg(w, 'cfg_backup_export').disabled, false);
    assert.ok(!JSON.stringify(w.toasts).includes('a long enough one'), 'the passphrase is never echoed');
  });

  await scenario('a refused export is reported in the status line and the button is free again', async () => {
    const w = settings(BASE, {local: true, passphrases: ['a long enough one', 'a long enough one']});
    await opened(w);
    const original = w.sandbox.fetch;
    w.sandbox.fetch = (u, i) => u === '/api/backup/export'
      ? Promise.resolve({ok: false, status: 500, headers: {get: () => ''}, json: () => Promise.resolve({ok: false, error: 'The backup could not be created.'})})
      : original(u, i);
    reg(w, 'cfg_backup_export').click();
    await settle();
    assert.ok(/could not be created/.test(reg(w, 'cfg_backup_status').textContent));
    assert.strictEqual(w.downloads.length, 0);
    assert.strictEqual(reg(w, 'cfg_backup_export').disabled, false);
  });

  // The restore: file, passphrase, contents, then the decision.
  const FILE = {name: 'site.lumabak'};
  const inspectSummary = {created_at: '2026-10-31 09:00', devices: 3, notes: [],
    sections: [{name: 'accounts', detail: '2 account(s)'}, {name: 'video_walls', detail: '1 video wall(s)'}, {name: 'display_groups', detail: '1 display group(s)'}],
    machine_specific: ['remote_access']};
  function withBackupEndpoints(w, summary) {
    const original = w.sandbox.fetch;
    w.sandbox.fetch = (u, i) => {
      if (u === '/api/backup/inspect' || u === '/api/backup/restore') {
        w.rec.requests.push({url: u, method: i.method, form: i.body && i.body.entries});
        const body = u === '/api/backup/inspect' ? {ok: true, summary: summary || inspectSummary}
          : {ok: true, status: 'RESTORED', admin_recovery: {note: 'Sign in again with an administrator from the backup.'}};
        return Promise.resolve({ok: true, status: 200, json: () => Promise.resolve(body)});
      }
      return original(u, i);
    };
    return w;
  }

  await scenario('a restore asks for the file, then its passphrase, shows the contents, and Cancel restores nothing', async () => {
    const w = withBackupEndpoints(settings(BASE, {local: true, chosenFile: FILE, passphrases: ['its passphrase'],
                                                  confirmAnswers: [true, false]}));
    await opened(w);
    reg(w, 'cfg_backup_restore').click();
    await settle();
    const urls = w.rec.requests.map(r => r.url);
    assert.ok(urls.includes('/api/backup/inspect'));
    assert.ok(!urls.includes('/api/backup/restore'), 'Cancel must not restore');
    assert.strictEqual(w.confirms[0].field.type, 'password');
    assert.ok(/Restore from site\.lumabak/.test(w.confirms[0].title));
    const decision = w.confirms[1];
    assert.strictEqual(decision.danger, true, 'the costly answer is behind the focus');
    assert.ok(/saved first/.test(decision.message));
    const labels = decision.summary.map(r => r.label);
    ['User accounts and permissions', 'Saved video walls', 'Display groups', 'Devices remembered', 'Not copied'].forEach(l => assert.ok(labels.includes(l), l));
    assert.ok(!labels.some(l => /_/.test(l)), 'no internal section name reaches the operator');
    assert.ok(!/multiview/i.test(JSON.stringify(decision)));
    assert.strictEqual(reg(w, 'cfg_backup_status').textContent, 'Nothing was changed.');
    assert.strictEqual(reg(w, 'cfg_backup_restore').disabled, false);
  });

  await scenario('a restore says plainly which accounts it removes and replaces, before anything is touched', async () => {
    const withAccounts = Object.assign({}, inspectSummary, {
      accounts: {removed: ['zed', 'kim'], replaced: ['alex'], added: [], unchanged: 1},
      will_remove: [], notes: ['2 accounts on this computer (kim, zed) are not in the backup and will be removed.']});
    const w = withBackupEndpoints(settings(BASE, {local: true, chosenFile: FILE, passphrases: ['its passphrase'],
                                                  confirmAnswers: [true, false]}), withAccounts);
    await opened(w);
    reg(w, 'cfg_backup_restore').click();
    await settle();
    const decision = w.confirms[1];
    assert.ok(/2 accounts on this computer will be removed/.test(decision.message), decision.message);
    const rows = Object.fromEntries(decision.summary.map(r => [r.label, r.value]));
    assert.ok(/zed, kim/.test(rows['Accounts removed']) && /signed out/.test(rows['Accounts removed']));
    assert.ok(/alex/.test(rows['Accounts replaced']));
    assert.ok(!w.rec.requests.some(r => r.url === '/api/backup/restore'), 'Cancel must not restore');
    // Nothing is claimed when no account is affected.
    const calm = withBackupEndpoints(settings(BASE, {local: true, chosenFile: FILE, passphrases: ['its passphrase'],
                                                     confirmAnswers: [true, false]}),
      Object.assign({}, inspectSummary, {accounts: {removed: [], replaced: [], added: [], unchanged: 2}}));
    await opened(calm);
    reg(calm, 'cfg_backup_restore').click();
    await settle();
    const quiet = calm.confirms[1];
    assert.ok(!/will be removed/.test(quiet.message));
    assert.ok(!quiet.summary.some(r => /Accounts (removed|replaced)/.test(r.label)));
  });

  await scenario('confirming a restore sends the file and passphrase to the restore endpoint, then says what to do next', async () => {
    const w = withBackupEndpoints(settings(BASE, {local: true, chosenFile: FILE, passphrases: ['its passphrase'],
                                                  confirmAnswers: [true, true, true]}));
    await opened(w);
    reg(w, 'cfg_backup_restore').click();
    await settle();
    const restore = w.rec.requests.find(r => r.url === '/api/backup/restore');
    assert.ok(restore, 'restore was requested after confirmation');
    assert.deepStrictEqual(plain(restore.form), [['backup', 'site.lumabak'], ['passphrase', 'its passphrase']]);
    assert.strictEqual(reg(w, 'cfg_backup_status').textContent, 'Backup restored.');
    const done = w.confirms[2];
    assert.strictEqual(done.title, 'Restore complete');
    assert.strictEqual(done.cancelText, null, 'an acknowledgement offers one button');
    assert.ok(/Run a Scan/.test(done.message) && /administrator from the backup/.test(done.message));
    assert.ok(!/RESTORED/.test(JSON.stringify(done)), 'no internal status text reaches the operator');
  });

  await scenario('cancelling the passphrase, or choosing no file, sends nothing', async () => {
    let w = withBackupEndpoints(settings(BASE, {local: true, chosenFile: FILE, confirmAnswers: [false]}));
    await opened(w);
    let n = w.rec.requests.length;
    reg(w, 'cfg_backup_restore').click();
    await settle();
    assert.strictEqual(w.rec.requests.length, n);
    w = withBackupEndpoints(settings(BASE, {local: true}));        // the chooser never answers
    await opened(w);
    n = w.rec.requests.length;
    reg(w, 'cfg_backup_restore').click();
    await settle();
    assert.strictEqual(w.rec.requests.length, n);
    assert.strictEqual(w.confirms.length, 0, 'no dialog without a file');
  });

  await scenario('a backup that cannot be opened is reported where the operator is looking', async () => {
    const w = settings(BASE, {local: true, chosenFile: FILE, passphrases: ['wrong one'], confirmAnswers: [true]});
    await opened(w);
    const original = w.sandbox.fetch;
    w.sandbox.fetch = (u, i) => u === '/api/backup/inspect'
      ? Promise.resolve({ok: false, status: 400, json: () => Promise.resolve({ok: false, error: 'That passphrase does not open this backup.'})})
      : original(u, i);
    reg(w, 'cfg_backup_restore').click();
    await settle();
    assert.ok(/does not open this backup/.test(reg(w, 'cfg_backup_status').textContent));
    assert.strictEqual(w.confirms.length, 1, 'no decision is offered about a backup that was not read');
    assert.ok(!w.rec.requests.some(r => r.url === '/api/backup/restore'));
  });

  await scenario('a dialog that is not available refuses the action instead of using a native one', async () => {
    const w = settings(BASE, {local: true, chosenFile: FILE});
    await opened(w);
    delete w.sandbox.window.lumaConfirm;
    const n = w.rec.requests.length;
    reg(w, 'cfg_backup_export').click();
    await settle();
    assert.strictEqual(w.rec.requests.length, n);
    assert.ok(/Cannot ask for a passphrase/.test(reg(w, 'cfg_backup_status').textContent));
  });

  console.log(passed + ' scenarios passed' + (process.exitCode ? ' (with failures)' : ''));
})();
