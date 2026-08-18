(function(){
  "use strict";

  function ensureStyles() {
    if (document.getElementById("lumaDialogStyles")) return;
    const style = document.createElement("style");
    style.id = "lumaDialogStyles";
    style.textContent = `
      .luma-dialog-backdrop {
        position: fixed;
        inset: 0;
        z-index: 100000;
        display: flex;
        align-items: center;
        justify-content: center;
        padding: 20px;
        background: rgba(2, 6, 23, .66);
        backdrop-filter: blur(3px);
      }
      .luma-dialog {
        width: min(460px, 100%);
        color: var(--text, #f8fafc);
        background: var(--card, #1f2933);
        border: 1px solid var(--border, #334155);
        border-radius: 8px;
        box-shadow: 0 18px 60px rgba(0,0,0,.42);
        overflow: hidden;
      }
      .luma-dialog-head {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
        padding: 16px 18px 10px;
        font-weight: 700;
        font-size: 16px;
      }
      .luma-dialog-body {
        padding: 0 18px 16px;
        color: var(--muted, #cbd5e1);
        line-height: 1.45;
        white-space: pre-wrap;
      }
      .luma-dialog-actions {
        display: flex;
        justify-content: flex-end;
        gap: 8px;
        padding: 12px 18px 16px;
        border-top: 1px solid var(--border, #334155);
      }
      .luma-dialog button {
        border: 1px solid var(--border, #334155);
        border-radius: 6px;
        padding: 7px 12px;
        color: #fff;
        background: var(--panel-2, #26323d);
        cursor: pointer;
      }
      .luma-dialog button.primary {
        background: var(--accent, #1e90ff);
        border-color: var(--accent, #1e90ff);
      }
      .luma-dialog button.danger {
        background: #b91c1c;
        border-color: #b91c1c;
      }
    `;
    document.head.appendChild(style);
  }

  function closeWith(backdrop, resolver, value) {
    backdrop.remove();
    resolver(value);
  }

  function show(options) {
    ensureStyles();
    return new Promise(resolve => {
      const backdrop = document.createElement("div");
      backdrop.className = "luma-dialog-backdrop";
      const title = options.title || "LumaSuite";
      const message = options.message || "";
      const confirmText = options.confirmText || "OK";
      const cancelText = options.cancelText || "Cancel";
      const destructive = !!options.destructive;
      backdrop.innerHTML = `
        <div class="luma-dialog" role="dialog" aria-modal="true" aria-labelledby="lumaDialogTitle">
          <div class="luma-dialog-head" id="lumaDialogTitle">${escapeHtml(title)}</div>
          <div class="luma-dialog-body">${escapeHtml(message)}</div>
          <div class="luma-dialog-actions">
            ${options.cancel === false ? "" : `<button type="button" data-action="cancel">${escapeHtml(cancelText)}</button>`}
            <button type="button" class="${destructive ? "danger" : "primary"}" data-action="confirm">${escapeHtml(confirmText)}</button>
          </div>
        </div>
      `;
      backdrop.addEventListener("click", event => {
        if (event.target === backdrop && options.cancel !== false) closeWith(backdrop, resolve, false);
      });
      backdrop.querySelector("[data-action='confirm']").addEventListener("click", () => closeWith(backdrop, resolve, true));
      const cancel = backdrop.querySelector("[data-action='cancel']");
      if (cancel) cancel.addEventListener("click", () => closeWith(backdrop, resolve, false));
      document.body.appendChild(backdrop);
      backdrop.querySelector("[data-action='confirm']").focus();
    });
  }

  function escapeHtml(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, ch => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;"
    }[ch]));
  }

  window.lumaDialog = {
    alert(message, title = "LumaSuite") {
      return show({ title, message, cancel: false, confirmText: "OK" });
    },
    confirm(options) {
      return show(options || {});
    }
  };

  window.alert = function(message) {
    window.lumaDialog.alert(message);
  };
})();
