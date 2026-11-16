/*
 * Shared LumaSuite / Hall Research family dialog module.
 *
 * Exposes:
 *   window.lumaConfirm(options) -> Promise<boolean>   (the family API)
 *   window.lumaDialog = { alert(message, title), confirm(options) }
 *                                                     (back-compat wrapper)
 *   window.toast(message, type)                       ("ok" | "error" | "info")
 *
 * Production UI must never call the native alert()/confirm()/prompt() --
 * enforced by tests/no_native_dialogs.test.js, which greps ui/*.html and
 * ui/*.js for those call sites. This module is the one place window.alert
 * is legitimately assigned, and the one place a native dialog name may
 * appear in a comment describing what NOT to do.
 */
(function () {
  "use strict";

  function escapeAttr(value) {
    return String(value == null ? "" : value);
  }

  // ---- Confirm dialog -------------------------------------------------

  var CONFIRM_BACKDROP_ID = "lumaConfirmBackdrop";

  function ensureConfirmMarkup() {
    var existing = document.getElementById(CONFIRM_BACKDROP_ID);
    if (existing) return existing;
    var backdrop = document.createElement("div");
    backdrop.id = CONFIRM_BACKDROP_ID;
    backdrop.className = "confirm-backdrop hidden";
    backdrop.innerHTML =
      '<div class="confirm-card" role="dialog" aria-modal="true" aria-labelledby="lumaConfirmTitle">' +
        '<h3 id="lumaConfirmTitle" class="confirm-title"></h3>' +
        '<div class="confirm-message"></div>' +
        '<div class="confirm-summary" hidden></div>' +
        '<label class="confirm-field" hidden>' +
          '<span class="confirm-field-label"></span>' +
          '<input class="confirm-field-input" type="password" autocomplete="new-password" spellcheck="false">' +
          '<span class="confirm-field-help" hidden></span>' +
        "</label>" +
        '<label class="confirm-suppress" hidden>' +
          '<input type="checkbox" class="confirm-suppress-box">' +
          '<span class="confirm-suppress-label"></span>' +
        "</label>" +
        '<div class="confirm-actions">' +
          '<button type="button" class="btn secondary confirm-cancel">Cancel</button>' +
          '<button type="button" class="btn secondary confirm-extra" hidden></button>' +
          '<button type="button" class="btn confirm-ok">Continue</button>' +
        "</div>" +
      "</div>";
    document.body.appendChild(backdrop);
    return backdrop;
  }

  function setSummaryRows(container, rows) {
    container.innerHTML = "";
    if (!rows || !rows.length) {
      container.hidden = true;
      return;
    }
    container.hidden = false;
    rows.forEach(function (row) {
      var line = document.createElement("div");
      line.className = "confirm-summary-row";
      var label = document.createElement("span");
      label.textContent = row.label == null ? "" : String(row.label);
      var value = document.createElement("span");
      value.textContent = row.value == null ? "" : String(row.value);
      line.appendChild(label);
      line.appendChild(value);
      container.appendChild(line);
    });
  }

  /**
   * window.lumaConfirm(options) -> Promise<boolean>
   *   options.title           string
   *   options.message         string
   *   options.summary         [{label, value}, ...]  what will actually happen
   *   options.confirmText     default "Continue"
   *   options.cancelText      default "Cancel"
   *   options.danger          boolean -- destructive/device-mutating action;
   *                            focuses Cancel by default (cost-following focus)
   *   options.cancelOnEscape  default true
   *   options.suppressKey     localStorage key for a "do not show again"
   *                            checkbox, scoped to options.appVersion
   *   options.appVersion      the running app version; a stored suppression
   *                            is honoured only when it matches this version
   *   options.suppressLabel   checkbox label text
   *   options.secondary       {text, value} -- a third action between Cancel and the primary one.
   *                            The promise resolves to `value` (a string) when it is chosen, so
   *                            it is never mistaken for Cancel (false) or the primary action
   *                            (true). Only a caller that passes this ever receives a string.
   *   options.field           {type, label, placeholder, value, help, onValue} -- one input in the dialog (a
   *                            passphrase). Its value goes to onValue only when the operator
   *                            confirms, and is cleared from the box either way.
   */
  window.lumaConfirm = function (options) {
    options = options || {};
    var appVersion = options.appVersion || (window.__lumaAppVersion || "");

    if (options.suppressKey && appVersion) {
      var storedVersion = null;
      try { storedVersion = localStorage.getItem(options.suppressKey); } catch (e) { storedVersion = null; }
      if (storedVersion === String(appVersion)) {
        return Promise.resolve(true);
      }
    }

    var backdrop = ensureConfirmMarkup();
    var card = backdrop.querySelector(".confirm-card");
    var titleEl = backdrop.querySelector(".confirm-title");
    var msgEl = backdrop.querySelector(".confirm-message");
    var summaryEl = backdrop.querySelector(".confirm-summary");
    var suppressWrap = backdrop.querySelector(".confirm-suppress");
    var suppressBox = backdrop.querySelector(".confirm-suppress-box");
    var suppressLabel = backdrop.querySelector(".confirm-suppress-label");
    var cancelBtn = backdrop.querySelector(".confirm-cancel");
    var okBtn = backdrop.querySelector(".confirm-ok");
    var extraBtn = backdrop.querySelector(".confirm-extra");
    var fieldWrap = backdrop.querySelector(".confirm-field");
    var fieldInput = backdrop.querySelector(".confirm-field-input");
    var fieldLabel = backdrop.querySelector(".confirm-field-label");
    var fieldHelp = backdrop.querySelector(".confirm-field-help");
    var field = options.field;
    if (field) {
      fieldLabel.textContent = field.label || "";
      fieldInput.type = field.type || "text";
      fieldInput.placeholder = field.placeholder || "";
      // A pre-filled value (a port number the operator is offered) is optional and never a secret.
      fieldInput.value = field.value == null ? "" : String(field.value);
      if (fieldHelp) {
        fieldHelp.textContent = field.help || "";
        fieldHelp.hidden = !field.help;
      }
      fieldWrap.hidden = false;
    } else {
      fieldWrap.hidden = true;
    }

    var isDanger = !!(options.danger || options.destructive);
    var noCancel = options.cancel === false || options.cancelText === null;

    titleEl.textContent = options.title || "Confirm";
    msgEl.textContent = options.message || "";
    setSummaryRows(summaryEl, options.summary);

    okBtn.textContent = options.confirmText || "Continue";
    cancelBtn.textContent = options.cancelText || "Cancel";
    cancelBtn.hidden = noCancel;
    var secondary = options.secondary && options.secondary.text ? options.secondary : null;
    extraBtn.hidden = !secondary;
    extraBtn.textContent = secondary ? String(secondary.text) : "";
    okBtn.classList.toggle("danger", isDanger);

    if (options.suppressKey) {
      suppressWrap.hidden = false;
      suppressBox.checked = false;
      suppressLabel.textContent = options.suppressLabel || "Don't show this again";
    } else {
      suppressWrap.hidden = true;
    }

    var previouslyFocused = document.activeElement;
    backdrop.classList.remove("hidden");

    return new Promise(function (resolve) {
      var settled = false;

      function finish(result) {
        if (settled) return;
        settled = true;
        backdrop.classList.add("hidden");
        document.removeEventListener("keydown", onKeydown, true);
        okBtn.removeEventListener("click", onOk);
        cancelBtn.removeEventListener("click", onCancel);
        extraBtn.removeEventListener("click", onExtra);
        backdrop.removeEventListener("click", onBackdropClick);
        // The field's value goes to the caller on the way out and is wiped either way.
        if (field) {
          if (result === true && typeof field.onValue === "function") field.onValue(fieldInput.value);
          fieldInput.value = "";
        }
        if (result === true && options.suppressKey && suppressBox.checked && appVersion) {
          try { localStorage.setItem(options.suppressKey, String(appVersion)); } catch (e) {}
        }
        if (previouslyFocused && typeof previouslyFocused.focus === "function") {
          try { previouslyFocused.focus(); } catch (e) {}
        }
        resolve(result);
      }

      var dismissResolvesTrue = noCancel;

      function onOk() { finish(true); }
      function onCancel() { finish(false); }
      function onExtra() { finish(String(secondary.value)); }
      function onBackdropClick(event) {
        if (event.target === backdrop && options.cancelOnEscape !== false) finish(dismissResolvesTrue);
      }
      function onKeydown(event) {
        // Enter in the field is the answer, which is what anybody typing a passphrase expects.
        if (event.key === "Enter" && field && event.target === fieldInput) {
          event.preventDefault();
          finish(true);
          return;
        }
        if (event.key === "Escape" && options.cancelOnEscape !== false) {
          event.preventDefault();
          finish(dismissResolvesTrue);
        }
      }

      okBtn.addEventListener("click", onOk);
      cancelBtn.addEventListener("click", onCancel);
      extraBtn.addEventListener("click", onExtra);
      backdrop.addEventListener("click", onBackdropClick);
      document.addEventListener("keydown", onKeydown, true);

      // Focus follows the cost: a dangerous action defaults focus to
      // Cancel; everything else defaults to Continue. The keyboard default
      // and the visual default must agree.
      var toFocus = field ? fieldInput : ((isDanger && !noCancel) ? cancelBtn : okBtn);
      window.setTimeout(function () {
        try { toFocus.focus(); } catch (e) {}
      }, 0);
    });
  };

  // ---- alert() replacement --------------------------------------------

  window.lumaDialog = {
    alert: function (message, title) {
      return window.lumaConfirm({
        title: title || "LumaSuite",
        message: String(message == null ? "" : message),
        cancelText: null,
        confirmText: "OK",
        cancelOnEscape: true
      }).then(function () {});
    },
    confirm: function (options) {
      return window.lumaConfirm(options || {});
    }
  };

  // Production UI must never call the native alert(); this is the one
  // legitimate reassignment of it, applied globally so any remaining call
  // site (there should be none -- see the enforcement test) still degrades
  // to the styled dialog rather than a browser-native alert box.
  window.alert = function (message) {
    window.lumaDialog.alert(message);
  };

  // ---- Toast ------------------------------------------------------------

  var TOAST_ID = "lumaFamilyToast";
  var toastHideTimer = null;
  var toastRemoveTimer = null;

  function ensureToastEl() {
    var el = document.getElementById(TOAST_ID);
    if (el) return el;
    el = document.createElement("div");
    el.id = TOAST_ID;
    el.className = "family-toast";
    el.setAttribute("role", "status");
    el.setAttribute("aria-live", "polite");
    document.body.appendChild(el);
    return el;
  }

  /**
   * window.toast(message, type) -- transient feedback. One live toast at a
   * time: a second call replaces the first rather than stacking.
   *   type: "ok" | "error" | "info" (default)
   */
  window.toast = function (message, type) {
    var el = ensureToastEl();
    if (toastHideTimer) window.clearTimeout(toastHideTimer);
    if (toastRemoveTimer) window.clearTimeout(toastRemoveTimer);

    el.textContent = String(message == null ? "" : message);
    el.className = "family-toast" + (type === "ok" ? " ok" : type === "error" ? " error" : "");
    el.setAttribute("role", type === "error" ? "alert" : "status");
    el.setAttribute("aria-live", type === "error" ? "assertive" : "polite");

    // Reflow before adding .show so the transition runs even when a toast
    // was already visible.
    el.classList.remove("show");
    void el.offsetHeight;
    el.classList.add("show");

    toastHideTimer = window.setTimeout(function () {
      el.classList.remove("show");
    }, 2600);
    toastRemoveTimer = window.setTimeout(function () {
      el.textContent = "";
    }, 3000);
  };
})();
