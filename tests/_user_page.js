// The restricted routing page (ui/user.js) running for real against the shared DOM stub and a
// fetch whose answers and delays the test chooses. Shared by the page tests and the benchmark so
// both drive exactly the same page.
//
// Previews are off in this harness (the remembered choice says so), which keeps picture traffic
// out of what is being measured; routing never depends on a picture.
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {Node, pageFor} = require('./_dom');

const ROOT = path.join(__dirname, '..');

const sleep = ms => new Promise(r => setTimeout(r, ms));
const now = () => Number(process.hrtime.bigint() / 1000n) / 1000;

function textNode(text) { const n = new Node('#text'); n.textContent = text; return n; }

function unit(id, name, extra) { return Object.assign({id, name, online: true, showing: 'Nothing'}, extra || {}); }

const SOURCES = [unit('src-a', 'Alpha'), unit('src-b', 'Bravo'), unit('src-c', 'Charlie')];
const DISPLAYS = [unit('dsp-1', 'One'), unit('dsp-2', 'Two'), unit('dsp-3', 'Three'), unit('dsp-4', 'Four'),
                  unit('dsp-5', 'Five')];
const GROUPS = [unit('grp-a', 'Group A', {members: 2, online_members: 2}),
                unit('grp-b', 'Group B', {members: 2, online_members: 2})];

function stateBody(over) {
  return Object.assign({ok: true, username: 'alex', rev: 'r1', routing: {standard: true, matrix: true},
                        sources: SOURCES, destinations: DISPLAYS, groups: GROUPS, walls: []}, over || {});
}

function matrixBody(over) {
  const rows = DISPLAYS.map(d => ({kind: 'display', id: d.id, name: d.name, modes: ['av', 'video', 'audio'],
                                   online_members: 1, members: 1, state: 'all', cells: {}}));
  return Object.assign({ok: true, rev: 'r1', sources: SOURCES, rows}, over || {});
}

/**
 * @param {object} opts
 *   source   path of the script to run (default: the page as it is in this tree)
 *   routes   {'METHOD /path': spec | (rec) => spec};  spec = {status, body, delay}
 */
async function openPage(opts) {
  opts = opts || {};
  const html = fs.readFileSync(path.join(ROOT, 'ui', 'user.html'), 'utf8');
  const code = opts.code || fs.readFileSync(opts.source || path.join(ROOT, 'ui', 'user.js'), 'utf8');
  const {byId, root} = pageFor(html);
  const requests = [];
  const toasts = [];
  const confirms = [];
  const store = {preview: 'off'};

  const doc = {
    body: root, activeElement: null, hidden: false, readyState: 'complete',
    getElementById: id => byId[id] || null,
    createElement: tag => new Node(tag),
    createTextNode: textNode,
    querySelectorAll: sel => root.querySelectorAll(sel),
    querySelector: sel => root.querySelector(sel),
    addEventListener() {},
  };
  const timers = [];
  const win = {
    addEventListener() {},
    toast: (message, kind) => toasts.push({message, kind, at: now()}),
    lumaConfirm: o => { confirms.push(o); return Promise.resolve(true); },
    lumaRemember: {get: k => store[k] || '', set: (k, v) => { store[k] = v; }},
  };
  const routes = Object.assign({
    'GET /api/auth/session': {status: 200, body: {ok: true, method: 'password', local: false}},
    'GET /api/user/state': {status: 200, body: stateBody()},
    'GET /api/user/matrix': {status: 200, body: matrixBody()},
    'GET /api/user/revision': {status: 200, body: {ok: true, rev: 'r1'}},
  }, opts.routes || {});

  function fetch(url, init) {
    init = init || {};
    const rec = {url: String(url), method: init.method || 'GET',
                 body: typeof init.body === 'string' ? JSON.parse(init.body) : undefined,
                 sent: now()};
    requests.push(rec);
    let spec = routes[rec.method + ' ' + rec.url.split('?')[0]];
    // A handler may hand back a promise (a held answer): the request is answered when it settles.
    return Promise.resolve(typeof spec === 'function' ? spec(rec) : spec).then(found => {
      spec = found || {status: 200, body: {ok: true}};
      return sleep(spec.delay || 0);
    }).then(() => {
      rec.answered = now();
      return {ok: spec.status >= 200 && spec.status < 300, status: spec.status,
              headers: {get: () => null},
              json: () => Promise.resolve(spec.body === undefined ? {} : spec.body),
              blob: () => Promise.resolve({size: 8})};
    });
  }

  const sandbox = {
    document: doc, window: win, fetch, JSON, Promise, Date, Math, Object, Array, String, Number, Boolean,
    console, CSS: {escape: s => String(s).replace(/[^\w-]/g, '\\$&')},
    URL: {createObjectURL: () => 'blob:x', revokeObjectURL() {}},
    sessionStorage: {getItem: () => 'tok', setItem() {}, removeItem() {}},
    location: {pathname: '/user', replace() {}, reload() {}},
    // `fastTimers` lets a test run the page's waits (a confirmation look at 1.5 s, 3 s, 5 s) at once.
    setTimeout: (fn, ms) => { const t = setTimeout(fn, opts.fastTimers && ms >= 1000 ? 1 : ms); timers.push(t); return t; },
    clearTimeout, setInterval: () => 0, clearInterval() {}, encodeURIComponent,
    performance: {now}, requestAnimationFrame: fn => setTimeout(fn, 0),
  };
  win.document = doc;
  sandbox.window = Object.assign(win, {location: sandbox.location, performance: sandbox.performance});
  vm.createContext(sandbox);
  vm.runInContext(code, sandbox);

  const page = {
    byId, root, requests, toasts, confirms, sandbox, now, sleep,
    cards(kind) { return root.querySelectorAll('[data-kind]').filter(n => n.getAttribute('data-kind') === kind); },
    card(kind, id) { return page.cards(kind).filter(n => n.getAttribute('data-id') === id)[0] || null; },
    cell(row, src) {
      return root.querySelectorAll('button.mx-cell')
        .filter(n => n.getAttribute('data-row') === row && n.getAttribute('data-src') === src)[0] || null;
    },
    routeRequests() { return requests.filter(r => r.url.split('?')[0] === '/api/user/route_multi'); },
    /** Drag a source onto a destination card exactly as a person does. */
    drop(sourceId, kind, id) {
      const src = page.card('source', sourceId), dst = page.card(kind, id);
      src.fire('dragstart', {dataTransfer: {setData() {}}});
      dst.fire('dragenter');
      dst.fire('dragover');
      dst.fire('drop');
      src.fire('dragend');
    },
    async ready() { for (let i = 0; i < 50 && !page.card('source', 'src-a'); i++) await sleep(2); },
    stop() { timers.forEach(clearTimeout); },
  };
  await page.ready();
  return page;
}

module.exports = {openPage, stateBody, matrixBody, SOURCES, DISPLAYS, GROUPS, sleep, now};
