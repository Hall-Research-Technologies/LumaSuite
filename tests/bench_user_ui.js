// What the page costs a person who routes: how long until it accepts the next route, and how long
// until a verified state is on screen. Not a test. Drives ui/user.js (or another copy of it, to compare
// a tree with the one before) with answers delayed by the figures the server benchmark measured.
//
//   node tests/bench_user_ui.js [--source path/to/user.js] [--server bench.json] [--out result.json]
//
// The page is real; the server is a fetch that waits as long as the measured server did. Real hardware
// is a separate, live step.
'use strict';
const fs = require('node:fs');
const {openPage, stateBody, SOURCES, DISPLAYS, GROUPS, sleep} = require('./_user_page');

const args = process.argv.slice(2);
const opt = (name, fallback) => { const i = args.indexOf(name); return i >= 0 ? args[i + 1] : fallback; };
const server = JSON.parse(fs.readFileSync(opt('--server'), 'utf8'));
const byName = Object.fromEntries(server.results.map(r => [r.scenario.split(' (')[0], r]));
const R = name => byName[name].response.median;
const S = byName['single route'].state.median;

function answer(responseMs) {
  return rec => {
    const b = rec.body || {};
    const displays = (b.displays || []).length, groups = (b.groups || []).length;
    const total = displays + groups * 2;
    const ok = r => ({ok: true, result: 'verified', audio: 'follows', name: 'x', index: r});
    return {status: 200, delay: responseMs, body: {
      ok: true, routed: total, total, summary: total + ' of ' + total + ' displays routed',
      results: Array.from({length: total}, (_, i) => ok(i)),
      targets: [].concat((b.displays || []).map((_, index) => ({kind: 'display', index, state: 'verified'})),
                         (b.groups || []).map((_, index) => ({kind: 'group', index, state: 'verified'})))}};
  };
}

async function run(name, responseMs, act) {
  const page = await openPage({source: opt('--source'), routes: {
    'POST /api/user/route_multi': answer(responseMs),
    'GET /api/user/state': {status: 200, body: stateBody(), delay: S},
    'GET /api/user/matrix': {status: 200, body: JSON.parse(JSON.stringify(require('./_user_page').matrixBody())), delay: S},
  }});
  // When each card first showed a verified route, whatever later clears it.
  const seen = {};
  const watcher = setInterval(() => {
    page.root.querySelectorAll('[data-kind]').forEach(n => {
      const key = n.getAttribute('data-kind') + ':' + n.getAttribute('data-id');
      if (n.classList.contains('route-ok') && !(key in seen)) seen[key] = page.now();
    });
  }, 1);
  page.seen = seen;
  const t0 = page.now();
  const marks = await act(page, t0);
  clearInterval(watcher);
  const answered = page.routeRequests().map(r => r.answered).filter(Boolean);
  const out = Object.assign({scenario: name, route_requests: page.routeRequests().length,
                             last_route_answered_ms: answered.length ? Math.round(Math.max(...answered) - t0) : null},
                            marks);
  page.stop();
  return out;
}

// Keep trying one action until the page accepts it (a request leaves) or the budget runs out.
async function untilAccepted(page, attempt, budgetMs) {
  const before = page.routeRequests().length;
  const began = page.now();
  while (page.now() - began < budgetMs) {
    attempt();
    await sleep(2);
    if (page.routeRequests().length > before) return page.now();
  }
  return null;
}
const okOn = (page, kind, id) => (kind + ':' + id) in page.seen;
async function until(page, test, budgetMs) {
  const began = page.now();
  while (page.now() - began < budgetMs) { if (test()) return page.now(); await sleep(2); }
  return null;
}
const rel = (t, t0) => t === null ? null : Math.round(t - t0);

