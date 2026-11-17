// Routing on the restricted page does not lock the page, and a route is only ever drawn as what it is:
// in flight, awaiting confirmation, verified or failed.
//
// The page is real (ui/user.js, through tests/_user_page.js); the server is a recording fetch with
// delays the test controls. Asserted: what requests leave and when, and what a card shows -- not that
// some text exists in a file. Waits the page makes (the confirmation looks at 1.5 s, 3 s and 5 s) run
// at once.
'use strict';
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const {openPage, stateBody, sleep} = require('./_user_page');

let passed = 0;
async function scenario(name, fn) {
  try { await fn(); passed++; }
  catch (err) { console.error('FAILED: ' + name + '\n  ' + (err && err.stack || err)); process.exitCode = 1; }
}

// A held answer: the request leaves at once and is answered when the test says.
function held() {
  let release;
  const gate = new Promise(r => { release = r; });
  return {gate, release};
}
const rowOf = (state, over) => Object.assign({index: 0, name: 'Lobby', ok: state === 'verified',
  result: state === 'verified' ? 'verified' : state === 'unconfirmed' ? 'not_verified' : 'failed',
  audio: 'follows'}, over || {});
// What the server says about a request: one row per display (groups count two), and `targets` by position.
function answerFor(body, states) {
  const sent = [].concat((body.displays || []).map(() => 1), (body.groups || []).map(() => 2));
  const total = sent.reduce((a, b) => a + b, 0);
  const wanted = states || {};
  const targets = [].concat((body.displays || []).map((_, index) => ({kind: 'display', index, state: wanted.display ? wanted.display[index] || 'verified' : 'verified'})),
                            (body.groups || []).map((_, index) => ({kind: 'group', index, state: wanted.group ? wanted.group[index] || 'verified' : 'verified'})));
  const rows = Array.from({length: total}, (_, i) => rowOf(
    i < (body.displays || []).length && wanted.display ? (wanted.display[i] || 'verified') : 'verified', {index: i}));
  const routed = rows.filter(r => r.ok).length;
  return {ok: routed === total, routed, total, results: rows, targets,
          summary: routed + ' of ' + total + ' display' + (total === 1 ? '' : 's') + ' routed'};
}
const stateReads = page => page.requests.filter(r => r.url.split('?')[0] === '/api/user/state').length;
const checks = page => page.requests.filter(r => r.url.split('?')[0] === '/api/user/route_check');
const modalRows = page => ((page.confirms[0] && page.confirms[0].summary) || []).map(r => r.label + ': ' + r.value);

