// Accounts, device names, display groups and activity.
//
// Everything here is presentation over the server's admin API. Permissions are
// stored by stable device identity (a hardware address), never by name or by
// network address, so renaming or re-addressing a device never changes who may
// route it. All text from the server is inserted with textContent.
(function () {
  'use strict';

  var state = {users: [], devices: [], groups: [], walls: [], editing: null, groupEditing: null,
               direct: {link: '', enabled: false}, expanded: {}};

  // ---------------------------------------------------------------- helpers
  function $(id) { return document.getElementById(id); }
  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = String(text);
    return node;
  }
  function say(message, kind) { if (window.toast) window.toast(message, kind); }
  function plural(n, one, many) { return n + ' ' + (n === 1 ? one : many); }

  function api(path, options) {
    options = options || {};
    var init = {method: options.method || 'GET', credentials: 'same-origin',
                headers: {'Accept': 'application/json'}};
    if (options.body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(options.body);
    }
    return fetch(path, init).then(function (res) {
      if (res.status === 401) { location.replace('/login?next=' + encodeURIComponent('/admin/users')); }
      return res.json().catch(function () { return {}; }).then(function (body) {
        return {ok: res.ok && body.ok !== false, status: res.status, body: body};
      });
    });
  }

  function confirmAsk(options) {
    // A decision whose wording is the point: if the shared dialog is missing, the
    // action is refused rather than falling back to a browser string box.
    if (!window.lumaConfirm) return Promise.resolve(false);
    return window.lumaConfirm(options);
  }

  function openModal(id) {
    var node = $(id);
    node._opener = document.activeElement;
    node.classList.add('open');
  }
  function closeModal(id) {
    var node = $(id);
    node.classList.remove('open');
    var back = node._opener;
    node._opener = null;
    if (back && back.focus) { try { back.focus(); } catch (e) { /* ignore */ } }
  }

  // --------------------------------------------------------------- tabs
  var panels = ['panel_accounts', 'panel_devices', 'panel_groups', 'panel_activity'];
  function selectTab(panelId) {
    panels.forEach(function (id) { $(id).hidden = id !== panelId; });
    Array.prototype.forEach.call($('admin_tabs').querySelectorAll('button'), function (b) {
      b.setAttribute('aria-selected', b.getAttribute('data-panel') === panelId ? 'true' : 'false');
    });
    if (panelId === 'panel_activity') loadAudit();
    if (panelId === 'panel_devices') renderDevices();
  }

  // ---------------------------------------------------------- loading
  function loadAll() {
    return Promise.all([api('/api/admin/users'), api('/api/admin/devices'),
                        api('/api/admin/groups'), api('/api/admin/walls')]).then(function (r) {
      state.users = r[0].body.users || [];
      state.devices = r[1].body.devices || [];
      state.dupes = r[1].body.duplicate_aliases || [];
      state.groups = r[2].body.groups || [];
      state.walls = (r[3] && r[3].body && r[3].body.walls) || [];
      renderUsers();
      renderDevices();
      renderGroups();
    });
  }

  // ------------------------------------------------------ accounts table
  function countsText(user) {
    if (user.role === 'admin') return 'Everything (administrator)';
    var p = user.permissions || {};
    return plural((p.sources || []).length, 'source', 'sources') + ' · ' +
           plural((p.destinations || []).length, 'display', 'displays') + ' · ' +
           plural((p.groups || []).length, 'group', 'groups') +
           ((p.walls || []).length ? ' · ' + plural(p.walls.length, 'wall', 'walls') : '') +
           ' · ' + (p.matrix_routing === true
                    ? (p.standard_routing === false ? 'Matrix only' : 'Standard + Matrix')
                    : 'Standard');
  }

  function renderUsers() {
    var body = $('user_table').querySelector('tbody');
    body.replaceChildren();
    $('user_empty').hidden = state.users.length > 0;
    var enabledAdmins = state.users.filter(function (u) { return u.role === 'admin' && u.enabled; }).length;
    state.users.forEach(function (user) {
      var row = el('tr');
      row.appendChild(el('td', '', user.username));
      row.appendChild(el('td', '', user.role === 'admin' ? 'Administrator' : 'User'));
      var status = el('td');
      status.appendChild(el('span', 'pill ' + (user.enabled ? 'on' : 'off'), user.enabled ? 'Enabled' : 'Disabled'));
      row.appendChild(status);
      row.appendChild(el('td', '', countsText(user)));
      var actions = el('td', 'actions');
      var lastAdmin = user.role === 'admin' && user.enabled && enabledAdmins <= 1;

      var edit = el('button', 'secondary', 'Edit');
      edit.type = 'button';
      edit.addEventListener('click', function () { openUserEditor(user); });
      var toggle = el('button', 'secondary', user.enabled ? 'Disable' : 'Enable');
      toggle.type = 'button';
      toggle.disabled = lastAdmin && user.enabled;
      toggle.addEventListener('click', function () { act(user, 'set_enabled', {enabled: !user.enabled}); });
      var del = el('button', 'danger', 'Delete');
      del.type = 'button';
      del.disabled = lastAdmin;
      del.addEventListener('click', function () {
        confirmAsk({title: 'Delete account', message: 'Delete ' + user.username + '? They will be signed out and ' +
                    'cannot sign in again.', confirmText: 'Delete', danger: true}).then(function (yes) {
          if (yes) act(user, 'delete', {});
        });
      });
      actions.appendChild(edit); actions.appendChild(toggle); actions.appendChild(del);
      row.appendChild(actions);
      body.appendChild(row);
    });
  }

  function act(user, action, extra) {
    return api('/api/admin/users/' + encodeURIComponent(user.id),
               {method: 'POST', body: Object.assign({action: action}, extra || {})}).then(function (r) {
      if (!r.ok) { say(r.body.error || 'That did not work.', 'error'); return r; }
      return loadAll().then(function () { return r; });
    });
  }

  // -------------------------------------------------------- checklists
  function checklist(rootId, items, selected, subText) {
    var root = $(rootId);
    var list = root.querySelector('ul');
    var search = root.querySelector('.search');
    var chosen = new Set(selected || []);
    function count() { root.querySelector('.count').textContent = chosen.size + ' selected'; }
    function paint() {
      var needle = (search.value || '').toLowerCase();
      list.replaceChildren();
      items.forEach(function (item) {
        var hay = (item.name + ' ' + (subText ? subText(item) : '')).toLowerCase();
        if (needle && hay.indexOf(needle) < 0) return;
        var li = el('li');
        var label = el('label');
        var box = document.createElement('input');
        box.type = 'checkbox';
        box.checked = chosen.has(item.id);
        box.addEventListener('change', function () {
          if (box.checked) chosen.add(item.id); else chosen.delete(item.id);
          count();
        });
        label.appendChild(box);
        label.appendChild(el('span', '', item.name));
        var sub = subText ? subText(item) : '';
        if (sub) label.appendChild(el('span', 'sub', sub));
        li.appendChild(label);
        list.appendChild(li);
      });
      if (!list.children.length) list.appendChild(el('li', 'muted', 'Nothing to show.'));
    }
    search.value = '';
    search.oninput = paint;
    root.querySelector('[data-all]').onclick = function () {
      items.forEach(function (i) { chosen.add(i.id); }); paint(); count();
    };
    root.querySelector('[data-none]').onclick = function () { chosen.clear(); paint(); count(); };
    paint(); count();
    return {values: function () { return Array.from(chosen); }};
  }

  function devicesOf(role) {
    return state.devices.filter(function (d) { return d.role === role; });
  }
  function deviceSub(d) {
    var parts = [];
    if (d.location) parts.push(d.location);
    else if (d.description) parts.push(d.description);
    if (d.alias && d.device_name && d.alias !== d.device_name) parts.push(d.device_name);
    return parts.join(' · ');
  }

  // ------------------------------------------------------ account editor
  var pick = {};

  function openUserEditor(user) {
    state.editing = user || null;
    var creating = !user;
    $('user_modal_title').textContent = creating ? 'New account' : 'Edit ' + user.username;
    $('u_name').value = creating ? '' : user.username;
    $('u_name').readOnly = !creating;
    $('u_role').value = creating ? 'user' : user.role;
    $('u_password').value = '';
    $('u_password_label').textContent = creating ? 'Password' : 'New password';
    $('u_password').placeholder = creating ? 'At least 8 characters' : 'Leave blank to keep';
    $('u_error').textContent = '';
    var perms = (user && user.permissions) || {};
    // An account without these keys predates them: Standard on, Matrix off.
    $('u_standard').checked = perms.standard_routing !== false;
    $('u_matrix').checked = perms.matrix_routing === true;
    pick.sources = checklist('assign_sources', devicesOf('encoder'), perms.sources, deviceSub);
    pick.displays = checklist('assign_displays', devicesOf('decoder'), perms.destinations, deviceSub);
    pick.groups = checklist('assign_groups',
      state.groups.map(function (g) { return {id: g.id, name: g.name}; }), perms.groups,
      function (g) { var full = state.groups.filter(function (x) { return x.id === g.id; })[0];
                     return full ? plural(full.members.length, 'display', 'displays') : ''; });
    // Saved walls are a category of their own: granting one does not grant the
    // displays it is made of, and only walls that have been saved can be chosen.
    pick.walls = checklist('assign_walls',
      state.walls.map(function (w) { return {id: w.id, name: w.name}; }), perms.walls,
      function (w) { var full = state.walls.filter(function (x) { return x.id === w.id; })[0];
                     return full ? plural(full.members, 'display', 'displays') : ''; });
    syncRoleSections();
    loadDirect();
    openModal('user_modal');
    $(creating ? 'u_name' : 'u_password').focus();
  }

  function syncRoleSections() {
    $('user_only').hidden = $('u_role').value === 'admin';
    $('direct_section').hidden = !state.editing || $('u_role').value === 'admin';
  }

  function saveUser() {
    var creating = !state.editing;
    var name = $('u_name').value.trim();
    var role = $('u_role').value;
    var password = $('u_password').value;
    var permissions = {sources: pick.sources.values(), destinations: pick.displays.values(),
                       groups: pick.groups.values(), walls: pick.walls.values(),
                       standard_routing: $('u_standard').checked,
                       matrix_routing: $('u_matrix').checked};
    $('u_error').textContent = '';
    if (role !== 'admin' && !permissions.standard_routing && !permissions.matrix_routing) {
      $('u_error').textContent = 'Choose at least one routing interface.';
      return;
    }
    $('u_save').disabled = true;
    var chain;
    if (creating) {
      chain = api('/api/admin/users', {method: 'POST',
        body: {username: name, password: password, role: role, permissions: permissions}});
    } else {
      var user = state.editing;
      chain = Promise.resolve({ok: true, body: {}});
      if (role !== user.role) {
        chain = chain.then(function () { return api('/api/admin/users/' + encodeURIComponent(user.id),
          {method: 'POST', body: {action: 'set_role', role: role}}); });
      }
      if (password) {
        chain = chain.then(function (r) { return !r.ok ? r : api('/api/admin/users/' + encodeURIComponent(user.id),
          {method: 'POST', body: {action: 'set_password', password: password}}); });
      }
      if (role !== 'admin') {
        chain = chain.then(function (r) { return !r.ok ? r : api('/api/admin/users/' + encodeURIComponent(user.id),
          {method: 'POST', body: {action: 'set_permissions', permissions: permissions}}); });
      }
    }
    chain.then(function (r) {
      $('u_save').disabled = false;
      if (!r.ok) { $('u_error').textContent = r.body.error || 'That did not work.'; return; }
      closeModal('user_modal');
      say(creating ? 'Account created' : 'Account saved', 'ok');
      return loadAll();
    });
  }

  // ----------------------------------------------------- direct access
  function paintDirect(body) {
    var enabled = !!(body.direct_access && body.direct_access.enabled);
    $('direct_enabled').checked = enabled;
    $('direct_link_box').hidden = !enabled;
    $('direct_link').value = body.link || '';
    var moved = body.address_moved && body.link ? body.address_moved : null;
    $('direct_note').textContent = moved
      ? moved.message + ' The link above is for ' + moved.from + ', where it works until Remote Access is moved; it changes to ' +
        moved.to + ' when it is.'
      : body.link ? (body.note || '')
      : (enabled ? (body.note || 'The link cannot be shown on this computer. Regenerate it to get a new one.') : '');
    $('direct_copy').disabled = !body.link;
    // The address remote access serves changed after this link was handed out. Said in words and
    // kept until acknowledged: the notice is dismissible, the fact that old copies of the link
    // point elsewhere is not, which is why acknowledging records the address rather than hiding a box.
    var changed = !!(body.address_changed && body.link);
    $('direct_address').hidden = !changed;
    if (changed) $('direct_address_text').textContent = directAddressText(body.issued_origin, body.link_origin);
  }

  function directAddressText(oldOrigin, newOrigin) {
    return 'This link was last handed out for ' + oldOrigin + '. Remote access now serves ' + newOrigin +
      ', so the link shown here is already the new address. The code in it has not changed: it works at ' +
      'the new address, and nobody can reach the old one because nothing is listening there any more. ' +
      'Send the new link to anyone who has the old one. If the old address may now belong to a different ' +
      'device, use Regenerate instead.';
  }

  function loadDirect() {
    paintDirect({});
    var user = state.editing;
    if (!user || user.role === 'admin') return;
    api('/api/admin/users/' + encodeURIComponent(user.id) + '/direct_access').then(function (r) {
      if (r.ok && state.editing && state.editing.id === user.id) paintDirect(r.body);
    });
  }

  function directAction(action) {
    var user = state.editing;
    if (!user) return Promise.resolve();
    return api('/api/admin/users/' + encodeURIComponent(user.id) + '/direct_access',
               {method: 'POST', body: {action: action}}).then(function (r) {
      if (!r.ok) { say(r.body.error || 'That did not work.', 'error'); loadDirect(); return; }
      paintDirect(r.body);
    });
  }

  // -------------------------------------------------- device names
  // The compact assignment summary for one device, expandable to the names behind it. It
  // is built from the server's own derivation (the same one Clear Units uses), never
  // recomputed here.
  function assignmentCell(d) {
    var record = d.assignments || {users: [], groups: [], walls: [], direct_access: []};
    var text = window.lumaClearUnits ? window.lumaClearUnits.summaryText(record) : 'Unassigned';
    var cell = el('td', 'muted');
    if (!(record.users.length || record.groups.length || record.walls.length)) {
      cell.textContent = text;
      return cell;
    }
    var open = state.expanded[d.id] === true;
    var toggle = el('button', 'secondary assign-toggle', text);
    toggle.type = 'button';
    toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    toggle.setAttribute('aria-label', 'Show what uses ' + (d.name || d.device_name || 'this device') + ': ' + text);
    toggle.addEventListener('click', function () {
      state.expanded[d.id] = !(state.expanded[d.id] === true);
      renderDevices();
    });
    cell.appendChild(toggle);
    if (open) {
      var detail = el('div', 'assign-detail');
      [['Users', record.users.map(function (n) {
          return (record.direct_access || []).indexOf(n) >= 0 ? n + ' (Direct Access)' : n; })],
       ['Display Groups', record.groups],
       ['Saved Video Walls', (record.wall_reasons && record.wall_reasons.length) ? record.wall_reasons : record.walls],
       ['Wall access', record.wall_access || []]].forEach(function (pair) {
        if (pair[1].length) detail.appendChild(el('div', '', pair[0] + ': ' + pair[1].join(', ')));
      });
      cell.appendChild(detail);
    }
    return cell;
  }

  function renderDevices() {
    var body = $('device_table').querySelector('tbody');
    var needle = ($('device_filter').value || '').toLowerCase();
    body.replaceChildren();
    state.devices.forEach(function (d) {
      var hay = [d.name, d.device_name, d.location, d.model].join(' ').toLowerCase();
      if (needle && hay.indexOf(needle) < 0) return;
      var row = el('tr');
      row.appendChild(el('td', '', d.role === 'encoder' ? 'Source' : d.role === 'decoder' ? 'Display' : 'Other'));
      var cell = el('td');
      var input = document.createElement('input');
      input.type = 'text'; input.maxLength = 48; input.value = d.alias || '';
      input.placeholder = d.device_name || '';
      input.setAttribute('data-id', d.id);
      input.setAttribute('aria-label', 'Friendly name for ' + (d.device_name || 'device'));
      cell.appendChild(input);
      row.appendChild(cell);
      row.appendChild(el('td', '', d.device_name));
      row.appendChild(el('td', '', d.location || d.description || ''));
      row.appendChild(assignmentCell(d));
      body.appendChild(row);
    });
    var dupes = state.dupes || [];
    $('device_dupes').hidden = dupes.length === 0;
    $('device_dupes').textContent = dupes.length
      ? 'Some devices share a name. People will see them told apart by location where there is one.' : '';
  }

  function saveDeviceNames() {
    var mapping = {};
    Array.prototype.forEach.call($('device_table').querySelectorAll('input[data-id]'), function (input) {
      var id = input.getAttribute('data-id');
      var original = (state.devices.filter(function (d) { return d.id === id; })[0] || {}).alias || '';
      if (input.value.trim() !== original) mapping[id] = input.value.trim();
    });
    if (!Object.keys(mapping).length) { say('No names were changed'); return; }
    api('/api/admin/aliases', {method: 'POST', body: {aliases: mapping}}).then(function (r) {
      if (!r.ok) { say(r.body.error || 'That did not work.', 'error'); return; }
      say('Names saved', 'ok');
      return loadAll();
    });
  }

  // ---------------------------------------------------- clear devices
  // The question, the assignments view and the request are shared with Device Manager
  // (clear_units.js). If it is missing the action is refused: the wording is the point.
  function reviewClear() {
    if (!window.lumaClearUnits) { say('Clear Units is not available.', 'error'); return; }
    window.lumaClearUnits.run({
      api: api,
      onError: function (text) { say(text, 'error'); },
      onDone: function (result) { say(window.lumaClearUnits.describe(result), 'ok'); loadAll(); }
    });
  }

  // ---------------------------------------------------- display groups
  function renderGroups() {
    var body = $('group_table').querySelector('tbody');
    body.replaceChildren();
    $('group_empty').hidden = state.groups.length > 0;
    state.groups.forEach(function (g) {
      var row = el('tr');
      row.appendChild(el('td', '', g.name));
      var members = g.members.map(function (m) { return m.name + (m.known ? '' : ' (not found)'); });
      row.appendChild(el('td', '', plural(g.members.length, 'display', 'displays') +
        (members.length ? ': ' + members.slice(0, 4).join(', ') + (members.length > 4 ? '…' : '') : '')));
      row.appendChild(el('td', 'muted', (g.granted_to || []).join(', ')));
      var actions = el('td', 'actions');
      var edit = el('button', 'secondary', 'Edit'); edit.type = 'button';
      edit.addEventListener('click', function () { openGroupEditor(g); });
      var del = el('button', 'danger', 'Delete'); del.type = 'button';
      del.addEventListener('click', function () {
        var who = g.granted_to || [];
        confirmAsk({title: 'Delete display group',
          message: 'Delete ' + g.name + '? The displays in it are not affected.' +
            (who.length ? ' It will be removed from: ' + who.join(', ') + '.' : ''),
          confirmText: 'Delete', danger: true}).then(function (yes) {
          if (!yes) return;
          api('/api/admin/groups/' + encodeURIComponent(g.id), {method: 'POST', body: {action: 'delete'}})
            .then(function (r) {
              if (!r.ok) { say(r.body.error || 'That did not work.', 'error'); return; }
              say('Group deleted', 'ok'); loadAll();
            });
        });
      });
      actions.appendChild(edit); actions.appendChild(del);
      row.appendChild(actions);
      body.appendChild(row);
    });
  }

  function openGroupEditor(group) {
    state.groupEditing = group || null;
    $('group_modal_title').textContent = group ? 'Edit ' + group.name : 'New display group';
    $('g_name').value = group ? group.name : '';
    $('g_error').textContent = '';
    var selected = group ? group.members.map(function (m) { return m.id; }) : [];
    pick.members = checklist('assign_members', devicesOf('decoder'), selected, deviceSub);
    openModal('group_modal');
    $('g_name').focus();
  }

  function saveGroup() {
    var group = state.groupEditing;
    var name = $('g_name').value.trim();
    var members = pick.members.values();
    $('g_error').textContent = '';
    $('g_save').disabled = true;
    var request;
    if (!group) {
      request = api('/api/admin/groups', {method: 'POST', body: {name: name, members: members}});
    } else {
      var url = '/api/admin/groups/' + encodeURIComponent(group.id);
      request = Promise.resolve({ok: true});
      if (name !== group.name) request = request.then(function () {
        return api(url, {method: 'POST', body: {action: 'rename', name: name}}); });
      request = request.then(function (r) { return !r.ok ? r :
        api(url, {method: 'POST', body: {action: 'set_members', members: members}}); });
    }
    request.then(function (r) {
      $('g_save').disabled = false;
      if (!r.ok) { $('g_error').textContent = (r.body || {}).error || 'That did not work.'; return; }
      closeModal('group_modal');
      say('Group saved', 'ok');
      loadAll();
    });
  }

  // ----------------------------------------------------------- activity
  function loadAudit() {
    var q = new URLSearchParams();
    if ($('audit_q').value) q.set('q', $('audit_q').value);
    if ($('audit_event').value) q.set('event', $('audit_event').value);
    if ($('audit_actor').value) q.set('actor', $('audit_actor').value);
    q.set('limit', '200');
    return api('/api/admin/audit?' + q.toString()).then(function (r) {
      if (!r.ok) { say(r.body.error || 'That did not work.', 'error'); return; }
      var list = $('audit_list');
      list.replaceChildren();
      var entries = r.body.entries || [];
      $('audit_empty').hidden = entries.length > 0;
      entries.forEach(function (entry) {
        var row = el('div', 'activity-row' + (entry.event === 'access.denied' || entry.event === 'login.failure' ? ' bad' : ''));
        row.appendChild(el('span', 'muted', new Date((entry.at || 0) * 1000).toLocaleString()));
        row.appendChild(el('span', '', entry.actor || 'This computer'));
        row.appendChild(el('span', '', entry.text || entry.label || ''));
        list.appendChild(row);
      });
      fillFilters(r.body.summary || {});
      paintRetention(r.body.retention || {});
    });
  }

  // How long activity is kept. The server states the bounds; the page only
  // fills the fields and reports the answer.
  function paintRetention(ret) {
    if (!ret || typeof ret.days !== 'number') return;
    if (document.activeElement !== $('ret_days')) $('ret_days').value = ret.days;
    if (document.activeElement !== $('ret_mb')) $('ret_mb').value = Math.round(ret.max_bytes / (1024 * 1024));
    var used = (ret.used_bytes || 0) / (1024 * 1024);
    $('ret_status').textContent = (ret.entries || 0) + ' entries, ' + (used < 0.1 ? 'under 0.1' : used.toFixed(1)) + ' MB in use';
  }

  function saveRetention() {
    var days = parseInt($('ret_days').value, 10);
    var mb = parseInt($('ret_mb').value, 10);
    if (!(days >= 1) || !(mb >= 1)) { say('Enter whole numbers for days and megabytes.', 'error'); return; }
    $('ret_save').disabled = true;
    api('/api/admin/audit/retention', {method: 'POST', body: {days: days, max_bytes: mb * 1024 * 1024}}).then(function (r) {
      $('ret_save').disabled = false;
      if (!r.ok) { say((r.body || {}).error || 'That did not work.', 'error'); return; }
      say('Retention saved', 'ok');
      paintRetention(r.body.retention);
      loadAudit();
    });
  }

  function fillFilters(summary) {
    function fill(select, values, keep) {
      var current = select.value;
      select.replaceChildren(select.firstElementChild);
      values.forEach(function (v) {
        var option = el('option', '', v.label || v);
        option.value = v.id || v;
        select.appendChild(option);
      });
      select.value = keep && Array.prototype.some.call(select.options, function (o) { return o.value === current; }) ? current : '';
    }
    fill($('audit_event'), summary.events || [], true);
    fill($('audit_actor'), summary.accounts || [], true);
  }

  // --------------------------------------------------------------- start
  function wire() {
    Array.prototype.forEach.call($('admin_tabs').querySelectorAll('button'), function (b) {
      b.addEventListener('click', function () { selectTab(b.getAttribute('data-panel')); });
    });
    $('user_new').addEventListener('click', function () { openUserEditor(null); });
    $('u_role').addEventListener('change', syncRoleSections);
    $('u_cancel').addEventListener('click', function () { closeModal('user_modal'); });
    $('u_save').addEventListener('click', saveUser);
    $('direct_enabled').addEventListener('change', function () {
      directAction($('direct_enabled').checked ? 'enable' : 'disable');
    });
    $('direct_address_ack').addEventListener('click', function () { directAction('acknowledge_address'); });
    $('direct_copy').addEventListener('click', function () {
      var value = $('direct_link').value;
      if (!value) return;
      var done = function () { say('Link copied', 'ok'); };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(value).then(done, function () { $('direct_link').select(); say('Press Ctrl+C to copy'); });
      } else { $('direct_link').select(); say('Press Ctrl+C to copy'); }
    });
    $('direct_regen').addEventListener('click', function () {
      confirmAsk({title: 'Regenerate direct link', message: 'The current link will stop working immediately, ' +
        'including on any device that has it saved.', confirmText: 'Regenerate', danger: true}).then(function (yes) {
        if (yes) directAction('regenerate');
      });
    });
    $('device_filter').addEventListener('input', renderDevices);
    $('device_save').addEventListener('click', saveDeviceNames);
    $('clear_open').addEventListener('click', reviewClear);
    // A wall or an account can be changed on another page. Coming back to this one re-reads what
    // depends on each device (cache and stores only, no device traffic), at most once every two
    // seconds, and only while the Device names panel is the one being looked at.
    var lastRefresh = 0;
    function refreshAssignments() {
      if (document.hidden || $('panel_devices').hidden) return;
      var now = Date.now();
      if (now - lastRefresh < 2000) return;
      lastRefresh = now;
      loadAll();
    }
    document.addEventListener('visibilitychange', refreshAssignments);
    if (typeof window.addEventListener === 'function') window.addEventListener('focus', refreshAssignments);
    $('group_new').addEventListener('click', function () { openGroupEditor(null); });
    $('g_cancel').addEventListener('click', function () { closeModal('group_modal'); });
    $('g_save').addEventListener('click', saveGroup);
    $('audit_refresh').addEventListener('click', loadAudit);
    $('ret_save').addEventListener('click', saveRetention);
    ['audit_event', 'audit_actor'].forEach(function (id) { $(id).addEventListener('change', loadAudit); });
    var timer = null;
    $('audit_q').addEventListener('input', function () {
      clearTimeout(timer); timer = setTimeout(loadAudit, 300);
    });
    document.addEventListener('keydown', function (event) {
      if (event.key !== 'Escape') return;
      // Only the topmost open dialog, and not while the shared confirmation is up.
      var asking = document.getElementById('lumaConfirmBackdrop');
      if (asking && !asking.classList.contains('hidden')) return;
      if ($('group_modal').classList.contains('open')) closeModal('group_modal');
      else if ($('user_modal').classList.contains('open')) closeModal('user_modal');
    });
  }

  wire();
  loadAll();
}());
