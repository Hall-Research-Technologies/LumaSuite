// Video Wall: routing is immediate (no Route/Cancel pending card), and the Selected Display
// editor carries a Video Wall Mode control that shows what the decoder reports.
//
// ui/wall.js is loaded into a vm sandbox with a tolerant fake DOM and a recording fetch, and the
// real click-path functions are driven: arm a source, pick a target, inspect the requests actually
// made. No socket is opened; nothing here can reach a device.
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const read = f => fs.readFileSync(path.join(__dirname, '..', f), 'utf8');
const stripComments = s => s.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:'"`\\])\/\/.*$/gm, '$1');
const WALL_JS = read('ui/wall.js');
const WALL_HTML = read('ui/wall.html');
const MANUAL = read('ui/manual.html');
const tick = () => new Promise(r => setTimeout(r, 2));
async function settle(n = 12) { for (let i = 0; i < n; i++) await tick(); }
const plain = v => JSON.parse(JSON.stringify(v));

// ---- a tolerant fake element --------------------------------------------------------------
function el(id) {
  const node = {
    id, hidden: false, value: '', checked: false, innerHTML: '', textContent: '', disabled: false,
    style: {}, dataset: {}, children: [], options: [],
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    addEventListener() {}, removeEventListener() {}, setAttribute() {}, appendChild(c) { node.children.push(c); return c; },
    querySelector(sel) { return sel === '.route-bar-text' ? (node._text || (node._text = el('text'))) : el('q'); },
    querySelectorAll: () => [], closest: () => null, focus() {}, getBoundingClientRect: () => ({ left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0 }),
  };
  return node;
}

function page(routes) {
  const nodes = {};
  const requests = [];
  const document = {
    getElementById: id => nodes[id] || (nodes[id] = el(id)),
    createElement: t => el(t), addEventListener() {}, querySelectorAll: () => [], querySelector: () => null,
    body: el('body'), documentElement: el('html'),
  };
  async function fetch(url, init) {
    init = init || {};
    const method = init.method || 'GET';
    const body = init.body ? JSON.parse(init.body) : undefined;
    requests.push({ url, method, body });
    const handler = routes[method + ' ' + url];
    const spec = typeof handler === 'function' ? handler(body) : (handler || { status: 200, body: { ok: true } });
    return { ok: spec.status < 300, status: spec.status, json: async () => spec.body };
  }
  const sandbox = {
    window: { setTimeout: (fn) => 0, clearTimeout() {}, addEventListener() {} },
    document, fetch, console, Date, Math, Number, String, Map, Set, Array, Object, Promise, JSON,
    Image: function () {}, localStorage: { getItem: () => null, setItem() {} }, URLSearchParams,
    setTimeout, clearTimeout, requestAnimationFrame: fn => 0, getComputedStyle: () => ({}), navigator: {},
  };
  sandbox.window.document = document;
  vm.createContext(sandbox);
  vm.runInContext(WALL_JS, sandbox);
  return { hooks: sandbox.window.__videoWallTest, nodes, requests };
}

const ENC = [{ id: 'mac:enc1', ip: '192.0.2.10', hostname: 'Alpha', label: 'Alpha', streamname: 's1' }];
const DEC = [{ id: 'mac:d1', ip: '198.51.100.21', hostname: 'Lobby One', label: 'Lobby One' },
             { id: 'mac:d2', ip: '198.51.100.22', hostname: 'Lobby Two', label: 'Lobby Two' },
             { id: 'mac:d3', ip: '198.51.100.23', hostname: 'Lobby Three', label: 'Lobby Three' },
             { id: 'mac:d4', ip: '198.51.100.24', hostname: 'Lobby Four', label: 'Lobby Four' }];

function freshPage(routes) {
  const p = page(routes);
  const wall = p.hooks.createWall('Atrium', 2, 2);
  wall.id = 'wall-test';
  wall.cells.forEach((c, i) => { c.decoder_id = DEC[i].id; });
  p.hooks.__setWallStateForTest(wall, ENC, DEC, '1:1');
  return { ...p, wall };
}

