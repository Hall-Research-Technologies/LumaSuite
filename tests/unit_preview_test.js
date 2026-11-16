// The Device Manager's picture of a unit: the hover card and the popup window (ui/unit-preview.js).
// Driven for real with a scripted Image, clock and window, so "one request at a time", "stops when
// closed" and "never asks a unit that has its thumbnail off" are measurements, not source greps.
'use strict';
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const {Node} = require('./_dom.js');

const P = require('../ui/unit-preview.js');
const strip = s => s.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');

// ------------------------------------------------------------------ a scripted environment
function makeEnv() {
  const docHandlers = {};
  const nodes = {};
  const body = new Node('body');
  const doc = {
    body,
    addEventListener: (t, f) => { (docHandlers[t] = docHandlers[t] || []).push(f); },
    getElementById: id => nodes[id] || null,
    createElement: tag => {
      const n = new Node(tag);
      n.removeAttribute = k => { delete n.attrs[k]; delete n.src; };
      n.appendChild = c => { if (c.id) nodes[c.id] = c; return Node.prototype.appendChild.call(n, c); };
      return n;
    },
  };
  body.appendChild = c => { if (c.id) nodes[c.id] = c; return Node.prototype.appendChild.call(body, c); };
  const images = [];
  class FakeImage { constructor() { this.onload = null; this.onerror = null; images.push(this); } }
  const timers = [];
  const intervals = [];
  const log = {opened: [], said: [], blobs: [], revoked: []};
  const env = {
    document: doc, window: {innerWidth: 1000, innerHeight: 800},
    Image: FakeImage, now: () => 1000,
    setInterval: (f, ms) => { const t = {f, ms, live: true}; intervals.push(t); return t; },
    clearInterval: t => { if (t) t.live = false; },
    setTimeout: (f, ms) => { const t = {f, ms, live: true}; timers.push(t); return t; },
    clearTimeout: t => { if (t) t.live = false; },
    open: (u, n, f) => { log.opened.push({u, n, f}); return env.popupBlocked ? null : {}; },
    makeBlobURL: html => { log.blobs.push(html); return 'blob:test/' + log.blobs.length; },
    revokeBlobURL: u => log.revoked.push(u),
    say: m => log.said.push(m),
  };
  return {env, doc, docHandlers, images, timers, intervals, log, nodes};
}

function cell(state, ip) {
  const c = new Node('td'); c.className = 'preview-cell'; c.dataset = {ip: ip || '192.0.2.7'};
  const cb = new Node('input'); cb.className = 'preview-checkbox';
  cb.checked = state === 'on'; cb.indeterminate = state === 'unknown'; cb.disabled = state === 'busy';
  c.appendChild(cb);
  const a = new Node('a'); a.className = 'action-link preview-link';
  a.closest = sel => (sel === '.preview-link' ? a : sel === 'td.preview-cell' ? c : null);
  a.contains = n => n === a;
  c.querySelector = sel => (sel === 'input.preview-checkbox' ? cb : null);
  c.appendChild(a);
  return {c, cb, a};
}

