// The account editor's third destination category: saved video walls.
//
// Driven for real: admin_users.js runs against a small DOM and a recording fetch, and the
// requests the Save button actually makes are inspected. A wall is its own list, never mixed
// into the displays, and only saved walls (the ones /api/admin/walls reports) can be chosen.
'use strict';
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {Node, pageFor} = require('./_dom');

const read = f => fs.readFileSync(path.join(__dirname, '..', f), 'utf8');
const HTML = read('ui/admin_users.html');
const plain = v => JSON.parse(JSON.stringify(v));
const tick = () => new Promise(r => setTimeout(r, 5));
async function settle(n = 8) { for (let i = 0; i < n; i++) await tick(); }

const WALL = 'wall-aaaaaaaaaaaa', WALL2 = 'wall-bbbbbbbbbbbb';

function accounts(opts = {}) {
  const {byId, root} = pageFor(HTML);
  const requests = [];
  const users = [{id: 'u1', username: 'root', role: 'admin', enabled: true, permissions: {}, direct_access: {enabled: false}},
                 {id: 'u2', username: 'alex', role: 'user', enabled: true,
                  permissions: {sources: ['02:00:5e:10:00:0a'], destinations: ['02:00:5e:20:00:01'], groups: [], walls: opts.have || []},
                  direct_access: {enabled: false}}];
  const routes = {
    'GET /api/admin/users': {status: 200, body: {ok: true, users}},
    'GET /api/admin/devices': {status: 200, body: {ok: true, duplicate_aliases: [], devices: [
      {id: '02:00:5e:10:00:0a', role: 'encoder', name: 'Alpha Camera', device_name: 'Alpha Camera'},
      {id: '02:00:5e:20:00:01', role: 'decoder', name: 'Lobby One', device_name: 'Lobby One'},
      {id: '02:00:5e:20:00:02', role: 'decoder', name: 'Lobby Two', device_name: 'Lobby Two'}]}},
    'GET /api/admin/groups': {status: 200, body: {ok: true, groups: []}},
    'GET /api/admin/walls': opts.noWalls ? {status: 404, body: {ok: false}} : {status: 200, body: {ok: true, walls: [
      {id: WALL, name: 'Atrium Wall', members: 4, online_members: 4, granted_to: []},
      {id: WALL2, name: 'Gallery Wall', members: 2, online_members: 1, granted_to: []}]}},
    'POST /api/admin/users/u2': {status: 200, body: {ok: true}},
  };
  function fetch(url, init) {
    init = init || {};
    const method = init.method || 'GET';
    const record = {url: String(url), method, body: init.body ? JSON.parse(init.body) : undefined};
    requests.push(record);
    const spec = routes[method + ' ' + record.url.split('?')[0]] || {status: 404, body: {ok: false}};
    return Promise.resolve({ok: spec.status < 300, status: spec.status,
                            headers: {get: () => 'application/json'}, json: () => Promise.resolve(spec.body)});
  }
  const doc = {getElementById: id => byId[id], createElement: t => new Node(t),
               addEventListener() {}, querySelector: () => null, querySelectorAll: () => []};
  const sandbox = {document: doc, window: {toast() {}, lumaConfirm: () => Promise.resolve(true)},
    fetch, location: {replace() {}}, navigator: {}, URLSearchParams, Set, Promise, Date, JSON, Object, Array, console,
    setTimeout, clearTimeout, encodeURIComponent};
  sandbox.window.window = sandbox.window;
  vm.createContext(sandbox);
  vm.runInContext(read('ui/admin_users.js'), sandbox);
  return {byId, root, requests};
}

const buttons = node => { const out = []; const walk = n => n.children.forEach(c => { if (c.tagName === 'button') out.push(c); walk(c); }); walk(node); return out; };
const checkboxes = host => host.querySelectorAll('ul')[0].querySelectorAll('input');
const labels = host => host.querySelectorAll('ul')[0].querySelectorAll('label').map(l => l.textContent);
const editAlex = a => buttons(a.byId.user_table).filter(b => b.textContent === 'Edit')[1].click();

(async () => {
  let passed = 0;
  async function scenario(name, fn) {
    try { await fn(); passed++; }
    catch (err) { console.error('FAILED: ' + name + '\n  ' + (err && err.stack || err)); process.exitCode = 1; }
  }

  await scenario('the editor offers saved walls by name, as a category separate from displays and groups', async () => {
    const a = accounts();
    await settle();
    editAlex(a);
    await settle();
    assert.ok(a.byId.assign_walls, 'the page has a saved-walls list');
    assert.deepStrictEqual(labels(a.byId.assign_walls).map(t => t.replace(/\d+ displays?/, '').trim()),
                           ['Atrium Wall', 'Gallery Wall']);
    assert.ok(!labels(a.byId.assign_displays).some(t => /Wall/.test(t)), 'walls are not listed among displays');
    assert.ok(!labels(a.byId.assign_walls).some(t => /Lobby|Alpha/.test(t)), 'displays are not listed among walls');
    assert.ok(/saved video walls/i.test(HTML.match(/id="assign_walls"[\s\S]*?<\/h4>/)[0]));
  });

  await scenario('ticking a wall and saving sends it in its own list, and the other lists are untouched', async () => {
    const a = accounts();
    await settle();
    editAlex(a);
    await settle();
    checkboxes(a.byId.assign_walls)[0].checked = true;
    checkboxes(a.byId.assign_walls)[0].fire('change');
    const before = a.requests.length;
    a.byId.u_save.click();
    await settle();
    const post = a.requests.slice(before).filter(r => r.method === 'POST' && r.url === '/api/admin/users/u2')[0];
    assert.ok(post, 'a permissions request was sent');
    const perms = plain(post.body.permissions);
    assert.strictEqual(post.body.action, 'set_permissions');
    assert.deepStrictEqual(perms.walls, [WALL]);
    assert.deepStrictEqual(perms.destinations, ['02:00:5e:20:00:01'], 'granting a wall adds no display');
    assert.deepStrictEqual(perms.sources, ['02:00:5e:10:00:0a']);
    assert.ok(!JSON.stringify(perms.destinations).includes('wall-'));
  });

  await scenario('an account that already holds a wall opens with it ticked, and unticking removes it', async () => {
    const a = accounts({have: [WALL2]});
    await settle();
    editAlex(a);
    await settle();
    const boxes = checkboxes(a.byId.assign_walls);
    assert.deepStrictEqual(boxes.map(b => b.checked), [false, true]);
    boxes[1].checked = false; boxes[1].fire('change');
    const before = a.requests.length;
    a.byId.u_save.click();
    await settle();
    const post = a.requests.slice(before).filter(r => r.method === 'POST')[0];
    assert.deepStrictEqual(plain(post.body.permissions).walls, []);
  });

  await scenario('the accounts list counts walls only when an account has some', async () => {
    const none = accounts();
    await settle();
    assert.ok(!/wall/.test(none.byId.user_table.textContent));
    const some = accounts({have: [WALL, WALL2]});
    await settle();
    assert.ok(/2 walls/.test(some.byId.user_table.textContent));
  });

  await scenario('a host without the walls list still opens the editor, with an empty walls category', async () => {
    const a = accounts({noWalls: true});
    await settle();
    editAlex(a);
    await settle();
    assert.strictEqual(checkboxes(a.byId.assign_walls).length, 0);
    assert.ok(a.byId.u_save, 'the editor is usable');
  });

  console.log(passed + ' scenarios passed' + (process.exitCode ? ' (with failures)' : ''));
})();