const okRoutes = {
  'POST /api/video_wall/source': { status: 200, body: { ok: true, results: {} } },
  'POST /api/video_wall/save': { status: 200, body: { ok: true, wall: null } },
  'POST /api/video_wall/refresh': { status: 200, body: { ok: true, reports: {} } },
};
function routesFor(wall, extra) {
  return { ...okRoutes, 'POST /api/video_wall/save': () => ({ status: 200, body: { ok: true, wall } }), ...(extra || {}) };
}
const posts = (p, u) => p.requests.filter(r => r.method === 'POST' && r.url === u);

const scenarios = [];
const scenario = (name, fn) => scenarios.push([name, fn]);

// ---- the pending confirmation cannot come back -------------------------------------------
scenario('the page has no Route or Cancel button and no pending state', async () => {
  const html = stripComments(WALL_HTML), js = stripComments(WALL_JS);
  for (const gone of ['nonDragRouteBtn', 'nonDragRouteCancelBtn']) {
    assert.ok(!html.includes(gone), `wall.html still has ${gone}`);
    assert.ok(!js.includes(gone), `wall.js still references ${gone}`);
  }
  for (const gone of ['commitArmedRoute', 'armedTarget']) assert.ok(!js.includes(gone), `wall.js still has ${gone}`);
  const bar = WALL_HTML.slice(WALL_HTML.indexOf('id="nonDragRouteBar"'), WALL_HTML.indexOf('id="nonDragRouteBar"') + 300);
  assert.ok(!/<button/i.test(bar.slice(0, bar.indexOf('</div>'))), 'the selected-source hint must hold no button');
  assert.ok(!/press Route|click Route/i.test(WALL_HTML), 'wall.html still tells the operator to press Route');
});

scenario('manual no longer tells the operator to press Route for walls', async () => {
  const start = MANUAL.indexOf('Physical Wall');
  const wallSection = MANUAL.slice(start, MANUAL.indexOf('</section>', start));
  assert.ok(!/press Route/i.test(wallSection), 'wall section still tells the operator to press Route');
  assert.ok(/Video Wall Mode/.test(wallSection) && /Partial/.test(wallSection), 'wall section must describe Video Wall Mode and the Partial result');
  assert.ok(/as soon as you click the target/i.test(wallSection), 'wall section must say routing is immediate');
});

scenario('source then single display: the route request is sent at once, nothing pending', async () => {
  const p = freshPage();
  p.hooks.armSourceForNonDragRouting('mac:enc1', null);
  assert.equal(p.requests.length, 0, 'selecting a source alone sends nothing');
  p.hooks.routeArmedSourceTo([{ row: 1, column: 2 }], 'R1 C2');
  await settle();
  const sent = posts(p, '/api/video_wall/source');
  assert.equal(sent.length, 1, 'the click on the target is the route');
  assert.equal(sent[0].body.scope, 'cell');
  assert.deepEqual([sent[0].body.row, sent[0].body.column], [1, 2]);
  const cell = sent[0].body.wall.cells.find(c => c.row === 1 && c.column === 2);
  assert.equal(cell.override_source_id, 'mac:enc1');
});

scenario('a second target click without re-selecting a source does not route again', async () => {
  const p = freshPage();
  p.hooks.armSourceForNonDragRouting('mac:enc1', null);
  p.hooks.routeArmedSourceTo([{ row: 1, column: 1 }], 'R1 C1');
  p.hooks.routeArmedSourceTo([{ row: 2, column: 2 }], 'R2 C2');
  await settle();
  assert.equal(posts(p, '/api/video_wall/source').length, 1);
});

