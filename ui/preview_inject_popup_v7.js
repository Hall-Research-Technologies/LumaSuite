// === /ui/preview_inject_popup_v7.js ===
// Hover: centered preview
// Click: POPUP WINDOW (window.open with features), not a new tab
// Encoders: MJPEG stream, no ?t=
// Decoders: thumbnail, refresh every 2s

(() => {
  if (window.__previewInjectPopupV7) return;
  window.__previewInjectPopupV7 = true;

  const encURL = ip => `http://${ip}/stream?resolution=320x180&fps=15&bitrate=512`;
  const decURL = ip => `http://${ip}/thumbnail/thumbnail1.jpg`;

  // ---------- UI: hover ----------
  function ensureHover() {
    if (document.getElementById("hover_preview")) return;
    const style = document.createElement("style");
    style.textContent = `
      .hover-preview{
        position:fixed;z-index:10000;display:none;
        border:1px solid var(--border);background:var(--card);
        padding:6px;border-radius:8px;box-shadow:0 6px 20px rgba(0,0,0,.35);
        max-width:70vw;max-height:70vh;overflow:hidden;
        left:50%;top:50%;transform:translate(-50%,-50%);
      }
      .hover-preview img{max-width:70vw;max-height:70vh;display:block}
    `;
    document.head.appendChild(style);

    const box = document.createElement("div");
    box.id = "hover_preview";
    box.className = "hover-preview";
    const img = document.createElement("img");
    img.id = "hover_preview_img";
    img.alt = "preview";
    box.appendChild(img);
    document.body.appendChild(box);

    document.addEventListener("keydown", e => {
      if (e.key === "Escape") hideHover();
    });
  }

  let hoverTimer = null;
  function showHoverFor(link) {
    ensureHover();
    const box = document.getElementById("hover_preview");
    const img = document.getElementById("hover_preview_img");
    if (!box || !img) return;
    if (hoverTimer) { clearInterval(hoverTimer); hoverTimer = null; }

    const ip   = link.dataset.ip || ipFromRow(link.closest("tr"));
    const base = link.dataset.base || link.getAttribute("href") || "";
    const enc  = link.dataset.enc === "1" || base.includes("/stream?");

    if (enc) {
      img.src = encURL(ip);
    } else {
      const bump = () => { img.src = decURL(ip) + "?t=" + Date.now(); };
      bump();
      hoverTimer = setInterval(bump, 2000);
    }

    box.style.display = "block";
  }

  function hideHover() {
    const box = document.getElementById("hover_preview");
    const img = document.getElementById("hover_preview_img");
    if (!box || !img) return;
    box.style.display = "none";
    img.src = "";
    if (hoverTimer) { clearInterval(hoverTimer); hoverTimer = null; }
  }

  // ---------- table helpers ----------
  function ensureHeader() {
    const thRow = document.querySelector("#tbl thead tr");
    if (!thRow) return;
    const has = Array.from(thRow.children).some(th => /preview/i.test(th.textContent || ""));
    if (!has) {
      const th = document.createElement("th");
      th.textContent = "Preview";
      thRow.appendChild(th);
    }
  }

  function ipFromRow(tr) {
    if (!tr) return "";
    if (tr.dataset && tr.dataset.ip) return tr.dataset.ip;
    const ipA = tr.querySelector("td:nth-child(2) a.ip, td a.ip");
    if (ipA && ipA.textContent) return ipA.textContent.trim();
    for (const td of Array.from(tr.children)) {
      const t = (td.textContent || "").trim();
      if (/^\d{1,3}(\.\d{1,3}){3}$/.test(t)) return t;
    }
    return "";
  }

  function unitForIP(ip) {
    try { return (window.unitMap && window.unitMap.get && window.unitMap.get(ip)) || {}; }
    catch { return {}; }
  }

  function isEncoderUnit(u) {
    const t = (u && (u.typeLabel || u.type || "")) + "";
    if (/^enc/i.test(t)) return true;
    const m = ((u && u.model) || "").toLowerCase();
    return m.startsWith("at-luma-e") || m.startsWith("at-omni-e") || /\b(111|112|512|e\d{4})\b/.test(m);
  }

  function isSwitcherUnit(u) {
    const t = (((u && (u.typeLabel || u.type || u.role)) || "") + "").toLowerCase();
    const m = ((u && u.model) || "").toLowerCase();
    return t.includes("switcher") || m.includes("at-ome-cs31") || m.includes("cs31");
  }

  function addPreviewCells() {
    ensureHeader();
    const tb = document.getElementById("tbody");
    if (!tb) return;
    Array.from(tb.querySelectorAll("tr")).forEach(tr => {
      if (tr.querySelector("a.preview-link")) return;
      const ip = ipFromRow(tr);
      if (!ip) return;
      const u = unitForIP(ip);
      if (isSwitcherUnit(u)) return;
      const enc = isEncoderUnit(u);
      const base = enc ? encURL(ip) : decURL(ip);

      const td = document.createElement("td");
      const a  = document.createElement("a");
      a.href = base;
      a.className = "ip preview-link";
      a.textContent = "Preview";
      a.dataset.ip = ip;
      a.dataset.enc = enc ? "1" : "0";
      a.dataset.base = base;
      td.appendChild(a);
      tr.appendChild(td);
    });
  }

  // hook into renderRows (if present)
  const originalRender = window.renderRows;
  if (typeof originalRender === "function") {
    window.renderRows = function(list) {
      try { originalRender.call(this, list); }
      finally { try { addPreviewCells(); } catch (e) { console.warn("[preview]", e); } }
    };
  } else {
    // fallback: observe tbody
    const tb = document.getElementById("tbody");
    if (tb && window.MutationObserver) {
      const obs = new MutationObserver(() => { try { addPreviewCells(); } catch (e) {} });
      obs.observe(tb, { childList: true });
    }
  }

  // ---------- delegated events ----------
  document.addEventListener("mouseover", e => {
    const a = e.target.closest && e.target.closest("a.preview-link");
    if (!a) return;
    showHoverFor(a);
  });

  document.addEventListener("mouseout", e => {
    if (e.relatedTarget && e.relatedTarget.closest && e.relatedTarget.closest("a.preview-link")) return;
    if (e.target.closest && e.target.closest("a.preview-link")) hideHover();
  });

  // CLICK -> REAL POPUP
  document.addEventListener("click", e => {
    const a = e.target.closest && e.target.closest("a.preview-link");
    if (!a) return;
    e.preventDefault();

    const ip   = a.dataset.ip || ipFromRow(a.closest("tr"));
    const base = a.dataset.base || a.getAttribute("href") || "";
    const isEnc = a.dataset.enc === "1" || base.includes("/stream?");

    const url = isEnc ? base : (base + (base.includes("?") ? "&" : "?") + "t=" + Date.now());

    // open a popup window (not a new tab)
    const win = window.open(
      url,
      "preview_" + (ip || ""),
      "popup=1,width=520,height=340,menubar=0,toolbar=0,location=0,status=0,resizable=1,scrollbars=0"
    );
    // if blocked, at least navigate the current tab
    if (!win) {
      window.location.href = url;
    }
  });

})();
