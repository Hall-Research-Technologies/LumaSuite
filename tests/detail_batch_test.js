// Device Manager detail refresh beyond 32 units, driven as code.
//
// Defect (present in v1.1.0): with more than 32 visible units the page posted every address to /api/scan in one
// request. The server refuses a scan that names more than luma_scanplan.MAX_RANGES (32) expressions, with HTTP 400
// (too_many_ranges), so the refresh never worked and the page retried it every round. The server limit is right
// (it guards operator-typed range text) and is unchanged; the page now sends batches the server accepts, one at a
// time, in the cancellable background lane, and backs off when a batch is refused.
//
// Functions are lifted from ui/index.html and run, so this measures behaviour rather than text.
'use strict';
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const ROOT = path.join(__dirname, '..');
const page = fs.readFileSync(path.join(ROOT, 'ui', 'index.html'), 'utf8').replace(/\r\n/g, '\n');

function lift(source, name) {
  const at = source.search(new RegExp('(?:async\\s+)?function\\s+' + name + '\\s*\\('));
  assert.ok(at >= 0, name + ' not found');
  let depth = 0;
  const open = source.slice(at).search(/\)\s*\{/) + at;
  for (let i = source.indexOf('{', open); i < source.length; i++) {
    if (source[i] === '{') depth++;
    else if (source[i] === '}' && --depth === 0) return source.slice(at, i + 1);
  }
  throw new Error('unbalanced ' + name);
}
function liftConst(source, name) {
  const at = source.indexOf('const ' + name + ' =');
  assert.ok(at >= 0, name + ' not found');
  return source.slice(at, source.indexOf('\n', at));
}

const scenarios = [];
const scenario = (name, fn) => scenarios.push([name, fn]);
const tick = () => new Promise(r => setImmediate(r));
const ips = n => Array.from({length: n}, (_, i) => '198.51.100.' + (i + 1));

// A server that enforces the real limit: more than 32 comma-separated expressions is a 400.
function fakeServer(options) {
  const o = Object.assign({silent: new Set(), manual: false, limit: 32}, options || {});
  const net = {calls: [], live: 0, maxLive: 0, o};
  net.fetch = (url, init) => new Promise((resolve, reject) => {
    const targets = init && init.body ? JSON.parse(init.body).targets.split(',') : [];
    const call = {url, targets, signal: init && init.signal, settled: false};
    net.calls.push(call);
    net.live++; net.maxLive = Math.max(net.maxLive, net.live);
    const finish = fn => { if (call.settled) return; call.settled = true; net.live--; fn(); };
    call.reply = () => finish(() => {
      if (targets.length > o.limit) return resolve({ok: false, status: 400, json: async () => ({ok: false, reason: 'too_many_ranges'})});
      const answered = targets.filter(t => !o.silent.has(t));
      resolve({ok: true, status: 200, json: async () => { if (o.onJson) o.onJson(); return {ok: true, units: answered.map(ip => ({ip, mac: 'aa:' + ip})), answered, stale: false}; }});
    });
    const abort = () => finish(() => reject(Object.assign(new Error('aborted'), {name: 'AbortError'})));
    if (call.signal && call.signal.aborted) return abort();     // a real fetch refuses at once with a spent signal
    if (call.signal) call.signal.addEventListener('abort', abort);
    if (!o.manual) setImmediate(call.reply);
  });
  return net;
}

