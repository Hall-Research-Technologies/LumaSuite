// Where keyboard focus is after a route. user.js runs against a small DOM stub that models what a
// browser does to focus -- a focused control that is disabled or removed falls back to the page --
// and a dialog that, like dialogs.js, returns focus to whatever held it when it opened.
//
// The measured question is document.activeElement after the route and after the dialog closes:
// the control the person pressed, found again by identity (the Route button, a card's kind and id,
// a cell's row and source), and never a place the person has moved to meanwhile. Scenarios share a
// DOM and a request log, so they run one after another, never overlapping.
'use strict';
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const ROOT = path.join(__dirname, '..');
const SOURCE = fs.readFileSync(path.join(ROOT, 'ui', 'user.js'), 'utf8');
const HTML = fs.readFileSync(path.join(ROOT, 'ui', 'user.html'), 'utf8');
const {Node, pageFor} = require('./_dom');

const A = '02:00:5e:10:00:0a', B = '02:00:5e:10:00:0b', C = '02:00:5e:10:00:0c';
const D1 = '02:00:5e:20:00:01';

// The focus model. `active` is what was last focused; reading it applies the browser's fixup:
// a node that is disabled or no longer in the page is no longer the active element.
let page = null;
// Attached means every step up is still in its parent's child list: the stub's replaceChildren
// drops children without clearing their parentNode, which a browser would.
const connected = n => {
  for (let p = n; p; p = p.parentNode) {
    if (p === page.root) return true;
    if (p.parentNode && p.parentNode.children.indexOf(p) < 0) return false;
  }
  return false;
};
Node.prototype.focus = function () { if (!this.disabled && connected(this)) page.active = this; };
Object.defineProperty(Node.prototype, 'isConnected', {get() { return connected(this); }});

