// The shared confirmation dialog's optional field (a passphrase), driven for real. The passphrase must
// reach the caller only when the operator confirms, must be wiped from the box either way, and Enter in
// the box must be an answer. A dialog without a field must be exactly what it was.
'use strict';
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'ui', 'dialogs.js'), 'utf8');

// A fake DOM just big enough for the dialog: setting innerHTML on the backdrop materialises one node per
// class named in the markup, which querySelector then finds.
class N {
  constructor() {
    this.attrs = {}; this.handlers = {}; this.style = {}; this.hidden = false; this.value = ''; this.checked = false;
    this._text = ''; this.classes = new Set(); this.children = [];
    const self = this;
    this.classList = {add: c => self.classes.add(c), remove: c => self.classes.delete(c),
                      toggle: (c, on) => { if (on) self.classes.add(c); else self.classes.delete(c); },
                      contains: c => self.classes.has(c)};
  }
  set className(v) { this.classes = new Set(String(v).split(/\s+/).filter(Boolean)); }
  get className() { return [...this.classes].join(' '); }
  set textContent(v) { this._text = String(v); }
  get textContent() { return this._text; }
  setAttribute(k, v) { this.attrs[k] = v; }
  addEventListener(t, f) { (this.handlers[t] = this.handlers[t] || []).push(f); }
  removeEventListener(t, f) { this.handlers[t] = (this.handlers[t] || []).filter(x => x !== f); }
  focus() { this.focused = true; }
  appendChild(c) { this.children.push(c); return c; }
  querySelector(sel) { return this.parts && this.parts[sel.replace(/^\./, '')] || null; }
  set innerHTML(html) {
    this._html = html;
    if (this.isBackdrop) {
      this.parts = {};
      for (const m of String(html).matchAll(/class="([^"]+)"/g)) {
        const first = m[1].split(/\s+/)[0];
        const n = new N(); n.className = m[1]; this.parts[first] = n;
        m[1].split(/\s+/).forEach(c => { this.parts[c] = n; });
      }
      this.parts['confirm-field-input'].type = 'password';
    }
  }
  get innerHTML() { return this._html || ''; }
}

function page() {
  const docHandlers = {};
  const byId = {};
  const doc = {
    body: new N(), activeElement: null,
    getElementById: id => byId[id] || null,
    createElement: () => { const n = new N(); return n; },
    addEventListener: (t, f) => { (docHandlers[t] = docHandlers[t] || []).push(f); },
    removeEventListener: (t, f) => { docHandlers[t] = (docHandlers[t] || []).filter(x => x !== f); },
  };
  doc.body.appendChild = c => { if (c.id) byId[c.id] = c; return c; };
  const origCreate = doc.createElement;
  doc.createElement = tag => { const n = origCreate(tag); if (tag === 'div') n.isBackdrop = true; Object.defineProperty(n, 'id', {get() { return n._id; }, set(v) { n._id = v; }}); return n; };
  const ctx = {document: doc, window: {setTimeout: f => { f(); return 0; }, clearTimeout() {}}, localStorage: {getItem: () => null, setItem() {}},
               Promise, String, Array};
  ctx.window.window = ctx.window;
  vm.createContext(ctx);
  vm.runInContext(source, ctx);
  const backdrop = () => byId.lumaConfirmBackdrop;
  const key = (key, target) => (docHandlers.keydown || []).forEach(f => f({key, target, preventDefault() {}}));
  return {confirm: ctx.window.lumaConfirm, backdrop, key, doc};
}

