/*
 * One mirrored horizontal scrollbar, shared by every section that overflows.
 *
 * WHY THIS FILE EXISTS
 * --------------------
 * A container's own horizontal scrollbar sits at its bottom edge. On a
 * viewport-tall inventory, or on a route grid that fills 70vh, reaching it
 * means scrolling past every row first -- so the control that moves the table
 * sideways is only available once the operator has stopped looking at the
 * thing they wanted to move. Every section that scrolls horizontally therefore
 * also gets a scrollbar immediately above its content, driving the same
 * container.
 *
 * Device Info grew one of these first, written inline in ui/index.html. Four
 * more sections need the identical behaviour, and four more copies is four
 * implementations to keep in step -- so the behaviour lives here and the pages
 * supply only the opt-in and the placement CSS.
 *
 * THE OPT-IN IS EXPLICIT
 * ----------------------
 * A scroll container asks for a mirror by carrying `data-mirror-scroll`.
 * Nothing else on the page is touched: "every div that happens to overflow" is
 * how a tooltip, a dropdown or a code block acquires a scrollbar nobody asked
 * for. The supporting attributes, all optional:
 *
 *   data-mirror-strip="<id>"       adopt the element with this id as the strip
 *                                  instead of creating one, so a page that has
 *                                  already placed and styled its own mirror
 *                                  keeps that placement. The strip is moved to
 *                                  sit immediately before the container if it
 *                                  is not there already.
 *   data-mirror-class="<class>"    class for a strip this module creates
 *                                  (default `mirror-scroll`).
 *   data-mirror-height-var="--x"   publish the strip's rendered height as this
 *                                  custom property on the document element, for
 *                                  a stylesheet that has to reserve the space.
 *
 * NO ASSUMED WIDTHS
 * -----------------
 * The track is sized from the container's real `scrollWidth`, and the strip
 * itself from the container's measured `clientWidth` and left content edge. A
 * strip given the page width instead has a LARGER maximum scrollLeft than the
 * container, because the container loses pixels to its border and to a vertical
 * scrollbar that comes and goes with the number of rows -- measured on Device
 * Info at 158 against 160 before that was corrected, so dragging the mirror to
 * its right-hand end left the table two pixels short of its own.
 *
 * THE REENTRANCY GUARD
 * --------------------
 * Assigning `scrollLeft` makes the receiver fire its own scroll event, which
 * would assign straight back. `syncOwner` lets only the element that began the
 * exchange drive it, and ownership is released on the next animation frame, by
 * which time the echo has been discarded. One owner per container pair, so two
 * sections can never block each other's synchronisation.
 *
 * OBSERVERS ARE CREATED ONCE AND NEVER RE-ARMED
 * ---------------------------------------------
 * Exactly one ResizeObserver and one MutationObserver per container, both on
 * the container element, which is in the page markup and is never replaced --
 * only its rows are. Neither callback creates an observer. That is deliberate
 * and it is the whole reason this file is careful: ui/sticky-columns.js once
 * re-armed itself from inside its own MutationObserver callback without
 * disconnecting, so the observer count doubled per render (measured at 2, 4, 8,
 * 16, 32, 64, 128, 256 callbacks per batch of ten), every callback forced a
 * synchronous layout, and Chrome eventually put up "Page Unresponsive".
 * Anything whose target really is replaced must disconnect before re-observing;
 * here nothing is, so nothing is re-observed.
 *
 * Sizes are observed, never polled. There is no timer in this file.
 */
'use strict';

