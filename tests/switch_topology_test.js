// Multi-switch awareness in the Device Manager, driven as a module against a stub page: the advisory's
// wording and counts, when it shows, what dismissing it does and does not do, the "Different switch" note,
// and the wiring in ui/index.html. The module is RUN, so an undefined helper fails here and not in front of
// an operator. Ported from OmniSuite's lldp_topology_test.js scenarios, adapted to Luma's page.
'use strict';
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {Node} = require('./_dom');

const ROOT = path.join(__dirname, '..');
const read = rel => fs.readFileSync(path.join(ROOT, rel), 'utf8').replace(/\r\n/g, '\n');
const stripComments = s => s.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:\\'"`])\/\/[^\n]*/g, '$1');
const MODULE = read('ui/switch-topology.js');
const PAGE = read('ui/index.html');

let passed = 0;
const queue = [];
// Scenarios share a rendered page and a request log, so they run one after another.
function scenario(name, fn) { queue.push([name, fn]); }

function world(options = {}) {
  const banner = new Node('div'); banner.hidden = true;
  const title = new Node('div'); title.className = 'switch-warning-title';
  const detail = new Node('div'); detail.className = 'switch-warning-detail';
  banner.appendChild(title); banner.appendChild(detail);
  const button = new Node('button'); button.dataset = {};
  const byId = {switch_topology_warning: banner, switch_topology_dismiss: button};
  const cells = [];
  const requests = [];
  const toasts = [];
  const timers = [];
  const doc = {
    readyState: 'complete',
    getElementById: id => byId[id] || null,
    createElement: tag => { const n = new Node(tag); n.ownerDocument = doc; return n; },
    querySelectorAll: sel => (sel === '#tbody td.mac-lldp[data-ip]' ? cells : []),
    addEventListener() {},
  };
  const win = {
    document: doc, console: {error() {}, debug() {}},
    timers,
    setTimeout: (fn, ms) => { timers.push({fn, ms}); return timers.length; },
    clearTimeout: id => { if (timers[id - 1]) timers[id - 1].cancelled = true; },
    toast: (...a) => toasts.push(a),
    fetch: (url, init) => {
      requests.push({url, init});
      if (options.failAck) return Promise.reject(new Error('offline'));
      return Promise.resolve({json: () => Promise.resolve(options.ackBody || {ok: true, acknowledged: true})});
    },
  };
  win.window = win;
  const ctx = {window: win, document: doc, String, Object, Array, Promise, JSON, Error};
  vm.createContext(ctx);
  vm.runInContext(MODULE, ctx);
  const api = win.lumaSwitchTopology;
  function cell(ip, mac) {
    const td = new Node('td'); td.ownerDocument = doc; td.className = 'mac-lldp'; td.dataset = {ip}; td.textContent = mac;
    cells.push(td); return td;
  }
  return {api, banner, title, detail, button, cell, requests, toasts, cells, timers, win};
}

const sw = (id, count, name) => ({chassis_id: id, count, chassis_name: name});
function topo(groups, extra = {}) {
  const sorted = groups.slice().sort((a, b) => b.count - a.count);
  const tied = sorted.length > 1 && sorted[0].count === sorted[1].count;
  const devices = {};
  (extra.devices || []).forEach(d => { devices[d.ip] = d; });
  return Object.assign({
    switch_count: groups.length, groups: sorted, tied, multi_switch: groups.length > 1,
    minority_ips: Object.values(devices).filter(d => d.state === 'different').map(d => d.ip),
    devices,
  }, extra, {devices});
}
const dev = (ip, state, name) => ({ip, state, switch_name: name || ''});

// ------------------------------------------------------------------ wording and counts
scenario('two switches are described as primary and another, with the live counts', () => {
  const w = world();
  const t = w.api.warningText(topo([sw('a', 10, 'sw-alpha'), sw('b', 5, 'sw-beta')]));
  assert.strictEqual(t.title, 'Multiple network switches detected.');
  assert.strictEqual(t.detail, '10 devices are connected through the primary switch and 5 devices are connected through '
    + 'another switch. If USB or AV-over-IP performance issues occur, verify uplink bandwidth between switches.');
});

