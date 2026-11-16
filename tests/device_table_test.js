// The Device Manager table in the browser: link speed normalisation and presentation, the sort
// keys (IP numeric, version numeric, link speed numeric with unknown last), the sort indicator,
// and the shared chrome module running against a stub page.
//
// Functions are lifted from ui/index.html and run, so this measures behaviour rather than text.
'use strict';
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {Node} = require('./_dom');

const ROOT = path.join(__dirname, '..');
const read = rel => fs.readFileSync(path.join(ROOT, rel), 'utf8').replace(/\r\n/g, '\n');
let passed = 0;
function scenario(name, fn) {
  try { fn(); passed++; }
  catch (err) { console.error('FAILED: ' + name + '\n  ' + (err && err.stack || err)); process.exitCode = 1; }
}

function lift(source, name) {
  const at = source.search(new RegExp('(?:async\\s+)?function\\s+' + name + '\\s*\\('));
  assert.ok(at >= 0, name + ' not found');
  let depth = 0;
  for (let i = source.indexOf('{', at); i < source.length; i++) {
    if (source[i] === '{') depth++;
    else if (source[i] === '}' && --depth === 0) return source.slice(at, i + 1);
  }
  throw new Error('unbalanced ' + name);
}

const page = read('ui/index.html');

// ---------------------------------------------------------------- link speed
function speedSandbox() {
  const ctx = {};
  vm.createContext(ctx);
  vm.runInContext(lift(page, 'normalizeLinkSpeed') + '\n' + lift(page, 'renderLinkSpeedCell'), ctx);
  return ctx;
}

scenario('equivalent speeds normalise to one number of Mbps', () => {
  const {normalizeLinkSpeed} = speedSandbox();
  for (const raw of [1000, '1000', '1000M', '1000 Mbps', '1000m/Full', '1G', '1 Gbps', '1 gbit', 1.0e3]) {
    assert.strictEqual(normalizeLinkSpeed(raw), 1000, String(raw));
  }
  assert.strictEqual(normalizeLinkSpeed('100M'), 100);
  assert.strictEqual(normalizeLinkSpeed(2500), 2500);
  assert.strictEqual(normalizeLinkSpeed('2.5G'), 2500);
});

scenario('what is not a speed is unknown, never a speed and never zero', () => {
  const {normalizeLinkSpeed} = speedSandbox();
  for (const raw of [undefined, null, '', '   ', 'down', 'n/a', '0', 0, -5, NaN, '1T', {}, [], 'Mbps']) {
    assert.strictEqual(normalizeLinkSpeed(raw), null, JSON.stringify(raw));
  }
});

scenario('exactly gigabit is green, any other speed is red, unknown is neutral and is not called a failure', () => {
  const ctx = speedSandbox();
  const cell = () => ({dataset: {}, className: '', textContent: ''});
  const g = cell(); ctx.renderLinkSpeedCell(g, '1G');
  assert.strictEqual(g.textContent, '1000 Mbps');
  assert.ok(/\blink-ok\b/.test(g.className) && !/link-slow/.test(g.className));
  const slow = cell(); ctx.renderLinkSpeedCell(slow, 100);
  assert.strictEqual(slow.textContent, '100 Mbps');
  assert.ok(/\blink-slow\b/.test(slow.className));
  const fast = cell(); ctx.renderLinkSpeedCell(fast, 2500);          // as OmniSuite: exactly 1 Gbps is green, anything else red
  assert.ok(/\blink-slow\b/.test(fast.className) && !/link-ok/.test(fast.className));
  const odd = cell(); ctx.renderLinkSpeedCell(odd, 'auto');          // not a speed at all: neutral, never red
  assert.ok(/\blink-unknown\b/.test(odd.className));
  const none = cell(); ctx.renderLinkSpeedCell(none, null);
  assert.strictEqual(none.textContent, '—');
  assert.ok(/\blink-unknown\b/.test(none.className) && !/link-slow/.test(none.className));
  assert.ok(!/fail|down|error/i.test(none.textContent));
  assert.strictEqual(none.dataset.linkspeed, '');
  assert.strictEqual(g.dataset.linkspeed, '1000');
});

