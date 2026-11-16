/*
 * Firmware availability summary for Settings.
 *
 * Reads /api/firmware_status, which answers from the last good catalogue and the units already
 * discovered: opening Settings never waits on Hall Research, never starts a download and never
 * touches a device. Reports only. The one exception is the Check Now button, which asks once, on
 * purpose, through the server's own lookup.
 *
 * Three states in the operator's words -- "Up to Date", "Available", "Unknown" -- and an Unknown
 * always says why.
 */
(function () {
  'use strict';
  if (window.lumaFirmwareSummary) return;

  var STATES = {up_to_date: 'Up to Date', update_available: 'Available', unknown: 'Unknown'};
  var REASONS = {
    published_version_unavailable: 'Hall Research has no published version for this model yet.',
    installed_version_unreadable: "This unit's firmware version could not be read."
  };

  function stateOf(row) { return STATES[row && row.state] ? row.state : 'unknown'; }

  function when(seconds) {
    if (typeof seconds !== 'number' || !isFinite(seconds) || seconds <= 0) return '';
    return new Date(seconds * 1000).toLocaleString();
  }

  // What the section says about the whole inventory.
  function summarize(data) {
    if (!data || !data.ok) return {text: 'Firmware status is unavailable right now.', tone: 'muted', detail: '', rows: []};
    var rows = (Array.isArray(data.model_rows) ? data.model_rows : []).map(function (r) {
      var state = stateOf(r);
      return {model: String(r.model || ''), units: Number(r.units) || 0,
              installed: (Array.isArray(r.installed) ? r.installed : []).join(', '),
              latest: r.latest || '', state: state, label: STATES[state],
              reason: state === 'unknown' ? (REASONS[r.reason] || '') : ''};
    });
    var lookup = data.hall_lookup || {};
    var checked = when(lookup.fetched_at);
    var available = rows.filter(function (r) { return r.state === 'update_available'; }).length;
    var text, tone = 'muted';
    if (!checked) {
      text = lookup.state === 'unavailable' ? 'Could not reach Hall Research yet. It will try again.'
                                            : 'Checking Hall Research…';
    } else if (available) {
      text = available + ' model' + (available === 1 ? ' has' : 's have') + ' newer firmware available.';
      tone = 'warn';
    } else if (rows.length) {
      text = 'Everything with a published version is up to date.'; tone = 'ok';
    } else {
      text = 'No devices to compare yet.';
    }
    var detail = checked ? 'Last checked ' + checked + '.' : '';
    if (lookup.stale) detail += ' The last check could not reach Hall Research, so this is the last known result.';
    return {text: text, tone: tone, detail: detail.trim(), rows: rows};
  }

  function render(data) {
    var status = document.getElementById('cfg_fw_status');
    var detail = document.getElementById('cfg_fw_detail');
    if (!status || !detail) return;
    var summary = summarize(data);
    status.textContent = summary.text;
    status.className = 'update-status ' + (summary.tone === 'ok' ? 'ok' : summary.tone === 'warn' ? 'warn' : 'muted');
    detail.replaceChildren();
    if (summary.detail) {
      var line = document.createElement('div');
      line.textContent = summary.detail;
      detail.appendChild(line);
    }
    if (!summary.rows.length) return;
    var table = document.createElement('table');
    table.className = 'fw-table';
    var head = document.createElement('tr');
    ['Model', 'Installed', 'Latest', 'Status'].forEach(function (label) {
      var th = document.createElement('th'); th.textContent = label; head.appendChild(th);
    });
    table.appendChild(head);
    summary.rows.forEach(function (row) {
      var tr = document.createElement('tr');
      [row.model + (row.units > 1 ? ' × ' + row.units : ''), row.installed || '—',
       row.latest || '—', row.label].forEach(function (text, index) {
        var td = document.createElement('td');
        td.textContent = text;
        if (index === 3) {
          td.className = 'fw-state fw-' + row.state;
          if (row.reason) {
            var why = document.createElement('div');
            why.className = 'fw-reason';
            why.textContent = row.reason;
            td.appendChild(why);
          }
        }
        tr.appendChild(td);
      });
      table.appendChild(tr);
    });
    detail.appendChild(table);
  }

  // The local answer: what is remembered and what is on the network. Never waits on Hall Research.
  function fetchStatus() {
    return fetch('/api/firmware_status', {credentials: 'same-origin', cache: 'no-store'})
      .then(function (r) { return r.ok ? r.json() : null; })
      .catch(function () { return null; });
  }

  function refresh() { return fetchStatus().then(function (data) { render(data); return data; }); }

  var POLL_MS = 1000;
  var POLL_LIMIT = 30;

  // Check Now: one explicit check, started on the server through the same lookup the schedule uses,
  // then the (local) status is read until that check has finished. Resolves to the final status, or
  // null when the check could not even be started.
  function check(wait) {
    var pause = wait || function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };
    return fetch('/api/firmware_status/check', {method: 'POST', credentials: 'same-origin',
                                                headers: {'Content-Type': 'application/json'}, body: '{}'})
      .then(function (r) { return r.status === 202; })
      .catch(function () { return false; })
      .then(function (started) {
        if (!started) return null;
        var data = null, n = 0;
        function poll() {
          return pause(POLL_MS).then(fetchStatus).then(function (d) {
            data = d; n += 1;
            var running = d && d.hall_lookup && d.hall_lookup.running;
            return (running && n < POLL_LIMIT) ? poll() : data;
          });
        }
        return poll();
      });
  }

  // What happened, as one sentence for the operator and a tone for the toast.
  function outcome(before, after) {
    if (!after || !after.ok) return {text: 'The firmware check could not start.', tone: 'error'};
    var look = after.hall_lookup || {};
    if (look.running) return {text: 'The firmware check is still running.', tone: 'error'};
    var fresh = typeof look.last_attempt === 'number' && look.last_attempt > ((before && before.hall_lookup && before.hall_lookup.last_attempt) || 0);
    if (look.state === 'unavailable' || look.stale || !fresh) {
      return {text: 'Could not reach Hall Research. The last known result is shown.', tone: 'error'};
    }
    return {text: 'Firmware check complete.', tone: 'ok'};
  }

  function onCheckClick() {
    var button = document.getElementById('cfg_fw_check');
    var status = document.getElementById('cfg_fw_status');
    if (!button || button.disabled) return Promise.resolve(null);
    button.disabled = true;
    if (status) { status.textContent = 'Checking Hall Research…'; status.className = 'update-status muted'; }
    var before = null;
    return fetchStatus().then(function (d) { before = d; return check(); }).then(function (after) {
      return (after ? Promise.resolve(after) : fetchStatus()).then(function (final) {
        render(final);
        var result = outcome(before, after);
        if (window.toast) window.toast(result.text, result.tone);
        return final;
      });
    }).then(function (final) { button.disabled = false; return final; },
            function (err) { button.disabled = false; throw err; });
  }

  window.lumaFirmwareSummary = {summarize: summarize, render: render, refresh: refresh, check: check,
                                outcome: outcome, fetchStatus: fetchStatus, onCheckClick: onCheckClick};
  document.addEventListener('luma:settings-open', function () { refresh(); });
  // The button is built by Settings; the module owns what it does.
  document.addEventListener('click', function (e) {
    var t = e.target && e.target.closest ? e.target.closest('#cfg_fw_check') : null;
    if (t) onCheckClick();
  });
}());