scenario('a zone (row) target routes immediately as a region', async () => {
  const p = freshPage();
  p.hooks.armSourceForNonDragRouting('mac:enc1', null);
  p.hooks.routeArmedSourceTo([{ row: 1, column: 1 }, { row: 1, column: 2 }], 'Top Row');
  await settle();
  const sent = posts(p, '/api/video_wall/source');
  assert.equal(sent.length, 1);
  assert.equal(sent[0].body.scope, 'region');
  assert.equal(sent[0].body.wall.regions[0].source_id, 'mac:enc1');
});

scenario('Entire Wall routes immediately through the wall source', async () => {
  const p = freshPage();
  p.hooks.armSourceForNonDragRouting('mac:enc1', null);
  const all = p.wall.cells.map(c => ({ row: c.row, column: c.column }));
  p.hooks.routeArmedSourceTo(all, 'Entire Wall');
  await settle();
  const sent = posts(p, '/api/video_wall/source');
  assert.equal(sent.length, 1);
  assert.equal(sent[0].body.scope, 'wall');
});

scenario('with no source selected a target click routes nothing', async () => {
  const p = freshPage();
  p.hooks.routeArmedSourceTo([{ row: 1, column: 1 }], 'R1 C1');
  await settle();
  assert.equal(p.requests.length, 0);
});

scenario('a server refusal is still reported, not swallowed', async () => {
  const p = freshPage({ 'POST /api/video_wall/source': { status: 409, body: { ok: false, error: 'display is offline' } } });
  p.hooks.armSourceForNonDragRouting('mac:enc1', null);
  p.hooks.routeArmedSourceTo([{ row: 1, column: 1 }], 'R1 C1');
  await settle();
  assert.equal(p.nodes.toast.textContent, 'display is offline');
});

// ---- Video Wall Mode control ---------------------------------------------------------------
scenario('an unread decoder shows Unknown, never a guessed state, even if it is a saved member', async () => {
  const p = freshPage();
  p.hooks.renderDetails();
  const html = p.nodes.details.innerHTML;
  assert.match(html, /Video Wall Mode/);
  assert.match(html, /data-action="wall-mode"/);
  assert.match(html, /Unknown/);
  assert.doesNotMatch(html, /<option value="enabled" selected/);
  assert.doesNotMatch(html, /<option value="disabled" selected/);
});

scenario('the control shows what the decoder reported, and a learned read', async () => {
  const p = freshPage();
  const cell = p.wall.cells[0];
  assert.equal(p.hooks.wallModeFor(cell, { video_wall: { vmenable: true } }).state, 'enabled');
  assert.equal(p.hooks.wallModeFor(cell, { video_wall: { vmenable: false } }).state, 'disabled');
  assert.equal(p.hooks.wallModeFor(cell, { video_wall: { vmenable: 'yes' } }).state, 'unknown', 'a non-boolean flag is unknown');
  // Direct: a learned read is what the control shows.
  const q = freshPage({ 'POST /api/video_wall/decoder_mode': { status: 200, body: { ok: true, wall_mode: { enabled: true, vwmatrix: '2_2_1_1' } } } });
  await q.hooks.readDecoderWallMode(q.wall.cells[0]);
  q.hooks.renderDetails();
  assert.match(q.nodes.details.innerHTML, /<option value="enabled" selected/);
  assert.equal(q.hooks.wallModeFor(q.wall.cells[0], {}).state, 'enabled');
});

scenario('choosing Enabled posts the real write and shows the verified read-back', async () => {
  const p = freshPage({ 'POST /api/video_wall/decoder_mode': b => ({ status: 200, body: { ok: true, wall_mode: { enabled: b.enabled, vwmatrix: '2_2_1_1' } } }) });
  await p.hooks.setDecoderWallMode(p.wall.cells[0], true);
  const sent = posts(p, '/api/video_wall/decoder_mode');
  assert.equal(sent.length, 1);
  assert.deepEqual(plain(sent[0].body), { decoder_id: 'mac:d1', enabled: true, wall_id: 'wall-test' });
  assert.equal(p.hooks.wallModeFor(p.wall.cells[0], {}).state, 'enabled');
  assert.match(p.nodes.toast.textContent, /enabled on .*confirmed by the decoder/);
});

