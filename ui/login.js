(function () {
  'use strict';

  // What protects THIS connection, read from the page's own scheme rather than
  // from anything the server said: a claim about encryption that arrived over the
  // connection it describes is worth nothing if that connection is not encrypted.
  (function statement() {
    var note = document.getElementById('transport_note');
    if (!note) return;
    if (window.location.protocol === 'https:') {
      note.textContent = 'This connection is encrypted.';
      note.classList.add('form-note-ok');
    } else {
      note.textContent = 'This connection is not encrypted. Use it only on a network you trust.';
      note.classList.add('form-note-warn');
    }
    note.hidden = false;
  }());

  var form = document.getElementById('login_form');
  var errorLine = document.getElementById('error');
  var submit = document.getElementById('submit');

  // Where to go afterwards. Taken from the query string but validated as a
  // same-site path: an open redirect on a sign-in page is how somebody is sent to
  // a convincing copy of it. A single leading slash, no scheme, no host, no
  // backslash, and only characters a path or an ordinary query can contain --
  // `//host` is a protocol-relative URL and is refused by the same rule that
  // refuses `https://host`.
  //
  // With nothing asked for, the landing page depends on the role: a restricted
  // account gets its routing page, an administrator gets the application.
  function destination(role) {
    var wanted = new URLSearchParams(location.search).get('next') || '';
    var safe = /^\/(?!\/)[A-Za-z0-9/_.-]*(\?[A-Za-z0-9/_.=&%-]*)?$/;
    var home = role === 'admin' ? '/' : '/user';
    if (!wanted || !safe.test(wanted) || wanted === '/login') return home;
    // Somewhere they can actually open. Authorisation is entirely the server's;
    // this only avoids sending someone where they are certain to be refused,
    // which, straight after entering correct credentials, reads as a wrong password.
    if (role !== 'admin' && !/^\/user(\/|\?|$)/.test(wanted)) return home;
    return wanted;
  }

  form.addEventListener('submit', function (event) {
    event.preventDefault();
    errorLine.textContent = '';
    submit.disabled = true;
    submit.textContent = 'Signing in...';
    fetch('/api/auth/login', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      // The session cookie is set by the server and is HttpOnly, so nothing on
      // this page ever holds it.
      credentials: 'same-origin',
      body: JSON.stringify({
        username: document.getElementById('username').value,
        password: document.getElementById('password').value
      })
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) {
        return {res: res, data: data};
      });
    }).then(function (answer) {
      var data = answer.data;
      if (answer.res.ok && data.ok) {
        // The CSRF token is the one piece the page keeps, in this tab only. It is
        // useless without the cookie, and storing it longer would outlive the
        // session it belongs to.
        try { sessionStorage.setItem('luma_csrf', data.csrf_token || ''); } catch (e) { /* ignore */ }
        location.replace(destination((data.user || {}).role));
        return;
      }
      errorLine.textContent = data.error || 'Sign in failed.';
      submit.disabled = false;
      submit.textContent = 'Sign in';
    }).catch(function () {
      errorLine.textContent = 'Could not reach LumaSuite.';
      submit.disabled = false;
      submit.textContent = 'Sign in';
    });
  });
}());
