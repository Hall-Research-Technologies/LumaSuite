// The restricted page's two routing interfaces, driven for real: user.js runs against a small
// DOM stub and a recording fetch, and the tests inspect the requests a click actually makes.
//
// The tab choice, the matrix grid, the request a cell sends and the page's reaction to an
// administrator's change are all measured here -- source-text assertions would prove only
// that the words are in the file. Scenarios share a DOM and a request log, so they run one
// after another, never overlapping.
'use strict';
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const ROOT = path.join(__dirname, '..');
const SOURCE = fs.readFileSync(path.join(ROOT, 'ui', 'user.js'), 'utf8');
const HTML = fs.readFileSync(path.join(ROOT, 'ui', 'user.html'), 'utf8');
const {Node, pageFor} = require('./_dom');

const A = '02:00:5e:10:00:0a', B = '02:00:5e:10:00:0b';
const D1 = '02:00:5e:20:00:01', D2 = '02:00:5e:20:00:02';
const GRP = 'grp_0123456789abcdef', WALL = 'wall-aaaaaaaaaaaa';

function harness(options = {}) {
  const {byId, root} = pageFor(HTML);
  const requests = [], toasts = [], timers = [], intervals = [], revoked = [], aborts = [];
  const store = {luma_csrf: 'csrf-token-1'};
  let replaced = null;
  const routes = options.routes || {};
  function fetch(url, init) {
    init = init || {};
    const record = {url: String(url), method: init.method || 'GET',
                    headers: init.headers || {}, body: init.body ? JSON.parse(init.body) : undefined};
    requests.push(record);
    const handler = routes[record.method + ' ' + record.url.split('?')[0]];
    let answer;
    try { answer = typeof handler === 'function' ? handler(record) : handler; }
    catch (err) { return Promise.reject(err); }
    return Promise.resolve(answer).then(got => {
      const spec = got || {status: 404, body: {ok: false}};
      return {ok: spec.status >= 200 && spec.status < 300, status: spec.status,
              headers: {get: h => (h.toLowerCase() === 'content-type' ? (spec.type || 'application/json') : null)},
              json: () => Promise.resolve(spec.body === undefined ? {} : spec.body),
              blob: () => Promise.resolve({size: 4})};
    });
  }
  const listeners = {};
  const sandbox = {
    document: {getElementById: id => byId[id] || null, createElement: tag => new Node(tag),
               createTextNode: t => { const n = new Node('#text'); n._text = t; return n; },
               querySelector: sel => root.querySelector(sel), querySelectorAll: sel => root.querySelectorAll(sel),
               hidden: false, addEventListener: (type, fn) => { (listeners[type] = listeners[type] || []).push(fn); }},
    window: {addEventListener: (type, fn) => { (listeners[type] = listeners[type] || []).push(fn); },
             toast: (m, k) => toasts.push([m, k]), lumaConfirm: () => Promise.resolve(true),
             lumaRemember: options.remember, lumaForgetCsrf() {}},
    sessionStorage: {getItem: k => store[k] || null, setItem: (k, v) => { store[k] = v; }, removeItem: k => { delete store[k]; }},
    location: {pathname: '/user', search: '', reload() {}, replace(u) { replaced = u; }},
    fetch, console, JSON, Promise, URL: {createObjectURL: () => 'blob:x', revokeObjectURL: u => revoked.push(u)},
    AbortController: class { constructor() { this.signal = {}; } abort() { aborts.push(1); } },
    CSS: {escape: s => String(s)},
    Image: class extends Node { constructor() { super('img'); this.onload = null; this.onerror = null; }
      set src(v) { this._src = v; setTimeout(() => this.onload && this.onload(), 0); } },
    setInterval: (fn, ms) => { intervals.push([fn, ms]); return intervals.length; },
    clearInterval: id => { if (intervals[id - 1]) intervals[id - 1] = null; },
    setTimeout: (fn, ms) => { timers.push([fn, ms, true]); return timers.length; },
    clearTimeout: id => { if (timers[id - 1]) timers[id - 1][2] = false; },
    encodeURIComponent, Date,
  };
  sandbox.window.window = sandbox.window;
  vm.createContext(sandbox);
  vm.runInContext(SOURCE, sandbox, {filename: 'user.js'});
  return {byId, root, requests, toasts, timers, intervals, listeners, store, revoked, aborts,
          get replaced() { return replaced; }};
}