(function () {
  const SELECTOR = '[data-mirror-scroll]';
  const STRIP_CLASS = 'mirror-scroll';
  const TRACK_CLASS = 'mirror-scroll-track';

  const registry = new WeakMap();   // container -> record
  const records = [];               // registration order, for diagnostics and tests

  // Diagnostics are opt-in (`?diag=1`) and absent otherwise, so every call site
  // is guarded rather than assuming the global exists. The measure counter is
  // what makes "is the recalculation rate accelerating?" a question with a
  // number rather than an opinion.
  function diag(name) {
    if (window.omniDiag) window.omniDiag.count(name);
  }

  function frame(fn) {
    if (typeof requestAnimationFrame === 'function') return requestAnimationFrame(fn);
    // No frame clock (a non-browser host running this for a test). Running now
    // is still correct: the value guards below stop an echo regardless, the
    // guard simply stops owning the exchange sooner.
    fn();
    return 0;
  }

  // ---- geometry ----------------------------------------------------------

  const publishedVars = new Map();

  function publishHeight(record, height) {
    if (!record.heightVar) return;
    const value = Math.max(0, Math.round(height)) + 'px';
    // Writing an unchanged custom property still invalidates style, and this
    // write is reachable from the observers watching what it affects.
    if (publishedVars.get(record.heightVar) === value) return;
    publishedVars.set(record.heightVar, value);
    document.documentElement.style.setProperty(record.heightVar, value);
  }

  function measure(record) {
    diag('mirror.measure');
    const container = record.container;
    const strip = record.strip;
    const track = record.track;

    const clientWidth = container.clientWidth;
    const scrollWidth = container.scrollWidth;
    // One pixel of tolerance: a fractional layout width rounds to a scrollWidth
    // a hair over clientWidth on a container that visibly does not scroll.
    const overflows = scrollWidth - clientWidth > 1;

    // An empty scrollbar means nothing and invites a click that does nothing,
    // so the strip is present only while there is something to scroll.
    if (strip.hidden !== !overflows) strip.hidden = !overflows;

    if (overflows) {
      const trackWidth = scrollWidth + 'px';
      if (track.style.width !== trackWidth) track.style.width = trackWidth;

      // Same content width as the container, so the two have the same maximum
      // scrollLeft and their right-hand ends line up.
      const stripWidth = clientWidth + 'px';
      if (strip.style.width !== stripWidth) strip.style.width = stripWidth;

      // Same left edge, too. The strip is a block in the container's own
      // parent, so with no margin its left edge is the parent's content edge;
      // subtracting the margin already applied recovers that natural position
      // in one read, whatever the parent's padding is and however the container
      // itself is aligned (`.matrix-wrap` is centred with auto margins).
      const applied = parseFloat(strip.style.marginLeft) || 0;
      const natural = strip.getBoundingClientRect().left - applied;
      const wanted = container.getBoundingClientRect().left + container.clientLeft;
      const marginLeft = Math.round(wanted - natural) + 'px';
      if (strip.style.marginLeft !== marginLeft) strip.style.marginLeft = marginLeft;

      // The browser clamps scrollLeft when a scroller narrows, and it clamps
      // the two scrollers independently -- hiding a column while scrolled to
      // the far right otherwise leaves the mirror pointing at a width that no
      // longer exists. Re-asserting the container's position settles both.
      if (strip.scrollLeft !== container.scrollLeft) strip.scrollLeft = container.scrollLeft;
    }

    // Hidden, the strip measures zero, so whatever reserved space for it
    // reclaims the strip's height without a second rule.
    publishHeight(record, strip.offsetHeight);
  }

  // Every observer funnels through one frame-batched pass, so a render that
  // mutates six hundred cells measures once.
  const dirty = new Set();
  let pending = false;

  function scheduleMeasure(record) {
    dirty.add(record);
    if (pending) return;
    pending = true;
    diag('mirror.schedule');
    frame(() => {
      pending = false;
      const batch = Array.from(dirty);
      dirty.clear();
      batch.forEach(measure);
    });
  }

  function measureAll() {
    records.forEach(scheduleMeasure);
  }

  function measureNow() {
    dirty.clear();
    records.forEach(measure);
  }

  // ---- synchronisation ---------------------------------------------------

  function attachSync(record) {
    const container = record.container;
    const strip = record.strip;

    // Per pair, never shared: a guard held in module scope would let a scroll
    // in one section swallow a scroll that arrived in another in the same
    // frame, and the sections are supposed to be independent.
    let syncOwner = null;
    let syncFrame = 0;

    function releaseOwner() {
      if (syncFrame) return;
      syncFrame = frame(() => { syncFrame = 0; syncOwner = null; });
    }

    function follow(source, target) {
      if (syncOwner && syncOwner !== source) return;   // the reentrancy guard
      syncOwner = source;
      if (target.scrollLeft !== source.scrollLeft) target.scrollLeft = source.scrollLeft;
      releaseOwner();
    }

    strip.addEventListener('scroll', () => {
      diag('mirror.sync');
      follow(strip, container);
    }, {passive: true});
    // The container's own scrollbar, the wheel, a trackpad, a keyboard and a
    // programmatic scrollLeft all raise this one event, so the mirror follows
    // every one of them through the same path.
    container.addEventListener('scroll', () => {
      diag('mirror.sync');
      follow(container, strip);
    }, {passive: true});

    record.syncState = () => ({owner: syncOwner === strip ? 'strip'
                                    : syncOwner === container ? 'container' : null});
  }

  // ---- registration ------------------------------------------------------

  function findTrack(strip) {
    let track = strip.firstElementChild;
    if (!track) {
      track = document.createElement('div');
      track.className = TRACK_CLASS;
      strip.appendChild(track);
    }
    return track;
  }

  function buildStrip(container) {
    const adoptId = container.dataset.mirrorStrip;
    let strip = adoptId ? document.getElementById(adoptId) : null;
    if (!strip) {
      strip = document.createElement('div');
      strip.className = container.dataset.mirrorClass || STRIP_CLASS;
      strip.hidden = true;
    }
    // Immediately before the container it drives. A strip anywhere else is a
    // scrollbar the operator cannot associate with the thing it moves.
    if (strip.nextElementSibling !== container && container.parentNode) {
      container.parentNode.insertBefore(strip, container);
    }
    return strip;
  }

  function register(container) {
    const existing = registry.get(container);
    if (existing) return existing;                    // a second init() is a no-op
    if (!container.parentNode) return null;
    const strip = buildStrip(container);
    const record = {
      container,
      strip,
      track: findTrack(strip),
      heightVar: container.dataset.mirrorHeightVar || '',
      name: container.id || container.dataset.mirrorScroll || container.className,
    };
    registry.set(container, record);
    records.push(record);
    attachSync(record);
    observeOnce(record);
    scheduleMeasure(record);
    return record;
  }

  // ---- what makes the numbers stale --------------------------------------

  function observeOnce(record) {
    const container = record.container;
    // One of each, for the life of the container. The container is in the page
    // markup and is never replaced -- a render rewrites its table's rows -- so
    // neither observer ever needs re-pointing, and neither callback creates
    // one. See the header: re-arming from inside a callback is what made a page
    // stop answering.
    if (typeof ResizeObserver === 'function') {
      record.resizeObserver = new ResizeObserver(() => scheduleMeasure(record));
      record.resizeObserver.observe(container);
    }
    if (typeof MutationObserver === 'function') {
      // Row membership, and a column hidden or shown by class, both change
      // scrollWidth without resizing any box that already existed -- so the
      // subtree is watched, filtered to the attributes that can move a column.
      // Nothing written by measure() lands inside the container, so this cannot
      // re-trigger itself.
      record.mutationObserver = new MutationObserver(() => scheduleMeasure(record));
      record.mutationObserver.observe(container, {
        childList: true, subtree: true,
        attributes: true, attributeFilter: ['class', 'style', 'hidden'],
      });
    }
  }

  function init() {
    document.querySelectorAll(SELECTOR).forEach(register);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  // Created once, at module scope, on things that outlive every render.
  window.addEventListener('resize', measureAll, {passive: true});
  // Browser and pinch zoom change the CSS-pixel size of everything measured
  // here; visualViewport reports the pinch case that window.resize does not.
  if (window.visualViewport) {
    window.visualViewport.addEventListener('resize', measureAll, {passive: true});
  }
  // Layout template and appearance change density, and with it every width.
  if (typeof MutationObserver === 'function') {
    new MutationObserver(measureAll).observe(document.documentElement,
      {attributes: true, attributeFilter: ['data-template', 'class']});
  }
  // Web fonts land after first paint and change every column's width.
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(measureAll);
  window.addEventListener('load', measureAll, {passive: true});

  // Exposed so the geometry can be asserted directly rather than only by eye.
  const api = {init, register, measure, measureAll, measureNow, records, SELECTOR};
  window.mirrorScroll = api;
  if (typeof globalThis !== 'undefined') globalThis.mirrorScroll = api;
})();
