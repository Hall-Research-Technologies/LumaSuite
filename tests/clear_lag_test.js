// The Device Manager's background reads, driven as code: a recording fetch stands in for the browser's
// six-connection queue and the server.
//
// Measured with a real browser against 60 units (12 encoders that never answer): the RTMP status read ran
// every three seconds for every encoder with no limit and no "one round at a time", so 1,092 requests were
// issued in two minutes, 159 were waiting at the 60-second mark, and a Clear Units click waited in the same
// queue -- 61 s for the question to appear and 110 s more for the request to be sent. The server answered
// Clear Units itself in milliseconds throughout.
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
  return source.slice(at, source.indexOf(';\n', at) + 1);
}

let passed = 0;
const scenarios = [];
const scenario = (name, fn) => scenarios.push([name, fn]);
const tick = () => new Promise(r => setImmediate(r));

// A fetch whose replies are released by the test, so "how many are in flight" is a measurement.
function fakeNetwork() {
  const net = {calls: [], live: 0, maxLive: 0, hold: new Map(), answers: new Map()};
  net.fetch = (url, init) => new Promise((resolve, reject) => {
    const body = init && init.body ? JSON.parse(init.body) : {};
    const call = {url, body, signal: init && init.signal, settled: false};
    net.calls.push(call);
    net.live++; net.maxLive = Math.max(net.maxLive, net.live);
    const finish = (fn) => { if (call.settled) return; call.settled = true; net.live--; fn(); };
    call.resolve = (json) => finish(() => resolve({json: async () => json}));
    if (call.signal) call.signal.addEventListener('abort', () => finish(() => reject(Object.assign(new Error('aborted'), {name: 'AbortError'}))));
    if (!net.manual) {
      const answer = net.answers.get(body.ip);
      setImmediate(() => call.resolve(answer || {result: {enable: false}}));
    }
  });
  return net;
}

function world(net, units, extra) {
  const leds = new Map();
  const unitMap = new Map();
  for (const ip of units) { const led = {style: {}, ip}; leds.set(ip, led); unitMap.set(ip, {streamLed: led}); }
  const ctx = Object.assign({
    fetch: net.fetch, AbortController, JSON, Promise, Date, Math, Array, Object, Map, Set, setTimeout,
    appConfig: {is_default_password: false}, unitMap, unitDataEpoch: 0,
    stopRtmpPolling() {}, updateStreamLed(led, live) { led.live = live; },
  }, extra || {});
  vm.createContext(ctx);
  vm.runInContext([
    'const bgLane = { ctl: new AbortController() };',
    lift(page, 'bgFetch'), lift(page, 'cancelBackgroundRequests'), lift(page, 'runLimited'),
    liftConst(page, 'RTMP_POLL_CONCURRENCY'), liftConst(page, 'RTMP_READ_TIMEOUT_SECONDS'),
    liftConst(page, 'RTMP_BACKOFF_FIRST_MS'), liftConst(page, 'RTMP_BACKOFF_CAP_MS'),
    'const rtmpBackoff = new Map(); let rtmpPollInFlight = false;',
    lift(page, 'rtmpNoteAnswer'), lift(page, 'pollEncoderRtmpStatus'),
  ].join('\n'), ctx);
  ctx.leds = leds;
  return ctx;
}
const ips = n => Array.from({length: n}, (_, i) => '198.51.100.' + (i + 1));

scenario('a round never has more than the limit in flight, however many encoders there are', async () => {
  const net = fakeNetwork(); net.manual = true;
  const w = world(net, ips(40));
  const round = w.pollEncoderRtmpStatus();
  for (let i = 0; i < 20; i++) { await tick(); net.calls.filter(c => !c.settled).forEach(c => c.resolve({result: {enable: false}})); }
  await round;
  assert.ok(net.maxLive <= 4, 'in flight at once: ' + net.maxLive);
  assert.strictEqual(net.calls.length, 40, 'every encoder is still read once per round');
});