(async () => {
  const results = [];
  const sources = SOURCES.map(s => s.id), displays = DISPLAYS.slice(0, 4).map(d => d.id);

  // one route
  results.push(await run('single route', R('single route'), async (page, t0) => {
    page.drop('src-a', 'display', 'dsp-1');
    const sent = page.routeRequests()[0] ? page.routeRequests()[0].sent : null;
    const free = await untilAccepted(page, () => page.drop('src-b', 'display', 'dsp-2'), 20000);
    const verified = await until(page, () => okOn(page, 'display', 'dsp-1'), 20000);
    return {submit_ms: sent === null ? null : Math.round(sent - t0), accepts_next_ms: rel(free, t0),
            first_verified_on_screen_ms: rel(verified, t0)};
  }));

  // five consecutive routes, each a drop of a source on a display
  results.push(await run('five consecutive routes', R('five consecutive routes'), async (page, t0) => {
    const plan = [['src-a', 'dsp-1'], ['src-b', 'dsp-2'], ['src-c', 'dsp-3'], ['src-a', 'dsp-4'], ['src-b', 'dsp-1']];
    const issued = [];
    for (const [s, d] of plan) {
      const t = await untilAccepted(page, () => page.drop(s, 'display', d), 30000);
      issued.push(t);
    }
    const last = await until(page, () => ['dsp-1', 'dsp-2', 'dsp-3', 'dsp-4'].every(id => okOn(page, 'display', id)), 30000);
    return {all_five_issued_ms: rel(issued[4], t0), second_accepted_ms: rel(issued[1], t0),
            all_verified_on_screen_ms: rel(last, t0)};
  }));

  // one source to four chosen displays, by the Route button
  const choose = (page, kind, ids) => ids.forEach(id => page.card(kind, id).click());
  results.push(await run('one source -> four displays', R('one source -> four displays'), async (page, t0) => {
    page.card('source', 'src-a').click();
    choose(page, 'display', displays);
    page.byId.route_btn.click();
    const free = await untilAccepted(page, () => page.drop('src-b', 'display', 'dsp-5'), 20000);
    const verified = await until(page, () => displays.every(id => okOn(page, 'display', id)), 20000);
    return {accepts_next_ms: rel(free, t0), verified_on_screen_ms: rel(verified, t0)};
  }));

  results.push(await run('one source -> two groups', R('one source -> two groups'), async (page, t0) => {
    page.card('source', 'src-a').click();
    choose(page, 'group', ['grp-a', 'grp-b']);
    page.byId.route_btn.click();
    const free = await untilAccepted(page, () => page.drop('src-b', 'display', 'dsp-5'), 20000);
    const verified = await until(page, () => ['grp-a', 'grp-b'].every(id => okOn(page, 'group', id)), 20000);
    return {accepts_next_ms: rel(free, t0), verified_on_screen_ms: rel(verified, t0)};
  }));

  results.push(await run('one source -> one display + one group', R('one source -> one display + one group'), async (page, t0) => {
    page.card('source', 'src-a').click();
    choose(page, 'display', ['dsp-3']);
    choose(page, 'group', ['grp-a']);
    page.byId.route_btn.click();
    const free = await untilAccepted(page, () => page.drop('src-b', 'display', 'dsp-5'), 20000);
    const verified = await until(page, () => okOn(page, 'display', 'dsp-3') && okOn(page, 'group', 'grp-a'), 20000);
    return {accepts_next_ms: rel(free, t0), verified_on_screen_ms: rel(verified, t0)};
  }));

  // matrix: five consecutive cells
  results.push(await run('five consecutive matrix cells', R('five consecutive matrix cells'), async (page, t0) => {
    page.byId.tab_matrix.click();
    await sleep(60);
    const issued = [];
    const t1 = page.now();
    const plan = [['src-a', 'dsp-1'], ['src-b', 'dsp-2'], ['src-c', 'dsp-3'], ['src-a', 'dsp-4'], ['src-b', 'dsp-1']];
    for (const [s, d] of plan) {
      const cell = () => page.cell('display:' + d, s);
      issued.push(await untilAccepted(page, () => cell() && cell().click(), 30000));
    }
    return {all_five_issued_ms: rel(issued[4], t1), second_accepted_ms: rel(issued[1], t1)};
  }));

  const text = JSON.stringify({page: opt('--source', 'ui/user.js'), server_state_ms: S, results}, null, 1);
  if (opt('--out')) fs.writeFileSync(opt('--out'), text);
  console.log(text);
  process.exit(0);
})().catch(e => { console.error(e); process.exit(1); });
