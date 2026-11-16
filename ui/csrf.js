// Attach the CSRF token to state-changing requests, once, for every page.
//
// The defect this exists for: an administrator signed in from another device
// could read everything and change nothing. Measured against the live remote
// listener -- POST /api/get_thumbnail_status answered 403 "not permitted"
// without the header and 400 "IP address required" with it, which is the
// handler's own answer. Reads were unaffected throughout, so the session was
// fine and nothing on screen said what was wrong.
//
// The cause is ordinary history. The matrix, USB, Multiview and Settings pages
// were written when LumaSuite answered only on the computer it ran on, where
// the gate trusts the socket and never looks for a token. Remote access added
// the requirement; those 57 call sites predate it.
//
// So this is deliberately NOT 57 edits. One wrapper around `fetch` covers every
// existing call site and every one somebody writes later, which is the half of
// the problem that a sweep through the files would not have solved.
//
// It is not a security control. The gate decides; this only makes the browser
// present what the gate asks for, exactly as the sign-in page and the two
// remote pages already did by hand.
(function () {
  'use strict';

  // Loaded by every page, and some pages load others. Installing the wrapper
  // twice would chain it, and a chained wrapper re-reads and re-attaches on
  // every call for no benefit.
  if (window.__lumaCsrfInstalled) return;
  window.__lumaCsrfInstalled = true;

  var HEADER = 'X-LumaSuite-CSRF';
  var STORAGE_KEY = 'luma_csrf';
  var UNSAFE = {POST: 1, PUT: 1, PATCH: 1, DELETE: 1};

  // In memory for the life of the page, and in sessionStorage so a second tab
  // does not have to ask again. Never localStorage: a token outliving the
  // session it belongs to is a token that is wrong.
  var cached = null;
  // What the session endpoint said, and the request in flight to get it.
  // One place, because this module is also what `lumaSession` answers from:
  // two callers asking the same endpoint for the same answer is one request
  // too many.
  //
  // There was a second single-flight guard here, on the token alone. A
  // mutation of it changed nothing measurable -- `session()` already collapses
  // concurrent callers, so the token guard could never be reached by a second
  // one. Two guards for one thing is what later diverges, so it is gone
  // rather than kept and left untested.
  var summary = null;
  var summaryAsking = null;

  function stored() {
    try { return sessionStorage.getItem(STORAGE_KEY) || ''; }
    catch (err) { return ''; }
  }

  function remember(token) {
    cached = token || '';
    try { sessionStorage.setItem(STORAGE_KEY, cached); }
    catch (err) { /* storage off: the in-memory copy still serves this page */ }
    return cached;
  }

  // The token for this session, asking the server only if nothing has it.
  //
  // A local operator has no session and needs no token -- the gate trusts the
  // socket the request arrived on -- and the endpoint says so. An empty answer
  // is cached exactly like a real one, so a local page does not ask again on
  // every action.
  function token(original) {
    if (cached !== null) return Promise.resolve(cached);
    // sessionStorage is NOT the authority, only a fallback.
    //
    // It is per-tab and it survives a change of session. A tab that had signed
    // in with a password, then opened a Direct Access link, kept the token
    // belonging to the session that had ended -- and this returned it without
    // ever asking the server, so every action was refused for a bad CSRF token
    // with nothing on screen to explain it. Measured on the bench: five
    // consecutive routes refused with reason "csrf" from a direct-link session.
    //
    // The session endpoint answers with the token for the session the request
    // actually arrived on, so that is what decides. `session()` is
    // single-flighted and cached in memory for the life of the page, and every
    // page already asks it once, so this costs no extra request.
    return session(original)
      .then(function (body) {
        var fresh = (body || {}).csrf_token;
        if (fresh) return remember(fresh);
        // The local operator has no session and needs no token; an empty
        // answer is cached exactly like a real one. Only when the request
        // itself failed is the stored copy worth falling back to.
        return remember(body && Object.keys(body).length ? '' : stored());
      })
      .catch(function () { return remember(stored()); });
  }

  // Who this page is talking as. One request, shared by everything that asks.
  function session(fetcher) {
    if (summary !== null) return Promise.resolve(summary);
    if (summaryAsking) return summaryAsking;
    summaryAsking = (fetcher || original)(
      '/api/auth/session', {credentials: 'same-origin'})
      .then(function (response) { return response.ok ? response.json() : {}; })
      .catch(function () { return {}; })
      .then(function (body) {
        summary = body || {};
        summaryAsking = null;
        return summary;
      });
    return summaryAsking;
  }

  // Whether this browser is the operator sitting at the LumaSuite computer.
  //
  // Pages use it to avoid asking for something only the local operator can
  // have -- the firmware folder is on the host's own filesystem, so a remote
  // administrator gets a deliberate 404 and a console error for a request that
  // was never going to work. Not a permission check: the server decides, and
  // it decides the same way whatever this returns.
  window.lumaSession = function () { return session(original); };
  window.lumaIsLocal = function () {
    return session(original).then(function (body) { return !!body.local; });
  };

  // Same-origin only. A request to somebody else's server must never carry
  // this, and the thumbnails the matrix fetches go straight to a device.
  function sameOrigin(input) {
    try {
      return new URL(
        typeof input === 'string' ? input : (input && input.url) || '',
        window.location.href).origin === window.location.origin;
    } catch (err) {
      return false;
    }
  }

  function methodOf(input, init) {
    var method = (init && init.method)
      || (input && typeof input !== 'string' && input.method)
      || 'GET';
    return String(method).toUpperCase();
  }

  function alreadyCarriesIt(init, input) {
    var headers = (init && init.headers)
      || (input && typeof input !== 'string' && input.headers);
    if (!headers) return false;
    if (typeof headers.get === 'function') return !!headers.get(HEADER);
    for (var name in headers) {
      if (Object.prototype.hasOwnProperty.call(headers, name)
          && name.toLowerCase() === HEADER.toLowerCase()) {
        return true;
      }
    }
    return false;
  }

  var original = window.fetch.bind(window);

  window.fetch = function (input, init) {
    var method = methodOf(input, init);
    if (!UNSAFE[method] || !sameOrigin(input)
        || alreadyCarriesIt(init, input)) {
      return original(input, init);
    }
    return token(original).then(function (value) {
      if (!value) return original(input, init);
      // A new object rather than a mutation: the caller's own init is theirs,
      // and a Request passed as `input` is immutable anyway.
      var options = Object.assign({credentials: 'same-origin'}, init || {});
      var headers = new Headers(
        (init && init.headers)
        || (input && typeof input !== 'string' && input.headers)
        || {});
      headers.set(HEADER, value);
      options.headers = headers;
      return original(input, options);
    });
  };

  // If a token turns out to be stale -- a session replaced while a page stayed
  // open -- forget it so the next action asks for the current one rather than
  // failing for as long as the tab is open.
  window.lumaForgetCsrf = function () {
    cached = null;
    try { sessionStorage.removeItem(STORAGE_KEY); }
    catch (err) { /* nothing to forget */ }
  };
}());