function harness(options = {}) {
  const {byId, root} = pageFor(HTML);
  page = {root, active: null};
  const requests = [], dialogs = [], timers = [], intervals = [], listeners = {};
  const store = {luma_csrf: 'csrf-token-1'};
  const routes = options.routes || {};
  function fetch(url, init) {
    init = init || {};
    const record = {url: String(url), method: init.method || 'GET', body: init.body ? JSON.parse(init.body) : undefined};
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
  const document = {
    body: root, getElementById: id => byId[id] || null, createElement: tag => new Node(tag),
    createTextNode: t => { const n = new Node('#text'); n._text = t; return n; },
    querySelector: sel => root.querySelector(sel), querySelectorAll: sel => root.querySelectorAll(sel),
    hidden: false, addEventListener() {},
  };
  Object.defineProperty(document, 'activeElement', {get() {
    if (page.active && (page.active.disabled || !connected(page.active))) page.active = null;
    return page.active || root;
  }});
  // Opens as dialogs.js does: remembers what had focus, takes focus, and hands it back on close.
  function lumaConfirm(opts) {
    const opener = document.activeElement;
    const ok = new Node('button');
    root.appendChild(ok);
    ok.focus();
    return new Promise(resolve => {
      dialogs.push({opts, opener, close() {
        ok.remove();
        if (opener && typeof opener.focus === 'function') opener.focus();
        resolve(true);
      }});
    });
  }
  const sandbox = {
    document,
    window: {addEventListener: (type, fn) => { (listeners[type] = listeners[type] || []).push(fn); },
             toast() {}, lumaConfirm, lumaRemember: options.remember, lumaForgetCsrf() {}},
    sessionStorage: {getItem: k => store[k] || null, setItem: (k, v) => { store[k] = v; }, removeItem: k => { delete store[k]; }},
    location: {pathname: '/user', search: '', reload() {}, replace() {}},
    fetch, console, JSON, Promise, URL: {createObjectURL: () => 'blob:x', revokeObjectURL() {}},
    CSS: {escape: s => String(s)},
    Image: class extends Node { set src(v) { this._src = v; } },
    setInterval: (fn, ms) => { intervals.push([fn, ms]); return intervals.length; },
    clearInterval() {},
    setTimeout: (fn, ms) => { timers.push([fn, ms, true]); return timers.length; },
    clearTimeout() {},
    encodeURIComponent, Date,
  };
  sandbox.window.window = sandbox.window;
  vm.createContext(sandbox);
  vm.runInContext(SOURCE, sandbox, {filename: 'user.js'});
  return {byId, root, requests, dialogs, document, listeners};
}

const tick = () => new Promise(r => global.setTimeout(r, 5));
async function settle(n = 8) { for (let i = 0; i < n; i++) await tick(); }
function memory(seed = {}) {
  const data = Object.assign({}, seed);
  return {get: k => data[k] || '', set: (k, v) => { if (v) data[k] = String(v); else delete data[k]; }, data};
}

const STATE = routing => ({ok: true, role: 'user', username: 'alex', rev: 'r1', routing,
  sources: [{id: A, name: 'Alpha Camera', online: true}, {id: B, name: 'Bravo Camera', online: true}],
  destinations: [{id: D1, name: 'Lobby One', online: true, showing: {mode: 'none', source_id: '', source_name: ''}}],
  groups: [], walls: []});
const cell = (video, audio) => ({video, audio, video_n: video === 'all' ? 1 : 0, audio_n: audio === 'all' ? 1 : 0});
const MATRIX = sources => ({ok: true, role: 'user', rev: 'r1', interfaces: {standard: true, matrix: true},
  sources,
  rows: [{kind: 'display', id: D1, name: 'Lobby One', members: 1, online_members: 1, degraded: false, state: 'all',
          modes: ['av', 'video', 'audio'], cells: {[A]: cell('none', 'none'), [B]: cell('none', 'none')}}]});
const TWO = [{id: A, name: 'Alpha Camera', online: true}, {id: B, name: 'Bravo Camera', online: true}];
const THREE = TWO.concat([{id: C, name: 'Charlie Camera', online: true}]);

const OK_ONE = {ok: true, routed: 1, total: 1, summary: '1 of 1 display routed',
                results: [{index: 0, name: 'Lobby One', ok: true, result: 'verified', audio: 'follows'}]};
const FAILED = {ok: false, routed: 0, total: 1, summary: '0 of 1 display routed',
                results: [{index: 0, name: 'Lobby One', ok: false, result: 'unreachable'}]};

const BOTH = {standard: true, matrix: true}, STD = {standard: true, matrix: false};

function routes(routing, extra = {}) {
  return Object.assign({
    'GET /api/auth/session': {status: 200, body: {ok: true, role: 'user', username: 'alex', csrf_token: 'csrf-token-1', method: 'password'}},
    'GET /api/user/state': {status: 200, body: STATE(routing)},
    'GET /api/user/matrix': {status: 200, body: MATRIX(TWO)},
    'GET /api/user/preview': {status: 200, type: 'image/jpeg', body: {}},
    'POST /api/user/route_multi': {status: 200, body: OK_ONE},
  }, extra);
}

const cellAt = (h, src) => h.byId.mx_table.querySelectorAll('button.mx-cell')
  .filter(c => c.attrs['data-row'] === 'display:' + D1 && c.attrs['data-src'] === src)[0];
const cardAt = (h, host, kind, id) => h.byId[host].querySelectorAll('[data-kind]')
  .filter(c => c.attrs['data-kind'] === kind && c.attrs['data-id'] === id)[0];

(async () => {
  let passed = 0;
  async function scenario(name, fn) {
    try { await fn(); passed++; }
    catch (err) { console.error('FAILED: ' + name + '\n  ' + (err && err.stack || err)); process.exitCode = 1; }
  }

  // The stub has to be able to lose focus, or every scenario below proves nothing.
  await scenario('the focus model drops focus from a control that is disabled or removed', async () => {
    const h = harness({routes: routes(STD), remember: memory()});
    await settle();
    const card = cardAt(h, 'sources', 'source', A);
    card.focus();
    assert.strictEqual(h.document.activeElement, card);
    card.disabled = true;
    assert.strictEqual(h.document.activeElement, h.root, 'a disabled control is not the active element');
    card.disabled = false;
    card.focus();
    h.byId.sources.replaceChildren();
    assert.strictEqual(h.document.activeElement, h.root, 'a removed control is not the active element');
  });

  await scenario('Matrix: the cell that was pressed still has focus after a successful route', async () => {
    const h = harness({routes: routes(BOTH), remember: memory({view: 'matrix'})});
    await settle();
    cellAt(h, A).focus();
    cellAt(h, A).click();
    await settle();
    assert.strictEqual(h.requests.filter(r => r.method === 'POST').length, 1, 'one route was sent');
    assert.strictEqual(h.document.activeElement, cellAt(h, A));
  });

  await scenario('Matrix: after the result dialog closes, focus is back on the cell that opened it', async () => {
    const h = harness({routes: routes(BOTH, {'POST /api/user/route_multi': {status: 200, body: FAILED}}),
                       remember: memory({view: 'matrix'})});
    await settle();
    cellAt(h, A).focus();
    cellAt(h, A).click();
    await settle();
    assert.strictEqual(h.dialogs.length, 1, 'the failed route raised its dialog');
    assert.strictEqual(h.dialogs[0].opener, cellAt(h, A), 'the dialog was opened from the cell, not from the page');
    assert.notStrictEqual(h.document.activeElement, cellAt(h, A), 'the dialog holds focus while it is open');
    h.dialogs[0].close();
    await settle();
    assert.strictEqual(h.document.activeElement, cellAt(h, A));
  });

  await scenario('Matrix: a grid rebuilt while the route ran gets focus back on the same row and source', async () => {
    let rebuilt = false;
    const h = harness({remember: memory({view: 'matrix'}), routes: routes(BOTH, {
      'GET /api/user/matrix': () => ({status: 200, body: MATRIX(rebuilt ? THREE : TWO)}),
      'POST /api/user/route_multi': () => { rebuilt = true; return {status: 200, body: OK_ONE}; }})});
    await settle();
    const before = cellAt(h, B);
    before.focus();
    before.click();
    await settle();
    assert.strictEqual(h.byId.mx_table.querySelectorAll('button.mx-cell').length, 3, 'a source was added to the grid');
    assert.notStrictEqual(cellAt(h, B), before, 'the cell was rebuilt');
    assert.strictEqual(h.document.activeElement, cellAt(h, B), 'found again by row and source');
  });

  // No route is running here, so nothing but the grid's own repaint can put focus back: this is
  // the second line of defence reached on its own, the way a live refresh reaches it.
  await scenario('Matrix: a grid rebuilt by a live refresh, with no route running, keeps focus on its cell', async () => {
    let changed = false;
    const h = harness({remember: memory({view: 'matrix'}), routes: routes(BOTH, {
      'GET /api/user/matrix': () => ({status: 200, body: MATRIX(changed ? THREE : TWO)}),
      'GET /api/user/revision': () => ({status: 200, body: {ok: true, rev: changed ? 'r2' : 'r1'}})})});
    await settle();
    const before = cellAt(h, A);
    before.focus();
    changed = true;                                              // an administrator added a source
    h.listeners.focus.forEach(fn => fn());
    await settle();
    assert.strictEqual(h.requests.filter(r => r.method === 'POST').length, 0, 'no route was sent');
    assert.strictEqual(h.byId.mx_table.querySelectorAll('button.mx-cell').length, 3, 'the grid was rebuilt');
    assert.notStrictEqual(cellAt(h, A), before);
    assert.strictEqual(h.document.activeElement, cellAt(h, A));
  });

  await scenario('Matrix: a place the person moved to while the route ran is not taken back', async () => {
    let h = null;
    h = harness({remember: memory({view: 'matrix'}), routes: routes(BOTH, {
      'POST /api/user/route_multi': () => { cellAt(h, B).focus(); return {status: 200, body: OK_ONE}; }})});
    await settle();
    cellAt(h, A).focus();
    cellAt(h, A).click();
    await settle();
    assert.strictEqual(h.document.activeElement, cellAt(h, B));
  });

  // The Route button is disabled while its route runs, so the page restores it at the end -- and
  // must not do that over a card the person chose in the meantime.
  await scenario('Standard: a place the person moved to while the route ran is not taken back', async () => {
    let h = null;
    h = harness({remember: memory(), routes: routes(STD, {
      'POST /api/user/route_multi': () => { cardAt(h, 'sources', 'source', B).focus(); return {status: 200, body: OK_ONE}; }})});
    await settle();
    cardAt(h, 'sources', 'source', A).click();
    cardAt(h, 'displays', 'display', D1).click();
    h.byId.route_btn.focus();
    h.byId.route_btn.click();
    await settle();
    assert.strictEqual(h.document.activeElement, cardAt(h, 'sources', 'source', B));
  });

  await scenario('Standard: a place the person moved to while the result dialog was open is not taken back', async () => {
    const h = harness({routes: routes(STD, {'POST /api/user/route_multi': {status: 200, body: FAILED}}), remember: memory()});
    await settle();
    cardAt(h, 'sources', 'source', A).click();
    cardAt(h, 'displays', 'display', D1).click();
    h.byId.route_btn.focus();
    h.byId.route_btn.click();
    await settle();
    h.dialogs[0].close();                                        // focus goes to the Route button...
    cardAt(h, 'sources', 'source', B).focus();                   // ...and the person moves on before the page reacts
    await settle();
    assert.strictEqual(h.document.activeElement, cardAt(h, 'sources', 'source', B));
  });

  await scenario('Matrix: a second press while a route is running sends nothing', async () => {
    let release = null;
    const h = harness({remember: memory({view: 'matrix'}), routes: routes(BOTH, {
      'POST /api/user/route_multi': () => new Promise(res => { release = () => res({status: 200, body: OK_ONE}); })})});
    await settle();
    cellAt(h, A).focus();
    cellAt(h, A).click();
    await settle();
    assert.strictEqual(cellAt(h, A).attrs['aria-disabled'], 'true', 'busy is said to assistive technology');
    assert.strictEqual(cellAt(h, A).disabled, false, 'busy does not disable the control that holds focus');
    cellAt(h, B).click();
    cellAt(h, A).click();
    assert.strictEqual(h.requests.filter(r => r.method === 'POST').length, 1);
    release();
    await settle();
    assert.strictEqual(cellAt(h, A).attrs['aria-disabled'], 'false');
  });

  // No dialog opens here, so only the end of the route can give the button its focus back: it was
  // disabled while the route ran and the page cannot know that without remembering it.
  await scenario('Standard: after a route that succeeds, focus is back on the Route button', async () => {
    const h = harness({routes: routes(STD), remember: memory()});
    await settle();
    cardAt(h, 'sources', 'source', A).click();
    cardAt(h, 'displays', 'display', D1).click();
    h.byId.route_btn.focus();
    h.byId.route_btn.click();
    assert.strictEqual(h.byId.route_btn.disabled, true, 'the button is disabled while the route runs');
    await settle();
    assert.strictEqual(h.dialogs.length, 0, 'no dialog opened');
    assert.strictEqual(h.document.activeElement, h.byId.route_btn);
  });

  await scenario('Standard: after the result dialog closes, focus is back on the Route button', async () => {
    const h = harness({routes: routes(STD, {'POST /api/user/route_multi': {status: 200, body: FAILED}}), remember: memory()});
    await settle();
    cardAt(h, 'sources', 'source', A).click();
    cardAt(h, 'displays', 'display', D1).click();
    assert.strictEqual(h.byId.route_btn.disabled, false, 'both chosen, Route is ready');
    h.byId.route_btn.focus();
    h.byId.route_btn.click();
    await settle();
    assert.strictEqual(h.dialogs.length, 1);
    h.dialogs[0].close();
    await settle();
    assert.strictEqual(h.document.activeElement, h.byId.route_btn);
  });

  await scenario('Standard: choosing a card by keyboard leaves focus on that card, which the page rebuilt', async () => {
    const h = harness({routes: routes(STD), remember: memory()});
    await settle();
    const before = cardAt(h, 'sources', 'source', B);
    before.focus();
    before.click();
    const after = cardAt(h, 'sources', 'source', B);
    assert.notStrictEqual(after, before, 'the card was rebuilt');
    assert.strictEqual(h.document.activeElement, after);
  });

  await scenario('Standard: a drop routes and focus stays on the source card that was dragged', async () => {
    const h = harness({routes: routes(STD), remember: memory()});
    await settle();
    const source = cardAt(h, 'sources', 'source', A);
    source.focus();
    const transfer = {setData() {}};
    source.fire('dragstart', {dataTransfer: transfer});
    cardAt(h, 'displays', 'display', D1).fire('drop', {dataTransfer: transfer});
    await settle();
    assert.strictEqual(h.requests.filter(r => r.method === 'POST').length, 1, 'the drop sent the one route request');
    assert.strictEqual(h.document.activeElement, cardAt(h, 'sources', 'source', A));
  });

  console.log(passed + ' scenarios passed');
})();