scenario('singular wording for a single device on a switch', () => {
  const w = world();
  const t = w.api.warningText(topo([sw('a', 7, 'x'), sw('b', 1, 'y')]));
  assert.ok(t.detail.startsWith('7 devices are connected through the primary switch and 1 device is connected through another switch.'), t.detail);
});

scenario('counts are not hard-coded: other numbers give other sentences', () => {
  const w = world();
  const t = w.api.warningText(topo([sw('a', 23, 'x'), sw('b', 14, 'y')]));
  assert.ok(t.detail.startsWith('23 devices are connected through the primary switch and 14 devices are connected through another switch.'));
});

scenario('three switches are summarised without naming a culprit', () => {
  const w = world();
  const t = w.api.warningText(topo([sw('a', 6, 'x'), sw('b', 3, 'y'), sw('c', 2, 'z')]));
  assert.strictEqual(t.title, 'Devices were detected across 3 network switches.');
  assert.strictEqual(t.detail, 'If performance issues occur, verify inter-switch/uplink bandwidth and configuration.');
  assert.ok(!/primary/.test(t.detail + t.title));
});

scenario('a tie is summarised, never split into primary and other', () => {
  const w = world();
  const t = w.api.warningText(topo([sw('a', 3, 'x'), sw('b', 3, 'y')]));
  assert.strictEqual(t.title, 'Devices were detected across 2 network switches.');
  assert.ok(!/primary/.test(t.detail));
});

// ------------------------------------------------------------------ when it shows
scenario('one switch, no LLDP at all and an unacknowledged two-switch inventory', () => {
  const w = world();
  assert.strictEqual(w.api.shouldShow(topo([sw('a', 5, 'x')]), false), false);
  assert.strictEqual(w.api.shouldShow(topo([]), false), false);
  assert.strictEqual(w.api.shouldShow(null, false), false);
  assert.strictEqual(w.api.shouldShow(topo([sw('a', 5, 'x'), sw('b', 2, 'y')]), false), true);
  assert.strictEqual(w.api.shouldShow(topo([sw('a', 5, 'x'), sw('b', 2, 'y')]), true), false);
});

scenario('apply shows, fills in and hides the banner from the flag alone', () => {
  const w = world();
  w.api.apply(topo([sw('a', 4, 'x'), sw('b', 2, 'y')]), false);
  assert.strictEqual(w.banner.hidden, false);
  assert.strictEqual(w.title.textContent, 'Multiple network switches detected.');
  assert.ok(w.detail.textContent.startsWith('4 devices are connected through the primary switch'));
  w.api.apply(topo([sw('a', 4, 'x')]), false);
  assert.strictEqual(w.banner.hidden, true);
});

scenario('visibility is decided by the flag, never read back from the banner', () => {
  const w = world();
  const two = topo([sw('a', 4, 'x'), sw('b', 2, 'y')]);
  w.api.apply(two, true);
  w.banner.hidden = false;                 // something else un-hid it
  w.api.apply(two, true);                  // the acknowledgement still wins
  assert.strictEqual(w.banner.hidden, true);
  w.banner.hidden = true;                  // and a hidden banner does not mean "acknowledged"
  w.api.apply(two, false);
  assert.strictEqual(w.banner.hidden, false);
});

scenario('only a true acknowledgement counts', () => {
  const w = world();
  const two = topo([sw('a', 4, 'x'), sw('b', 2, 'y')]);
  for (const bad of [undefined, null, 'true', 1, {}]) {
    w.api.apply(two, bad);
    assert.strictEqual(w.banner.hidden, false, String(bad));
  }
});

// ------------------------------------------------------------------ dismissal
scenario('dismissing hides at once, records one acknowledgement, and the marks are untouched', async () => {
  const w = world();
  const two = topo([sw('a', 4, 'x'), sw('b', 2, 'sw-beta')], {devices: [dev('203.0.113.5', 'different', 'sw-beta')]});
  const td = w.cell('203.0.113.5', '02:00:00:00:00:05');
  w.api.apply(two, false);
  const pending = w.api.dismiss();
  assert.strictEqual(w.banner.hidden, true, 'hidden before the server has answered');
  assert.strictEqual(await pending, true);
  assert.strictEqual(w.requests.length, 1);
  assert.strictEqual(w.requests[0].url, '/api/lldp_topology/acknowledge');
  assert.strictEqual(w.requests[0].init.method, 'POST');
  assert.deepStrictEqual(JSON.parse(w.requests[0].init.body), {acknowledged: true});
  assert.ok(td.classList.contains('alternate-switch'), 'the unit is still marked');
});