scenario('a tick while a round is still running starts nothing', async () => {
  const net = fakeNetwork(); net.manual = true;
  const w = world(net, ips(10));
  const first = w.pollEncoderRtmpStatus();
  await tick();
  const before = net.calls.length;
  for (let i = 0; i < 5; i++) await w.pollEncoderRtmpStatus();           // five more timer ticks
  assert.strictEqual(net.calls.length, before, 'a running round is the answer to the tick');
  for (let i = 0; i < 20; i++) { await tick(); net.calls.filter(c => !c.settled).forEach(c => c.resolve({result: {enable: false}})); }
  await first;
  const again = w.pollEncoderRtmpStatus();                                // and the next one runs normally
  await tick();
  assert.ok(net.calls.length > before);
  net.manual = false; net.calls.filter(c => !c.settled).forEach(c => c.resolve({result: {enable: false}}));
  for (let i = 0; i < 20; i++) { await tick(); net.calls.filter(c => !c.settled).forEach(c => c.resolve({result: {enable: false}})); }
  await again;
});

scenario('the read asks the server for a short timeout, not the three-second default', async () => {
  const net = fakeNetwork();
  const w = world(net, ips(3));
  await w.pollEncoderRtmpStatus();
  assert.ok(net.calls.length >= 3);
  for (const c of net.calls) assert.ok(c.body.timeout > 0 && c.body.timeout <= 2, JSON.stringify(c.body));
});

scenario('an encoder that does not answer is left alone for a while, growing, and is read again when it answers', async () => {
  const net = fakeNetwork();
  net.answers.set('198.51.100.1', {error: 'device_no_response', method: 'Rtmp.Get'});
  const w = world(net, ips(3));
  await w.pollEncoderRtmpStatus();
  const first = net.calls.filter(c => c.body.ip === '198.51.100.1').length;
  assert.strictEqual(first, 1);
  await w.pollEncoderRtmpStatus();
  await w.pollEncoderRtmpStatus();
  assert.strictEqual(net.calls.filter(c => c.body.ip === '198.51.100.1').length, 1, 'skipped while backed off');
  assert.strictEqual(net.calls.filter(c => c.body.ip === '198.51.100.2').length, 3, 'a working encoder is read every round');
  const held = vm.runInContext('rtmpBackoff.get("198.51.100.1")', w);
  assert.ok(held && held.step === 10000, JSON.stringify(held));
  vm.runInContext('rtmpBackoff.get("198.51.100.1").until = 0', w);        // the wait is over
  await w.pollEncoderRtmpStatus();
  assert.strictEqual(net.calls.filter(c => c.body.ip === '198.51.100.1').length, 2);
  const second = vm.runInContext('rtmpBackoff.get("198.51.100.1").step', w);
  assert.strictEqual(second, 20000, 'the wait doubles');
  net.answers.set('198.51.100.1', {result: {enable: false}});
  vm.runInContext('rtmpBackoff.get("198.51.100.1").until = 0', w);
  await w.pollEncoderRtmpStatus();
  assert.strictEqual(vm.runInContext('rtmpBackoff.has("198.51.100.1")', w), false, 'an answer clears it');
  for (let i = 0; i < 12; i++) vm.runInContext('rtmpNoteAnswer("198.51.100.9", false)', w);
  assert.strictEqual(vm.runInContext('rtmpBackoff.get("198.51.100.9").step', w), 120000, 'capped');
});

scenario('cancelling the lane ends every request at once and is not held against the unit', async () => {
  const net = fakeNetwork(); net.manual = true;
  const w = world(net, ips(12));
  const round = w.pollEncoderRtmpStatus();
  await tick();
  assert.ok(net.live > 0);
  w.cancelBackgroundRequests();
  await round;
  assert.strictEqual(net.live, 0, 'nothing is left in flight to queue ahead of the operator');
  assert.strictEqual(vm.runInContext('rtmpBackoff.size', w), 0, 'a cancelled read says nothing about the unit');
  const next = w.pollEncoderRtmpStatus();                                 // the next lane works
  await tick();
  assert.ok(net.calls[net.calls.length - 1].signal.aborted === false);
  net.calls.filter(c => !c.settled).forEach(c => c.resolve({result: {enable: false}}));
  for (let i = 0; i < 20; i++) { await tick(); net.calls.filter(c => !c.settled).forEach(c => c.resolve({result: {enable: false}})); }
  await next;
});