scenario('link speed colours come from state tokens, not literals', () => {
  const css = page.replace(/\/\*[\s\S]*?\*\//g, '');
  for (const cls of ['link-ok', 'link-slow', 'link-unknown']) {
    const rule = css.match(new RegExp('\\.' + cls + '\\s*\\{([^}]*)\\}'));
    assert.ok(rule, cls);
    assert.ok(/var\(--/.test(rule[1]) && !/#[0-9a-f]{3,8}\b/i.test(rule[1]), cls + ': ' + rule[1]);
  }
});

// -------------------------------------------------- the scan's link read, shown
scenario('the link speed shown is the scan\'s own read, then a plain field, and a down link is not a slow one', () => {
  const ctx = {};
  vm.createContext(ctx);
  vm.runInContext(lift(page, 'unitLinkSpeed'), ctx);
  assert.strictEqual(ctx.unitLinkSpeed({net: {linkspeed_mbps: 100}, linkspeed: 1000}), 100);
  assert.strictEqual(ctx.unitLinkSpeed({net: {linkspeed_mbps: null, linkspeed_raw: 'auto'}}), 'auto');
  assert.strictEqual(ctx.unitLinkSpeed({net: {linkspeed_mbps: null, linkspeed_raw: null, link_up: false}, linkspeed: undefined}), undefined);
  assert.strictEqual(ctx.unitLinkSpeed({linkspeed: '1G'}), '1G');
  assert.strictEqual(ctx.unitLinkSpeed(null), undefined);
});

const NEIGHBOUR = {SysName: 'Switch', ChassisID: '02:00:5e:00:00:01', MgmtIP: '192.0.2.1', PortDescr: 'GigabitEthernet1/0/32', PortID: 'Gi1/0/32',
  TTL: 120, Capabilities: ['Bridge, on', 'Router, on'], SysDescr: '<img src=x onerror=alert(1)>'};

scenario('the switch popup names what is known and says plainly what is not', () => {
  const ctx = {};
  vm.createContext(ctx);
  vm.runInContext(lift(page, 'lldpFacts'), ctx);
  const labels = rows => Array.from(rows, r => r[0]);   // plain Array: the sandbox's own arrays are a different realm
  const full = ctx.lldpFacts({net: {link_up: true, lldp: NEIGHBOUR}});
  assert.strictEqual(full.state, 'neighbor');
  assert.deepStrictEqual(labels(full.rows), ['Interface', 'Switch', 'Mgmt IP', 'Chassis ID', 'Port', 'TTL', 'Capabilities', 'Description']);
  assert.strictEqual(full.rows.find(r => r[0] === 'Port')[1], 'GigabitEthernet1/0/32 (Gi1/0/32)');
  assert.strictEqual(full.rows.find(r => r[0] === 'TTL')[1], '120 s');
  const same = ctx.lldpFacts({net: {link_up: true, lldp: {PortDescr: 'ge-0', PortID: 'ge-0'}}});
  assert.strictEqual(same.rows.find(r => r[0] === 'Port')[1], 'ge-0');
  assert.strictEqual(ctx.lldpFacts({net: {link_up: true, lldp: null}}).message, 'No LLDP neighbors reported.');
  assert.ok(/no link/.test(ctx.lldpFacts({net: {link_up: false}}).message));
  assert.ok(/not reported/.test(ctx.lldpFacts({}).message));
  assert.ok(/not reported/.test(ctx.lldpFacts(null).message));
  // Absent is not a neighbour: nothing is invented to fill the table.
  assert.deepStrictEqual(labels(ctx.lldpFacts({net: {link_up: true, lldp: {SysName: 'Only'}}}).rows), ['Interface', 'Switch']);
});

scenario('the popup is built from text nodes only, so device text cannot become markup', () => {
  const created = [];
  const document = {createElement: tag => { const n = new Node(tag); created.push(n); return n; }};
  const ctx = {document};
  vm.createContext(ctx);
  vm.runInContext(lift(page, 'lldpFacts') + '\n' + lift(page, 'renderLldpPopup'), ctx);
  const box = new Node('div');
  ctx.renderLldpPopup(box, {net: {link_up: true, lldp: NEIGHBOUR}});     // _dom's Node throws if innerHTML is assigned
  const text = box.textContent;
  assert.ok(text.includes('<img src=x onerror=alert(1)>'), 'the description is shown as literal text');
  assert.ok(text.includes('Connected switch'));
  assert.ok(!created.some(n => n.tagName === 'img'), 'no element was made from device text');
  ctx.renderLldpPopup(box, {net: {link_up: true, lldp: null}});
  assert.ok(box.textContent.includes('No LLDP neighbors reported.'));
  assert.ok(!box.textContent.includes('GigabitEthernet'), 'the previous unit\'s neighbour does not linger');
});

scenario('the MAC cell is the hover and keyboard target and nothing is requested on hover', () => {
  const body = page.slice(page.indexOf('function showLldpPopup'), page.indexOf('function paintSortIndicators'));
  assert.ok(!/fetch\(|XMLHttpRequest|api\(/.test(body), 'showing a switch makes no request');
  assert.ok(/td\.mac-lldp/.test(body) && /focusin/.test(body) && /focusout/.test(body) && /Escape/.test(body));
  assert.ok(/tdMAC\.tabIndex=0/.test(page));
});

// ------------------------------------------------------------------- sorting
function sortSandbox(units, key, dir) {
  const ctx = {units, sortKey: key, sortDir: dir, applyFirmwareRoleFilter: l => l, classifyRole: () => 'Encoder'};
  vm.createContext(ctx);
  for (const fn of ['ipToNum', 'normalizeLinkSpeed', 'unitLinkSpeed', 'compareVersions', 'getDisplayList']) vm.runInContext(lift(page, fn), ctx);
  return ctx;
}
const ips = ctx => ctx.getDisplayList().map(u => u.ip);

scenario('IP addresses sort numerically, not as text', () => {
  const units = ['192.168.1.100', '192.168.1.9', '192.168.1.20', '10.0.0.200', '192.168.10.1'].map(ip => ({ip}));
  assert.deepStrictEqual(ips(sortSandbox(units, 'ip', 'asc')), ['10.0.0.200', '192.168.1.9', '192.168.1.20', '192.168.1.100', '192.168.10.1']);
  assert.deepStrictEqual(ips(sortSandbox(units, 'ip', 'desc')).reverse(), ips(sortSandbox(units, 'ip', 'asc')));
});

scenario('versions sort as dotted numbers: 1.0.10 is newer than 1.0.9', () => {
  const units = [{ip: '198.18.0.1', version: 'V1.0.2.10'}, {ip: '198.18.0.2', version: 'V1.0.2.6'}, {ip: '198.18.0.3', version: '1.0.2'}, {ip: '198.18.0.4', version: 'V1.0.2.3'}];
  assert.deepStrictEqual(ips(sortSandbox(units, 'version', 'asc')), ['198.18.0.3', '198.18.0.4', '198.18.0.2', '198.18.0.1']);
  assert.deepStrictEqual(ips(sortSandbox(units, 'version', 'desc'))[0], '198.18.0.1');
});

scenario('link speed sorts by number, and an unknown speed sorts last in both directions', () => {
  const units = [{ip: '198.18.0.1', linkspeed: '1G'}, {ip: '198.18.0.2', linkspeed: 100}, {ip: '198.18.0.3'}, {ip: '198.18.0.4', linkspeed: 2500}, {ip: '198.18.0.5', linkspeed: 'down'}];
  const asc = ips(sortSandbox(units, 'linkspeed', 'asc'));
  const desc = ips(sortSandbox(units, 'linkspeed', 'desc'));
  assert.deepStrictEqual(asc.slice(0, 3), ['198.18.0.2', '198.18.0.1', '198.18.0.4']);
  assert.deepStrictEqual(desc.slice(0, 3), ['198.18.0.4', '198.18.0.1', '198.18.0.2']);
  assert.deepStrictEqual(asc.slice(3).sort(), ['198.18.0.3', '198.18.0.5']);
  assert.deepStrictEqual(desc.slice(3).sort(), ['198.18.0.3', '198.18.0.5']);
});

scenario('text columns sort case-insensitively with the address as the tiebreak', () => {
  const units = [{ip: '198.18.0.9', hostname: 'beta'}, {ip: '198.18.0.2', hostname: 'Alpha'}, {ip: '198.18.0.5', hostname: 'alpha'}];
  assert.deepStrictEqual(ips(sortSandbox(units, 'hostname', 'asc')), ['198.18.0.2', '198.18.0.5', '198.18.0.9']);
});

// ------------------------------------------------------------ sort indicator
scenario('the sorted header is marked and announced; the others are neutral; nothing uses opacity', () => {
  const mk = key => {
    const ind = {textContent: ''};
    const classes = new Set();
    const attrs = {};
    return {dataset: {key}, classList: {toggle: (c, on) => (on ? classes.add(c) : classes.delete(c)), has: c => classes.has(c)},
      setAttribute: (k, v) => { attrs[k] = v; }, attrs, ind, querySelector: () => ind, style: {}};
  };
  const ths = ['ip', 'hostname', 'mac', 'linkspeed'].map(mk);
  const ctx = {sortKey: 'linkspeed', sortDir: 'desc', document: {querySelectorAll: () => ths}};
  vm.createContext(ctx);
  vm.runInContext(lift(page, 'paintSortIndicators'), ctx);
  ctx.paintSortIndicators();
  const by = Object.fromEntries(ths.map(t => [t.dataset.key, t]));
  assert.strictEqual(by.linkspeed.ind.textContent, '↓');
  assert.strictEqual(by.linkspeed.attrs['aria-sort'], 'descending');
  assert.ok(by.linkspeed.classList.has('sorted'));
  for (const k of ['ip', 'hostname', 'mac']) {
    assert.strictEqual(by[k].ind.textContent, '');
    assert.strictEqual(by[k].attrs['aria-sort'], 'none');
    assert.ok(!by[k].classList.has('sorted'));
  }
  ctx.sortKey = 'ip'; ctx.sortDir = 'asc'; ctx.paintSortIndicators();
  assert.strictEqual(by.ip.ind.textContent, '↑');
  assert.strictEqual(by.linkspeed.ind.textContent, '');
  assert.ok(ths.every(t => t.style.opacity === undefined), 'a sticky header cell must not be faded with opacity');
});

scenario('every sortable header can be reached and operated from the keyboard', () => {
  const body = page.slice(page.indexOf('document.querySelectorAll("th.sortable").forEach'));
  const wired = body.slice(0, body.indexOf('paintSortIndicators();\n', body.indexOf('th.tabIndex')) + 30);
  assert.ok(/th\.tabIndex = 0/.test(wired));
  assert.ok(/e\.key==="Enter" \|\| e\.key===" "/.test(wired));
  assert.ok(/th\.click\(\)/.test(wired));
});

// ---------------------------------------------------- the shared chrome module
scenario('the chrome module publishes the measured stack and survives a missing page', () => {
  const src = read('ui/device-table-chrome.js');
  // Absent elements: it must do nothing and throw nothing.
  const bare = {document: {getElementById: () => null, querySelector: () => null, documentElement: {style: {setProperty() {}}}, body: {}},
    getComputedStyle: () => ({}), window: {}, requestAnimationFrame: f => f()};
  bare.window = bare; vm.createContext(bare);
  vm.runInContext(src, bare);
  assert.strictEqual(bare.deviceTableChrome, undefined);

  const props = {};
  const rect = (h, w = 100) => ({height: h, width: w, left: 0, top: 0});
  const el = (h, w) => ({getBoundingClientRect: () => rect(h, w), offsetHeight: h, clientHeight: h, offsetWidth: w, querySelectorAll: () => [], querySelector: () => null, addEventListener() {}});
  const cells = [el(40, 30), el(40, 120), el(40, 160)];
  const table = Object.assign(el(500, 900), {tHead: Object.assign(el(41, 900), {querySelectorAll: () => cells}), querySelector: () => null});
  const wrapper = Object.assign(el(300, 900), {querySelector: () => table});
  const ids = {app_header: el(70), scan_bar: el(60), firmware_bar: el(50), units_table_wrapper: wrapper,
    tbody: el(0), table_scroll_mirror: el(10), table_scroll_mirror_track: el(1)};
  const doc = {getElementById: id => ids[id] || null, querySelector: sel => (sel === '.footer-bar' ? el(40) : null),
    documentElement: {style: {setProperty: (k, v) => { props[k] = v; }}}, body: {}, fonts: null};
  const ctx = {document: doc, getComputedStyle: () => ({marginTop: '16px', marginBottom: '4px', borderTopWidth: '1px', borderBottomWidth: '1px'}),
    requestAnimationFrame: f => { f(); return 1; }, addEventListener() {}, visualViewport: null,
    ResizeObserver: class { observe() {} disconnect() {} }, MutationObserver: class { observe() {} disconnect() {} }};
  ctx.window = ctx; vm.createContext(ctx);
  vm.runInContext(src, ctx);
  assert.ok(ctx.deviceTableChrome, 'the module exposes its handle when the page has a table');
  // A bar's contribution is its margin box: 70 + 16 + 4, 60 + 16 + 4.
  assert.strictEqual(props['--shell-header-h'], '90px');
  assert.strictEqual(props['--scan-bar-h'], '80px');
  assert.strictEqual(props['--firmware-bar-h'], '70px');
  assert.strictEqual(props['--table-head-h'], '41px');
  assert.strictEqual(props['--frozen-columns-w'], '310px');   // the three frozen columns, measured
  assert.strictEqual(props['--shell-gutter'], '16px');
});

console.log(passed + ' scenarios passed');
