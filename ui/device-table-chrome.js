/*
 * Locked command area measurements for the Device Manager table.
 *
 * ui/device-table.css states the sticky relationships; this publishes the numbers
 * they are built from (header, scan bar, firmware bar and sticky-header heights,
 * the frozen columns' combined width, the platform scrollbar thickness). The same
 * module runs on OmniSuite's Device Info, so the two tables are positioned by one
 * mechanism. Sizes are observed, never polled.
 */
(function () {
  'use strict';

  // Every sticky offset on this page is derived from a height measured here and
  // published as a custom property. Hardcoding the stack (top: 0 / 37px / 91px)
  // is correct at exactly one font size, one density template and one zoom
  // level; a single Settings change or a browser zoom silently misaligns it and
  // the header row hides behind the command bars.
  const root = document.documentElement;
  const px = (n) => Math.max(0, Math.round(n)) + 'px';
  const published = new Map();

  function publish(name, value) {
    // Writing an unchanged value still invalidates style, and these writes can
    // be triggered by the observers watching what they affect.
    if (published.get(name) === value) return;
    published.set(name, value);
    root.style.setProperty(name, value);
  }

  function num(value) {
    const n = parseFloat(value);
    return Number.isFinite(n) ? n : 0;
  }

  // A bar's contribution to the stack is its margin box: the margins between
  // the bars are part of the height the next sticky element has to clear.
  function outerHeight(el) {
    if (!el) return 0;
    const rect = el.getBoundingClientRect();
    if (!rect.height) return 0;
    const cs = getComputedStyle(el);
    return rect.height + num(cs.marginTop) + num(cs.marginBottom);
  }

  const header = document.getElementById('app_header');
  const scanBar = document.getElementById('scan_bar');
  const firmwareBar = document.getElementById('firmware_bar');
  const footer = document.querySelector('.footer-bar');
  const wrapper = document.getElementById('units_table_wrapper');
  const table = wrapper ? wrapper.querySelector('table.device-table') : null;
  const tbody = document.getElementById('tbody');
  const mirror = document.getElementById('table_scroll_mirror');
  const mirrorTrack = document.getElementById('table_scroll_mirror_track');
  if (!wrapper || !table || !mirror || !mirrorTrack) return;

  function measureShell() {
    const bodyStyle = getComputedStyle(document.body);
    publish('--shell-gutter', px(num(bodyStyle.marginTop)));
    publish('--shell-header-h', px(outerHeight(header)));
    publish('--scan-bar-h', px(outerHeight(scanBar)));
    publish('--firmware-bar-h', px(outerHeight(firmwareBar)));
    // Whatever has to remain visible below the table viewport, so bounding the
    // table never pushes Reboot/Remove Selected off the bottom of the window.
    publish('--table-viewport-reserve',
            px(outerHeight(footer) + num(bodyStyle.marginBottom)));
    const head = table.tHead;
    publish('--table-head-h', px(head ? head.getBoundingClientRect().height : 0));

    // How much of a row the frozen columns occupy, so keyboard focus is never
    // scrolled to a position where they cover the element that just took it.
    const lead = head ? head.querySelectorAll('tr > th') : [];
    let frozen = 0;
    for (let i = 0; i < Math.min(3, lead.length); i += 1) {
      frozen += lead[i].getBoundingClientRect().width;
    }
    publish('--frozen-columns-w', px(frozen));

    // The platform's own horizontal scrollbar thickness, so the mirror is
    // exactly as tall as a scrollbar rather than a guessed strip. offsetHeight
    // counts the borders that clientHeight does not, so they come back out.
    const cs = getComputedStyle(wrapper);
    const borders = num(cs.borderTopWidth) + num(cs.borderBottomWidth);
    publish('--table-scrollbar-h',
            px(wrapper.offsetHeight - wrapper.clientHeight - borders));
  }

  function refresh() {
    measureShell();
  }

  // Every observer funnels through one frame-batched refresh, so a render that
  // mutates six hundred cells still measures once.
  let pending = 0;
  function scheduleRefresh() {
    if (pending) return;
    pending = requestAnimationFrame(() => { pending = 0; refresh(); });
  }

  // Scroll synchronisation, the reentrancy guard and the mirror geometry are
  // ui/mirror-scroll.js's job for every page, including this one. This module
  // keeps only what is peculiar to the device table pages: the heights of the locked
  // command bars, and the platform scrollbar thickness the strip is drawn at.

  // ---- what makes the numbers stale --------------------------------------
  // Sizes are observed, never polled: a timer would either lag a column toggle
  // or burn a frame every tick for a page that changes shape a few times a day.
  if (typeof ResizeObserver === 'function') {
    const ro = new ResizeObserver(scheduleRefresh);
    [header, scanBar, firmwareBar, footer, wrapper, table].forEach(el => el && ro.observe(el));
  }
  // Row membership changes the table's width without resizing any element that
  // already existed.
  if (tbody) new MutationObserver(scheduleRefresh).observe(tbody, {childList: true});
  // "Show Device Details" and the codec controls hide and show whole columns by
  // class, which changes scrollWidth while the table's own box may not move.
  new MutationObserver(scheduleRefresh).observe(table, {
    attributes: true, subtree: true, attributeFilter: ['class', 'style', 'hidden']
  });
  window.addEventListener('resize', scheduleRefresh, {passive: true});
  // Browser and pinch zoom change the CSS-pixel size of everything measured
  // here; visualViewport reports the pinch case that window.resize does not.
  if (window.visualViewport) {
    window.visualViewport.addEventListener('resize', scheduleRefresh, {passive: true});
  }
  // Web fonts land after first paint and change every bar's height.
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(scheduleRefresh);
  window.addEventListener('load', scheduleRefresh, {passive: true});

  refresh();

  // Exposed so the layout can be asserted directly rather than only by eye.
  window.deviceTableChrome = {refresh, wrapper, mirror, mirrorTrack};
})();