scenario('an answer that arrives after the inventory changed is not applied', async () => {
  const net = fakeNetwork(); net.manual = true;
  const w = world(net, ips(1));
  const round = w.pollEncoderRtmpStatus();
  await tick();
  vm.runInContext('unitDataEpoch += 1', w);                                // Clear Units happened
  net.calls[0].resolve({result: {enable: true}});
  await round;
  assert.strictEqual(net.calls.length, 1, 'no second read for a unit that is gone');
  assert.strictEqual(w.leds.get('198.51.100.1').live, undefined, 'nothing was painted');
});

scenario('the reachability round is one at a time and bounded, and a cancelled round does not mark units down', async () => {
  const net = fakeNetwork(); net.manual = true;
  const status = [];
  const sandbox = Object.assign(world(net, ips(1)), {
    rebootTracker: {}, isSliderOn: () => true, getVisibleIPs: () => ips(30), failureMap: new Map(),
    Upgrade: {isActive: () => false}, setStatus: (ip, text) => status.push([ip, text]),
    enableHeartbeat() {}, rebootHeartbeatOwned: false, pollVisibleUnitDetails: async () => {},
  });
  vm.runInContext([lift(page, 'tcpProbe'), 'let heartbeatRound = null;', lift(page, 'heartbeatOnce'),
                   lift(page, 'heartbeatRoundImpl')].join('\n'), sandbox);
  const a = sandbox.heartbeatOnce();
  const b = sandbox.heartbeatOnce();
  assert.strictEqual(a, b, 'a second call while one is running is the same round');
  for (let i = 0; i < 6; i++) { await tick(); }
  assert.ok(net.maxLive <= 8, 'probes in flight: ' + net.maxLive);
  sandbox.cancelBackgroundRequests();
  await a;
  assert.deepStrictEqual(status, [], 'cancelled probes are not a unit being down');
  assert.strictEqual(sandbox.failureMap.size, 0, 'a cancelled probe is not a missed one: nothing was counted against any unit');
  assert.strictEqual(vm.runInContext('heartbeatRound', sandbox), null, 'the round is released');
});

function badgeWorld(answers) {
  const badge = {style: {display: 'none'}, textContent: '', classList: {toggle() {}}};
  const timers = [];
  const calls = [];
  const ctx = {
    document: {getElementById: id => id === 'vwBadge' ? badge : null},
    bgFetch: (url) => { calls.push(url); const a = answers.shift(); if (a instanceof Error) return Promise.reject(a); return Promise.resolve({json: async () => a}); },
    setTimeout: (fn, ms) => { timers.push({fn, ms}); return timers.length; }, clearTimeout: () => {}, Number, Promise, Math,
  };
  vm.createContext(ctx);
  vm.runInContext(liftConst(page, 'VW_BADGE_RETRY_MS').replace('const VW_BADGE_RETRY_MS', 'var VW_BADGE_RETRY_MS') +
    '\nvar vwBadgeTimer = null;\n' + lift(page, 'refreshVideoWallBadge'), ctx);
  return {ctx, badge, timers, calls};
}

