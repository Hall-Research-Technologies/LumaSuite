// Settings > Remote Access, and what Settings hides when it is not the local computer.
//
// Loaded by settings.js and built into the same Settings dialog, so it looks and
// behaves like the rest of it. Presentation only: the server decides everything,
// and a hidden control is never the boundary.
//
//   * Remote access is off unless an administrator turns it on, and needs an
//     administrator account first (created on this computer).
//   * A first-time setup starts on HTTP, labelled not encrypted, and plain HTTP needs an
//     explicit confirmation. HTTPS is one choice away: a certificate that is missing,
//     unreadable or for the wrong address refuses to start -- it never falls back to
//     plaintext.
//   * Showing the device password and choosing a folder on the host computer work
//     only on that computer, so a remote administrator is not offered them.
(function () {
  'use strict';

  var els = {};
  var lastState = null;

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

  function ask(options) {
    // A confirmation whose wording is the point is refused, not downgraded, when
    // the shared dialog is unavailable.
    return window.lumaConfirm ? window.lumaConfirm(options) : Promise.resolve(false);
  }

  // ------------------------------------------------------------ markup
  function build(host) {
    var section = el('div', 'settings-section');
    section.id = 'cfg_remote_section';
    section.appendChild(el('h4', '', 'Remote Access'));

    els.status = el('div', 'small', 'Loading\u2026');
    els.status.id = 'cfg_remote_status';
    els.status.style.margin = '0 0 8px';
    section.appendChild(els.status);

    // One quiet line, shown only while Remote Access is on: Clear Units keeps assigned devices.
    els.protect = el('div', 'small', 'Assigned devices are protected from Clear Units while Remote ' +
                     'Access is enabled.');
    els.protect.id = 'cfg_remote_protect_note';
    els.protect.style.cssText = 'margin:0 0 8px;color:var(--text-muted);';
    els.protect.hidden = true;
    section.appendChild(els.protect);

    // Direct links handed out for an address remote access no longer serves. Advisory, shown
    // only while remote access is serving; names a count and where to go, never a link.
    els.directNotice = el('div', 'small', '');
    els.directNotice.id = 'cfg_remote_direct_notice';
    els.directNotice.style.cssText = 'margin:0 0 8px;color:var(--text);';
    els.directNotice.hidden = true;
    section.appendChild(els.directNotice);

    // First administrator: created here, on this computer, because a remote client
    // must never be able to make itself one.
    els.bootstrap = el('div');
    els.bootstrap.id = 'cfg_remote_bootstrap';
    els.bootstrap.hidden = true;
    var intro = el('div', 'small', 'Create the administrator account first. It signs in with this ' +
                   'username and password, and is created only on this computer.');
    intro.style.margin = '0 0 6px';
    els.bootstrap.appendChild(intro);
    [['cfg_admin_user', 'Username', 'text'], ['cfg_admin_pass', 'Password', 'password'],
     ['cfg_admin_confirm', 'Confirm', 'password']].forEach(function (f) {
      var r = row(f[1], f[0]);
      var input = document.createElement('input');
      input.id = f[0]; input.type = f[2]; input.autocomplete = f[2] === 'text' ? 'username' : 'new-password';
      r.appendChild(input);
      els.bootstrap.appendChild(r);
    });
    els.bootstrapBtn = el('button', 'btn small', 'Create administrator');
    els.bootstrapBtn.type = 'button';
    els.bootstrapBtn.id = 'cfg_admin_create';
    var br = row('');
    br.appendChild(els.bootstrapBtn);
    els.bootstrap.appendChild(br);
    section.appendChild(els.bootstrap);

    // Network and security.
    els.config = el('div');
    els.config.id = 'cfg_remote_config';
    var netRow = row('Network', 'cfg_remote_adapter');
    els.adapter = document.createElement('select');
    els.adapter.id = 'cfg_remote_adapter';
    netRow.appendChild(els.adapter);
    els.config.appendChild(netRow);

    var secRow = row('Security', 'cfg_remote_transport');
    els.transport = document.createElement('select');
    els.transport.id = 'cfg_remote_transport';
    [['http', 'HTTP — not encrypted (default)'], ['https', 'HTTPS — certificate required']].forEach(function (o) {
      var option = el('option', '', o[1]); option.value = o[0]; els.transport.appendChild(option);
      if (o[0] === 'https') els.httpsOption = option;
    });
    secRow.appendChild(els.transport);
    els.config.appendChild(secRow);

    // The honest statement about the connection. Exactly one of these shows.
    els.warning = el('div', 'notice notice-warning', 'Connections are not encrypted. Sign-in details and ' +
                     'routing commands cross the network in clear.');
    els.warning.id = 'cfg_remote_warning';
    els.warning.hidden = true;
    els.note = el('div', 'small', 'Traffic is encrypted. Your browser will warn about a certificate ' +
                  'LumaSuite creates itself; the fingerprint below is what to check.');
    els.note.id = 'cfg_remote_note';
    els.note.style.margin = '4px 0 8px';
    els.config.appendChild(els.warning);
    els.config.appendChild(els.note);

    // Certificate.
    els.cert = el('div');
    els.cert.id = 'cfg_remote_cert';
    els.certStatus = el('div', 'small');
    els.certStatus.id = 'cfg_remote_cert_status';
    els.certStatus.style.margin = '0 0 6px';
    els.cert.appendChild(els.certStatus);
    var certButtons = el('div');
    certButtons.style.cssText = 'display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px;';
    els.certGenerate = el('button', 'btn small secondary', 'Create one for this network');
    els.certGenerate.type = 'button';
    els.certGenerate.id = 'cfg_remote_cert_generate';
    els.certUse = el('button', 'btn small secondary', 'Use my own files');
    els.certUse.type = 'button';
    els.certRemove = el('button', 'btn small secondary', 'Remove certificate');
    els.certRemove.type = 'button';
    [els.certGenerate, els.certUse, els.certRemove].forEach(function (b) { certButtons.appendChild(b); });
    els.cert.appendChild(certButtons);
    els.supplied = el('div');
    els.supplied.hidden = true;
    ['cfg_cert_path', 'cfg_key_path'].forEach(function (id, i) {
      var r = row(i === 0 ? 'Certificate file' : 'Key file', id);
      var input = document.createElement('input');
      input.id = id; input.type = 'text'; input.autocomplete = 'off';
      r.appendChild(input);
      els.supplied.appendChild(r);
    });
    els.certApply = el('button', 'btn small', 'Use these');
    els.certApply.type = 'button';
    var ar = row(''); ar.appendChild(els.certApply);
    els.supplied.appendChild(ar);
    els.cert.appendChild(els.supplied);
    els.config.appendChild(els.cert);

    var actions = el('div', 'modal-row');
    els.toggle = el('button', 'btn', 'Turn on remote access');
    els.toggle.type = 'button';
    els.toggle.id = 'cfg_remote_toggle';
    els.manage = el('a', 'btn secondary', 'Manage accounts');
    els.manage.href = '/admin/users';
    els.manage.id = 'cfg_remote_manage';
    actions.appendChild(els.toggle);
    actions.appendChild(els.manage);
    els.config.appendChild(actions);
    section.appendChild(els.config);

    host.appendChild(section);
    buildBackup(host);
  }

  // ------------------------------------------------------ backup and restore
  //
  // Laid out as OmniSuite's Settings lays it out: one labelled row holding the explanation, two
  // buttons and a status line, and every question asked in the shared dialog. Local only: a backup
  // holds the device credentials, so neither half is reachable from the network, and a remote
  // administrator is shown why instead. The passphrase is typed into the dialog and sent once;
  // nothing keeps it, and it is never logged.
  var BACKUP_MIN_PASSPHRASE = 8;

  function buildBackup(host) {
    var section = el('div', 'settings-section');
    section.id = 'cfg_backup_section';
    section.appendChild(el('h4', '', 'Backup and Restore'));
    var r = row('Backup');
    r.style.alignItems = 'flex-start';
    var body = el('div');
    body.style.cssText = 'flex:1 1 auto;min-width:0;';

    els.backupBlurb = el('div', 'small',
      'Saves your accounts and permissions, device names, display groups, saved video walls, ' +
      'Production Studio settings, remote access settings and device sign-in details to one ' +
      'encrypted file. A Direct Access link already in use keeps working after a restore but cannot ' +
      'be shown again: generate a new one to copy it. Keep the file somewhere other than this computer — a backup that only exists ' +
      'on the machine that failed is not a backup.');
    els.backupBlurb.id = 'cfg_backup_blurb';
    els.backupBlurb.hidden = true;
    els.backupBlurb.style.cssText = 'color:var(--text-muted);margin-bottom:8px;';
    body.appendChild(els.backupBlurb);

    // Hidden and disabled until the SERVER says this browser is the local operator, so a remote
    // administrator never sees the controls even for the moment that answer takes to arrive.
    var buttons = el('div');
    buttons.style.cssText = 'display:flex;gap:8px;flex-wrap:wrap;';
    els.backupExport = el('button', 'btn secondary', 'Export Configuration…');
    els.backupExport.type = 'button'; els.backupExport.id = 'cfg_backup_export';
    els.backupRestore = el('button', 'btn secondary', 'Restore Configuration…');
    els.backupRestore.type = 'button'; els.backupRestore.id = 'cfg_backup_restore';
    [els.backupExport, els.backupRestore].forEach(function (b2) { b2.hidden = true; b2.disabled = true; buttons.appendChild(b2); });
    body.appendChild(buttons);

    els.backupRemote = el('div', 'small',
      'Configuration backup and restore require local access to LumaSuite through 127.0.0.1. ' +
      'Open LumaSuite on the computer it is running on to export or restore a configuration.');
    els.backupRemote.id = 'cfg_backup_remote';
    els.backupRemote.hidden = true;
    els.backupRemote.style.marginTop = '4px';
    body.appendChild(els.backupRemote);

    els.backupStatus = el('div', 'small');
    els.backupStatus.id = 'cfg_backup_status';
    els.backupStatus.style.cssText = 'margin-top:8px;color:var(--text-muted);';
    body.appendChild(els.backupStatus);

    r.appendChild(body);
    section.appendChild(r);
    host.appendChild(section);
    els.backup = section;
  }

  function backupStatus(text, tone) {
    if (!els.backupStatus) return;
    els.backupStatus.textContent = text || '';
    els.backupStatus.style.color = tone === 'error' ? 'var(--state-error-text)'
      : (tone === 'ok' ? 'var(--state-success-text)' : 'var(--text-muted)');
  }

  // In the operator's words, never the name the file stores it under.
  var SECTION_LABELS = {
    accounts: 'User accounts and permissions', device_names: 'Device names', display_groups: 'Display groups',
    device_credentials: 'Device sign-in', remote_access: 'Remote access', video_walls: 'Saved video walls',
    production_studio: 'Production Studio'
  };
  function sectionLabel(name) {
    return SECTION_LABELS[name] || String(name || '').replace(/_/g, ' ');
  }

  // The shared dialog with a password field in it. Never a browser prompt: this is a credential,
  // and a prompt shows it in the clear and cannot carry the sentence explaining what it is for.
  // Resolves to the typed text, or null when the operator cancelled or no dialog is available.
  function askForPassphrase(opts) {
    if (!window.lumaConfirm) { backupStatus('Cannot ask for a passphrase on this page.', 'error'); return Promise.resolve(null); }
    var value = null;
    return window.lumaConfirm({
      title: opts.title, message: opts.message, confirmText: opts.confirmText, danger: !!opts.danger,
      field: {type: 'password', label: opts.label || 'Backup passphrase', placeholder: opts.placeholder || '',
              onValue: function (v) { value = v; }}
    }).then(function (ok) { return ok ? (value || '') : null; });
  }

  function filenameFrom(disposition) {
    var m = /filename="?([^";]+)"?/i.exec(disposition || '');
    return (m && m[1]) || 'LumaSuite-backup.lumabak';
  }

  function exportBackup() {
    return askForPassphrase({
      title: 'Export configuration',
      message: 'Choose a passphrase for this backup. You will need it to restore, and nobody can recover it ' +
               'for you. At least ' + BACKUP_MIN_PASSPHRASE + ' characters — letters, numbers, symbols and ' +
               'spaces are all fine.',
      confirmText: 'Export', placeholder: 'At least ' + BACKUP_MIN_PASSPHRASE + ' characters'
    }).then(function (pass) {
      if (pass === null) return null;
      if (pass.length < BACKUP_MIN_PASSPHRASE) {
        backupStatus('That passphrase is too short. Use at least ' + BACKUP_MIN_PASSPHRASE + ' characters.', 'error');
        return null;
      }
      // Asked once, as OmniSuite does. The passphrase is sent as typed; the server checks its
      // length again, and the key derivation and encryption are unchanged.
      return pass;
    }).then(function (pass) {
      if (pass === null) return;
      backupStatus('Preparing the backup…');
      els.backupExport.disabled = true;
      return fetch('/api/backup/export', {method: 'POST', credentials: 'same-origin',
                                          headers: {'Content-Type': 'application/json'},
                                          body: JSON.stringify({passphrase: pass})}).then(function (res) {
        if (!res.ok) {
          return res.json().catch(function () { return {}; }).then(function (b) {
            throw new Error(b.error || 'The backup could not be created.');
          });
        }
        var name = filenameFrom(res.headers && res.headers.get && res.headers.get('Content-Disposition'));
        return res.blob().then(function (blob) {
          var url = URL.createObjectURL(blob);
          var link = document.createElement('a');
          link.href = url; link.download = name;
          document.body.appendChild(link);
          link.click();
          if (link.remove) link.remove();
          // Revoked later, never on this tick: revoking underneath a download that has not started
          // reading cancels it, which looks exactly like a button that did nothing.
          setTimeout(function () { URL.revokeObjectURL(url); }, 60000);
          backupStatus('Saved ' + name + '. If your browser asked where to put it, the save window may be behind ' +
                       'this one. Keep the file somewhere other than this computer.', 'ok');
        });
      }).catch(function (err) {
        backupStatus(err && err.message ? err.message : 'The backup could not be created.', 'error');
      }).then(function () { els.backupExport.disabled = false; });
    });
  }

  function chooseBackupFile() {
    return new Promise(function (resolve) {
      var input = document.createElement('input');
      input.type = 'file';
      input.setAttribute('accept', '.lumabak');
      input.addEventListener('change', function () { resolve(input.files && input.files[0] ? input.files[0] : null); });
      input.click();
    });
  }

  function postBackup(path, file, passphrase) {
    var form = new FormData();
    form.append('backup', file);
    form.append('passphrase', passphrase);
    return fetch(path, {method: 'POST', credentials: 'same-origin', body: form}).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (b) { return {ok: res.ok && b.ok !== false, body: b}; });
    }).catch(function () { return {ok: false, body: {error: 'LumaSuite could not be reached.'}}; });
  }

  function restoreBackup() {
    return chooseBackupFile().then(function (file) {
      if (!file) return null;
      return askForPassphrase({
        title: 'Restore from ' + file.name,
        message: 'Enter the passphrase this backup was created with. Nothing is changed until you have seen what ' +
                 'it contains and confirmed.',
        confirmText: 'Open backup', placeholder: 'Backup passphrase'
      }).then(function (pass) {
        if (pass === null) return null;
        backupStatus('Checking the backup…');
        els.backupRestore.disabled = true;
        // Step one: decrypt and validate, changing nothing.
        return postBackup('/api/backup/inspect', file, pass).then(function (inspected) {
          if (!inspected.ok) { backupStatus(inspected.body.error || 'That backup could not be opened.', 'error'); return null; }
          var summary = inspected.body.summary || {};
          var rows = (summary.sections || []).map(function (sec) { return {label: sectionLabel(sec.name), value: sec.detail || 'included'}; });
          rows.push({label: 'Devices remembered', value: String(summary.devices || 0)});
          var accounts = summary.accounts || {};
          if ((accounts.removed || []).length) {
            rows.push({label: 'Accounts removed', value: accounts.removed.join(', ') +
              ' (signed out; any Direct Access link stops working)'});
          }
          if ((accounts.replaced || []).length) {
            rows.push({label: 'Accounts replaced', value: accounts.replaced.join(', ') +
              ' (the backup\'s password, role, permissions and link take over; anyone signed in as one of them is signed out)'});
          }
          (summary.notes || []).forEach(function (n) { rows.push({label: 'Note', value: String(n)}); });
          if ((summary.machine_specific || []).length) {
            rows.push({label: 'Not copied', value: 'Settings that belong to the other computer (network adapter, folders, certificate). This computer keeps its own.'});
          }
          // Step two: show what is in it, and what will not survive the move.
          return ask({
            title: 'Replace this configuration?',
            message: 'This replaces the accounts, permissions, device names, display groups and settings on this ' +
                     'computer with the ones in the backup (made ' + (summary.created_at || 'earlier') + '). ' +
                     ((accounts.removed || []).length
                       ? (accounts.removed.length === 1 ? '1 account' : accounts.removed.length + ' accounts') +
                         ' on this computer will be removed. ' : '') +
                     'A copy of the current configuration is saved first and put back automatically if ' +
                     'anything goes wrong.',
            summary: rows, confirmText: 'Restore', cancelText: 'Cancel', danger: true
          }).then(function (yes) {
            if (!yes) { backupStatus('Nothing was changed.'); return null; }
            // Step three: apply.
            backupStatus('Restoring…');
            return postBackup('/api/backup/restore', file, pass);
          });
        });
      }).then(function (result) {
        els.backupRestore.disabled = false;
        if (!result) return;
        if (!result.ok) { backupStatus(result.body.error || 'The restore did not complete.', 'error'); return; }
        backupStatus('Backup restored.', 'ok');
        var note = result.body.admin_recovery && result.body.admin_recovery.note;
        // A warning is a part of the restore that did not complete (the device details could not be
        // saved): said plainly, never folded into "restored".
        var warning = result.body.warning;
        ask({title: 'Restore complete',
             message: 'The configuration was restored. Run a Scan so LumaSuite finds the equipment again.' + (warning ? '\n\n' + warning : '') + (note ? '\n\n' + note : ''),
             confirmText: 'Close', cancelText: null});
        load();
      });
    }).catch(function () { backupStatus('The restore did not complete.', 'error'); els.backupRestore.disabled = false; });
  }

  // ------------------------------------------------------------- state
  function fillAdapters(state) {
    var previous = els.adapter.value;
    els.adapter.replaceChildren();
    var adapters = state.adapters || [];
    if (!adapters.length) {
      var none = el('option', '', 'No suitable network found');
      none.value = '';
      els.adapter.appendChild(none);
      els.adapter.disabled = true;
      return;
    }
    adapters.forEach(function (a) {
      var option = el('option', '', a.name + ' — ' + a.address);
      option.value = a.id;
      option.setAttribute('data-address', a.address);
      els.adapter.appendChild(option);
    });
    var wanted = [previous, state.configured_adapter].filter(Boolean)
      .filter(function (id) { return adapters.some(function (a) { return a.id === id; }); })[0];
    els.adapter.value = wanted || adapters[0].id;
    // Not while it is serving: changing the network under a running listener is a
    // decision, so turn it off first.
    els.adapter.disabled = !!state.enabled;
  }

  // What the certificate actually is, decided once from the server's description of it and never
  // from which Security option is selected. 'Active' means LumaSuite has a usable certificate to
  // serve HTTPS with; it says nothing about whether a browser trusts whoever issued it.
  //   self_signed  the certificate LumaSuite generated
  //   custom       a certificate the administrator supplied
  //   required     missing, unreadable, expired, or made for another network
  function certificateKind(c) {
    if (!c || !c.present || !c.readable || c.expired) return 'required';
    // Asked of the same loader the listener uses, so "active" cannot be claimed for a
    // certificate whose key is missing or does not match.
    if (c.usable === false) return 'required';
    var supplied = c.generated === false;
    // A supplied certificate is never judged against the adapter: it may carry a name
    // LumaSuite has no way of knowing about, and the server never blocks on it either.
    if (!supplied && c.for_address && c.matches_address === false) return 'required';
    return supplied ? 'custom' : 'self_signed';
  }

  // WHY a certificate is not usable, as the one decision the page can offer for it.
  //   missing    nothing exists                       -> Create Certificate
  //   mismatch   LumaSuite's own, for another address -> Regenerate Certificate
  //   unusable   LumaSuite's own, unreadable/expired  -> Regenerate Certificate
  //   supplied   the site's own, whatever is wrong    -> never replaced; HTTP or Cancel
  // null when the certificate is usable.
  function certificateReason(c) {
    if (!c || !c.present) return 'The certificate file is missing.';
    if (!c.readable) return c.error || 'The certificate file could not be read.';
    if (c.expired) return 'The certificate has expired.';
    if (c.usable === false) return c.unusable_reason || 'The certificate cannot be used.';
    return '';
  }

  function certificateProblem(c) {
    if (certificateKind(c) !== 'required') return null;
    if (c && c.generated === false) {
      return {kind: 'supplied', reason: certificateReason(c) || 'It cannot be used.'};
    }
    if (!c || !c.present) return {kind: 'missing', reason: ''};
    var addressOnly = c.readable && c.usable !== false && !c.expired && c.for_address &&
      c.matches_address === false;
    if (addressOnly) return {kind: 'mismatch', reason: ''};
    return {kind: 'unusable', reason: certificateReason(c)};
  }

  function httpsLabel(c) {
    var kind = certificateKind(c);
    if (kind === 'self_signed') return 'HTTPS — encrypted · certificate active';
    if (kind === 'custom') return 'HTTPS — encrypted · custom certificate active';
    return 'HTTPS — certificate required';
  }

  var REQUIRED_GUIDANCE = 'HTTPS certificate is missing or invalid. Turn off Remote Access, then create ' +
    'or install a certificate for this network before enabling HTTPS.';

  function certificateWords(c, state) {
    if (!c) return '';
    var kind = certificateKind(c);
    if (kind === 'required') {
      var why = '';
      if (c.present && !c.readable) why = 'The certificate file could not be read. ';
      else if (c.present && c.expired) why = 'The certificate has expired. ';
      else if (c.present && c.usable === false) why = (c.unusable_reason || 'The certificate cannot be used.') + ' ';
      else if (c.present && c.for_address && c.matches_address === false) {
        why = 'The certificate does not cover ' + c.for_address + '. ';
      }
      return why + REQUIRED_GUIDANCE;
    }
    var where = c.for_address || c.subject;
    var text = (kind === 'custom' ? 'Custom certificate active' : 'Self-signed certificate active') +
      (where ? ' for ' + where : '') +
      (typeof c.days_remaining === 'number' ? ' (' + c.days_remaining + ' days left)' : '') + '.';
    if (kind === 'self_signed') text += ' Your browser may warn because this certificate is self-signed.';
    if (c.fingerprint) text += ' Fingerprint: ' + String(c.fingerprint).slice(0, 23) + '…';
    return text;
  }

  function paint(state) {
    lastState = state;
    var saved = !!state.configured;
    var https = (state.transport || 'http') === 'https';
    var enabled = !!state.enabled;

    // Status, in a sentence.
    var text;
    if (!state.has_admin) text = 'Off — an administrator account is needed first.';
    else if (enabled && state.address_moved && state.address_moved.message) text = 'ON \u2014 ' + state.address_moved.message;
    else if (enabled && state.encrypted) text = 'ON — other devices on this network can reach LumaSuite securely at ' + state.url;
    else if (enabled) text = 'ON — not encrypted, at ' + state.url;
    else if (saved && !state.configured_available) text = 'Configured for ' + (state.configured_adapter_name || 'a network') +
        ', which is not available right now.';
    else text = 'Off — LumaSuite is only reachable on this computer.';
    if (state.restore_error) text += ' ' + state.restore_error;
    if (enabled && typeof state.sessions === 'number') text += ' · ' + state.sessions + ' signed in';
    els.status.textContent = text;
    els.protect.hidden = !enabled;
    var stale = (state.direct_links_stale || []).length;
    els.directNotice.hidden = !(enabled && stale);
    els.directNotice.textContent = stale
      ? (stale === 1 ? 'One direct link was' : stale + ' direct links were') +
        ' handed out for an earlier address. The links themselves still work at the new address, but ' +
        'anyone holding the old one needs the new one. Open Users, edit the account and use Copy link.'
      : '';

    els.bootstrap.hidden = !!state.has_admin || !state.local;
    els.config.hidden = !state.has_admin;

    fillAdapters(state);
    els.transport.value = https ? 'https' : 'http';
    els.transport.disabled = enabled;
    if (els.httpsOption) els.httpsOption.textContent = httpsLabel(state.certificate);

    // Exactly one transport statement; the hidden attribute must beat any class
    // that sets display.
    var plaintext = enabled ? !state.encrypted : !https;
    els.warning.hidden = !plaintext;
    els.note.hidden = plaintext;
    // The certificate belongs to HTTPS. Remote Access that deliberately uses HTTP has none to describe, and
    // showing this row there said "HTTPS certificate is missing or invalid" under Security for a setup that
    // never asked for one. Same rule as OmniSuite's Settings: choosing HTTP hides the certificate row.
    els.cert.hidden = !https;

    els.certStatus.textContent = certificateWords(state.certificate, state);
    els.certGenerate.disabled = !(state.certificate && state.certificate.can_generate) || enabled;
    // With a LumaSuite-made certificate already there, this button replaces it, so it says so.
    els.certGenerate.textContent = replacesCertificate(state.certificate) ? 'Regenerate certificate' : 'Create one for this network';
    els.certUse.disabled = enabled;
    els.certRemove.disabled = enabled || !(state.certificate && state.certificate.present);
    els.toggle.textContent = enabled ? 'Turn off remote access' : 'Turn on remote access';
    els.toggle.disabled = !enabled && !els.adapter.value;
  }

  function load() {
    return Promise.all([api('/api/remote_access'),
                        window.lumaSession ? window.lumaSession() : Promise.resolve({})])
      .then(function (r) {
        if (!r[0].ok) { els.status.textContent = 'Remote access settings could not be read.'; return; }
        var state = r[0].body;
        state.local = !!(r[1] && r[1].local);
        paint(state);
        applyLocality(state.local);
      });
  }

  // What only the computer LumaSuite runs on can do: reveal a device password,
  // and browse that computer's folders. A remote administrator is not offered
  // either, and is told why, rather than shown controls that always fail.
  function applyLocality(isLocal) {
    ['cfg_pw_toggle', 'cfg_fallback_pw_toggle', 'cfg_firmware_browse',
     'cfg_imagestream_browse', 'cfg_logos_browse'].forEach(function (id) {
      var node = $(id);
      if (node) node.hidden = !isLocal;
    });
    // The section stays; only its controls follow where the browser is. Remote, the buttons are
    // withdrawn and the reason is shown, which is better than a control that could only fail.
    [els.backupExport, els.backupRestore].forEach(function (b2) { if (b2) { b2.hidden = !isLocal; b2.disabled = !isLocal; } });
    if (els.backupBlurb) els.backupBlurb.hidden = !isLocal;
    if (els.backupRemote) els.backupRemote.hidden = !!isLocal;
    if (!isLocal) backupStatus('');
    var note = $('cfg_remote_local_note');
    if (!note) {
      note = el('div', 'small', 'Showing device passwords, choosing folders on the host computer, ' +
                'creating the first administrator and backups work only on the computer LumaSuite ' +
                'runs on.');
      note.id = 'cfg_remote_local_note';
      note.style.cssText = 'margin:6px 0;color:var(--text-muted);';
      els.status.parentNode.insertBefore(note, els.status.nextSibling);
    }
    note.hidden = !!isLocal;
  }

  // ------------------------------------------------------------- actions
  function createAdmin() {
    var username = $('cfg_admin_user').value.trim();
    var password = $('cfg_admin_pass').value;
    var confirmValue = $('cfg_admin_confirm').value;
    els.bootstrapBtn.disabled = true;
    api('/api/bootstrap/admin', {method: 'POST',
        body: {username: username, password: password, confirm: confirmValue}}).then(function (r) {
      els.bootstrapBtn.disabled = false;
      if (!r.ok) { say(r.body.error || 'That did not work.', 'error'); return; }
      $('cfg_admin_pass').value = ''; $('cfg_admin_confirm').value = '';
      say('Administrator created', 'ok');
      load();
    });
  }

  // HTTPS with no usable certificate is a decision, not a failure to report afterwards. Three
  // honest answers, none of them taken for the operator: create (or regenerate) a certificate,
  // carry on over HTTP (which then asks its own plaintext question), or change nothing. Cancel
  // is only ever Cancel. A certificate the site supplied is never offered a replacement.
  function certificateDecision(problem, c) {
    var NOTHING = ' Nothing has been turned on.';
    var HTTP = 'switch to HTTP, which does not encrypt sign-in details';
    var FINGERPRINT = ' Regenerating changes its fingerprint, so browsers that accepted the old ' +
      'one will warn again.';
    var option = els.adapter.options[els.adapter.selectedIndex];
    var address = (c && c.for_address) || (option ? option.getAttribute('data-address') : '') || '';
    var names = (c && c.san_ips && c.san_ips.length ? c.san_ips.join(', ') : '') || (c && c.subject) || 'another address';
    if (problem.kind === 'supplied') {
      return ask({title: 'The certificate you supplied cannot be used',
        message: problem.reason + ' LumaSuite never replaces a certificate you supplied. Fix or remove ' +
          'it under Certificate in Settings, or ' + HTTP + '.' + NOTHING,
        confirmText: 'Change to HTTP', cancelText: 'Cancel', danger: true}).then(function (yes) {
        return yes === true ? 'http' : 'cancel';
      });
    }
    var dialog;
    if (problem.kind === 'mismatch') {
      dialog = {title: 'The certificate is for another network',
        message: 'The certificate LumaSuite created is for ' + names + ', not for ' +
          (address || 'this network') + '. Regenerate it for this network, or ' + HTTP + '.' +
          FINGERPRINT + NOTHING,
        confirmText: 'Regenerate Certificate'};
    } else if (problem.kind === 'unusable') {
      dialog = {title: 'The certificate cannot be used',
        message: problem.reason + ' Regenerate the certificate LumaSuite created, or ' + HTTP + '.' +
          FINGERPRINT + NOTHING,
        confirmText: 'Regenerate Certificate'};
    } else {
      dialog = {title: 'HTTPS needs a certificate',
        message: 'Remote access is set to HTTPS, but there is no usable certificate for this ' +
          'network. Create one now, or ' + HTTP + '.' + NOTHING,
        confirmText: 'Create Certificate'};
    }
    // Cancel takes the focus: neither costly answer is made by pressing Enter.
    dialog.cancelText = 'Cancel';
    dialog.secondary = {text: 'Change to HTTP', value: 'http'};
    dialog.danger = true;
    return ask(dialog).then(function (choice) {
      if (choice === 'http') return 'http';
      if (choice === true) return 'create';
      return 'cancel';
    });
  }

  // Resolves once the enable workflow has run to its end (or been stopped by the operator).
  // Create / Regenerate performs the certificate operation and the workflow CONTINUES to the
  // one meaningful question, "Turn on remote access?" -- it is not a dead end, and the
  // certificate being made never turns anything on by itself.
  function turnOn() {
    var cert = lastState && lastState.certificate;
    var problem = els.transport.value === 'https' ? certificateProblem(cert) : null;
    if (!problem) return turnOnConfirmed();
    return certificateDecision(problem, cert).then(function (choice) {
      if (choice === 'http') { els.transport.value = 'http'; return turnOnConfirmed(); }
      if (choice === 'create') {
        return generateCertificate({confirmed: true}).then(function (made) {
          // Only a certificate that was actually made is a go-ahead.
          return made === false ? null : turnOnConfirmed();
        });
      }
      return null;
    });
  }

  function turnOnConfirmed() {
    var transport = els.transport.value;
    var option = els.adapter.options[els.adapter.selectedIndex];
    var summary = [{label: 'Network', value: option ? option.textContent : ''},
                   {label: 'Security', value: transport === 'https' ? 'HTTPS (encrypted)' : 'HTTP (not encrypted)'}];
    return ask({title: 'Turn on remote access?',
      message: transport === 'https'
        ? 'Other devices on this network will be able to reach LumaSuite and sign in.'
        : 'Other devices on this network will be able to reach LumaSuite, and sign-in details will cross ' +
          'the network in clear.',
      summary: summary, confirmText: 'Turn on', danger: transport === 'http'}).then(function (yes) {
      if (!yes) return;
      return api('/api/remote_access', {method: 'POST', body: {
        enabled: true, adapter_id: els.adapter.value,
        address: option ? option.getAttribute('data-address') : '',
        transport: transport, accept_insecure: transport === 'http'}}).then(function (r) {
        if (!r.ok) { say(r.body.error || 'That did not work.', 'error'); }
        else say('Remote access is on', 'ok');
        if (window.lumaRefreshNav) window.lumaRefreshNav();
        return load();
      });
    });
  }

  function turnOff() {
    return ask({title: 'Turn off remote access',
      message: 'Everyone signed in from another device will be signed out.',
      confirmText: 'Turn off', danger: true}).then(function (yes) {
      if (!yes) return;
      return api('/api/remote_access', {method: 'POST', body: {enabled: false}}).then(function (r) {
        if (!r.ok) say(r.body.error || 'That did not work.', 'error'); else say('Remote access is off', 'ok');
        if (window.lumaRefreshNav) window.lumaRefreshNav();
        return load();
      });
    });
  }

  function certAction(body, done) {
    return api('/api/admin/certificate', {method: 'POST', body: body}).then(function (r) {
      // Resolves whether the operation worked, so a flow that depends on it (turning
      // Remote Access on after a certificate was made) can tell a made certificate from a
      // refused one.
      if (!r.ok) { say(r.body.error || 'That did not work.', 'error'); return load().then(function () { return false; }); }
      if (done) say(done, 'ok');
      return load().then(function () { return true; });
    });
  }

  // A certificate LumaSuite made is present (readable or not): creating again replaces it and
  // changes the fingerprint. A supplied (custom) certificate is never replaced from here.
  function replacesCertificate(c) {
    return !!(c && c.present && c.generated !== false);
  }

  function shortPrint(value) { return value ? String(value).slice(0, 23) + '…' : ''; }

  // `opts.confirmed` is passed by the decision offered when Remote Access is turned on: the
  // operator has just chosen Create or Regenerate in a dialog that said what it does, so
  // asking the same question again would be a second confirmation of one action. The button
  // next to the certificate keeps its own question.
  function generateCertificate(opts) {
    var confirmed = !!(opts && opts.confirmed);
    var option = els.adapter.options[els.adapter.selectedIndex];
    var address = option ? option.getAttribute('data-address') : '';
    if (!address) { say('Choose a network first.', 'error'); return Promise.resolve(); }
    var current = lastState && lastState.certificate;
    // A supplied certificate is never replaced from here, and the server refuses too; saying
    // so first spares a question that cannot lead anywhere.
    if (current && current.present && current.generated === false) {
      say('A certificate you supplied is in use. Remove it first if you want LumaSuite to create one.', 'error');
      return Promise.resolve(false);
    }
    if (!replacesCertificate(current)) {
      return certAction({action: 'generate', address: address}, 'Certificate created');
    }
    if (confirmed) {
      return api('/api/admin/certificate', {method: 'POST', body: {action: 'generate', address: address}}).then(function (r) {
        if (!r.ok) { say(r.body.error || 'That did not work.', 'error'); return load().then(function () { return false; }); }
        var next = r.body.certificate && r.body.certificate.fingerprint;
        say('Certificate regenerated. Fingerprint changed from ' + shortPrint(r.body.previous_fingerprint) +
          ' to ' + shortPrint(next) + '.', 'ok');
        return load().then(function () { return true; });
      });
    }
    // Never automatic: replacing the certificate changes the fingerprint, so browsers will warn
    // again and anyone who compared the old fingerprint has to compare the new one.
    return ask({title: 'Regenerate certificate',
      message: 'This replaces the certificate LumaSuite created' +
        (current.fingerprint ? ' (fingerprint ' + shortPrint(current.fingerprint) + ')' : '') +
        ' with a new one for ' + address + '. The fingerprint will change and browsers will warn ' +
        'again until the new certificate is accepted. A certificate you supplied is not affected.',
      confirmText: 'Regenerate', danger: true}).then(function (yes) {
      if (!yes) return null;
      return api('/api/admin/certificate', {method: 'POST', body: {action: 'generate', address: address}}).then(function (r) {
        if (!r.ok) { say(r.body.error || 'That did not work.', 'error'); return load(); }
        var next = r.body.certificate && r.body.certificate.fingerprint;
        say('Certificate regenerated. Fingerprint changed from ' + shortPrint(r.body.previous_fingerprint) +
          ' to ' + shortPrint(next) + '.', 'ok');
        return load();
      });
    });
  }

  function removeCertificate() {
    ask({title: 'Remove certificate',
      message: 'Remote access cannot use HTTPS until there is a certificate again.',
      confirmText: 'Remove', danger: true}).then(function (yes) {
      if (yes) certAction({action: 'remove'}, 'Certificate removed');
    });
  }

  function wire() {
    els.bootstrapBtn.addEventListener('click', createAdmin);
    els.toggle.addEventListener('click', function () {
      if (lastState && lastState.enabled) turnOff(); else turnOn();
    });
    els.transport.addEventListener('change', function () {
      if (lastState) { lastState.transport = els.transport.value; paint(lastState); }
    });
    els.certGenerate.addEventListener('click', generateCertificate);
    els.certUse.addEventListener('click', function () { els.supplied.hidden = !els.supplied.hidden; });
    els.certApply.addEventListener('click', function () {
      certAction({action: 'use', cert_path: $('cfg_cert_path').value.trim(),
                  key_path: $('cfg_key_path').value.trim()}, 'Certificate chosen');
    });
    els.certRemove.addEventListener('click', removeCertificate);
    els.backupExport.addEventListener('click', exportBackup);
    els.backupRestore.addEventListener('click', restoreBackup);
    document.addEventListener('luma:settings-open', load);
  }

  // Whether the dialog this section lives in is already open. The script is loaded after
  // Settings is built, so the dialog can be opened before this exists, and the "opened" event
  // that would have loaded the section has then already come and gone.
  function insideOpenDialog(node) {
    for (var n = node; n; n = n.parentNode) {
      if (n.classList && n.classList.contains('open')) return true;
    }
    return false;
  }

  function init() {
    var host = $('cfg_extension_point');
    if (!host || $('cfg_remote_section')) return;       // built once, never duplicated
    build(host);
    wire();
    if (insideOpenDialog(host)) load();
    if (window.lumaSession) {
      window.lumaSession().then(function (s) { applyLocality(!!(s && s.local)); });
    }
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
}());
