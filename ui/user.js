// The restricted routing page: choose a source, choose a display (or a display
// group), press Route.
//
// Everything here is presentation. The server decides what this account may see
// and do, and refuses anything else however the request is made; hiding a card
// is never the boundary. All text from the server is inserted with textContent.
//
// There is no device polling. The page reads on load and after its own action; the
// server shares one device read between everyone. A slow, visible-only check of a
// small permission digest (no device is read) lets an administrator's change to
// this account reach an open page; only a changed digest causes a re-read.
//
// Pictures are the one thing that refreshes on a cadence: while Preview is on and the
// page is visible, the cards actually on screen are re-fetched every five seconds,
// one request per picture at a time, through the server's shared preview cache.
//
// A source can also be dragged onto a display, group or saved video wall. That is a
// second way to press Route, not a second router: it ends in the same request.
(function () {
  'use strict';

  var CSRF_KEY = 'luma_csrf';
  var DIRECT_PATH = /^\/access\//;

  var state = {
    sources: [], displays: [], groups: [], walls: [],
    source: '',                       // selected source id (exactly one)
    targets: [],                      // [{kind: 'display'|'group'|'wall', id}] in the order chosen
    drag: null,                       // {source: id} while a source is being dragged
    outcome: {},                      // target key -> 'ok' | 'failed', from the last route
    busy: false,
    routing: {},                      // target key -> true while a route is in flight
    directLink: false,
    preview: true,                    // pictures on; the person's choice, remembered
    // "Audio follows video": a routing preference for Standard Routing, not a permission. On, a
    // Standard route sends audio and video to the source; off, it changes the video alone and the
    // display keeps its audio. It starts on for every page load and is never stored: nothing
    // outlives the session, so it cannot carry from one person to the next on a shared browser.
    // The Matrix never reads it -- its three modes say exactly what a click does.
    audioFollowsVideo: true,
    // Which routing interfaces this account may use. A server that does not say
    // (or an account from before the setting existed) has Standard only.
    interfaces: {standard: true, matrix: false},
    view: 'standard', viewChosen: false,
    matrix: null, mode: 'av', mxPending: ''
  };
  var REVISION_MS = 20000;
  var SHOT_MS = 5000;                 // how often pictures on screen are re-fetched
  // A browser keeps about six connections to one server and a picture is the slowest thing this page
  // asks for. Seven cards put seven requests in flight, and a Route -- or the permission check --
  // queued behind pictures that each take seconds. So at most this many pictures are being fetched
  // at once and the rest wait their turn, in the order they were asked for; a route, a read or a
  // permission check always finds a connection free.
  var PICTURE_MAX = 3;
  var pictureActive = 0;              // pictures actually being fetched
  var pictureWaiting = [];            // [{key, run, skip, urgent}] in the order they will start
  var shotFresh = {};                 // key -> true when the picture waiting for a turn must be a fresh one
  var revisionTimer = null;
  var shotTimer = null;               // the one picture timer; null when stopped
  var visibleShots = {};              // `${kind}:${id}` -> true while the card is on screen
  var shotInflight = {};              // key -> true while a request for it is pending
  var shotQueued = {};                // key -> 'fresh'|'plain' asked for while one was pending
  var shots = {};                     // `${kind}:${id}` -> {url}
  var observer = null;
  var firstPreview = {};              // identities already fetched this page load
  var pictures = {};                  // `${kind}:${id}` -> last picture received (a Blob)

  // ------------------------------------------------------------ helpers
  function $(id) { return document.getElementById(id); }
  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = String(text);
    return node;
  }
  function remember(key, value) { if (window.lumaRemember) window.lumaRemember.set(key, value); }
  function recall(key) { return window.lumaRemember ? window.lumaRemember.get(key) : ''; }
  function say(message, kind) { if (window.toast) window.toast(message, kind); }
  function targetKey(target) { return target ? target.kind + ':' + target.id : ''; }

  // Focus is identity, not position. A control the page rebuilds, or disables while a route
  // runs, drops focus to the page body, and a person who pressed Enter on it is left at the
  // top of the document. The control is remembered by what it is -- the Route button, a
  // card's kind and id, a matrix cell's row and source -- and focus goes back only if it was
  // stranded, never over a place the person has since chosen.
  function focusToken() {
    var held = document.activeElement;
    if (!held || held === document.body || !held.classList) return null;
    if (held === $('route_btn')) return {button: 'route_btn'};
    if (held.classList.contains('mx-cell')) return {cell: [held.getAttribute('data-row'), held.getAttribute('data-src')]};
    if (held.classList.contains('pick')) return {card: [held.getAttribute('data-kind'), held.getAttribute('data-id')]};
    return null;
  }

  function focusStranded() {
    var held = document.activeElement;
    return !held || held === document.body || held.isConnected === false;
  }

  function focusNodeFor(token) {
    if (token.button) return $(token.button);
    var want = token.cell || token.card;
    var list = document.querySelectorAll(token.cell ? 'button.mx-cell' : '[data-kind]');
    for (var i = 0; i < list.length; i++) {
      var node = list[i];
      var have = token.cell ? [node.getAttribute('data-row'), node.getAttribute('data-src')]
        : [node.getAttribute('data-kind'), node.getAttribute('data-id')];
      if (have[0] === want[0] && have[1] === want[1]) return node;
    }
    return null;
  }

  function restoreFocus(token) {
    if (!token || !focusStranded()) return;
    var node = focusNodeFor(token);
    if (node && !node.disabled) {
      try { node.focus({preventScroll: true}); } catch (e) { /* not focusable at the moment */ }
    }
  }
  function isChosen(kind, id) {
    return state.targets.some(function (t) { return t.kind === kind && t.id === id; });
  }
  function plural(n, one, many) { return n + ' ' + (n === 1 ? one : many); }

  function csrfToken() {
    try { return sessionStorage.getItem(CSRF_KEY) || ''; } catch (e) { return ''; }
  }

  // Runs `task` (which returns a promise) once fewer than PICTURE_MAX pictures are being fetched.
  // An `urgent` one -- the picture of a display a route has just changed -- goes ahead of waiting
  // cadence refreshes but behind earlier urgent ones. The slot is released however the task ends,
  // or one failed picture would hold its turn and nothing after it would ever be fetched.
  function pictureSlot(task, urgent, key) {
    return new Promise(function (resolve) {
      var entry = {urgent: !!urgent, key: key};
      entry.run = function () {
        pictureActive++;
        var finished = false;
        function done(value) {
          if (finished) return;
          finished = true;
          pictureActive--;
          var next = pictureWaiting.shift();
          if (next) next.run();
          resolve(value);
        }
        var started;
        try { started = Promise.resolve(task()); } catch (err) { started = Promise.reject(err); }
        started.then(done, function () { done(null); });
      };
      entry.skip = function () { resolve(null); };
      if (pictureActive < PICTURE_MAX) { entry.run(); return; }
      if (entry.urgent) insertUrgent(entry); else pictureWaiting.push(entry);
    });
  }

  function insertUrgent(entry) {
    var at = 0;
    while (at < pictureWaiting.length && pictureWaiting[at].urgent) at++;
    pictureWaiting.splice(at, 0, entry);
  }

  // A route has just changed this card, and its cadence picture has not started yet: move that one
  // up rather than queueing a second request behind it. True when there was one to move.
  function promotePicture(key) {
    for (var i = 0; i < pictureWaiting.length; i++) {
      if (pictureWaiting[i].key !== key) continue;
      var entry = pictureWaiting.splice(i, 1)[0];
      entry.urgent = true;
      insertUrgent(entry);
      return true;
    }
    return false;
  }

  // Everything still waiting for a turn is answered without a fetch: the page was hidden or left,
  // the session ended, or Preview was switched off. Nothing waits for a turn that is not coming.
  function releasePictureQueue() {
    pictureWaiting.splice(0).forEach(function (entry) { entry.skip(); });
    shotFresh = {};
  }

  // Stops all picture work that has not started: the cadence and the queue.
  function stopPictureWork() {
    stopShotTimer();
    releasePictureQueue();
  }

  function toLogin() {
    stopPictureWork();
    // A direct link has no sign-in page: reloading the link mints a fresh session
    // (a host restart ends every session, and the person holding a wall panel
    // has no password to give). The token is never put into a sign-in URL.
    if (DIRECT_PATH.test(location.pathname)) { location.reload(); return; }
    location.replace('/login?next=' + encodeURIComponent('/user'));
  }

  function api(path, options) {
    options = options || {};
    var headers = {'Accept': 'application/json'};
    if (options.body) headers['Content-Type'] = 'application/json';
    if (options.method && options.method !== 'GET') headers['X-LumaSuite-CSRF'] = csrfToken();
    return fetch(path, {
      method: options.method || 'GET', headers: headers, credentials: 'same-origin',
      body: options.body ? JSON.stringify(options.body) : undefined
    }).then(function (res) {
      if (res.status === 401) { toLogin(); return {res: res, body: {}}; }
      return res.json().catch(function () { return {}; }).then(function (body) {
        return {res: res, body: body};
      });
    });
  }

  // What went wrong, in a sentence. Never a status word, never an internal name.
  function failureText(res, body) {
    if (body && body.reason === 'resource_busy') {
      return 'That display is busy with another change. Try again in a moment.';
    }
    if (res.status === 403) {
      return (body && body.error) || 'Your permission for that has changed. Ask an administrator.';
    }
    if (res.status === 404) return (body && body.error) || 'That equipment is not available right now.';
    if (res.status === 502) return (body && body.error) || 'The display did not confirm the change.';
    if (res.status === 503) return (body && body.error) || 'That device did not answer.';
    return (body && body.error) || 'That did not work.';
  }

  // ------------------------------------------------- wording for state
  function describeShowing(showing) {
    if (!showing) return 'Unknown';
    if (showing.mode === 'none') return 'Unrouted';
    if (showing.mode === 'source') return 'Showing: ' + (showing.source_name || 'a source');
    if (showing.mode === 'mixed') return 'Mixed';
    if (showing.mode === 'unavailable') return 'Not available';
    return 'Showing another source';
  }

  // ------------------------------------------------------------ cards
  function isAvailable(kind, item) {
    // A group or a wall is usable while any member answers (the server reports the
    // ones that did not); a single source or display must itself be answering.
    return kind === 'group' || kind === 'wall' ? item.online_members > 0 : !!item.online;
  }

  function findItem(kind, id) {
    var list = kind === 'source' ? state.sources : kind === 'group' ? state.groups
      : kind === 'wall' ? state.walls : state.displays;
    for (var i = 0; i < list.length; i++) if (list[i].id === id) return list[i];
    return null;
  }

  function card(kind, item) {
    var multi = kind === 'group' || kind === 'wall';
    var button = el('button', 'pick' + (multi ? ' group' : '') + (kind === 'wall' ? ' wall' : ''));
    button.type = 'button';
    button.setAttribute('data-id', item.id);
    button.setAttribute('data-kind', kind);
    var selected = kind === 'source' ? state.source === item.id : isChosen(kind, item.id);
    button.setAttribute('aria-pressed', selected ? 'true' : 'false');

    if (!multi) {
      var shot = el('div', 'pick-shot empty');
      shot.setAttribute('data-note', state.preview ? '' : 'Preview off');
      shot.setAttribute('data-shot', kind + ':' + item.id);
      // A card rebuilt by a click shows the picture it already has: choosing
      // something is not a reason to ask a device for the same picture again.
      var have = pictures[kind + ':' + item.id];
      if (state.preview && have) { shot.setAttribute('data-have', '1'); paint(shot, have); }
      button.appendChild(shot);
    }

    var body = el('div', 'pick-body');
    if (kind === 'group') body.appendChild(el('span', 'group-badge', 'Group'));
    if (kind === 'wall') body.appendChild(el('span', 'group-badge', 'Wall'));
    body.appendChild(el('span', 'pick-name', item.name));

    var online = isAvailable(kind, item);
    var sub;
    if (kind === 'source') {
      sub = online ? 'Online' : 'Offline';
    } else if (multi) {
      sub = plural(item.members, 'display', 'displays') + ' · ' + describeShowing(item.showing);
      if (!online) sub = plural(item.members, 'display', 'displays') + ' · Offline';
      else if (item.degraded) sub += ' · ' + (item.members - item.online_members) + ' offline';
    } else {
      sub = online ? describeShowing(item.showing) : 'Offline';
    }
    var key = kind + ':' + item.id;
    if (state.routing[key]) sub = 'Routing…';
    body.appendChild(el('span', 'pick-sub', sub));
    button.appendChild(body);

    if (!online) {
      // An offline resource stays listed -- hiding it would suggest the assignment
      // had been taken away -- but it cannot be chosen, dragged or dropped on. The
      // server refuses it too; this only says so before the person tries.
      button.classList.add('offline');
      button.setAttribute('aria-disabled', 'true');
    }
    if (multi && item.degraded && online) button.classList.add('degraded');
    if (state.routing[key]) button.classList.add('routing');
    if (state.outcome[key] === 'ok') button.classList.add('route-ok');
    if (state.outcome[key] === 'failed') button.classList.add('route-failed');

    button.addEventListener('click', function () { if (online) choose(kind, item.id); });
    wireDrag(button, kind, item, online);
    return button;
  }

  // ------------------------------------------------------ drag and drop
  //
  // Dragging is a second way to press Route. The source being dragged is the one this
  // page holds in memory from the moment the drag started -- never an identity read
  // back out of the event -- and a drop ends in routeTo(), which is what the Route
  // button calls, which sends the one request the server authorises and verifies.
  // Nothing is routed by an ordinary click, and Route + keyboard work unchanged.
  function canDrop(kind) {
    return !!(state.drag && !state.busy && kind !== 'source');
  }

  function clearDragMarks() {
    Array.prototype.forEach.call(document.querySelectorAll('[data-kind]'), function (n) {
      n.classList.remove('drop-over');
      n.classList.remove('drop-target');
      n.classList.remove('dragging');
    });
  }

  function wireDrag(button, kind, item, online) {
    if (kind === 'source') {
      if (!online) return;
      button.setAttribute('draggable', 'true');
      button.addEventListener('dragstart', function (event) {
        if (state.busy) { if (event.preventDefault) event.preventDefault(); return; }
        state.drag = {source: item.id};
        button.classList.add('dragging');
        if (event.dataTransfer) {
          // Only a label travels with the drag; the page trusts what it holds itself.
          try { event.dataTransfer.setData('text/plain', item.name); } catch (e) { /* ignore */ }
          event.dataTransfer.effectAllowed = 'copy';
        }
        // Say, on the destinations themselves, which ones will take it.
        Array.prototype.forEach.call(document.querySelectorAll('[data-kind]'), function (n) {
          var k = n.getAttribute('data-kind');
          if (k === 'source' || n.classList.contains('offline')) return;
          n.classList.add('drop-target');
        });
      });
      button.addEventListener('dragend', function () {
        state.drag = null;
        clearDragMarks();
      });
      return;
    }
    // Entering and leaving fire for every child element the pointer crosses, so the card is
    // only un-highlighted when the pointer has left it altogether (a depth count).
    var depth = 0;
    button.addEventListener('dragover', function (event) {
      if (!online || !canDrop(kind)) return;       // not preventDefault: the drop is refused
      event.preventDefault();
      if (event.dataTransfer) event.dataTransfer.dropEffect = 'copy';
      button.classList.add('drop-over');
    });
    button.addEventListener('dragenter', function (event) {
      if (online && canDrop(kind)) { depth++; event.preventDefault(); button.classList.add('drop-over'); }
    });
    button.addEventListener('dragleave', function () {
      depth = Math.max(0, depth - 1);
      if (depth === 0) button.classList.remove('drop-over');
    });
    button.addEventListener('drop', function (event) {
      event.preventDefault();
      depth = 0;
      button.classList.remove('drop-over');
      var held = state.drag;
      state.drag = null;
      clearDragMarks();
      if (!held || !online || state.busy) return;
      var src = findItem('source', held.source);
      if (!src || !src.online) { say('That source is not available.', 'error'); return; }
      state.source = held.source;
      remember('source', state.source);
      routeTo(held.source, [{kind: kind, id: item.id}]);
    });
  }

  function renderSources() {
    var host = $('sources');
    host.replaceChildren();
    if (!state.sources.length) {
      host.appendChild(el('p', 'remote-empty', 'No sources have been assigned to your account.'));
      return;
    }
    state.sources.forEach(function (item) { host.appendChild(card('source', item)); });
  }

  function renderDisplays() {
    var host = $('displays');
    host.replaceChildren();
    if (!state.groups.length && !state.displays.length && !state.walls.length) {
      host.appendChild(el('p', 'remote-empty', 'No displays have been assigned to your account.'));
      return;
    }
    if (state.walls.length) {
      host.appendChild(el('div', 'section-label', 'Saved video walls'));
      var wallGrid = el('div', 'pick-grid');
      state.walls.forEach(function (item) { wallGrid.appendChild(card('wall', item)); });
      host.appendChild(wallGrid);
    }
    if (state.groups.length) {
      host.appendChild(el('div', 'section-label', 'Display groups'));
      var groupGrid = el('div', 'pick-grid');
      state.groups.forEach(function (item) { groupGrid.appendChild(card('group', item)); });
      host.appendChild(groupGrid);
    }
    if (state.displays.length) {
      if (state.groups.length || state.walls.length) host.appendChild(el('div', 'section-label', 'Displays'));
      var grid = el('div', 'pick-grid');
      state.displays.forEach(function (item) { grid.appendChild(card('display', item)); });
      host.appendChild(grid);
    }
  }

  function render() {
    // Standard routing's cards exist only while it is the interface in front of the
    // person: a hidden page must not ask devices for pictures nobody can see.
    if (state.view !== 'standard') return;
    var held = focusToken();         // the cards are rebuilt below; focus must outlive that
    renderSources();
    renderDisplays();
    renderAction();
    watchPreviews();
    restoreFocus(held);
  }

  // ---------------------------------------------------------- previews
  //
  // A native still from the device itself, fetched by the server on our behalf
  // (the browser never learns a device address). Every card near the viewport asks
  // once; after that, while Preview is on and the page is visible, the cards that are
  // actually on screen are refreshed every five seconds. One timer for the page,
  // one request per picture at a time (a slow device is skipped, never stacked), and
  // nothing at all while the switch is off, the tab is hidden or the person has gone.
  function paint(shot, blob) {
    var url = URL.createObjectURL(blob);
    var img = new Image();
    img.alt = '';
    img.onload = function () {
      URL.revokeObjectURL(url);
      if (!state.preview) return;
      shot.replaceChildren(img);
      shot.classList.remove('empty');
      shot.setAttribute('data-note', '');
    };
    img.onerror = function () { URL.revokeObjectURL(url); showNoPreview(shot); };
    img.src = url;
  }

  function shotNode(key) {
    return document.querySelector('[data-shot="' + CSS.escape(key) + '"]');
  }

  function loadPreview(shot, fresh) {
    if (!state.preview) return Promise.resolve(null);
    var parts = shot.getAttribute('data-shot').split(':');
    var kind = parts[0], id = parts.slice(1).join(':');
    var key = kind + ':' + id;
    var query = (kind === 'source' ? 'source=' : 'display=') + encodeURIComponent(id);
    if (fresh || !firstPreview[key]) query += '&fresh=1';
    else query += '&t=' + Date.now();
    firstPreview[key] = true;
    return fetch('/api/user/preview?' + query, {credentials: 'same-origin'}).then(function (res) {
      if (res.status === 401) { toLogin(); return null; }
      var type = res.headers.get('Content-Type') || '';
      if (res.ok && type.indexOf('image/') === 0) return res.blob();
      return res.json().catch(function () { return {}; }).then(function () { return null; });
    }).then(function (blob) {
      var current = shotNode(key);
      // A picture that arrives after the switch went off is dropped, not shown.
      if (!current || !state.preview) return;
      if (!blob) { showNoPreview(current); return; }
      pictures[key] = blob;
      paint(current, blob);
    }).catch(function () {
      var current = shotNode(key);
      if (current) showNoPreview(current);
    });
  }

  // One picture at a time per card. A request for a card that already has one in
  // flight is remembered and made when that one returns -- so a route's fresh
  // picture is never lost behind an older request, and a slow device never has
  // more than one request waiting.
  function refreshShot(key, fresh) {
    if (!state.preview) return;
    if (shotInflight[key]) {
      // Its turn has not come yet: that very request becomes the fresh one, at the front.
      if (fresh && promotePicture(key)) { shotFresh[key] = true; return; }
      shotQueued[key] = (fresh || shotQueued[key] === 'fresh') ? 'fresh' : 'plain';
      return;
    }
    var node = shotNode(key);
    if (!node) return;
    shotInflight[key] = true;
    if (fresh) shotFresh[key] = true;
    // The card is found again when its turn comes: a render may have rebuilt it while it waited.
    pictureSlot(function () {
      var current = shotNode(key);
      var wantFresh = !!shotFresh[key];
      delete shotFresh[key];
      return current ? loadPreview(current, wantFresh) : null;
    }, !!fresh, key).then(function () {}, function () {}).then(function () {
      delete shotInflight[key];
      var again = shotQueued[key];
      delete shotQueued[key];
      if (again && state.preview && !document.hidden) refreshShot(key, again === 'fresh');
    });
  }

  function tickShots() {
    if (!state.preview || document.hidden) { stopPictureWork(); return; }
    Object.keys(visibleShots).forEach(function (key) { refreshShot(key, false); });
  }

  function startShotTimer() {
    if (shotTimer || !state.preview || document.hidden) return;
    shotTimer = setInterval(tickShots, SHOT_MS);
  }

  function stopShotTimer() {
    if (shotTimer) { clearInterval(shotTimer); shotTimer = null; }
  }

  // After a route: the pictures that matter are fetched now, and the five-second
  // cadence starts again from this moment rather than firing a second time at once.
  function refreshNow(keys) {
    if (!state.preview || document.hidden) return;
    keys.forEach(function (key) { if (visibleShots[key]) refreshShot(key, true); });
    stopShotTimer();
    startShotTimer();
  }

  function showNoPreview(shot) {
    // Never a broken image: a quiet note instead. The picture is informational;
    // routing never depends on it.
    if (!shot.querySelector('img')) {
      shot.classList.add('empty');
      shot.setAttribute('data-note', 'No preview');
    }
  }

  function watchPreviews() {
    if (observer) { observer.disconnect(); observer = null; }
    if (!state.preview) { visibleShots = {}; stopShotTimer(); return; }
    var nodes = Array.prototype.slice.call(document.querySelectorAll('[data-shot]'));
    // The cards are rebuilt on every render but keep their keys, and a new observer only
    // reports after the next frame. What was on screen is therefore kept for keys that still
    // exist -- otherwise a route's own re-render would forget which pictures to refresh at
    // once -- and the observer then corrects it.
    var present = {};
    nodes.forEach(function (n) { present[n.getAttribute('data-shot')] = true; });
    Object.keys(visibleShots).forEach(function (key) { if (!present[key]) delete visibleShots[key]; });
    if (!nodes.length) { startShotTimer(); return; }
    if (!('IntersectionObserver' in window)) {
      nodes.forEach(function (n) {
        visibleShots[n.getAttribute('data-shot')] = true;
        if (!n.getAttribute('data-have')) refreshShot(n.getAttribute('data-shot'), false);
      });
      startShotTimer();
      return;
    }
    // One observer for the cards that exist now; it is replaced, never added to,
    // whenever the cards are rebuilt. A card leaving the screen stops refreshing.
    observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        var key = entry.target.getAttribute('data-shot');
        if (!entry.isIntersecting) { delete visibleShots[key]; return; }
        visibleShots[key] = true;
        // A card painted from a picture already held asks for nothing on arrival.
        if (!entry.target.getAttribute('data-have') && !entry.target.getAttribute('data-asked')) {
          entry.target.setAttribute('data-asked', '1');
          refreshShot(key, false);
        }
      });
    }, {rootMargin: '150px'});
    nodes.forEach(function (n) { observer.observe(n); });
    startShotTimer();
  }

  // The switch. Off: stop watching, drop every picture, and say so on the card.
  // On: watch again, so only cards near the viewport ask for a picture.
  function setPreview(on, persist) {
    state.preview = !!on;
    var box = $('preview_toggle');
    if (box) box.checked = state.preview;
    var mirror = $('preview_toggle_m');
    if (mirror) mirror.checked = state.preview;
    if (persist) remember('preview', state.preview ? '' : 'off');
    if (!state.preview) cancelPreview();
    if (!state.preview) {
      if (observer) { observer.disconnect(); observer = null; }
      stopPictureWork();
      visibleShots = {};
      shotInflight = {};
      shotQueued = {};
      shotFresh = {};
      Array.prototype.forEach.call(document.querySelectorAll('[data-shot]'), function (shot) {
        shot.replaceChildren();
        shot.classList.add('empty');
        shot.setAttribute('data-note', 'Preview off');
        shot.setAttribute('data-have', '');           // nothing is held any more: ask again when on
      });
      firstPreview = {};
      pictures = {};
    } else {
      Array.prototype.forEach.call(document.querySelectorAll('[data-shot]'), function (shot) {
        if (!shot.querySelector('img')) shot.setAttribute('data-note', '');
      });
      watchPreviews();
    }
  }

  // ------------------------------------------------------ selection
  //
  // One source; any number of displays and display groups. Choosing the same card
  // again removes it. Nothing is chosen on the person's behalf.
  function choose(kind, id) {
    if (state.busy) return;
    var item = findItem(kind, id);
    if (!item || !isAvailable(kind, item)) return;          // offline or no longer listed
    if (kind === 'source') {
      state.source = state.source === id ? '' : id;
      remember('source', state.source);
    } else if (isChosen(kind, id)) {
      state.targets = state.targets.filter(function (t) { return !(t.kind === kind && t.id === id); });
    } else {
      state.targets = state.targets.concat([{kind: kind, id: id}]);
    }
    if (kind !== 'source') rememberTargets();
    render();
  }

  function rememberTargets() {
    // '|' cannot appear in an identity or a group id; ':' does (it is a MAC).
    remember('targets', state.targets.map(targetKey).join('|'));
  }

  function clearTargets() {
    if (state.busy || !state.targets.length) return;
    state.targets = [];
    rememberTargets();
    render();
  }

  function nameOf(list, id) {
    for (var i = 0; i < list.length; i++) if (list[i].id === id) return list[i].name;
    return '';
  }

  function chosenWords() {
    var displays = state.targets.filter(function (t) { return t.kind === 'display'; }).length;
    var walls = state.targets.filter(function (t) { return t.kind === 'wall'; }).length;
    var groups = state.targets.length - displays - walls;
    var parts = [];
    if (displays) parts.push(plural(displays, 'display', 'displays'));
    if (groups) parts.push(plural(groups, 'group', 'groups'));
    if (walls) parts.push(plural(walls, 'video wall', 'video walls'));
    return parts.join(' and ');
  }

  function renderAction() {
    var summary = $('summary');
    var button = $('route_btn');
    var clear = $('clear_targets');
    var count = $('selection_count');
    summary.replaceChildren();
    if (clear) clear.hidden = !state.targets.length;
    if (count) count.textContent = state.targets.length ? chosenWords() + ' chosen' : '';
    if (state.busy) {
      summary.textContent = 'Working…';
      button.disabled = true;
      return;
    }
    var source = state.source ? nameOf(state.sources, state.source) : '';
    if (source && state.targets.length) {
      var shown = chosenWords();
      if (state.targets.length > 1) {
        // Everything chosen is routed by this one press, including groups chosen earlier and
        // still ticked: say how many displays that is. Groups can share displays, so it is a ceiling.
        var reach = 0, spans = false;
        state.targets.forEach(function (t) {
          if (t.kind === 'display') { reach += 1; return; }
          var list = t.kind === 'wall' ? state.walls : state.groups;
          var held = list.filter(function (g) { return g.id === t.id; })[0];
          reach += held ? (held.members || 0) : 0;
          spans = true;
        });
        if (spans) shown += ' (up to ' + plural(reach, 'display', 'displays') + ')';
      }
      if (state.targets.length === 1) {
        var only = state.targets[0];
        var list = only.kind === 'group' ? state.groups : only.kind === 'wall' ? state.walls : state.displays;
        shown = nameOf(list, only.id);
        if (only.kind === 'group' || only.kind === 'wall') {
          var held = list.filter(function (g) { return g.id === only.id; })[0];
          if (held) shown += ' (' + plural(held.members, 'display', 'displays') + ')';
        }
      }
      summary.appendChild(document.createTextNode('Route '));
      summary.appendChild(el('strong', '', source));
      summary.appendChild(document.createTextNode(' to '));
      summary.appendChild(el('strong', '', shown));
      button.disabled = false;
    } else {
      // Say what is still missing; the action is never withdrawn.
      var span = el('span', 'muted', !source && !state.targets.length ? 'Choose a source and at least one display.'
        : !source ? 'Choose a source.' : 'Choose at least one display.');
      summary.appendChild(span);
      button.disabled = true;
    }
  }

  // ------------------------------------------------------------ routing
  function setBusy(on) { state.busy = on; renderAction(); }

  function refreshAfter(displayIds) {
    return refresh().then(function () {
      // The pictures of what each routed display shows now are fetched at once and
      // the five-second cadence starts again from here: no settle delay, no extra timer.
      refreshNow(displayIds.map(function (id) { return 'display:' + id; }));
    });
  }

  // The Route button.
  function routeNow() {
    if (!state.source || !state.targets.length) return;
    routeTo(state.source, state.targets.slice());
  }

  // The one routing action. Both the Route button and a drop end here, so there is
  // one request shape and one authorisation path; the page never sequences writes.
  function routeTo(source, chosen) {
    if (state.busy || !source || !chosen.length) return;
    var opener = focusToken();       // the Route button is disabled while this runs
    var displays = chosen.filter(function (t) { return t.kind === 'display'; })
      .map(function (t) { return t.id; });
    var groups = chosen.filter(function (t) { return t.kind === 'group'; })
      .map(function (t) { return t.id; });
    var walls = chosen.filter(function (t) { return t.kind === 'wall'; })
      .map(function (t) { return t.id; });
    // The single-flight claim is taken before anything is asked or sent.
    setBusy(true);
    state.outcome = {};
    chosen.forEach(function (t) { state.routing[targetKey(t)] = true; });
    render();
    renderAction();

    var body = {source: source, displays: displays, groups: groups,
                audio_follows_video: state.audioFollowsVideo !== false};
    if (walls.length) body.walls = walls;
    api('/api/user/route_multi', {method: 'POST', body: body}).then(function (answer) {
      return afterRoute(answer.res, answer.body || {}, displays, chosen, opener);
    }).catch(function () {
      say('Could not reach LumaSuite', 'error');
    }).then(function () {
      chosen.forEach(function (t) { delete state.routing[targetKey(t)]; });
      setBusy(false);
      render();
      restoreFocus(opener);
    });
  }

  function afterRoute(res, body, displayIds, chosen, opener) {
    if (!body.results) {                       // refused before anything was written
      chosen.forEach(function (t) { state.outcome[targetKey(t)] = 'failed'; });
      say(failureText(res, body), 'error');
      return refresh();
    }
    var summary = body.summary || (body.routed + ' of ' + plural(body.total, 'display', 'displays') + ' routed');
    chosen.forEach(function (t) { state.outcome[targetKey(t)] = body.ok ? 'ok' : 'failed'; });
    if (body.ok) {
      var unfollowed = body.results.some(function (r) { return r.audio === 'not_following'; });
      var done = body.total === 1 ? 'Routed' : summary;
      say(unfollowed ? done + '. A display is not set to follow video for audio.' : done, 'ok');
    } else {
      say(summary, 'error');
      // The per-display outcome is something to read, not a passing message.
      var rows = body.results.filter(function (r) { return !r.ok; }).map(function (r) {
        return {label: r.name, value: resultWords(r)};
      });
      if (window.lumaConfirm && rows.length) {
        // The dialog returns focus to whatever held it when it opened, which was the body
        // if the control had been disabled by then. Naming the opener puts it back.
        var closed = window.lumaConfirm({title: 'Some displays did not change',
                                         message: summary + '.',
                                         summary: rows, confirmText: 'OK', cancelText: null});
        if (closed && typeof closed.then === 'function') {
          closed.then(function () { restoreFocus(opener); });
        }
      }
    }
    // Selections stay as they were, as on the OmniSuite page: the same choice is
    // usually repeated, and clearing it would be the page deciding for the person.
    return refreshAfter(displayIds);
  }

  function resultWords(r) {
    // A wall that changed some of its displays says how many, never "worked".
    if (r.kind === 'wall' && r.result === 'partial') {
      return 'Only ' + plural(r.routed || 0, 'display', 'displays') + ' of ' + (r.total || 0) + ' changed';
    }
    // The engine declined before writing anything and said why: that sentence, not "Did not work".
    if (r.message) return String(r.message);
    return {not_verified: 'Sent, but the display did not confirm it',
            unreachable: 'Did not answer', not_found: 'Not found',
            offline: 'Offline', busy: 'Busy with another change',
            refused: 'Not permitted'}[r.result] || 'Did not work';
  }

  // ----------------------------------------------------------- matrix
  //
  // The Matrix is a second way to press the same Route. The grid is whatever the server
  // projected for this account -- columns are the sources it may use, rows the displays,
  // groups and saved walls it may drive -- and the page derives nothing from it: a
  // click sends the one request Standard routing sends (plus the interface and mode),
  // and the server decides again. Video and audio are separate: a breakaway is simply
  // two columns lit in one row. A group or wall is one row; "all" means every member.
  var MODE_HINTS = {
    av: 'A click routes audio and video together (ring = video, dot = audio).',
    video: 'A click routes the video only; the audio stays as it is.',
    audio: 'A click routes the audio only; the video stays as it is.'
  };
  var MODE_LABELS = {av: 'Route audio and video', video: 'Route video', audio: 'Route audio'};
  var mxSeq = 0;                     // an answer that arrives after a newer request is dropped
  var mxSignature = '';              // what the grid was last built from (structure, not state)
  var mxHover = {timer: null, ctrl: null, url: ''};
  var PREVIEW_DELAY_MS = 400;

  function viewAllowed(view) {
    return !!(state.interfaces && state.interfaces[view]);
  }

  // Which interface is showing. A remembered choice is a bookmark: honoured only while
  // the account still holds that interface, otherwise the one it holds. Applied on every
  // state read, so an administrator's change reaches an open page without a reload.
  function applyView() {
    var both = viewAllowed('standard') && viewAllowed('matrix');
    var want = state.viewChosen ? state.view : (recall('view') || 'standard');
    var view = viewAllowed(want) ? want
      : viewAllowed('standard') ? 'standard' : viewAllowed('matrix') ? 'matrix' : '';
    state.viewChosen = true;
    state.view = view;
    $('mode_tabs').hidden = !both;
    ['standard', 'matrix'].forEach(function (name) {
      $('tab_' + name).setAttribute('aria-selected', view === name ? 'true' : 'false');
    });
    $('content').hidden = view !== 'standard';
    $('matrix_content').hidden = view !== 'matrix';
    $('action_bar').hidden = view !== 'standard';
    $('no_routing').hidden = view !== '';
    if (view !== 'matrix') cancelPreview();
    if (view !== 'standard') {
      if (observer) { observer.disconnect(); observer = null; }
      visibleShots = {};
      stopShotTimer();
    }
  }

  function setView(view) {
    if (!viewAllowed(view) || state.view === view) return;
    state.view = view;
    remember('view', view);              // client-local, never security state
    applyView();
    if (view === 'matrix') loadMatrix(); else render();
  }

  function rowKey(row) { return row.kind + ':' + row.id; }

  function stateWords(row) {
    if (row.kind === 'display') return row.online_members ? '' : 'Offline';
    var members = plural(row.members, 'display', 'displays');
    if (row.state === 'unavailable') return members + ' · Not available';
    if (row.state === 'mixed') return members + ' · Mixed';
    if (row.state === 'partial') return members + ' · ' + (row.members - row.online_members) + ' offline';
    return members + ' · All';
  }

  // Whether a cell may be pressed. The server refuses the rest again; this only says
  // so before the person tries.
  function cellUsable(row, source) {
    if (!source.online || !row.online_members) return false;
    return row.modes.indexOf(state.mode) >= 0;
  }

  function paintCell(button, row, source) {
    var cell = (row.cells && row.cells[source.id]) || {video: 'none', audio: 'none'};
    var ring = button.firstChild;
    ring.className = 'mx-ring' + (cell.video === 'all' ? ' v-all' : cell.video === 'partial' ? ' v-partial' : '');
    ring.firstChild.className = 'mx-dot' + (cell.audio === 'all' ? ' a-all' : cell.audio === 'partial' ? ' a-partial' : '');
    var usable = cellUsable(row, source);
    // Busy is said with aria-disabled, not `disabled`: a disabled button that holds focus
    // drops it to the page body, and the cell just pressed is the one that must keep it.
    // matrixRoute() refuses a second press while a route is running, so nothing is lost.
    button.disabled = !usable;
    button.setAttribute('aria-disabled', usable && !state.busy ? 'false' : 'true');
    var pending = state.routing[rowKey(row)] && state.mxPending === source.id;
    button.classList.toggle('pending', !!pending);
    var held = [];
    if (cell.video !== 'none') held.push(cell.video === 'all' ? 'video routed' : 'video on some displays');
    if (cell.audio !== 'none') held.push(cell.audio === 'all' ? 'audio routed' : 'audio on some displays');
    button.setAttribute('aria-label', MODE_LABELS[state.mode] + ': ' + source.name + ' to ' + row.name +
      (held.length ? ' (' + held.join(', ') + ')' : ''));
  }

  function structureOf(data) {
    return JSON.stringify([
      (data.sources || []).map(function (s) { return [s.id, s.name]; }),
      (data.rows || []).map(function (r) { return [r.kind, r.id, r.name, r.modes]; })]);
  }

  function rowOf(key) {
    var rows = (state.matrix && state.matrix.rows) || [];
    for (var i = 0; i < rows.length; i++) if (rowKey(rows[i]) === key) return rows[i];
    return null;
  }

  function cellNode(table, row, source) {
    var found = null;
    Array.prototype.forEach.call(table.querySelectorAll('button.mx-cell'), function (b) {
      if (b.getAttribute('data-row') === rowKey(row) && b.getAttribute('data-src') === source.id) found = b;
    });
    return found;
  }

  function buildGrid(table, sources, rows) {
    table.replaceChildren();
    cancelPreview();
    if (!sources.length || !rows.length) {
      table.appendChild(el('caption', 'remote-empty', !sources.length
        ? 'No sources have been assigned to your account.'
        : 'No displays have been assigned to your account.'));
      return;
    }
    var head = el('thead');
    var hr = el('tr');
    hr.appendChild(el('th', 'mx-corner', ''));
    sources.forEach(function (s) {
      var th = el('th', 'mx-col');
      th.setAttribute('scope', 'col');
      th.setAttribute('data-src', s.id);
      var name = el('button', 'mx-src', s.name);
      name.type = 'button';
      th.appendChild(name);
      var sub = el('span', 'mx-sub', s.online ? '' : 'Offline');
      sub.setAttribute('data-colsub', s.id);
      th.appendChild(sub);
      hr.appendChild(th);
    });
    head.appendChild(hr);
    table.appendChild(head);
    var body = el('tbody');
    rows.forEach(function (row) {
      var tr = el('tr', 'mx-row mx-' + row.kind);
      tr.setAttribute('data-row', rowKey(row));
      var th = el('th', 'mx-rowhead');
      th.setAttribute('scope', 'row');
      if (row.kind !== 'display') th.appendChild(el('span', 'group-badge', row.kind === 'wall' ? 'Wall' : 'Group'));
      th.appendChild(el('span', 'mx-name', row.name));
      var sub = el('span', 'mx-sub', '');
      sub.setAttribute('data-sub', rowKey(row));
      th.appendChild(sub);
      tr.appendChild(th);
      sources.forEach(function (s) {
        var td = el('td', 'mx-td');
        var button = el('button', 'mx-cell');
        button.type = 'button';
        button.setAttribute('data-row', rowKey(row));
        button.setAttribute('data-src', s.id);
        var ring = el('span', 'mx-ring');
        ring.appendChild(el('span', 'mx-dot'));
        button.appendChild(ring);
        button.addEventListener('click', function () { matrixRoute(rowOf(rowKey(row)), s.id); });
        // The picture is asked for at the routing dot, as in the application's own matrix: the
        // cell is the intersection of one source and one display, so what the pointer rests on
        // is what the preview shows. The heading alone does nothing.
        wirePreview(td, button, s);
        td.appendChild(button);
        tr.appendChild(td);
      });
      body.appendChild(tr);
    });
    table.appendChild(body);
  }

  function renderMatrix() {
    var data = state.matrix;
    var table = $('mx_table');
    if (!data) return;
    var held = focusToken();         // a rebuilt grid has new cell nodes
    $('mx_hint').textContent = MODE_HINTS[state.mode];
    var sources = data.sources || [], rows = data.rows || [];
    var signature = structureOf(data);
    // Membership or identity changed: rebuild. Otherwise only live state is repainted,
    // in place, so a refresh never costs the person their scroll position or focus.
    if (signature !== mxSignature || !table.firstChild) {
      mxSignature = signature;
      buildGrid(table, sources, rows);
    }
    sources.forEach(function (s) {
      var sub = table.querySelector('[data-colsub="' + CSS.escape(s.id) + '"]');
      if (sub) sub.textContent = s.online ? '' : 'Offline';
    });
    rows.forEach(function (row) {
      var sub = table.querySelector('[data-sub="' + CSS.escape(rowKey(row)) + '"]');
      if (sub) sub.textContent = state.routing[rowKey(row)] ? 'Routing…' : stateWords(row);
      sources.forEach(function (s) {
        var button = cellNode(table, row, s);
        if (button) paintCell(button, row, s);
      });
    });
    restoreFocus(held);
  }

  function loadMatrix() {
    if (state.view !== 'matrix') return Promise.resolve();
    var seq = ++mxSeq;
    return api('/api/user/matrix').then(function (answer) {
      if (seq !== mxSeq) return;                 // a newer read has been asked for
      var body = answer.body || {};
      if (answer.res.status === 403) {            // the interface was taken away under us
        state.interfaces.matrix = false;
        applyView();
        render();
        return refresh();
      }
      if (!answer.res.ok || !body.ok) {
        $('mx_hint').textContent = failureText(answer.res, body);
        return;
      }
      state.matrix = body;
      renderMatrix();
    }).catch(function () { $('mx_hint').textContent = 'Could not reach LumaSuite.'; });
  }

  function matrixRoute(row, sourceId) {
    if (state.busy || !row || !sourceId) return;
    var source = null;
    ((state.matrix && state.matrix.sources) || []).forEach(function (s) { if (s.id === sourceId) source = s; });
    if (!source || !cellUsable(row, source)) return;
    // One request, built from what the grid held a moment ago and re-authorised by the
    // server. The single-flight claim is taken before anything is sent.
    var chosen = [{kind: row.kind, id: row.id}];
    var opener = focusToken();       // the cell pressed; it keeps focus unless the grid is rebuilt
    setBusy(true);
    state.outcome = {};
    state.routing[rowKey(row)] = true;
    state.mxPending = sourceId;
    renderMatrix();
    var body = {source: sourceId, interface: 'matrix', mode: state.mode,
                displays: [], groups: [], walls: []};
    body[row.kind === 'display' ? 'displays' : row.kind === 'group' ? 'groups' : 'walls'] = [row.id];
    api('/api/user/route_multi', {method: 'POST', body: body}).then(function (answer) {
      return afterRoute(answer.res, answer.body || {}, row.kind === 'display' ? [row.id] : [], chosen, opener);
    }).catch(function () {
      say('Could not reach LumaSuite', 'error');
    }).then(function () {
      delete state.routing[rowKey(row)];
      state.mxPending = '';
      setBusy(false);
      render();
      renderMatrix();                // restores the cell itself if the grid was rebuilt
    });
  }

  // A preview is one frame, asked for when the pointer rests on a routing cell (the dot) and
  // dropped when it leaves. Nothing is fetched on load and nothing repeats.
  function cancelPreview() {
    if (mxHover.timer) { clearTimeout(mxHover.timer); mxHover.timer = null; }
    if (mxHover.ctrl) { try { mxHover.ctrl.abort(); } catch (e) { /* ignore */ } }
    mxHover.ctrl = null;
    if (mxHover.url) { URL.revokeObjectURL(mxHover.url); mxHover.url = ''; }
    var card = $('mx_preview');
    if (card) { card.replaceChildren(); card.hidden = true; }
  }

  function wirePreview(node, button, source) {
    function start() {
      cancelPreview();
      if (!state.preview || !source.online) return;
      mxHover.timer = setTimeout(function () {
        mxHover.timer = null;
        var ctrl = typeof AbortController === 'function' ? new AbortController() : {};
        mxHover.ctrl = ctrl;
        fetch('/api/user/preview?source=' + encodeURIComponent(source.id) + '&fresh=1',
              {credentials: 'same-origin', signal: ctrl.signal}).then(function (res) {
          if (res.status === 401) { toLogin(); return null; }
          var type = res.headers.get('Content-Type') || '';
          return res.ok && type.indexOf('image/') === 0 ? res.blob() : null;
        }).then(function (blob) {
          if (mxHover.ctrl !== ctrl || !state.preview) return;     // the pointer left meanwhile
          var card = $('mx_preview');
          card.replaceChildren();
          if (blob) {
            mxHover.url = URL.createObjectURL(blob);
            var img = new Image();
            img.alt = '';
            img.src = mxHover.url;
            card.appendChild(img);
          } else {
            card.appendChild(el('span', 'mx-sub', 'No preview'));
          }
          var box = node.getBoundingClientRect ? node.getBoundingClientRect() : {left: 0, bottom: 0};
          card.style.left = Math.max(8, Math.min(box.left, (window.innerWidth || 1024) - 280)) + 'px';
          card.style.top = (box.bottom + 6) + 'px';
          card.hidden = false;
        }).catch(function () { /* aborted or unreachable: no card */ });
      }, PREVIEW_DELAY_MS);
    }
    // The card takes no pointer events, so it can never sit between the pointer and the dot. A
    // press ends the preview first: the click is the person's answer, and the picture has done
    // its job. Keyboard focus on the dot shows the same picture, so it is not mouse-only.
    var pressed = false;             // a mouse press focuses the dot; that focus is not a request
    function release() { pressed = false; }
    node.addEventListener('mouseenter', start);
    node.addEventListener('mouseleave', function () { release(); cancelPreview(); });
    button.addEventListener('mousedown', function () { pressed = true; cancelPreview(); });
    button.addEventListener('click', release);
    button.addEventListener('focus', function () { if (!pressed) start(); });
    button.addEventListener('blur', function () { release(); cancelPreview(); });
  }

  function setMode(mode) {
    if (!MODE_HINTS[mode]) return;
    state.mode = mode;
    renderMatrix();
  }

  // ------------------------------------------------------------- loading
  function restoreSelection() {
    // A remembered choice is honoured only if the server just listed it, and only
    // while it is still usable: a source or display that has gone offline, or a
    // group, wall or display the administrator took away, is deselected.
    var s = recall('source');
    var usable = function (kind, id) {
      var item = findItem(kind, id);
      return !!item && isAvailable(kind, item);
    };
    if (s && usable('source', s)) state.source = s;
    else if (state.source && !usable('source', state.source)) state.source = '';
    var saved = recall('targets');
    if (!saved && recall('target')) { saved = recall('target'); remember('target', ''); }
    if (!state.targets.length && saved) {
      var seen = {};
      var next = [];
      saved.split('|').forEach(function (entry) {
        var parts = entry.split(':');
        var kind = parts[0], id = parts.slice(1).join(':');
        if ((kind === 'display' || kind === 'group' || kind === 'wall') && usable(kind, id) && !seen[entry]) {
          seen[entry] = true;
          next.push({kind: kind, id: id});
        }
      });
      state.targets = next;
    } else {
      state.targets = state.targets.filter(function (t) { return usable(t.kind, t.id); });
    }
  }

  function refresh() {
    return api('/api/user/state').then(function (answer) {
      var body = answer.body || {};
      if (!answer.res.ok || !body.ok) {
        $('loading').hidden = false;
        $('loading').textContent = failureText(answer.res, body);
        $('content').hidden = true;
        $('matrix_content').hidden = true;
        $('mode_tabs').hidden = true;
        return;
      }
      state.sources = body.sources || [];
      state.displays = body.destinations || [];
      state.groups = body.groups || [];
      state.walls = body.walls || [];
      state.rev = body.rev || '';
      var routing = body.routing || {};
      state.interfaces = {standard: routing.standard !== false, matrix: routing.matrix === true};
      $('who').textContent = body.username || '';
      restoreSelection();
      $('loading').hidden = true;
      applyView();
      render();
      return loadMatrix();
    });
  }

  // Ask whether this account's grants changed. Never a device read, never while the
  // tab is hidden, and a route in flight is left to finish and refresh itself.
  function checkRevision() {
    if (document.hidden || state.busy || !state.rev) return Promise.resolve();
    return api('/api/user/revision').then(function (answer) {
      var body = answer.body || {};
      if (answer.res.ok && body.ok && body.rev && body.rev !== state.rev) return refresh();
    }).catch(function () { /* a missed check is retried at the next interval */ });
  }

  function watchRevision() {
    if (revisionTimer) return;
    revisionTimer = setInterval(checkRevision, REVISION_MS);
    document.addEventListener('visibilitychange', function () {
      if (!document.hidden) checkRevision();
    });
    window.addEventListener('focus', checkRevision);
  }

  function signOut() {
    api('/api/auth/logout', {method: 'POST', body: {}}).then(function () {
      if (window.lumaForgetCsrf) window.lumaForgetCsrf();
      try { sessionStorage.removeItem(CSRF_KEY); } catch (e) { /* ignore */ }
      toLogin();
    });
  }

  function watchVisibility() {
    // Pictures refresh only while the page is on screen: hidden stops the timer,
    // coming back fetches what is visible at once and resumes the cadence.
    document.addEventListener('visibilitychange', function () {
      if (document.hidden) { stopPictureWork(); return; }
      refreshNow(Object.keys(visibleShots));
    });
    // Navigating away ends it too, so nothing outlives the page.
    window.addEventListener('pagehide', stopPictureWork);
  }

  function start() {
    watchVisibility();
    $('route_btn').addEventListener('click', routeNow);
    $('clear_targets').addEventListener('click', clearTargets);
    // Bound as soon as the control exists, not after the saved value loads.
    var afv = $('afv_toggle');
    if (afv) {
      afv.checked = state.audioFollowsVideo;
      afv.addEventListener('change', function () { state.audioFollowsVideo = !!afv.checked; });
    }
    $('preview_toggle').addEventListener('change', function () {
      setPreview($('preview_toggle').checked, true);
    });
    var mirror = $('preview_toggle_m');
    if (mirror) mirror.addEventListener('change', function () { setPreview(mirror.checked, true); });
    ['standard', 'matrix'].forEach(function (name) {
      $('tab_' + name).addEventListener('click', function () { setView(name); });
    });
    Object.keys(MODE_HINTS).forEach(function (mode) {
      var radio = $('mx_mode_' + mode);
      if (radio) radio.addEventListener('change', function () { if (radio.checked) setMode(mode); });
    });
    setPreview(recall('preview') !== 'off', false);
    $('signout').addEventListener('click', signOut);
    // How this session was opened decides what the page offers. A direct link has
    // no password to sign back in with, so it shows no sign-out.
    api('/api/auth/session').then(function (answer) {
      var body = answer.body || {};
      state.directLink = body.method === 'direct_link';
      $('signout').hidden = state.directLink || body.local === true;
      if (body.csrf_token) {
        try { sessionStorage.setItem(CSRF_KEY, body.csrf_token); } catch (e) { /* ignore */ }
      }
      return refresh().then(watchRevision);
    }).catch(function () {
      $('loading').textContent = 'Could not reach LumaSuite.';
    });
  }

  start();
}());