(async () => {
  // -------------------------------------------------- the address is a plain IPv4 literal or nothing
  assert.strictEqual(P.thumbURL('192.0.2.7'), 'http://192.0.2.7/thumbnail/thumbnail1.jpg');
  ['', 'host.example', '192.0.2.7/../x', '192.0.2.7"><script>', '1.2.3', 'javascript:1', '192.0.2.7\n', null, undefined]
    .forEach(bad => assert.strictEqual(P.thumbURL(bad), '', 'refused: ' + JSON.stringify(bad)));
  assert.strictEqual(P.REFRESH_MS, 2000, 'the same interval as OmniSuite Device Info');

  // ------------------------------------------------------------------ the unit's own switch decides
  assert.strictEqual(P.stateOf(cell('on').c), 'on');
  assert.strictEqual(P.stateOf(cell('off').c), 'off');
  assert.strictEqual(P.stateOf(cell('unknown').c), 'unknown');
  assert.strictEqual(P.stateOf(cell('busy').c), 'busy');
  assert.strictEqual(P.stateOf(new Node('td')), 'none');
  ['off', 'unknown', 'busy', 'none'].forEach(s => assert.ok(P.whyNot(s).length > 20 && !/_/.test(P.whyNot(s)), 'a sentence for ' + s));

  // ------------------------------------------------------------ the single-flight loader
  {
    const t = makeEnv(); const got = []; let failed = 0;
    const loader = P.createLoader(t.env, 'http://192.0.2.7/thumbnail/thumbnail1.jpg', s => got.push(s), () => failed++);
    assert.strictEqual(loader.tick(), true);
    assert.strictEqual(loader.tick(), false, 'a second tick while one is outstanding starts nothing');
    assert.strictEqual(loader.tick(), false);
    assert.strictEqual(t.images.length, 1, 'one request outstanding, not three');
    assert.ok(/\?t=1000$/.test(t.images[0].src), 'cache-busted');
    t.images[0].onload();
    assert.deepStrictEqual(got, [t.images[0].src]);
    assert.strictEqual(loader.tick(), true, 'free again once the first answered');
    t.images[1].onerror();
    assert.strictEqual(failed, 1);
    // A silent unit: the watchdog frees the slot, so the next tick can try again, and a late answer from
    // the abandoned request is ignored.
    assert.strictEqual(loader.tick(), true);
    const silent = t.images[2];
    const dog = t.timers.filter(x => x.live).pop();
    assert.strictEqual(dog.ms, P.GIVE_UP_MS);
    dog.f();
    assert.strictEqual(failed, 2);
    assert.strictEqual(silent.onload, null, 'the abandoned request can no longer land');
    assert.strictEqual(loader.tick(), true);
    loader.stop();
    assert.strictEqual(loader.tick(), false, 'a stopped loader never starts again');
  }

  // --------------------------------------------------------------------------------- the hover card
  {
    const t = makeEnv();
    const hover = P.createHover(t.env);
    // Off / unknown / busy: never a request, never a card.
    ['off', 'unknown', 'busy'].forEach(s => hover.show({clientX: 10, clientY: 10}, cell(s).c));
    assert.strictEqual(t.images.length, 0, 'a unit with its thumbnail off is never asked for a picture');
    assert.strictEqual(hover.active, false);
    // A cell with no usable address: nothing.
    hover.show({clientX: 10, clientY: 10}, cell('on', 'not-an-address').c);
    assert.strictEqual(t.images.length, 0);

    hover.show({clientX: 10, clientY: 10}, cell('on').c);
    assert.strictEqual(t.images.length, 1, 'one request to open');
    assert.strictEqual(hover.active, true);
    const iv = t.intervals.filter(x => x.live);
    assert.strictEqual(iv.length, 1); assert.strictEqual(iv[0].ms, 2000);
    // Several ticks while the first has not answered: still one request.
    iv[0].f(); iv[0].f(); iv[0].f();
    assert.strictEqual(t.images.length, 1, 'slow unit: no pile-up');
    t.images[0].onload();
    const pic = t.nodes.hover_preview_img;
    assert.ok(pic && /^http:\/\/192\.0\.2\.7\/thumbnail\/thumbnail1\.jpg\?t=/.test(pic.src), 'the card shows the unit\'s picture');
    iv[0].f();
    assert.strictEqual(t.images.length, 2, 'next tick after an answer asks again');
    // Closing stops everything: no timer, and an answer that arrives later is ignored.
    const late = t.images[1];
    hover.hide();
    assert.strictEqual(hover.active, false);
    assert.strictEqual(t.intervals.filter(x => x.live).length, 0, 'no timer left behind');
    assert.strictEqual(t.nodes.hover_preview.style.display, 'none');
    late.onload && late.onload();
    assert.ok(!t.nodes.hover_preview_img.src, 'a late answer does not repaint a closed card');
    // A failing unit hides the card instead of leaving a broken image.
    hover.show({clientX: 10, clientY: 10}, cell('on').c);
    t.images[t.images.length - 1].onerror();
    assert.strictEqual(t.nodes.hover_preview.style.display, 'none');
    assert.strictEqual(hover.active, false);
    // Re-opening replaces rather than stacks.
    hover.show({clientX: 10, clientY: 10}, cell('on').c);
    hover.show({clientX: 10, clientY: 10}, cell('on').c);
    assert.strictEqual(t.intervals.filter(x => x.live).length, 1, 'one timer however many times it is opened');
  }

  // -------------------------------------------------------------------------------- the popup window
  {
    const t = makeEnv();
    assert.strictEqual(P.openPopup(t.env, cell('off').c), 'refused');
    assert.strictEqual(P.openPopup(t.env, cell('unknown').c), 'refused');
    assert.strictEqual(P.openPopup(t.env, cell('busy').c), 'refused');
    assert.strictEqual(t.log.opened.length, 0, 'no window for a unit that has its thumbnail off');
    assert.strictEqual(t.log.said.length, 3, 'and the operator is told why each time');
    assert.ok(/switched off/.test(t.log.said[0]));
    assert.strictEqual(P.openPopup(t.env, cell('on', 'bad address').c), 'refused');

    assert.strictEqual(P.openPopup(t.env, cell('on').c), 'opened');
    assert.strictEqual(t.log.opened.length, 1);
    assert.ok(/^blob:/.test(t.log.opened[0].u));
    assert.ok(/width=480,height=400/.test(t.log.opened[0].f));
    const html = t.log.blobs[0];
    assert.ok(html.indexOf('http://192.0.2.7/thumbnail/thumbnail1.jpg') > 0);
    const revoke = t.timers.filter(x => x.live).pop();
    revoke.f();
    assert.deepStrictEqual(t.log.revoked, ['blob:test/1'], 'the blob is released');
    // A blocked window is reported, not silent.
    t.env.popupBlocked = true;
    assert.strictEqual(P.openPopup(t.env, cell('on').c), 'blocked');
    assert.ok(/blocked/.test(t.log.said[t.log.said.length - 1]));
  }

  // The window's own page, run for real: single-flight, an honest message, and no timer after it closes.
  {
    const html = P.popupHTML('http://192.0.2.7/thumbnail/thumbnail1.jpg');
    assert.ok(!/<script>[\s\S]*<\/script>[\s\S]*<script>/.test(html), 'exactly one script element');
    const script = html.slice(html.indexOf('<script>') + 8, html.indexOf('</script>'));
    const pic = new Node('img'); pic.style = {};
    const note = new Node('div'); note.style = {};
    const imgs = []; const timers = []; const ivs = []; const handlers = {};
    class Img { constructor() { imgs.push(this); } }
    const ctx = {
      document: {getElementById: id => (id === 'preview' ? pic : note)},
      Image: Img, Date: {now: () => 5},
      setInterval: (f, ms) => { const t = {f, ms, live: true}; ivs.push(t); return t; },
      clearInterval: t => { t.live = false; },
      setTimeout: (f, ms) => { const t = {f, ms, live: true}; timers.push(t); return t; },
      clearTimeout: t => { if (t) t.live = false; },
      addEventListener: (type, f) => { handlers[type] = f; },
    };
    vm.createContext(ctx); vm.runInContext(script, ctx);
    assert.strictEqual(ivs.length, 1); assert.strictEqual(ivs[0].ms, 2000);
    assert.strictEqual(imgs.length, 1, 'one request on open');
    ivs[0].f(); ivs[0].f();
    assert.strictEqual(imgs.length, 1, 'no overlap while the unit is slow');
    imgs[0].onload();
    assert.ok(/thumbnail1\.jpg\?t=5$/.test(pic.src)); assert.strictEqual(note.style.display, 'none');
    ivs[0].f();
    assert.strictEqual(imgs.length, 2);
    imgs[1].onerror();
    assert.strictEqual(note.style.display, 'block', 'a unit that does not answer says so');
    assert.strictEqual(pic.style.display, 'none');
    ivs[0].f();
    assert.strictEqual(imgs.length, 3, 'and is tried again');
    imgs[2].onload();
    assert.strictEqual(note.style.display, 'none', 'recovers when it answers');
    handlers.pagehide();
    assert.strictEqual(ivs[0].live, false, 'closing the window stops its refresh');

    // Hostile text can never end the script element.
    const hostile = P.popupHTML('http://x/</script><script>alert(1)</script>');
    assert.strictEqual((hostile.match(/<script>/g) || []).length, 1);
    assert.strictEqual((hostile.match(/<\/script>/g) || []).length, 1);
  }

  // -------------------------------------------------------------------------- the page wiring, for real
  {
    const t = makeEnv();
    const hover = P.wire(t.env);
    const {c, cb, a} = cell('on');
    const fire = (type, e) => (t.docHandlers[type] || []).forEach(f => f(e));
    // Over the checkbox: nothing.
    fire('mouseover', {target: {closest: () => null}});
    assert.strictEqual(t.images.length, 0);
    // Over the word: the card.
    fire('mouseover', {target: a, clientX: 5, clientY: 5});
    assert.strictEqual(t.images.length, 1);
    assert.strictEqual(hover.active, true);
    // Moving within the word keeps it; leaving it closes it.
    fire('mouseout', {target: a, relatedTarget: a});
    assert.strictEqual(hover.active, true);
    fire('mouseout', {target: a, relatedTarget: null});
    assert.strictEqual(hover.active, false);
    assert.strictEqual(t.intervals.filter(x => x.live).length, 0);
    // A click on the word: hides the card, opens the window, and prevents navigation.
    fire('mouseover', {target: a, clientX: 5, clientY: 5});
    let prevented = false;
    fire('click', {target: a, preventDefault() { prevented = true; }});
    assert.ok(prevented); assert.strictEqual(hover.active, false);
    assert.strictEqual(t.log.opened.length, 1);
    // Keyboard: Enter and Space on the word do the same; other keys do not.
    fire('keydown', {target: a, key: 'Enter', preventDefault() {}});
    fire('keydown', {target: a, key: ' ', preventDefault() {}});
    fire('keydown', {target: a, key: 'x', preventDefault() {}});
    assert.strictEqual(t.log.opened.length, 3);
    // A click that is not on the word does nothing here (the checkbox has its own handler).
    fire('click', {target: cb, preventDefault() {}});
    assert.strictEqual(t.log.opened.length, 3);
    // With the switch turned off, the word explains instead of opening.
    cb.checked = false;
    fire('click', {target: a, preventDefault() {}});
    assert.strictEqual(t.log.opened.length, 3);
    assert.ok(/switched off/.test(t.log.said[t.log.said.length - 1]));
  }

  // ----------------------------------------------------------- the module never writes to a unit
  const source = strip(fs.readFileSync(path.join(__dirname, '..', 'ui', 'unit-preview.js'), 'utf8'));
  assert.ok(!/fetch\s*\(|XMLHttpRequest|method\s*:\s*['"]POST/.test(source), 'no writes and no API calls from the preview module');
  assert.ok(!/\b(alert|confirm|prompt)\s*\(/.test(source), 'no native dialogs');
  assert.ok(!/innerHTML\s*=/.test(source));
  console.log('unit preview: ok');
})().catch(e => { console.error(e); process.exit(1); });