scenario('a dismissal the server did not record puts the banner back', async () => {
  for (const options of [{failAck: true}, {ackBody: {ok: false, error: 'no'}}]) {
    const w = world(options);
    w.api.apply(topo([sw('a', 4, 'x'), sw('b', 2, 'y')]), false);
    assert.strictEqual(await w.api.dismiss(), false);
    assert.strictEqual(w.banner.hidden, false);
    assert.strictEqual(w.toasts.length, 1, 'and says so');
  }
});

scenario('refresh: one switch, a second appears, dismiss, it leaves, it returns, still dismissed', async () => {
  const w = world();
  const one = topo([sw('a', 4, 'x')]), two = topo([sw('a', 4, 'x'), sw('b', 2, 'y')]);
  w.api.apply(one, false);
  assert.strictEqual(w.banner.hidden, true);
  w.api.apply(two, false);
  assert.strictEqual(w.banner.hidden, false);
  await w.api.dismiss();
  w.api.apply(two, true);                   // the server now reports the acknowledgement with the inventory
  assert.strictEqual(w.banner.hidden, true);
  w.api.apply(one, true);
  assert.strictEqual(w.banner.hidden, true);
  w.api.apply(two, true);                   // the second switch returns
  assert.strictEqual(w.banner.hidden, true, 'not shown again for the same inventory');
  const three = topo([sw('a', 4, 'x'), sw('b', 2, 'y'), sw('c', 1, 'z')]);
  w.api.apply(three, true);
  assert.strictEqual(w.banner.hidden, true, 'a third switch is not a new acknowledgement');
});

scenario('clearing the inventory resets: the next two-switch inventory shows again', () => {
  const w = world();
  const two = topo([sw('a', 4, 'x'), sw('b', 2, 'y')]);
  w.api.apply(two, true);
  w.api.apply(null, false);                 // what the page does after Clear Units
  assert.strictEqual(w.banner.hidden, true);
  w.api.apply(two, false);
  assert.strictEqual(w.banner.hidden, false);
});

scenario('an absent topology (older server, failed read) shows nothing and invents nothing', () => {
  const w = world();
  const td = w.cell('198.51.100.9', 'm');
  w.api.apply(undefined, false);
  w.api.apply('garbage', false);
  assert.strictEqual(w.banner.hidden, true);
  assert.ok(!td.classList.contains('alternate-switch'));
});

// ------------------------------------------------------------------ the unit
scenario('a unit on a different switch is marked with its switch, and nobody else is', () => {
  const w = world();
  const cells = {
    p: w.cell('198.51.100.1', 'AA'), d: w.cell('203.0.113.1', 'BB'), u: w.cell('192.0.2.1', 'CC'),
  };
  w.api.apply(topo([sw('a', 5, 'sw-alpha'), sw('b', 1, 'sw-beta')], {devices: [
    dev('198.51.100.1', 'primary', 'sw-alpha'), dev('203.0.113.1', 'different', 'sw-beta'), dev('192.0.2.1', 'unknown')]}), false);
  assert.ok(cells.d.classList.contains('alternate-switch'));
  assert.strictEqual(cells.d.querySelector('.switch-note').textContent, 'Different switch: sw-beta');
  assert.ok(!cells.p.classList.contains('alternate-switch') && !cells.p.querySelector('.switch-note'));
  assert.ok(!cells.u.classList.contains('alternate-switch') && !cells.u.querySelector('.switch-note'),
    'unknown is never different');
  assert.strictEqual(cells.d._text, 'BB', 'the MAC itself is untouched');
});

scenario('a different switch without a name says so without an identifier or an address', () => {
  const w = world();
  const td = w.cell('203.0.113.1', 'BB');
  w.api.apply(topo([sw('a', 5, 'x'), sw('b', 1, '')], {devices: [dev('203.0.113.1', 'different', '')]}), false);
  assert.strictEqual(td.querySelector('.switch-note').textContent, 'Different switch');
});

scenario('a tie marks nobody', () => {
  const w = world();
  const td = w.cell('203.0.113.1', 'BB');
  w.api.apply(topo([sw('a', 3, 'x'), sw('b', 3, 'y')], {devices: [dev('203.0.113.1', 'tied', 'y')]}), false);
  assert.ok(!td.classList.contains('alternate-switch'));
});

