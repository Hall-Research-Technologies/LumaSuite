// The account editor's "Routing interface" choice, driven for real: admin_users.js runs
// against a small DOM and a recording fetch, and the requests the Save button actually makes
// are inspected. An account that predates the setting opens as Standard only; at least one
// interface must stay ticked; the choice travels inside the permissions the server stores.
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

function editor(perms) {
  const {byId, root} = pageFor(HTML);
  const requests = [];
  const users = [{id: 'u1', username: 'root', role: 'admin', enabled: true, permissions: {}, direct_access: {enabled: false}},
                 {id: 'u2', username: 'alex', role: 'user', enabled: true, permissions: perms,
                  direct_access: {enabled: false}}];
  const routes = {
    'GET /api/admin/users': {status: 200, body: {ok: true, users}},
    'GET /api/admin/devices': {status: 200, body: {ok: true, duplicate_aliases: [], devices: [
      {id: '02:00:5e:10:00:0a', role: 'encoder', name: 'Alpha Camera', device_name: 'Alpha Camera'},
      {id: '02:00:5e:20:00:01', role: 'decoder', name: 'Lobby One', device_name: 'Lobby One'}]}},
    'GET /api/admin/groups': {status: 200, body: {ok: true, groups: []}},
    'GET /api/admin/walls': {status: 200, body: {ok: true, walls: []}},
    'POST /api/admin/users/u2': {status: 200, body: {ok: true}},
    'POST /api/admin/users': {status: 200, body: {ok: true}},
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
  const doc = {getElementById: id => byId[id], createElement: t => new Node(t), hidden: false,
               addEventListener() {}, querySelector: () => null, querySelectorAll: () => []};
  const sandbox = {document: doc, window: {toast() {}, lumaConfirm: () => Promise.resolve(true), addEventListener() {}},
    fetch, location: {replace() {}}, navigator: {}, URLSearchParams, Set, Promise, Date, JSON, Object, Array, console,
    setTimeout, clearTimeout, setInterval() { return 0; }, clearInterval() {}, encodeURIComponent};
  sandbox.window.window = sandbox.window;
  vm.createContext(sandbox);
  vm.runInContext(read('ui/admin_users.js'), sandbox);
  return {byId, root, requests};
}

const buttons = node => { const out = []; const walk = n => n.children.forEach(c => { if (c.tagName === 'button') out.push(c); walk(c); }); walk(node); return out; };
const editAlex = a => buttons(a.byId.user_table).filter(b => b.textContent === 'Edit')[1].click();
const saved = (a, before) => a.requests.slice(before).filter(r => r.method === 'POST' && r.url === '/api/admin/users/u2')[0];

(async () => {
  let passed = 0;
  async function scenario(name, fn) {
    try { await fn(); passed++; }
    catch (err) { console.error('FAILED: ' + name + '\n  ' + (err && err.stack || err)); process.exitCode = 1; }
  }

  await scenario('the editor has a Routing interface choice with both interfaces named', async () => {
    assert.ok(/<legend>Routing interface<\/legend>/.test(HTML));
    assert.ok(/id="u_standard"/.test(HTML) && /id="u_matrix"/.test(HTML));
    assert.ok(/Standard routing/.test(HTML) && /Matrix routing/.test(HTML));
    assert.ok(!/drag/i.test(HTML.match(/<legend>Routing interface<\/legend>[\s\S]*?<\/fieldset>/)[0].replace(/with drag and drop/, '')),
              'drag and drop is not a third choice');
  });

  await scenario('an account stored before the setting existed opens as Standard only', async () => {
    const a = editor({sources: ['02:00:5e:10:00:0a'], destinations: ['02:00:5e:20:00:01'], groups: [], walls: []});
    await settle();
    editAlex(a);
    await settle();
    assert.strictEqual(a.byId.u_standard.checked, true);
    assert.strictEqual(a.byId.u_matrix.checked, false);
  });

  await scenario('an account that holds Matrix only opens with only Matrix ticked', async () => {
    const a = editor({sources: [], destinations: [], groups: [], walls: [], standard_routing: false, matrix_routing: true});
    await settle();
    editAlex(a);
    await settle();
    assert.strictEqual(a.byId.u_standard.checked, false);
    assert.strictEqual(a.byId.u_matrix.checked, true);
  });

  await scenario('ticking Matrix and saving sends both choices inside the permissions', async () => {
    const a = editor({sources: ['02:00:5e:10:00:0a'], destinations: [], groups: [], walls: []});
    await settle();
    editAlex(a);
    await settle();
    a.byId.u_matrix.checked = true;
    const before = a.requests.length;
    a.byId.u_save.click();
    await settle();
    const post = saved(a, before);
    assert.ok(post, 'a permissions request was sent');
    const perms = plain(post.body.permissions);
    assert.strictEqual(post.body.action, 'set_permissions');
    assert.strictEqual(perms.standard_routing, true);
    assert.strictEqual(perms.matrix_routing, true);
    assert.deepStrictEqual(perms.sources, ['02:00:5e:10:00:0a'], 'the grants are untouched by the choice');
  });

  await scenario('unticking Standard keeps Matrix and sends exactly that', async () => {
    const a = editor({sources: [], destinations: [], groups: [], walls: [], matrix_routing: true});
    await settle();
    editAlex(a);
    await settle();
    a.byId.u_standard.checked = false;
    const before = a.requests.length;
    a.byId.u_save.click();
    await settle();
    const perms = plain(saved(a, before).body.permissions);
    assert.strictEqual(perms.standard_routing, false);
    assert.strictEqual(perms.matrix_routing, true);
  });

  await scenario('neither ticked: nothing is sent and the editor says why', async () => {
    const a = editor({sources: [], destinations: [], groups: [], walls: []});
    await settle();
    editAlex(a);
    await settle();
    a.byId.u_standard.checked = false;
    a.byId.u_matrix.checked = false;
    const before = a.requests.length;
    a.byId.u_save.click();
    await settle();
    assert.strictEqual(saved(a, before), undefined, 'a request that can only be refused is not sent');
    assert.ok(/at least one routing interface/i.test(a.byId.u_error.textContent));
    assert.strictEqual(a.byId.u_save.disabled, false, 'Save stays available once the choice is fixed');
  });

  await scenario('the accounts table says which interface each account has', async () => {
    const a = editor({sources: [], destinations: [], groups: [], walls: [], standard_routing: true, matrix_routing: true});
    await settle();
    assert.ok(/Standard \+ Matrix/.test(a.byId.user_table.textContent));
  });

  console.log(passed + ' scenarios passed' + (process.exitCode ? ' (with failures)' : ''));
})();
