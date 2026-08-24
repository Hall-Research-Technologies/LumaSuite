// matrix.js - Matrix Routing UI
console.log('matrix.js loaded');
document.addEventListener('DOMContentLoaded', function() {
  // Store filter values globally
  let encFilterValue = '';
  let decFilterValue = '';
  const multicastCache = new Map();
  const producerRpc = (ip, method, params = {}, timeout = 2.5) => fetch('/api/producer/jsonrpc', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ip, method, params, timeout })
  }).then(r => r.json());
  let matrixRenderDeferred = false;

  function isMatrixLiveControlActive() {
    const active = document.activeElement;
    return !!(active && active.closest && active.closest('#matrix') && active.matches('input,select,textarea'));
  }

  async function requestRenderMatrix(force = false) {
    if (!force && isMatrixLiveControlActive()) {
      matrixRenderDeferred = true;
      return;
    }
    matrixRenderDeferred = false;
    await renderMatrix();
  }

  function flushDeferredMatrixRender() {
    if (!matrixRenderDeferred || isMatrixLiveControlActive()) return;
    requestRenderMatrix(true).catch(e => logDebug('Deferred matrix render failed', e));
  }

  // Wire up filter boxes ONCE
  const encFilterInput = document.getElementById('encFilterInput');
  const decFilterInput = document.getElementById('decFilterInput');
  const encFilterClearBtn = document.getElementById('encFilterClearBtn');
  const decFilterClearBtn = document.getElementById('decFilterClearBtn');
  if (encFilterInput) {
    encFilterInput.addEventListener('input', function() {
      encFilterValue = encFilterInput.value;
      console.debug('[encFilterInput] Changed:', encFilterValue);
      renderMatrix();
    });
  }
  if (decFilterInput) {
    decFilterInput.addEventListener('input', function() {
      decFilterValue = decFilterInput.value;
      console.debug('[decFilterInput] Changed:', decFilterValue);
      renderMatrix();
    });
  }
  if (encFilterClearBtn && encFilterInput) {
    encFilterClearBtn.addEventListener('click', function() {
      encFilterInput.value = '';
      encFilterValue = '';
      renderMatrix();
    });
  }
  if (decFilterClearBtn && decFilterInput) {
    decFilterClearBtn.addEventListener('click', function() {
      decFilterInput.value = '';
      decFilterValue = '';
      renderMatrix();
    });
  }

  // Utility: returns true if any field matches filter (case-insensitive)
  function deviceMatchesFilter(dev, filter, type) {
    if (!filter) return true;
    filter = filter.toLowerCase();
    let stream = '';
    if (type === 'enc' && dev.enc && dev.enc.video) stream = dev.enc.video.streamname || '';
    if (type === 'dec' && dev.dec && dev.dec.video) stream = dev.dec.video.streamname || '';
    const match = (
      (dev.hostname && dev.hostname.toLowerCase().includes(filter)) ||
      (dev.ip && dev.ip.toLowerCase().includes(filter)) ||
      (stream && stream.toLowerCase().includes(filter))
    );
    if (match) {
      console.debug(`[deviceMatchesFilter] MATCH for filter '${filter}' on`, dev, 'type:', type);
    }
    return match;
  }

  // Sticky headers toggle logic
  const stickyToggle = document.getElementById('sticky_headers_toggle');
  const stickySwitch = document.getElementById('sticky_switch');
  const matrixTable = document.getElementById('matrix');
  if (stickyToggle && matrixTable && stickySwitch) {
    function updateSwitch() {
      if (stickyToggle.checked) {
        matrixTable.classList.add('sticky-enabled');
        stickySwitch.classList.add('on');
      } else {
        matrixTable.classList.remove('sticky-enabled');
        stickySwitch.classList.remove('on');
      }
    }
    stickyToggle.addEventListener('change', function() {
      updateSwitch();
      localStorage.setItem('matrix_sticky_headers', stickyToggle.checked ? 'true' : 'false');
    });
    // Restore previous state from localStorage
    const stickyPref = localStorage.getItem('matrix_sticky_headers');
    if (stickyPref === 'true') {
      stickyToggle.checked = true;
    }
    updateSwitch();
  }


  // Render matrix immediately from cache, then poll live and update
  async function refreshAndRenderMatrix() {
    // 1. Render immediately from cache
    await requestRenderMatrix();
    // 2. Poll live device state in background, then re-render
    fetch('/api/refresh_matrix_state', { method: 'POST' })
      .then(() => requestRenderMatrix())
      .catch(e => logDebug('Error refreshing matrix state', e));
  }

  // Initial render
  // ...existing code...
  refreshAndRenderMatrix();

  // Poll every 10 seconds to keep matrix in sync
  setInterval(() => {
    // ...existing code...
    refreshAndRenderMatrix();
  }, 10000);

  // Wire up refresh button to re-render matrix
  var refreshBtn = document.getElementById('refreshBtn');
  if (refreshBtn) {
    refreshBtn.addEventListener('click', async () => {
      await refreshAndRenderMatrix();
      toast('Matrix refreshed', true);
    });
  }

  document.addEventListener('focusout', () => {
    setTimeout(flushDeferredMatrixRender, 150);
  });



  // Apply dark mode from localStorage (set by index.html)
  const dark = localStorage.getItem('dark') !== 'false';
  document.body.classList.toggle('dark', dark);
  document.body.classList.toggle('light', !dark);

  function encoderURL(ip, isSwitcher){
    if(isSwitcher) return `http://${ip}/stream?t=${Math.random()}`;
    return `http://${ip}/stream?resolution=320x180&fps=15&bitrate=512`;
  }

  function ensureHoverPreview(){
    if (document.getElementById('hover_preview')) return;
    const box = document.createElement('div');
    box.id = 'hover_preview';
    box.className = 'hover-preview';
    const img = document.createElement('img');
    img.id = 'hover_preview_img';
    img.alt = 'preview';
    box.appendChild(img);
    document.body.appendChild(box);
  }

  function moveHoverPreview(e){
    const box = document.getElementById('hover_preview');
    if (!box || box.style.display !== 'block') return;
    const pad = 16;
    let x = e.clientX - box.offsetWidth - pad;
    let y = e.clientY + pad;
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const w = box.offsetWidth;
    const h = box.offsetHeight;
    if (x < 0) x = e.clientX + pad;
    if (y + h > vh) y = e.clientY - h - pad;
    if (x + w > vw) x = vw - w - 4;
    if (y + h > vh) y = vh - h - 4;
    if (y < 0) y = 4;
    box.style.left = `${x}px`;
    box.style.top = `${y}px`;
  }

  function showEncoderHoverPreview(e, routeDot){
    const encIp = routeDot?.dataset?.encIp;
    if (!encIp) return;
    ensureHoverPreview();
    const box = document.getElementById('hover_preview');
    const img = document.getElementById('hover_preview_img');
    if (!box || !img) return;
    const typeLabel = (routeDot.dataset.encType || '').toLowerCase();
    const model = (routeDot.dataset.encModel || '').toLowerCase();
    const isSwitcher = typeLabel.includes('switcher') || model.includes('at-ome-cs31');
    img.src = encoderURL(encIp, isSwitcher);
    moveHoverPreview(e);
    box.style.display = 'block';
  }

  function hideHoverPreview(){
    const box = document.getElementById('hover_preview');
    const img = document.getElementById('hover_preview_img');
    if (!box || !img) return;
    box.style.display = 'none';
    img.src = '';
  }

  if (matrixTable) {
    matrixTable.addEventListener('mouseover', (e) => {
      const routeDot = e.target.closest('input.route-radio');
      if (!routeDot) return;
      showEncoderHoverPreview(e, routeDot);
    });
    matrixTable.addEventListener('mousemove', (e) => {
      const routeDot = e.target.closest('input.route-radio');
      if (!routeDot) return;
      moveHoverPreview(e);
    });
    matrixTable.addEventListener('mouseout', (e) => {
      const routeDot = e.target.closest('input.route-radio');
      if (!routeDot) return;
      const toDot = e.relatedTarget && e.relatedTarget.closest ? e.relatedTarget.closest('input.route-radio') : null;
      if (toDot) return;
      hideHoverPreview();
    });
  }

  // Fetch units from backend and split into encoders/decoders
  async function fetchUnits() {
    // Always fetch fresh data (no cache)
    console.debug('[fetchUnits] Fetching /api/cache ...');
    const res = await fetch('/api/cache', { cache: 'no-store' });
    const data = await res.json();
    console.debug('[fetchUnits] Response:', data);
    let units = (data.units || []);
    
    // Check both type and role fields for encoder/decoder/switcher classification
    const typeOrRole = (u) => `${(u.type||'')} ${(u.role||'')} ${(u.model||'')}`.toLowerCase();
    const encoders = units.filter(u => typeOrRole(u).includes('encoder'));
    const decoders = units.filter(u => {
      const tr = typeOrRole(u);
      return tr.includes('decoder') || tr.includes('switcher') || tr.includes('at-ome-cs31');
    });
    console.debug('[fetchUnits] Encoders:', encoders);
    console.debug('[fetchUnits] Decoders/Switchers:', decoders);
    return { encoders, decoders };
  }

  function getEncoderStreamInfo(enc) {
    // Returns {streamname, ip, port} for encoder
    if (enc.enc && enc.enc.video) {
      return {
        streamname: enc.enc.video.streamname,
        ip: enc.enc.video.ip,
        port: enc.enc.video.port
      };
    }
    return { streamname: enc.hostname || enc.ip, ip: enc.ip, port: null };
  }

  async function renderMatrix() {
    // Hide and clear hover preview before rebuilding table
    const preview = document.getElementById('hover-preview');
    if (preview) {
      preview.style.display = 'none';
    }
    
    // Render encoder and decoder tables at the bottom (after encoders/decoders are initialized)
    function renderDeviceTable(tblId, devices, type, encoderByStream) {
      const tbl = document.getElementById(tblId);
      if (!tbl) return;
      tbl.innerHTML = '';
      const thead = document.createElement('thead');
      thead.innerHTML = `<tr><th>Hostname</th><th>IP</th><th>Streamname</th><th>Video Multicast</th><th>Audio Multicast</th></tr>`;
      tbl.appendChild(thead);
      const tbody = document.createElement('tbody');
      devices.forEach(dev => {
        let stream = '';
        let videoMulti = '';
        let audioMulti = '';
        const cacheKey = `${type}:${dev.ip || ''}`;
        
        if (type === 'enc') {
          if (dev.enc && dev.enc.video) {
            stream = dev.enc.video.streamname || '';
            const vip = dev.enc.video.ip || dev.enc.video.userdefineip || '';
            const vport = dev.enc.video.port || dev.enc.video.userdefineport || '';
            if (vip) videoMulti = vport ? `${vip}:${vport}` : vip;
          }
          if (dev.enc && dev.enc.audio) {
            const aip = dev.enc.audio.ip || dev.enc.audio.userdefineip || '';
            const aport = dev.enc.audio.port || dev.enc.audio.userdefineport || '';
            if (aip) audioMulti = aport ? `${aip}:${aport}` : aip;
          }
        }
        
        if (type === 'dec') {
          if (dev.dec && dev.dec.video) {
            stream = dev.dec.video.streamname || '';
          }
          if (stream && encoderByStream && encoderByStream.has(stream)) {
            const enc = encoderByStream.get(stream);
            if (enc && enc.enc && enc.enc.video) {
              const vip = enc.enc.video.ip || enc.enc.video.userdefineip || '';
              const vport = enc.enc.video.port || enc.enc.video.userdefineport || '';
              if (vip) videoMulti = vport ? `${vip}:${vport}` : vip;
            }
            if (enc && enc.enc && enc.enc.audio) {
              const aip = enc.enc.audio.ip || enc.enc.audio.userdefineip || '';
              const aport = enc.enc.audio.port || enc.enc.audio.userdefineport || '';
              if (aip) audioMulti = aport ? `${aip}:${aport}` : aip;
            }
          } else {
            if (dev.dec && dev.dec.video) {
              const vip = dev.dec.video.ip || dev.dec.video.userdefineip || '';
              const vport = dev.dec.video.port || dev.dec.video.userdefineport || '';
              if (vip) videoMulti = vport ? `${vip}:${vport}` : vip;
            }
            if (dev.dec && dev.dec.audio) {
              const aip = dev.dec.audio.ip || dev.dec.audio.userdefineip || '';
              const aport = dev.dec.audio.port || dev.dec.audio.userdefineport || '';
              if (aip) audioMulti = aport ? `${aip}:${aport}` : aip;
            }
          }
        }
        
        if (!videoMulti && multicastCache.has(cacheKey)) {
          const cached = multicastCache.get(cacheKey) || {};
          videoMulti = cached.videoMulti || '';
        }
        if (!audioMulti && multicastCache.has(cacheKey)) {
          const cached = multicastCache.get(cacheKey) || {};
          audioMulti = cached.audioMulti || '';
        }
        if (videoMulti || audioMulti) {
          multicastCache.set(cacheKey, { videoMulti, audioMulti });
        }

        const tr = document.createElement('tr');
        tr.innerHTML = `<td>${dev.hostname||dev.ip}</td><td>${dev.ip}</td><td>${stream}</td><td>${videoMulti}</td><td>${audioMulti}</td>`;
        tbody.appendChild(tr);
      });
      tbl.appendChild(tbody);
    }

    console.debug('[renderMatrix] Start');
    let { encoders, decoders } = await fetchUnits();
    // Sort encoders and decoders by IP address ascending
    encoders = sortByIpAsc(encoders).filter(e => deviceMatchesFilter(e, encFilterValue, 'enc'));
    decoders = sortByIpAsc(decoders).filter(d => deviceMatchesFilter(d, decFilterValue, 'dec'));
    console.debug('[renderMatrix] encFilterValue:', encFilterValue, 'Filtered encoders:', encoders);
    console.debug('[renderMatrix] decFilterValue:', decFilterValue, 'Filtered decoders:', decoders);
    const encoderByStream = new Map();
    encoders.forEach(enc => {
      const stream = enc && enc.enc && enc.enc.video ? enc.enc.video.streamname : '';
      if (stream) encoderByStream.set(stream, enc);
    });
    renderDeviceTable('encTbl', encoders, 'enc', encoderByStream);
    renderDeviceTable('decTbl', decoders, 'dec', encoderByStream);
    // Add vertical header style if not present (do this first)
    if (!document.getElementById('vertical-enc-header-style')) {
      const style = document.createElement('style');
      style.id = 'vertical-enc-header-style';
      style.textContent = `.vertical-enc-header { vertical-align:bottom; padding:0 2px; min-width:40px; max-width:60px; }
        .vertical-enc-header > a > div { writing-mode:vertical-rl; transform:rotate(180deg); white-space:nowrap; font-size:13px; }
        .vertical-enc-header > a { display:block; width:100%; height:100%; }`;
      document.head.appendChild(style);
    }
    console.debug('[renderMatrix] Got encoders:', encoders);
    console.debug('[renderMatrix] Got decoders:', decoders);
    const table = document.getElementById('matrix');
    if (!table) {
      console.error('[renderMatrix] #matrix table not found in DOM');
      return;
    }
    table.innerHTML = '';
    console.debug('[renderMatrix] Cleared table');
    const thead = document.createElement('thead');
    const trHead = document.createElement('tr');
    // Add master group checkbox in the first column
    trHead.innerHTML = `<th><input type="checkbox" id="group-master-checkbox"></th><th>Decoders \\ Encoders</th>` +
      encoders.map(e =>
        `<th class="vertical-enc-header">
          <a href="http://${e.ip}" target="_blank" style="color:inherit;text-decoration:underline;">
            <div style="writing-mode:vertical-rl;transform:rotate(180deg);white-space:nowrap;">
              ${e.enc && e.enc.video && e.enc.video.streamname ? e.enc.video.streamname : '(no stream)'}<br><span style='font-size:11px;'>${e.ip}</span>
            </div>
          </a>
        </th>`
      ).join('');
    thead.appendChild(trHead);
    table.appendChild(thead);
    const tbody = document.createElement('tbody');
    // Group selection state
    if (!window.selectedDecoders) window.selectedDecoders = new Set();
    console.debug('[renderMatrix] selectedDecoders:', Array.from(window.selectedDecoders));
    decoders.forEach(dec => {
      const tr = document.createElement('tr');
      // Highlight offline decoders
      if ((dec.status || '').toLowerCase() !== 'idle') {
        tr.classList.add('offline-device');
      }
      const checkedGroup = window.selectedDecoders.has(dec.ip) ? 'checked' : '';
      tr.innerHTML = `<td><input type="checkbox" class="group-checkbox" data-dec-ip="${dec.ip}" ${checkedGroup}></td>` +
        `<th>
          <a href="http://${dec.ip}" target="_blank" style="color:inherit;text-decoration:underline;">
            <div>
              ${dec.hostname||dec.ip}<br><span style='font-size:11px;'>${dec.ip}</span>
            </div>
          </a>
        </th>` +
        encoders.map(enc => {
          let checked = '';
          if (dec.dec && dec.dec.video && enc.enc && enc.enc.video) {
            if (dec.dec.video.streamname === enc.enc.video.streamname) checked = 'checked';
          }
          const encType = (enc.type || enc.role || '');
          const encModel = (enc.model || enc.modelname || '');
          return `<td><input type="radio" name="route-${dec.ip}" class="route-radio" data-dec-ip="${dec.ip}" data-enc-ip="${enc.ip}" data-enc-type="${encType}" data-enc-model="${encModel}" ${checked}></td>`;
        }).join('');
      tbody.appendChild(tr);
      // Debug: log decoder row
      console.debug('[renderMatrix] Decoder row:', dec, tr.innerHTML);
      // Add vertical header style if not present
      if (!document.getElementById('vertical-enc-header-style')) {
        const style = document.createElement('style');
        style.id = 'vertical-enc-header-style';
        style.textContent = `.vertical-enc-header { vertical-align:bottom; padding:0 2px; min-width:40px; max-width:60px; }
          .vertical-enc-header > a > div { writing-mode:vertical-rl; transform:rotate(180deg); white-space:nowrap; font-size:13px; }
          .vertical-enc-header > a { display:block; width:100%; height:100%; }`;
        document.head.appendChild(style);
      }
    });
    console.debug('[renderMatrix] Table updated');
    table.appendChild(tbody);

    // Handle group checkbox changes

    // Handle group checkbox changes
    const groupCheckboxes = Array.from(tbody.querySelectorAll('.group-checkbox'));
    groupCheckboxes.forEach(cb => {
      cb.addEventListener('change', function(e) {
        const ip = cb.getAttribute('data-dec-ip');
        if (cb.checked) window.selectedDecoders.add(ip);
        else window.selectedDecoders.delete(ip);
        // Update master checkbox state
        const master = document.getElementById('group-master-checkbox');
        if (master) {
          master.checked = groupCheckboxes.every(c => c.checked);
          master.indeterminate = !master.checked && groupCheckboxes.some(c => c.checked);
        }
      });
    });

    // Master checkbox logic
    const masterCheckbox = document.getElementById('group-master-checkbox');
    if (masterCheckbox) {
      masterCheckbox.checked = groupCheckboxes.length > 0 && groupCheckboxes.every(c => c.checked);
      masterCheckbox.indeterminate = !masterCheckbox.checked && groupCheckboxes.some(c => c.checked);
      masterCheckbox.addEventListener('change', function() {
        groupCheckboxes.forEach(cb => {
          cb.checked = masterCheckbox.checked;
          const ip = cb.getAttribute('data-dec-ip');
          if (masterCheckbox.checked) window.selectedDecoders.add(ip);
          else window.selectedDecoders.delete(ip);
        });
      });
    }

    // Wire up routing for radio buttons
    tbody.addEventListener('change', async function(e) {
      if (e.target.classList.contains('route-radio')) {
        console.log('route-radio clicked', e.target.getAttribute('data-dec-ip'), e.target.getAttribute('data-enc-ip'));
        const decIp = e.target.getAttribute('data-dec-ip');
        const encIp = e.target.getAttribute('data-enc-ip');
        const encoder = encoders.find(e => e.ip === encIp);
        if (!encoder) {
          toast('Encoder not found', false);
          return;
        }
        // Determine which decoders to route: all checked, or just this one
        const group = Array.from(window.selectedDecoders || []);
        const targets = group.length > 0 && group.includes(decIp) ? group : [decIp];
        const applyRouteSelection = (targetIps, encoderIp) => {
          targetIps.forEach(targetIp => {
            const rowRadios = tbody.querySelectorAll(`input.route-radio[data-dec-ip="${CSS.escape(targetIp)}"]`);
            rowRadios.forEach(radio => { radio.checked = radio.getAttribute('data-enc-ip') === encoderIp; });
          });
        };
        applyRouteSelection(targets, encIp);
        
        // Validate encoder profile for CS31 compatibility
        const getEncoderEncodeMode = (encUnit) => {
          const rawMode = encUnit?.enc?.profile?.parsed?.encodeMode
            ?? encUnit?.profile?.parsed?.encodeMode
            ?? encUnit?.enc?.profile?.encodeMode
            ?? encUnit?.profile?.encodeMode;
          const parsedMode = Number(rawMode);
          return Number.isFinite(parsedMode) ? parsedMode : null;
        };
        const getEncodeModeFromRpcResult = (rpcResult) => {
          let resultObj = rpcResult;
          if (typeof resultObj === 'string') {
            try {
              resultObj = JSON.parse(resultObj);
            } catch {
              resultObj = null;
            }
          }
          const rawMode = resultObj?.encodeMode
            ?? resultObj?.profile?.encodeMode
            ?? resultObj?.parsed?.encodeMode;
          const parsedMode = Number(rawMode);
          return Number.isFinite(parsedMode) ? parsedMode : null;
        };

        const hasCS31Target = targets.some(targetIp => {
          const decoder = decoders.find(d => d.ip === targetIp);
          if (!decoder) return false;
          const decoderModel = (decoder.model || '').toLowerCase();
          const decoderTypeRole = `${(decoder.type || '')} ${(decoder.role || '')}`.toLowerCase();
          return decoderModel.includes('at-ome-cs31') || decoderTypeRole.includes('switcher');
        });

        let encoderProfile = getEncoderEncodeMode(encoder);
        if (hasCS31Target) {
          try {
            const liveProfileResp = await producerRpc(encIp, 'ProfileSelection.Get', {});
            if (liveProfileResp && !liveProfileResp.error) {
              const liveMode = getEncodeModeFromRpcResult(liveProfileResp.result);
              if (liveMode !== null) encoderProfile = liveMode;
            }
          } catch (err) {
            console.warn('[matrix] ProfileSelection.Get failed, using cached profile mode', err);
          }
        }

        const encoderName = encoder.hostname || encoder.ip;

        for (const targetIp of targets) {
          const decoder = decoders.find(d => d.ip === targetIp);
          if (!decoder) continue;
          const decoderName = decoder.hostname || decoder.ip;
          const decoderModel = (decoder.model || '').toLowerCase();
          const decoderTypeRole = `${(decoder.type || '')} ${(decoder.role || '')}`.toLowerCase();
          const isCS31Target = decoderModel.includes('at-ome-cs31') || decoderTypeRole.includes('switcher');
          if (isCS31Target && encoderProfile !== 3) {
            toast(`Warning: ${encoderName} encoder profile is not "AT-OME-CS31". Video may not display properly on ${decoderName}.`, false);
          }
        }
        
        // Send all route commands in parallel for minimal delay
        let allOk = true;
        let lastError = '';
        const sendRoute = async targetIp => {
          const postData = { decoder_ip: targetIp, encoder_ip: encIp };
          console.debug('[sendRoute] POST /api/route_matrix', postData);
          try {
            const resp = await fetch('/api/route_matrix', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify(postData)
            });
            const result = await resp.json().catch(() => ({}));
            console.debug('[sendRoute] Response', resp.status, result);
            return { targetIp, result, status: resp.status };
          } catch (err) {
            console.error('[sendRoute] Error', err);
            return { targetIp, result: { ok: false, error: err && err.message ? err.message : 'Network error' }, status: 0 };
          }
        };

        const results = await Promise.all(targets.map(sendRoute));
        
        // Track successes and failures
        let hasFailure = false;
        let hasSuccess = false;
        
        for (const { targetIp, result, status } of results) {
          const decoder = decoders.find(d => d.ip === targetIp);
          const decoderName = decoder ? (decoder.hostname || decoder.ip) : targetIp;
          
          if (!result.ok || status !== 200) {
            hasFailure = true;
            toast(`Route Failed for ${decoderName}`, false);
            // Uncheck the radio button if failed
            const radio = document.querySelector(`input.route-radio[data-dec-ip='${targetIp}'][data-enc-ip='${encIp}']`);
            if (radio) radio.checked = false;
          } else {
            hasSuccess = true;
          }
        }
        
        // If any succeeded, poll once and batch-refresh UI
        if (hasSuccess) {
          let confirmed = false;
          let attempts = 0;
          const maxAttempts = 5;
          const delay = ms => new Promise(res => setTimeout(res, ms));
          await delay(500);
          while (attempts < maxAttempts && !confirmed) {
            await delay(300);
            try {
              const pollRes = await fetch('/api/cache');
              const pollData = await pollRes.json();
              const units = pollData.units || [];
              // Check if ANY target succeeded
              confirmed = results.some(({ targetIp, result, status }) => {
                if (result.ok && status === 200) {
                  const dec = units.find(u => u.ip === targetIp);
                  const enc = units.find(u => u.ip === encIp);
                  return dec && dec.dec && dec.dec.video && enc && enc.enc && enc.enc.video &&
                    dec.dec.video.streamname === enc.enc.video.streamname;
                }
                return false;
              });
              if (confirmed) break;
            } catch {}
            attempts++;
          }
          if (confirmed) {
            // Single backend refresh and single renderMatrix call
            await fetch('/api/refresh_matrix_state', { method: 'POST' });
            await requestRenderMatrix(true);
          }
        }
      }
    });
  }

  // Initial render

  // (Already handled above) Initial render, polling, and refresh now use refreshAndRenderMatrix

  let routeMode = 'av';

  function setMode(m){
    routeMode = m;
    document.querySelectorAll('.modebtn').forEach(b=>b.classList.toggle('active', b.dataset.mode===m));
    const modeLabel = document.getElementById('mode_label');
    if(modeLabel){
      const upper = (m||'').toUpperCase();
      const label = upper==='AV' ? 'AV (Audio + Video)' : (upper==='VIDEO' ? 'Video Only' : 'Audio Only');
      modeLabel.textContent = label;
    }
  }
  document.addEventListener('click', (e)=>{
    const b = e.target.closest('.modebtn'); if(!b) return;
    setMode(b.dataset.mode);
  });

  function toast(msg, good=false){
    const el = document.createElement('div');
    el.className = 'toast'+(good?' ok':'');
    el.textContent = msg;
    document.body.appendChild(el);
    setTimeout(()=>el.classList.add('show'),10);
    setTimeout(()=>el.classList.remove('show'), 6000);
    setTimeout(()=>el.remove(), 6400);
  }

  function ipNum(ip){
    const m = (ip||'').trim().match(/^(\d+)\.(\d+)\.(\d+)\.(\d+)$/);
    if(!m) return Number.MAX_SAFE_INTEGER;
    return (+m[1]<<24) + (+m[2]<<16) + (+m[3]<<8) + (+m[4]);
  }
  function sortByIpAsc(arr){ return [...arr].sort((a,b)=>ipNum(a.ip)-ipNum(b.ip)); }



  // Remove old demo refresh/render. All updates use renderMatrix()
  setMode('av');
  // Collapsible sections
  document.querySelectorAll('.collapsible .header').forEach(header=>{
    header.addEventListener('click', ()=>{
      const section = header.closest('.collapsible');
      section.classList.toggle('collapsed');
    });
  });
});