const tick = () => new Promise(r => global.setTimeout(r, 5));
async function settle(n = 8) { for (let i = 0; i < n; i++) await tick(); }
const revisionTick = h => h.intervals.filter(Boolean).filter(i => i[1] === 20000)[0][0];
const pendingTimers = h => h.timers.filter(t => t[2]);
function memory(seed = {}) {
  const data = Object.assign({}, seed);
  return {get: k => data[k] || '', set: (k, v) => { if (v) data[k] = String(v); else delete data[k]; }, data};
}

const STATE = routing => ({ok: true, role: 'user', username: 'alex', rev: 'r1', routing,
  sources: [{id: A, name: 'Alpha Camera', online: true}, {id: B, name: 'Bravo Camera', online: true}],
  destinations: [{id: D1, name: 'Lobby One', online: true, showing: {mode: 'none', source_id: '', source_name: ''}}],
  groups: [], walls: []});

const cell = (video, audio) => ({video, audio, video_n: video === 'all' ? 1 : 0, audio_n: audio === 'all' ? 1 : 0});
const MATRIX = (overrides = {}) => Object.assign({ok: true, role: 'user', rev: 'r1',
  interfaces: {standard: true, matrix: true},
  sources: [{id: A, name: 'Alpha Camera', online: true}, {id: B, name: '<img src=x onerror=alert(1)>', online: true}],
  rows: [
    {kind: 'wall', id: WALL, name: 'Atrium Wall', members: 2, online_members: 2, degraded: false, state: 'all',
     modes: ['av', 'video', 'audio'], cells: {[A]: cell('all', 'all'), [B]: cell('none', 'none')}},
    {kind: 'group', id: GRP, name: 'Lobby Pair', members: 2, online_members: 2, degraded: false, state: 'mixed',
     modes: ['av', 'video', 'audio'], cells: {[A]: cell('partial', 'partial'), [B]: cell('partial', 'none')}},
    {kind: 'display', id: D1, name: 'Lobby One', members: 1, online_members: 1, degraded: false, state: 'all',
     modes: ['av', 'video', 'audio'], cells: {[A]: cell('all', 'none'), [B]: cell('none', 'all')}},
    {kind: 'display', id: D2, name: 'Lobby Two', members: 1, online_members: 0, degraded: false, state: 'unavailable',
     modes: ['av', 'video', 'audio'], cells: {[A]: cell('none', 'none'), [B]: cell('none', 'none')}},
  ]}, overrides);

const OK_ONE = {ok: true, routed: 1, total: 1, summary: '1 of 1 display routed',
                results: [{index: 0, name: 'Lobby One', ok: true, result: 'verified', audio: 'follows'}]};

function routes(routing, extra = {}) {
  return Object.assign({
    'GET /api/auth/session': {status: 200, body: {ok: true, role: 'user', username: 'alex',
                                                  csrf_token: 'csrf-token-1', method: 'password'}},
    'GET /api/user/state': {status: 200, body: STATE(routing)},
    'GET /api/user/matrix': {status: 200, body: MATRIX()},
    'GET /api/user/preview': {status: 200, type: 'image/jpeg', body: {}},
    'POST /api/user/route_multi': {status: 200, body: OK_ONE},
  }, extra);
}

const BOTH = {standard: true, matrix: true}, STD = {standard: true, matrix: false};
const MX = {standard: false, matrix: true}, NONE = {standard: false, matrix: false};
const cells = h => h.byId.mx_table.querySelectorAll('button.mx-cell');
const cellAt = (h, row, src) => cells(h).filter(c => c.attrs['data-row'] === row && c.attrs['data-src'] === src)[0];
const matrixRequests = h => h.requests.filter(r => r.url.split('?')[0] === '/api/user/matrix');

