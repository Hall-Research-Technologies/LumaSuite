// The restricted routing page, driven for real: user.js runs against a small DOM stub
// and a recording fetch, and the tests inspect the requests a click actually makes.
//
// Source-text assertions prove the text; this proves the behaviour. Scenarios that
// share a rendered DOM or a request log run one after another, never overlapping.
'use strict';
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const ROOT = path.join(__dirname, '..');
const SOURCE = fs.readFileSync(path.join(ROOT, 'ui', 'user.js'), 'utf8');
const HTML = fs.readFileSync(path.join(ROOT, 'ui', 'user.html'), 'utf8');

const {Node, matches, pageFor} = require('./_dom');

const page = () => pageFor(HTML);

// -------------------------------------------------------------------- harness
function harness(options = {}) {
  const {byId, root} = page();
  const requests = [];
  const store = {luma_csrf: 'csrf-token-1'};
  const timers = [];
  const intervals = [];
  const listeners = {};
  const toasts = [];
  const confirms = [];
  const observers = [];
  let reloaded = 0, replaced = null;
  const location = {pathname: options.pathname || '/user', search: '',
                    reload() { reloaded++; }, replace(u) { replaced = u; }};

  const routes = options.routes || {};
  function fetch(url, init) {
    init = init || {};
    const record = {url: String(url), method: init.method || 'GET', headers: init.headers || {},
                    body: init.body ? JSON.parse(init.body) : undefined};
    requests.push(record);
    const key = record.method + ' ' + record.url.split('?')[0];
    const handler = routes[key] || routes[record.url];
    let answer;
    try { answer = typeof handler === 'function' ? handler(record) : handler; }
    catch (err) { return Promise.reject(err); }          // a real fetch rejects; it does not throw
    // A handler may return a promise, so a test can hold a response back and release it later.
    return Promise.resolve(answer).then(got => {
      const spec = got || {status: 404, body: {ok: false}};
      return {
        ok: spec.status >= 200 && spec.status < 300, status: spec.status,
        headers: {get: h => (h.toLowerCase() === 'content-type' ? (spec.type || 'application/json') : null)},
        json: () => Promise.resolve(spec.body === undefined ? {} : spec.body),
        blob: () => Promise.resolve({size: 4}),
      };
    });
  }

  const sandbox = {
    document: {
      getElementById: id => byId[id] || null,
      createElement: tag => new Node(tag),
      createTextNode: t => { const n = new Node('#text'); n._text = t; return n; },
      querySelector: sel => root.querySelector(sel),
      querySelectorAll: sel => root.querySelectorAll(sel),
      hidden: false,
      addEventListener: (type, fn) => { (listeners[type] = listeners[type] || []).push(fn); },
    },
    window: {addEventListener: (type, fn) => { (listeners[type] = listeners[type] || []).push(fn); },
             toast: (m, k) => toasts.push([m, k]),
             lumaConfirm: o => { confirms.push(o); return Promise.resolve(true); },
             lumaRemember: options.remember, lumaForgetCsrf() {}},
    sessionStorage: {getItem: k => store[k] || null, setItem: (k, v) => { store[k] = v; },
                     removeItem: k => { delete store[k]; }},
    location, fetch, console, JSON, Promise, URL: {createObjectURL: () => 'blob:x', revokeObjectURL() {}},
    CSS: {escape: s => String(s)},
    // An image is a node in the tree, and loads on the next turn of the loop.
    Image: class extends Node { constructor() { super('img'); this.onload = null; this.onerror = null; }
      set src(v) { this._src = v; setTimeout(() => this.onload && this.onload(), 0); } },
    setInterval: (fn, ms) => { intervals.push([fn, ms]); return intervals.length; },
    clearInterval: id => { if (intervals[id - 1]) intervals[id - 1] = null; },
    encodeURIComponent, setTimeout: (fn, ms) => { timers.push([fn, ms]); return timers.length; },
    Date,
  };
  sandbox.window.window = sandbox.window;
  if (options.io) {
    // A controllable IntersectionObserver: the test decides which cards are on screen.
    sandbox.IntersectionObserver = sandbox.window.IntersectionObserver = class {
      constructor(cb) { this.cb = cb; this.nodes = new Set(); this.dead = false; observers.push(this); }
      observe(n) { this.nodes.add(n); }
      unobserve(n) { this.nodes.delete(n); }
      disconnect() { this.nodes.clear(); this.dead = true; }
    };
  }
  vm.createContext(sandbox);
  vm.runInContext(SOURCE, sandbox, {filename: 'user.js'});
  return {byId, root, requests, toasts, confirms, timers, intervals, listeners, store, sandbox, observers,
          get reloaded() { return reloaded; }, get replaced() { return replaced; }};
}

const tick = () => new Promise(r => setTimeout(r, 5));
// The intervals still running (a cleared one is null), and the permission check among them.
const live = h => h.intervals.filter(Boolean);
const revisionTick = h => h.intervals.filter(Boolean).filter(i => i[1] === 20000)[0][0];
async function settle(n = 6) { for (let i = 0; i < n; i++) await tick(); }

const STATE = {ok: true, role: 'user', username: 'alex',
  sources: [{id: '02:00:5e:10:00:0a', name: 'Alpha Camera', online: true},
            {id: '02:00:5e:10:00:0b', name: '<img src=x onerror=alert(1)>', online: false}],
  destinations: [{id: '02:00:5e:20:00:01', name: 'Lobby One', online: true,
                  showing: {mode: 'none', source_id: '', source_name: ''}},
                 {id: '02:00:5e:20:00:02', name: 'Lobby Two', online: true,
                  showing: {mode: 'source', source_id: '02:00:5e:10:00:0a', source_name: 'Alpha Camera'}}],
  groups: [{id: 'grp_0123456789abcdef', name: 'Lobby Displays', members: 4, online_members: 4,
            degraded: false, showing: {mode: 'mixed', source_id: '', source_name: ''}}]};

const OK_ONE = {ok: true, routed: 1, total: 1, summary: '1 of 1 display routed',
                results: [{index: 0, name: 'Lobby One', ok: true, result: 'verified', audio: 'follows'}]};

function baseRoutes(extra = {}) {
  return Object.assign({
    'GET /api/auth/session': {status: 200, body: {ok: true, role: 'user', username: 'alex',
                                                  csrf_token: 'csrf-token-1', method: 'password'}},
    'GET /api/user/state': {status: 200, body: STATE},
    'GET /api/user/preview': {status: 200, type: 'image/jpeg', body: {}},
    'POST /api/user/route_multi': {status: 200, body: OK_ONE},
  }, extra);
}

const SRC_A = '02:00:5e:10:00:0a', D1 = '02:00:5e:20:00:01', D2 = '02:00:5e:20:00:02', GRP = 'grp_0123456789abcdef';
function memory(seed = {}) {
  const data = Object.assign({}, seed);
  return {get: k => data[k] || '', set: (k, v) => { if (v) data[k] = String(v); else delete data[k]; }, data};
}
const cards = (h, id) => h.byId[id].querySelectorAll('[data-kind]');
const find = (h, id, dataId) => cards(h, id).filter(c => c.attrs['data-id'] === dataId)[0];