function world(net, extra) {
  const ctx = Object.assign({
    fetch: net.fetch, AbortController, JSON, Promise, Date, Math, Array, Object, Map, Set, setTimeout, console: {warn() {}, log() {}},
    units: [], unitDataEpoch: 0, scanAbortController: null, Upgrade: {isActive: () => false},
    applied: [], ntp: 0, saved: 0, rendered: 0,
    mergeByIP(a, b) { const m = new Map(a.map(r => [r.ip, r])); (b || []).forEach(r => m.set(r.ip, Object.assign({}, m.get(r.ip), r))); return Array.from(m.values()); },
    ingestTopology(d) { ctx.applied.push(d); }, saveLocal() { ctx.saved++; }, rerenderFiltered() { ctx.rendered++; }, updateTotals() {},
    updateDashboardNtpFromUnits() { ctx.ntp++; }, selectedNtpIPs: () => [],
  }, extra || {});
  vm.createContext(ctx);
  vm.runInContext([
    'const bgLane = { ctl: new AbortController() };',
    lift(page, 'bgFetch'), lift(page, 'cancelBackgroundRequests'),
    liftConst(page, 'DETAIL_BATCH_SIZE'), liftConst(page, 'DETAIL_BACKOFF_FIRST_MS'), liftConst(page, 'DETAIL_BACKOFF_CAP_MS'),
    liftConst(page, 'DETAIL_UNIT_BACKOFF_FIRST_MS'), liftConst(page, 'DETAIL_UNIT_BACKOFF_CAP_MS'),
    'const detailBackoff = { step: 0, until: 0 }; const detailUnitBackoff = new Map(); let detailPollInFlight = false;',
    lift(page, 'chunkList'), lift(page, 'detailNoteReplies'), lift(page, 'rescanIPs'), lift(page, 'pollVisibleUnitDetails'),
  ].join('\n'), ctx);
  return ctx;
}
const run = (w, code) => vm.runInContext(code, w);

scenario('the batch size is within the server limit it is there to respect', () => {
  const n = Number(/const DETAIL_BATCH_SIZE = (\d+);/.exec(page)[1]);
  assert.ok(n >= 1 && n <= 32, 'batch size ' + n);
});

for (const count of [25, 32, 33, 50, 100, 250, 500, 1000]) {
  scenario('a refresh of ' + count + ' units is accepted: every batch is legal, every unit is read once, one request at a time', async () => {
    const net = fakeServer();
    const w = world(net);
    await w.pollVisibleUnitDetails(ips(count));
    const scans = net.calls;
    assert.ok(scans.length >= 1);
    for (const c of scans) assert.ok(c.targets.length <= 32, 'a batch of ' + c.targets.length);
    assert.deepStrictEqual(scans.flatMap(c => c.targets).sort(), ips(count).sort(), 'every unit exactly once');
    assert.strictEqual(net.maxLive, 1, 'requests in flight at once: ' + net.maxLive);
    assert.ok(scans.every(c => c.signal), 'every batch is in the cancellable lane');
    assert.strictEqual(run(w, 'units.length'), count, 'the rows were merged');
    assert.strictEqual(w.rendered, 1, 'one redraw per round, not one per batch');
    assert.strictEqual(w.ntp, 1);
    assert.strictEqual(run(w, 'detailBackoff.until'), 0, 'a good round holds nothing back');
  });
}

scenario('cancelling the lane ends the round at once: no further batch, nothing applied, nothing held against the server', async () => {
  const net = fakeServer({manual: true});
  const w = world(net);
  const round = w.pollVisibleUnitDetails(ips(200));
  await tick();
  assert.strictEqual(net.live, 1);
  w.cancelBackgroundRequests();
  await round;
  assert.strictEqual(net.calls.length, 1, 'the remaining batches were never sent');
  assert.strictEqual(net.live, 0);
  assert.strictEqual(run(w, 'units.length'), 0);
  assert.strictEqual(w.rendered, 0);
  assert.strictEqual(run(w, 'detailBackoff.until'), 0, 'a cancelled round is not a refusal');
  assert.strictEqual(run(w, 'detailUnitBackoff.size'), 0, 'and says nothing about any unit');
  assert.strictEqual(run(w, 'detailPollInFlight'), false, 'the round is released');
});

scenario('a batch answered after the inventory changed (Clear Units) is not applied and ends the round', async () => {
  const net = fakeServer({manual: true});
  const w = world(net);
  const round = w.pollVisibleUnitDetails(ips(100));
  await tick();
  run(w, 'unitDataEpoch += 1');
  net.calls[0].reply();
  await round;
  assert.strictEqual(net.calls.length, 1, 'no batch for a cleared inventory');
  assert.strictEqual(run(w, 'units.length'), 0, 'a late answer does not bring a cleared unit back');
});

