// Remembers which source and display the person last chose, and nothing else.
//
// A bookmark, never authority: the page honours a remembered identity only if the
// server has just listed it, and every action is authorised by the server
// regardless. This file is separate from user.js on purpose -- user.js can see
// session state, so it is not allowed to write to browser storage at all, and this
// file can see neither a link nor a token.
(function () {
  'use strict';
  var PREFIX = 'luma_user_';

  function get(key) {
    try { return window.localStorage.getItem(PREFIX + key) || ''; } catch (e) { return ''; }
  }
  function set(key, value) {
    try {
      if (value) window.localStorage.setItem(PREFIX + key, String(value));
      else window.localStorage.removeItem(PREFIX + key);
    } catch (e) { /* storage unavailable: the page works without it */ }
  }
  window.lumaRemember = {get: get, set: set};
}());