scenario('the video wall badge asks in the cancellable lane, and asks again, on a growing delay, while the answer is pending', async () => {
  const w = badgeWorld([{ok: true, review_count: 0, by_status: {}, stale: true, pending: true},
                        {ok: true, review_count: 2, by_status: {CONFLICT: 1}, stale: true, pending: false},
                        {ok: true, review_count: 2, by_status: {CONFLICT: 1}}]);
  await w.ctx.refreshVideoWallBadge();
  assert.strictEqual(w.badge.style.display, 'none', 'pending is not "no candidates", but nothing is shown yet either');
  assert.strictEqual(w.timers.length, 1);
  assert.strictEqual(w.timers[0].ms, 4000);
  w.timers[0].fn(); await tick(); await tick();
  assert.strictEqual(w.badge.style.display, 'inline-flex', 'the last good count is shown while it is out of date');
  assert.strictEqual(w.timers[1].ms, 8000, 'the delay grows');
  w.timers[1].fn(); await tick(); await tick();
  assert.strictEqual(w.timers.length, 2, 'a settled answer is not asked for again');
  assert.deepStrictEqual(w.calls, ['/api/video_wall/candidates_summary', '/api/video_wall/candidates_summary', '/api/video_wall/candidates_summary']);
});

scenario('the badge gives up after a bounded number of asks, and a cancelled ask says nothing', async () => {
  const pending = () => ({ok: true, review_count: 0, by_status: {}, stale: true, pending: true});
  const w = badgeWorld([pending(), pending(), pending(), pending(), pending(), pending()]);
  await w.ctx.refreshVideoWallBadge();
  for (let i = 0; i < 6 && w.timers[i]; i++) { w.timers[i].fn(); await tick(); await tick(); }
  assert.ok(w.calls.length <= 4, 'asks: ' + w.calls.length);
  const shown = badgeWorld([{ok: true, review_count: 1, by_status: {}}, Object.assign(new Error('aborted'), {name: 'AbortError'})]);
  await shown.ctx.refreshVideoWallBadge();
  assert.strictEqual(shown.badge.style.display, 'inline-flex');
  await shown.ctx.refreshVideoWallBadge();
  assert.strictEqual(shown.badge.style.display, 'inline-flex', 'a cancelled read is not evidence there is nothing to show');
});

scenario('a pending answer does not hide a badge that was showing, and is not read as there being nothing to review', async () => {
  const w = badgeWorld([{ok: true, review_count: 2, by_status: {}},
                        {ok: true, review_count: 0, by_status: {}, stale: true, pending: true}]);
  await w.ctx.refreshVideoWallBadge();
  assert.strictEqual(w.badge.style.display, 'inline-flex');
  await w.ctx.refreshVideoWallBadge();
  assert.strictEqual(w.badge.style.display, 'inline-flex', 'a count of zero that is not an answer removed the badge');
});

scenario('Clear Units stops background reads before it asks anything, and restarts them only if nothing was cleared', () => {
  const code = page.replace(/\/\/.*$/gm, '');
  const at = code.indexOf('$("#clear_units").addEventListener');
  assert.ok(at > 0);
  const handler = code.slice(at, code.indexOf('// Targets input clear button', at) > 0 ? code.indexOf('Targets input clear button', at) : at + 6000);
  const cancel = handler.indexOf('cancelBackgroundRequests()');
  const run = handler.indexOf('window.lumaClearUnits.run(');
  assert.ok(cancel > 0 && run > cancel, 'the lane is cancelled before the question is asked');
  assert.ok(/stopRtmpPolling\(\)[\s\S]*enableHeartbeat\(false\)[\s\S]*lumaClearUnits\.run\(/.test(handler));
  assert.ok(/finally\s*\{\s*resumePolls\(\)/.test(handler), 'a cancelled question restarts what was stopped');
  assert.ok(/if \(cleared\) return;/.test(handler), 'and an emptied inventory is not polled');
  assert.ok(/rtmpBackoff\.clear\(\)/.test(handler));
});

(async () => {
  for (const [name, fn] of scenarios) {
    try { await Promise.race([fn(), new Promise((_, rej) => setTimeout(() => rej(new Error('timed out')), 5000))]); passed++; }
    catch (err) { console.error('FAILED: ' + name + '\n  ' + (err && err.stack || err)); process.exitCode = 1; }
  }
  console.log(passed + ' scenarios passed');
  if (process.exitCode) console.log('FAILURES');
})();
