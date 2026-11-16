/*
 * A unit's own picture, shown from the Device Manager's Preview column.
 *
 * Two ways to look, both reading the unit's thumbnail directly and both gated on the unit's own
 * thumbnail switch (the checkbox in the same cell):
 *
 *   hover  -- a small card beside the pointer while it rests on the word "Preview";
 *   click  -- a small window of its own that keeps refreshing until it is closed.
 *
 * Neither ever asks a unit for a picture it has switched off. The refresh interval is the same two
 * seconds the OmniSuite Device Info page uses, and a refresh is never started while the previous
 * one is still waiting: a slow or silent unit costs one outstanding request, not a growing pile.
 *
 * Nothing here writes to a unit; the checkbox does that, through the server, in index.html.
 */
(function (root) {
  'use strict';

  var REFRESH_MS = 2000;
  // A request that has not answered by now is abandoned, so a silent unit cannot hold the slot
  // forever and a later refresh can try again.
  var GIVE_UP_MS = 8000;
  var IPV4 = /^\d{1,3}(\.\d{1,3}){3}$/;

  // Only a plain IPv4 literal ever becomes part of a URL. The address reaches an inline script in
  // the popup, so it is checked here rather than escaped.
  function thumbURL(ip) {
    return IPV4.test(String(ip)) ? 'http://' + ip + '/thumbnail/thumbnail1.jpg' : '';
  }

  // The unit's own switch, read from the checkbox in the cell. Unknown (indeterminate) is not "on".
  function stateOf(cell) {
    var cb = cell && cell.querySelector ? cell.querySelector('input.preview-checkbox') : null;
    if (!cb) return 'none';
    if (cb.disabled) return 'busy';
    if (cb.indeterminate) return 'unknown';
    return cb.checked ? 'on' : 'off';
  }

  // The one sentence the operator is told when there is nothing to show, in their words.
  function whyNot(state) {
    if (state === 'off') return 'Thumbnail is switched off for this unit. Tick the box to turn it on.';
    if (state === 'unknown') return 'This unit has not reported its thumbnail setting yet.';
    if (state === 'busy') return 'The thumbnail setting is being changed. Try again in a moment.';
    return 'This unit has no preview.';
  }

  /*
   * A single-flight picture loader: tick() starts a load unless one is outstanding. The picture is
   * fetched once into a detached Image and only then handed to the visible one, so a failure never
   * blanks what is on screen and one tick is one request.
   */
  function createLoader(env, url, onPicture, onFailure) {
    var busy = false;
    var stopped = false;
    var giveUp = null;
    function finish() {
      busy = false;
      if (giveUp) { env.clearTimeout(giveUp); giveUp = null; }
    }
    return {
      tick: function () {
        if (stopped || busy) return false;
        busy = true;
        var probe = new env.Image();
        var src = url + '?t=' + env.now();
        probe.onload = function () { if (stopped) return; finish(); onPicture(src); };
        probe.onerror = function () { if (stopped) return; finish(); onFailure(); };
        giveUp = env.setTimeout(function () { giveUp = null; busy = false; probe.onload = probe.onerror = null; onFailure(); }, GIVE_UP_MS);
        probe.src = src;
        return true;
      },
      stop: function () { stopped = true; finish(); },
      get busy() { return busy; }
    };
  }

  /* ---------------------------------------------------------------- hover card */
  function createHover(env) {
    var timer = null;
    var loader = null;
    function box() { return env.document.getElementById('hover_preview'); }
    function img() { return env.document.getElementById('hover_preview_img'); }
    function ensure() {
      if (box()) return;
      var card = env.document.createElement('div'); card.id = 'hover_preview'; card.className = 'hover-preview';
      var pic = env.document.createElement('img'); pic.id = 'hover_preview_img'; pic.alt = 'preview';
      card.appendChild(pic); env.document.body.appendChild(card);
    }
    function hide() {
      if (timer) { env.clearInterval(timer); timer = null; }
      if (loader) { loader.stop(); loader = null; }
      var b = box(), i = img();
      if (!b || !i) return;
      b.style.display = 'none';
      i.removeAttribute('src');
    }
    function move(e) {
      var b = box();
      if (!b || b.style.display !== 'block') return;
      var pad = 16, vw = env.window.innerWidth, vh = env.window.innerHeight, w = b.offsetWidth, h = b.offsetHeight;
      var x = e.clientX - w - pad, y = e.clientY + pad;
      if (x < 0) x = e.clientX + pad;
      if (y + h > vh) y = e.clientY - h - pad;
      if (x + w > vw) x = vw - w - 4;
      if (y + h > vh) y = vh - h - 4;
      if (y < 0) y = 4;
      b.style.left = x + 'px'; b.style.top = y + 'px';
    }
    function show(e, cell) {
      if (stateOf(cell) !== 'on') return;
      var url = thumbURL(cell.dataset && cell.dataset.ip);
      if (!url) return;
      ensure();
      hide();
      var i = img();
      loader = createLoader(env, url, function (src) { i.src = src; }, function () { hide(); });
      loader.tick();
      timer = env.setInterval(function () { if (loader) loader.tick(); }, REFRESH_MS);
      box().style.display = 'block';
      move(e);
    }
    return {show: show, hide: hide, move: move, get active() { return timer !== null; }};
  }

  /* ------------------------------------------------------------------- popup */
  // The window's own page. It owns its refresh, so closing the window stops it with nothing left
  // behind in this page. The address is placed as a JSON string with '<' escaped so it can never
  // end the script element.
  function popupHTML(url) {
    var literal = JSON.stringify(String(url)).replace(/</g, '\\u003c');
    return '<!DOCTYPE html><html><head><meta charset="utf-8"><title>Preview</title><style>' +
      'body{margin:0;padding:8px;background:#1d232a;color:#c9d1d9;font:13px system-ui,sans-serif;' +
      'display:flex;align-items:center;justify-content:center;height:100vh;box-sizing:border-box}' +
      'img{max-width:100%;max-height:100%;object-fit:contain}#note{display:none;text-align:center}' +
      '</style></head><body><img id="preview" alt="preview"><div id="note">Preview unavailable</div><script>' +
      '(function(){var u=' + literal + ',pic=document.getElementById("preview"),note=document.getElementById("note"),' +
      'busy=false,give=null;' +
      'function end(){busy=false;if(give){clearTimeout(give);give=null}}' +
      'function r(){if(busy)return;busy=true;var p=new Image(),s=u+"?t="+Date.now();' +
      'p.onload=function(){end();pic.src=s;pic.style.display="";note.style.display="none"};' +
      'p.onerror=function(){end();pic.style.display="none";note.style.display="block"};' +
      'give=setTimeout(function(){give=null;busy=false;p.onload=p.onerror=null;pic.style.display="none";note.style.display="block"},' + GIVE_UP_MS + ');' +
      'p.src=s}' +
      'r();var h=setInterval(r,' + REFRESH_MS + ');' +
      'addEventListener("pagehide",function(){clearInterval(h)})})();' +
      '<\/script></body></html>';
  }

  // Opens the window for a unit, or says why not. Returns what happened so it can be asserted.
  function openPopup(env, cell) {
    var state = stateOf(cell);
    if (state !== 'on') { env.say(whyNot(state)); return 'refused'; }
    var url = thumbURL(cell.dataset && cell.dataset.ip);
    if (!url) { env.say(whyNot('none')); return 'refused'; }
    var blobUrl = env.makeBlobURL(popupHTML(url));
    var width = 480, height = 400;
    var left = Math.max(0, (env.window.innerWidth - width) / 2), top = Math.max(0, (env.window.innerHeight - height) / 2);
    var win = env.open(blobUrl, 'preview_' + env.now(),
      'width=' + width + ',height=' + height + ',left=' + left + ',top=' + top + ',resizable=yes,scrollbars=yes');
    // The blob only has to live until the window has read it.
    env.setTimeout(function () { env.revokeBlobURL(blobUrl); }, 60000);
    if (!win) { env.say('The preview window was blocked by the browser.'); return 'blocked'; }
    return 'opened';
  }

  /* ------------------------------------------------------------ page wiring */
  function link(e) { return e.target && e.target.closest ? e.target.closest('.preview-link') : null; }
  function cellOfLink(a) { return a && a.closest ? a.closest('td.preview-cell') : null; }

  function wire(env) {
    var hover = createHover(env);
    var d = env.document;
    d.addEventListener('mouseover', function (e) {
      var a = link(e); if (!a) return;
      if (e.relatedTarget && a.contains && a.contains(e.relatedTarget)) return;
      var cell = cellOfLink(a); if (cell) hover.show(e, cell);
    });
    d.addEventListener('mousemove', function (e) { if (link(e)) hover.move(e); });
    d.addEventListener('mouseout', function (e) {
      var a = link(e); if (!a) return;
      if (e.relatedTarget && a.contains && a.contains(e.relatedTarget)) return;
      hover.hide();
    });
    function activate(a) {
      hover.hide();
      var cell = cellOfLink(a); if (cell) openPopup(env, cell);
    }
    d.addEventListener('click', function (e) {
      var a = link(e); if (!a) return;
      if (e.preventDefault) e.preventDefault();
      activate(a);
    });
    // The word is a link in the Omni sense; give it the keyboard behaviour of a button.
    d.addEventListener('keydown', function (e) {
      var a = link(e); if (!a) return;
      if (e.key === 'Enter' || e.key === ' ') { if (e.preventDefault) e.preventDefault(); activate(a); }
    });
    return hover;
  }

  var api = {REFRESH_MS: REFRESH_MS, GIVE_UP_MS: GIVE_UP_MS, thumbURL: thumbURL, stateOf: stateOf, whyNot: whyNot,
             createLoader: createLoader, createHover: createHover, popupHTML: popupHTML, openPopup: openPopup, wire: wire};
  root.lumaUnitPreview = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;

  // In a page, wire once with the real browser.
  if (typeof document !== 'undefined' && root.document === document && !root.__lumaUnitPreviewWired) {
    root.__lumaUnitPreviewWired = true;
    api.hover = wire({
      document: document, window: root, Image: root.Image,
      now: function () { return Date.now(); },
      setInterval: function (f, ms) { return root.setInterval(f, ms); },
      clearInterval: function (t) { root.clearInterval(t); },
      setTimeout: function (f, ms) { return root.setTimeout(f, ms); },
      clearTimeout: function (t) { root.clearTimeout(t); },
      open: function (u, n, f) { return root.open(u, n, f); },
      makeBlobURL: function (html) { return URL.createObjectURL(new Blob([html], {type: 'text/html'})); },
      revokeBlobURL: function (u) { URL.revokeObjectURL(u); },
      say: function (m) { if (root.toast) root.toast(m, 'error'); }
    });
  }
}(typeof window !== 'undefined' ? window : this));
