(() => {
  const BUILD = "R14_FIXED_FULL";
  // console.log("[producer] build", BUILD, "loaded");

  /* ============================================================
     SECURITY UTILITIES (P2 #7, P3 #3)
  ============================================================ */
  // Escape HTML to prevent XSS when inserting user content
  function escapeHtml(unsafe) {
    if (unsafe == null) return "";
    return String(unsafe)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  // Track event listeners to prevent duplicates (P2 #7)
  const eventListenerRegistry = new WeakMap();
  function addEventListenerOnce(element, event, handler, options) {
    if (!eventListenerRegistry.has(element)) {
      eventListenerRegistry.set(element, new Map());
    }
    const elementListeners = eventListenerRegistry.get(element);
    const key = `${event}:${handler.name || 'anonymous'}`;
    
    if (!elementListeners.has(key)) {
      element.addEventListener(event, handler, options);
      elementListeners.set(key, true);
    }
  }

  /* ============================================================
     ENDPOINTS
  ============================================================ */
  const EP = {
    assets: "/api/producer2/assets",
    pushImage: "/api/producer2/push/image",
    pushLogo: "/api/producer2/push/logo",
    rpc: "/api/producer/jsonrpc",
    cache: "/api/cache",
    globalState: "/api/producer2/state"
  };

  /* ============================================================
     HELPERS
  ============================================================ */
  const $ = (sel, root=document) => root.querySelector(sel);
  const debounce = (fn, t=300) => { let h; return (...a) => { clearTimeout(h); h=setTimeout(()=>fn(...a),t); }; };

  const MJPEG = ip => `http://${ip}/stream?resolution=640x360&fps=25&bitrate=1024`;
  const DOWN_IMG  = ip => `http://${ip}/downStreamImage`;
  const DOWN_LOGO = ip => `http://${ip}/downOverlayImage`;

  function hexToRgbTriplet(hex) {
    const clean = hex.replace("#","");
    const num = parseInt(clean,16);
    return { r:(num>>16)&255, g:(num>>8)&255, b:num&255 };
  }
  const rgbToHex = (r,g,b) =>
    "#" + [r,g,b].map(v=>v.toString(16).padStart(2,"0")).join("");
// === FIX: provide validateBitrate() for HTML onblur ===
window.validateBitrate = function(inp){
  if (!inp) return;
  const clean = inp.value.replace(/[^\d]/g,"").slice(0,6);
  let num = Number(clean || 0);
  if (num < 128) num = 128;
  if (num > 60000) num = 60000;
  inp.value = num;
};

  /* ============================================================
     STATE
  ============================================================ */
  let pollCount=0, lastPollTs=0, livePollHandle=null;
let pollNow = false;

  const state = {
    units: [],
    encs: [],
    unit: null,
    assets: { images:{}, logos:{} },
    presets: [],
    ticker: { dir:"rtl", rows:[], enabled:false, pending:null },
    tickerColor: localStorage.getItem("tickerFontColor") || "255.255.255"
  };
  const pendingMute = {
	  audio: false,
	  video: false
	};
  let pendingLogo = false;
  let pendingTicker = false;
  let pendingRtmp = false;
  let savedProfileBeforeRtmp = null;  // Track profile before RTMP is enabled


  // Apply dark mode from localStorage (set by index.html)
  (function applyDarkMode(){
    const dark = localStorage.getItem('dark') !== 'false';
    document.body.classList.toggle('dark', dark);
    document.body.classList.toggle('light', !dark);
  })();

  /* ============================================================
     RPC WRAPPER
  ============================================================ */
async function rpc(method, params={}, timeout=4) {
  if (!state.unit) throw new Error("No active unit");

  const payload = {
    ip: state.unit.ip,
    method,
    jsonrpc:"2.0",
    id:`${method}_call`,
    params:{...params},
    timeout
  };

  console.debug(`[RPC] Sending to ${payload.ip}: ${method}`, payload);

  const res = await fetch(EP.rpc, {
    method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify(payload)
  });

  const text = await res.text();
  console.debug(`[RPC] Raw response for ${method}:`, text.slice(0, 500));

  let j;
  try {
    j = JSON.parse(text);
  } catch {
    // console.error("Bad RPC JSON:", text);
    throw new Error("Bad JSON from RPC");
  }

  console.debug(`[RPC] Parsed response for ${method}:`, j);

  if (j.error) {
    console.error(`[RPC] Error response for ${method}:`, j.error);
    throw new Error(j.error.message || JSON.stringify(j.error));
  }

  // If this is a Set method, throttle poll requests to avoid amplification (max 1 per 500ms)
  if (/\.Set$/.test(method) && state.unit) {
    if (!window.lastSetPollTime || (Date.now() - window.lastSetPollTime > 500)) {
      window.lastSetPollTime = Date.now();
      pollNow = true;
    }
  }

  return j.result;
}
/* ============================================================
   DEBUG: Inspect RTMP traffic
   ============================================================ */
const _fetch = fetch;
fetch = async function(url, opts){
  const body = opts?.body;
  let parsed = null;

  try { parsed = JSON.parse(body); } catch {}

  if (parsed?.method === "RtmpRunStatus.Get") {
    // console.log("📨 RTMP REQUEST →", parsed);
  }

  const res = await _fetch(url, opts);

  try {
    const text = await res.clone().text();
    const json = JSON.parse(text);
    if (parsed?.method === "RtmpRunStatus.Get") {
      // console.log("📩 RTMP RESPONSE ←", json);
    }
  } catch(err) {
    // console.log("📩 RTMP RESPONSE ← <unreadable>", err);
  }

  return res;
};



  /* ============================================================
     LOAD SERVER CACHE
  ============================================================ */
  async function loadCache() {
    try {
      const text = await fetch(EP.cache,{cache:"no-store"}).then(r=>r.text());
      const arr = JSON.parse(text);
      let units = Array.isArray(arr) ? arr :
                  Array.isArray(arr?.units) ? arr.units : [];

      // Filter out offline devices based on index page status
      try {
        const statusMap = JSON.parse(localStorage.getItem('status_map_v1') || '{}');
        units = units.filter(u => {
          const status = statusMap[u.ip];
          // Hide if marked as offline
          return !status || status.text !== 'offline';
        });
      } catch(e) {
        console.warn('[producer] Failed to filter offline devices:', e);
      }

      state.units = units;
      state.encs = units.filter(u => (u.role || u.type || "").toLowerCase() === "encoder");

      $("#cacheStatus").textContent =
        `Loaded ${state.encs.length} encoders / ${units.length - state.encs.length} decoders`;
    } catch(e) {
      // console.warn("loadCache failed", e);
      state.units = [];
      state.encs = [];
    }
  }

  /* ============================================================
     LOAD ASSETS (Images / Logos)
  ============================================================ */
  async function loadAssets() {
    try {
      state.assets = await (await fetch(EP.assets,{cache:"no-store"})).json();
    } catch {
      state.assets = { images:{}, logos:{} };
    }
  }

  /* ============================================================
     GLOBAL STATE (Ticker + RTMP Presets)
  ============================================================ */
  const saveGlobalState = debounce(async ()=> {
    try {
      const payload = {
        ticker: state.ticker,
        streams: state.presets
      };
      await fetch(EP.globalState, {
        method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify(payload)
      });
    } catch(e) {
      // console.warn("saveGlobalState failed",e);
    }
  }, 300);

  async function loadGlobalState() {
    try {
      const data = await (await fetch(EP.globalState,{cache:"no-store"})).json();
      if (data.ticker) state.ticker = data.ticker;
      if (data.streams) state.presets = data.streams;
    } catch(e) {
      // console.warn("loadGlobalState failed",e);
    }
  }

  /* ============================================================
     POPULATE ENCODERS DROPDOWN
  ============================================================ */
  function populateEncoders() {
    const sel = $("#unitSelect");
    sel.innerHTML = "";

    if (!state.encs.length) {
      // Placeholder option
      const o = document.createElement("option");
      o.value = "";
      o.textContent = "No encoders detected";
      sel.append(o);

      // Add dynamic Scan button between dropdown block and refresh button
      const refreshBtn = $("#refreshBtn");
      const row = refreshBtn.parentElement; // row contains col + refresh
      let scanBtn = document.getElementById("scanEncodersBtn");
      if (!scanBtn) {
        scanBtn = document.createElement("button");
        scanBtn.id = "scanEncodersBtn";
        scanBtn.type = "button";
        scanBtn.className = "btn";
        scanBtn.textContent = "Scan";
        scanBtn.setAttribute("aria-label","Scan for encoders (go to Device Manager)");
        scanBtn.style.background = "var(--accent)";
        scanBtn.style.color = "#fff";
        scanBtn.style.borderColor = "var(--accent)";
        // FIX P2 #7: Use addEventListener instead of onclick for better event management
        scanBtn.addEventListener('click', () => { window.location.href = "index.html"; });
        // Insert before refresh button (after the col)
        row.insertBefore(scanBtn, refreshBtn);
      }
      // Remove legacy link if still present
      const oldLink = document.getElementById("scanEncodersLink");
      if (oldLink) oldLink.remove();
      return;
    }

    // Remove scan button if encoders now present
    const existingBtn = document.getElementById("scanEncodersBtn");
    if (existingBtn) existingBtn.remove();
    const existingLink = document.getElementById("scanEncodersLink");
    if (existingLink) existingLink.remove();

    const saved = localStorage.getItem("selectedEncoderIP");

    // Add placeholder option at top if encoders exist
    if (state.encs.length) {
      const placeholder = document.createElement("option");
      placeholder.value = "";
      placeholder.textContent = "Choose Encoder";
      sel.append(placeholder);
    }

    state.encs.forEach(u => {
      const o = document.createElement("option");
      o.value = u.ip;
      o.textContent = `${u.ip} — ${u.hostname || u.model || ""}`;
      sel.append(o);
    });

    if (state.encs.length) {
      if (saved && state.encs.some(e=>e.ip===saved)) {
        sel.value = saved;
        selectUnit(saved);
      } else {
        sel.selectedIndex = 0;
        // Don't auto-select first encoder, leave on "Choose Encoder"
      }
    }
  }

  $("#unitSelect").onchange = e => selectUnit(e.target.value);

  /* ============================================================

        // Initialize placeholders for preview images if empty or broken
        ["logoPreview","activeImage","livePreview"].forEach(id => {
          const img = document.getElementById(id);
          if (!img) return;
          // If no src or no natural size yet after brief delay, mark placeholder
          setTimeout(() => {
            if (!img.getAttribute("src") || !img.naturalWidth) {
              const slot = img.closest('.slot');
              if (slot) slot.classList.add('placeholder');
            }
          }, 100);
        });
     IMAGE / LOGO SLOT PAINTER
  ============================================================ */
  function radioFor(text,onClick){
    const w = document.createElement("label");
    w.style.display="inline-flex";
    w.style.alignItems="center";
    w.style.gap="6px";

    const rb=document.createElement("input");
    rb.type="radio";
    rb.name="slotSel";
    rb.onclick=onClick;

    const sp=document.createElement("span");
    sp.textContent=text;

    w.append(rb,sp);
    return w;
  }

  function paintImageSlots(){
    const wrap=$("#imageSlots");
    wrap.innerHTML="";
    const entries = Object.entries(state.assets.images||{});
    if (!entries.length) {
      wrap.textContent="No image files found.";
      return;
    }
    entries.sort(([a],[b])=>a-b).forEach(([slot,ent])=>{
      const div=document.createElement("div");
      div.className="slot";
      div.style.width="180px";
      div.style.padding="0px";
      div.style.margin="0px";
      div.style.boxSizing="border-box";

      const img=document.createElement("img");
      img.className="thumb";
      img.src = ent.jpg || ent.png;
      img.style.cursor="pointer";
      // Clicking image pushes jpg if available, otherwise png
      const format = ent.jpg ? "jpg" : "png";
      img.addEventListener('click', () => pushImage(slot, format));

      const lbl=document.createElement("div");
      lbl.className="small";
      const filename = (ent.jpg || ent.png).split(/[\\\/]/).pop();
      lbl.textContent=filename;

      div.append(img,lbl);
      wrap.append(div);
    });
  }

  async function pushImage(slot,ext){
    if (!state.unit) return;
    const body = new URLSearchParams({ip:state.unit.ip,slot});
    if (ext) body.set("ext",ext);

    const j = await fetch(EP.pushImage,{method:"POST",body}).then(r=>r.json());
    if (!j.ok) return alert("Image push failed");

    // Wait for device to update, then refresh with retries
    await new Promise(r=>setTimeout(r,800));
    const url = DOWN_IMG(state.unit.ip)+`?t=${Date.now()}`;
    const img = $("#activeImage");
    img.onerror = () => {
      setTimeout(() => { img.src = DOWN_IMG(state.unit.ip)+`?t=${Date.now()}`; }, 500);
    };
    img.src = url;
  }

  function paintLogoSlots(){
  const wrap = document.getElementById("logoGrid");
  if (!wrap) return;

  wrap.innerHTML = "";

  const entries = Object.entries(state.assets.logos || {});
  if (!entries.length) {
    wrap.textContent = "No logo files found.";
    return;
  }

  entries.sort(([a],[b]) => a - b).forEach(([slot, ent]) => {

    // Container
    const cell = document.createElement("div");
    cell.style.display = "flex";
    cell.style.flexDirection = "column";
    cell.style.alignItems = "center";
    cell.style.textAlign = "center";
    cell.style.background = "var(--panel-2)";
    cell.style.border = "1px solid var(--border)";
    cell.style.padding = "0px";
    cell.style.margin = "0px";
    cell.style.borderRadius = "10px";
    cell.style.width = "180px";
    cell.style.boxSizing = "border-box";

    // Thumbnail
    const img = document.createElement("img");
    img.className = "thumb";
    img.src = ent.jpg || ent.png;
    img.style.cursor = "pointer";
    // Clicking image pushes jpg if available, otherwise png
    const format = ent.jpg ? "jpg" : "png";
    img.addEventListener('click', () => pushLogo(slot, format));

    const lbl = document.createElement("div");
    lbl.className = "small";
    lbl.style.marginTop = "4px";
    const filename = (ent.jpg || ent.png).split(/[\\\/]/).pop();
    lbl.textContent = filename;

    cell.append(img, lbl);
    wrap.append(cell);
  });
}


  async function pushLogo(slot,ext){
    if (!state.unit) return;
    
    // Check if logo is currently enabled
    const pill = $("#tgLogo .pill");
    const wasEnabled = pill ? pill.classList.contains("on") : false;
    
    // Disable logo if it was enabled
    if (wasEnabled) {
      pendingLogo = true;
      await rpc("OsdLogo.Set", {
        osdlogoenabled: false,
        displaylogo: {}
      });
      await new Promise(r=>setTimeout(r, 200));
      // Don't clear pendingLogo yet - keep it set during entire upload sequence
    }
    
    // Upload the logo
    const body = new URLSearchParams({ip:state.unit.ip,slot});
    if (ext) body.set("ext",ext);

    const j = await fetch(EP.pushLogo,{method:"POST",body}).then(r=>r.json());
    if (!j.ok) {
      // On error, clear pending flag and alert
      setTimeout(()=> pendingLogo = false, 1500);
      return alert("Logo push failed");
    }

    // Wait for device to update, then refresh with retries
    await new Promise(r=>setTimeout(r,800));
    const url = DOWN_LOGO(state.unit.ip)+`?t=${Date.now()}`;
    const img = $("#logoPreview");
    img.onerror = () => {
      setTimeout(() => { img.src = DOWN_LOGO(state.unit.ip)+`?t=${Date.now()}`; }, 500);
    };
    img.src = url;
    
    // Re-enable logo if it was enabled before
    if (wasEnabled) {
      await new Promise(r=>setTimeout(r, 200));
      await rpc("OsdLogo.Set", {
        osdlogoenabled: true,
        displaylogo: {}
      });
    }
    
    // Clear pending flag after entire sequence completes
    setTimeout(()=> pendingLogo = false, 1500);
  }

  /* ============================================================
     RTMP PANEL
  ============================================================ */
  function updateRtmpLed(active) {
	  // console.log("💡 updateRtmpLed CALLED WITH =", active);
	  const led = $("#rtmpLed");
	  if (!led) return;
	  led.style.background = active ? "#00ff5a" : "#222";  // green or dark
	}
  function paintRtmpRows(){
    const panel = $("#rtmpPanel");
    panel.innerHTML="";

    state.presets.forEach((p,idx)=>{
      const row=document.createElement("div");
      row.className="rtmp-row";
      row.dataset.index=idx;

      row.innerHTML=`
        <select data-protocol>
          <option value="rtmp://">rtmp://</option>
          <option value="rtmps://">rtmps://</option>
        </select>
        <input type="text" data-server placeholder="server" />
        <input type="text" data-key placeholder="key" />
        <input type="number" data-port class="mono w-port" min="0" max="65535" value="1935" />
        <input type="radio" name="rtmpApply" data-apply>
        ${idx>0 ? '<span class="icon trash" data-del>🗑️</span>' : ""}
      `;

      panel.append(row);

      row.querySelector("[data-protocol]").value=p.protocol||"rtmp://";
      row.querySelector("[data-server]").value=p.server||"";
      row.querySelector("[data-key]").value=p.key||"";
      row.querySelector("[data-port]").value=p.port||1935;

      row.oninput = () => {
        const i = Number(row.dataset.index);
        state.presets[i] = {
          protocol: row.querySelector("[data-protocol]").value,
          server:   row.querySelector("[data-server]").value.replace(/^rtmps?:\/\//i,""),
          key:      row.querySelector("[data-key]").value,
          port:     Number(row.querySelector("[data-port]").value)
        };
        saveGlobalState();
      };

      const del=row.querySelector("[data-del]");
      if (del) {
        // NOTE: onclick used here (not addEventListener) because paintRtmpRows() can be called
        // multiple times. onclick assignment replaces the previous handler, preventing duplicates.
        del.onclick = () => {
          state.presets.splice(idx,1);
          saveGlobalState();
          paintRtmpRows();
        };
      }
    });

    // FIX P2 #7: Replace global onclick with addEventListener
    const addStreamBtn = $("#addStream");
    if (addStreamBtn && !addStreamBtn.dataset.listenerAdded) {
      addStreamBtn.addEventListener('click', () => {
        state.presets.push({protocol:"rtmp://",server:"",key:"",port:1935});
        saveGlobalState();
        paintRtmpRows();
      });
      addStreamBtn.dataset.listenerAdded = "true";
    }
  }

  /* ============================================================
     RTMP Toggle + Apply
  ============================================================ */
  function wireRtmpToggle(){
    // FIX P2 #7: Replace onclick with addEventListener (wireRtmpToggle called once at init)
    const tgStreamBtn = $("#tgStream");
    if (tgStreamBtn && !tgStreamBtn.dataset.listenerAdded) {
      tgStreamBtn.addEventListener('click', async () => {
        if (pendingRtmp) return;
        if (!state.unit) return;
        
        const pill = $("#tgStream .pill");
        const isOn = pill.classList.contains("on");
        
        // If disabling, no preset needed
        if (isOn) {
          pill.classList.remove("on");
          pill.classList.add("off");
          updateRtmpLed(false);
          
          const selectedRadio = document.querySelector('input[type="radio"][data-apply]:checked');
          let rtmpParams = {};
          if (selectedRadio){
            const row = selectedRadio.closest(".rtmp-row");
            const protocol = row.querySelector("[data-protocol]").value;
            const server   = row.querySelector("[data-server]").value;
            const key      = row.querySelector("[data-key]").value;
            const port     = row.querySelector("[data-port]").value;
            rtmpParams = {
              url: `${protocol}${server.replace(/^rtmps?:\/\//i,"")}`,
              port: Number(port),
              streamname: key
            };
          }
          
          // Set pending flag BEFORE UI update
          pendingRtmp = true;
          
          try {
            await rpc("Rtmp.Set",{ enable:false, ...rtmpParams });
            
            // Restore saved profile after disabling RTMP
            if (savedProfileBeforeRtmp !== null && savedProfileBeforeRtmp !== undefined) {
              console.log("[RTMP Disable] Restoring profile to:", savedProfileBeforeRtmp);
              await new Promise(r=>setTimeout(r,300));
              await rpc("ProfileSelection.Set", { profilemode: savedProfileBeforeRtmp });
              console.log("[RTMP Disable] Profile restored");
              savedProfileBeforeRtmp = null; // Clear saved profile
            } else {
              console.warn("[RTMP Disable] No saved profile to restore");
            }
          } catch(e) {
            console.error("[RTMP Disable] Error:", e);
            pill.classList.remove("off");
            pill.classList.add("on");
            updateRtmpLed(true);
            alert("RTMP disable failed");
          } finally {
            setTimeout(() => pendingRtmp = false, 3000);
          }
        } else {
          // Enabling RTMP - check if preset is selected OR if RTMP is already loaded
          const selectedRadio = document.querySelector('input[type="radio"][data-apply]:checked');
          // Check if RTMP is already loaded on device (via last poll state)
          const rtmpLoaded = state.lastPollState && state.lastPollState.rtmp && state.lastPollState.rtmp.url ? true : false;
          
          if (!selectedRadio && !rtmpLoaded) {
            // No preset selected and no loaded RTMP config
            alert("Please select an RTMP preset first, or load RTMP settings on the device");
            return;
          }
          
          let rtmpParams = {};
          if (selectedRadio) {
            // Use selected preset
            const row = selectedRadio.closest(".rtmp-row");
            const protocol = row.querySelector("[data-protocol]").value;
            const server   = row.querySelector("[data-server]").value;
            const key      = row.querySelector("[data-key]").value;
            const port     = row.querySelector("[data-port]").value;
            rtmpParams = {
              url: `${protocol}${server.replace(/^rtmps?:\/\//i,"")}`,
              port: Number(port),
              streamname: key
            };
          }
          // If no preset selected, rtmpParams stays empty and device uses loaded config
          
          // Set pending flag BEFORE UI update
          pendingRtmp = true;
          
          try {
            // Save current profile before enabling RTMP
            // Try to get profile from lastPollState first
            let currentProfile = state.lastPollState?.profile?.encodeMode;
            
            // If not available, try to query it now
            if (currentProfile === undefined) {
              try {
                const profileResp = await rpc("ProfileSelection.Get", {});
                currentProfile = profileResp?.encodeMode;
                console.log("[RTMP Enable] Got current profile:", currentProfile);
              } catch(e) {
                console.warn("[RTMP Enable] Failed to get current profile:", e);
              }
            }
            
            // Save the profile for later restoration
            if (currentProfile !== undefined && currentProfile !== null) {
              savedProfileBeforeRtmp = currentProfile;
              console.log("[RTMP Enable] Saved profile for restoration:", savedProfileBeforeRtmp);
            } else {
              console.warn("[RTMP Enable] Could not get current profile, will not restore");
            }
            
            // Enable RTMP - immediate UI feedback
            pill.classList.remove("off");
            pill.classList.add("on");
            updateRtmpLed(true);
            
            // Before enabling RTMP, set profile to rtmp and HDCP to none
            await rpc("ProfileSelection.Set", { profilemode: 5 }); // 5 = rtmp profile
            await new Promise(r=>setTimeout(r,200));
            await rpc("HdcpAnnouncement.Set", { source: "in1", hdcpversion: "NONE" });
            await new Promise(r=>setTimeout(r,200));
            // Enable RTMP
            await rpc("Rtmp.Set",{ enable:true, ...rtmpParams });
          } catch(e) {
            // Revert UI on error
            pill.classList.remove("on");
            pill.classList.add("off");
            updateRtmpLed(false);
            savedProfileBeforeRtmp = null;
            alert("RTMP settings send failed");
          } finally {
            setTimeout(() => pendingRtmp = false, 3000);
          }
        }
      });
      tgStreamBtn.dataset.listenerAdded = "true";
    }
  }

  function wireRtmpApply(){
    document.addEventListener("change", async (e)=>{
      const t=e.target;
      if (!t.matches('input[type="radio"][data-apply]')) return;
      if (!state.unit) return;

      const row = t.closest(".rtmp-row");
      const protocol = row.querySelector("[data-protocol]").value;
      const server   = row.querySelector("[data-server]").value;
      const key      = row.querySelector("[data-key]").value;
      const port     = row.querySelector("[data-port]").value;

      try{
        const pill = document.querySelector("#tgStream .pill");
        const wasOn = pill && pill.classList.contains("on");
        // 1. Disable if currently on
        if (wasOn){
          await rpc("Rtmp.Set",{ enable:false });
          if (pill){ pill.classList.remove("on"); }
          updateRtmpLed(false);
          await new Promise(r=>setTimeout(r,250));
        }
        // 2. Send new settings while disabled
        await rpc("Rtmp.Set",{
          enable:false,
          url: `${protocol}${server.replace(/^rtmps?:\/\//i,"")}`,
          port: Number(port),
          streamname: key
        });
        // Set pollNow flag so next poll is immediate and prioritized
        pollNow = true;
        if (state.unit) pollUnit(state.unit.ip);
      } catch(e){
        alert("RTMP Apply failed");
        t.checked=false;
        // Attempt to reflect failure visually
        updateRtmpLed(false);
      }
    });
  }

  /* ============================================================
     AUDIO MUTE / VIDEO MUTE / AUDIO SOURCE
  ============================================================ */
  function wireMuteHandlers(){
    // Video Mute (checkbox + image toggle)
    const ck = $("#ckVideoMute");
    const tg = $("#tgImageStream");

    const apply = (muted) => {
      if (ck) ck.checked = muted;
      const pill = $("#tgImageStream .pill");
      if (pill) pill.classList.toggle("on",muted);
    };

	const sendMute = async (muted)=>{
	  pendingMute.video = true;
	  apply(muted);

	  await rpc("VideoInputMute.Set",{videomute:muted});
	  
	  setTimeout(()=> pendingMute.video = false, 1500);
	};


    if (ck) ck.onchange = () => sendMute(ck.checked);
    if (tg) tg.onclick  = () => sendMute(!ck.checked);
  }

	function wireAudioMuteHandlers(){
	  const ck = $("#ckAudioMute");
	  if (!ck) return;

	  ck.onchange = async () => {
		pendingMute.audio = true;

		await rpc("AudioInputMute.Set",{mute:ck.checked});

		// allow encoder to settle before poll overwrites UI
		setTimeout(()=> pendingMute.audio = false, 1500);
	  };
	}


  function wireAudioSourceHandlers(){
    const sel = $("#selAudioSource");
    if (!sel) return;
    
    let pending = false;

    sel.onchange = debounce(async () => {
      pending = true;
      try {
        await rpc("AudioInSelection.Set",{audiosource:sel.value});
        await new Promise(r=>setTimeout(r,500));
      } catch(e) {
        // console.error("Audio source failed",e);
      } finally {
        pending = false;
      }
    }, 300);
    
    wireAudioSourceHandlers.isPending = () => pending;
  }

  /* ============================================================
     LOGO TOGGLE + CONTROLS
  ============================================================ */
  function wireLogoHandlers(){
    // Logo toggle
    $("#tgLogo").onclick = async ()=>{
      if (pendingLogo) return;
      
      const pill = $("#tgLogo .pill");
      const en = !pill.classList.contains("on");
      
      // Set pending flag BEFORE UI update to prevent polling from overwriting
      pendingLogo = true;
      
      // Immediate UI feedback
      pill.classList.toggle("on", en);
      
      try {
        await rpc("OsdLogo.Set",{
          osdlogoenabled:en,
          displaylogo:{}
        });
      } catch (e) {
        // Revert on error
        pill.classList.toggle("on", !en);
      } finally {
        // Allow encoder to settle before poll overwrites UI
        setTimeout(()=> pendingLogo = false, 1500);
      }
    };

    // Function to calculate position based on preset and height
    function calculatePosition(preset, height) {
      switch(preset) {
        case 'upper-right':
          return { startx: 98 - height, starty: height };
        case 'lower-right':
          return { startx: 98 - height, starty: 100 };
        case 'upper-left':
          return { startx: 0, starty: height };
        case 'lower-left':
          return { startx: 0, starty: 100 };
        case 'custom':
          // Use manual input values
          const startx = parseInt($("#logoHorizontal")?.value || 88);
          const starty = parseInt($("#logoVertical")?.value || 10);
          return { startx, starty };
        default:
          return { startx: 98 - height, starty: height }; // default upper-right
      }
    }

    // Function to send logo settings
    async function applyLogoSettings() {
      if (!state.unit) return;
      
      pendingLogo = true;
      
      const transparency = parseInt($("#logoTransparency")?.value || 255);
      const height = parseInt($("#logoHeight")?.value || 10);
      const position = $("#logoPosition")?.value || 'upper-right';
      
      // Preserve current enabled state
      const pill = $("#tgLogo .pill");
      const currentEnabled = pill ? pill.classList.contains("on") : true;
      
      const pos = calculatePosition(position, height);
      
      await rpc("OsdLogo.Set", {
        osdlogoenabled: currentEnabled,
        displaylogo: {
          backtransparency: transparency,
          startpostion: pos,
          logohight: height
        }
      });
      
      // Allow encoder to settle before poll overwrites UI
      setTimeout(()=> pendingLogo = false, 1500);
    }

    // Wire up transparency input
    const transInput = $("#logoTransparency");
    if (transInput) {
      transInput.onchange = applyLogoSettings;
    }

    // Wire up height input
    const heightInput = $("#logoHeight");
    if (heightInput) {
      heightInput.onchange = applyLogoSettings;
    }

    // Wire up position dropdown
    const posSelect = $("#logoPosition");
    if (posSelect) {
      posSelect.onchange = async () => {
        // Show/hide coordinate inputs based on selection
        const coordRow = $("#logoCoordinatesRow");
        if (coordRow) {
          coordRow.style.display = posSelect.value === 'custom' ? 'flex' : 'none';
        }
        
        // Only apply settings if not switching to custom mode
        // (let user enter values when in custom mode)
        if (posSelect.value !== 'custom') {
          await applyLogoSettings();
        }
      };
    }

    // Wire up horizontal/vertical coordinate inputs
    const horizInput = $("#logoHorizontal");
    if (horizInput) {
      horizInput.onchange = applyLogoSettings;
    }
    
    const vertInput = $("#logoVertical");
    if (vertInput) {
      vertInput.onchange = applyLogoSettings;
    }

    // Store function to detect position preset from coordinates
    wireLogoHandlers.detectPreset = (startx, starty, height) => {
      if (startx === 0 && starty === height) return 'upper-left';
      if (startx === 0 && starty === 100) return 'lower-left';
      if (startx === 98 - height && starty === 100) return 'lower-right';
      if (startx === 98 - height && starty === height) return 'upper-right';
      return 'custom'; // Custom coordinates
    };
  }

  /* ============================================================
     HDCP
  ============================================================ */
  function wireHdcpHandler(){
    const sel = $("#selHdcp");
    if (!sel) return;
    
    let pending = false;
    
    sel.onchange = async () => {
      if (!state.unit) return;
      pending = true;
      try {
        await rpc("HdcpAnnouncement.Set", {
          source: "in1",
          hdcpversion: sel.value
        });
        await new Promise(r=>setTimeout(r,500));
      } finally {
        pending = false;
      }
    };
    
    wireHdcpHandler.isPending = () => pending;
  }

  /* ============================================================
     PROFILE
  ============================================================ */
  function wireProfileHandler(){
    const sel = $("#selProfile");
    if (!sel) return;
    
    let pending = false;
    
    sel.onchange = async () => {
      if (!state.unit) return;
      pending = true;
      try {
        await rpc("ProfileSelection.Set", {
          profilemode: Number(sel.value)
        });
        await new Promise(r=>setTimeout(r,500));
      } catch(e) {
        // console.error("Profile set failed:", e.message || e);
      } finally {
        pending = false;
      }
    };
    
    wireProfileHandler.isPending = () => pending;
  }

  /* ============================================================
     VIDEO ENCODE (Res / FPS / Bitrate)
  ============================================================ */
  function wireVideoEncodeHandlers(){
    const res = $("#selResolution");
    const fps = $("#selFramerate");
    const br  = $("#inpBitrate");
    const mode = $("#selMode");
    
    let pendingUpdate = false;

    const send = debounce(async () => {
      const b = Number(br.value||0);
      if (isNaN(b) || b<128) return;

      // Get current encode state to preserve values we're not changing
      const currentEncode = state.lastPollState?.encode || {};
      
      pendingUpdate = true;
      try {
        console.log("[BITRATE SET] MainStreamVideoEncode.Set called with:", {
          chn: currentEncode.chn || 0,
          encodetype: currentEncode.encodetype || "h264",
          rctype: mode ? mode.value : (currentEncode.rctype || "cbr"),
          framerate: Number(fps.value),
          gop: currentEncode.gop || 60,
          bitrate: b,
          resolution: res.value
        });
        await rpc("MainStreamVideoEncode.Set",{
          chn: currentEncode.chn || 0,
          encodetype: currentEncode.encodetype || "h264",
          rctype: mode ? mode.value : (currentEncode.rctype || "cbr"),
          framerate: Number(fps.value),
          gop: currentEncode.gop || 60,
          bitrate: b,
          resolution: res.value
        });
        // Wait a bit for device to apply and polling to catch up
        await new Promise(r=>setTimeout(r,500));
      } finally {
        pendingUpdate = false;
      }
    },300);

    res.onchange = send;
    fps.onchange = send;
    if (mode) mode.onchange = send;
    br.oninput = ()=> {
      const clean = br.value.replace(/[^\d]/g,"").slice(0,6);
      const num = Math.min(60000, Number(clean||0));
      br.value = num || "";
      if (num>=128) send();
    };
    
    // Expose pendingUpdate flag for applyLiveState to check
    wireVideoEncodeHandlers.isPending = () => pendingUpdate;
  }

  /* ============================================================
     TICKER: Color Picker
  ============================================================ */
  function initTickerColor(){
    const picker = $("#tickerColor");
    if (!picker) return;

    let [r,g,b] = (state.tickerColor || "255.255.255").split(".").map(n => Number(n));

	if (
	  Number.isNaN(r) || Number.isNaN(g) || Number.isNaN(b) ||
	  r < 0 || r > 255 ||
	  g < 0 || g > 255 ||
	  b < 0 || b > 255
	) {
	  // console.warn("Invalid tickerFontColor, resetting.");
	  r = g = b = 255;
	  state.tickerColor = "255.255.255";
	  localStorage.setItem("tickerFontColor", state.tickerColor);
	}

	picker.value = rgbToHex(r,g,b);

    let colorUpdateTimeout = null;
    picker.oninput = e => {
      const rgb = hexToRgbTriplet(e.target.value);
      const trip = `${rgb.r}.${rgb.g}.${rgb.b}`;
      state.tickerColor = trip;
      localStorage.setItem("tickerFontColor",trip);
      saveGlobalState();

      // Debounce device updates - only send after user stops moving slider
      clearTimeout(colorUpdateTimeout);
      colorUpdateTimeout = setTimeout(async () => {
        // If ticker is currently enabled, send color change to device
        if (state.ticker.enabled && state.ticker.pending && state.unit) {
          try {
            const cfg = state.ticker.pending;
            await rpc("OsdText.Set",{
              osdindex:0,
              osdtextenabled:true,
              displaytext:{
                content:cfg.text,
                fontcolor:trip,
                backcolor:"0.0.0",
                fonttransparency:255,
                backtransparency:0,
                startpostion:{startx:0,starty:cfg.starty},
                fontsize:{fonth:cfg.fonth,fontw:80},
                displayscrolleffects:{enable:true,iterations:cfg.cycles,speed:cfg.speed,direction:cfg.direction}
              }
            });
          } catch(e) {
            console.error("Failed to update ticker color on device:", e);
          }
        }
      }, 300); // Wait 300ms after user stops moving before sending
    };
  }

  /* ============================================================
     TICKER: Rows
  ============================================================ */
function paintTickerRows(){
    // 🔥 Normalize ticker object — prevents undefined.forEach crash
    if (!state.ticker || typeof state.ticker !== "object") {
      state.ticker = { dir:"rtl", rows:[], enabled:false };
    }
    if (!Array.isArray(state.ticker.rows)) {
      state.ticker.rows = [];
    }

    const box = $("#tickerRows");
    box.innerHTML = `
      // Column headers are now in HTML, not generated here
    `;


    state.ticker.rows.forEach((t,idx)=>{

      const row=document.createElement("div");
      row.className="ticker-row";
      row.dataset.index=idx;

      // SECURITY FIX (P3 #3): Use createElement to avoid XSS from user ticker text
      row.innerHTML=`
        <input type="radio" name="tickerApply">
      `;
      
      const textInput = document.createElement("input");
      textInput.type = "text";
      textInput.setAttribute("data-text", "");
      textInput.placeholder = "Text";
      textInput.value = t.text || "";  // Safe: .value auto-escapes
      textInput.style.flex = "1";
      row.appendChild(textInput);
      
      const selectPos = document.createElement("select");
      selectPos.setAttribute("data-pos", "");
      selectPos.innerHTML = `
        <option value="top">top</option>
        <option value="middle">middle</option>
        <option value="bottom">bottom</option>
      `;
      selectPos.value = t.pos || "bottom";
      row.appendChild(selectPos);
      
      const speedInput = document.createElement("input");
      speedInput.type = "number";
      speedInput.setAttribute("data-speed", "");
      speedInput.min = "0";
      speedInput.max = "40";
      speedInput.value = t.speed || 8;
      speedInput.className = "w-2dig";
      speedInput.style.width = "2ch";
      row.appendChild(speedInput);
      
      const cyclesInput = document.createElement("input");
      cyclesInput.type = "number";
      cyclesInput.setAttribute("data-cycles", "");
      cyclesInput.min = "0";
      cyclesInput.max = "99";
      cyclesInput.value = t.cycles || 0;
      cyclesInput.className = "w-2dig";
      cyclesInput.style.width = "2ch";
      row.appendChild(cyclesInput);
      
      const fonthInput = document.createElement("input");
      fonthInput.type = "number";
      fonthInput.setAttribute("data-fonth", "");
      fonthInput.min = "1";
      fonthInput.max = "100";
      fonthInput.value = t.fonth || 10;
      fonthInput.className = "w-2dig";
      fonthInput.style.width = "2ch";
      fonthInput.title = "Font Height";
      row.appendChild(fonthInput);
        // Direction dropdown per row
        const dirSelect = document.createElement("select");
        dirSelect.setAttribute("data-dir", "");
        dirSelect.innerHTML = `<option value="0">→</option><option value="1">←</option>`;
        dirSelect.value = typeof t.direction !== 'undefined' ? String(t.direction) : "0";
        row.appendChild(dirSelect);

        // Color picker per row
        const colorInput = document.createElement("input");
        colorInput.type = "color";
        colorInput.setAttribute("data-color", "");
        colorInput.value = t.color || "#ff7000";
        colorInput.style.width = "28px";
        colorInput.style.height = "28px";
        colorInput.style.border = "none";
        colorInput.style.cursor = "pointer";
        colorInput.style.borderRadius = "4px";
        colorInput.style.background = "transparent";
        colorInput.style.padding = "0";
        row.appendChild(colorInput);
      
      const deleteSpan = document.createElement("span");
      if (idx > 0) {
        deleteSpan.className = "icon trash";
        deleteSpan.setAttribute("data-del", "");
        deleteSpan.textContent = "🗑️";
      } else {
        deleteSpan.style.width = "2.5em";
        deleteSpan.style.display = "inline-block";
      }
      row.appendChild(deleteSpan);
      
      box.append(row);

      row.oninput = () => {
        const i=idx;
        const text=row.querySelector("[data-text]").value;
        const pos=row.querySelector("[data-pos]").value;
        const speed=Math.min(40,Math.max(0,Number(row.querySelector("[data-speed]").value)));
        const cycles=Math.min(99,Math.max(0,Number(row.querySelector("[data-cycles]").value)));
        const fonth=Math.min(100,Math.max(1,Number(row.querySelector("[data-fonth]").value)));
        const direction=Number(row.querySelector('[data-dir]').value);
        const color=row.querySelector('[data-color]').value;

        state.ticker.rows[i]={text,pos,speed,cycles,fonth,direction,color};
        saveGlobalState();

        // Debounce device updates when ticker is active
        if (state.ticker.enabled && state.ticker.pending && row.querySelector('input[type="radio"]').checked) {
          clearTimeout(row.deviceUpdateTimeout);
          row.deviceUpdateTimeout = setTimeout(async () => {
            try {
              let starty = 0;
              if (pos === "top") starty = 0;
              else if (pos === "middle") starty = 45;
              else if (pos === "bottom") starty = 100 - fonth;

              // Update pending config with new values
              state.ticker.pending = {
                direction,
                text,
                pos,
                speed,
                cycles,
                fonth,
                starty,
                color
              };

              // Send to device after user stops editing
              await rpc("OsdText.Set",{
                osdindex:0,
                osdtextenabled:true,
                displaytext:{
                  content:text,
                  fontcolor:color,
                  backcolor:"0.0.0",
                  fonttransparency:255,
                  backtransparency:0,
                  startpostion:{startx:0,starty:starty},
                  fontsize:{fonth:fonth,fontw:80},
                  displayscrolleffects:{enable:true,iterations:cycles,speed:speed,direction:direction}
                }
              });
            } catch(e) {
              console.error("Failed to update ticker on device:", e);
            }
          }, 300); // Wait 300ms after edits stop before sending
        }
      };

      // APPLY - Function to send ticker from a row to device immediately
      const applyTickerFromRow = async (rowElement) => {
        if (!state.unit) return;

        const direction = Number(rowElement.querySelector('[data-dir]').value);
        const color = rowElement.querySelector('[data-color]').value;
        const text = rowElement.querySelector("[data-text]").value;
        const pos = rowElement.querySelector("[data-pos]").value;
        const speed = Number(rowElement.querySelector("[data-speed]").value);
        const cycles = Number(rowElement.querySelector("[data-cycles]").value);
        const fonth = Math.min(100,Math.max(1,Number(rowElement.querySelector("[data-fonth]").value)));

        let starty = 0;
        if (pos === "top") starty = 0;
        else if (pos === "middle") starty = 45;
        else if (pos === "bottom") starty = 100 - fonth;

        // Store the pending ticker config
        state.ticker.pending = {
          direction,
          text,
          pos,
          speed,
          cycles,
          fonth,
          starty,
          color
        };

        // Send to device immediately (keep current enabled/disabled state)
        try {
          await rpc("OsdText.Set",{
            osdindex:0,
            osdtextenabled:state.ticker.enabled || false,
            displaytext:{
              content:text,
              fontcolor:color,
              backcolor:"0.0.0",
              fonttransparency:255,
              backtransparency:0,
              startpostion:{startx:0,starty:starty},
              fontsize:{fonth:fonth,fontw:80},
              displayscrolleffects:{enable:true,iterations:cycles,speed:speed,direction:direction}
            }
          });
        } catch(e) {
          console.error("Failed to send ticker to device:", e);
        }

        saveGlobalState();
      };

      // Store reference to apply function for use by toggle
      row.applyTickerFromRow = applyTickerFromRow;

      row.querySelector('input[type="radio"]').onchange = async () => {
        if (!state.unit) return;
        
        try {
          await applyTickerFromRow(row);
        } catch(e) {
          console.error("Failed to load ticker:", e);
        }
      };

      // DELETE
      const del = row.querySelector("[data-del]");
      if (del) {
        del.onclick = () => {
          state.ticker.rows.splice(idx,1);
          saveGlobalState();
          paintTickerRows();
        };
      }
    });

    $("#addTicker").onclick = ()=>{
      state.ticker.rows.push({text:"",pos:"bottom",speed:8,cycles:0});
      saveGlobalState();
      paintTickerRows();
    };

  }

  /* ============================================================
     TICKER: START / STOP
  ============================================================ */
  /* ============================================================
   TICKER STOP BUTTON — SINGLE-FUNCTION BEHAVIOR
   ============================================================ */
	function wireTickerToggle(){
	  const tg = $("#tgTicker");
	  if (!tg) return;

	  tg.onclick = async () => {
		if (pendingTicker) return;
		if (!state.unit) return;

		const pill = $("#tgTicker .pill");
		const isOn = pill.classList.contains("on");

		// Set pending flag BEFORE UI update
		pendingTicker = true;

		if (isOn) {
		  // Turn ticker OFF - send full structure to keep settings intact on device
		  pill.classList.remove("on");

		  try {
			state.ticker.enabled = false;
			// Send full displaytext structure even when disabling, to maintain device state
			const cfg = state.ticker.pending || {text:"", pos:"bottom", speed:8, cycles:0, fonth:10, starty:100-10, direction:0};
			await rpc("OsdText.Set",{
			  osdindex:0,
			  osdtextenabled:false,
			  displaytext:{
				content:cfg.text,
				fontcolor:state.tickerColor,
				backcolor:"0.0.0",
				fonttransparency:255,
				backtransparency:0,
				startpostion:{startx:0,starty:cfg.starty},
				fontsize:{fonth:cfg.fonth,fontw:80},
				displayscrolleffects:{enable:true,iterations:cfg.cycles,speed:cfg.speed,direction:cfg.direction}
			  }
			});
			saveGlobalState();
		  } catch(e) {
			pill.classList.add("on");
		  } finally {
			setTimeout(() => pendingTicker = false, 3000);
		  }
		} else {
		  // Turn ticker ON - use the pending ticker settings
		  if (!state.ticker.pending) {
			alert("Please select a ticker first (check a radio button)");
			return;
		  }

		  // Set pending flag AFTER validation guard
		  pendingTicker = true;

		  pill.classList.add("on");

		  try {
            const cfg = state.ticker.pending;
			await rpc("OsdText.Set",{
			  osdindex:0,
			  osdtextenabled:true,
			  displaytext:{
				content:cfg.text,
				fontcolor:state.tickerColor,
				backcolor:"0.0.0",
				fonttransparency:255,
				backtransparency:0,
				startpostion:{startx:0,starty:cfg.starty},
				fontsize:{fonth:cfg.fonth,fontw:80},
				displayscrolleffects:{enable:true,iterations:cfg.cycles,speed:cfg.speed,direction:cfg.direction}
			  }
			});
            state.ticker.enabled = true;
            saveGlobalState();
		  } catch(e) {
			pill.classList.remove("on");
		  } finally {
			setTimeout(() => pendingTicker = false, 3000);
		  }
		}
	  };
	}
					

  /* ============================================================
     VALIDATE RPC RESPONSE (Detect server response mixing bug)
  ============================================================ */
  function validateResponse(method, result) {
    if (!result || typeof result !== 'object') return true; // null/string ok
    
    // Check for wrong response signatures
    switch(method) {
      case 'RtmpRunStatus.Get':
        // Must have 'runstatus' property, NOT 'enable' or 'bitrate' or 'displaylogo'
        return result.hasOwnProperty('runstatus') && 
               !result.hasOwnProperty('enable') &&
               !result.hasOwnProperty('bitrate') &&
               !result.hasOwnProperty('displaylogo');
      
      case 'Rtmp.Get':
        // Must have 'enable', NOT 'runstatus' or 'bitrate'
        return result.hasOwnProperty('enable') &&
               !result.hasOwnProperty('runstatus') &&
               !result.hasOwnProperty('bitrate');
      
      case 'MainStreamVideoEncode.Get':
        // Must have 'bitrate', NOT 'runstatus' or 'enable'
        return result.hasOwnProperty('bitrate') &&
               !result.hasOwnProperty('runstatus');
      
      case 'OsdLogo.Get':
        // Must have 'displaylogo', NOT 'runstatus' or 'enable'
        return result.hasOwnProperty('displaylogo') &&
               !result.hasOwnProperty('runstatus') &&
               !result.hasOwnProperty('enable');
      
      default:
        return true; // Don't validate other methods
    }
  }

  /* ============================================================
     FETCH UNIT STATE (Polling)
  ============================================================ */
  
  // Fast-track RTMP poll - fetch immediately for green indicator
  async function quickPollRtmpStatus(ip) {
    try {
      const body = {
        ip,
        method: "Rtmp.Get",
        jsonrpc: "2.0",
        id: "rtmp_quick_poll",
        params: {}
      };
      const txt = await fetch(EP.rpc, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body)
      }).then(r => r.text());
      
      const j = JSON.parse(txt);
      if (j.result && typeof j.result === 'object') {
        const rtmpData = j.result;
        
        // Check RtmpRunStatus for actual streaming state
        const statusBody = {
          ip,
          method: "RtmpRunStatus.Get",
          jsonrpc: "2.0",
          id: "rtmp_status_quick",
          params: {}
        };
        
        let isStreaming = false;
        try {
          const statusTxt = await fetch(EP.rpc, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(statusBody)
          }).then(r => r.text());
          
          const statusJ = JSON.parse(statusTxt);
          if (statusJ.result && statusJ.result.runstatus) {
            isStreaming = statusJ.result.runstatus.toLowerCase() === "active";
          }
        } catch (e) {
          console.debug("[quickPollRtmpStatus] Status check failed:", e.message);
        }
        
        // Apply UI immediately (don't wait for full poll)
        setTimeout(() => {
          if (!pendingRtmp) {
            // Update toggle and LED
            const toggle = document.querySelector("#tgStream .pill");
            if (toggle && rtmpData.enable !== undefined) {
              if (toggle.classList.contains("on") !== !!rtmpData.enable)
                toggle.classList.toggle("on", rtmpData.enable);
              if (toggle.classList.contains("off") === !!rtmpData.enable)
                toggle.classList.toggle("off", !rtmpData.enable);
            }
            
            const led = $("#rtmpLed");
            if (led) {
              led.style.background = isStreaming ? "#00ff5a" : "#222";
            }
            
            // Update URL display
            const urlDisplay = $("#rtmpUrlDisplay");
            if (urlDisplay && rtmpData.url && urlDisplay.textContent !== rtmpData.url) {
              urlDisplay.textContent = rtmpData.url;
              urlDisplay.title = rtmpData.url;
            } else if (urlDisplay && !rtmpData.url && urlDisplay.textContent !== "—") {
              urlDisplay.textContent = "—";
              urlDisplay.title = "";
            }
            
            // Update top streaming indicator
            const unit = state.encs.find(e => e.ip === ip);
            if (unit) {
              encoderStreamStatus.set(ip, {
                hostname: unit.hostname || ip,
                ip: ip,
                isStreaming: isStreaming
              });
              updateStreamingDisplay();
            }
            
            console.log("⚡ Quick RTMP UI updated immediately (enable=" + rtmpData.enable + ", streaming=" + isStreaming + ")");
          }
        }, 0);
        
        return rtmpData;
      }
    } catch (e) {
      console.debug("[quickPollRtmpStatus] Failed:", e.message);
    }
    return null;
  }
  
  async function fetchUnitState(ip){
    const startTime = performance.now();
    
    // Start quick RTMP poll immediately (don't wait for it)
    const rtmpPromise = quickPollRtmpStatus(ip);
    
    const methods = [
      { key:"ticker",      method:"OsdText.Get", params:{osdindex:0} },
      { key:"videoMute",   method:"VideoInputMute.Get", params:{}},
      { key:"audioMute",   method:"AudioInputMute.Get", params:{}},
      { key:"logo",        method:"OsdLogo.Get", params:{}},
      { key:"encode",      method:"MainStreamVideoEncode.Get", params:{}},
      { key:"audioSource", method:"AudioInSelection.Get", params:{}},
	  { key:"rtmpStatus",  method:"RtmpRunStatus.Get", params:{} },
	  { key:"hdcp",        method:"HdcpAnnouncement.Get", params:{} },
	  { key:"profile",     method:"ProfileSelection.Get", params:{} }
    ];

    const results={};
    
    // SEQUENTIAL APPROACH with ID validation
    // Poll one at a time with delays and validate response IDs match request IDs
    for (const m of methods){
      try {
        const methodStart = performance.now();
        const body = {
          ip,
          method:m.method,
          jsonrpc:"2.0",
          id:`${m.key}_poll`,
          params:{...m.params}
        };
        console.debug(`[fetchUnitState] Polling ${m.method} from ${ip}...`);
        const txt = await fetch(EP.rpc,{
          method:"POST",
          headers:{"Content-Type":"application/json"},
          body:JSON.stringify(body)
        }).then(r=>r.text());

        const j = JSON.parse(txt);
        console.debug(`[fetchUnitState] Response for ${m.method}:`, j);
        
        // Validate response ID matches request ID to detect response mixing
        if (j.id && j.id !== body.id) {
          console.warn(`⚠️ Response ID mismatch for ${m.method}! Expected '${body.id}', got '${j.id}'. Skipping.`);
          results[m.key] = null;
          continue; // Skip this corrupted response
        }
        
        // Check for JSON-RPC error responses
        if (j.error) {
          console.warn(`[${m.method}] Device error: ${j.error.message || JSON.stringify(j.error)}`);
          results[m.key] = null;
          continue;
        }
        
        let result = j.result || null;
        
        // Debug log for empty/null responses (help diagnose issues)
        if (!result || (typeof result === 'object' && Object.keys(result).length === 0)) {
          console.debug(`[${m.method}] Response: ${JSON.stringify(j).substring(0, 200)}...`);
        }

        // Robust parsing for MainStreamVideoEncode.Get
        if (m.method === "MainStreamVideoEncode.Get") {
          // Double-encoded JSON string fix
          if (typeof result === 'string') {
            try {
              result = JSON.parse(result);
            } catch {
              // If parsing fails, keep as string
            }
          }
          
          // Accept response if it's an object, even if missing some fields
          // Fill in missing fields with defaults rather than rejecting entire response
          if (result && typeof result === 'object') {
            // Merge with defaults for any missing fields
            result = {
              bitrate: result.bitrate || '',
              resolution: result.resolution || 'auto',
              framerate: result.framerate || '0',
              rctype: result.rctype || 'cbr',
              ...result  // Keep all other fields from device response
            };
          } else {
            // Only use fallback if result is truly null/undefined/invalid
            console.warn("[MainStreamVideoEncode.Get] Missing/corrupt data, using lastPollState.encode if available", result);
            if (state.lastPollState && state.lastPollState.encode) {
              result = { ...state.lastPollState.encode };
            } else {
              // Fallback to defaults
              result = { bitrate: '', resolution: 'auto', framerate: '0', rctype: 'cbr' };
            }
          }
        } else if (m.method === "HdcpAnnouncement.Get") {
          // Parse double-encoded if needed
          if (typeof result === 'string') {
            try { result = JSON.parse(result); } catch { }
          }
          // Accept if it's an object (with or without hdcpannouncements field)
          if (result && typeof result !== 'object') {
            console.warn(`[${m.method}] Unexpected response type: ${typeof result}, discarding`);
            result = null;
          }
        } else if (m.method === "Rtmp.Get") {
          // Parse double-encoded if needed
          if (typeof result === 'string') {
            try { result = JSON.parse(result); } catch { }
          }
          // Accept if it's an object with enable field, or any object
          if (result && typeof result !== 'object') {
            console.warn(`[${m.method}] Unexpected response type: ${typeof result}, discarding`);
            result = null;
          }
        } else {
          // Some methods return double-encoded JSON strings - parse again if needed
          if (typeof result === 'string') {
            try {
              result = JSON.parse(result);
            } catch {
              // If parsing fails, keep as string
            }
          }
        }

        const methodTime = performance.now() - methodStart;
        if (methodTime > 200) {
          console.log(`⏱️ ${m.method}: ${methodTime.toFixed(0)}ms`);
        }

        results[m.key] = result;
      } catch (e) {
        console.log(`❌ ${m.method} failed:`, e.message);
        results[m.key]=null;
      }
      // Delay between requests to prevent server mixing responses (increased to 100ms for reliability)
      await new Promise(r=>setTimeout(r,100));
    }
    
    // Merge in the quick RTMP poll result (which should be ready by now)
    try {
      const quickRtmp = await rtmpPromise;
      if (quickRtmp) {
        results.rtmp = quickRtmp;
        console.log(`✅ Quick RTMP poll merged:`, quickRtmp);
      }
    } catch (e) {
      console.debug("[fetchUnitState] Quick RTMP merge failed:", e.message);
    }
    
    const totalTime = performance.now() - startTime;
    console.log(`🎯 fetchUnitState TOTAL: ${totalTime.toFixed(0)}ms (sequential, 100ms delays)`);
    
    return results;
  }

  /* ============================================================
     APPLY POLL STATE → UI
  ============================================================ */
  function applyLiveState(st){
    // Store latest poll state for reference by handlers
    state.lastPollState = st;
    
    // RTMP
    if (st.rtmp && st.rtmp.enable !== undefined && !pendingRtmp) {
        const toggle = document.querySelector("#tgStream .pill");
        if (toggle) {
          if (toggle.classList.contains("on") !== !!st.rtmp.enable)
            toggle.classList.toggle("on", st.rtmp.enable);
          if (toggle.classList.contains("off") === !!st.rtmp.enable)
            toggle.classList.toggle("off", !st.rtmp.enable);
        }
        // Update RTMP URL display only if changed
        const urlDisplay = $("#rtmpUrlDisplay");
        if (urlDisplay && st.rtmp.url && urlDisplay.textContent !== st.rtmp.url) {
          urlDisplay.textContent = st.rtmp.url;
          urlDisplay.title = st.rtmp.url; // Full URL on hover
        } else if (urlDisplay && !st.rtmp.url && urlDisplay.textContent !== "—") {
          urlDisplay.textContent = "—";
          urlDisplay.title = "";
        }
    }
    // Ticker
    if (st.ticker && typeof st.ticker.osdtextenabled==="boolean" && !pendingTicker){
      state.ticker.enabled = st.ticker.osdtextenabled;
      const pill=$("#tgTicker .pill");
      if (pill && pill.classList.contains("on") !== !!st.ticker.osdtextenabled)
        pill.classList.toggle("on", st.ticker.osdtextenabled);
      
      // Display loaded ticker text
      const tickerDisplay = $("#tickerLoadedText");
      if (tickerDisplay && st.ticker.displaytext && st.ticker.displaytext.content) {
        tickerDisplay.textContent = st.ticker.displaytext.content;
        tickerDisplay.title = st.ticker.displaytext.content; // Full text on hover
      } else if (tickerDisplay) {
        tickerDisplay.textContent = "";
        tickerDisplay.title = "";
      }
    }
	// 🔥 Guard: ignore poll results too soon after user action
  if (pendingMute.video || pendingMute.audio) {
    // console.log("⏳ Skipping mute UI update due to pendingMute");
    // do NOT update UI from poll yet
  } else {
    // ---- VIDEO MUTE ----
    if (st.videoMute){
      const muted = !!st.videoMute.videomute;
      const ck=$("#ckVideoMute");
      if (ck && ck.checked !== muted && document.activeElement !== ck) ck.checked = muted;
      const pill=$("#tgImageStream .pill");
      if (pill && pill.classList.contains("on") !== muted) pill.classList.toggle("on", muted);
    }

    // ---- AUDIO MUTE ----
    if (st.audioMute){
      const ck=$("#ckAudioMute");
      if (ck && ck.checked !== !!st.audioMute.mute && document.activeElement !== ck) ck.checked = !!st.audioMute.mute;
    }
  }
    // Logo
    if (st.logo && !pendingLogo){
      const pill=$("#tgLogo .pill");
      if (pill && pill.classList.contains("on") !== !!st.logo.osdlogoenabled)
        pill.classList.toggle("on", !!st.logo.osdlogoenabled);
      
      // Update transparency input
      const transInput = $("#logoTransparency");
      if (transInput && st.logo.displaylogo?.backtransparency !== undefined) {
        const trans = st.logo.displaylogo.backtransparency;
        if (transInput.value !== String(trans) && document.activeElement !== transInput) {
          transInput.value = trans;
        }
      }
      
      // Update height input
      const heightInput = $("#logoHeight");
      if (heightInput && st.logo.displaylogo?.logohight !== undefined) {
        const height = st.logo.displaylogo.logohight;
        if (heightInput.value !== String(height) && document.activeElement !== heightInput) {
          heightInput.value = height;
        }
      }
      
      // Update horizontal/vertical inputs
      const horizInput = $("#logoHorizontal");
      const vertInput = $("#logoVertical");
      if (st.logo.displaylogo?.startpostion) {
        const { startx, starty } = st.logo.displaylogo.startpostion;
        if (horizInput && horizInput.value !== String(startx) && document.activeElement !== horizInput) {
          horizInput.value = startx;
        }
        if (vertInput && vertInput.value !== String(starty) && document.activeElement !== vertInput) {
          vertInput.value = starty;
        }
      }
      
      // Update position dropdown based on current coordinates
      const posSelect = $("#logoPosition");
      if (posSelect && st.logo.displaylogo?.startpostion && st.logo.displaylogo?.logohight !== undefined) {
        // Don't change dropdown if user is in custom mode (editing coordinates)
        if (posSelect.value !== 'custom' && document.activeElement !== posSelect) {
          const { startx, starty } = st.logo.displaylogo.startpostion;
          const height = st.logo.displaylogo.logohight;
          const detected = wireLogoHandlers.detectPreset(startx, starty, height);
          if (posSelect.value !== detected) {
            posSelect.value = detected;
            // Hide coordinates row if switched away from custom
            const coordRow = $("#logoCoordinatesRow");
            if (coordRow) {
              coordRow.style.display = detected === 'custom' ? 'flex' : 'none';
            }
          }
        }
      }
    }

    // Audio source
    if (st.audioSource && !wireAudioSourceHandlers.isPending?.()){
      const sel=$("#selAudioSource");
      if (sel && sel.value !== (st.audioSource.audiosource || "hdmiin") && document.activeElement !== sel)
        sel.value = st.audioSource.audiosource || "hdmiin";
    }

    // Encode settings
    if (st.encode && !wireVideoEncodeHandlers.isPending?.()){
      const r=String(st.encode.resolution||"auto");
      const f=String(st.encode.framerate||"0");
      const b=String(st.encode.bitrate||"");
      const rc=String(st.encode.rctype||"cbr");

      // Debug log for video output info
      console.log("[VIDEO OUTPUT INFO] Poll state:", {
        resolution: r,
        framerate: f,
        bitrate: b,
        rctype: rc,
        encode: st.encode
      });

      const selRes = $("#selResolution");
      if (selRes && selRes.value !== r && document.activeElement !== selRes) selRes.value = r;
      const selFps = $("#selFramerate");
      if (selFps && selFps.value !== f && document.activeElement !== selFps) selFps.value = f;
      const modeSelect = $("#selMode");
      if (modeSelect && modeSelect.value !== rc && document.activeElement !== modeSelect) modeSelect.value = rc;
      // Only update bitrate if input is not focused and changed
      const bitrateInput = $("#inpBitrate");
      if (bitrateInput && document.activeElement !== bitrateInput && bitrateInput.value !== b) {
        bitrateInput.value = b;
      }
    }

    // HDCP
    if (st.hdcp && st.hdcp.hdcpannouncements && st.hdcp.hdcpannouncements[0] && !wireHdcpHandler.isPending?.()){
      const hdcpSel = $("#selHdcp");
      const version = st.hdcp.hdcpannouncements[0].hdcpversion || "NONE";
      if (hdcpSel && hdcpSel.value !== version && document.activeElement !== hdcpSel) {
        hdcpSel.value = version;
      }
    }

    // Profile
    if (st.profile && st.profile.encodeMode !== undefined && !wireProfileHandler.isPending?.()){
      const profileSel = $("#selProfile");
      const modeVal = String(st.profile.encodeMode);
        if (profileSel && profileSel.value !== modeVal && document.activeElement !== profileSel) {
          profileSel.value = modeVal;
        }
        // Lock profile dropdown if RTMP is enabled
        if (profileSel) {
          profileSel.disabled = !!(st.rtmp && st.rtmp.enable);
        }
    }
      // Lock HDCP dropdown if RTMP is enabled
      const hdcpSel = $("#selHdcp");
      if (hdcpSel) {
        hdcpSel.disabled = !!(st.rtmp && st.rtmp.enable);
      }
    }

  /* ============================================================
     SELECT UNIT
  ============================================================ */
  async function selectUnit(ip){
    state.unit = state.encs.find(e=>e.ip===ip)||null;
    
    // If empty/placeholder selection, clear previews and return
    if (!ip || !state.unit) {
      const liveImg = $("#livePreview");
      const activeImg = $("#activeImage");
      const logoImg = $("#logoPreview");
      
      liveImg.src = "";
      activeImg.src = "";
      logoImg.src = "";
      $("#previewMeta").textContent = "";
      
      // Clear error handlers
      liveImg.onerror = null;
      activeImg.onerror = null;
      logoImg.onerror = null;
      
      // Force placeholder display
      setTimeout(() => {
        [liveImg, activeImg, logoImg].forEach(img => {
          const slot = img.closest('.slot');
          if (slot) slot.classList.add('placeholder');
        });
      }, 10);
      
      if (livePollHandle) clearInterval(livePollHandle);
      if (window.mjpegKeepAlive) clearInterval(window.mjpegKeepAlive);
      return;
    }

    // IMMEDIATE UI FEEDBACK - Show loading state
    console.log(`⚡ Switching to ${state.unit.hostname || ip}...`);
    const loadingStart = performance.now();

    localStorage.setItem("selectedEncoderIP",ip);

    if (livePollHandle) clearInterval(livePollHandle);

    // Start loading images immediately (no delay)
    const mjpegUrl = MJPEG(ip);
    $("#livePreview").src = mjpegUrl+`&t=${Date.now()}`;
    $("#previewMeta").textContent = `MJPEG: ${mjpegUrl}`;
    
    const activeImg = $("#activeImage");
    const logoImg = $("#logoPreview");
    
    // Add error handlers for retry
    activeImg.onerror = () => {
      setTimeout(() => { activeImg.src = DOWN_IMG(ip)+`?t=${Date.now()}`; }, 1000);
    };
    logoImg.onerror = () => {
      setTimeout(() => { logoImg.src = DOWN_LOGO(ip)+`?t=${Date.now()}`; }, 1000);
    };
    
    activeImg.src = DOWN_IMG(ip)+`?t=${Date.now()}`;
    logoImg.src = DOWN_LOGO(ip)+`?t=${Date.now()}`;

    // Fetch state and apply while images load in parallel
    const st = await fetchUnitState(ip);
    applyLiveState(st);
    
    const switchTime = performance.now() - loadingStart;
    console.log(`✅ Unit switched in ${switchTime.toFixed(0)}ms`);
    
    // Keepalive: reload MJPEG every 30s to prevent stale streams
    if (window.mjpegKeepAlive) clearInterval(window.mjpegKeepAlive);
    window.mjpegKeepAlive = setInterval(() => {
      if ($("#livePreview").src.startsWith(mjpegUrl)) {
        $("#livePreview").src = mjpegUrl+`&t=${Date.now()}`;
      }
    }, 30000);

    livePollHandle = setInterval(()=>{
      if (!state.unit) return;
      if (pollNow) {
        pollUnit(state.unit.ip);
        pollNow = false;
      } else {
        pollUnit(state.unit.ip);
      }
    },3000);
  }
async function pollUnit(ip){
  pollCount++;
  const st = await fetchUnitState(ip);

  if (Date.now() < lastPollTs) return;
  lastPollTs = Date.now();
  // console.log("🔥 pollUnit START →", {
  // pillClass: document.querySelector("#tgStream .pill")?.classList.value,
  // streamOn: document.querySelector("#tgStream .pill")?.classList.contains("on")
  // });

  applyLiveState(st);
  
  // =========================================================
  // RTMP RUN STATUS - Use already-fetched data from fetchUnitState
  // =========================================================
  // Guard: ignore stale responses from previous unit selections
  if (!state.unit || st.ip !== state.unit.ip) return;
  
  const streamOn = document.querySelector("#tgStream .pill")?.classList.contains("on");
  let currentUnitActive = false;

  if (streamOn && st.rtmpStatus) {
    // Validate that we got a proper RtmpRunStatus response
    const isValidResponse = st.rtmpStatus && 
      st.rtmpStatus.hasOwnProperty('runstatus') && 
      typeof st.rtmpStatus.runstatus === 'string';
    
    if (!isValidResponse) {
      console.log(`⚠️ Invalid RtmpRunStatus for ${ip}, ignoring:`, st.rtmpStatus);
      // Don't update anything, keep previous state
    } else {
      const active = st.rtmpStatus.runstatus.toLowerCase() === "active";
      console.log(`🔴 RtmpRunStatus for ${ip}: active=${active}`);
      updateRtmpLed(active);
      currentUnitActive = active;
    }
  } else {
    updateRtmpLed(false);
    currentUnitActive = false;
  }
  
  // Update streaming status for current unit
  const unit = state.encs.find(e => e.ip === ip);
  if (unit) {
    encoderStreamStatus.set(ip, {
      hostname: unit.hostname || ip,
      ip: ip,
      isStreaming: currentUnitActive
    });
  }
  
  // Poll all OTHER encoders for streaming status
  pollOtherEncodersRtmpStatus(ip);
  
  // Update the display after all statuses are updated
  updateStreamingDisplay();
}



  /* ============================================================
     PREVIEW POPUP — DRAGGABLE / RESIZABLE
  ============================================================ */
  window.openPreviewPopup = (src)=>{
    const win = $("#floatingPreview");
    $("#floatingImg").src = src;
    win.style.display="block";
    win.style.zIndex=99999;
  };
  window.closeFloatingPreview = ()=> {
    $("#floatingPreview").style.display="none";
  };

  (function dragFloating(){
    const float = $("#floatingPreview");
    const head  = $("#floatHeader");
    let down=false,offX=0,offY=0;

    head.onmousedown = e=>{
      down=true;
      offX=e.clientX-float.offsetLeft;
      offY=e.clientY-float.offsetTop;
      document.onmousemove = ev=>{
        if (!down) return;
        float.style.left = (ev.clientX-offX)+"px";
        float.style.top  = (ev.clientY-offY)+"px";
      };
      document.onmouseup = ()=>{
        down=false;
        document.onmousemove=null;
      };
    };
  })();

  /* ============================================================
     STREAMING STATUS POLLING (OTHER ENCODERS)
  ============================================================ */
  let streamingPollInterval = null;
  const encoderStreamStatus = new Map(); // ip -> {hostname, ip, isStreaming}

  async function pollOtherEncodersRtmpStatus(currentIp) {
    if (!state.encs || state.encs.length === 0) return;
    
    // Poll all encoders EXCEPT the current one (current one is already polled in pollUnit)
    const otherEncoders = state.encs.filter(enc => enc.ip !== currentIp);
    
    if (otherEncoders.length === 0) return;
    
    for (const enc of otherEncoders) {
      try {
        // First check if RTMP is enabled
        const rtmpBody = {
          ip: enc.ip,
          method: "Rtmp.Get",
          jsonrpc: "2.0",
          id: "rtmp_check",
          params: {}
        };
        
        const rtmpResponse = await fetch(EP.rpc, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify(rtmpBody)
        });
        
        const rtmpData = await rtmpResponse.json();
        
        // Validate response
        const isValidRtmpResponse = rtmpData.result && (
          rtmpData.result.hasOwnProperty('enable') || 
          rtmpData.result.hasOwnProperty('advanced')
        );
        
        if (!isValidRtmpResponse) {
          continue; // Skip this encoder
        }
        
        const rtmpEnabled = rtmpData.result.enable === 1 || rtmpData.result.enable === true;
        
        let isStreaming = false;
        
        // If RTMP is enabled, check actual run status (same as server section LED)
        if (rtmpEnabled) {
          const statusBody = {
            ip: enc.ip,
            method: "RtmpRunStatus.Get",
            jsonrpc: "2.0",
            id: "rtmp_status",
            params: {}
          };
          
          const statusResponse = await fetch(EP.rpc, {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify(statusBody)
          });
          
          const statusData = await statusResponse.json();
          
          // Validate RtmpRunStatus response
          const isValidStatus = statusData.result && 
            statusData.result.hasOwnProperty('runstatus') && 
            typeof statusData.result.runstatus === 'string';
          
          if (isValidStatus) {
            isStreaming = statusData.result.runstatus.toLowerCase() === "active";
          }
          // If invalid, keep isStreaming as false (don't update)
        }
        
        encoderStreamStatus.set(enc.ip, {
          hostname: enc.hostname || enc.ip,
          ip: enc.ip,
          isStreaming: isStreaming
        });
      } catch (e) {
        // If poll fails, keep previous status or mark as not streaming
        if (!encoderStreamStatus.has(enc.ip)) {
          encoderStreamStatus.set(enc.ip, {
            hostname: enc.hostname || enc.ip,
            ip: enc.ip,
            isStreaming: false
          });
        }
      }
    }
  }

  function updateStreamingDisplay() {
    const streamingStatus = document.getElementById("streamingStatus");
    const streamingUnits = document.getElementById("streamingUnits");
    
    if (!streamingStatus || !streamingUnits) return;
    
    const activeEncoders = Array.from(encoderStreamStatus.values())
      .filter(status => status.isStreaming);
    
    if (activeEncoders.length > 0) {
      streamingUnits.innerHTML = activeEncoders.map(enc => {
        const previewUrl = `http://${enc.ip}/stream?resolution=320x180&fps=15&bitrate=128`;
        return `<div class="streaming-unit" data-ip="${enc.ip}" style="display:flex;align-items:center;gap:6px;position:relative;cursor:pointer;padding:4px;border-radius:4px;">
          <div style="width:10px;height:10px;border-radius:50%;background:#28a745;box-shadow:0 0 6px #28a745;"></div>
          <span>${enc.hostname}</span>
          <div class="stream-preview" style="display:none;position:absolute;top:100%;left:0;margin-top:4px;z-index:1000;border:2px solid var(--border);border-radius:8px;background:var(--card);box-shadow:0 4px 12px rgba(0,0,0,0.4);padding:4px;">
            <img src="${previewUrl}" style="width:320px;height:180px;display:block;border-radius:4px;" />
          </div>
        </div>`;
      }).join('');
      streamingStatus.style.display = 'block';
      
      // Add hover event listeners
      document.querySelectorAll('.streaming-unit').forEach(unit => {
        const preview = unit.querySelector('.stream-preview');
        unit.addEventListener('mouseenter', () => {
          preview.style.display = 'block';
        });
        unit.addEventListener('mouseleave', () => {
          preview.style.display = 'none';
        });
      });
      
      streamingStatus.style.display = 'block';
    } else {
      streamingStatus.style.display = 'none';
    }
  }

  // No separate polling needed - streaming status updates via pollUnit

  /* ============================================================
     BOOT SEQUENCE (FIXED)
  ============================================================ */
  async function boot(){
    document.querySelectorAll('.toggle:not(#darkToggle)').forEach(t=>t.classList.add('right-label'));
    
    // Restore collapse state from localStorage
    const tickerCollapsed = localStorage.getItem('panel-ticker-collapsed') === 'true';
    const streamsCollapsed = localStorage.getItem('panel-streams-collapsed') === 'true';
    
    if (tickerCollapsed) {
      const tickerPanel = document.getElementById('panel-ticker');
      if (tickerPanel) tickerPanel.classList.add('collapsed');
    }
    if (streamsCollapsed) {
      const streamsPanel = document.getElementById('panel-streams');
      if (streamsPanel) streamsPanel.classList.add('collapsed');
    }
    
    // Limit collapse interaction to chevron icon only
    document.querySelectorAll('.collapsible .header .chev').forEach(icon=>{
      icon.onclick = (e)=>{
        e.stopPropagation();
        const panel = icon.closest('.collapsible');
        if (!panel) return;
        panel.classList.toggle('collapsed');
        if (panel.id === 'panel-ticker') {
          localStorage.setItem('panel-ticker-collapsed', panel.classList.contains('collapsed'));
        } else if (panel.id === 'panel-streams') {
          localStorage.setItem('panel-streams-collapsed', panel.classList.contains('collapsed'));
        }
      };
    });


    await loadCache();
    await loadGlobalState();
    await loadAssets();

    paintImageSlots();
    paintLogoSlots();
    paintRtmpRows();
    paintTickerRows();
    initTickerColor();

    wireMuteHandlers();
    wireAudioMuteHandlers();
    wireAudioSourceHandlers();
    wireVideoEncodeHandlers();
    wireProfileHandler();
    wireHdcpHandler();
    wireLogoHandlers();
    wireRtmpToggle();
    wireRtmpApply();
    wireTickerToggle();

    populateEncoders();
    // Streaming status now updates via pollUnit - no separate polling needed
  }

  boot();

})();