(async () => {
  let passed = 0;
  async function scenario(name, fn) {
    try { await fn(); passed++; }
    catch (err) { console.error('FAILED: ' + name + '\n  ' + (err && err.stack || err)); process.exitCode = 1; }
  }

  await scenario('a server that does not mention routing gives Standard only: no tabs, no matrix request', async () => {
    const r = routes(STD);
    r['GET /api/user/state'] = {status: 200, body: Object.assign(STATE(STD), {routing: undefined})};
    const h = harness({routes: r, remember: memory({view: 'matrix'})});
    await settle();
    assert.strictEqual(h.byId.mode_tabs.hidden, true);
    assert.strictEqual(h.byId.content.hidden, false);
    assert.strictEqual(h.byId.matrix_content.hidden, true);
    assert.strictEqual(matrixRequests(h).length, 0);
  });

  await scenario('Standard only shows Standard routing without a useless second tab', async () => {
    const h = harness({routes: routes(STD), remember: memory()});
    await settle();
    assert.strictEqual(h.byId.mode_tabs.hidden, true);
    assert.strictEqual(h.byId.content.hidden, false);
    assert.strictEqual(h.byId.action_bar.hidden, false);
  });

  await scenario('Matrix only shows the matrix, with no way into Standard routing', async () => {
    const h = harness({routes: routes(MX), remember: memory()});
    await settle();
    assert.strictEqual(h.byId.mode_tabs.hidden, true);
    assert.strictEqual(h.byId.content.hidden, true);
    assert.strictEqual(h.byId.action_bar.hidden, true);
    assert.strictEqual(h.byId.matrix_content.hidden, false);
    assert.strictEqual(matrixRequests(h).length, 1);
  });

  await scenario('neither interface says so instead of showing an empty page', async () => {
    const h = harness({routes: routes(NONE), remember: memory()});
    await settle();
    assert.strictEqual(h.byId.no_routing.hidden, false);
    assert.strictEqual(h.byId.content.hidden, true);
    assert.strictEqual(h.byId.matrix_content.hidden, true);
    assert.strictEqual(matrixRequests(h).length, 0);
  });

  await scenario('both interfaces show Routing | Matrix, default to Standard, and fetch no matrix yet', async () => {
    const h = harness({routes: routes(BOTH), remember: memory()});
    await settle();
    assert.strictEqual(h.byId.mode_tabs.hidden, false);
    assert.strictEqual(h.byId.tab_standard.attrs['aria-selected'], 'true');
    assert.strictEqual(h.byId.tab_matrix.attrs['aria-selected'], 'false');
    assert.strictEqual(h.byId.content.hidden, false);
    assert.strictEqual(matrixRequests(h).length, 0, 'the matrix is read only once it is shown');
  });

  await scenario('choosing Matrix remembers it, client-locally, and a reload opens there', async () => {
    const mem = memory();
    const h = harness({routes: routes(BOTH), remember: mem});
    await settle();
    h.byId.tab_matrix.click();
    await settle();
    assert.strictEqual(mem.data.view, 'matrix');
    assert.strictEqual(h.byId.matrix_content.hidden, false);
    assert.strictEqual(h.byId.content.hidden, true);
    assert.strictEqual(h.byId.tab_matrix.attrs['aria-selected'], 'true');
    const again = harness({routes: routes(BOTH), remember: mem});
    await settle();
    assert.strictEqual(again.byId.matrix_content.hidden, false);
    h.byId.tab_standard.click();
    assert.strictEqual(mem.data.view, 'standard');
  });

  await scenario('a remembered interface the account no longer holds falls back to the one it has', async () => {
    const h = harness({routes: routes(STD), remember: memory({view: 'matrix'})});
    await settle();
    assert.strictEqual(h.byId.content.hidden, false);
    assert.strictEqual(h.byId.matrix_content.hidden, true);
    const m = harness({routes: routes(MX), remember: memory({view: 'standard'})});
    await settle();
    assert.strictEqual(m.byId.matrix_content.hidden, false);
  });

  await scenario('the grid is the server\'s projection: headers, row types, cell states, text not markup', async () => {
    const h = harness({routes: routes(MX), remember: memory()});
    await settle();
    assert.strictEqual(h.byId.mx_table.querySelectorAll('button.mx-src').length, 2);
    assert.ok(h.byId.mx_table.textContent.includes('<img src=x onerror=alert(1)>'));
    assert.strictEqual(cells(h).length, 8);
    const rows = h.byId.mx_table.querySelectorAll('tr').filter(r => r.attrs['data-row']);
    assert.deepStrictEqual(rows.map(r => r.attrs['data-row']), ['wall:' + WALL, 'group:' + GRP, 'display:' + D1, 'display:' + D2]);
    const ring = c => c.children[0], dot = c => c.children[0].children[0];
    assert.ok(ring(cellAt(h, 'display:' + D1, A)).classList.contains('v-all'));
    assert.ok(!dot(cellAt(h, 'display:' + D1, A)).classList.contains('a-all'), 'video only: no audio dot');
    assert.ok(dot(cellAt(h, 'display:' + D1, B)).classList.contains('a-all'), 'breakaway: audio from the other source');
    assert.ok(!ring(cellAt(h, 'display:' + D1, B)).classList.contains('v-all'));
    assert.ok(ring(cellAt(h, 'group:' + GRP, A)).classList.contains('v-partial'), 'a group is never claimed whole when it is not');
    assert.ok(!ring(cellAt(h, 'group:' + GRP, A)).classList.contains('v-all'));
    const sub = id => h.byId.mx_table.querySelector('[data-sub="' + id + '"]').textContent;
    assert.strictEqual(sub('group:' + GRP), '2 displays · Mixed');
    assert.strictEqual(sub('display:' + D2), 'Offline');
    assert.strictEqual(sub('wall:' + WALL), '2 displays · All');
  });

  await scenario('a cell click sends one request: identities, interface and mode, with the CSRF header', async () => {
    const h = harness({routes: routes(MX), remember: memory()});
    await settle();
    const before = h.requests.length;
    cellAt(h, 'display:' + D1, A).click();
    cellAt(h, 'display:' + D1, A).fire('click');                  // a queued second click
    await settle();
    const posts = h.requests.slice(before).filter(r => r.method === 'POST');
    assert.strictEqual(posts.length, 1, 'single flight');
    assert.strictEqual(posts[0].url, '/api/user/route_multi');
    assert.deepStrictEqual(posts[0].body, {source: A, interface: 'matrix', mode: 'av',
                                           displays: [D1], groups: [], walls: []});
    assert.strictEqual(posts[0].headers['X-LumaSuite-CSRF'], 'csrf-token-1');
    assert.ok(!/\d+\.\d+\.\d+\.\d+/.test(JSON.stringify(posts[0].body)));
    assert.deepStrictEqual(h.toasts[0], ['Routed', 'ok']);
  });

  await scenario('the mode radios decide what a click asks for; a group and a wall use their own field', async () => {
    const h = harness({routes: routes(MX), remember: memory()});
    await settle();
    const post = () => h.requests.filter(r => r.method === 'POST').pop().body;
    h.byId.mx_mode_video.checked = true; h.byId.mx_mode_video.fire('change');
    cellAt(h, 'display:' + D1, B).click();
    await settle();
    assert.strictEqual(post().mode, 'video');
    assert.deepStrictEqual(post().displays, [D1]);
    h.byId.mx_mode_audio.checked = true; h.byId.mx_mode_audio.fire('change');
    cellAt(h, 'group:' + GRP, A).click();
    await settle();
    assert.strictEqual(post().mode, 'audio');
    assert.deepStrictEqual([post().displays, post().groups, post().walls], [[], [GRP], []]);
    h.byId.mx_mode_av.checked = true; h.byId.mx_mode_av.fire('change');
    cellAt(h, 'wall:' + WALL, B).click();
    await settle();
    assert.deepStrictEqual([post().mode, post().walls, post().groups], ['av', [WALL], []]);
  });

  await scenario('a saved wall is routed in the mode chosen: video alone and audio alone are real requests', async () => {
    const h = harness({routes: routes(MX), remember: memory()});
    await settle();
    for (const mode of ['video', 'audio', 'av']) {
      h.byId['mx_mode_' + mode].checked = true; h.byId['mx_mode_' + mode].fire('change');
      const wallCell = cellAt(h, 'wall:' + WALL, B);
      assert.strictEqual(wallCell.disabled, false, mode);
      const before = h.requests.filter(r => r.method === 'POST').length;
      wallCell.click();
      await settle();
      const posts = h.requests.filter(r => r.method === 'POST');
      assert.strictEqual(posts.length, before + 1, mode + ': exactly one request');
      const body = posts[posts.length - 1].body;
      assert.deepStrictEqual([body.mode, body.walls, body.displays, body.groups, body.interface],
                             [mode, [WALL], [], [], 'matrix']);
      assert.ok(!('audio_follows_video' in body), 'the Matrix never sends the Standard preference');
    }
  });

  await scenario('an offline display and an offline source cannot be pressed', async () => {
    const m = MATRIX();
    m.sources[1].online = false;
    const h = harness({routes: routes(MX, {'GET /api/user/matrix': {status: 200, body: m}}), remember: memory()});
    await settle();
    assert.strictEqual(cellAt(h, 'display:' + D2, A).disabled, true, 'offline display');
    assert.strictEqual(cellAt(h, 'display:' + D1, B).disabled, true, 'offline source');
    assert.strictEqual(cellAt(h, 'display:' + D1, A).disabled, false);
    const before = h.requests.length;
    cellAt(h, 'display:' + D2, A).fire('click');
    cellAt(h, 'display:' + D1, B).fire('click');
    await settle();
    assert.strictEqual(h.requests.length, before);
  });

  await scenario('a refused route is a result: the toast says why and the grid is read again', async () => {
    const h = harness({routes: routes(MX, {'POST /api/user/route_multi':
      {status: 403, body: {ok: false, reason: 'interface_not_permitted', error: 'Your account is not set up to route that way. Ask an administrator.'}}}),
      remember: memory()});
    await settle();
    const reads = matrixRequests(h).length;
    cellAt(h, 'display:' + D1, A).click();
    await settle();
    assert.strictEqual(h.toasts[0][1], 'error');
    assert.ok(h.toasts[0][0].includes('not set up to route that way'));
    assert.ok(matrixRequests(h).length > reads, 'the matrix is re-read after a route');
  });

  await scenario('live state repaints cells in place: same nodes, new state, no rebuild', async () => {
    let call = 0;
    const h = harness({routes: routes(MX, {'GET /api/user/matrix': () => {
      call++;
      const m = MATRIX();
      if (call > 1) m.rows[2].cells[A] = cell('none', 'none');
      return {status: 200, body: m};
    }}), remember: memory()});
    await settle();
    const first = cellAt(h, 'display:' + D1, A);
    assert.ok(first.children[0].classList.contains('v-all'));
    h.listeners.focus.forEach(fn => fn());                        // an administrator changed something
    await settle();
    h.intervals.filter(Boolean).forEach(i => { if (i[1] === 20000) i[0](); });
    await settle();
    assert.ok(call >= 1);
    h.byId.mx_mode_video.checked = true; h.byId.mx_mode_video.fire('change');
    assert.strictEqual(cellAt(h, 'display:' + D1, A), first, 'a mode change repaints, it does not rebuild');
  });

  await scenario('Matrix removed while it is open: the page moves to Standard at once', async () => {
    let routing = BOTH;
    const h = harness({routes: routes(BOTH, {
      'GET /api/user/state': () => ({status: 200, body: Object.assign(STATE(routing), {rev: routing === BOTH ? 'r1' : 'r2'})}),
      'GET /api/user/revision': () => ({status: 200, body: {ok: true, rev: routing === BOTH ? 'r1' : 'r2'}}),
    }), remember: memory({view: 'matrix'})});
    await settle();
    assert.strictEqual(h.byId.matrix_content.hidden, false);
    routing = STD;
    revisionTick(h)();
    await settle();
    assert.strictEqual(h.byId.matrix_content.hidden, true);
    assert.strictEqual(h.byId.content.hidden, false);
    assert.strictEqual(h.byId.mode_tabs.hidden, true);
  });

  await scenario('both interfaces removed while open: the page says there is nothing to route with', async () => {
    let routing = BOTH;
    const h = harness({routes: routes(BOTH, {
      'GET /api/user/state': () => ({status: 200, body: Object.assign(STATE(routing), {rev: routing === BOTH ? 'r1' : 'r2'})}),
      'GET /api/user/revision': () => ({status: 200, body: {ok: true, rev: routing === BOTH ? 'r1' : 'r2'}}),
    }), remember: memory({view: 'matrix'})});
    await settle();
    routing = NONE;
    revisionTick(h)();
    await settle();
    assert.strictEqual(h.byId.no_routing.hidden, false);
    assert.strictEqual(h.byId.matrix_content.hidden, true);
    assert.strictEqual(h.byId.content.hidden, true);
  });

  await scenario('a 403 from the matrix itself (permission changed between reads) moves to Standard', async () => {
    const h = harness({routes: routes(BOTH, {'GET /api/user/matrix':
      {status: 403, body: {ok: false, reason: 'interface_not_permitted', error: 'x'}}}),
      remember: memory({view: 'matrix'})});
    await settle();
    assert.strictEqual(h.byId.matrix_content.hidden, true);
    assert.strictEqual(h.byId.content.hidden, false);
    assert.ok(matrixRequests(h).length <= 2, 'no loop of retries');
  });

  await scenario('Matrix added live: the tab appears without a reload', async () => {
    let routing = STD;
    const h = harness({routes: routes(STD, {
      'GET /api/user/state': () => ({status: 200, body: Object.assign(STATE(routing), {rev: routing === STD ? 'r1' : 'r2'})}),
      'GET /api/user/revision': () => ({status: 200, body: {ok: true, rev: routing === STD ? 'r1' : 'r2'}}),
    }), remember: memory()});
    await settle();
    assert.strictEqual(h.byId.mode_tabs.hidden, true);
    routing = BOTH;
    revisionTick(h)();
    await settle();
    assert.strictEqual(h.byId.mode_tabs.hidden, false);
  });

  const previews = h => h.requests.filter(r => r.url.startsWith('/api/user/preview'));
  const pointerOn = (h, node) => node.parentNode.fire('mouseenter');
  const pointerOff = (h, node) => node.parentNode.fire('mouseleave');
  const runTimers = h => { const t = pendingTimers(h); t.forEach(x => { x[2] = false; x[0](); }); return t.length; };

  await scenario('previews: nothing on load, one frame after a hover on the dot, none if the pointer leaves first', async () => {
    const h = harness({routes: routes(MX), remember: memory()});
    await settle();
    assert.strictEqual(previews(h).length, 0, 'an idle matrix makes no preview traffic');
    const dot = cellAt(h, 'display:' + D1, A);
    pointerOn(h, dot);
    pointerOff(h, dot);
    runTimers(h);                                                    // a cancelled timer is not pending
    await settle();
    assert.strictEqual(previews(h).length, 0, 'leaving before the delay requests nothing');
    pointerOn(h, dot);
    assert.strictEqual(pendingTimers(h).length, 1, 'the debounce is one timer');
    runTimers(h);
    await settle();
    assert.strictEqual(previews(h).length, 1);
    assert.ok(previews(h)[0].url.includes('source=' + encodeURIComponent(A)));
    assert.strictEqual(h.byId.mx_preview.hidden, false);
    assert.ok(!h.intervals.filter(Boolean).some(i => i[1] === 5000), 'no repeating picture timer on the matrix');
  });

  await scenario('the heading alone is not a trigger: hovering or focusing a source name asks for nothing', async () => {
    const h = harness({routes: routes(MX), remember: memory()});
    await settle();
    for (const name of h.byId.mx_table.querySelectorAll('button.mx-src')) {
      for (const type of ['mouseenter', 'focus', 'mouseover']) { name.fire(type); name.parentNode.fire(type); }
    }
    assert.strictEqual(runTimers(h), 0, 'no timer was armed by a heading');
    await settle();
    assert.strictEqual(previews(h).length, 0);
    assert.strictEqual(h.byId.mx_preview.hidden, true);
  });

  await scenario('the preview is of the column the dot is in: each source, on a display, a group and a saved wall', async () => {
    const h = harness({routes: routes(MX), remember: memory()});
    await settle();
    for (const row of ['display:' + D1, 'group:' + GRP, 'wall:' + WALL]) {
      for (const src of [A, B]) {
        h.requests.length = 0;
        pointerOn(h, cellAt(h, row, src));
        runTimers(h);
        await settle();
        assert.strictEqual(previews(h).length, 1, row + ' ' + src);
        assert.ok(previews(h)[0].url.includes('source=' + encodeURIComponent(src)), row + ' ' + src);
        pointerOff(h, cellAt(h, row, src));
        assert.strictEqual(h.byId.mx_preview.hidden, true, 'it goes when the pointer leaves');
        assert.strictEqual(h.byId.mx_preview.children.length, 0, 'and the picture is dropped, not hidden');
      }
    }
    assert.ok(h.revoked.length >= 6, 'every picture was released');
  });

  await scenario('the preview works in every Matrix mode and never changes the mode', async () => {
    const h = harness({routes: routes(MX), remember: memory()});
    await settle();
    for (const mode of ['av', 'video', 'audio']) {
      h.byId['mx_mode_' + mode].checked = true; h.byId['mx_mode_' + mode].fire('change');
      h.requests.length = 0;
      pointerOn(h, cellAt(h, 'wall:' + WALL, B));
      runTimers(h);
      await settle();
      assert.strictEqual(previews(h).length, 1, mode);
      assert.strictEqual(h.requests.filter(r => r.method === 'POST').length, 0, 'a hover routes nothing');
      pointerOff(h, cellAt(h, 'wall:' + WALL, B));
    }
  });

  await scenario('rapid movement across cells leaves one request, for the cell the pointer rests on', async () => {
    const h = harness({routes: routes(MX), remember: memory()});
    await settle();
    const trail = [['display:' + D1, A], ['display:' + D1, B], ['group:' + GRP, B], ['wall:' + WALL, A],
                   ['group:' + GRP, A], ['display:' + D1, A]];
    trail.forEach(([row, src], i) => {
      pointerOn(h, cellAt(h, row, src));
      assert.strictEqual(pendingTimers(h).length, 1, 'never more than one pending preview (step ' + i + ')');
      if (i < trail.length - 1) pointerOff(h, cellAt(h, row, src));
    });
    runTimers(h);
    await settle();
    assert.strictEqual(previews(h).length, 1, 'only the resting cell was asked for');
    assert.ok(previews(h)[0].url.includes('source=' + encodeURIComponent(A)));
  });

  await scenario('moving on while a picture is on its way abandons that request', async () => {
    let release;
    const gate = new Promise(r => { release = r; });
    const h = harness({routes: routes(MX, {'GET /api/user/preview': () => gate.then(() => ({status: 200, type: 'image/jpeg', body: {}}))}),
                       remember: memory()});
    await settle();
    pointerOn(h, cellAt(h, 'display:' + D1, A));
    runTimers(h);
    await settle();
    assert.strictEqual(previews(h).length, 1);
    pointerOff(h, cellAt(h, 'display:' + D1, A));
    assert.ok(h.aborts.length >= 1, 'the request in flight was cancelled');
    release();
    await settle();
    assert.strictEqual(h.byId.mx_preview.hidden, true, 'a late answer does not resurrect the card');
    assert.strictEqual(h.byId.mx_preview.children.length, 0);
  });

  await scenario('a route click with the preview showing: the preview goes, the click is one request', async () => {
    const h = harness({routes: routes(MX), remember: memory()});
    await settle();
    const dot = cellAt(h, 'display:' + D1, A);
    pointerOn(h, dot);
    runTimers(h);
    await settle();
    assert.strictEqual(h.byId.mx_preview.hidden, false, 'precondition: the preview is up');
    dot.fire('mousedown');
    assert.strictEqual(h.byId.mx_preview.hidden, true, 'a press dismisses it before the click lands');
    dot.fire('focus');                                               // a mouse press also focuses the dot
    assert.strictEqual(pendingTimers(h).length, 0, 'that focus does not bring the picture back');
    dot.click();
    await settle();
    const posts = h.requests.filter(r => r.method === 'POST');
    assert.strictEqual(posts.length, 1);
    assert.deepStrictEqual([posts[0].body.source, posts[0].body.displays], [A, [D1]]);
    assert.strictEqual(previews(h).length, 1, 'no second picture was fetched for the press');
    const css = fs.readFileSync(path.join(ROOT, 'ui', 'remote.css'), 'utf8');
    assert.ok(/\.mx-preview\s*\{[^}]*pointer-events:\s*none/.test(css), 'the card can never sit between the pointer and the dot');
  });

  await scenario('keyboard focus on a dot shows the same picture; leaving it drops it', async () => {
    const h = harness({routes: routes(MX), remember: memory()});
    await settle();
    const dot = cellAt(h, 'display:' + D1, B);
    dot.fire('focus');
    runTimers(h);
    await settle();
    assert.strictEqual(previews(h).length, 1);
    assert.ok(previews(h)[0].url.includes('source=' + encodeURIComponent(B)));
    dot.fire('blur');
    assert.strictEqual(h.byId.mx_preview.hidden, true);
  });

  await scenario('Preview off: a hover on a dot requests nothing, and an offline source has none to show', async () => {
    const h = harness({routes: routes(MX), remember: memory({preview: 'off'})});
    await settle();
    pointerOn(h, cellAt(h, 'display:' + D1, A));
    runTimers(h);
    await settle();
    assert.strictEqual(previews(h).length, 0);
    const m = MATRIX();
    m.sources[1].online = false;
    const g = harness({routes: routes(MX, {'GET /api/user/matrix': {status: 200, body: m}}), remember: memory()});
    await settle();
    pointerOn(g, cellAt(g, 'display:' + D1, B));
    runTimers(g);
    await settle();
    assert.strictEqual(previews(g).length, 0, 'nothing is asked of a source that is not answering');
  });

  await scenario('switching Preview off while one is up removes it', async () => {
    const h = harness({routes: routes(MX), remember: memory()});
    await settle();
    pointerOn(h, cellAt(h, 'display:' + D1, A));
    runTimers(h);
    await settle();
    assert.strictEqual(h.byId.mx_preview.hidden, false);
    h.byId.preview_toggle_m.checked = false; h.byId.preview_toggle_m.fire('change');
    assert.strictEqual(h.byId.mx_preview.hidden, true);
  });

  await scenario('a 401 sends the person to sign in rather than showing a stale grid', async () => {
    const h = harness({routes: routes(MX, {'GET /api/user/matrix': {status: 401, body: {}}}), remember: memory()});
    await settle();
    assert.ok(String(h.replaced || '').startsWith('/login'));
  });

  console.log(passed + ' scenarios passed' + (process.exitCode ? ' (with failures)' : ''));
})();
