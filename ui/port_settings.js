// Settings > Local Application Port.
//
// The port of the local application listener the LumaSuite launcher starts on 127.0.0.1. It is NOT
// the Remote Access listener, which has its own port, and nothing here touches that one.
//
// Presentation only. The launcher's controller decides everything and persists nothing until the
// operator explicitly asks (Save and Restart, Make <port> Fixed, Change Fixed Port). The section
// is shown to the local computer only; a remote session never sees it and the endpoints refuse it.
//
//   * Automatic is the default: LumaSuite picks an available port. Unchecked shows the port in use.
//   * Checking "Use a fixed TCP port" asks for a port, pre-filled with the port actually in use,
//     and needs a restart ("Save and Restart").
//   * If the fixed port was busy at start the launcher offered a temporary alternate; here that
//     reads as "Running temporarily on TCP port N" with Retry / Make N Fixed / Change.
(function () {
  'use strict';

  var els = {};
  var state = null;
  var busy = false;

  function $(id) { return document.getElementById(id); }
  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = String(text);
    return node;
  }
  function row(labelText, forId) {
    var r = el('div', 'modal-row');
    var l = el('label', '', labelText);
    if (forId) l.setAttribute('for', forId);
    r.appendChild(l);
    return r;
  }
  function say(message, kind) { if (window.toast) window.toast(message, kind); }
  function ask(options) {
    // A confirmation whose wording is the point is refused, not downgraded, when the shared
    // dialog is unavailable.
    return window.lumaConfirm ? window.lumaConfirm(options) : Promise.resolve(false);
  }
  function addressOf(port) { return 'http://127.0.0.1:' + port + '/'; }

  function api(path, options) {
    options = options || {};
    var init = {method: options.method || 'GET', credentials: 'same-origin',
                headers: {'Accept': 'application/json'}, cache: 'no-store'};
    if (options.body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(options.body);
    }
    return fetch(path, init).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (body) {
        return {ok: res.ok && body.ok !== false, status: res.status, body: body};
      });
    });
  }

  // Mirrors the server's rule so a typo is caught before a request; the server decides.
  function checkPort(text) {
    var t = String(text == null ? '' : text).trim();
    if (!t) return 'Enter a TCP port number.';
    if (!/^[0-9]{1,6}$/.test(t)) return 'A TCP port is a whole number between 1024 and 65535.';
    var n = parseInt(t, 10);
    if (n < 1024 || n > 65535) return 'Use a TCP port between 1024 and 65535.';
    return '';
  }

  // ------------------------------------------------------------ markup
  function build(host) {
    var section = el('div', 'settings-section');
    section.id = 'cfg_port_section';
    section.appendChild(el('h4', '', 'Local Application Port'));

    var checkRow = row('Use a fixed TCP port');
    els.fixed = document.createElement('input');
    els.fixed.type = 'checkbox';
    els.fixed.id = 'cfg_port_fixed';
    checkRow.appendChild(els.fixed);
    section.appendChild(checkRow);

    var portRow = row('Port', 'cfg_port_value');
    els.port = document.createElement('input');
    els.port.type = 'text';
    els.port.id = 'cfg_port_value';
    els.port.readOnly = true;                    // changed through the buttons and the dialog, never by typing here
    portRow.appendChild(els.port);
    section.appendChild(portRow);

    els.address = el('div', 'small', '');
    els.address.id = 'cfg_port_address';
    els.address.style.margin = '0 0 6px';
    section.appendChild(els.address);

    els.helper = el('div', 'small', 'Use a fixed port to keep LumaSuite’s local browser address ' +
                    'consistent after restart.');
    els.helper.id = 'cfg_port_helper';
    els.helper.style.cssText = 'margin:0 0 6px;color:var(--text-muted);';
    section.appendChild(els.helper);

    els.auto = el('div', 'small', 'LumaSuite automatically selects an available port.');
    els.auto.id = 'cfg_port_auto';
    els.auto.style.cssText = 'margin:0 0 6px;color:var(--text-muted);';
    section.appendChild(els.auto);

    // Running on a temporary alternate port.
    els.temp = el('div');
    els.temp.id = 'cfg_port_temp';
    els.temp.hidden = true;
    els.tempWarn = el('div', 'notice notice-warning', '');
    els.tempWarn.id = 'cfg_port_temp_warning';
    els.tempConfigured = el('div', 'small', '');
    els.tempConfigured.id = 'cfg_port_temp_configured';
    els.tempCurrent = el('div', 'small', '');
    els.tempCurrent.id = 'cfg_port_temp_current';
    els.temp.appendChild(els.tempWarn);
    els.temp.appendChild(els.tempConfigured);
    els.temp.appendChild(els.tempCurrent);
    var tempButtons = el('div');
    tempButtons.style.cssText = 'display:flex;gap:6px;flex-wrap:wrap;margin:8px 0;';
    els.retry = el('button', 'btn small', 'Retry Fixed Port');
    els.retry.type = 'button';
    els.retry.id = 'cfg_port_retry';
    els.make = el('button', 'btn small secondary', 'Make Fixed');
    els.make.type = 'button';
    els.make.id = 'cfg_port_make';
    els.changeTemp = el('button', 'btn small secondary', 'Change Fixed Port');
    els.changeTemp.type = 'button';
    els.changeTemp.id = 'cfg_port_change_temp';
    [els.retry, els.make, els.changeTemp].forEach(function (b) { tempButtons.appendChild(b); });
    els.temp.appendChild(tempButtons);
    section.appendChild(els.temp);

    // Fixed and healthy.
    var actions = el('div');
    actions.style.cssText = 'display:flex;gap:6px;flex-wrap:wrap;margin:8px 0;';
    els.change = el('button', 'btn small secondary', 'Change Fixed Port');
    els.change.type = 'button';
    els.change.id = 'cfg_port_change';
    els.change.hidden = true;
    actions.appendChild(els.change);
    section.appendChild(actions);

    els.status = el('div', 'small', '');
    els.status.id = 'cfg_port_status';
    els.status.style.margin = '4px 0';
    section.appendChild(els.status);

    // Shown instead of the controls when there is no launcher to change the port (or not here).
    els.unavailable = el('div', 'small', '');
    els.unavailable.id = 'cfg_port_unavailable';
    els.unavailable.style.cssText = 'margin:0 0 6px;color:var(--text-muted);';
    els.unavailable.hidden = true;
    section.appendChild(els.unavailable);

    els.section = section;
    host.insertBefore(section, host.firstChild);        // above Remote Access: a different listener
  }

  function status(text) { if (els.status) els.status.textContent = text || ''; }

  // ------------------------------------------------------------ rendering
  function setControls(on) {
    [els.fixed, els.retry, els.make, els.change, els.changeTemp].forEach(function (c) {
      if (c) c.disabled = !on || busy;
    });
  }

  function render(s) {
    state = s;
    els.section.hidden = false;
    if (!s || s.available === false) {
      els.unavailable.hidden = false;
      els.unavailable.textContent = (s && s.message) ||
        'The local application port is not available in this run.';
      [els.fixed, els.port, els.address, els.helper, els.auto, els.temp, els.change].forEach(function (n) {
        if (n && n.parentNode) n.hidden = true;
      });
      if (els.fixed.parentNode) els.fixed.parentNode.hidden = true;
      if (els.port.parentNode) els.port.parentNode.hidden = true;
      return;
    }
    els.unavailable.hidden = true;
    els.fixed.parentNode.hidden = false;
    els.port.parentNode.hidden = false;
    els.fixed.hidden = false; els.port.hidden = false; els.address.hidden = false; els.helper.hidden = false;
    var fixed = !!s.fixed;
    els.fixed.checked = fixed;
    // With the box ticked the field is the CONFIGURED fixed port; a temporary current port is
    // shown in the 'Current port' line, never as the setting.
    var shownPort = (fixed && s.configured_port != null) ? s.configured_port : s.current_port;
    els.port.value = String(shownPort == null ? '' : shownPort);
    els.address.textContent = s.current_port == null ? '' :
      'Current local address: ' + addressOf(s.current_port);
    els.auto.hidden = fixed;
    els.helper.hidden = fixed;
    els.temp.hidden = !s.temporary;
    els.change.hidden = !(fixed && !s.temporary);
    if (s.temporary) {
      els.tempWarn.textContent = '⚠ Running temporarily on TCP port ' + s.current_port;
      els.tempConfigured.textContent = 'Configured fixed port: ' + s.configured_port;
      els.tempCurrent.textContent = 'Current port: ' + s.current_port;
      els.make.textContent = 'Make ' + s.current_port + ' Fixed';
    } else if (fixed) {
      els.helper.hidden = false;
      els.helper.textContent = 'Fixed port ' + s.configured_port + '. LumaSuite’s local browser ' +
        'address stays the same after restart.';
    }
    if (!fixed) {
      els.helper.hidden = false;
      els.helper.textContent = 'Use a fixed port to keep LumaSuite’s local browser address ' +
        'consistent after restart.';
    }
    setControls(true);
  }

  // Ask who this is from the session answer the page already has, so a remote administrator is
  // never sent to a request that can only be refused.
  function isLocalComputer() {
    if (!window.lumaSession) return Promise.resolve(true);
    return window.lumaSession().then(function (s) { return !!(s && s.local); }, function () { return true; });
  }

  function load() {
    return isLocalComputer().then(function (local) {
      if (!local) { els.section.hidden = true; return null; }
      return api('/api/local_port');
    }).then(function (r) {
      if (!r) return;
      if (r.status === 404 || r.status === 401 || r.status === 403) {
        els.section.hidden = true;                      // not the local computer: not offered
        return;
      }
      if (!r.ok && !r.body) return;
      render(r.body);
    }).catch(function () { status('The port could not be read.'); });
  }

  // ------------------------------------------------------------ actions
  function post(action, body) {
    busy = true; setControls(true);
    return api('/api/local_port/' + action, {method: 'POST', body: body || {}}).then(function (r) {
      busy = false;
      return r;
    }, function (e) { busy = false; throw e; });
  }

  function failure(r, fallback) {
    var message = (r.body && r.body.message) || fallback;
    status(message);
    say(message, 'error');
  }

  // Move the browser to the address the server is now on. A different port is a different origin,
  // so wait (without reading the reply) until something answers there, then go.
  function waitAndGo(url, attempt) {
    attempt = attempt || 0;
    var same = false;
    try { same = window.location && window.location.origin === new URL(url).origin; } catch (e) { same = false; }
    if (same) { load(); return; }
    var go = function () { window.location.href = url; };
    fetch(url + 'api/app_version', {mode: 'no-cors', cache: 'no-store'}).then(go, function () {
      if (attempt >= 40) { status('LumaSuite did not answer at ' + url + '. Open that address by hand.'); return; }
      setTimeout(function () { waitAndGo(url, attempt + 1); }, 1000);
    });
  }

  // The dialog that asks for a port, pre-filled with the port actually in use.
  function askPort(options, error) {
    var typed = '';
    return ask({
      title: options.title,
      message: (error ? error + ' ' : '') + options.message,
      summary: [{label: 'Current local address:', value: addressOf(state.current_port)}],
      confirmText: options.confirmText,
      cancelText: 'Cancel',
      field: {type: 'text', label: 'TCP port:', value: String(state.current_port), help: options.help,
              onValue: function (v) { typed = v; }}
    }).then(function (yes) {
      if (yes !== true) return null;
      var problem = checkPort(typed);
      if (problem) return askPort(options, problem);
      return String(typed).trim();
    });
  }

  function enableFixed() {
    return askPort({
      title: 'Use a fixed local LumaSuite port',
      message: 'A fixed port keeps LumaSuite’s local browser address consistent after restart.',
      help: 'The current port has been filled in automatically. Keep it or enter another available ' +
            'TCP port. Changing this setting requires LumaSuite to restart.',
      confirmText: 'Save and Restart'
    }).then(function (port) {
      if (port == null) { render(state); return; }       // Cancel: nothing changes, the box unchecks
      return post('enable', {port: parseInt(port, 10)}).then(function (r) {
        if (!r.ok) { failure(r, 'The port could not be saved.'); render(state); return; }
        status('LumaSuite is restarting. This page will move to ' + r.body.url + ' when it is ready.');
        waitAndGo(r.body.url);
      });
    });
  }

  function disableFixed() {
    return ask({
      title: 'Use automatic local port selection?',
      message: 'LumaSuite will return to automatic local port selection. The local browser address ' +
               'may change after restart.',
      confirmText: 'Use Automatic', cancelText: 'Cancel'
    }).then(function (yes) {
      if (yes !== true) { render(state); return; }
      return post('disable').then(function (r) {
        if (!r.ok) { failure(r, 'The setting could not be saved.'); render(state); return; }
        status(r.body.message || 'Automatic local port selection is on.');
        return load();
      });
    });
  }

  function retryFixed() {
    return post('retry').then(function (r) {
      if (!r.ok) { failure(r, 'The fixed port is still unavailable.'); load(); return; }
      status('Running on the configured port. Moving to ' + r.body.url + '.');
      waitAndGo(r.body.url);
    });
  }

  function makeFixed() {
    var n = state.current_port;
    return ask({
      title: 'Make ' + n + ' the fixed port?',
      message: 'LumaSuite will use TCP port ' + n + ' every time it starts.',
      confirmText: 'Make ' + n + ' Fixed', cancelText: 'Cancel'
    }).then(function (yes) {
      if (yes !== true) return;
      return post('make_fixed').then(function (r) {
        if (!r.ok) { failure(r, 'The port could not be saved.'); return; }
        status('TCP port ' + n + ' is now the fixed port.');
        return load();
      });
    });
  }

  function changeFixed() {
    return askPort({
      title: 'Change fixed TCP port',
      message: 'LumaSuite will start on the new port now and use it every time it starts.',
      help: 'The port is saved only after LumaSuite has started on it. If it is unavailable nothing changes.',
      confirmText: 'Start'
    }).then(function (port) {
      if (port == null) return;
      return post('change', {port: parseInt(port, 10)}).then(function (r) {
        if (!r.ok) { failure(r, 'The port could not be changed.'); return; }
        status('The fixed port is now ' + port + '. Moving to ' + r.body.url + '.');
        waitAndGo(r.body.url);
      });
    });
  }

  function wire() {
    els.fixed.addEventListener('change', function () {
      if (busy) { render(state); return; }
      if (els.fixed.checked) enableFixed(); else disableFixed();
    });
    els.retry.addEventListener('click', retryFixed);
    els.make.addEventListener('click', makeFixed);
    els.change.addEventListener('click', changeFixed);
    els.changeTemp.addEventListener('click', changeFixed);
    document.addEventListener('luma:settings-open', load);
  }

  function insideOpenDialog(node) {
    for (var n = node; n; n = n.parentNode) {
      if (n.classList && n.classList.contains('open')) return true;
    }
    return false;
  }

  function init() {
    var host = $('cfg_extension_point');
    if (!host || $('cfg_port_section')) return;         // built once, never duplicated
    build(host);
    wire();
    els.section.hidden = true;                          // until the server has said this is the local computer
    if (insideOpenDialog(host)) load();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
}());