scenario('a stale answer is not merged even into the round copy, and no further batch is sent', async () => {
  const net = fakeServer();
  const w = world(net);
  net.o.onJson = () => run(w, 'unitDataEpoch += 1');
  let merges = 0;
  const inner = w.mergeByIP;
  w.mergeByIP = (a, b) => { merges++; return inner(a, b); };
  await w.pollVisibleUnitDetails(ips(100));
  assert.strictEqual(merges, 0, 'a reply that describes a cleared inventory was merged');
  assert.strictEqual(net.calls.length, 1);
  assert.strictEqual(run(w, 'units.length'), 0);
});

scenario('the lane or the inventory changing between two batches stops the round before the next one is sent', async () => {
  for (const how of ['lane', 'inventory']) {
    const net = fakeServer();
    const w = world(net);
    const inner = w.mergeByIP;
    w.mergeByIP = (a, b) => { if (how === 'lane') w.cancelBackgroundRequests(); else run(w, 'unitDataEpoch += 1'); return inner(a, b); };
    await w.pollVisibleUnitDetails(ips(100));
    assert.strictEqual(net.calls.length, 1, how + ': the next batch was sent after the change');
    assert.strictEqual(run(w, 'units.length'), 0, how + ': nothing is applied');
  }
});

scenario('a refused batch fails the round, applies nothing, and the next rounds stand off, doubling and capped', async () => {
  const net = fakeServer({limit: 5});
  const w = world(net);
  await w.pollVisibleUnitDetails(ips(60));
  assert.strictEqual(net.calls.length, 1, 'the round stops at the first refusal');
  assert.strictEqual(run(w, 'units.length'), 0);
  assert.strictEqual(w.rendered, 0);
  assert.strictEqual(run(w, 'detailBackoff.step'), 10000);
  await w.pollVisibleUnitDetails(ips(60));
  await w.pollVisibleUnitDetails(ips(60));
  assert.strictEqual(net.calls.length, 1, 'no request while standing off');
  run(w, 'detailBackoff.until = 0');
  await w.pollVisibleUnitDetails(ips(60));
  assert.strictEqual(run(w, 'detailBackoff.step'), 20000, 'the stand-off doubles');
  for (let i = 0; i < 10; i++) { run(w, 'detailBackoff.until = 0'); await w.pollVisibleUnitDetails(ips(60)); }
  assert.strictEqual(run(w, 'detailBackoff.step'), 120000, 'capped');
  net.o.limit = 32; run(w, 'detailBackoff.until = 0');
  await w.pollVisibleUnitDetails(ips(60));
  assert.strictEqual(run(w, 'detailBackoff.step'), 0, 'a good round clears it');
  assert.strictEqual(run(w, 'units.length'), 60);
});

scenario('a unit that gives no reply is left out of later rounds for a while, and read again when it answers', async () => {
  const silent = new Set(['198.51.100.2']);
  const net = fakeServer({silent});
  const w = world(net);
  await w.pollVisibleUnitDetails(ips(5));
  const reads = ip => net.calls.filter(c => c.targets.includes(ip)).length;
  assert.strictEqual(reads('198.51.100.2'), 1);
  await w.pollVisibleUnitDetails(ips(5));
  await w.pollVisibleUnitDetails(ips(5));
  assert.strictEqual(reads('198.51.100.2'), 1, 'skipped while backed off');
  assert.strictEqual(reads('198.51.100.3'), 3, 'an answering unit is read every round');
  assert.strictEqual(run(w, 'detailUnitBackoff.get("198.51.100.2").step'), 10000);
  run(w, 'detailUnitBackoff.get("198.51.100.2").until = 0');
  await w.pollVisibleUnitDetails(ips(5));
  assert.strictEqual(run(w, 'detailUnitBackoff.get("198.51.100.2").step'), 20000, 'doubles');
  silent.clear(); run(w, 'detailUnitBackoff.get("198.51.100.2").until = 0');
  await w.pollVisibleUnitDetails(ips(5));
  assert.strictEqual(run(w, 'detailUnitBackoff.has("198.51.100.2")'), false, 'an answer clears it');
  for (let i = 0; i < 12; i++) run(w, 'detailNoteReplies(["198.51.100.9"], [])');
  assert.strictEqual(run(w, 'detailUnitBackoff.get("198.51.100.9").step'), 120000, 'capped');
});

