// A tiny DOM for driving the browser scripts without a browser. Shared by the page tests.
//
// Deliberately strict: assigning innerHTML throws, so a script that builds markup from
// device-controlled text fails its test instead of passing it.
'use strict';

// ----------------------------------------------------------------- a tiny DOM
class ClassList {
  constructor(node) { this.node = node; this.set = new Set(); }
  add(c) { this.set.add(c); this.node._cls = [...this.set].join(' '); }
  remove(c) { this.set.delete(c); this.node._cls = [...this.set].join(' '); }
  contains(c) { return this.set.has(c); }
  toggle(c, force) { if (force === undefined ? !this.set.has(c) : force) this.add(c); else this.remove(c); }
}
class Node {
  constructor(tag) {
    this.tagName = tag; this.children = []; this.attrs = {}; this.handlers = {};
    this._text = ''; this._cls = ''; this.hidden = false; this.disabled = false;
    this.classList = new ClassList(this); this.parentNode = null; this.style = {};
  }
  set className(v) { this._cls = v; this.classList.set = new Set(String(v).split(/\s+/).filter(Boolean)); }
  get className() { return this._cls; }
  set textContent(v) { this._text = String(v); this.children = []; }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(''); }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  appendChild(c) { c.parentNode = this; this.children.push(c); return c; }
  insertBefore(c, ref) {
    c.parentNode = this;
    const at = ref ? this.children.indexOf(ref) : -1;
    if (at < 0) this.children.push(c); else this.children.splice(at, 0, c);
    return c;
  }
  // A <select>: its options are its children, and the selection follows its value.
  get options() { return this.children; }
  get selectedIndex() {
    const at = this.children.findIndex(o => o.value === this.value);
    return at < 0 ? 0 : at;
  }
  get firstChild() { return this.children[0] || null; }
  get lastChild() { return this.children[this.children.length - 1] || null; }
  get nextSibling() {
    const siblings = this.parentNode ? this.parentNode.children : [];
    return siblings[siblings.indexOf(this) + 1] || null;
  }
  remove() {
    if (!this.parentNode) return;
    const at = this.parentNode.children.indexOf(this);
    if (at >= 0) this.parentNode.children.splice(at, 1);
    this.parentNode = null;
  }
  replaceChildren(...kids) { this.children = []; this._text = ''; kids.forEach(k => this.appendChild(k)); }
  addEventListener(type, fn) { (this.handlers[type] = this.handlers[type] || []).push(fn); }
  click() { if (this.disabled) return; this.fire('click'); }
  focus() {}
  select() {}
  // Deliver an event whatever the control's state: a browser can still deliver one that was
  // already queued when the button was disabled, so the page's own guard must hold by itself.
  // `extra` lets a test carry a payload (a drag's dataTransfer); the event records preventDefault.
  fire(type, extra) {
    const event = Object.assign({target: this, defaultPrevented: false,
                                 preventDefault() { this.defaultPrevented = true; }}, extra || {});
    (this.handlers[type] || []).forEach(fn => fn(event));
    return event;
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  querySelectorAll(sel) {
    const out = [];
    const walk = n => { n.children.forEach(c => { if (matches(c, sel)) out.push(c); walk(c); }); };
    walk(this); return out;
  }
  set innerHTML(v) { throw new Error('innerHTML must not be used on the restricted page'); }
}
function matches(node, sel) {
  let m = sel.match(/^\[([\w-]+)(?:="(.*)")?\]$/);
  if (m) return m[1] in node.attrs && (m[2] === undefined || node.attrs[m[1]] === m[2]);
  m = sel.match(/^\.([\w-]+)$/);
  if (m) return node.classList.contains(m[1]);
  m = sel.match(/^([a-z][a-z0-9]*)\.([\w-]+)$/);
  if (m) return node.tagName === m[1] && node.classList.contains(m[2]);
  if (/^[a-z][a-z0-9]*$/.test(sel)) return node.tagName === sel;
  return false;
}


// Every id in an HTML file, as a node, so a script finds the elements it expects.
function pageFor(html) {
  const byId = {};
  [...html.matchAll(/<(\w+)[^>]* id="([\w-]+)"[^>]*>/g)].forEach(m => {
    const node = new Node(m[1]);
    // The structure scripts reach into: a table has a body, a checklist has its parts.
    if (m[1] === 'table') node.appendChild(new Node('tbody'));
    if (/class="assign"/.test(m[0])) {
      node.appendChild(new Node('ul'));
      const search = new Node('input'); search.className = 'search'; node.appendChild(search);
      const count = new Node('span'); count.className = 'count'; node.appendChild(count);
      const all = new Node('button'); all.attrs['data-all'] = ''; node.appendChild(all);
      const none = new Node('button'); none.attrs['data-none'] = ''; node.appendChild(none);
    }
    byId[m[2]] = node;
  });
  const root = new Node('body');
  Object.values(byId).forEach(n => root.appendChild(n));
  return {byId, root};
}

module.exports = {Node, ClassList, matches, pageFor};