scenario('a write the decoder did not hold shows the real state and the reason', async () => {
  const p = freshPage({ 'POST /api/video_wall/decoder_mode': { status: 502, body: { ok: false, reason: 'not_verified',
    error: 'decoder reports wall mode disabled, expected enabled', wall_mode: { enabled: false, vwmatrix: '1_1_1_1' } } } });
  await p.hooks.setDecoderWallMode(p.wall.cells[0], true);
  assert.equal(p.hooks.wallModeFor(p.wall.cells[0], {}).state, 'disabled', 'shows what the decoder holds, not what was asked');
  assert.match(p.nodes.toast.textContent, /not changed/);
  p.hooks.renderDetails();
  assert.match(p.nodes.details.innerHTML, /Not changed: decoder reports wall mode disabled/);
});

scenario('an unreadable decoder stays Unknown with the reason', async () => {
  const p = freshPage({ 'POST /api/video_wall/decoder_mode': { status: 502, body: { ok: false, error: 'no response' } } });
  await p.hooks.readDecoderWallMode(p.wall.cells[0]);
  assert.equal(p.hooks.wallModeFor(p.wall.cells[0], {}).state, 'unknown');
  p.hooks.renderDetails();
  assert.match(p.nodes.details.innerHTML, /Unknown: no response/);
});

// ---- Apply to Entire Wall: truthful aggregate -------------------------------------------------
scenario('apply outcome text: complete, partial naming the member, failed, empty', async () => {
  const { hooks } = freshPage();
  const complete = hooks.applyOutcomeText({ members: 2, succeeded: 2, failed_members: [] });
  assert.deepEqual([complete.ok, /verified on 2 of 2/.test(complete.text)], [true, true]);
  const partial = hooks.applyOutcomeText({ members: 2, succeeded: 1, failed_members: [{ label: 'Lobby Two', reason: 'verify wall mode: decoder reports wall mode disabled' }] });
  assert.equal(partial.ok, false);
  assert.match(partial.text, /^Partial: 1 of 2/);
  assert.match(partial.text, /Lobby Two: verify wall mode/);
  assert.match(hooks.applyOutcomeText({ members: 2, succeeded: 0, failed_members: [{ label: 'A', reason: 'x' }, { label: 'B', reason: 'y' }] }).text, /^Failed: .*\(\+1 more\)/);
  assert.equal(hooks.applyOutcomeText({ members: 0, succeeded: 0, failed_members: [] }).ok, false);
});

scenario('Apply posts the saved wall and reports Partial from the server aggregate', async () => {
  const p = freshPage();
  const wall = p.wall;
  p.requests.length = 0;
  const q = page({
    'POST /api/video_wall/save': () => ({ status: 200, body: { ok: true, wall } }),
    'POST /api/video_wall/apply': { status: 200, body: { ok: false, status: 'partial', members: 2, succeeded: 1,
      failed_members: [{ decoder_id: 'mac:d2', label: 'Lobby Two', reason: 'verify wall mode: not held' }], results: {} } },
    'POST /api/video_wall/refresh': { status: 200, body: { ok: true, reports: {} } },
  });
  q.hooks.__setWallStateForTest(wall, ENC, DEC, '1:1');
  await q.hooks.applyWall();
  await settle();
  assert.equal(posts(q, '/api/video_wall/apply').length, 1);
  assert.match(q.nodes.toast.textContent, /^Partial: 1 of 2 .*Lobby Two/);
});

(async () => {
  let failed = 0;
  for (const [name, fn] of scenarios) {
    try {
      await Promise.race([fn(), new Promise((_, rej) => setTimeout(() => rej(new Error('scenario timed out')), 5000))]);
      console.log('ok   ' + name);
    } catch (e) {
      failed++;
      console.log('FAIL ' + name + '\n     ' + (e && e.message));
    }
  }
  console.log(`${scenarios.length - failed} scenarios passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
})();
