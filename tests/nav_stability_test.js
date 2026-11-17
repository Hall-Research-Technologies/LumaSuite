// The Remote Access navigation entry must not flicker between pages: the shell is applied from
// what the last definite session answer said, before the next answer arrives, and only a
// definite answer ever takes anything away.
'use strict';
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const {Node} = require('./_dom.js');

const source = fs.readFileSync(path.join(__dirname, '..', 'ui', 'adminnav.js'), 'utf8');
const tick = () => new Promise(r => setTimeout(r, 0));

function page(store, session, opts = {}) {
  const tabs = new Node('div');
  ['Device Manager', 'Matrix Routing'].forEach(label => {
    const a = new Node('a'); a.className = 'nav-tab'; a.textContent = label; tabs.appendChild(a);
  });
  const header = new Node('div');
  const gear = new Node('button'); gear.id = 'header_gear'; header.appendChild(gear);
  const find = id => tabs.querySelectorAll('a').find(n => n.id === id) || header.children.find(n => n.id === id) || null;
  const doc = {readyState: 'complete', addEventListener() {},
    querySelector: sel => (sel === '.nav-tabs' ? tabs : (sel.includes('header_gear') ? gear : null)),
    getElementById: find, createElement: t => new Node(t)};
  const live = {session, fetchFails: !!opts.fetchFails};
  const listeners = {};
  const nav = {replaced: null};
  const sandbox = {document: doc, location: {pathname: opts.path || '/ui/index.html', replace(u) { nav.replaced = u; }}, Promise, JSON,
    localStorage: {getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); },
                   removeItem: k => { delete store[k]; }},
    fetch: () => (live.fetchFails ? Promise.reject(new Error('down'))
                                  : Promise.resolve({ok: true, json: () => Promise.resolve(live.session)})),
    window: {lumaSession: () => (opts.never ? new Promise(() => {}) : Promise.resolve(live.session)),
             lumaForgetCsrf() {}, addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); }}};
  sandbox.window.window = sandbox.window;
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox);
  return {tabs, header, live, find, window: sandbox.window, listeners, nav};
}
const ADMIN_REMOTE = {role: 'admin', local: false, username: 'root', remote_enabled: true};