scenario('marks follow the current topology in place and never accumulate', () => {
  const w = world();
  const td = w.cell('203.0.113.1', 'BB');
  const two = topo([sw('a', 5, 'x'), sw('b', 1, 'sw-beta')], {devices: [dev('203.0.113.1', 'different', 'sw-beta')]});
  w.api.apply(two, false); w.api.apply(two, false); w.api.apply(two, true);
  assert.strictEqual(td.querySelectorAll('.switch-note').length, 1);
  w.api.apply(topo([sw('a', 6, 'x')], {devices: [dev('203.0.113.1', 'primary', 'x')]}), true);
  assert.ok(!td.classList.contains('alternate-switch'));
  assert.strictEqual(td.querySelectorAll('.switch-note').length, 0);
  w.api.apply(topo([sw('b', 6, 'sw-beta')], {devices: [dev('203.0.113.1', 'primary', 'sw-beta')]}), true);
  assert.ok(!td.classList.contains('alternate-switch'), 'the other switch becoming primary moves the mark with it');
});

scenario('a freshly built row is decorated the same way', () => {
  const w = world();
  w.api.apply(topo([sw('a', 5, 'x'), sw('b', 1, 'sw-beta')], {devices: [dev('203.0.113.1', 'different', 'sw-beta')]}), false);
  const td = new Node('td'); td.ownerDocument = {createElement: t => new Node(t)}; td.dataset = {ip: '203.0.113.1'};
  w.api.decorateMacCell(td, {ip: '203.0.113.1'});
  assert.strictEqual(td.querySelector('.switch-note').textContent, 'Different switch: sw-beta');
});

scenario('a hostile switch name is text and never markup', () => {
  const w = world();
  const evil = '<img src=x onerror=alert(1)>"&';
  const td = w.cell('203.0.113.1', 'BB');
  w.api.apply(topo([sw('a', 5, 'x'), sw('b', 1, evil)], {devices: [dev('203.0.113.1', 'different', evil)]}), false);
  assert.strictEqual(td.querySelector('.switch-note').textContent, 'Different switch: ' + evil);
  // The stub throws on any innerHTML assignment, so reaching here also proves none was made.
  assert.ok(!/innerHTML|insertAdjacentHTML|document\.write/.test(stripComments(MODULE)));
  w.api.apply(topo([sw('a', 5, evil), sw('b', 1, 'y')]), false);
  assert.strictEqual(w.title.textContent.indexOf('<'), -1);
});

