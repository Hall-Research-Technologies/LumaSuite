// Regression coverage for the Video Wall post-route preview-refresh burst
// (see PROJECT.md's Video Wall preview-refresh section). Loads ui/wall.js
// into a small vm sandbox -- the same technique tests/video_wall_model.test.js
// already uses -- extended with a minimal fake DOM/`Image`/timer surface
// just sufficient to drive `refreshWallPreviewImages()` and
// `burstRefreshPreviewsForCells()` deterministically, without a browser or
// any real network/device I/O (hardware-fence rule from AGENTS.md: this file
// touches no socket/websocket/requests at all -- it only exercises frontend
// JS in a synthetic DOM).
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const wallJsPath = path.join(__dirname, "..", "ui", "wall.js");
const source = fs.readFileSync(wallJsPath, "utf8");

// ---------------------------------------------------------------------
// Regression guard #5 ("preview refresh does not generate device
// configuration .Set calls") and the cache-busting-only guard (#7): source-
// text checks against the exact functions this pass added/changed, so a
// future edit that sneaks a device write or a non-preview URL into either
// function fails this test immediately. `api(` is the ONLY function wall.js
// uses to call any backend/device-mutating endpoint (see e.g. liveRouteSource/
// liveRouteRegion/saveWallQuiet above) -- neither preview function may
// reference it.
function extractFunctionSource(fnName) {
  const start = source.indexOf(`function ${fnName}(`);
  assert.ok(start >= 0, `function ${fnName} not found in ui/wall.js`);
  // Walk braces from the first `{` after the signature to find the matching
  // close -- good enough for this file's formatting (no braces inside
  // string/regex literals in these specific functions).
  const braceStart = source.indexOf("{", start);
  let depth = 0;
  let i = braceStart;
  for (; i < source.length; i++) {
    if (source[i] === "{") depth++;
    else if (source[i] === "}") {
      depth--;
      if (depth === 0) { i++; break; }
    }
  }
  return source.slice(start, i);
}

const refreshFnSource = extractFunctionSource("refreshWallPreviewImages");
const burstFnSource = extractFunctionSource("burstRefreshPreviewsForCells");
for (const [name, body] of [["refreshWallPreviewImages", refreshFnSource], ["burstRefreshPreviewsForCells", burstFnSource]]) {
  assert.ok(!/\bapi\s*\(/.test(body), `${name} must never call api(...) (no device/.Set traffic from a preview refresh)`);
  assert.ok(!/\.Set\b/.test(body), `${name} must never reference a .Set-style device call`);
}

// The two call sites (liveRouteSource/liveRouteRegion) must fire the burst
// without awaiting it, so a route's own completion can never be delayed by
// preview refresh (regression guard #3, "route completion is not blocked").
const routeCallSites = [...source.matchAll(/burstRefreshPreviewsForCells\(targets\);/g)];
assert.ok(routeCallSites.length >= 2, "expected burstRefreshPreviewsForCells(targets) at both liveRouteSource and liveRouteRegion");
for (const match of routeCallSites) {
  const precedingText = source.slice(Math.max(0, match.index - 8), match.index);
  assert.ok(!/await\s*$/.test(precedingText), "burstRefreshPreviewsForCells(...) must not be awaited at its call site");
}

// ---------------------------------------------------------------------
// Behavioral test: a minimal fake DOM + Image + manually-driven fake timer
// queue (so the test does not have to wait multiple real seconds for
// PREVIEW_BURST_DELAYS_MS to elapse), sufficient to drive
// refreshWallPreviewImages()/burstRefreshPreviewsForCells() end-to-end.
// ---------------------------------------------------------------------

let fakeTimerId = 0;
let fakeTimers = []; // {id, fn}
let imageLoads = []; // every url an <img> was pointed at, in order

class FakeImage {
  set src(url) {
    this._src = url;
    imageLoads.push(url);
    // Resolve success asynchronously (microtask), like a real network
    // image load resolves later than the synchronous assignment -- this is
    // what lets `await preloadWallPreviewImage(url)` actually suspend.
    Promise.resolve().then(() => { if (this.onload) this.onload(); });
  }
  get src() { return this._src; }
}

function makeCell(pos, previewBase) {
  const cellEl = { dataset: { pos } };
  const previewEl = {
    dataset: { previewBase },
    style: {},
    closest(sel) { return sel === ".wall-cell" ? cellEl : null; }
  };
  cellEl.previewEl = previewEl;
  return { cellEl, previewEl };
}

function buildSandbox() {
  fakeTimerId = 0;
  fakeTimers = [];
  imageLoads = [];
  const cells = {
    "1:1": makeCell("1:1", "http://198.51.100.51/thumbnail/thumbnail1.jpg"),
    "1:2": makeCell("1:2", "http://198.51.100.68/thumbnail/thumbnail1.jpg")
  };
  const sandbox = {
    window: {},
    document: {
      addEventListener() {},
      querySelectorAll(sel) {
        if (sel === ".wall-cell-preview[data-preview-base]") {
          return Object.values(cells).map(c => c.previewEl);
        }
        return [];
      }
    },
    Image: FakeImage,
    console,
    Date,
    Math,
    Number,
    String,
    Map,
    Set,
    Array,
    Object,
    Promise
  };
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox);
  // Fake timer queue takes over BOTH scheduling primitives the preview code
  // uses (the preload-timeout race and the burst's own inter-step delays)
  // so the test can drive time deterministically instead of waiting for
  // PREVIEW_BURST_DELAYS_MS's real ~3 seconds to elapse.
  sandbox.window.setTimeout = (fn) => { const id = ++fakeTimerId; fakeTimers.push({ id, fn }); return id; };
  sandbox.window.clearTimeout = (id) => { fakeTimers = fakeTimers.filter(t => t.id !== id); };
  return { sandbox, cells };
}