(async () => {
  // ----------------------------------------------------- with a field: confirmed -> the value reaches the caller
  let p = page(); let got = null;
  let promise = p.confirm({title: 'Export', message: 'm', confirmText: 'Export',
    field: {type: 'password', label: 'Backup passphrase', placeholder: 'At least 8', onValue: v => { got = v; }}});
  let b = p.backdrop();
  const input = b.querySelector('.confirm-field-input'), wrap = b.querySelector('.confirm-field');
  assert.strictEqual(wrap.hidden, false, 'the field is shown');
  assert.strictEqual(input.type, 'password'); assert.strictEqual(input.placeholder, 'At least 8');
  assert.strictEqual(b.querySelector('.confirm-field-label').textContent, 'Backup passphrase');
  assert.strictEqual(input.focused, true, 'focus goes to the field, where the answer is typed');
  input.value = 'correct horse';
  b.querySelector('.confirm-ok').handlers.click[0]();
  assert.strictEqual(await promise, true);
  assert.strictEqual(got, 'correct horse');
  assert.strictEqual(input.value, '', 'the passphrase is wiped from the box');

  // ----------------------------------------------------- cancelled -> the caller never sees what was typed
  p = page(); got = 'untouched';
  promise = p.confirm({title: 'Export', field: {onValue: v => { got = v; }}});
  b = p.backdrop();
  b.querySelector('.confirm-field-input').value = 'secret';
  b.querySelector('.confirm-cancel').handlers.click[0]();
  assert.strictEqual(await promise, false);
  assert.strictEqual(got, 'untouched', 'Cancel delivers nothing');
  assert.strictEqual(b.querySelector('.confirm-field-input').value, '', 'and still wipes the box');

  // ----------------------------------------------------- Escape is a cancel; Enter in the field is the answer
  p = page(); got = 'untouched';
  promise = p.confirm({title: 'Export', field: {onValue: v => { got = v; }}});
  b = p.backdrop();
  b.querySelector('.confirm-field-input').value = 'via enter';
  p.key('Enter', b.querySelector('.confirm-field-input'));
  assert.strictEqual(await promise, true); assert.strictEqual(got, 'via enter');
  p = page(); got = 'untouched';
  promise = p.confirm({title: 'Export', field: {onValue: v => { got = v; }}});
  b = p.backdrop();
  b.querySelector('.confirm-field-input').value = 'x';
  p.key('Escape', b.querySelector('.confirm-field-input'));
  assert.strictEqual(await promise, false); assert.strictEqual(got, 'untouched');
  // Enter anywhere else is not an answer.
  p = page();
  promise = p.confirm({title: 'Export', field: {}});
  p.key('Enter', {});
  let settled = false; promise.then(() => { settled = true; });
  await new Promise(r => setImmediate(r));
  assert.strictEqual(settled, false, 'Enter outside the field does not confirm');
  p.backdrop().querySelector('.confirm-cancel').handlers.click[0]();
  await promise;

  // ----------------------------------------------------- without a field: unchanged, and the box is hidden
  p = page();
  promise = p.confirm({title: 'Sure?', danger: true});
  b = p.backdrop();
  assert.strictEqual(b.querySelector('.confirm-field').hidden, true, 'no field unless asked for');
  assert.strictEqual(b.querySelector('.confirm-cancel').focused, true, 'a costly action still puts Cancel under the cursor');
  b.querySelector('.confirm-ok').handlers.click[0]();
  assert.strictEqual(await promise, true);
  // A field-bearing call followed by a plain one leaves no field behind.
  p = page();
  promise = p.confirm({title: 'A', field: {onValue() {}}}); p.backdrop().querySelector('.confirm-cancel').handlers.click[0](); await promise;
  promise = p.confirm({title: 'B'});
  assert.strictEqual(p.backdrop().querySelector('.confirm-field').hidden, true, 'whatever a dialog shows for one caller it hides for the next');
  p.backdrop().querySelector('.confirm-ok').handlers.click[0](); await promise;

  // ----------------------------------------------------- a third action is its own answer, never Cancel or OK
  p = page();
  promise = p.confirm({title: 'Clear', danger: true, secondary: {text: 'View Assignments', value: 'assignments'}});
  b = p.backdrop();
  assert.strictEqual(b.querySelector('.confirm-extra').hidden, false, 'the third button is shown when asked for');
  assert.strictEqual(b.querySelector('.confirm-extra').textContent, 'View Assignments');
  b.querySelector('.confirm-extra').handlers.click[0]();
  assert.strictEqual(await promise, 'assignments', 'it resolves to its own value');
  assert.ok(await promise !== false && await promise !== true);
  // A caller that never asked for one never sees it, and it does not linger for the next caller.
  p = page();
  promise = p.confirm({title: 'A', secondary: {text: 'More', value: 'more'}}); p.backdrop().querySelector('.confirm-cancel').handlers.click[0](); await promise;
  promise = p.confirm({title: 'B'});
  assert.strictEqual(p.backdrop().querySelector('.confirm-extra').hidden, true, 'hidden again for the next caller');
  p.backdrop().querySelector('.confirm-ok').handlers.click[0]();
  assert.strictEqual(await promise, true, 'a plain dialog still answers true/false only');
  // Escape and Cancel stay false with a third action present; the third action never delivers a field value.
  p = page(); got = 'untouched';
  promise = p.confirm({title: 'C', field: {onValue: v => { got = v; }}, secondary: {text: 'Other', value: 'other'}});
  b = p.backdrop(); b.querySelector('.confirm-field-input').value = 'typed';
  b.querySelector('.confirm-extra').handlers.click[0]();
  assert.strictEqual(await promise, 'other'); assert.strictEqual(got, 'untouched');

  // ----------------------------------------------------- source rules
  const code = source.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
  assert.ok(!/localStorage\.setItem\([^)]*field/.test(code), 'a passphrase is never stored');
  console.log('dialog field: ok');
})().catch(e => { console.error(e); process.exit(1); });
