(function(){
  "use strict";

  const MAX_GRID = 16;
  const DEFAULT_DISPLAY = {
    totalwidth: 0,
    totalheight: 0,
    topborder: 0,
    bottomborder: 0,
    leftborder: 0,
    rightborder: 0
  };
  const SELECTED_WALL_KEY = "lumasuite_video_wall_selected_id";

  let state = { walls: [], encoders: [], decoders: [] };
  let currentWall = null;
  let selectedPos = "1:1";
  let reports = {};
  let liveRouteSeq = 0;
  let autoRefreshTimer = null;

  function clampInt(value, min, max, fallback) {
    const n = Number.parseInt(value, 10);
    if (!Number.isFinite(n)) return fallback;
    return Math.min(max, Math.max(min, n));
  }

  function wallDeviceId(unit) {
    if (!unit || typeof unit !== "object") return "";
    for (const key of ["mac", "serialnumber", "uuid", "id"]) {
      const value = String(unit[key] || "").trim();
      if (value) return `${key}:${value.toLowerCase()}`;
    }
    const ip = String(unit.ip || "").trim();
    return ip ? `ip:${ip}` : "";
  }

  function newId() {
    if (window.crypto && window.crypto.randomUUID) return `wall-${window.crypto.randomUUID()}`;
    return `wall-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }

  function emptyCell(row, column) {
    return {
      row,
      column,
      decoder_id: "",
      source_mode: "wall",
      override_source_id: "",
      rotation: 0,
      display: {},
      decoder_snapshot: null,
      override_source_snapshot: null
    };
  }

  function createWall(name, rows, columns) {
    return normalizeWall({
      id: newId(),
      name: name || "Video Wall",
      rows: rows || 2,
      columns: columns || 2,
      source_id: "",
      display: { ...DEFAULT_DISPLAY },
      cells: []
    });
  }

  function normalizeWall(input) {
    const wall = input && typeof input === "object" ? { ...input } : {};
    wall.id = wall.id || newId();
    wall.name = String(wall.name || "Video Wall").trim() || "Video Wall";
    wall.rows = clampInt(wall.rows, 1, MAX_GRID, 2);
    wall.columns = clampInt(wall.columns, 1, MAX_GRID, 2);
    wall.source_id = String(wall.source_id || "");
    wall.display = { ...DEFAULT_DISPLAY, ...(wall.display || {}) };

    const old = new Map();
    for (const cell of Array.isArray(wall.cells) ? wall.cells : []) {
      const row = clampInt(cell.row, 1, MAX_GRID, 1);
      const column = clampInt(cell.column, 1, MAX_GRID, 1);
      old.set(`${row}:${column}`, {
        ...emptyCell(row, column),
        ...cell,
        row,
        column,
        source_mode: cell.source_mode === "override" ? "override" : "wall",
        rotation: clampInt(cell.rotation, 0, 270, 0),
        display: cell.display && typeof cell.display === "object" ? cell.display : {},
        decoder_snapshot: cell.decoder_snapshot && typeof cell.decoder_snapshot === "object" ? cell.decoder_snapshot : null,
        override_source_snapshot: cell.override_source_snapshot && typeof cell.override_source_snapshot === "object" ? cell.override_source_snapshot : null
      });
    }

    wall.cells = [];
    for (let row = 1; row <= wall.rows; row++) {
      for (let column = 1; column <= wall.columns; column++) {
        wall.cells.push(old.get(`${row}:${column}`) || emptyCell(row, column));
      }
    }
    return wall;
  }

  function resizeWall(wall, rows, columns) {
    const next = normalizeWall({ ...wall, rows, columns });
    const validKeys = new Set(next.cells.map(c => `${c.row}:${c.column}`));
    next.cells = next.cells.map(cell => {
      const old = (wall.cells || []).find(c => `${c.row}:${c.column}` === `${cell.row}:${cell.column}`);
      return validKeys.has(`${cell.row}:${cell.column}`) && old ? { ...cell, ...old, row: cell.row, column: cell.column } : cell;
    });
    return normalizeWall(next);
  }

  function deviceLabel(device) {
    if (!device) return "Unassigned";
    const name = device.hostname || device.label || device.ip || device.mac || device.id || "Unknown";
    return device.ip && !String(name).includes(device.ip) ? `${device.ip} - ${name}` : name;
  }

  function sourceLabel(device) {
    if (!device) return "Not selected";
    const stream = device.streamname || device.label || device.hostname || device.model || device.mac || device.id || "";
    return device.ip ? `${device.ip} - ${stream}` : (stream || deviceLabel(device));
  }

  function compactDeviceSnapshot(device) {
    if (!device) return null;
    return {
      id: device.id || wallDeviceId(device),
      ip: device.ip || "",
      mac: device.mac || "",
      serialnumber: device.serialnumber || "",
      hostname: device.hostname || "",
      model: device.model || "",
      type: device.type || "",
      role: device.role || "",
      label: device.label || deviceLabel(device),
      streamname: device.streamname || ""
    };
  }

  function idMap(list) {
    return new Map((list || []).map(item => [item.id || wallDeviceId(item), item]));
  }

  function sourceNameById(sourceId, encoders) {
    const match = (encoders || []).find(e => e.id === sourceId);
    return match ? deviceLabel(match) : "";
  }

  function findDeviceWithSnapshot(id, list, snapshot) {
    return (list || []).find(item => item.id === id) || snapshot || null;
  }

  function enrichWallWithSnapshots(wall, encoders, decoders) {
    const encMap = idMap(encoders);
    const decMap = idMap(decoders);
    const next = normalizeWall(wall);
    next.source_snapshot = compactDeviceSnapshot(encMap.get(next.source_id)) || next.source_snapshot || null;
    next.cells = next.cells.map(cell => {
      const cleanCell = { ...cell };
      delete cleanCell.live_source_pending;
      return {
        ...cleanCell,
        decoder_snapshot: compactDeviceSnapshot(decMap.get(cell.decoder_id)) || cell.decoder_snapshot || null,
        override_source_snapshot: compactDeviceSnapshot(encMap.get(cell.override_source_id)) || cell.override_source_snapshot || null
      };
    });
    return next;
  }

  function rememberSelectedWall(wallId) {
    try {
      if (wallId) localStorage.setItem(SELECTED_WALL_KEY, wallId);
    } catch {}
  }

  function rememberedWallId() {
    try {
      return localStorage.getItem(SELECTED_WALL_KEY) || "";
    } catch {
      return "";
    }
  }

  function snapAllDisplaysToWallVideo(wall) {
    for (const cell of wall?.cells || []) {
      cell.source_mode = "wall";
      cell.override_source_id = "";
      cell.override_source_snapshot = null;
    }
    return wall;
  }

  function statusClass(status) {
    return String(status || "").toLowerCase();
  }

  function statusFromReports(wall, reportMap) {
    if (!wall) return "Unsaved";
    const cells = wall.cells || [];
    if (cells.some(c => c.source_mode === "override")) return "Override";
    const assigned = cells.filter(c => c.decoder_id);
    if (!assigned.length) return "Unassigned";
    if (!Object.keys(reportMap || {}).length) return "Not refreshed";
    if (assigned.some(c => !Object.prototype.hasOwnProperty.call(reportMap, c.decoder_id))) return "Not refreshed";
    if (assigned.some(c => !(reportMap[c.decoder_id] || {}).online)) return "Partial";
    if (assigned.some(c => cellDiffs(wall, c, reportMap[c.decoder_id]).length)) return "Modified";
    return "Healthy";
  }

  function cellDiffs(wall, cell, report) {
    const diffs = [];
    if (!report || !report.online) return cell.decoder_id ? ["offline or not reported"] : [];
    const vw = report.video_wall || {};
    const expected = {
      rows: wall.rows,
      columns: wall.columns,
      row: cell.row,
      column: cell.column
    };
    for (const key of Object.keys(expected)) {
      if (vw[key] != null && Number(vw[key]) !== Number(expected[key])) {
        diffs.push(`${key}: saved ${expected[key]}, reported ${vw[key]}`);
      }
    }
    const expectedEnabled = cell.source_mode !== "override";
    if (vw.vmenable != null && Boolean(vw.vmenable) !== expectedEnabled) {
      diffs.push(`wall mode: saved ${expectedEnabled ? "on" : "off"}, reported ${vw.vmenable ? "on" : "off"}`);
    }
    if (report.rotation != null && Number(report.rotation) !== Number(cell.rotation || 0)) {
      diffs.push(`rotation: saved ${cell.rotation || 0}, reported ${report.rotation}`);
    }
    return diffs;
  }

  function setStatus(text, cls) {
    const chip = document.getElementById("statusChip");
    if (!chip) return;
    chip.className = `status-chip ${statusClass(cls || text)}`;
    chip.textContent = text || "";
  }

  function toast(message, ok) {
    const el = document.getElementById("toast");
    if (!el) return;
    el.textContent = message;
    el.className = `toast ${ok ? "ok" : ""} show`;
    window.setTimeout(() => el.classList.remove("show"), 2600);
  }

  async function api(path, body) {
    const response = await fetch(path, {
      method: body == null ? "GET" : "POST",
      headers: body == null ? undefined : { "Content-Type": "application/json" },
      body: body == null ? undefined : JSON.stringify(body)
    });
    const data = await response.json().catch(() => ({}));
    if (response.status === 404 && path.startsWith("/api/video_wall/")) {
      throw new Error("Video Wall backend is not loaded. Restart LumaSuite and refresh this page.");
    }
    if (!response.ok || data.ok === false) throw new Error(data.error || `Request failed: ${response.status}`);
    return data;
  }

  function syncFormFromWall() {
    if (!currentWall) return;
    document.getElementById("wallName").value = currentWall.name;
    document.getElementById("rowsInput").value = currentWall.rows;
    document.getElementById("colsInput").value = currentWall.columns;
    document.getElementById("wallSource").value = currentWall.source_id || "";
    setDisplayFormValues(currentWall.display || DEFAULT_DISPLAY);
  }

  function setDisplayFormValues(display) {
    for (const [id, key] of [
      ["totalWidth", "totalwidth"],
      ["totalHeight", "totalheight"],
      ["topBorder", "topborder"],
      ["bottomBorder", "bottomborder"],
      ["leftBorder", "leftborder"],
      ["rightBorder", "rightborder"]
    ]) {
      document.getElementById(id).value = display[key] || 0;
    }
    updateBezelSizeLabel();
  }

  function selectedDisplayValues() {
    const cell = selectedCell();
    const report = cell && cell.decoder_id ? reports[cell.decoder_id] : null;
    if (report && report.bezel) return { ...DEFAULT_DISPLAY, ...report.bezel };
    if (cell && cell.display && Object.keys(cell.display).length) return { ...DEFAULT_DISPLAY, ...cell.display };
    return { ...DEFAULT_DISPLAY, ...(currentWall?.display || {}) };
  }

  function syncDisplayFormFromSelected() {
    setDisplayFormValues(selectedDisplayValues());
  }

  function syncWallFromForm() {
    if (!currentWall) currentWall = createWall("Video Wall 1", 2, 2);
    currentWall.name = document.getElementById("wallName").value || "Video Wall";
    currentWall.source_id = document.getElementById("wallSource").value || "";
    currentWall = resizeWall(
      currentWall,
      document.getElementById("rowsInput").value,
      document.getElementById("colsInput").value
    );
    const display = {
      totalwidth: clampInt(document.getElementById("totalWidth").value, 0, 999999, 0),
      totalheight: clampInt(document.getElementById("totalHeight").value, 0, 999999, 0),
      topborder: clampInt(document.getElementById("topBorder").value, 0, 999999, 0),
      bottomborder: clampInt(document.getElementById("bottomBorder").value, 0, 999999, 0),
      leftborder: clampInt(document.getElementById("leftBorder").value, 0, 999999, 0),
      rightborder: clampInt(document.getElementById("rightBorder").value, 0, 999999, 0)
    };
    if (document.getElementById("applyBezelAll").checked) {
      currentWall.display = display;
      for (const cell of currentWall.cells || []) cell.display = {};
    } else {
      const cell = selectedCell();
      if (cell) cell.display = display;
    }
  }

  function renderSelectors() {
    const wallSelect = document.getElementById("wallSelect");
    const wallSource = document.getElementById("wallSource");
    wallSelect.innerHTML = "";
    for (const wall of state.walls) {
      const option = document.createElement("option");
      option.value = wall.id;
      option.textContent = wall.name;
      wallSelect.appendChild(option);
    }
    wallSelect.value = currentWall ? currentWall.id : "";

    wallSource.innerHTML = `<option value="">Select encoder source</option>`;
    const sourceOptions = [...state.encoders];
    if (currentWall?.source_id && !sourceOptions.some(e => e.id === currentWall.source_id) && currentWall.source_snapshot) {
      sourceOptions.push(currentWall.source_snapshot);
    }
    for (const encoder of sourceOptions) {
      const option = document.createElement("option");
      option.value = encoder.id;
      option.textContent = sourceLabel(encoder);
      wallSource.appendChild(option);
    }
    if (currentWall) wallSource.value = currentWall.source_id || "";
  }

  function optionHtml(items, selected, emptyText) {
    const parts = [`<option value="">${emptyText}</option>`];
    for (const item of items) {
      const value = item.id || wallDeviceId(item);
      parts.push(`<option value="${escapeHtml(value)}"${value === selected ? " selected" : ""}>${escapeHtml(deviceLabel(item))}</option>`);
    }
    return parts.join("");
  }

  function availableDecodersForCell(decoders, cells, cell) {
    const used = new Set((cells || [])
      .filter(other => other !== cell && other.decoder_id)
      .map(other => other.decoder_id));
    return (decoders || []).filter(decoder => decoder.id === cell.decoder_id || !used.has(decoder.id));
  }

  function decoderOptionsForCell(cell) {
    const options = availableDecodersForCell(state.decoders, currentWall?.cells || [], cell);
    if (cell.decoder_id && !options.some(decoder => decoder.id === cell.decoder_id) && cell.decoder_snapshot) {
      options.unshift(cell.decoder_snapshot);
    }
    return options;
  }

  function sourceOptionsForCell(cell) {
    const selected = cell.source_mode === "override" ? cell.override_source_id : "__wall__";
    const parts = [`<option value="__wall__"${selected === "__wall__" ? " selected" : ""}>Wall Video</option>`];
    const sourceOptions = [...(state.encoders || [])];
    if (currentWall?.source_snapshot && currentWall.source_id && !sourceOptions.some(e => e.id === currentWall.source_id)) {
      sourceOptions.push(currentWall.source_snapshot);
    }
    if (cell.override_source_snapshot && cell.override_source_id && !sourceOptions.some(e => e.id === cell.override_source_id)) {
      sourceOptions.push(cell.override_source_snapshot);
    }
    for (const encoder of sourceOptions) {
      const value = encoder.id || wallDeviceId(encoder);
      parts.push(`<option value="${escapeHtml(value)}"${value === selected ? " selected" : ""}>${escapeHtml(sourceLabel(encoder))}</option>`);
    }
    return parts.join("");
  }

  function selectedSourceId(cell) {
    return cell.source_mode === "override" ? cell.override_source_id : "__wall__";
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

  function renderGrid() {
    const grid = document.getElementById("wallGrid");
    const summary = document.getElementById("gridSummary");
    if (!currentWall) {
      grid.innerHTML = `<div class="empty">Create or select a video wall.</div>`;
      summary.textContent = "";
      return;
    }
    const compact = currentWall.rows * currentWall.columns > 9;
    grid.style.gridTemplateColumns = `repeat(${currentWall.columns}, minmax(160px, 1fr))`;
    summary.textContent = `${currentWall.rows} x ${currentWall.columns} (${currentWall.cells.length} displays)`;
    grid.innerHTML = "";
    for (const cell of currentWall.cells) {
      const key = `${cell.row}:${cell.column}`;
      const report = reports[cell.decoder_id] || {};
      const hasReport = cell.decoder_id && Object.prototype.hasOwnProperty.call(reports, cell.decoder_id);
      const diffs = hasReport ? cellDiffs(currentWall, cell, report) : [];
      const sourceBusy = cell.live_source_pending;
      const cellEl = document.createElement("div");
      cellEl.className = `wall-cell ${compact ? "compact" : ""} ${key === selectedPos ? "selected" : ""}`;
      cellEl.dataset.pos = key;
      cellEl.innerHTML = `
        <div class="head">
          <div class="title">R${cell.row} C${cell.column}</div>
          <span class="status-chip ${sourceBusy ? "modified" : (diffs.length ? "modified" : (report.online ? "healthy" : ""))}">${sourceBusy ? "Routing" : (cell.decoder_id ? (diffs.length ? "Diff" : (hasReport ? (report.online ? "OK" : "Offline") : "Saved")) : "Empty")}</span>
        </div>
        <div class="field"><label>Decoder</label><select data-action="decoder">${optionHtml(decoderOptionsForCell(cell), cell.decoder_id, "Unassigned")}</select></div>
        <div class="cell-row">
          <div class="field"><label>Source</label><select data-action="source" data-last="${escapeHtml(selectedSourceId(cell))}">${sourceOptionsForCell(cell)}</select></div>
          <div class="field"><label>Rotation</label><select data-action="rotation">
            ${[0, 90, 180, 270].map(v => `<option value="${v}"${Number(cell.rotation || 0) === v ? " selected" : ""}>${v}</option>`).join("")}
          </select></div>
        </div>
      `;
      grid.appendChild(cellEl);
    }
    renderDetails();
  }

  function selectedCell() {
    if (!currentWall) return null;
    return currentWall.cells.find(cell => `${cell.row}:${cell.column}` === selectedPos) || currentWall.cells[0] || null;
  }

  function renderDetails() {
    const details = document.getElementById("details");
    const identifyBtn = document.getElementById("identifyBtn");
    const cell = selectedCell();
    if (!cell) {
      details.innerHTML = `<div class="empty">No display selected.</div>`;
      if (identifyBtn) identifyBtn.disabled = true;
      return;
    }
    const decoders = idMap(state.decoders);
    const decoder = findDeviceWithSnapshot(cell.decoder_id, state.decoders, cell.decoder_snapshot);
    if (identifyBtn) {
      identifyBtn.disabled = !(decoder && decoder.ip);
      identifyBtn.dataset.ip = decoder?.ip || "";
      identifyBtn.dataset.model = (decoder?.model || "").toLowerCase();
    }
    const report = reports[cell.decoder_id] || {};
    const diffs = cellDiffs(currentWall, cell, report);
    const sourceDevice = cell.source_mode === "override"
      ? findDeviceWithSnapshot(cell.override_source_id, state.encoders, cell.override_source_snapshot)
      : findDeviceWithSnapshot(currentWall.source_id, state.encoders, currentWall.source_snapshot);
    const source = sourceDevice ? sourceLabel(sourceDevice) : "";
    const perDisplay = !document.getElementById("applyBezelAll").checked;
    const display = { ...currentWall.display, ...(cell.display || {}) };
    details.innerHTML = `
      <div class="kv"><span>Position</span><span>Row ${cell.row}, Column ${cell.column}</span></div>
      <div class="kv"><span>Decoder</span><span>${escapeHtml(deviceLabel(decoder))}</span></div>
      <div class="kv"><span>IP</span><span>${escapeHtml(decoder ? decoder.ip : "")}</span></div>
      <div class="kv"><span>Source</span><span>${escapeHtml(source || "Not selected")}</span></div>
      <div class="kv"><span>Mode</span><span>${cell.source_mode === "override" ? "Override source" : "Wall video"}</span></div>
      <div class="kv"><span>Rotation</span><span>${Number(cell.rotation || 0)}</span></div>
      <div class="kv"><span>Reported</span><span>${escapeHtml(report.online ? "Online" : (cell.decoder_id ? "Not refreshed/offline" : "No decoder"))}</span></div>
      ${perDisplay ? `
        <div class="panel-title" style="margin-top:8px;margin-bottom:0;"><h3>Display Bezel Override</h3></div>
        <div class="bezel-grid">
          ${[
            ["totalwidth", "Total W"],
            ["totalheight", "Total H"],
            ["topborder", "Top"],
            ["bottomborder", "Bottom"],
            ["leftborder", "Left"],
            ["rightborder", "Right"]
          ].map(([key, label]) => `<div class="field"><label>${label}</label><input data-action="cell-bezel" data-key="${key}" type="number" min="0" value="${Number(display[key] || 0)}"></div>`).join("")}
        </div>
      ` : ""}
      ${diffs.length ? `<ul class="diff-list">${diffs.map(d => `<li>${escapeHtml(d)}</li>`).join("")}</ul>` : ""}
    `;
  }

  function renderReports() {
    const el = document.getElementById("reports");
    const count = document.getElementById("reportCount");
    const entries = Object.entries(reports);
    count.textContent = entries.length ? `${entries.length} checked` : "";
    if (!entries.length) {
      el.textContent = "Refresh from devices to compare saved state with decoder state.";
      return;
    }
    el.classList.remove("muted");
    el.innerHTML = entries.map(([id, report]) => {
      const vw = report.video_wall || {};
      return `<div class="kv"><span>${escapeHtml(report.label || id)}</span><span>${report.online ? "online" : "offline"} ${vw.rows ? `- ${vw.rows}x${vw.columns} r${vw.row} c${vw.column}` : ""}</span></div>`;
    }).join("");
  }

  function updateBezelSizeLabel() {
    const label = document.getElementById("bezelSizeLabel");
    const screen = label ? label.closest(".bezel-screen") : null;
    if (!label) return;
    const w = document.getElementById("totalWidth")?.value || 0;
    const h = document.getElementById("totalHeight")?.value || 0;
    label.textContent = `${w} x ${h}`;
    const width = Math.max(1, Number.parseInt(w, 10) || 16);
    const height = Math.max(1, Number.parseInt(h, 10) || 9);
    if (screen) screen.style.setProperty("--bezel-ratio", `${width} / ${height}`);
  }

  function render() {
    renderSelectors();
    syncFormFromWall();
    renderGrid();
    syncDisplayFormFromSelected();
    renderReports();
    setStatus(statusFromReports(currentWall, reports));
  }

  function replaceCurrentInState() {
    if (!currentWall) return;
    const idx = state.walls.findIndex(w => w.id === currentWall.id);
    if (idx >= 0) state.walls[idx] = currentWall;
    else state.walls.push(currentWall);
  }

  async function loadState() {
    const data = await api("/api/video_wall/state");
    const stateFileInfo = document.getElementById("stateFileInfo");
    if (stateFileInfo) stateFileInfo.textContent = data.state_file ? `Config file: ${data.state_file}` : "";
    state = {
      walls: (data.walls || []).map(normalizeWall),
      encoders: data.encoders || [],
      decoders: data.decoders || []
    };
    const remembered = rememberedWallId();
    currentWall = state.walls.find(w => w.id === remembered) || state.walls[0] || createWall("Video Wall 1", 2, 2);
    if (!state.walls.length) state.walls.push(currentWall);
    rememberSelectedWall(currentWall.id);
    render();
    refreshWall({ silent: true }).catch(e => toast(e.message, false));
  }

  async function saveWall() {
    syncWallFromForm();
    currentWall = enrichWallWithSnapshots(currentWall, state.encoders, state.decoders);
    const data = await api("/api/video_wall/save", { wall: currentWall });
    currentWall = normalizeWall(data.wall);
    state.encoders = data.encoders || state.encoders;
    state.decoders = data.decoders || state.decoders;
    replaceCurrentInState();
    rememberSelectedWall(currentWall.id);
    render();
    toast("Video wall saved", true);
    scheduleAutoSaveRefresh(100);
  }

  async function saveWallQuiet() {
    syncWallFromForm();
    currentWall = enrichWallWithSnapshots(currentWall, state.encoders, state.decoders);
    const data = await api("/api/video_wall/save", { wall: currentWall });
    currentWall = normalizeWall(data.wall);
    state.encoders = data.encoders || state.encoders;
    state.decoders = data.decoders || state.decoders;
    replaceCurrentInState();
    rememberSelectedWall(currentWall.id);
    return data;
  }

  async function liveRouteSource(scope, cell) {
    syncWallFromForm();
    replaceCurrentInState();
    const seq = ++liveRouteSeq;
    const targets = scope === "wall"
      ? (currentWall.cells || []).filter(c => c.decoder_id && (c.source_mode || "wall") === "wall")
      : (cell && cell.decoder_id ? [cell] : []);
    if (!targets.length) {
      toast("Assign a decoder before routing source", false);
      return;
    }
    for (const target of targets) target.live_source_pending = true;
    renderGrid();
    setStatus("Syncing source...", "modified");
    try {
      await api("/api/video_wall/source", {
        wall: currentWall,
        scope,
        row: cell ? cell.row : undefined,
        column: cell ? cell.column : undefined
      });
      await saveWallQuiet();
      if (seq === liveRouteSeq) {
        reports = {};
        setStatus("Source synced", "healthy");
        toast("Source synced", true);
        refreshWall({ silent: true }).catch(() => {});
      }
    } catch (e) {
      if (seq === liveRouteSeq) {
        setStatus("Route failed", "error");
        toast(e.message, false);
      }
    } finally {
      for (const target of currentWall.cells || []) delete target.live_source_pending;
      renderGrid();
    }
  }

  function routeSelectedWallSource() {
    syncWallFromForm();
    snapAllDisplaysToWallVideo(currentWall);
    liveRouteSource("wall").catch(e => toast(e.message, false));
  }

  async function liveApplyBezel() {
    syncWallFromForm();
    replaceCurrentInState();
    updateBezelSizeLabel();
    const applyAll = document.getElementById("applyBezelAll").checked;
    const cell = selectedCell();
    const targets = applyAll
      ? (currentWall.cells || []).filter(c => c.decoder_id)
      : (cell && cell.decoder_id ? [cell] : []);
    if (!targets.length) {
      toast(applyAll ? "Assign decoders before applying bezel" : "Select an assigned display before applying bezel", false);
      render();
      return;
    }
    setStatus(applyAll ? "Applying bezel to all..." : "Applying bezel...", "modified");
    try {
      const data = await api("/api/video_wall/bezel", {
        wall: currentWall,
        scope: applyAll ? "wall" : "cell",
        row: cell ? cell.row : undefined,
        column: cell ? cell.column : undefined
      });
      await saveWallQuiet();
      reports = {};
      const count = data.success_count || Object.values(data.results || {}).filter(r => r.ok).length;
      toast(applyAll ? `Bezel applied to ${count} display(s)` : "Bezel applied to selected display", true);
      refreshWall({ silent: true }).catch(() => {});
    } catch (e) {
      setStatus("Bezel apply failed", "error");
      toast(e.message, false);
    }
  }

  async function refreshWall(options = {}) {
    const data = await api("/api/video_wall/refresh", { id: currentWall.id });
    state.encoders = data.encoders || state.encoders;
    state.decoders = data.decoders || state.decoders;
    reports = data.reports || {};
    render();
    setStatus(data.status || statusFromReports(currentWall, reports));
    if (!options.silent) toast("Device state refreshed", true);
  }

  function scheduleAutoSaveRefresh(delay = 500) {
    if (autoRefreshTimer) window.clearTimeout(autoRefreshTimer);
    const wallId = currentWall?.id || "";
    autoRefreshTimer = window.setTimeout(async () => {
      autoRefreshTimer = null;
      if (!wallId || currentWall?.id !== wallId) return;
      try {
        await saveWallQuiet();
        if (currentWall?.id !== wallId) return;
        await refreshWall({ silent: true });
      } catch (e) {
        setStatus("Auto refresh failed", "error");
        toast(e.message, false);
      }
    }, delay);
  }

  async function applyWall() {
    await saveWallQuiet();
    const data = await api("/api/video_wall/apply", { id: currentWall.id });
    const failed = Object.values(data.results || {}).filter(r => !r.ok);
    toast(failed.length ? `Applied with ${failed.length} display error(s)` : "Video wall applied", !failed.length);
    reports = {};
    render();
    refreshWall({ silent: true }).catch(() => {});
  }

  async function deleteWall() {
    if (!currentWall) return;
    const ok = await window.lumaDialog.confirm({
      title: "Delete Video Wall",
      message: `Delete "${currentWall.name}" from the saved wall configurations?`,
      confirmText: "Delete",
      destructive: true
    });
    if (!ok) return;
    await api("/api/video_wall/delete", { id: currentWall.id });
    state.walls = state.walls.filter(w => w.id !== currentWall.id);
    currentWall = state.walls[0] || createWall("Video Wall 1", 2, 2);
    if (!state.walls.length) state.walls.push(currentWall);
    rememberSelectedWall(currentWall.id);
    reports = {};
    render();
    toast("Video wall deleted", true);
  }

  function configDownloadName() {
    const base = String(currentWall?.name || "video-wall")
      .trim()
      .replace(/[^a-z0-9._-]+/gi, "-")
      .replace(/^-+|-+$/g, "")
      .toLowerCase() || "video-wall";
    return `${base}-video-walls.json`;
  }

  async function exportWallConfig() {
    const ok = await window.lumaDialog.confirm({
      title: "Export Video Wall Config",
      message: "This will download the entire video wall configuration file, including all saved walls and assigned decoder/source snapshots.\n\nRename the downloaded file for easy job-site recall.",
      confirmText: "Export"
    });
    if (!ok) return;
    if (currentWall) {
      syncWallFromForm();
      currentWall = enrichWallWithSnapshots(currentWall, state.encoders, state.decoders);
      replaceCurrentInState();
    }
    const payload = JSON.stringify({
      kind: "lumasuite.video_walls",
      version: 1,
      exported_at: new Date().toISOString(),
      walls: state.walls.map(normalizeWall)
    }, null, 2);
    const url = URL.createObjectURL(new Blob([payload], { type: "application/json" }));
    const a = document.createElement("a");
    a.href = url;
    a.download = configDownloadName();
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    toast("Video wall config exported", true);
  }

  async function importWallConfig(file) {
    if (!file) return;
    const text = await file.text();
    let payload;
    try {
      payload = JSON.parse(text);
    } catch {
      throw new Error("Import file is not valid JSON");
    }
    if (!payload || payload.kind !== "lumasuite.video_walls" || !Array.isArray(payload.walls)) {
      throw new Error("Import file is not a LumaSuite video wall config");
    }
    const incomingWalls = payload.walls;
    if (!incomingWalls.every(wall => wall && Array.isArray(wall.cells) && wall.cells.every(cell => cell && "row" in cell && "column" in cell))) {
      throw new Error("Video wall config contains invalid wall/display data");
    }
    const incomingCount = Array.isArray(incomingWalls) ? incomingWalls.length : 0;
    const ok = await window.lumaDialog.confirm({
      title: "Replace Video Wall Config",
      message: `Import "${file.name}"?\n\nThe current video wall configurations will be replaced.\n\nCurrent walls: ${state.walls.length}\nImported walls: ${incomingCount}`,
      confirmText: "Replace Config",
      destructive: true
    });
    if (!ok) return;
    const data = await api("/api/video_wall/import", payload);
    state = {
      walls: (data.walls || []).map(normalizeWall),
      encoders: data.encoders || state.encoders,
      decoders: data.decoders || state.decoders
    };
    currentWall = state.walls[0] || createWall("Video Wall 1", 2, 2);
    if (!state.walls.length) state.walls.push(currentWall);
    rememberSelectedWall(currentWall.id);
    selectedPos = "1:1";
    reports = {};
    render();
    toast(`Imported ${state.walls.length} wall config(s)`, true);
    refreshWall({ silent: true }).catch(() => {});
  }

  async function blinkSelectedDecoder() {
    const btn = document.getElementById("identifyBtn");
    const cell = selectedCell();
    const decoder = cell ? findDeviceWithSnapshot(cell.decoder_id, state.decoders, cell.decoder_snapshot) : null;
    const ip = decoder?.ip || "";
    if (!btn || !ip) {
      toast("Select a display with an assigned decoder first", false);
      return;
    }
    const isCS31 = String(decoder.model || "").toLowerCase().includes("at-ome-cs31");
    const originalText = btn.textContent;
    btn.disabled = true;
    btn.textContent = "Blinking";
    try {
      if (isCS31) {
        const getResult = await api("/api/producer/jsonrpc", { ip, method: "System.Get", params: {} });
        const currentState = getResult.result?.blinkled || false;
        await api("/api/producer/jsonrpc", { ip, method: "SystemBlinkLed.Set", params: !currentState });
      } else {
        await api("/api/producer/jsonrpc", { ip, method: "FrontPanelBlinkLed.Set", params: {} });
      }
      toast("Decoder blink command sent", true);
      window.setTimeout(() => {
        btn.textContent = originalText;
        renderDetails();
      }, isCS31 ? 1000 : 30000);
    } catch (e) {
      toast(e.message, false);
      btn.textContent = originalText;
      renderDetails();
    }
  }

  function wireEvents() {
    document.getElementById("wallSelect").addEventListener("change", event => {
      currentWall = state.walls.find(w => w.id === event.target.value) || currentWall;
      rememberSelectedWall(currentWall?.id);
      reports = {};
      selectedPos = "1:1";
      render();
      refreshWall({ silent: true }).catch(() => {});
    });
    document.getElementById("newWallBtn").addEventListener("click", () => {
      currentWall = createWall(`Video Wall ${state.walls.length + 1}`, 2, 2);
      state.walls.push(currentWall);
      rememberSelectedWall(currentWall.id);
      reports = {};
      selectedPos = "1:1";
      render();
    });
    document.getElementById("saveWallBtn").addEventListener("click", () => saveWall().catch(e => toast(e.message, false)));
    document.getElementById("refreshWallBtn").addEventListener("click", () => refreshWall().catch(e => toast(e.message, false)));
    document.getElementById("applyWallBtn").addEventListener("click", () => applyWall().catch(e => toast(e.message, false)));
    document.getElementById("deleteWallBtn").addEventListener("click", () => deleteWall().catch(e => toast(e.message, false)));
    document.getElementById("identifyBtn").addEventListener("click", () => blinkSelectedDecoder());
    document.getElementById("routeWallSourceBtn").addEventListener("click", () => routeSelectedWallSource());
    document.getElementById("exportWallConfigBtn").addEventListener("click", () => exportWallConfig().catch(e => toast(e.message, false)));
    document.getElementById("importWallConfigBtn").addEventListener("click", () => document.getElementById("importWallConfigFile").click());
    document.getElementById("importWallConfigFile").addEventListener("change", event => {
      importWallConfig(event.target.files?.[0]).catch(e => toast(e.message, false));
      event.target.value = "";
    });

    for (const id of ["wallName", "rowsInput", "colsInput"]) {
      document.getElementById(id).addEventListener("change", () => {
        syncWallFromForm();
        replaceCurrentInState();
        reports = {};
        render();
        scheduleAutoSaveRefresh();
      });
    }
    for (const id of ["totalWidth", "totalHeight", "topBorder", "bottomBorder", "leftBorder", "rightBorder"]) {
      document.getElementById(id).addEventListener("change", () => liveApplyBezel());
    }
    document.getElementById("wallSource").addEventListener("change", () => {
      routeSelectedWallSource();
    });

    document.getElementById("wallGrid").addEventListener("click", event => {
      if (event.target.closest("select, input, button, option, label")) return;
      const cellEl = event.target.closest(".wall-cell");
      if (!cellEl) return;
      selectedPos = cellEl.dataset.pos;
      renderGrid();
      syncDisplayFormFromSelected();
    });

    document.getElementById("wallGrid").addEventListener("change", event => {
      const select = event.target.closest("select");
      const cellEl = event.target.closest(".wall-cell");
      if (!select || !cellEl || !currentWall) return;
      const cell = currentWall.cells.find(c => `${c.row}:${c.column}` === cellEl.dataset.pos);
      if (!cell) return;
      if (select.dataset.action === "decoder") {
        cell.decoder_id = select.value;
        cell.decoder_snapshot = compactDeviceSnapshot(idMap(state.decoders).get(cell.decoder_id)) || cell.decoder_snapshot || null;
      }
      if (select.dataset.action === "source") {
        if (select.value === "__wall__") {
          cell.source_mode = "wall";
          cell.override_source_id = "";
          cell.override_source_snapshot = null;
        } else {
          cell.source_mode = "override";
          cell.override_source_id = select.value;
          cell.override_source_snapshot = compactDeviceSnapshot(idMap(state.encoders).get(cell.override_source_id)) || cell.override_source_snapshot || null;
        }
        liveRouteSource("cell", cell);
        return;
      }
      if (select.dataset.action === "rotation") cell.rotation = clampInt(select.value, 0, 270, 0);
      reports = {};
      replaceCurrentInState();
      renderGrid();
      setStatus("Unsaved changes", "modified");
      scheduleAutoSaveRefresh();
    });

    document.getElementById("details").addEventListener("change", event => {
      const input = event.target.closest("input[data-action='cell-bezel']");
      const cell = selectedCell();
      if (!input || !cell) return;
      cell.display = { ...(cell.display || {}) };
      cell.display[input.dataset.key] = clampInt(input.value, 0, 999999, 0);
      replaceCurrentInState();
      reports = {};
      setStatus("Unsaved changes", "modified");
      scheduleAutoSaveRefresh();
    });

    document.getElementById("applyBezelAll").addEventListener("change", () => {
      if (document.getElementById("applyBezelAll").checked) setDisplayFormValues(currentWall.display || DEFAULT_DISPLAY);
      else syncDisplayFormFromSelected();
      renderDetails();
    });
  }

  if (typeof window !== "undefined") {
    window.__videoWallTest = {
      clampInt,
      wallDeviceId,
      createWall,
      normalizeWall,
      resizeWall,
      availableDecodersForCell,
      enrichWallWithSnapshots,
      snapAllDisplaysToWallVideo,
      sourceLabel,
      cellDiffs,
      statusFromReports
    };
    document.addEventListener("DOMContentLoaded", () => {
      wireEvents();
      loadState().catch(e => {
        setStatus("Load failed", "error");
        toast(e.message, false);
      });
    });
  }
})();