(async () => {
  let passed = 0;
  async function scenario(name, fn) {
    try { await fn(); passed++; }
    catch (err) { console.error('FAILED: ' + name + '\n  ' + (err && err.stack || err)); process.exitCode = 1; }
  }

  await scenario('the page loads the session, then the state, and renders cards from the server', async () => {
    const h = harness({routes: baseRoutes()});
    await settle();
    assert.deepStrictEqual(h.requests.slice(0, 2).map(r => r.url), ['/api/auth/session', '/api/user/state']);
    assert.strictEqual(cards(h, 'sources').length, 2);
    assert.strictEqual(cards(h, 'displays').length, 3);          // one group + two displays
    assert.strictEqual(h.byId.who.textContent, 'alex');
    assert.strictEqual(h.byId.content.hidden, false);
    assert.strictEqual(h.byId.loading.hidden, true);
  });

  await scenario('device-controlled text is rendered as text, never as markup', async () => {
    const h = harness({routes: baseRoutes()});       // innerHTML assignment throws in the stub
    await settle();
    const hostile = find(h, 'sources', '02:00:5e:10:00:0b');
    assert.ok(hostile.textContent.includes('<img src=x onerror=alert(1)>'));
  });

  await scenario('Route stays in place and says what is missing until both are chosen', async () => {
    const h = harness({routes: baseRoutes()});
    await settle();
    assert.strictEqual(h.byId.route_btn.disabled, true);
    assert.ok(h.byId.summary.textContent.includes('Choose a source and at least one display'));
    find(h, 'sources', SRC_A).click();
    assert.ok(h.byId.summary.textContent.includes('Choose at least one display'));
    assert.strictEqual(h.byId.route_btn.disabled, true);
    find(h, 'displays', D1).click();
    assert.strictEqual(h.byId.route_btn.disabled, false);
    assert.strictEqual(h.byId.summary.textContent, 'Route Alpha Camera to Lobby One');
  });

  await scenario('clicking Route sends exactly one request carrying identities only', async () => {
    const h = harness({routes: baseRoutes()});
    await settle();
    find(h, 'sources', SRC_A).click();
    find(h, 'displays', D1).click();
    const before = h.requests.length;
    h.byId.route_btn.click();
    assert.strictEqual(h.byId.route_btn.disabled, true, 'the button is disabled while a route is in flight');
    h.byId.route_btn.click();                                    // a disabled button ignores this...
    h.byId.route_btn.fire('click');                              // ...but a queued event is still delivered
    await settle();
    const posts = h.requests.slice(before).filter(r => r.method === 'POST');
    assert.strictEqual(posts.length, 1, 'single flight: one request, not two');
    assert.strictEqual(posts[0].url, '/api/user/route_multi');
    assert.deepStrictEqual(posts[0].body, {source: SRC_A, displays: [D1], groups: [], audio_follows_video: true});
    assert.strictEqual(posts[0].headers['X-LumaSuite-CSRF'], 'csrf-token-1');
    assert.ok(!/\d+\.\d+\.\d+\.\d+/.test(JSON.stringify(posts[0].body)), 'no address in the request');
    assert.deepStrictEqual(h.toasts[0], ['Routed', 'ok']);
  });

  await scenario('a display group is routed by its opaque id, in the same request shape', async () => {
    const h = harness({routes: baseRoutes({'POST /api/user/route_multi': {status: 200, body: {
      ok: true, routed: 4, total: 4, summary: '4 of 4 displays routed',
      results: [0, 1, 2, 3].map(i => ({index: i, name: 'D' + i, ok: true, result: 'verified', audio: 'follows'}))}}})});
    await settle();
    find(h, 'sources', SRC_A).click();
    find(h, 'displays', GRP).click();
    assert.ok(h.byId.summary.textContent.includes('Lobby Displays (4 displays)'));
    h.byId.route_btn.click();
    await settle();
    const post = h.requests.filter(r => r.method === 'POST')[0];
    assert.strictEqual(post.url, '/api/user/route_multi');
    assert.deepStrictEqual(post.body, {source: SRC_A, displays: [], groups: [GRP], audio_follows_video: true});
    assert.deepStrictEqual(h.toasts[0], ['4 of 4 displays routed', 'ok']);
  });

  await scenario('a group card is text and status: a badge, a count and what it is showing', async () => {
    const h = harness({routes: baseRoutes()});
    await settle();
    const group = find(h, 'displays', GRP);
    assert.ok(group.classList.contains('group'));
    assert.strictEqual(group.querySelectorAll('[data-shot]').length, 0, 'no picture for a group');
    const text = group.textContent;
    assert.ok(text.includes('Group') && text.includes('4 displays') && text.includes('Mixed'), text);
  });

  await scenario('a partial group failure says how many routed and names the displays that did not', async () => {
    const h = harness({routes: baseRoutes({'POST /api/user/route_multi': {status: 200, body: {
      ok: false, routed: 3, total: 4, summary: '3 of 4 displays routed', results: [
        {index: 0, name: 'Lobby One', ok: true, result: 'verified'},
        {index: 1, name: 'Lobby Two', ok: false, result: 'unreachable'}]}}})});
    await settle();
    find(h, 'sources', SRC_A).click();
    find(h, 'displays', GRP).click();
    h.byId.route_btn.click();
    await settle();
    assert.deepStrictEqual(h.toasts[0], ['3 of 4 displays routed', 'error']);
    assert.strictEqual(h.confirms.length, 1);
    assert.deepStrictEqual(JSON.parse(JSON.stringify(h.confirms[0].summary)),
                           [{label: 'Lobby Two', value: 'Did not answer'}]);
    assert.strictEqual(h.confirms[0].cancelText, null, 'an acknowledgement offers one button');
  });

  const failures = [
    [409, {ok: false, reason: 'resource_busy', error: 'busy'}, 'That display is busy with another change. Try again in a moment.'],
    [403, {ok: false, reason: 'destination_not_permitted', error: 'You do not have access to that display.'}, 'You do not have access to that display.'],
    [502, {ok: false, result: 'not_verified', error: 'The command was sent, but the display did not confirm it.'}, 'The command was sent, but the display did not confirm it.'],
    [503, {ok: false, error: 'That device did not answer.'}, 'That device did not answer.'],
    [500, {}, 'That did not work.'],
  ];
  for (const [status, body, sentence] of failures) {
    await scenario('a ' + status + ' is shown as a sentence, never as a status word', async () => {
      const h = harness({routes: baseRoutes({'POST /api/user/route_multi': {status, body}})});
      await settle();
      find(h, 'sources', SRC_A).click();
      find(h, 'displays', D1).click();
      h.byId.route_btn.click();
      await settle();
      assert.deepStrictEqual(h.toasts[0], [sentence, 'error']);
      assert.ok(!/\b(40\d|50\d|forbidden|conflict|bad gateway)\b/i.test(h.toasts[0][0]));
      assert.strictEqual(h.byId.route_btn.disabled, false, 'the page is usable after a failure');
    });
  }

  await scenario('the page is usable again after a request that never answers', async () => {
    const h = harness({routes: baseRoutes({'POST /api/user/route_multi': () => { throw new Error('offline'); }})});
    await settle();
    find(h, 'sources', SRC_A).click();
    find(h, 'displays', D1).click();
    h.byId.route_btn.click();
    await settle();
    assert.deepStrictEqual(h.toasts[0], ['Could not reach LumaSuite', 'error']);
    assert.strictEqual(h.byId.route_btn.disabled, false);
  });

  await scenario('a display that is not following video is told to the person', async () => {
    const h = harness({routes: baseRoutes({'POST /api/user/route_multi': {status: 200, body: Object.assign({}, OK_ONE,
      {results: [{index: 0, name: 'Lobby One', ok: true, result: 'verified', audio: 'not_following'}]})}})});
    await settle();
    find(h, 'sources', SRC_A).click();
    find(h, 'displays', D1).click();
    h.byId.route_btn.click();
    await settle();
    assert.ok(h.toasts[0][0].includes('not set to follow video'));
  });

  await scenario('previews come only from the server, by identity, and never from a device', async () => {
    const h = harness({routes: baseRoutes()});
    await settle(10);
    const previews = h.requests.filter(r => r.url.startsWith('/api/user/preview'));
    assert.ok(previews.length >= 3, 'sources and displays ask for a preview, groups do not');
    previews.forEach(r => assert.ok(/^\/api\/user\/preview\?(source|display)=[^&]+&(fresh=1|t=\d+)$/.test(r.url), r.url));
    assert.ok(previews[0].url.includes('fresh=1'), 'the first request per device is fresh');
    h.requests.forEach(r => assert.ok(!/^https?:/.test(r.url) && !/\d+\.\d+\.\d+\.\d+/.test(r.url), r.url));
    assert.ok(!previews.some(r => r.url.includes('grp_')), 'a group has no picture');
  });

  await scenario('a preview that is not an image leaves a quiet note, never a broken picture', async () => {
    const h = harness({routes: baseRoutes({'GET /api/user/preview': {status: 200, body: {ok: false, reason: 'preview_unavailable'}}})});
    await settle(10);
    const shot = h.byId.sources.querySelectorAll('[data-shot]')[0];
    assert.strictEqual(shot.attrs['data-note'], 'No preview');
    assert.strictEqual(shot.querySelectorAll('img').length, 0);
  });

  await scenario('no one-shot timers: the page has the permission check and the picture cadence, and nothing else', async () => {
    const h = harness({routes: baseRoutes()});
    await settle();
    assert.strictEqual(h.timers.length, 0, 'an idle page schedules no one-shot timer');
    assert.deepStrictEqual(live(h).map(i => i[1]).sort((a, b) => a - b), [5000, 20000]);
    find(h, 'sources', SRC_A).click();
    find(h, 'displays', D1).click();
    h.byId.route_btn.click();
    await settle();
    assert.strictEqual(h.timers.length, 0, 'a route no longer waits on a settle delay');
    assert.strictEqual(live(h).length, 2, 'and does not add a timer: the cadence is restarted, not doubled');
  });

  await scenario('an administrator change reaches the open page: the lost source is deselected and Route is disabled', async () => {
    let rev = 'r1', granted = STATE.sources.slice(0, 1);
    const h = harness({routes: baseRoutes({
      'GET /api/user/state': () => ({status: 200, body: Object.assign({}, STATE, {rev, sources: granted})}),
      'GET /api/user/revision': () => ({status: 200, body: {ok: true, rev}}),
    })});
    await settle();
    assert.strictEqual(live(h).filter(i => i[1] >= 15000).length, 1, 'one slow permission check');
    find(h, 'sources', SRC_A).click();
    find(h, 'displays', D1).click();
    assert.strictEqual(h.byId.route_btn.disabled, false);
    const stateReads = () => h.requests.filter(r => r.url === '/api/user/state').length;
    const before = stateReads();
    await revisionTick(h)(); await settle();
    assert.strictEqual(stateReads(), before, 'an unchanged digest costs no state read');
    rev = 'r2'; granted = [];
    await revisionTick(h)(); await settle();
    assert.strictEqual(stateReads(), before + 1);
    assert.strictEqual(cards(h, 'sources').length, 0);
    assert.strictEqual(h.byId.route_btn.disabled, true, 'Route is disabled once the source is gone');
    assert.ok(!h.requests.some(r => r.method === 'POST' && r.url === '/api/user/route_multi'));
  });

  await scenario('a hidden tab asks nothing about permissions', async () => {
    const h = harness({routes: baseRoutes({'GET /api/user/revision': {status: 200, body: {ok: true, rev: 'x'}}})});
    await settle();
    h.sandbox.document.hidden = true;
    await revisionTick(h)(); await settle();
    assert.ok(!h.requests.some(r => r.url === '/api/user/revision'));
  });

  // ------------------------------------------------------------ multiselect
  await scenario('displays are multi-select, the source is single-select, and a second click deselects', async () => {
    const SRC_B = '02:00:5e:10:00:0c';
    const h = harness({routes: baseRoutes({'GET /api/user/state': {status: 200, body: Object.assign({}, STATE, {
      sources: STATE.sources.concat([{id: SRC_B, name: 'Bravo Camera', online: true}])})}})});
    await settle();
    find(h, 'sources', SRC_A).click();
    find(h, 'sources', SRC_B).click();            // choosing another source replaces it
    assert.strictEqual(find(h, 'sources', SRC_A).attrs['aria-pressed'], 'false');
    assert.strictEqual(find(h, 'sources', SRC_B).attrs['aria-pressed'], 'true');
    find(h, 'sources', SRC_B).click();            // and again removes it
    assert.strictEqual(find(h, 'sources', SRC_B).attrs['aria-pressed'], 'false');
    find(h, 'displays', D1).click();
    find(h, 'displays', D2).click();
    find(h, 'displays', GRP).click();
    [D1, D2, GRP].forEach(id => assert.strictEqual(find(h, 'displays', id).attrs['aria-pressed'], 'true', id));
    assert.strictEqual(h.byId.selection_count.textContent, '2 displays and 1 group chosen');
    find(h, 'displays', D2).click();
    assert.strictEqual(find(h, 'displays', D2).attrs['aria-pressed'], 'false');
    assert.strictEqual(h.byId.selection_count.textContent, '1 display and 1 group chosen');
    assert.strictEqual(h.byId.clear_targets.hidden, false);
    h.byId.clear_targets.click();
    assert.strictEqual(h.byId.selection_count.textContent, '');
    assert.strictEqual(h.byId.clear_targets.hidden, true);
    [D1, GRP].forEach(id => assert.strictEqual(find(h, 'displays', id).attrs['aria-pressed'], 'false', id));
  });

  await scenario('Route needs a source and at least one display or group, and sends them in the order chosen', async () => {
    const h = harness({routes: baseRoutes({'POST /api/user/route_multi': {status: 200, body: {
      ok: true, routed: 3, total: 3, summary: '3 of 3 displays routed',
      results: [0, 1, 2].map(i => ({index: i, name: 'D' + i, ok: true, result: 'verified', audio: 'follows'}))}}})});
    await settle();
    find(h, 'displays', D2).click();
    assert.strictEqual(h.byId.route_btn.disabled, true, 'no source yet');
    find(h, 'sources', SRC_A).click();
    find(h, 'displays', GRP).click();
    find(h, 'displays', D1).click();
    assert.strictEqual(h.byId.route_btn.disabled, false);
    assert.strictEqual(h.byId.summary.textContent, 'Route Alpha Camera to 2 displays and 1 group (up to 6 displays)');
    const before = h.requests.length;
    h.byId.route_btn.click();
    await settle();
    const posts = h.requests.slice(before).filter(r => r.method === 'POST');
    assert.strictEqual(posts.length, 1, 'one request, never one per display');
    assert.deepStrictEqual(posts[0].body, {source: SRC_A, displays: [D2, D1], groups: [GRP], audio_follows_video: true});
    assert.deepStrictEqual(h.toasts[0], ['3 of 3 displays routed', 'ok']);
  });

  await scenario('the selection survives a successful route and the refresh after it', async () => {
    const h = harness({routes: baseRoutes()});
    await settle();
    find(h, 'sources', SRC_A).click();
    find(h, 'displays', D1).click();
    h.byId.route_btn.click();
    await settle();
    assert.strictEqual(find(h, 'sources', SRC_A).attrs['aria-pressed'], 'true');
    assert.strictEqual(find(h, 'displays', D1).attrs['aria-pressed'], 'true');
    assert.strictEqual(h.byId.route_btn.disabled, false);
  });

  await scenario('a refused selection is a sentence, writes nothing the page can see, and keeps the selection', async () => {
    const h = harness({routes: baseRoutes({'POST /api/user/route_multi': {status: 403, body: {
      ok: false, reason: 'destination_not_permitted', error: 'You do not have access to that display.'}}})});
    await settle();
    find(h, 'sources', SRC_A).click();
    find(h, 'displays', D1).click();
    find(h, 'displays', D2).click();
    h.byId.route_btn.click();
    await settle();
    assert.deepStrictEqual(h.toasts[0], ['You do not have access to that display.', 'error']);
    assert.strictEqual(h.confirms.length, 0);
    assert.strictEqual(find(h, 'displays', D2).attrs['aria-pressed'], 'true');
  });

  await scenario('displays and groups chosen are remembered by identity and restored only if still listed', async () => {
    const mem = memory({source: SRC_A, targets: 'display:' + D1 + '|group:' + GRP + '|display:02:00:5e:99:99:99|group:grp_gone'});
    const h = harness({routes: baseRoutes(), remember: mem});
    await settle();
    assert.strictEqual(find(h, 'displays', D1).attrs['aria-pressed'], 'true');
    assert.strictEqual(find(h, 'displays', GRP).attrs['aria-pressed'], 'true');
    assert.strictEqual(h.byId.selection_count.textContent, '1 display and 1 group chosen', 'unlisted identities are dropped');
    find(h, 'displays', D2).click();
    assert.strictEqual(mem.data.targets, 'display:' + D1 + '|group:' + GRP + '|display:' + D2);
    h.byId.clear_targets.click();
    assert.strictEqual(mem.data.targets, undefined, 'clearing forgets the bookmark');
  });

  await scenario('the old single-display bookmark is read once and retired', async () => {
    const mem = memory({target: 'display:' + D1});
    const h = harness({routes: baseRoutes(), remember: mem});
    await settle();
    assert.strictEqual(find(h, 'displays', D1).attrs['aria-pressed'], 'true');
    assert.strictEqual(mem.data.target, undefined);
  });

  // ----------------------------------------------------------------- preview
  const previewRequests = h => h.requests.filter(r => r.url.startsWith('/api/user/preview'));

  await scenario('Preview off: no picture is requested at all, and the cards say so', async () => {
    const h = harness({routes: baseRoutes(), remember: memory({preview: 'off'})});
    await settle(10);
    assert.strictEqual(previewRequests(h).length, 0);
    assert.strictEqual(h.byId.preview_toggle.checked, false);
    h.byId.sources.querySelectorAll('[data-shot]').forEach(n => assert.strictEqual(n.attrs['data-note'], 'Preview off'));
  });

  await scenario('Preview off: choosing and routing ask for no picture, and schedule no refresh', async () => {
    const h = harness({routes: baseRoutes(), remember: memory({preview: 'off'})});
    await settle();
    find(h, 'sources', SRC_A).click();
    find(h, 'displays', D1).click();
    h.byId.route_btn.click();
    await settle(10);
    assert.strictEqual(previewRequests(h).length, 0);
    assert.strictEqual(h.timers.length, 0, 'no post-route picture refresh while previews are off');
  });

  await scenario('turning Preview off drops every picture and is remembered; turning it on asks again', async () => {
    const mem = memory();
    const h = harness({routes: baseRoutes(), remember: mem});
    await settle(10);
    const loaded = previewRequests(h).length;
    assert.ok(loaded >= 3);
    assert.ok(h.byId.sources.querySelectorAll('img').length >= 1, 'a picture was shown');
    h.byId.preview_toggle.checked = false;
    h.byId.preview_toggle.fire('change');
    assert.strictEqual(mem.data.preview, 'off');
    assert.strictEqual(h.byId.sources.querySelectorAll('img').length, 0, 'the pictures are dropped, not hidden');
    h.byId.sources.querySelectorAll('[data-shot]').forEach(n => assert.strictEqual(n.attrs['data-note'], 'Preview off'));
    find(h, 'displays', D1).click();                        // interaction while off asks for nothing
    await settle(10);
    assert.strictEqual(previewRequests(h).length, loaded);
    h.byId.preview_toggle.checked = true;
    h.byId.preview_toggle.fire('change');
    assert.strictEqual(mem.data.preview, undefined, 'on is the default and stores nothing');
    await settle(10);
    assert.ok(previewRequests(h).length > loaded, 'on again: pictures are requested again');
  });

  await scenario('Preview off then on again repaints cards that had been repainted from a held picture', async () => {
    const h = harness({routes: baseRoutes()});
    await settle(10);
    find(h, 'displays', D1).click();                    // the click rebuilds the cards from held pictures
    await settle(10);
    h.byId.preview_toggle.checked = false;
    h.byId.preview_toggle.fire('change');
    const before = previewRequests(h).length;
    h.byId.preview_toggle.checked = true;
    h.byId.preview_toggle.fire('change');
    await settle(10);
    assert.ok(previewRequests(h).length >= before + 3, 'every visible card asks again, none is skipped');
    assert.ok(h.byId.sources.querySelectorAll('img').length >= 1, 'and pictures are back on screen');
  });

  await scenario('a picture that arrives after Preview went off is dropped, not shown', async () => {
    let release;
    const gate = new Promise(r => { release = r; });
    const h = harness({routes: baseRoutes({'GET /api/user/preview': () => gate.then(() => ({status: 200, type: 'image/jpeg', body: {}}))})});
    await settle(6);                                  // the requests are in flight, held by the gate
    assert.ok(previewRequests(h).length >= 3, 'precondition: pictures were asked for before the switch went off');
    h.byId.preview_toggle.checked = false;
    h.byId.preview_toggle.fire('change');
    release();
    await settle(10);
    assert.strictEqual(h.byId.sources.querySelectorAll('img').length, 0, 'a late picture must not appear');
    h.byId.sources.querySelectorAll('[data-shot]').forEach(n => assert.strictEqual(n.attrs['data-note'], 'Preview off'));
  });

  await scenario('a picture refresh that is queued behind a request does not appear once Preview goes off', async () => {
    const h = harness({routes: baseRoutes()});
    await settle(10);
    find(h, 'sources', SRC_A).click();
    find(h, 'displays', D1).click();
    h.byId.route_btn.click();
    await settle(10);
    h.byId.preview_toggle.checked = false;
    h.byId.preview_toggle.fire('change');
    const before = previewRequests(h).length;
    await live(h).filter(i => i[1] === 5000).forEach(i => i[0]());     // nothing is left to fire
    await settle(10);
    assert.strictEqual(previewRequests(h).length, before);
    assert.strictEqual(live(h).filter(i => i[1] === 5000).length, 0, 'the picture timer is stopped');
  });

  await scenario('choosing a card does not ask a device for a picture it already has', async () => {
    const h = harness({routes: baseRoutes()});
    await settle(10);
    const before = previewRequests(h).length;
    assert.ok(before >= 3);
    find(h, 'sources', SRC_A).click();
    find(h, 'displays', D1).click();
    find(h, 'displays', D2).click();
    await settle(10);
    assert.strictEqual(previewRequests(h).length, before, 'selecting is not a reason to fetch');
    assert.ok(h.byId.sources.querySelectorAll('img').length >= 1, 'the held picture is shown again');
  });

  await scenario('a chosen card is marked with a tick the stylesheet can actually draw', () => {
    const css = fs.readFileSync(path.join(ROOT, 'ui', 'remote.css'), 'latin1');
    assert.ok(!/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/.test(css), 'no control characters in the stylesheet');
    assert.ok(css.includes('.pick[aria-pressed="true"] .pick-name::before { content: "\\2713\\00a0"; }'));
  });

  await scenario('the Preview control is a real, labelled checkbox that is reachable and not display:none', () => {
    assert.ok(/<input[^>]*type="checkbox"[^>]*id="preview_toggle"/.test(HTML) || /id="preview_toggle"[^>]*type="checkbox"/.test(HTML));
    assert.ok(/<label[^>]*id="preview_toggle_label"/.test(HTML));
    assert.ok(/class="[^"]*switch-input/.test(HTML.match(/<input[^>]*id="preview_toggle"[^>]*>/)[0]));
    const css = fs.readFileSync(path.join(ROOT, 'ui', 'family.css'), 'utf8');
    const rule = css.slice(css.indexOf('.toggle input[type="checkbox"].switch-input'));
    assert.ok(!/display\s*:\s*none/.test(rule.slice(0, rule.indexOf('}'))), 'a hidden input leaves the tab order');
    assert.ok(/clip/.test(rule.slice(0, rule.indexOf('}'))));
  });

  await scenario('an expired session sends the browser to sign in and remembers where it was going', async () => {
    const h = harness({routes: baseRoutes({'GET /api/user/state': {status: 401, body: {}}})});
    await settle();
    assert.strictEqual(h.replaced, '/login?next=%2Fuser');
  });

  await scenario('a direct-link page re-opens its own link instead of asking for a password', async () => {
    const h = harness({pathname: '/access/some-token', routes: baseRoutes({'GET /api/user/state': {status: 401, body: {}}})});
    await settle();
    assert.strictEqual(h.reloaded >= 1, true);
    assert.strictEqual(h.replaced, null);
    h.requests.forEach(r => assert.ok(!r.url.includes('some-token'), 'the token is never sent anywhere'));
  });

  await scenario('a direct-link session shows no sign-out and no account controls', async () => {
    const h = harness({pathname: '/access/t', routes: baseRoutes({'GET /api/auth/session': {status: 200,
      body: {ok: true, role: 'user', username: 'panel', csrf_token: 'c', method: 'direct_link'}}})});
    await settle();
    assert.strictEqual(h.byId.signout.hidden, true);
  });

  await scenario('a password session offers sign-out, which ends the session and forgets the token', async () => {
    const h = harness({routes: baseRoutes({'POST /api/auth/logout': {status: 200, body: {ok: true}}})});
    await settle();
    assert.strictEqual(h.byId.signout.hidden, false);
    h.byId.signout.click();
    await settle();
    assert.ok(h.requests.some(r => r.url === '/api/auth/logout' && r.method === 'POST'));
    assert.strictEqual(h.store.luma_csrf, undefined);
  });

  // ------------------------------------------------------------ source rules
  const code = SOURCE.replace(/\/\/.*$/gm, '').replace(/\/\*[\s\S]*?\*\//g, '');
  await scenario('the page has a slow permission check and a picture cadence, no innerHTML, no native dialog, no storage of its own', () => {
    // The single timer reads a permission digest, never a device, and is paused while hidden.
    assert.strictEqual((code.match(/setInterval/g) || []).length, 2, 'the digest check and the picture cadence');
    assert.ok(/setInterval\(checkRevision, REVISION_MS\)/.test(code));
    assert.ok(/setInterval\(tickShots, SHOT_MS\)/.test(code));
    assert.ok(/\/api\/user\/revision/.test(code) && /document\.hidden/.test(code));
    assert.ok(!/innerHTML|insertAdjacentHTML|outerHTML|document\.write/.test(code));
    assert.ok(!/\b(alert|confirm|prompt)\s*\(/.test(code.replace(/lumaConfirm/g, '')));
    assert.ok(!/localStorage/.test(code), 'only remember.js may touch browser storage');
    assert.ok(!/document\.cookie/.test(code));
  });

  await scenario('the verb is Route, and the page never says Show for this operation', () => {
    assert.ok(/Route/.test(code));
    assert.ok(!/\bShow\b|\bshow\b/.test(code.replace(/showNoPreview|NoPreview|describeShowing|showing|Showing/g, '')));
    assert.ok(/id="route_btn"[^>]*>Route</.test(HTML));
    assert.ok(!/>Show</.test(HTML));
  });

  await scenario('every address-shaped value is absent from the page source', () => {
    assert.ok(!/\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b/.test(code));
    assert.ok(!/https?:\/\//.test(code), 'the page never builds a device URL');
  });

  await scenario('only the shared modules and its own scripts are loaded, and csrf comes first', () => {
    const scripts = [...HTML.matchAll(/<script src="([^"]+)"/g)].map(m => m[1]);
    assert.strictEqual(scripts[0], '/ui/theme-boot.js');
    assert.ok(scripts.indexOf('/ui/csrf.js') < scripts.indexOf('/ui/user.js'));
    ['settings.js', 'matrix.js', 'wall.js', 'producer.js', 'admin_users.js'].forEach(name =>
      assert.ok(!scripts.some(s => s.endsWith(name)), 'the restricted page must not load ' + name));
  });

  // ============================================ pictures on a five-second cadence
  const SHOT_PERIOD = 5000;
  const shotTick = h => live(h).filter(i => i[1] === SHOT_PERIOD)[0][0];
  const previewKeys = h => previewRequests(h).map(r => r.url);

  await scenario('with Preview on, visible pictures are re-fetched every five seconds, one request per picture', async () => {
    const h = harness({routes: baseRoutes()});
    await settle(10);
    const first = previewRequests(h).length;
    assert.strictEqual(first, 4, 'each of the four cards asked once on arrival');
    shotTick(h)();
    await settle(10);
    const after = previewRequests(h).slice(first);
    assert.strictEqual(after.length, 4, 'one refresh per visible picture');
    assert.ok(after.every(r => r.url.startsWith('/api/user/preview?') && !/fresh=1/.test(r.url)),
      'a cadence refresh is an ordinary cache-friendly request, not a forced fresh one');
    assert.ok(after.every(r => !/\d+\.\d+\.\d+\.\d+/.test(r.url)), 'no device address in any request');
  });

  await scenario('a slow device is skipped, never stacked: a picture with a request in flight is not asked for again', async () => {
    let release;
    let inFlight = 0, peak = 0;
    const slow = new Promise(r => { release = r; });
    const h = harness({routes: baseRoutes({'GET /api/user/preview': () => {
      inFlight++; peak = Math.max(peak, inFlight);
      return slow.then(() => { inFlight--; return {status: 200, type: 'image/jpeg', body: {}}; });
    }})});
    await settle(6);
    const seen = previewRequests(h).length;
    for (let i = 0; i < 5; i++) shotTick(h)();
    await settle(6);
    assert.strictEqual(previewRequests(h).length, seen, 'five ticks while every request is pending add none');
    release();
    await settle(10);
    assert.strictEqual(peak, 3, 'never more than one request per picture, and never more than three at all');
  });

  await scenario('Preview off: no timer, no request, however many ticks arrive', async () => {
    const h = harness({routes: baseRoutes(), remember: memory({preview: 'off'})});
    await settle(10);
    assert.strictEqual(live(h).filter(i => i[1] === SHOT_PERIOD).length, 0);
    assert.strictEqual(previewRequests(h).length, 0);
  });

  await scenario('turning Preview off stops the cadence and turning it on starts it again', async () => {
    const h = harness({routes: baseRoutes()});
    await settle(10);
    assert.strictEqual(live(h).filter(i => i[1] === SHOT_PERIOD).length, 1);
    h.byId.preview_toggle.checked = false; h.byId.preview_toggle.fire('change');
    assert.strictEqual(live(h).filter(i => i[1] === SHOT_PERIOD).length, 0);
    const n = previewRequests(h).length;
    h.byId.preview_toggle.checked = true; h.byId.preview_toggle.fire('change');
    await settle(10);
    assert.strictEqual(live(h).filter(i => i[1] === SHOT_PERIOD).length, 1, 'exactly one timer, not two');
    assert.ok(previewRequests(h).length > n);
  });

  await scenario('a hidden page stops the cadence; coming back refreshes at once and resumes it', async () => {
    const h = harness({routes: baseRoutes()});
    await settle(10);
    h.sandbox.document.hidden = true;
    (h.listeners.visibilitychange || []).forEach(fn => fn());
    assert.strictEqual(live(h).filter(i => i[1] === SHOT_PERIOD).length, 0, 'no timer while hidden');
    const hiddenTick = h.intervals.filter(Boolean).length;
    const n = previewRequests(h).length;
    h.sandbox.document.hidden = false;
    (h.listeners.visibilitychange || []).forEach(fn => fn());
    await settle(10);
    assert.ok(previewRequests(h).length > n, 'visible again: the pictures are fetched at once');
    assert.strictEqual(live(h).filter(i => i[1] === SHOT_PERIOD).length, 1, 'and the cadence resumes');
    assert.ok(hiddenTick >= 1);
  });

  await scenario('leaving the page, or the session ending, stops the cadence', async () => {
    const h = harness({routes: baseRoutes()});
    await settle(10);
    (h.listeners.pagehide || []).forEach(fn => fn());
    assert.strictEqual(live(h).filter(i => i[1] === SHOT_PERIOD).length, 0, 'pagehide');
    const h2 = harness({routes: baseRoutes({'GET /api/user/preview': {status: 401, body: {ok: false}}})});
    await settle(10);
    assert.strictEqual(live(h2).filter(i => i[1] === SHOT_PERIOD).length, 0, 'a 401 sends the person to sign in and stops it');
    assert.ok(h2.replaced && h2.replaced.indexOf('/login') === 0);
  });

  await scenario('only cards on screen refresh: one that scrolls out of view stops, and returns when it does', async () => {
    const h = harness({routes: baseRoutes(), io: true});
    await settle(6);
    const io = h.observers[h.observers.length - 1];
    const nodes = [...io.nodes];
    assert.ok(nodes.length >= 4);
    const show = (n, on) => io.cb([{target: n, isIntersecting: on}]);
    nodes.forEach(n => show(n, true));
    await settle(6);
    const n0 = previewRequests(h).length;
    nodes.slice(2).forEach(n => show(n, false));              // two cards scroll away
    shotTick(h)();
    await settle(6);
    assert.strictEqual(previewRequests(h).length - n0, 2, 'only the two still on screen are refreshed');
    show(nodes[2], true);
    shotTick(h)();
    await settle(6);
    assert.strictEqual(previewRequests(h).length - n0, 5);
  });

  await scenario('a rebuilt page replaces its observer; it never accumulates observers or timers', async () => {
    const h = harness({routes: baseRoutes(), io: true});
    await settle(6);
    for (let i = 0; i < 6; i++) find(h, 'displays', D1).click();     // each click re-renders
    await settle(6);
    assert.ok(h.observers.slice(0, -1).every(o => o.dead), 'every earlier observer was disconnected');
    assert.strictEqual(live(h).filter(i => i[1] === SHOT_PERIOD).length, 1);
  });

  await scenario('a completed route fetches the routed display\'s picture now (fresh) and restarts the cadence', async () => {
    const h = harness({routes: baseRoutes()});
    await settle(10);
    find(h, 'sources', SRC_A).click();
    find(h, 'displays', D1).click();
    const oldAt = h.intervals.indexOf(live(h).filter(i => i[1] === SHOT_PERIOD)[0]);
    const n = previewRequests(h).length;
    h.byId.route_btn.click();
    await settle(12);
    const fresh = previewRequests(h).slice(n).filter(r => /display=02%3A00%3A5e%3A20%3A00%3A01/.test(r.url) || /display=02:00:5e:20:00:01/.test(r.url));
    assert.ok(fresh.length >= 1 && fresh.every(r => /fresh=1/.test(r.url)), 'a forced fresh picture of that display');
    assert.strictEqual(h.timers.length, 0, 'no settle delay');
    assert.strictEqual(h.intervals[oldAt], null, 'the old cadence was cleared');
    assert.strictEqual(live(h).filter(i => i[1] === SHOT_PERIOD).length, 1, 'and exactly one new one started');
  });

  await scenario('a route that rebuilds the cards still refreshes the routed display at once: what is on screen is remembered', async () => {
    const h = harness({routes: baseRoutes(), io: true});
    await settle(6);
    h.observers[h.observers.length - 1].nodes.forEach(n => h.observers[h.observers.length - 1].cb([{target: n, isIntersecting: true}]));
    await settle(6);
    find(h, 'sources', SRC_A).click();
    find(h, 'displays', D1).click();
    h.byId.route_btn.click();
    await settle(14);       // the route, then the re-read that rebuilds every card; the new observer has reported nothing yet
    const live = h.observers[h.observers.length - 1];
    assert.ok(live.nodes.size >= 4 && live.cb, 'precondition: the cards were rebuilt under a new observer');
    const fresh = previewRequests(h).filter(r => /fresh=1/.test(r.url) && /02(%3A|:)00(%3A|:)5e(%3A|:)20(%3A|:)00(%3A|:)01/.test(r.url));
    assert.ok(fresh.length >= 2, 'the first look plus the one right after the route: ' + fresh.length);
  });

  await scenario('moving across the children of a destination does not drop its highlight until the pointer leaves it', async () => {
    const h = harness({routes: baseRoutes()});
    await settle(8);
    const tx = {setData() {}, getData: () => '', effectAllowed: '', dropEffect: ''};
    find(h, 'sources', SRC_A).fire('dragstart', {dataTransfer: tx});
    const card = find(h, 'displays', D2);
    card.fire('dragenter', {dataTransfer: tx});                 // into the card
    card.fire('dragenter', {dataTransfer: tx});                 // into a child of it: enter fires again...
    card.fire('dragleave', {});                                 // ...and leave fires for the one it left
    assert.ok(card.classList.contains('drop-over'), 'still over the card');
    card.fire('dragleave', {});                                 // now it really left
    assert.ok(!card.classList.contains('drop-over'));
  });

  await scenario('repeated routing while pictures refresh never doubles requests or timers', async () => {
    let inFlight = {}, peak = 0;
    const h = harness({routes: baseRoutes({'GET /api/user/preview': r => {
      const key = r.url.replace(/&(t|fresh)=\d+/, '').replace(/&fresh=1/, '');
      inFlight[key] = (inFlight[key] || 0) + 1; peak = Math.max(peak, inFlight[key]);
      return new Promise(res => setTimeout(() => { inFlight[key]--; res({status: 200, type: 'image/jpeg', body: {}}); }, 8));
    }})});
    await settle(10);
    find(h, 'sources', SRC_A).click();
    find(h, 'displays', D1).click();
    for (let i = 0; i < 6; i++) {
      h.byId.route_btn.click();
      shotTick(h)();
      await settle(8);
    }
    assert.strictEqual(peak, 1, 'at most one request per picture, however often routed');
    assert.strictEqual(live(h).filter(i => i[1] === SHOT_PERIOD).length, 1);
    assert.strictEqual(h.timers.length, 0);
  });

  // ================================================================ offline
  const STATE_OFFLINE = Object.assign({}, STATE, {
    sources: STATE.sources.concat([{id: '02:00:5e:10:00:0c', name: 'Bravo Camera', online: true}]),
    destinations: STATE.destinations.concat([{id: '02:00:5e:20:00:03', name: 'Lobby Three', online: false,
      showing: {mode: 'none', source_id: '', source_name: ''}}]),
    groups: STATE.groups.concat([{id: 'grp_fedcba9876543210', name: 'Closed Group', members: 2, online_members: 0,
      degraded: true, showing: {mode: 'unavailable', source_id: '', source_name: ''}}])});
  const D3 = '02:00:5e:20:00:03', SRC_OFF = '02:00:5e:10:00:0b', GRP_OFF = 'grp_fedcba9876543210';
  const offlineRoutes = () => baseRoutes({'GET /api/user/state': {status: 200, body: STATE_OFFLINE}});

  await scenario('an offline source, display or group is listed, visibly unavailable, and cannot be chosen', async () => {
    const h = harness({routes: offlineRoutes()});
    await settle(8);
    [['sources', SRC_OFF], ['displays', D3], ['displays', GRP_OFF]].forEach(([host, id]) => {
      const c = find(h, host, id);
      assert.ok(c, id + ' is still listed');
      assert.strictEqual(c.attrs['aria-disabled'], 'true', id);
      assert.ok(c.classList.contains('offline'), id);
      c.click();
      assert.strictEqual(c.attrs['aria-pressed'], 'false', id + ' must not become selected');
    });
    assert.strictEqual(h.byId.route_btn.disabled, true);
  });

  await scenario('a group with only some members offline can still be chosen, and says how many are offline', async () => {
    const h = harness({routes: baseRoutes({'GET /api/user/state': {status: 200, body: Object.assign({}, STATE, {
      groups: [Object.assign({}, STATE.groups[0], {online_members: 3, degraded: true})]})}})});
    await settle(8);
    assert.ok(find(h, 'displays', GRP).textContent.includes('1 offline'));
    find(h, 'displays', GRP).click();
    assert.strictEqual(find(h, 'displays', GRP).attrs['aria-pressed'], 'true');
  });

  await scenario('a display that goes offline is deselected on the next state read and Route is disabled', async () => {
    let rev = 'r1', down = false;
    const h = harness({routes: baseRoutes({
      'GET /api/user/state': () => ({status: 200, body: Object.assign({}, STATE, {rev,
        destinations: STATE.destinations.map(d => d.id === D1 ? Object.assign({}, d, {online: !down}) : d)})}),
      'GET /api/user/revision': () => ({status: 200, body: {ok: true, rev}})})});
    await settle(8);
    find(h, 'sources', SRC_A).click();
    find(h, 'displays', D1).click();
    assert.strictEqual(h.byId.route_btn.disabled, false);
    rev = 'r2'; down = true;
    await revisionTick(h)(); await settle(8);
    assert.strictEqual(find(h, 'displays', D1).attrs['aria-pressed'], 'false');
    assert.strictEqual(h.byId.route_btn.disabled, true);
  });

  // ============================================================ saved walls
  const WALL = 'wall-0123456789ab';
  const STATE_WALL = Object.assign({}, STATE, {walls: [{id: WALL, name: 'Atrium Wall', members: 4, online_members: 4,
    degraded: false, showing: {mode: 'none', source_id: '', source_name: ''}}]});
  const wallRoutes = extra => baseRoutes(Object.assign({'GET /api/user/state': {status: 200, body: STATE_WALL}}, extra || {}));

  await scenario('an assigned saved video wall is its own labelled destination, shown by name', async () => {
    const h = harness({routes: wallRoutes()});
    await settle(8);
    const wall = find(h, 'displays', WALL);
    assert.ok(wall, 'the wall is listed');
    assert.ok(wall.textContent.includes('Atrium Wall') && wall.textContent.includes('Wall') && wall.textContent.includes('4 displays'));
    assert.ok(h.byId.displays.querySelectorAll('.section-label').some(n => n.textContent === 'Saved video walls'));
    assert.strictEqual(wall.querySelectorAll('[data-shot]').length, 0, 'a wall has no picture of its own');
  });

  await scenario('routing to a wall sends the wall id in its own list, beside the same source and no address', async () => {
    const h = harness({routes: wallRoutes({'POST /api/user/route_multi': {status: 200, body: {
      ok: true, routed: 1, total: 1, summary: '1 of 1 destination routed',
      results: [{index: 0, name: 'Atrium Wall', ok: true, result: 'verified', kind: 'wall', routed: 4, total: 4, audio: 'unknown'}]}}})});
    await settle(8);
    find(h, 'sources', SRC_A).click();
    find(h, 'displays', WALL).click();
    assert.strictEqual(h.byId.summary.textContent, 'Route Alpha Camera to Atrium Wall (4 displays)');
    h.byId.route_btn.click();
    await settle(8);
    const post = h.requests.filter(r => r.method === 'POST')[0];
    assert.deepStrictEqual(post.body, {source: SRC_A, displays: [], groups: [], walls: [WALL], audio_follows_video: true});
    assert.deepStrictEqual(h.toasts[0], ['Routed', 'ok']);
  });

  await scenario('a wall that changed only some of its displays says so: it is never reported as done', async () => {
    const h = harness({routes: wallRoutes({'POST /api/user/route_multi': {status: 200, body: {
      ok: false, routed: 0, total: 1, summary: '0 of 1 destination routed',
      results: [{index: 0, name: 'Atrium Wall', ok: false, result: 'partial', kind: 'wall', routed: 3, total: 4, audio: 'unknown'}]}}})});
    await settle(8);
    find(h, 'sources', SRC_A).click();
    find(h, 'displays', WALL).click();
    h.byId.route_btn.click();
    await settle(8);
    assert.deepStrictEqual(h.toasts[0], ['0 of 1 destination routed', 'error']);
    assert.strictEqual(h.confirms.length, 1);
    assert.strictEqual(JSON.stringify(h.confirms[0].summary),
      JSON.stringify([{label: 'Atrium Wall', value: 'Only 3 displays of 4 changed'}]));
    assert.ok(find(h, 'displays', WALL).classList.contains('route-failed'));
  });

  await scenario('a wall the administrator took away disappears and is deselected; Route is disabled', async () => {
    let rev = 'r1', wallsNow = STATE_WALL.walls;
    const h = harness({routes: baseRoutes({
      'GET /api/user/state': () => ({status: 200, body: Object.assign({}, STATE_WALL, {rev, walls: wallsNow})}),
      'GET /api/user/revision': () => ({status: 200, body: {ok: true, rev}})})});
    await settle(8);
    find(h, 'sources', SRC_A).click();
    find(h, 'displays', WALL).click();
    assert.strictEqual(h.byId.route_btn.disabled, false);
    rev = 'r2'; wallsNow = [];
    await revisionTick(h)(); await settle(8);
    assert.strictEqual(find(h, 'displays', WALL), undefined);
    assert.strictEqual(h.byId.route_btn.disabled, true);
    assert.ok(!/walls/.test(JSON.stringify(h.requests.filter(r => r.method === 'POST'))));
  });

  // ======================================================= drag and drop
  const dt = (extra) => Object.assign({setData() {}, getData: () => '', effectAllowed: '', dropEffect: ''}, extra || {});
  const dragSource = (h, id) => find(h, 'sources', id).fire('dragstart', {dataTransfer: dt()});
  const posts = h => h.requests.filter(r => r.method === 'POST' && r.url === '/api/user/route_multi');

  await scenario('only an online source is draggable; destinations are not', async () => {
    const h = harness({routes: offlineRoutes()});
    await settle(8);
    assert.strictEqual(find(h, 'sources', SRC_A).attrs.draggable, 'true');
    assert.strictEqual(find(h, 'sources', SRC_OFF).attrs.draggable, undefined, 'an offline source cannot be dragged');
    assert.strictEqual(find(h, 'displays', D1).attrs.draggable, undefined);
  });

  await scenario('dropping a source on a display sends the same request the Route button sends', async () => {
    const h = harness({routes: baseRoutes()});
    await settle(8);
    // The button path first, for comparison.
    find(h, 'sources', SRC_A).click(); find(h, 'displays', D1).click();
    h.byId.route_btn.click();
    await settle(8);
    const viaButton = posts(h)[0];
    // Now the drop.
    dragSource(h, SRC_A);
    const over = find(h, 'displays', D2).fire('dragover', {dataTransfer: dt()});
    assert.strictEqual(over.defaultPrevented, true, 'a valid destination accepts the drop');
    assert.ok(find(h, 'displays', D2).classList.contains('drop-over'), 'and highlights');
    const drop = find(h, 'displays', D2).fire('drop', {dataTransfer: dt()});
    assert.strictEqual(drop.defaultPrevented, true);
    await settle(8);
    const viaDrop = posts(h)[1];
    assert.strictEqual(posts(h).length, 2);
    assert.strictEqual(viaDrop.url, viaButton.url, 'one route, not two');
    assert.deepStrictEqual(viaDrop.body, {source: SRC_A, displays: [D2], groups: [], audio_follows_video: true});
    assert.deepStrictEqual(Object.keys(viaDrop.body), Object.keys(viaButton.body));
    assert.strictEqual(viaDrop.headers['X-LumaSuite-CSRF'], 'csrf-token-1');
  });

  await scenario('dropping on a group sends the group, and on a saved wall sends the wall', async () => {
    const h = harness({routes: wallRoutes()});
    await settle(8);
    dragSource(h, SRC_A);
    find(h, 'displays', GRP).fire('drop', {dataTransfer: dt()});
    await settle(8);
    assert.deepStrictEqual(posts(h)[0].body, {source: SRC_A, displays: [], groups: [GRP], audio_follows_video: true});
    dragSource(h, SRC_A);
    find(h, 'displays', WALL).fire('drop', {dataTransfer: dt()});
    await settle(8);
    assert.deepStrictEqual(posts(h)[1].body, {source: SRC_A, displays: [], groups: [], walls: [WALL], audio_follows_video: true});
  });

  await scenario('an ordinary click, a drag that is cancelled and a drop with no drag all route nothing', async () => {
    const h = harness({routes: baseRoutes()});
    await settle(8);
    find(h, 'sources', SRC_A).click();
    find(h, 'displays', D1).click();
    dragSource(h, SRC_A);
    find(h, 'sources', SRC_A).fire('dragend', {});
    find(h, 'displays', D2).fire('drop', {dataTransfer: dt()});        // the drag was cancelled first
    find(h, 'displays', D2).fire('drop', {dataTransfer: dt()});        // and a drop that never had a drag
    await settle(8);
    assert.strictEqual(posts(h).length, 0);
  });

  await scenario('an offline or unlisted destination refuses the drop and nothing is sent', async () => {
    const h = harness({routes: offlineRoutes()});
    await settle(8);
    dragSource(h, SRC_A);
    const offline = find(h, 'displays', D3);
    const over = offline.fire('dragover', {dataTransfer: dt()});
    assert.strictEqual(over.defaultPrevented, false, 'the browser shows a no-drop cursor');
    assert.ok(!offline.classList.contains('drop-over'));
    offline.fire('drop', {dataTransfer: dt()});
    find(h, 'displays', GRP_OFF).fire('drop', {dataTransfer: dt()});
    await settle(8);
    assert.strictEqual(posts(h).length, 0);
  });

  await scenario('the dragged identity is the one the page holds, never one read from the drag event', async () => {
    const h = harness({routes: baseRoutes()});
    await settle(8);
    dragSource(h, SRC_A);
    // A manipulated payload naming a source this account was never given.
    find(h, 'displays', D1).fire('drop', {dataTransfer: dt({getData: () => '02:00:5e:99:99:99'})});
    await settle(8);
    assert.deepStrictEqual(posts(h)[0].body, {source: SRC_A, displays: [D1], groups: [], audio_follows_video: true});
    assert.ok(!JSON.stringify(posts(h)).includes('99:99:99'));
  });

  await scenario('a destination stops accepting a drag that is already under way once a route is in flight', async () => {
    let release;
    const held = new Promise(r => { release = r; });
    const h = harness({routes: baseRoutes({'POST /api/user/route_multi': () => held.then(() => ({status: 200, body: OK_ONE}))})});
    await settle(8);
    find(h, 'sources', SRC_A).click(); find(h, 'displays', D1).click();
    dragSource(h, SRC_A);                                          // the drag is under way...
    const before = find(h, 'displays', D2).fire('dragover', {dataTransfer: dt()});
    assert.strictEqual(before.defaultPrevented, true, 'precondition: an idle page accepts it');
    h.byId.route_btn.click();                                      // ...and then a route starts
    const during = find(h, 'displays', D2).fire('dragover', {dataTransfer: dt()});
    assert.strictEqual(during.defaultPrevented, false, 'busy: the destination refuses');
    assert.ok(!find(h, 'displays', D2).classList.contains('drop-over'));
    release();
    await settle(8);
  });

  await scenario('a drop while a route is in flight is ignored: one request at a time', async () => {
    let release;
    const held = new Promise(r => { release = r; });
    const h = harness({routes: baseRoutes({'POST /api/user/route_multi': () => held.then(() => ({status: 200, body: OK_ONE}))})});
    await settle(8);
    find(h, 'sources', SRC_A).click(); find(h, 'displays', D1).click();
    h.byId.route_btn.click();
    dragSource(h, SRC_A);
    const over = find(h, 'displays', D2).fire('dragover', {dataTransfer: dt()});
    assert.strictEqual(over.defaultPrevented, false, 'busy: the drop is refused');
    find(h, 'displays', D2).fire('drop', {dataTransfer: dt()});
    release();
    await settle(8);
    assert.strictEqual(posts(h).length, 1);
  });

  await scenario('routing in progress and the outcome are shown on the destination that was dropped on', async () => {
    let release;
    const held = new Promise(r => { release = r; });
    const h = harness({routes: baseRoutes({'POST /api/user/route_multi': () => held.then(() => ({status: 200, body: OK_ONE}))})});
    await settle(8);
    dragSource(h, SRC_A);
    find(h, 'displays', D1).fire('drop', {dataTransfer: dt()});
    assert.ok(find(h, 'displays', D1).classList.contains('routing'));
    assert.ok(find(h, 'displays', D1).textContent.includes('Routing'));
    release();
    await settle(10);
    assert.ok(!find(h, 'displays', D1).classList.contains('routing'));
    assert.ok(find(h, 'displays', D1).classList.contains('route-ok'));
  });

  // ======================================== at most three pictures in flight, so a route is never queued behind them
  // Seven sources and seven displays: fourteen cards, each asking for a picture the moment it is seen.
  const SRC = i => '02:00:5e:10:01:0' + i, DSP = i => '02:00:5e:20:01:0' + i;
  const MANY = {ok: true, role: 'user', username: 'alex', groups: [],
    sources: Array.from({length: 7}, (_, i) => ({id: SRC(i), name: 'Source ' + i, online: true})),
    destinations: Array.from({length: 7}, (_, i) => ({id: DSP(i), name: 'Display ' + i, online: true,
      showing: {mode: 'none', source_id: '', source_name: ''}}))};
  // Pictures that stay out until the test lets them go, counting how many are out at once.
  function heldPictures(extra = {}) {
    const rig = {held: [], flying: 0, peak: 0, atRoute: null};
    const picture = () => {
      rig.flying++; rig.peak = Math.max(rig.peak, rig.flying);
      return new Promise(res => rig.held.push(() => { rig.flying--; res({status: 200, type: 'image/jpeg', body: {}}); }));
    };
    rig.routes = baseRoutes(Object.assign({
      'GET /api/user/state': {status: 200, body: MANY},
      'GET /api/user/preview': picture,
      'POST /api/user/route_multi': () => { rig.atRoute = rig.flying; return {status: 200, body: OK_ONE}; },
    }, extra));
    rig.release = async (n = 1) => { for (let i = 0; i < n && rig.held.length; i++) { rig.held.shift()(); await settle(4); } };
    return rig;
  }
  const shotOf = r => { const m = /\?(source|display)=([^&]+)/.exec(r.url); return m[1] + ':' + decodeURIComponent(m[2]); };
  const cardOrder = h => h.root.querySelectorAll('[data-shot]').map(n => n.attrs['data-shot']);

  await scenario('never more than three pictures are being fetched, however many cards there are', async () => {
    const rig = heldPictures();
    const h = harness({routes: rig.routes});
    await settle(10);
    assert.strictEqual(cardOrder(h).length, 14, 'precondition: fourteen cards with pictures');
    assert.strictEqual(previewRequests(h).length, 3, 'three started, eleven waiting their turn');
    assert.strictEqual(rig.flying, 3);
    await rig.release(40);
    assert.ok(rig.peak <= 3, 'never above the cap: ' + rig.peak);
    assert.strictEqual(previewRequests(h).length, 14, 'every card was fetched in the end');
    assert.strictEqual(new Set(previewRequests(h).map(shotOf)).size, 14, 'once each');
    assert.strictEqual(rig.flying, 0);
  });

  await scenario('the queue runs in the order the pictures were asked for', async () => {
    const rig = heldPictures();
    const h = harness({routes: rig.routes});
    await settle(10);
    await rig.release(40);
    assert.deepStrictEqual(previewRequests(h).map(shotOf), cardOrder(h), 'first asked, first fetched');
  });

  await scenario('a route is sent at once while pictures are still out, and finds the cap, not a full set of connections', async () => {
    const rig = heldPictures();
    const h = harness({routes: rig.routes});
    await settle(10);
    find(h, 'sources', SRC(0)).click();
    find(h, 'displays', DSP(6)).click();
    h.byId.route_btn.click();
    await settle(10);
    const posts = h.requests.filter(r => r.method === 'POST');
    assert.strictEqual(posts.length, 1, 'the route did not wait for a picture');
    assert.strictEqual(rig.atRoute, 3, 'three pictures were out when the route went, not fourteen');
    assert.ok(rig.peak <= 3);
  });

  await scenario('a route-triggered picture goes ahead of the waiting cadence pictures, as the one fresh request', async () => {
    const rig = heldPictures();
    const h = harness({routes: rig.routes});
    await settle(10);
    const before = previewRequests(h).length;
    find(h, 'sources', SRC(0)).click();
    find(h, 'displays', DSP(6)).click();               // the last card: its picture is still waiting
    h.byId.route_btn.click();
    await settle(10);
    await rig.release(1);
    const next = previewRequests(h).slice(before);
    assert.ok(next.length >= 1, 'the freed turn was used');
    assert.strictEqual(shotOf(next[0]), 'display:' + DSP(6), 'the routed display is next, not last of fourteen');
    assert.ok(/fresh=1/.test(next[0].url), 'and it is a fresh picture');
    await rig.release(60);
    // (The re-read a route triggers re-renders the cards, and a card that had asked once is asked
    // for again when its first request returns -- that is the page's own one-queued-request rule.)
    assert.ok(previewRequests(h).filter(r => shotOf(r) === 'display:' + DSP(6)).length <= 2,
      'its waiting request was moved up, not duplicated behind a fresh one');
  });

  await scenario('a hidden page starts nothing that was waiting, and coming back asks for the visible pictures again', async () => {
    const rig = heldPictures();
    const h = harness({routes: rig.routes});
    await settle(10);
    h.sandbox.document.hidden = true;
    (h.listeners.visibilitychange || []).forEach(fn => fn());
    await rig.release(3);                              // the three that were out finish...
    await settle(6);
    assert.strictEqual(previewRequests(h).length, 3, '...and none of the eleven waiting is started');
    assert.strictEqual(live(h).filter(i => i[1] === SHOT_PERIOD).length, 0, 'no timer while hidden');
    h.sandbox.document.hidden = false;
    (h.listeners.visibilitychange || []).forEach(fn => fn());
    await settle(10);
    assert.strictEqual(previewRequests(h).length, 6, 'the cards on screen ask again: three out, the rest waiting');
    await rig.release(60);
    assert.strictEqual(previewRequests(h).length, 17, 'three, then all fourteen again');
    assert.strictEqual(rig.flying, 0, 'nothing left out');
    assert.ok(rig.peak <= 3);
  });

  await scenario('leaving the page releases the queue', async () => {
    const rig = heldPictures();
    const h = harness({routes: rig.routes});
    await settle(10);
    (h.listeners.pagehide || []).forEach(fn => fn());
    await rig.release(60);
    assert.strictEqual(previewRequests(h).length, 3, 'the eleven that were waiting were never fetched');
    assert.strictEqual(live(h).filter(i => i[1] === SHOT_PERIOD).length, 0);
  });

  await scenario('Preview going off releases the queue; going on again asks once per card, still within the cap', async () => {
    const rig = heldPictures();
    const h = harness({routes: rig.routes});
    await settle(10);
    h.byId.preview_toggle.checked = false; h.byId.preview_toggle.fire('change');
    await rig.release(60);
    assert.strictEqual(previewRequests(h).length, 3, 'nothing waiting was fetched after the switch went off');
    h.byId.preview_toggle.checked = true; h.byId.preview_toggle.fire('change');
    await settle(10);
    await rig.release(60);
    assert.strictEqual(previewRequests(h).length, 17);
    assert.ok(rig.peak <= 3, 'peak ' + rig.peak);
    assert.strictEqual(rig.flying, 0);
  });

  await scenario('a picture whose request cannot even be made gives its turn to the next one', async () => {
    // fetch throwing at once (not rejecting) is the one way the gate's own failure branch is reached.
    const rig = heldPictures({'GET /api/user/preview': () => ({status: 200, type: 'image/jpeg', body: {}})});
    const h = harness({routes: rig.routes});
    const real = h.sandbox.fetch;
    let thrown = 0;
    h.sandbox.fetch = (url, init) => {
      if (String(url).startsWith('/api/user/preview') && thrown < 3) { thrown++; throw new Error('cannot start'); }
      return real(url, init);
    };
    await settle(30);
    assert.strictEqual(thrown, 3, 'precondition: three requests failed to start');
    assert.strictEqual(previewRequests(h).length, 11, 'the other eleven cards were still fetched: no turn was kept');
  });

  await scenario('Preview off then on again before anything finished does not let the old queue run beside the new one', async () => {
    const rig = heldPictures();
    const h = harness({routes: rig.routes});
    await settle(10);
    h.byId.preview_toggle.checked = false; h.byId.preview_toggle.fire('change');
    h.byId.preview_toggle.checked = true; h.byId.preview_toggle.fire('change');
    await settle(10);
    await rig.release(80);
    assert.strictEqual(previewRequests(h).length, 17, 'three in flight, then each of the fourteen cards once: the old queue was dropped');
    assert.ok(rig.peak <= 3);
  });

  await scenario('a session that ends releases the queue, and the sign-in page is shown', async () => {
    let n = 0;
    const rig = heldPictures({'GET /api/user/preview': () => (++n === 1 ? {status: 401, body: {ok: false}} : new Promise(() => {}))});
    const h = harness({routes: rig.routes});
    await settle(10);
    assert.ok(h.replaced && h.replaced.indexOf('/login') === 0);
    assert.strictEqual(previewRequests(h).length, 3, 'the others were never fetched');
  });

  await scenario('a picture that fails gives its turn to the next one', async () => {
    let n = 0;
    const rig = heldPictures({'GET /api/user/preview': () => { n++; if (n <= 3) throw new Error('refused'); return {status: 200, type: 'image/jpeg', body: {}}; }});
    const h = harness({routes: rig.routes});
    await settle(30);
    assert.strictEqual(previewRequests(h).length, 14, 'three failures held no turn');
  });

  await scenario('the cap and the queue are in the page as written: three, and released on every stop', () => {
    const code = SOURCE.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
    assert.ok(/var PICTURE_MAX = 3;/.test(code));
    assert.ok(/pictureSlot\(function \(\) \{[\s\S]*?loadPreview\(/.test(code), 'a picture is fetched only through the gate');
    assert.strictEqual((code.match(/loadPreview\(/g) || []).length, 2, 'the definition and the one call inside the gate');
    assert.ok(code.includes('function toLogin() {\n    stopPictureWork();'));
    assert.ok(/addEventListener\('pagehide', stopPictureWork\)/.test(code));
  });

  // ======================================== the engine's reason reaches the person on every way of routing
  const REFUSED_BODY = (row) => ({status: 200, body: {ok: false, routed: 0, total: 1, summary: '0 of 1 display routed',
    results: [Object.assign({index: 0, name: 'Lobby One', ok: false, result: 'failed', audio: 'unknown'}, row)]}});
  const AUDIO_SENTENCE = "That source's audio is not available to this display.";
  // Plain objects: the page runs in its own realm, and a prototype from there never deep-equals ours.
  const rowsShown = h => ((h.confirms[0] && h.confirms[0].summary) || []).map(r => ({label: r.label, value: r.value}));

  await scenario('a display the engine declined is described by its sentence, not "Did not work"', async () => {
    const h = harness({routes: baseRoutes({'POST /api/user/route_multi': REFUSED_BODY(
      {reason: 'audio_not_available', message: AUDIO_SENTENCE})})});
    await settle(8);
    find(h, 'sources', SRC_A).click();
    find(h, 'displays', D1).click();
    h.byId.route_btn.click();
    await settle(10);
    assert.deepStrictEqual(rowsShown(h), [{label: 'Lobby One', value: AUDIO_SENTENCE}]);
  });

  await scenario('the same sentence is shown when the route is a drop', async () => {
    const h = harness({routes: baseRoutes({'POST /api/user/route_multi': REFUSED_BODY(
      {reason: 'audio_not_available', message: AUDIO_SENTENCE})})});
    await settle(8);
    dragSource(h, SRC_A);
    find(h, 'displays', D1).fire('drop', {dataTransfer: dt()});
    await settle(10);
    assert.deepStrictEqual(rowsShown(h), [{label: 'Lobby One', value: AUDIO_SENTENCE}]);
  });

  await scenario('a failure with no sentence keeps the ordinary words, and the other outcomes keep theirs', async () => {
    const cases = [[{}, 'Did not work'], [{result: 'not_verified'}, 'Sent, but the display did not confirm it'],
                   [{result: 'offline'}, 'Offline'], [{result: 'busy'}, 'Busy with another change'],
                   [{reason: 'something_else'}, 'Did not work']];
    for (const [row, words] of cases) {
      const h = harness({routes: baseRoutes({'POST /api/user/route_multi': REFUSED_BODY(row)})});
      await settle(8);
      find(h, 'sources', SRC_A).click();
      find(h, 'displays', D1).click();
      h.byId.route_btn.click();
      await settle(10);
      assert.deepStrictEqual(rowsShown(h), [{label: 'Lobby One', value: words}], JSON.stringify(row));
    }
  });

  await scenario('the sentence is shown as text, never as markup', async () => {
    const h = harness({routes: baseRoutes({'POST /api/user/route_multi': REFUSED_BODY(
      {reason: 'audio_not_available', message: '<img src=x onerror=alert(1)>'})})});
    await settle(8);
    find(h, 'sources', SRC_A).click();
    find(h, 'displays', D1).click();
    h.byId.route_btn.click();
    await settle(10);
    assert.strictEqual(rowsShown(h)[0].value, '<img src=x onerror=alert(1)>');
  });

  await scenario('a drop refused by the server says why and marks the destination failed', async () => {
    const h = harness({routes: baseRoutes({'POST /api/user/route_multi': {status: 403, body: {
      ok: false, error: 'You do not have access to that display.', reason: 'destination_not_permitted'}}})});
    await settle(8);
    dragSource(h, SRC_A);
    find(h, 'displays', D1).fire('drop', {dataTransfer: dt()});
    await settle(10);
    assert.deepStrictEqual(h.toasts[0], ['You do not have access to that display.', 'error']);
    assert.ok(find(h, 'displays', D1).classList.contains('route-failed'));
  });

  await scenario('keyboard and button routing is unchanged: selecting and pressing Route still works with no drag', async () => {
    const h = harness({routes: baseRoutes()});
    await settle(8);
    find(h, 'sources', SRC_A).fire('click'); find(h, 'displays', D1).fire('click');
    h.byId.route_btn.fire('click');
    await settle(8);
    assert.deepStrictEqual(posts(h)[0].body, {source: SRC_A, displays: [D1], groups: [], audio_follows_video: true});
  });

  await scenario('Audio follows video: a visible toggle beside Preview, on by default, a real labelled checkbox', async () => {
    const h = harness({routes: baseRoutes()});
    await settle();
    assert.strictEqual(h.byId.afv_toggle.checked, true, 'default on for a fresh session');
    assert.ok(/<input[^>]*type="checkbox"[^>]*id="afv_toggle"/.test(HTML) || /id="afv_toggle"[^>]*type="checkbox"/.test(HTML));
    assert.ok(/class="[^"]*switch-input/.test(HTML.match(/<input[^>]*id="afv_toggle"[^>]*>/)[0]));
    const label = HTML.match(/<label[^>]*id="afv_toggle_label"[\s\S]*?<\/label>/)[0];
    assert.ok(label.includes('Audio follows video'));
    assert.ok(!/title=/.test(label), 'no native tooltip');
    const toolbar = HTML.slice(HTML.indexOf('id="content"'), HTML.indexOf('id="matrix_content"'));
    assert.ok(toolbar.includes('id="afv_toggle"') && toolbar.includes('id="preview_toggle"'), 'Standard routing, beside Preview');
    assert.ok(!HTML.slice(HTML.indexOf('id="matrix_content"')).includes('afv_toggle'), 'the Matrix has no such control');
  });

  await scenario('Audio follows video ON: every route request says so, for a display, a group and a wall, click or drop', async () => {
    const h = harness({routes: wallRoutes()});
    await settle(8);
    find(h, 'sources', SRC_A).click();
    for (const id of [D1, GRP, WALL]) {
      find(h, 'displays', id).click();
      h.byId.route_btn.click();
      await settle(8);
      find(h, 'displays', id).click();                       // deselect for the next
    }
    assert.ok(posts(h).length >= 3);
    posts(h).forEach(p => assert.strictEqual(p.body.audio_follows_video, true));
    dragSource(h, SRC_A);
    find(h, 'displays', D2).fire('drop', {dataTransfer: dt()});
    await settle(8);
    assert.strictEqual(posts(h)[posts(h).length - 1].body.audio_follows_video, true);
  });

  await scenario('Audio follows video OFF: the same click, a drop, a group and a wall all say video only', async () => {
    const h = harness({routes: wallRoutes()});
    await settle(8);
    h.byId.afv_toggle.checked = false; h.byId.afv_toggle.fire('change');
    find(h, 'sources', SRC_A).click();
    find(h, 'displays', D1).click();
    find(h, 'displays', GRP).click();
    find(h, 'displays', WALL).click();
    h.byId.route_btn.click();
    await settle(8);
    assert.deepStrictEqual(posts(h)[0].body, {source: SRC_A, displays: [D1], groups: [GRP], walls: [WALL],
                                              audio_follows_video: false});
    dragSource(h, SRC_A);
    find(h, 'displays', D2).fire('drop', {dataTransfer: dt()});
    await settle(8);
    assert.deepStrictEqual(posts(h)[1].body, {source: SRC_A, displays: [D2], groups: [], audio_follows_video: false},
                           'a drop follows the same rule as a click');
    h.byId.afv_toggle.checked = true; h.byId.afv_toggle.fire('change');
    dragSource(h, SRC_A);
    find(h, 'displays', D2).fire('drop', {dataTransfer: dt()});
    await settle(8);
    assert.strictEqual(posts(h)[2].body.audio_follows_video, true, 'turned back on, it applies at once');
  });

  await scenario('Audio follows video is not stored anywhere and never reaches another session', async () => {
    const mem = memory();
    const h = harness({routes: baseRoutes(), remember: mem});
    await settle();
    h.byId.afv_toggle.checked = false; h.byId.afv_toggle.fire('change');
    assert.deepStrictEqual(Object.keys(mem.data).filter(k => /audio|afv|follow/i.test(k)), [], 'nothing remembered');
    assert.ok(!Object.keys(h.store).some(k => /audio|afv|follow/i.test(k)), 'nothing in session storage either');
    const fresh = harness({routes: baseRoutes(), remember: mem});
    await settle();
    assert.strictEqual(fresh.byId.afv_toggle.checked, true, 'a fresh page starts on');
  });

  await scenario('cards are real buttons, so Enter and Space already work; dragging adds nothing to the tab order', () => {
    assert.ok(/el\('button', 'pick'/.test(SOURCE));
    assert.ok(!/tabindex/i.test(SOURCE), 'no tabindex is added to cards');
  });

  console.log(passed + ' scenarios passed' + (process.exitCode ? ' (with failures)' : ''));
})();
