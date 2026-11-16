/*
 * Frozen identity columns for wide tables.
 *
 * The Encoder, Decoder and USB tables on Configure, and the Device Info
 * inventory, are all wider than the viewport and scroll horizontally. Once they
 * do, the operator is reading a row of values with no idea which device they
 * belong to. The leading identity columns stay put; everything else scrolls
 * behind them.
 *
 * A frozen column's offset is the *rendered* width of the columns to its left,
 * which depends on content, template density, font and zoom -- so it is
 * measured rather than assumed. A hardcoded offset looks right in one layout
 * and is wrong in the other three. Measurements are re-taken when the table is
 * rebuilt, when the element resizes, and when the layout or theme changes.
 *
 * How many columns freeze is the table's own business: `data-sticky-cols="3"`
 * asks for three, and the default is the two that `table.sticky-identity` has
 * always frozen. The stylesheet decides which columns are sticky; this module
 * only supplies the offsets, so one measurement path serves every page.
 */
'use strict';

(function () {
  const SELECTOR = 'table.sticky-identity, table[data-sticky-cols]';
  const measured = new WeakMap();

  // Named rather than composed, so the property a stylesheet has to write is
  // greppable from this file and a table cannot quietly ask for an offset that
  // no stylesheet consumes. The first column needs none: its left is 0.
  const OFFSET_PROPS = ['--sticky-col2-offset', '--sticky-col3-offset', '--sticky-col4-offset'];

  function stickyColumnCount(table) {
    // Two is what `sticky-identity` has always meant, so a table that does not
    // ask for a count keeps the behaviour it already had.
    const asked = parseInt(table.dataset.stickyCols || '', 10);
    if (!Number.isFinite(asked) || asked < 2) return 2;
    return Math.min(asked, OFFSET_PROPS.length + 1);
  }

  function leadingCells(table, count) {
    // The header row if there is one, otherwise the first body row: a table
    // rendered without a thead still needs correct offsets.
    const row = table.querySelector('thead tr') || table.querySelector('tr');
    if (!row) return [];
    return Array.from(row.children).slice(0, count);
  }

  function firstColumnWidth(table) {
    const cell = table.querySelector('thead tr > *:first-child')
      || table.querySelector('tr > *:first-child');
    return cell ? cell.getBoundingClientRect().width : 0;
  }

  function apply(table) {
    if (!table) return;
    const count = stickyColumnCount(table);
    const cells = leadingCells(table, count);
    if (!cells.length) return;

    // Column N's left offset is the sum of every rendered width before it, so
    // the offsets stay correct when any one of those columns changes width.
    const offsets = [];
    let running = 0;
    for (let i = 0; i < cells.length - 1; i += 1) {
      running += cells[i].getBoundingClientRect().width;
      offsets.push(Math.round(running));
    }
    // A table that has not been laid out yet measures zero everywhere; writing
    // that would publish offsets that are wrong rather than merely absent.
    if (!offsets.length || !offsets[0]) return;

    const signature = offsets.join(',');
    if (measured.get(table) === signature) return;   // nothing changed; do not touch the DOM
    measured.set(table, signature);
    offsets.forEach((offset, i) => {
      table.style.setProperty(OFFSET_PROPS[i], `${offset}px`);
    });
  }

  function applyAll() {
    document.querySelectorAll(SELECTOR).forEach(apply);
  }

  // One observer of each kind per table, for the life of the table.
  //
  // This used to re-arm itself from inside its own MutationObserver callback --
  // clearing the guard flag and calling watch() again -- without disconnecting
  // anything. Every re-render therefore left the previous observers attached,
  // and because each surviving callback re-armed once more, the count doubled
  // per render: measured at 2, 4, 8, 16, 32, 64, 128, 256 callbacks per batch of
  // ten re-renders. Each callback calls apply(), which reads
  // getBoundingClientRect() and so forces a synchronous layout, so the cost of
  // one render grew exponentially with how long the page had been open. On
  // Configure, which rebuilds its tables every five seconds, that is a thread
  // that eventually stops answering -- which is what "Page Unresponsive" is.
  //
  // The table element itself never goes away, so its MutationObserver never
  // needs replacing. Only the ResizeObserver does, because it watches the
  // leading CELLS and a re-render replaces those.
  const cellObservers = new WeakMap();

  function observeLeadingCells(table) {
    if (typeof ResizeObserver !== 'function') return;
    // Disconnect before re-observing: the previous cells are detached by the
    // re-render, and an observer still holding them is a leak as well as work.
    const previous = cellObservers.get(table);
    if (previous) previous.disconnect();
    const observer = new ResizeObserver(() => apply(table));
    leadingCells(table, stickyColumnCount(table)).forEach(cell => observer.observe(cell));
    cellObservers.set(table, observer);
  }

  function watch(table) {
    if (table.dataset.stickyWatched === '1') {
      // Already watched; just re-point the cell observer at the current cells.
      observeLeadingCells(table);
      measured.delete(table);
      apply(table);
      return;
    }
    table.dataset.stickyWatched = '1';
    observeLeadingCells(table);
    // Created once. A re-render is a childList change on this same element, so
    // this observer stays valid for as long as the table does.
    new MutationObserver(() => {
      measured.delete(table);
      observeLeadingCells(table);
      apply(table);
    }).observe(table, {childList: true, subtree: false});
    apply(table);
  }

  function init() {
    document.querySelectorAll(SELECTOR).forEach(watch);
    applyAll();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  // The tables exist in the markup from the start and are filled in later, so
  // each one's own observer handles its re-renders. There is deliberately no
  // document-wide subtree observer: it would fire on every row of every render
  // for no benefit.
  window.addEventListener('resize', applyAll, {passive: true});
  // Layout and theme change density, and with it the leading column widths.
  new MutationObserver(applyAll).observe(document.documentElement,
                                         {attributes: true, attributeFilter: ['data-template', 'class']});

  if (typeof globalThis !== 'undefined') {
    globalThis.stickyIdentityColumns = {apply, applyAll, init, firstColumnWidth, stickyColumnCount};
  }
})();
