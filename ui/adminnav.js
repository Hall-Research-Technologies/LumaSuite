// Administrator navigation that only matters when LumaSuite is opened from another
// device: who is signed in, how to sign out, and the way to the Accounts page.
//
// On the computer LumaSuite runs on there is no account and nothing is added, so
// the local experience is exactly what it always was. Presentation only -- the
// server gates every page and every call; a missing link is never the boundary.
(function () {
  'use strict';
  if (window.__lumaAdminNavInstalled) return;
  window.__lumaAdminNavInstalled = true;

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = String(text);
    return node;
  }

  function signOut(button) {
    button.disabled = true;
    fetch('/api/auth/logout', {method: 'POST', credentials: 'same-origin',
                               headers: {'Content-Type': 'application/json'}, body: '{}'})
      .catch(function () { /* the redirect below is what matters */ })
      .then(function () {
        if (window.lumaForgetCsrf) window.lumaForgetCsrf();
        if (window.lumaForgetShell) window.lumaForgetShell();
        location.replace('/login');
      });
  }

  // The Remote Access entry in the main navigation. It exists only while Remote
  // Access is on (or when this page IS that area), because with it off there are
  // no accounts anybody can sign in with and a tab would lead to nothing useful.
  // Settings keeps the controls that turn Remote Access on. Presentation only:
  // the server gates the page and every call behind it.
  var REMOTE_PATH = '/admin/users';
  function remoteTab(show) {
    var tabs = document.querySelector('.nav-tabs');
    if (!tabs) return;
    var existing = document.getElementById('nav_remote_access');
    var here = location.pathname.replace(/\/+$/, '') === REMOTE_PATH;
    if (!show && !here) {
      if (existing) existing.remove();
      return;
    }
    if (existing) return;
    var link = el('a', 'nav-tab' + (here ? ' active' : ''));
    link.id = 'nav_remote_access';
    link.href = REMOTE_PATH;
    link.appendChild(el('span', null, 'Remote Access'));
    if (here) link.setAttribute('aria-current', 'page');
    tabs.appendChild(link);
  }

  // The shell is remembered between pages. Without that, every page load starts from "no
  // Remote Access tab, no sign-out control" and inserts them only when the session answer
  // arrives -- which shifts the whole header, and on a busy server takes seconds. What the last
  // definite answer said is applied before the first paint; a later definite answer corrects
  // it; no answer at all (a slow or failed request) corrects nothing. Presentation only: the
  // server gates every page and call whatever this holds.
  var SHELL_KEY = 'luma.shell.v1';
  function readShell() {
    try {
      var value = JSON.parse(localStorage.getItem(SHELL_KEY) || 'null');
      return value && typeof value === 'object' ? value : null;
    } catch (err) { return null; }
  }
  function writeShell(shell) {
    try { localStorage.setItem(SHELL_KEY, JSON.stringify(shell)); } catch (err) { /* storage off */ }
  }
  window.lumaForgetShell = function () {
    try { localStorage.removeItem(SHELL_KEY); } catch (err) { /* storage off */ }
  };
  // A session body is a definite answer only if it says who this is. `{}` is what a failed or
  // refused request turns into, and must never be read as "Remote Access is off".
  function isDefinite(session) {
    return !!session && (typeof session.local === 'boolean' || typeof session.role === 'string');
  }
  function shellOf(session) {
    return {admin: session.role === 'admin', remote: !!session.remote_enabled,
            local: !!session.local, username: session.username || ''};
  }

  function accountControls(shell) {
    var existing = document.getElementById('luma_signed_in');
    var wanted = !!(shell && !shell.local && shell.username);
    if (!wanted) {
      if (existing) existing.remove();
      var oldOut = document.getElementById('luma_signout');
      if (oldOut) oldOut.remove();
      return;
    }
    if (existing) { existing.textContent = shell.username; return; }
    // The same gear Settings finds: Production Studio calls it `#gear`, the other pages
    // `#header_gear`.
    var gear = document.querySelector('#header_gear, #gear, [data-luma-gear]');
    if (!gear || !gear.parentNode) return;
    var who = el('span', 'small', shell.username);
    who.id = 'luma_signed_in';
    who.style.cssText = 'color:var(--text-muted);';
    var out = el('button', 'btn small secondary', 'Sign out');
    out.id = 'luma_signout';
    out.type = 'button';
    out.addEventListener('click', function () { signOut(out); });
    gear.parentNode.insertBefore(who, gear);
    gear.parentNode.insertBefore(out, gear);
  }

  function apply(shell) {
    remoteTab(!!(shell && shell.admin && shell.remote));
    accountControls(shell);
  }

  function settle(session) {
    if (!isDefinite(session)) return;       // no answer: keep what is shown
    var shell = shellOf(session);
    writeShell(shell);
    apply(shell);
  }

  function refreshNav() {
    return fetch('/api/auth/session', {credentials: 'same-origin'})
      .then(function (r) { return r.ok ? r.json() : {}; })
      .catch(function () { return {}; })
      .then(settle);
  }
  // Settings calls this after turning Remote Access on or off, so the tab follows
  // without a reload.
  window.lumaRefreshNav = refreshNav;

  // A page restored from the back-forward cache after signing out must not show
  // an administrator's data: reload it so the gate answers.
  window.addEventListener('pageshow', function (event) {
    if (event.persisted) location.reload();
  });

  // First paint uses what the last definite answer said; the session answer then settles it.
  function start() {
    if (window.lumaSession) window.lumaSession().then(settle);
  }
  var remembered = readShell();
  if (remembered) apply(remembered);
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', function () {
    if (remembered) apply(remembered);   // a script run in <head> finds no navigation yet
    start();
  });
  else start();
}());
