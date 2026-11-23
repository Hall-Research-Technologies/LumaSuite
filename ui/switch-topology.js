/*
 * Which network switch each unit is plugged into -- the Device Manager's multi-switch awareness.
 *
 * Everything shown here is derived on the server from the LLDP neighbour each unit already reported
 * during a scan, and arrives with the inventory the page already fetched (`lldp_topology` and
 * `topology_warning_acknowledged` beside `units`). This module makes no request of its own to learn
 * the topology, starts no timer and reads nothing from a device. Its one request is recording that
 * the operator dismissed the advisory.
 *
 * Two things live here and must not be conflated:
 *   * the banner, which the operator acknowledges once for the lifetime of the discovered inventory;
 *   * the "Different switch" note on a unit, which describes the current topology and is not
 *     affected by that acknowledgement at all.
 * Dismissing the banner is an acknowledgement. It is not a statement about the network, so it
 * silences the banner and nothing else.
 *
 * A unit with no usable LLDP is simply not marked: unknown is never "different".
 */
(function (root) {
  'use strict';

  // Every value here is device text on its way to the screen (a switch's own name). It is only ever
  // assigned as text -- nothing in this module builds markup, so there is nothing to escape and no
  // path by which a hostile name could become an element.

  // Functional state. `acknowledged` comes from the server with the inventory it belongs to and is
  // never read back out of the DOM: whether the banner is visible is a consequence of the flag.
  var state = {
    acknowledged: false,
    topology: null,
    devices: {},       // address -> {state, switch_name, ...} as the server derived it
    multiSwitch: false,
  };

  // A unit that was never read has no switch yet; the server learns it in the sweep it already
  // runs. Ask the (local, derived, no device traffic) topology a few more times, spaced out, and
  // stop: this is not a poll.
  var LEARN_DELAYS = [6000, 15000, 30000];
  var learn = {tries: 0, timer: null};

  function plural(n, one, many) { return n === 1 ? one : many; }

  // Advisory wording. Several switches are not presented as a fault: the point is that traffic may
  // cross an uplink, so uplink capacity is worth checking if problems appear.
  function warningText(topology) {
    var groups = (topology && topology.groups) || [];
    if (groups.length === 2 && !(topology && topology.tied)) {
      var primary = groups[0], other = groups[1];
      return {
        title: 'Multiple network switches detected.',
        detail: primary.count + ' ' + plural(primary.count, 'device is', 'devices are') +
          ' connected through the primary switch and ' +
          other.count + ' ' + plural(other.count, 'device is', 'devices are') +
          ' connected through another switch. ' +
          'If USB or AV-over-IP performance issues occur, verify uplink bandwidth between switches.'
      };
    }
    // Three or more switches, or a tie in which no group is the primary one. Summarised without
    // designating any group: with equal counts there is no basis for calling one the odd one out.
    return {
      title: 'Devices were detected across ' + ((topology && topology.switch_count) || groups.length) +
        ' network switches.',
      detail: 'If performance issues occur, verify inter-switch/uplink bandwidth and configuration.'
    };
  }

  // Whether the banner is on screen right now, separate from rendering so the decision can be
  // asserted directly rather than inferred from markup.
  function shouldShow(topology, acknowledged) {
    return !!(topology && topology.multi_switch) && !acknowledged;
  }

  // The note under a unit's MAC. Subtle by design: it says "different switch", not "broken device".
  function noteText(record) {
    if (!record || record.state !== 'different') return '';
    var name = record.switch_name || '';
    return 'Different switch' + (name ? ': ' + name : '');
  }

  function isDifferentSwitch(ip) {
    var record = state.devices[ip];
    return !!record && record.state === 'different';
  }

  // Marks (or unmarks) one MAC cell in place. The cell's own text -- the MAC -- is never touched.
  function decorateMacCell(td, unit) {
    if (!td) return td;
    var ip = (unit && unit.ip) || (td.dataset && td.dataset.ip) || '';
    var old = td.querySelector ? td.querySelector('.switch-note') : null;
    if (old) old.remove();
    td.classList.remove('alternate-switch');
    var text = noteText(state.devices[ip]);
    if (!text) return td;
    var doc = td.ownerDocument || root.document;
    var note = doc.createElement('span');
    note.className = 'switch-note';
    note.textContent = text;
    td.classList.add('alternate-switch');
    td.appendChild(note);
    return td;
  }

  // State-only refresh: the cells already on screen are updated where they are, never rebuilt.
  function refreshCells() {
    if (!root.document) return;
    var cells = root.document.querySelectorAll('#tbody td.mac-lldp[data-ip]');
    for (var i = 0; i < cells.length; i++) decorateMacCell(cells[i], null);
  }

  function renderBanner() {
    var banner = root.document && root.document.getElementById('switch_topology_warning');
    if (!banner) return;
    if (!shouldShow(state.topology, state.acknowledged)) { banner.hidden = true; return; }
    var text = warningText(state.topology);
    banner.querySelector('.switch-warning-title').textContent = text.title;
    banner.querySelector('.switch-warning-detail').textContent = text.detail;
    banner.hidden = false;
  }

  // Takes what the server derived. An absent topology (an older server, a failed read) leaves the
  // page showing no switch indication rather than inventing one.
  function scheduleLearn(topology, fromRefresh) {
    if (learn.timer && root.clearTimeout) { root.clearTimeout(learn.timer); learn.timer = null; }
    if (!fromRefresh) learn.tries = 0;
    var pending = topology && topology.units_not_read > 0;
    if (!pending || learn.tries >= LEARN_DELAYS.length || !root.setTimeout) return;
    learn.timer = root.setTimeout(refreshFromServer, LEARN_DELAYS[learn.tries]);
  }

  async function refreshFromServer() {
    learn.timer = null;
    learn.tries += 1;
    try {
      var res = await root.fetch('/api/lldp_topology', {cache: 'no-store'});
      var body = await res.json();
      if (body && body.ok && body.topology) {
        var before = state.topology ? state.topology.units_not_read : 0;
        apply(body.topology, body.acknowledged, true);
        // Units were read since the page last held the inventory: the rows (link speed, the pop-up)
        // are as stale as the topology was, so let the page reload them once.
        if (body.topology.units_not_read < before && typeof learn.onLearned === 'function') learn.onLearned();
      }
    } catch (err) {
      if (root.console) root.console.debug('Switch topology refresh failed:', err);
    }
  }

  function apply(topology, acknowledged, fromRefresh) {
    state.topology = topology && typeof topology === 'object' ? topology : null;
    state.acknowledged = acknowledged === true;
    state.devices = (state.topology && state.topology.devices) || {};
    state.multiSwitch = !!(state.topology && state.topology.multi_switch);
    renderBanner();
    refreshCells();
    scheduleLearn(state.topology, fromRefresh === true);
    return state;
  }

  // Hide at once so the control feels responsive, but the server is what stores the flag: if
  // recording it fails the state goes back and the banner returns, rather than pretending an
  // acknowledgement was kept.
  async function dismiss() {
    state.acknowledged = true;
    renderBanner();
    try {
      var res = await root.fetch('/api/lldp_topology/acknowledge', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({acknowledged: true}),
      });
      var body = await res.json();
      if (!body || !body.ok) throw new Error((body && body.error) || 'acknowledgement was not recorded');
    } catch (err) {
      state.acknowledged = false;
      renderBanner();
      if (root.console) root.console.error('Could not record the switch notice dismissal:', err);
      if (typeof root.toast === 'function') root.toast('Could not save that dismissal.', 'error');
    }
    return state.acknowledged;
  }

  // Bound as soon as the control exists, not after anything loads.
  function bind() {
    var button = root.document && root.document.getElementById('switch_topology_dismiss');
    if (button && !button.dataset.bound) {
      button.dataset.bound = '1';
      button.addEventListener('click', function () { dismiss(); });
    }
  }
  if (root.document) {
    if (root.document.readyState === 'loading') root.document.addEventListener('DOMContentLoaded', bind);
    else bind();
  }

  root.lumaSwitchTopology = {
    state: state, apply: apply, dismiss: dismiss, bind: bind, learn: learn, refreshFromServer: refreshFromServer,
    warningText: warningText, shouldShow: shouldShow,
    noteText: noteText,
    decorateMacCell: decorateMacCell, refreshCells: refreshCells,
    isDifferentSwitch: isDifferentSwitch,
  };
})(typeof window !== 'undefined' ? window : globalThis);