// ------------------------------------------------------------------ the module itself
scenario('the module requests nothing on its own and starts no timer', () => {
  const w = world();
  w.api.apply(topo([sw('a', 4, 'x'), sw('b', 2, 'y')]), false);
  w.api.apply(null, false);
  assert.strictEqual(w.requests.length, 0);
  const code = stripComments(MODULE);
  assert.ok(!/setInterval|XMLHttpRequest|EventSource|WebSocket/.test(code), 'no repeating timer, no other transport');
  assert.strictEqual((code.match(/fetch\(/g) || []).length, 2, 'two requests: the acknowledgement and the bounded re-read');
  assert.ok(/\/api\/lldp_topology\/acknowledge/.test(code));
  assert.strictEqual(w.timers.length, 0, 'nothing is scheduled when every unit has been read');
});

scenario('units never read are re-asked a bounded number of times, then left alone', async () => {
  const w = world();
  const unread = Object.assign(topo([sw('a', 4, 'x')]), {units_not_read: 2});
  w.win.fetch = (url) => { w.requests.push({url}); return Promise.resolve({json: () => Promise.resolve({ok: true, topology: unread, acknowledged: false})}); };
  w.api.apply(unread, false);
  assert.strictEqual(w.timers.length, 1);
  for (let i = 0; i < 6; i++) {
    const next = w.timers.filter(x => !x.fn.done && !x.cancelled).pop();
    if (!next || next.ran) break;
    next.ran = true; await next.fn();
  }
  assert.strictEqual(w.requests.filter(r => r.url === '/api/lldp_topology').length, 3, 'three reads and no more');
  assert.deepStrictEqual(w.timers.map(x => x.ms), [6000, 15000, 30000]);
});

scenario('a read that finds every unit stops asking at once', async () => {
  const w = world();
  const unread = Object.assign(topo([sw('a', 4, 'x')]), {units_not_read: 1});
  const done = Object.assign(topo([sw('a', 4, 'x'), sw('b', 2, 'y')]), {units_not_read: 0});
  w.win.fetch = (url) => { w.requests.push({url}); return Promise.resolve({json: () => Promise.resolve({ok: true, topology: done, acknowledged: false})}); };
  w.api.apply(unread, false);
  await w.timers[0].fn();
  assert.strictEqual(w.timers.length, 1, 'no further read is scheduled');
  assert.strictEqual(w.banner.hidden, false, 'what it learned reaches the banner');
});

scenario('rows are reloaded once when a re-read has learned units, and not otherwise', async () => {
  const w = world();
  let reloads = 0;
  w.api.learn.onLearned = () => { reloads++; };
  const two = Object.assign(topo([sw('a', 4, 'x')]), {units_not_read: 2});
  const one = Object.assign(topo([sw('a', 4, 'x')]), {units_not_read: 1});
  const answer = [one, one];
  w.win.fetch = (url) => { w.requests.push({url}); return Promise.resolve({json: () => Promise.resolve({ok: true, topology: answer.shift(), acknowledged: false})}); };
  w.api.apply(two, false);
  await w.timers[w.timers.length - 1].fn();
  assert.strictEqual(reloads, 1, 'two unread became one: something was learned');
  await w.timers[w.timers.length - 1].fn();
  assert.strictEqual(reloads, 1, 'still one unread: nothing new, no reload');
});

scenario('every path that re-reads units tells the page the topology, not only the full scan', () => {
  const page = stripComments(PAGE);
  const rescan = page.slice(page.indexOf('async function rescanIPs'), page.indexOf('let detailPollInFlight'));
  assert.ok(rescan.length > 100 && rescan.indexOf('detailPollInFlight') === -1);
  assert.ok(/ingestTopology\(last\);\s*units = merged;/.test(rescan), 'rescanIPs ingests the topology it was sent (the last batch carries the inventory as it is now)');
});

scenario('the dismiss control is bound once the module is loaded', () => {
  const w = world();
  assert.strictEqual(w.button.dataset.bound, '1');
  assert.strictEqual((w.button.handlers.click || []).length, 1);
  w.api.bind();
  assert.strictEqual((w.button.handlers.click || []).length, 1, 'binding twice does not double the handler');
});

// ------------------------------------------------------------------ the page
scenario('the page carries the banner, the dismiss control and the module', () => {
  assert.ok(/<script src="\/ui\/switch-topology\.js[^"]*"><\/script>/.test(PAGE));
  const banner = PAGE.match(/<div id="switch_topology_warning"[^>]*>/)[0];
  assert.ok(/role="status"/.test(banner) && /aria-live="polite"/.test(banner) && /\bhidden\b/.test(banner));
  assert.ok(/class="notice notice-warning"/.test(banner), 'the shared advisory notice, not a new look');
  const button = PAGE.match(/<button[^>]*id="switch_topology_dismiss"[^>]*>/)[0];
  assert.ok(/aria-label="[^"]+"/.test(button), 'an icon-only control carries an accessible name');
  assert.ok(!/ title="/.test(banner + button), 'no native title tooltip');
  // Inside the table's own scroll container, above the table, so it scrolls away and never sits over a header.
  const at = {w: PAGE.indexOf('id="units_table_wrapper"'), b: PAGE.indexOf('id="switch_topology_warning"'), t: PAGE.indexOf('<table id="tbl"')};
  assert.ok(at.w > 0 && at.w < at.b && at.b < at.t);
});

scenario('every place the page loads the inventory hands its topology to the module, before rendering', () => {
  const code = stripComments(PAGE);
  const sites = [...code.matchAll(/await api\("\/api\/cache"\);\s*([\s\S]{0,80})/g)];
  assert.ok(sites.length >= 3);
  sites.forEach(m => assert.ok(/^ingestTopology\(payload\);/.test(m[1]), 'cache loaded without ingesting: ' + m[1].slice(0, 40)));
  // Every request that sends back the inventory (the full scan and the per-unit re-read) is followed by it.
  const scans = (code.match(/(?:fetch|send)\('\/api\/scan'/g) || []).length;
  const ingests = (code.match(/ingestTopology\(data\);\s*units = mergeByIP\(units, rows\);/g) || []).length
    + (code.match(/ingestTopology\(last\);\s*units = merged;/g) || []).length;
  assert.ok(scans >= 2 && ingests === scans, 'each of the ' + scans + ' scan requests ingests its topology (' + ingests + ' do)');
  assert.ok(/lumaSwitchTopology\.decorateMacCell\(tdMAC,u\)/.test(code), 'rows decorate their own MAC cell');
  assert.ok(/lumaSwitchTopology\.apply\(null, ?false\)/.test(code), 'clearing the inventory resets the page');
});

scenario('ingestTopology feeds the module only from an inventory payload', () => {
  const calls = [];
  const ctx = {window: {lumaSwitchTopology: {apply: (...a) => calls.push(a)}}, Array};
  vm.createContext(ctx);
  const at = PAGE.indexOf('function ingestTopology');
  let depth = 0, end = at;
  for (let i = PAGE.indexOf('{', at); i < PAGE.length; i++) {
    if (PAGE[i] === '{') depth++; else if (PAGE[i] === '}' && --depth === 0) { end = i + 1; break; }
  }
  vm.runInContext(PAGE.slice(at, end), ctx);
  ctx.ingestTopology(null); ctx.ingestTopology([]); ctx.ingestTopology({units: []});
  assert.strictEqual(calls.length, 0, 'an older payload leaves the page as it was');
  const t = {multi_switch: true};
  ctx.ingestTopology({units: [], lldp_topology: t, topology_warning_acknowledged: true});
  assert.deepStrictEqual(calls, [[t, true]]);
});

scenario('the colours are semantic tokens that exist, in both appearances, and mixes sum to 100', () => {
  const css = read('ui/family.css'), page = stripComments(PAGE);
  const rules = page.match(/#switch_topology_warning[\s\S]*?td\.mac-lldp \.switch-note[^}]*}/)[0];
  const used = [...rules.matchAll(/var\((--[\w-]+)/g)].map(m => m[1]);
  assert.ok(used.length > 0);
  used.forEach(token => assert.ok(new RegExp(token + '\\s*:').test(css), token + ' is used but never defined'));
  let mixes = 0;
  for (let at = rules.indexOf('color-mix('); at >= 0; at = rules.indexOf('color-mix(', at + 1)) {
    let depth = 0, end = at;
    for (let i = rules.indexOf('(', at); i < rules.length; i++) {
      if (rules[i] === '(') depth++; else if (rules[i] === ')' && --depth === 0) { end = i; break; }
    }
    const body = rules.slice(at, end + 1).replace(/var\([^)]*\)/g, 'V');
    const pcts = [...body.matchAll(/(\d+(?:\.\d+)?)%/g)].map(x => parseFloat(x[1]));
    assert.strictEqual(pcts.reduce((a, b) => a + b, 0), 100, body);
    mixes++;
  }
  assert.ok(mixes >= 2, 'the mixes were actually found');
  assert.ok(!/#[0-9a-f]{3,8}\b/i.test(rules), 'no hard-coded colour');
  assert.ok(/--state-info-text:/.test(css));
  // The advisory is not a danger state.
  assert.ok(!/--danger|--state-error|--state-danger/.test(rules));
});

scenario('the marked cell stays opaque so a sticky column does not show rows through it', () => {
  const rules = stripComments(PAGE).match(/table\.device-table td\.mac-lldp\.alternate-switch\s*{[^}]*}/)[0];
  assert.ok(/linear-gradient\(var\(--surface-raised\), ?var\(--surface-raised\)\)/.test(rules), 'the opaque layer stays underneath');
});

(async () => {
  for (const [name, fn] of queue) {
    try { await Promise.race([Promise.resolve().then(fn), new Promise((_, rej) => setTimeout(() => rej(new Error('timed out')), 5000))]); passed++; }
    catch (err) { console.error('FAILED: ' + name + '\n  ' + (err && err.stack || err)); process.exitCode = 1; }
  }
  console.log(passed + ' scenarios passed' + (process.exitCode ? ' (WITH FAILURES)' : ''));
  if (passed !== queue.length) process.exitCode = 1;
})();