scenario('a server that sends no list of replies leaves every unit in every round', () => {
  const w = world(fakeServer());
  run(w, 'detailNoteReplies(["198.51.100.1"], undefined)');
  assert.strictEqual(run(w, 'detailUnitBackoff.size'), 0);
});

scenario('when every unit is backed off no request is made at all', async () => {
  const net = fakeServer({silent: new Set(ips(3))});
  const w = world(net);
  await w.pollVisibleUnitDetails(ips(3));
  const n = net.calls.length;
  await w.pollVisibleUnitDetails(ips(3));
  assert.strictEqual(net.calls.length, n);
});

scenario('a scan in progress keeps the refresh away, and a unit being upgraded is not read', async () => {
  const net = fakeServer();
  const w = world(net, {scanAbortController: {}});
  await w.pollVisibleUnitDetails(ips(5));
  assert.strictEqual(net.calls.length, 0);
  const net2 = fakeServer();
  const w2 = world(net2, {Upgrade: {isActive: ip => ip === '198.51.100.1'}});
  await w2.pollVisibleUnitDetails(ips(3));
  assert.deepStrictEqual(net2.calls.flatMap(c => c.targets), ['198.51.100.2', '198.51.100.3']);
});

scenario('a foreground refresh of one unit is not in the background lane, so cancelling the lane never drops it', async () => {
  const net = fakeServer({manual: true});
  const w = world(net);
  const p = w.rescanIPs(['198.51.100.7']);
  await tick();
  assert.strictEqual(net.calls[0].signal, undefined);
  w.cancelBackgroundRequests();
  net.calls[0].reply();
  assert.strictEqual(await p, 'ok');
});

scenario('the reachability round reads in detail only the units that answered its probe', async () => {
  const reachable = new Set(['198.51.100.1', '198.51.100.3']);
  const asked = [];
  const w = world(fakeServer(), {
    rebootTracker: {}, isSliderOn: () => true, getVisibleIPs: () => ips(4), failureMap: new Map(), setStatus() {},
    enableHeartbeat() {}, rebootHeartbeatOwned: false, appConfig: {},
    asked,
    fetch: (url, init) => {
      const ip = JSON.parse(init.body).ip;
      return Promise.resolve({json: async () => ({ok: true, reachable: reachable.has(ip)})});
    },
  });
  vm.runInContext([lift(page, 'tcpProbe'), 'let heartbeatRound = null;', lift(page, 'runLimited'), lift(page, 'heartbeatOnce'), lift(page, 'heartbeatRoundImpl')].join('\n'), w);
  run(w, 'pollVisibleUnitDetails = async list => { asked.push(list); }');
  await w.heartbeatOnce();
  assert.deepStrictEqual(asked, [['198.51.100.1', '198.51.100.3']]);
});

scenario('Clear Units forgets what the refresh was holding back', () => {
  const code = page.replace(/\/\/.*$/gm, '');
  const at = code.indexOf('$("#clear_units").addEventListener');
  const handler = code.slice(at, at + 6000);
  assert.ok(/detailUnitBackoff\.clear\(\)/.test(handler));
  assert.ok(/detailBackoff\.step = 0/.test(handler));
});

(async () => {
  let passed = 0;
  for (const [name, fn] of scenarios) {
    try { await Promise.race([fn(), new Promise((_, rej) => setTimeout(() => rej(new Error('timed out')), 5000))]); passed++; }
    catch (err) { console.error('FAILED: ' + name + '\n  ' + (err && err.stack || err)); process.exitCode = 1; }
  }
  console.log(passed + ' scenarios passed');
  if (process.exitCode) console.log('FAILURES');
})();