async function flushMicrotasks(times = 8) {
  for (let i = 0; i < times; i++) await Promise.resolve();
}

// Fires exactly the next scheduled fake timer (FIFO), letting any
// microtasks it triggers settle before returning. Returns false when the
// queue is empty -- the burst has fully terminated.
async function fireNextFakeTimer() {
  await flushMicrotasks();
  const due = fakeTimers.shift();
  if (!due) return false;
  // A tiny REAL delay (Node's actual global setTimeout, never the
  // sandboxed/fake one) so Date.now() -- used unmodified from this file's
  // real environment inside refreshWallPreviewImages()'s cache-busting --
  // ticks forward between rounds instead of every round landing in the
  // same millisecond purely because the fake queue drains synchronously.
  await new Promise(resolve => globalThis.setTimeout(resolve, 2));
  due.fn();
  await flushMicrotasks();
  return true;
}

(async () => {
  // ---- previewUrlWithCacheBuster / decoderPreviewBaseUrl: pure, no DOM ---
  {
    const { sandbox } = buildSandbox();
    const { decoderPreviewBaseUrl, previewUrlWithCacheBuster } = sandbox.window.__videoWallTest;
    assert.equal(decoderPreviewBaseUrl({ ip: "198.51.100.51" }), "http://198.51.100.51/thumbnail/thumbnail1.jpg");
    assert.equal(decoderPreviewBaseUrl({}), "");
    const url1 = previewUrlWithCacheBuster("http://1.2.3.4/thumbnail/thumbnail1.jpg", 111);
    const url2 = previewUrlWithCacheBuster("http://1.2.3.4/thumbnail/thumbnail1.jpg", 222);
    assert.equal(url1, "http://1.2.3.4/thumbnail/thumbnail1.jpg?t=111");
    assert.equal(url2, "http://1.2.3.4/thumbnail/thumbnail1.jpg?t=222");
    assert.notEqual(url1, url2, "cache-busting must produce a different retrieval URL per refresh attempt");
    // Cache-busting must never touch the decoder endpoint path itself, only
    // the query string -- the decoder-side URL LumaSuite is told to fetch is
    // unchanged (do not change the decoder endpoint itself, per spec).
    assert.ok(url1.startsWith("http://1.2.3.4/thumbnail/thumbnail1.jpg"));
  }

  // ---- Scoped refresh: only the routed cell's preview is touched --------
  {
    const { sandbox, cells } = buildSandbox();
    const { burstRefreshPreviewsForCells, __setWallPreviewEnabledForTest } = sandbox.window.__videoWallTest;
    __setWallPreviewEnabledForTest(true);

    burstRefreshPreviewsForCells([{ row: 1, column: 1 }]);
    await flushMicrotasks();

    assert.ok(String(cells["1:1"].previewEl.style.backgroundImage || "").includes("198.51.100.51"),
      "the routed cell's preview must be refreshed immediately (first burst attempt)");
    assert.equal(cells["1:2"].previewEl.style.backgroundImage, undefined,
      "an unrelated cell's preview must NOT be touched by a scoped route (only affected previews refresh)");

    // Drain the rest of the burst and confirm it terminates on its own
    // rather than continuing to poll forever (regression guard #6).
    let rounds = 1; // the immediate attempt above already ran
    while (await fireNextFakeTimer()) rounds++;
    assert.equal(rounds, sandbox.window.__videoWallTest.PREVIEW_BURST_DELAYS_MS.length,
      "the burst must run exactly one attempt per configured delay, then stop");
    assert.equal(fakeTimers.length, 0, "no further preview-refresh timer may remain scheduled once the burst finishes");
    assert.ok(!(await fireNextFakeTimer()), "the burst must not schedule another round after its last attempt");

    // Every attempt for the routed cell used a distinct cache-busting url
    // (never the exact same request twice), and only that cell's base URL
    // appears in the fetch history.
    assert.equal(imageLoads.length, sandbox.window.__videoWallTest.PREVIEW_BURST_DELAYS_MS.length);
    assert.ok(imageLoads.every(u => u.startsWith("http://198.51.100.51/thumbnail/thumbnail1.jpg?t=")));
    assert.equal(new Set(imageLoads).size, imageLoads.length, "each burst attempt must use a fresh cache-busting value");
  }

  // ---- A new route supersedes a still-in-flight burst for a prior route -
  {
    const { sandbox, cells } = buildSandbox();
    const { burstRefreshPreviewsForCells, __setWallPreviewEnabledForTest } = sandbox.window.__videoWallTest;
    __setWallPreviewEnabledForTest(true);

    burstRefreshPreviewsForCells([{ row: 1, column: 1 }]); // first route, cell R1C1
    await flushMicrotasks();
    const firstRoundLoads = imageLoads.length;
    assert.ok(firstRoundLoads > 0);

    burstRefreshPreviewsForCells([{ row: 1, column: 2 }]); // second route, cell R1C2, before the first burst finished
    await flushMicrotasks();

    // Drain everything left in the fake timer queue.
    while (await fireNextFakeTimer()) { /* keep draining */ }

    // The superseded first burst must not have kept running once the second
    // one started -- no url pointed at decoder .51 (cell R1C1) after the
    // second route began.
    const loadsAfterSecondRoute = imageLoads.slice(firstRoundLoads);
    assert.ok(loadsAfterSecondRoute.every(u => u.includes("198.51.100.68")),
      "a new route's burst must supersede (cancel further rounds of) a still-in-flight previous burst rather than overlapping it");
    assert.ok(String(cells["1:2"].previewEl.style.backgroundImage || "").includes("198.51.100.68"));
  }

  // ---- wallPreviewEnabled=false: no fetch, no timer, a true no-op -------
  {
    const { sandbox } = buildSandbox();
    const { burstRefreshPreviewsForCells, __setWallPreviewEnabledForTest } = sandbox.window.__videoWallTest;
    __setWallPreviewEnabledForTest(false);
    burstRefreshPreviewsForCells([{ row: 1, column: 1 }]);
    await flushMicrotasks();
    assert.equal(imageLoads.length, 0, "preview refresh must be a no-op entirely when the preview toggle is off");
    assert.equal(fakeTimers.length, 0);
  }

  console.log("video wall preview refresh tests passed");
})().catch(e => {
  console.error(e);
  process.exit(1);
});