(async () => {
  await scenario('a route to another display goes out while the first is still being answered', async () => {
    const a = held();
    const page = await openPage({fastTimers: true, routes: {
      'POST /api/user/route_multi': rec => a.gate.then(() => ({status: 200, body: answerFor(rec.body)}))}});
    page.drop('src-a', 'display', 'dsp-1');
    page.drop('src-b', 'display', 'dsp-2');
    page.drop('src-c', 'display', 'dsp-3');
    assert.strictEqual(page.routeRequests().length, 3, 'three requests on the wire before any was answered');
    assert.ok(page.card('display', 'dsp-1').classList.contains('routing'));
    assert.ok(page.card('display', 'dsp-2').classList.contains('routing'));
    assert.strictEqual(page.byId.route_btn.disabled, true, 'nothing is chosen, so Route says so; it is not "busy"');
    a.release();
    await sleep(30);
    ['dsp-1', 'dsp-2', 'dsp-3'].forEach(id => assert.ok(page.card('display', id).classList.contains('route-ok'), id));
    page.stop();
  });

  await scenario('the display being routed to is not written to twice at once; the page says why', async () => {
    const a = held();
    const page = await openPage({routes: {'POST /api/user/route_multi': rec => a.gate.then(() => ({status: 200, body: answerFor(rec.body)}))}});
    page.drop('src-a', 'display', 'dsp-1');
    page.drop('src-b', 'display', 'dsp-1');
    assert.strictEqual(page.routeRequests().length, 1);
    assert.ok(page.toasts.some(t => /Still routing/.test(t.message)));
    a.release();
    await sleep(30);
    page.drop('src-b', 'display', 'dsp-1');                 // finished: now it goes
    assert.strictEqual(page.routeRequests().length, 2);
    page.stop();
  });

  await scenario('state is read once after a burst of routes, not once per route', async () => {
    const a = held();
    const page = await openPage({routes: {'POST /api/user/route_multi': rec => a.gate.then(() => ({status: 200, body: answerFor(rec.body)}))}});
    const before = stateReads(page);
    ['dsp-1', 'dsp-2', 'dsp-3', 'dsp-4'].forEach(id => page.drop('src-a', 'display', id));
    a.release();
    await sleep(60);
    assert.strictEqual(page.routeRequests().length, 4);
    assert.strictEqual(stateReads(page) - before, 1, 'one read for the whole burst');
    page.stop();
  });

  await scenario('the cards are not rebuilt by a route finishing, and another card keeps its mark', async () => {
    const page = await openPage({routes: {'POST /api/user/route_multi': rec => ({status: 200, body: answerFor(rec.body)})}});
    page.drop('src-a', 'display', 'dsp-1');
    await sleep(30);
    const kept = page.card('display', 'dsp-1');
    assert.ok(kept.classList.contains('route-ok'));
    page.drop('src-b', 'display', 'dsp-2');                  // a later route elsewhere...
    await sleep(30);
    assert.ok(page.card('display', 'dsp-1').classList.contains('route-ok'), '...does not wipe the first one');
    assert.ok(page.card('display', 'dsp-2').classList.contains('route-ok'));
    page.stop();
  });

  await scenario('a route the display has not confirmed yet is pending, never failed; a look later settles it', async () => {
    const look = held();
    const page = await openPage({fastTimers: true, routes: {
      'POST /api/user/route_multi': rec => ({status: 200, body: answerFor(rec.body, {display: ['unconfirmed']})}),
      'POST /api/user/route_check': rec => look.gate.then(() => ({status: 200, body: {ok: true,
        targets: [{kind: 'display', index: 0, state: 'verified'}]}}))}});
    page.drop('src-a', 'display', 'dsp-1');
    await sleep(30);
    const node = () => page.card('display', 'dsp-1');
    assert.ok(node().classList.contains('route-pending'), 'pending');
    assert.ok(!node().classList.contains('route-failed'), 'not drawn as a failure');
    assert.ok(!node().classList.contains('route-ok'), 'not drawn as a success');
    assert.ok(/Confirming/.test(node().textContent));
    assert.strictEqual(page.confirms.length, 0, 'no failure dialog while it is being confirmed');
    assert.ok(!page.toasts.some(t => t.kind === 'error'), 'no error toast');
    assert.strictEqual(checks(page).length, 1, 'it looked again, read-only');
    look.release();
    await sleep(30);
    assert.ok(node().classList.contains('route-ok'), 'verified');
    assert.ok(!node().classList.contains('route-pending'));
    assert.ok(page.toasts.some(t => t.message === 'Routed' && t.kind === 'ok'));
    assert.strictEqual(page.confirms.length, 0);
    page.stop();
  });

  await scenario('a display that never confirms ends as a verified failure, with its own sentence', async () => {
    const page = await openPage({fastTimers: true, routes: {
      'POST /api/user/route_multi': rec => ({status: 200, body: answerFor(rec.body, {display: ['unconfirmed']})}),
      'POST /api/user/route_check': {status: 200, body: {ok: false, targets: [{kind: 'display', index: 0, state: 'unconfirmed'}]}}}});
    page.drop('src-a', 'display', 'dsp-1');
    await sleep(150);
    assert.strictEqual(checks(page).length, 3, 'a bounded number of looks, then it stops');
    assert.ok(page.card('display', 'dsp-1').classList.contains('route-failed'));
    assert.deepStrictEqual(modalRows(page), ['Lobby: Sent, but the display did not confirm it']);
    page.stop();
  });

  await scenario('a look that cannot be answered is retried inside the same bounded budget', async () => {
    const page = await openPage({fastTimers: true, routes: {
      'POST /api/user/route_multi': rec => ({status: 200, body: answerFor(rec.body, {display: ['unconfirmed']})}),
      'POST /api/user/route_check': {status: 503, body: {}}}});
    page.drop('src-a', 'display', 'dsp-1');
    await sleep(150);
    assert.strictEqual(checks(page).length, 3);
    assert.ok(page.card('display', 'dsp-1').classList.contains('route-failed'));
    page.stop();
  });

  await scenario('each destination is marked by its own outcome', async () => {
    const page = await openPage({routes: {'POST /api/user/route_multi': rec => ({status: 200, body: answerFor(rec.body, {display: ['verified', 'failed']})})}});
    page.card('source', 'src-a').click();
    page.card('display', 'dsp-1').click();
    page.card('display', 'dsp-2').click();
    page.byId.route_btn.click();
    await sleep(40);
    assert.ok(page.card('display', 'dsp-1').classList.contains('route-ok'));
    assert.ok(page.card('display', 'dsp-2').classList.contains('route-failed'));
    assert.ok(!page.card('display', 'dsp-1').classList.contains('route-failed'), 'one failure does not mark its neighbour');
    page.stop();
  });

  await scenario('a late look at an older route never undoes a newer route to the same display', async () => {
    const late = held();
    let n = 0;
    const page = await openPage({fastTimers: true, routes: {
      'POST /api/user/route_multi': rec => ({status: 200, body: answerFor(rec.body, n++ === 0 ? {display: ['unconfirmed']} : {})}),
      'POST /api/user/route_check': () => late.gate.then(() => ({status: 200, body: {ok: false,
        targets: [{kind: 'display', index: 0, state: 'failed'}]}}))}});
    page.drop('src-a', 'display', 'dsp-1');
    await sleep(30);
    assert.ok(page.card('display', 'dsp-1').classList.contains('route-pending'));
    page.drop('src-b', 'display', 'dsp-1');                  // a newer route to the same display
    await sleep(30);
    assert.ok(page.card('display', 'dsp-1').classList.contains('route-ok'));
    late.release();
    await sleep(40);
    assert.ok(page.card('display', 'dsp-1').classList.contains('route-ok'), 'the older route has no say any more');
    assert.ok(!page.card('display', 'dsp-1').classList.contains('route-failed'));
    page.stop();
  });

  await scenario('a group is marked by the worst of its members and is looked at again when one is unconfirmed', async () => {
    const page = await openPage({fastTimers: true, routes: {
      'POST /api/user/route_multi': rec => ({status: 200, body: {ok: false, routed: 1, total: 2, summary: '1 of 2 displays routed',
        results: [rowOf('verified', {index: 0}), rowOf('unconfirmed', {index: 1})],
        targets: [{kind: 'group', index: 0, state: 'unconfirmed'}]}}),
      'POST /api/user/route_check': {status: 200, body: {ok: true, targets: [{kind: 'group', index: 0, state: 'verified'}]}}}});
    page.card('source', 'src-a').click();
    page.card('group', 'grp-a').click();
    page.byId.route_btn.click();
    await sleep(60);
    assert.strictEqual(checks(page).length, 1);
    assert.deepStrictEqual(checks(page)[0].body, {source: 'src-a', displays: [], groups: ['grp-a'], interface: 'standard'});
    assert.ok(page.card('group', 'grp-a').classList.contains('route-ok'));
    page.stop();
  });

  await scenario('nothing is looked at again when nothing was unconfirmed', async () => {
    const page = await openPage({routes: {'POST /api/user/route_multi': rec => ({status: 200, body: answerFor(rec.body)})}});
    page.drop('src-a', 'display', 'dsp-1');
    await sleep(40);
    assert.strictEqual(checks(page).length, 0);
    page.stop();
  });

  await scenario('a server that does not describe targets is treated as before: all together, immediately', async () => {
    const page = await openPage({routes: {'POST /api/user/route_multi': rec => {
      const body = answerFor(rec.body); delete body.targets; return {status: 200, body};}}});
    page.drop('src-a', 'display', 'dsp-1');
    await sleep(40);
    assert.ok(page.card('display', 'dsp-1').classList.contains('route-ok'));
    page.stop();
  });

  await scenario('request contract: the route, the matrix route and the look each send exactly their fields', async () => {
    const page = await openPage({fastTimers: true, routes: {
      'POST /api/user/route_multi': rec => ({status: 200, body: answerFor(rec.body, {display: ['unconfirmed']})}),
      'POST /api/user/route_check': {status: 200, body: {ok: true, targets: [{kind: 'display', index: 0, state: 'verified'}]}}}});
    page.drop('src-a', 'display', 'dsp-1');
    await sleep(40);
    assert.deepStrictEqual(page.routeRequests()[0].body, {source: 'src-a', displays: ['dsp-1'], groups: [], audio_follows_video: true});
    assert.deepStrictEqual(checks(page)[0].body, {source: 'src-a', displays: ['dsp-1'], groups: [], interface: 'standard'});
    page.stop();
    // And the paths are ones the server knows: pinned against its route table.
    const table = fs.readFileSync(path.join(__dirname, '..', 'luma_access.py'), 'utf8');
    ['/api/user/route_multi', '/api/user/route_check'].forEach(route => {
      assert.ok(table.includes('"' + route + '": Access.USER'), route + ' is a User route in luma_access.ROUTES');
    });
  });

  // ------------------------------------------------------------------ the matrix
  async function matrixPage(routes) {
    const page = await openPage({fastTimers: true, routes});
    page.byId.tab_matrix.click();
    await sleep(40);
    return page;
  }
  const cellOf = (page, d, s) => page.cell('display:' + d, s);

  await scenario('matrix: cells in different rows are routed together; the same row waits for itself', async () => {
    const a = held();
    const page = await matrixPage({'POST /api/user/route_multi': rec => a.gate.then(() => ({status: 200, body: answerFor(rec.body)}))});
    cellOf(page, 'dsp-1', 'src-a').click();
    cellOf(page, 'dsp-2', 'src-b').click();
    cellOf(page, 'dsp-1', 'src-c').click();                  // same row, mid-route: not sent
    assert.strictEqual(page.routeRequests().length, 2);
    assert.ok(cellOf(page, 'dsp-1', 'src-a').classList.contains('pending'));
    assert.ok(cellOf(page, 'dsp-2', 'src-b').classList.contains('pending'));
    assert.ok(!cellOf(page, 'dsp-3', 'src-a').classList.contains('pending'));
    assert.strictEqual(cellOf(page, 'dsp-3', 'src-a').getAttribute('aria-disabled'), 'false', 'other rows stay pressable');
    a.release();
    await sleep(40);
    assert.ok(!cellOf(page, 'dsp-1', 'src-a').classList.contains('pending'));
    assert.ok(!cellOf(page, 'dsp-1', 'src-a').classList.contains('failed'));
    page.stop();
  });

  await scenario('matrix: pending while unconfirmed, never failed, and failed only when verified so', async () => {
    const look = held();
    const page = await matrixPage({
      'POST /api/user/route_multi': rec => ({status: 200, body: answerFor(rec.body, {display: ['unconfirmed']})}),
      'POST /api/user/route_check': () => look.gate.then(() => ({status: 200, body: {ok: false,
        targets: [{kind: 'display', index: 0, state: 'failed'}]}}))});
    const cell = () => cellOf(page, 'dsp-1', 'src-a');
    cell().click();
    await sleep(40);
    assert.ok(cell().classList.contains('pending'), 'awaiting confirmation is pending');
    assert.ok(!cell().classList.contains('failed'), 'and not failed');
    assert.deepStrictEqual(page.routeRequests()[0].body, {source: 'src-a', displays: ['dsp-1'], groups: [], walls: [],
                                                          interface: 'matrix', mode: 'av'});
    assert.deepStrictEqual(checks(page)[0].body, {source: 'src-a', displays: ['dsp-1'], groups: [], interface: 'matrix'});
    look.release();
    await sleep(40);
    assert.ok(cell().classList.contains('failed'), 'a verified failure');
    assert.ok(!cell().classList.contains('pending'));
    page.stop();
  });

  console.log(passed + ' scenarios passed' + (process.exitCode ? ' (with failures)' : ''));
  process.exit(process.exitCode || 0);
})().catch(e => { console.error(e); process.exit(1); });