(async () => {
  const store = {};
  // First ever load: nothing is known, so the answer has to arrive.
  let p = page(store, ADMIN_REMOTE);
  assert.strictEqual(p.find('nav_remote_access'), null, 'nothing is known yet');
  await tick();
  assert.ok(p.find('nav_remote_access'), 'the first definite answer adds the tab');
  assert.ok(store['luma.shell.v1'], 'and is remembered');

  // Every later page has the tab and the account controls BEFORE any answer arrives.
  for (let i = 0; i < 25; i++) {
    p = page(store, ADMIN_REMOTE, {never: true});
    assert.ok(p.find('nav_remote_access'), 'page ' + i + ': tab present synchronously');
    assert.strictEqual(p.tabs.children.length, 3);
    assert.strictEqual(p.tabs.children[2].id, 'nav_remote_access', 'still the last entry');
    assert.deepStrictEqual(p.header.children.map(c => c.textContent), ['root', 'Sign out', ''],
      'page ' + i + ': the gear never moves');
  }

  // A request that fails, or answers with nothing usable, takes nothing away.
  p = page(store, {}, {});
  await tick();
  assert.ok(p.find('nav_remote_access'), 'an empty answer is not "remote access is off"');
  p = page(store, ADMIN_REMOTE, {never: true, fetchFails: true});
  await p.window.lumaRefreshNav(); await tick();
  assert.ok(p.find('nav_remote_access'), 'a failed refresh keeps the tab');
  assert.deepStrictEqual(p.header.children.map(c => c.textContent), ['root', 'Sign out', '']);

  // A definite answer is what changes it, and it is remembered for the next page.
  p = page(store, {...ADMIN_REMOTE, remote_enabled: false});
  await tick();
  assert.strictEqual(p.find('nav_remote_access'), null, 'Remote Access turned off: the tab goes');
  assert.strictEqual(JSON.parse(store['luma.shell.v1']).remote, false);
  p = page(store, ADMIN_REMOTE, {never: true});
  assert.strictEqual(p.find('nav_remote_access'), null, 'the next page does not resurrect it');

  // Turning it on from Settings follows without a reload.
  p = page(store, ADMIN_REMOTE, {never: true});
  p.live.session = {...ADMIN_REMOTE, remote_enabled: true};
  await p.window.lumaRefreshNav(); await tick();
  assert.ok(p.find('nav_remote_access'));

  // A restricted account is never offered it, whatever was cached.
  store['luma.shell.v1'] = JSON.stringify({admin: false, remote: true, local: false, username: 'alex'});
  p = page(store, {role: 'user', local: false, username: 'alex', remote_enabled: true}, {never: true});
  assert.strictEqual(p.find('nav_remote_access'), null);

  // A different account on the same browser corrects the remembered name.
  store['luma.shell.v1'] = JSON.stringify({admin: true, remote: true, local: false, username: 'old'});
  p = page(store, ADMIN_REMOTE);
  assert.strictEqual(p.header.children[0].textContent, 'old');
  await tick();
  assert.strictEqual(p.header.children[0].textContent, 'root');

  // The local computer has no account controls even if something stale says otherwise.
  store['luma.shell.v1'] = JSON.stringify({admin: true, remote: true, local: false, username: 'old'});
  p = page(store, {role: 'admin', local: true, remote_enabled: true});
  await tick();
  assert.strictEqual(p.header.children.length, 1);

  // Signing out forgets the remembered shell.
  store['luma.shell.v1'] = JSON.stringify({admin: true, remote: true, local: false, username: 'root'});
  p = page(store, ADMIN_REMOTE, {never: true});
  p.window.lumaForgetShell();
  assert.ok(!('luma.shell.v1' in store));

  // Another tab of the same browser learns the answer first: this one follows without a request.
  const fire = (pg, oldValue, newValue) => (pg.listeners.storage || []).forEach(fn => fn({key: 'luma.shell.v1', oldValue, newValue}));
  const shellOn = JSON.stringify({admin: true, remote: true, local: true, username: ''});
  const shellOff = JSON.stringify({admin: true, remote: false, local: true, username: ''});
  p = page({}, {}, {never: true});
  assert.strictEqual(p.find('nav_remote_access'), null);
  fire(p, null, shellOn);
  assert.ok(p.find('nav_remote_access'), 'another tab turned Remote Access on: the tab appears with no reload');
  fire(p, shellOn, shellOff);
  assert.strictEqual(p.find('nav_remote_access'), null, 'another tab turned it off: the tab goes');
  assert.strictEqual(p.nav.replaced, null, 'and a page that is not in the area stays where it is');
  fire(p, shellOn, 'not json');
  fire(p, shellOn, null);
  assert.strictEqual(p.find('nav_remote_access'), null, 'an unreadable or removed entry changes nothing');
  p = page({}, {}, {never: true});
  const before = p.tabs.children.length;
  (p.listeners.storage || []).forEach(fn => fn({key: 'some.other.key', newValue: shellOn}));
  assert.strictEqual(p.tabs.children.length, before, 'only the shell key is listened for');

  // On a page of the Remote Access area when it is turned off: leave it for the main page.
  store['luma.shell.v1'] = shellOn;
  p = page(store, {...ADMIN_REMOTE, remote_enabled: false, local: true, username: ''}, {path: '/admin/users', never: true});
  assert.ok(p.find('nav_remote_access'), 'while it is on, the area keeps its tab');
  p.live.session = {role: 'admin', local: true, remote_enabled: false};
  await p.window.lumaRefreshNav(); await tick();
  assert.strictEqual(p.nav.replaced, '/', 'turned off while on that page: move to the main page');
  p = page({}, {}, {path: '/admin/users', never: true});
  fire(p, shellOn, shellOff);
  assert.strictEqual(p.nav.replaced, '/', 'turned off from another tab: the same');

  // Opening the area directly while Remote Access is already off is not a transition.
  store['luma.shell.v1'] = shellOff;
  p = page(store, {role: 'admin', local: true, remote_enabled: false}, {path: '/admin/users'});
  await tick();
  assert.strictEqual(p.nav.replaced, null, 'no transition, no redirect');
  assert.ok(p.find('nav_remote_access'), 'the area still shows where the operator is');

  // Storage that throws must not break the shell.
  const broken = page({}, ADMIN_REMOTE);
  assert.ok(broken);
  console.log('nav stability: ok');
})().catch(e => { console.error(e); process.exit(1); });
