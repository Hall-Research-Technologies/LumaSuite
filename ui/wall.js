(function(){
  "use strict";

  const MAX_GRID = 16;
  const WALL_PREVIEW_KEY = "lumasuite_video_wall_preview_enabled";
  const WALL_PREVIEW_OPACITY_KEY = "lumasuite_video_wall_preview_opacity";
  const WALL_PREVIEW_REFRESH_KEY = "lumasuite_video_wall_preview_refresh_seconds";
  const DEFAULT_WALL_PREVIEW_OPACITY = 100;
  const DEFAULT_WALL_PREVIEW_REFRESH_SECONDS = 10;
  const WALL_PREVIEW_PRELOAD_TIMEOUT_MS = 3000;
  const WALL_CELL_WIDTH = 340;
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
  // Per-decoder Video Wall mode as last READ FROM THE DECODER (never from saved
  // membership): { [decoder_id]: {enabled, vwmatrix} | {error} | {pending:true} }.
  let wallMode = {};
  let liveRouteSeq = 0;
  let autoRefreshTimer = null;
  let renderDeferred = false;
  let wallPreviewEnabled = false;
  let wallPreviewOpacity = DEFAULT_WALL_PREVIEW_OPACITY;
  let wallPreviewRefreshSeconds = DEFAULT_WALL_PREVIEW_REFRESH_SECONDS;
  let wallPreviewRefreshTimer = null;
  let wallPreviewRefreshTimestamp = Date.now();
  let dragSourceId = null;
  let dragTargetCells = null;
  let dragHighlightedChip = null; // the zone-rail chip element currently drop-highlighted, if any
  let inspectorOpen = false;
  // ---- Zone editing (proposed-state-only, see enterZoneEditMode()) ------
  let zoneEditMode = false;
  let zoneDraft = null;          // working copy of currentWall.zones while editing; null when not editing
  let zoneDraftAnchor = null;    // {row,column} armed first corner (click-click creation/redefine)
  let zoneDraftHover = null;     // {row,column} current pointer cell while drawing
  let zoneDraftRedefineId = null; // id of an existing draft zone being redefined, or null when creating new
  // ---- Non-drag routing path (select source, then select target: routes at once) -
  let armedSourceId = null;
  // Dirty-state tracking for the "Save Changes" Wall Actions menu item
  // (top-bar reorganization pass): most edits already auto-persist via
  // existing paths (decoder assignment's scheduleAutoSaveRefresh(), live
  // source/rotation/bezel writes' saveWallQuiet()) within a fraction of a
  // second, so this is a best-effort "is there anything NOT yet covered
  // by one of those" indicator, not a new persistence mechanism -- the
  // Save Changes button itself already existed and is unconditionally
  // safe to click regardless of this flag.
  let lastSavedWallSnapshot = "";
  // Wizard (Create/Edit Wall) state -- see openWallWizard()/wireWallWizard().
  // wizardWall is a proposed-state-only object: nothing here is written to
  // a device or video_walls.json until Step 2's Create/Save Changes button.
  let wizardMode = null; // "create" | "edit" | "review" | null
  let wizardWall = null;
  let wizardEditingId = null;
  let wizardArmedDecoderId = null;
  // The site-discovery candidate currently open in the wizard's "review"
  // mode (see openWallWizard("review", candidate)) -- null otherwise. Used
  // for the Why? evidence panel and to carry the candidate's
  // device_set_fingerprint onto the saved wall's confirmed_fingerprint at
  // Confirm & Save time. Never sent to a device.
  let wizardReviewCandidate = null;
  // Most recent classified candidate list from /api/video_wall/detect,
  // kept so Ignore/Review button clicks in the candidates section don't
  // need to re-parse a dataset attribute per row.
  let latestCandidates = [];
  // Config-file path: relocated out of permanent top-bar space into the
  // Wall Actions > Details popover (top-bar reorganization pass); still
  // sourced from the same GET /api/video_wall/state response as before.
  let lastStateFilePath = "";
  // Source hover preview (task 1): one card, one debounce timer, reused for
  // every source tile -- same interaction shape as OmniSuite Multiview's
  // schedulePreview()/closePreview(), but fetching LumaSuite's own existing
  // encoder thumbnail URL (decoderPreviewBaseUrl() works on any device with
  // an .ip, decoder or encoder) instead of an OmniStream-specific endpoint.
  const SOURCE_PREVIEW_DELAY_MS = 300;
  const sourcePreview = { timer: null, id: null, card: null, image: null };

  function isLiveFieldActive() {
    const active = document.activeElement;
    return !!(active && active.closest && active.closest("select, input, textarea"));
  }

  function requestRender(force = false) {
    if (!force && isLiveFieldActive()) {
      renderDeferred = true;
      return;
    }
    renderDeferred = false;
    render();
  }

  function flushDeferredRender() {
    if (!renderDeferred || isLiveFieldActive()) return;
    requestRender(true);
  }

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

  // A region is an additional source assignment spanning more than one
  // physical cell -- the spatial Video Wall's boundary/intersection drop
  // targets. It sits alongside each cell's own source_mode, never replacing
  // it, so a wall with no regions is unaffected by any of this. Mirrors
  // lumaserver.py's _vw_clean_regions() exactly: a region must be a
  // complete rectangle of cells within the wall's current bounds, and no
  // cell may belong to two regions.
  function cleanRegions(value, rows, columns) {
    const out = [];
    if (!Array.isArray(value)) return out;
    const claimed = new Set();
    for (const region of value) {
      if (!region || typeof region !== "object") continue;
      const cells = [];
      const seen = new Set();
      for (const rc of (Array.isArray(region.cells) ? region.cells : [])) {
        const r = clampInt(rc && rc.row, 1, MAX_GRID, 0);
        const c = clampInt(rc && rc.column, 1, MAX_GRID, 0);
        if (r < 1 || r > rows || c < 1 || c > columns) continue;
        const key = `${r}:${c}`;
        if (seen.has(key)) continue;
        seen.add(key);
        cells.push({ row: r, column: c });
      }
      if (cells.length < 2) continue;
      const rowsPresent = [...new Set(cells.map(c => c.row))].sort((a, b) => a - b);
      const colsPresent = [...new Set(cells.map(c => c.column))].sort((a, b) => a - b);
      if (cells.length !== rowsPresent.length * colsPresent.length) continue;
      // Matching count alone isn't enough -- rows {1,3} with row 2 absent
      // has the same count as a genuine 2-row rectangle but isn't physically
      // adjacent. Mirrors lumaserver.py's _vw_is_contiguous_run().
      const isContiguous = values => values.length > 0 && values[values.length - 1] - values[0] + 1 === values.length;
      if (!isContiguous(rowsPresent) || !isContiguous(colsPresent)) continue;
      if ([...seen].some(key => claimed.has(key))) continue;
      seen.forEach(key => claimed.add(key));
      out.push({
        id: String(region.id || newId().replace("wall-", "region-")),
        cells: cells.sort((a, b) => a.row - b.row || a.column - b.column),
        source_id: String(region.source_id || ""),
        source_snapshot: region.source_snapshot && typeof region.source_snapshot === "object" ? region.source_snapshot : null
      });
    }
    return out;
  }

  // ---- Saved zone definitions -------------------------------------------
  //
  // A zone is a SAVED, REUSABLE rectangle definition an operator can route a
  // source to later -- a different lifecycle from wall.regions (above),
  // which is live/active routing state that a single routing action
  // creates and "Apply to Entire Wall" clears. Mirrors lumaserver.py's
  // _vw_clean_zones()/_vw_zone_cells()/_vw_zone_label() exactly: a zone's
  // canonical shape is its rectangle's two corners (row1/column1 top-left,
  // row2/column2 bottom-right); member cells are always DERIVED from that,
  // never stored separately. Unlike regions, zones MAY overlap each other
  // ("Top Row" and "Left 2x2" can coexist) since they carry no source_id
  // and trigger no device call merely by existing -- routing to one just
  // creates an ordinary region over the same cells (see assignSpatialTarget).
  function cleanZones(value, rows, columns) {
    const out = [];
    if (!Array.isArray(value)) return out;
    const seenIds = new Set();
    for (const zone of value) {
      if (!zone || typeof zone !== "object") continue;
      const r1 = clampInt(zone.row1, 1, MAX_GRID, 0);
      const c1 = clampInt(zone.column1, 1, MAX_GRID, 0);
      const r2 = clampInt(zone.row2, 1, MAX_GRID, 0);
      const c2 = clampInt(zone.column2, 1, MAX_GRID, 0);
      const top = Math.min(r1, r2), bottom = Math.max(r1, r2);
      const left = Math.min(c1, c2), right = Math.max(c1, c2);
      if (top < 1 || left < 1 || bottom > rows || right > columns) continue;
      if ((bottom - top + 1) * (right - left + 1) < 2) continue;
      let zid = String(zone.id || "");
      if (!zid || seenIds.has(zid)) zid = newId().replace("wall-", "zone-");
      seenIds.add(zid);
      out.push({ id: zid, name: String(zone.name || "").trim(), row1: top, column1: left, row2: bottom, column2: right });
    }
    return out;
  }

  function zoneCellsOf(zone) {
    const cells = [];
    for (let r = zone.row1; r <= zone.row2; r++) {
      for (let c = zone.column1; c <= zone.column2; c++) cells.push({ row: r, column: c });
    }
    return cells;
  }

  function zoneLabel(zone) {
    const name = (zone.name || "").trim();
    if (name) return name;
    const rows = zone.row2 - zone.row1 + 1;
    const columns = zone.column2 - zone.column1 + 1;
    return `${rows}x${columns} · R${zone.row1}C${zone.column1}–R${zone.row2}C${zone.column2}`;
  }

  // Zones that would no longer fit inside newRows x newColumns -- callers
  // (the wizard's dimension-change confirmation) must warn about these and
  // get explicit operator consent BEFORE resizing; cleanZones() itself is
  // only the last-resort backstop that silently drops them if the operator
  // proceeds anyway, exactly mirroring how orphaned decoder assignments are
  // already handled.
  function zonesInvalidatedByResize(wall, newRows, newColumns) {
    return (wall.zones || []).filter(z => z.row2 > newRows || z.column2 > newColumns);
  }

  // 2x2 convenience targets (Top/Bottom/Left/Right row/column pairs) are
  // generated HERE, on the fly, from the same generic rectangle engine that
  // powers every saved zone -- never persisted, never a hidden zone record.
  // This replaces the old crosshair's hardcoded 2x2-only geometry with the
  // smallest model that still gives a 2x2 wall its four familiar pair
  // targets: any other wall size simply has zero auto zones, and relies
  // entirely on operator-defined saved zones instead.
  function autoZonesForWall(wall) {
    if (!wall || wall.rows !== 2 || wall.columns !== 2) return [];
    return [
      { id: "auto-top", name: "Top Row", row1: 1, column1: 1, row2: 1, column2: 2, auto: true },
      { id: "auto-bottom", name: "Bottom Row", row1: 2, column1: 1, row2: 2, column2: 2, auto: true },
      { id: "auto-left", name: "Left Column", row1: 1, column1: 1, row2: 2, column2: 1, auto: true },
      { id: "auto-right", name: "Right Column", row1: 1, column1: 2, row2: 2, column2: 2, auto: true }
    ];
  }

  // Every zone (saved + auto) that is currently a valid routing target,
  // each carrying its own derived `cells` -- the single source of truth
  // renderZoneRail()/assignSpatialTarget() consult.
  function zonesForRouting(wall) {
    if (!wall) return [];
    const saved = (wall.zones || []).map(z => ({ ...z, cells: zoneCellsOf(z) }));
    const auto = autoZonesForWall(wall).map(z => ({ ...z, cells: zoneCellsOf(z) }));
    return [...saved, ...auto];
  }

  function cellRegion(wall, cell) {
    for (const region of (wall && wall.regions) || []) {
      if ((region.cells || []).some(rc => rc.row === cell.row && rc.column === cell.column)) return region;
    }
    return null;
  }

  function regionGeometry(region, cell) {
    const rowsPresent = [...new Set((region.cells || []).map(c => c.row))].sort((a, b) => a - b);
    const colsPresent = [...new Set((region.cells || []).map(c => c.column))].sort((a, b) => a - b);
    const localRow = rowsPresent.indexOf(cell.row) + 1 || 1;
    const localCol = colsPresent.indexOf(cell.column) + 1 || 1;
    return { rows: rowsPresent.length || 1, columns: colsPresent.length || 1, row: localRow, column: localCol };
  }

  // Mirrors lumaserver.py's _vw_effective_matrix_and_source(): a region --
  // if this cell belongs to one -- wins over the cell's own source_mode.
  // That is the only precedence rule regions introduce; decoder assignment,
  // rotation and bezel stay tied to the cell regardless.
  function effectiveGeometryAndSource(wall, cell) {
    const region = cellRegion(wall, cell);
    if (region) {
      const geo = regionGeometry(region, cell);
      return { ...geo, enabled: true, sourceId: region.source_id || "" };
    }
    const mode = cell.source_mode || "wall";
    return {
      rows: wall.rows, columns: wall.columns, row: cell.row, column: cell.column,
      enabled: mode === "wall",
      sourceId: mode === "wall" ? (wall.source_id || "") : (cell.override_source_id || "")
    };
  }

  function normalizeWall(input) {
    const wall = input && typeof input === "object" ? { ...input } : {};
    wall.id = wall.id || newId();
    wall.name = String(wall.name || "Video Wall").trim() || "Video Wall";
    wall.rows = clampInt(wall.rows, 1, MAX_GRID, 2);
    wall.columns = clampInt(wall.columns, 1, MAX_GRID, 2);
    wall.source_id = String(wall.source_id || "");
    wall.display = { ...DEFAULT_DISPLAY, ...(wall.display || {}) };
    wall.regions = cleanRegions(wall.regions, wall.rows, wall.columns);
    wall.zones = cleanZones(wall.zones, wall.rows, wall.columns);

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
    // "Apply to Entire Wall" means every temporary spatial combination is
    // broken, not just per-cell overrides -- a boundary/pair region wins
    // over a cell's own source_mode (effectiveGeometryAndSource(), and the
    // backend's /api/video_wall/source scope="wall" handler deliberately
    // excludes any cell still in a region, per its own comment), so a
    // region left in place here would silently survive an "apply to
    // entire wall" click on both the device and the saved config. Regions
    // are a pure app-side construct (see cleanRegions()/the "Detach from
    // region" flow) -- dissolving them is just clearing this array, the
    // same operation "Detach from region" already performs per-cell.
    if (wall) wall.regions = [];
    return wall;
  }

  function statusClass(status) {
    return String(status || "").toLowerCase();
  }

  function statusFromReports(wall, reportMap) {
    if (!wall) return "Unsaved";
    const cells = wall.cells || [];
    if (cells.some(c => c.source_mode === "override" || cellRegion(wall, c))) return "Override";
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
    const geo = effectiveGeometryAndSource(wall, cell);
    const expected = { rows: geo.rows, columns: geo.columns, row: geo.row, column: geo.column };
    for (const key of Object.keys(expected)) {
      if (vw[key] != null && Number(vw[key]) !== Number(expected[key])) {
        diffs.push(`${key}: saved ${expected[key]}, reported ${vw[key]}`);
      }
    }
    if (vw.vmenable != null && Boolean(vw.vmenable) !== geo.enabled) {
      diffs.push(`wall mode: saved ${geo.enabled ? "on" : "off"}, reported ${vw.vmenable ? "on" : "off"}`);
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

  function summarizeResultFailures(data) {
    const failures = [];
    for (const [id, result] of Object.entries(data?.results || {})) {
      if (result?.ok) continue;
      const step = (result?.steps || []).find(item => !item.ok) || {};
      const label = result?.label || id;
      const detail = step.error || result?.error || "failed";
      failures.push(`${label}: ${step.step || "apply"} ${detail}`);
    }
    return failures;
  }

  async function api(path, body, options = {}) {
    const response = await fetch(path, {
      method: body == null ? "GET" : "POST",
      headers: body == null ? undefined : { "Content-Type": "application/json" },
      body: body == null ? undefined : JSON.stringify(body)
    });
    const data = await response.json().catch(() => ({}));
    if (response.status === 404 && path.startsWith("/api/video_wall/")) {
      throw new Error("Video Wall backend is not loaded. Restart LumaSuite and refresh this page.");
    }
    if (!response.ok || (data.ok === false && !options.allowOkFalse)) {
      const failures = summarizeResultFailures(data);
      const message = data.error || failures.slice(0, 2).join("; ") || `Request failed: ${response.status}`;
      const error = new Error(message);
      error.data = data;
      error.status = response.status;
      throw error;
    }
    return data;
  }

  function syncFormFromWall() {
    if (!currentWall) return;
    // Name/Rows/Columns no longer live on this page at all (top-bar
    // reorganization pass) -- they are edited exclusively through the
    // Create/Edit Wall wizard, which writes them straight onto
    // currentWall/wizardWall itself rather than through a form field
    // here. Only Wall Source and bezel remain page-level form state.
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
    // Name/Rows/Columns are no longer read from this page's form (top-bar
    // reorganization pass) -- see syncFormFromWall(). currentWall already
    // carries the correct values, set either by loadState()/wallSelect,
    // or by the wizard's own Create/Save action (wizardFinish()).
    currentWall.source_id = document.getElementById("wallSource").value || "";
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
    if (currentWall) {
      const requestedSourceId = currentWall.source_id || "";
      wallSource.value = requestedSourceId;
      if (requestedSourceId && wallSource.value !== requestedSourceId) {
        currentWall.source_id = "";
        currentWall.source_snapshot = null;
        wallSource.value = "";
      }
    }
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

  function loadWallPreviewEnabled() {
    try {
      return localStorage.getItem(WALL_PREVIEW_KEY) === "true";
    } catch {
      return false;
    }
  }

  function saveWallPreviewEnabled(enabled) {
    try {
      localStorage.setItem(WALL_PREVIEW_KEY, enabled ? "true" : "false");
    } catch {}
  }

  function loadWallPreviewOpacity() {
    try {
      const value = Number(localStorage.getItem(WALL_PREVIEW_OPACITY_KEY));
      return Number.isFinite(value) ? clampInt(value, 20, 100, DEFAULT_WALL_PREVIEW_OPACITY) : DEFAULT_WALL_PREVIEW_OPACITY;
    } catch {
      return DEFAULT_WALL_PREVIEW_OPACITY;
    }
  }

  function saveWallPreviewOpacity(value) {
    try {
      localStorage.setItem(WALL_PREVIEW_OPACITY_KEY, String(clampInt(value, 20, 100, DEFAULT_WALL_PREVIEW_OPACITY)));
    } catch {}
  }

  function loadWallPreviewRefreshSeconds() {
    try {
      const value = Number(localStorage.getItem(WALL_PREVIEW_REFRESH_KEY));
      return Number.isFinite(value) ? clampInt(value, 2, 10, DEFAULT_WALL_PREVIEW_REFRESH_SECONDS) : DEFAULT_WALL_PREVIEW_REFRESH_SECONDS;
    } catch {
      return DEFAULT_WALL_PREVIEW_REFRESH_SECONDS;
    }
  }

  function saveWallPreviewRefreshSeconds(value) {
    try {
      localStorage.setItem(WALL_PREVIEW_REFRESH_KEY, String(clampInt(value, 2, 10, DEFAULT_WALL_PREVIEW_REFRESH_SECONDS)));
    } catch {}
  }

  function updateWallPreviewControls() {
    const opacityControl = document.getElementById("wallPreviewOpacityControl");
    const opacitySlider = document.getElementById("wallPreviewOpacity");
    const refreshControl = document.getElementById("wallPreviewRefreshControl");
    const refreshSlider = document.getElementById("wallPreviewRefreshSeconds");
    const refreshValue = document.getElementById("wallPreviewRefreshValue");
    if (opacityControl) opacityControl.style.display = wallPreviewEnabled ? "flex" : "none";
    if (opacitySlider) opacitySlider.value = String(wallPreviewOpacity);
    if (refreshControl) refreshControl.style.display = wallPreviewEnabled ? "flex" : "none";
    if (refreshSlider) refreshSlider.value = String(wallPreviewRefreshSeconds);
    if (refreshValue) refreshValue.textContent = `${wallPreviewRefreshSeconds}s`;
  }

  function decoderPreviewBaseUrl(decoder) {
    const ip = decoder?.ip || "";
    return ip ? `http://${ip}/thumbnail/thumbnail1.jpg` : "";
  }

  function previewUrlWithCacheBuster(base, timestamp = wallPreviewRefreshTimestamp) {
    if (!base) return "";
    return `${base}${base.includes("?") ? "&" : "?"}t=${timestamp}`;
  }

  function preloadWallPreviewImage(url) {
    return new Promise(resolve => {
      if (!url) {
        resolve(false);
        return;
      }
      const img = new Image();
      let done = false;
      const finish = ok => {
        if (done) return;
        done = true;
        window.clearTimeout(timer);
        img.onload = null;
        img.onerror = null;
        resolve(ok);
      };
      const timer = window.setTimeout(() => finish(false), WALL_PREVIEW_PRELOAD_TIMEOUT_MS);
      img.onload = () => finish(true);
      img.onerror = () => finish(false);
      img.src = url;
    });
  }

  // `cellKeys`, when given (a Set of "row:column" strings), scopes the
  // refresh to only those cells' preview elements -- used by the post-route
  // burst below so a single-display or zone route never re-fetches every
  // decoder on the wall. Omitted (the periodic-timer call site), it behaves
  // exactly as before: every visible preview. Cache-busting
  // (previewUrlWithCacheBuster's `?t=<timestamp>` query param, already
  // present before this change) is what makes a scoped or full refresh
  // actually reach the decoder instead of the browser's HTTP cache -- see
  // PROJECT.md's Video Wall preview-refresh section for the investigation
  // that confirmed this was already correct and not a contributor to the
  // reported delay.
  async function refreshWallPreviewImages(cellKeys = null) {
    if (!wallPreviewEnabled) return;
    let previews = Array.from(document.querySelectorAll(".wall-cell-preview[data-preview-base]"));
    if (cellKeys) {
      previews = previews.filter(el => {
        const cellEl = el.closest(".wall-cell");
        return cellEl && cellKeys.has(cellEl.dataset.pos);
      });
    }
    if (!previews.length) return;
    const timestamp = Date.now();
    const loaded = await Promise.all(previews.map(async el => {
      const base = el.dataset.previewBase || "";
      const url = previewUrlWithCacheBuster(base, timestamp);
      return { el, url, ok: await preloadWallPreviewImage(url) };
    }));
    // Advance the shared timestamp even for a scoped refresh: renderGrid()
    // computes each cell's initial preview URL via
    // `previewUrlWithCacheBuster(previewBase)` (default timestamp =
    // wallPreviewRefreshTimestamp), so any render that happens after this
    // pass -- e.g. the refreshWall() re-render that follows a route -- must
    // not reconstruct a cell with an older cache-busting value than one
    // this pass already successfully fetched, or the browser would show the
    // stale cached image again until the next refresh.
    wallPreviewRefreshTimestamp = timestamp;
    for (const item of loaded) {
      if (item.ok) item.el.style.backgroundImage = `url('${item.url}')`;
    }
  }

  // ---- Post-route preview burst -----------------------------------------
  // Desired UX (see PROJECT.md): the routing operation itself must never
  // wait for a preview; the affected preview(s) should instead be refreshed
  // with a short, front-loaded burst of attempts right after a successful
  // route, tapering off, rather than left to wait for the next tick of the
  // slow (2-10s, default 10s) periodic timer (scheduleWallPreviewRefresh()
  // above), which is what previously caused the observed ~10s worst-case
  // staleness -- routing didn't trigger any refresh of its own, so the new
  // source only appeared whenever the *unrelated* periodic timer next fired.
  // A fresh route supersedes (via previewBurstSeq) any burst already in
  // flight for a previous route rather than letting two overlapping bursts
  // hammer the same decoder; each burst runs a fixed, finite list of delays
  // and then stops on its own -- it never becomes a fast poll loop.
  const PREVIEW_BURST_DELAYS_MS = [0, 700, 1500, 3000];
  let previewBurstSeq = 0;

  function burstRefreshPreviewsForCells(cells) {
    if (!wallPreviewEnabled || !cells || !cells.length) return;
    const keys = new Set(cells.map(c => `${c.row}:${c.column}`));
    const seq = ++previewBurstSeq;
    const step = index => {
      if (seq !== previewBurstSeq || !wallPreviewEnabled) return;
      refreshWallPreviewImages(keys).catch(() => {}).then(() => {
        if (seq !== previewBurstSeq || !wallPreviewEnabled) return;
        const next = index + 1;
        if (next >= PREVIEW_BURST_DELAYS_MS.length) return;
        window.setTimeout(() => step(next), PREVIEW_BURST_DELAYS_MS[next] - PREVIEW_BURST_DELAYS_MS[index]);
      });
    };
    step(0);
  }

  // Positions any fixed-position floating card (source hover preview, the
  // Selected Display inspector) next to an anchor element -- beside it when
  // there's room, flipped to the other side or clamped within the viewport
  // otherwise. Mirrors OmniSuite Multiview's positionPreview() exactly; a
  // card that ends up off-screen is worse than no card.
  function positionFloating(card, anchor) {
    if (!card || !anchor) return;
    const rect = anchor.getBoundingClientRect();
    card.style.visibility = "hidden";
    card.style.left = "0px";
    card.style.top = "0px";
    const size = card.getBoundingClientRect();
    const gap = 12;
    let left = rect.right + gap;
    if (left + size.width > window.innerWidth - 8) {
      left = rect.left - size.width - gap;
    }
    if (left < 8) left = Math.max(8, window.innerWidth - size.width - 8);
    let top = rect.top;
    if (top + size.height > window.innerHeight - 8) {
      top = window.innerHeight - size.height - 8;
    }
    if (top < 8) top = 8;
    card.style.left = `${Math.round(left)}px`;
    card.style.top = `${Math.round(top)}px`;
    card.style.visibility = "";
  }

  function sourcePreviewCard() {
    if (sourcePreview.card) return sourcePreview.card;
    const card = document.createElement("div");
    card.className = "source-preview";
    card.id = "sourcePreviewCard";
    card.setAttribute("role", "tooltip");
    card.hidden = true;
    document.body.appendChild(card);
    sourcePreview.card = card;
    return card;
  }

  // Everything that has to stop, in one place, so no caller (dragstart,
  // pointerleave, blur, Escape, a palette re-render) can stop only half of
  // it and leave a stale card or a stale pending fetch behind.
  function closeSourcePreview() {
    if (sourcePreview.timer) {
      window.clearTimeout(sourcePreview.timer);
      sourcePreview.timer = null;
    }
    sourcePreview.id = null;
    const card = sourcePreview.card;
    if (card) {
      if (sourcePreview.image) sourcePreview.image.removeAttribute("src");
      card.hidden = true;
      card.innerHTML = "";
    }
    sourcePreview.image = null;
  }

  function scheduleSourcePreview(tile, encoder) {
    closeSourcePreview();
    sourcePreview.timer = window.setTimeout(() => {
      sourcePreview.timer = null;
      openSourcePreview(tile, encoder);
    }, SOURCE_PREVIEW_DELAY_MS);
  }

  async function openSourcePreview(tile, encoder) {
    const id = encoder.id || wallDeviceId(encoder);
    sourcePreview.id = id;
    const base = decoderPreviewBaseUrl(encoder);
    const url = base ? previewUrlWithCacheBuster(base, Date.now()) : "";
    const ok = url ? await preloadWallPreviewImage(url) : false;
    // The pointer may have moved to a different tile (or left entirely)
    // while that image was loading -- a fetch resolving late must not
    // resurrect a preview for a source that's no longer hovered.
    if (sourcePreview.id !== id) return;
    renderSourcePreview(tile, encoder, ok ? url : "");
  }

  function renderSourcePreview(tile, encoder, url) {
    const card = sourcePreviewCard();
    card.innerHTML = "";
    const name = document.createElement("div");
    name.className = "source-preview-name";
    name.textContent = encoder.hostname || encoder.label || "Encoder";
    card.appendChild(name);
    const meta = document.createElement("div");
    meta.className = "source-preview-meta";
    meta.textContent = `${encoder.model || "Encoder"} · ${encoder.ip || ""}`;
    card.appendChild(meta);
    const frame = document.createElement("div");
    frame.className = "source-preview-frame";
    const id = encoder.id || wallDeviceId(encoder);
    if (url) {
      const img = document.createElement("img");
      sourcePreview.image = img;
      img.alt = `Live preview of ${encoder.hostname || "source"}`;
      img.addEventListener("error", () => {
        if (sourcePreview.id !== id) return;
        frame.innerHTML = "";
        const note = document.createElement("div");
        note.className = "source-preview-note";
        note.textContent = "Preview unavailable";
        frame.appendChild(note);
      });
      img.src = url;
      frame.appendChild(img);
    } else {
      const note = document.createElement("div");
      note.className = "source-preview-note";
      note.textContent = encoder.ip ? "Preview unavailable" : "No IP address on record";
      frame.appendChild(note);
    }
    card.appendChild(frame);
    card.hidden = false;
    positionFloating(card, tile);
  }

  function stopWallPreviewRefresh() {
    if (!wallPreviewRefreshTimer) return;
    window.clearTimeout(wallPreviewRefreshTimer);
    wallPreviewRefreshTimer = null;
  }

  function wallPreviewRefreshDelayMs() {
    return clampInt(wallPreviewRefreshSeconds, 2, 10, DEFAULT_WALL_PREVIEW_REFRESH_SECONDS) * 1000;
  }

  function scheduleWallPreviewRefresh(delay = wallPreviewRefreshDelayMs()) {
    stopWallPreviewRefresh();
    if (!wallPreviewEnabled) {
      return;
    }
    wallPreviewRefreshTimer = window.setTimeout(async () => {
      wallPreviewRefreshTimer = null;
      await refreshWallPreviewImages();
      if (wallPreviewEnabled) scheduleWallPreviewRefresh();
    }, delay);
  }

  function syncWallPreviewRefresh() {
    if (!wallPreviewEnabled) {
      stopWallPreviewRefresh();
      return;
    }
    if (wallPreviewRefreshTimer) return;
    scheduleWallPreviewRefresh();
  }

  function rotationClassValue(value) {
    const normalized = ((Number(value) % 360) + 360) % 360;
    return [0, 90, 180, 270].includes(normalized) ? normalized : 0;
  }

  function previewFitScale(rotation) {
    return rotation === 90 || rotation === 270 ? 0.5625 : 1;
  }

  function effectiveSourceDevice(wall, cell) {
    const geo = effectiveGeometryAndSource(wall, cell);
    if (!geo.sourceId) return null;
    const region = cellRegion(wall, cell);
    const snapshot = region ? region.source_snapshot
      : (cell.source_mode === "override" ? cell.override_source_snapshot : wall.source_snapshot);
    return findDeviceWithSnapshot(geo.sourceId, state.encoders, snapshot);
  }

  function renderGrid() {
    const grid = document.getElementById("wallGrid");
    const summary = document.getElementById("gridSummary");
    if (!currentWall) {
      grid.innerHTML = `<div class="empty">Create or select a video wall.</div>`;
      summary.textContent = "";
      stopWallPreviewRefresh();
      return;
    }
    const compact = currentWall.rows * currentWall.columns > 9;
    grid.style.gridTemplateColumns = `repeat(${currentWall.columns}, ${WALL_CELL_WIDTH}px)`;
    summary.textContent = `${currentWall.rows} x ${currentWall.columns} (${currentWall.cells.length} displays)`;
    grid.innerHTML = "";
    const draftRect = zoneEditMode ? currentZoneDraftRect() : null;
    for (const cell of currentWall.cells) {
      const key = `${cell.row}:${cell.column}`;
      const report = reports[cell.decoder_id] || {};
      const hasReport = cell.decoder_id && Object.prototype.hasOwnProperty.call(reports, cell.decoder_id);
      const diffs = hasReport ? cellDiffs(currentWall, cell, report) : [];
      const sourceBusy = cell.live_source_pending;
      const decoder = findDeviceWithSnapshot(cell.decoder_id, state.decoders, cell.decoder_snapshot);
      const previewBase = wallPreviewEnabled ? decoderPreviewBaseUrl(decoder) : "";
      const previewUrl = previewUrlWithCacheBuster(previewBase);
      const rotation = rotationClassValue(cell.rotation || 0);
      const previewScale = previewFitScale(rotation);
      const previewOpacity = Math.max(0.2, Math.min(1, wallPreviewOpacity / 100));
      const previewVeil = previewOpacity >= 1 ? 0 : Math.max(0.12, 0.55 - previewOpacity * 0.42);
      const region = cellRegion(currentWall, cell);
      const sourceDevice = effectiveSourceDevice(currentWall, cell);
      const cellEl = document.createElement("div");
      const inDraft = draftRect && cell.row >= draftRect.row1 && cell.row <= draftRect.row2 && cell.column >= draftRect.column1 && cell.column <= draftRect.column2;
      const zoneCount = zoneEditMode ? (zoneDraft || []).filter(z => cell.row >= z.row1 && cell.row <= z.row2 && cell.column >= z.column1 && cell.column <= z.column2).length : 0;
      cellEl.className = `wall-cell ${compact ? "compact" : ""} ${key === selectedPos ? "selected" : ""} ${previewBase ? "preview-enabled" : ""} ${region ? "regioned" : ""} ${zoneEditMode ? "zone-edit" : ""} ${inDraft ? "zone-draft" : ""} ${zoneCount ? "in-saved-zone" : ""} ${zoneDraftAnchor && zoneDraftAnchor.row === cell.row && zoneDraftAnchor.column === cell.column ? "zone-armed" : ""}`;
      cellEl.dataset.pos = key;
      if (previewBase) {
        cellEl.style.setProperty("--preview-opacity", previewOpacity.toFixed(2));
        cellEl.style.setProperty("--preview-veil", previewVeil.toFixed(2));
      }
      // Task 3/4: the old large centered source-name block is gone -- the
      // live preview (when enabled) now dominates the monitor face. What's
      // left is a compact bottom chip (truncated, full text in its title
      // tooltip) plus a glanceable decoder/rotation line. Both the decoder
      // and rotation spans are click targets (data-action) that select this
      // cell and jump straight to that field in the contextual inspector --
      // task 9's "click decoder identity to change decoder" -- without any
      // new editing logic: the inspector's existing <select>s remain
      // authoritative.
      const modeLabel = region ? "Region" : (cell.source_mode === "override" ? "Override" : "Wall Video");
      const chipText = sourceDevice ? `${modeLabel} · ${sourceLabel(sourceDevice)}` : "Drop a source here";
      const decoderText = decoder ? deviceLabel(decoder) : "No decoder assigned";
      const zoneBadge = zoneCount ? `<span class="zone-count-badge" title="Member of ${zoneCount} saved zone${zoneCount === 1 ? "" : "s"}">${zoneCount}</span>` : "";
      cellEl.innerHTML = `
        ${previewBase ? `<div class="wall-cell-preview" data-preview-base="${escapeHtml(previewBase)}" style="background-image:url('${escapeHtml(previewUrl)}');transform:rotate(${rotation}deg) scale(${previewScale});"></div>` : ""}
        <div class="head">
          <div class="title">R${cell.row} C${cell.column}${zoneBadge}</div>
          <span class="status-chip ${sourceBusy ? "modified" : (diffs.length ? "modified" : (report.online ? "healthy" : ""))}">${sourceBusy ? "Routing" : (cell.decoder_id ? (diffs.length ? "Diff" : (hasReport ? (report.online ? "OK" : "Offline") : "Saved")) : "Empty")}</span>
        </div>
        <div class="wall-cell-spacer"></div>
        <div class="wall-cell-foot">
          <div class="wall-video-chip" title="${escapeHtml(chipText)}">${escapeHtml(chipText)}</div>
          <div class="wall-cell-meta">
            <span class="muted chip-decoder" data-action="open-decoder" title="Click to change decoder">${escapeHtml(decoderText)}</span>
            <span class="muted chip-rotation" data-action="open-rotation" title="Click to change rotation">${cell.rotation ? `${cell.rotation}&deg;` : "0&deg;"}</span>
          </div>
        </div>
      `;
      grid.appendChild(cellEl);
    }
    renderDetails();
    repositionInspectorIfOpen();
    syncWallPreviewRefresh();
    renderZoneRail();
    renderZoneEditorPanel();
  }

  function selectedCell() {
    if (!currentWall) return null;
    return currentWall.cells.find(cell => `${cell.row}:${cell.column}` === selectedPos) || currentWall.cells[0] || null;
  }

  // ---- Source palette (drag source) -----------------------------------

  function renderSourcePalette() {
    const host = document.getElementById("sourcePalette");
    if (!host) return;
    // The palette is fully rebuilt below (host.innerHTML = ""), which would
    // otherwise leave a preview card pointing at a tile that no longer
    // exists in the document.
    closeSourcePreview();
    host.innerHTML = "";
    if (!state.encoders.length) {
      host.innerHTML = `<div class="empty">No eligible encoders discovered yet.</div>`;
      return;
    }
    for (const encoder of state.encoders) {
      const id = encoder.id || wallDeviceId(encoder);
      const tile = document.createElement("div");
      tile.className = `source-tile ${id === armedSourceId ? "armed" : ""}`;
      tile.draggable = true;
      tile.tabIndex = 0;
      tile.dataset.sourceId = id;
      tile.setAttribute("role", "button");
      tile.setAttribute("aria-label", `Source ${deviceLabel(encoder)}. Drag onto the wall, or click to select it and then click a display or zone to route at once.`);
      tile.innerHTML = `
        <div class="source-tile-name">${escapeHtml(encoder.hostname || encoder.label || "Encoder")}</div>
        <div class="source-tile-meta">${escapeHtml(encoder.model || "Encoder")} &middot; ${escapeHtml(encoder.ip || "")}</div>
        ${encoder.streamname ? `<div class="source-tile-meta">${escapeHtml(encoder.streamname)}</div>` : ""}
      `;
      // Non-drag routing path (click source, click target: routes at once) --
      // see armSourceForNonDragRouting()/routeArmedSourceTo(), which end at
      // the SAME assignSpatialTarget() the drag/drop path calls, not a
      // second routing engine.
      tile.addEventListener("click", () => {
        if (zoneEditMode) return;
        armSourceForNonDragRouting(id, tile);
      });
      // Hover preview (task 1): debounced, one fetch per hover, closed the
      // instant the pointer leaves or a drag starts -- see
      // schedulePreview/closePreview commentary above. Never routes
      // anything and never writes device state; it only ever reads the
      // same thumbnail URL the decoder-preview toggle already reads.
      tile.addEventListener("pointerenter", event => {
        if (event.pointerType === "touch") return;
        scheduleSourcePreview(tile, encoder);
      });
      tile.addEventListener("pointerleave", closeSourcePreview);
      tile.addEventListener("focus", () => scheduleSourcePreview(tile, encoder));
      tile.addEventListener("blur", closeSourcePreview);
      host.appendChild(tile);
    }
  }

  function wireSourcePalette() {
    const host = document.getElementById("sourcePalette");
    if (!host) return;
    host.addEventListener("dragstart", event => {
      const tile = event.target.closest(".source-tile");
      if (!tile) return;
      // A drag must never be blocked or delayed by a preview card sitting
      // on top of the tile being dragged (task 1's "dragging must still
      // work naturally"); close it immediately, before anything else.
      closeSourcePreview();
      dragSourceId = tile.dataset.sourceId;
      tile.classList.add("dragging");
      const stage = document.getElementById("wallStage");
      if (stage) stage.classList.add("routing-active");
      document.getElementById("zoneRail")?.classList.add("dragging-active");
      event.dataTransfer.setData("text/plain", dragSourceId);
      event.dataTransfer.effectAllowed = "copy";
    });
    host.addEventListener("dragend", event => {
      const tile = event.target.closest(".source-tile");
      if (tile) tile.classList.remove("dragging");
      dragSourceId = null;
      document.getElementById("zoneRail")?.classList.remove("dragging-active");
      clearDropHighlight();
    });
  }

  // ---- Spatial drop targets ---------------------------------------------
  //
  // Every assigned display is always reachable as its own 1x1 target
  // (plain per-cell drop, hit-tested with elementFromPoint -- the CURRENT
  // pointer position, no hysteresis/stickiness: moving off one cell and
  // onto another updates the highlight on the very next dragover). Multi-
  // cell targets (a saved zone, an on-the-fly 2x2 convenience pair/column,
  // or the entire wall) are NEVER resolved by an invisible/oversized
  // pointer hit-area -- they are always separate, clearly labeled chips in
  // the zone rail (renderZoneRail()) rendered alongside the grid, each its
  // own real drop target. This replaces the old 2x2-only crosshair
  // (PROJECT.md, "Video Wall: spatial routing, 2x2 first"): a wall of any
  // size gets exactly the same interaction model, with zero geometry
  // invented per wall size.

  function hitTestTarget(x, y) {
    if (!currentWall) return null;
    const el = document.elementFromPoint(x, y);
    const cellEl = el ? el.closest(".wall-cell") : null;
    if (!cellEl || !cellEl.dataset.pos) return null;
    const [row, column] = cellEl.dataset.pos.split(":").map(Number);
    return [{ row, column }];
  }

  // Finds a zone (saved or auto) whose member cells exactly match `cells`,
  // for presentation text only ("Route to Top Row" vs "Route to 2
  // Displays") -- never used to decide what a drop actually does.
  function zoneForCells(wall, cells) {
    if (!cells) return null;
    const keys = new Set(cells.map(c => `${c.row}:${c.column}`));
    return zonesForRouting(wall).find(z => {
      const zKeys = z.cells.map(c => `${c.row}:${c.column}`);
      return zKeys.length === keys.size && zKeys.every(k => keys.has(k));
    }) || null;
  }

  // Single place that clears drop-target highlighting from BOTH kinds of
  // drop target (grid cells and zone rail chips) -- used by every caller
  // that needs to reset highlighting before applying a new one, so the
  // "which classes get cleared" list can never drift out of sync between them.
  function clearAllDropTargetHighlights() {
    document.querySelectorAll(".wall-cell.drop-target").forEach(el => el.classList.remove("drop-target"));
    document.querySelectorAll(".zone-chip.drop-target").forEach(el => el.classList.remove("drop-target"));
  }

  // The single choke point for "what is currently drop-highlighted" --
  // both the grid-cell drag path (chipEl omitted) and the zone-rail-chip
  // drag path (wireRailTarget(), chipEl given) call this rather than each
  // hand-rolling their own clear-then-highlight logic, so there is exactly
  // one place that decides what counts as "no change, skip the DOM churn".
  function setDropHighlight(cells, chipEl) {
    const nextChip = chipEl || null;
    const same = JSON.stringify(cells) === JSON.stringify(dragTargetCells) && dragHighlightedChip === nextChip;
    if (same) return;
    dragTargetCells = cells;
    dragHighlightedChip = nextChip;
    clearAllDropTargetHighlights();
    if (nextChip) nextChip.classList.add("drop-target");
    if (!cells || !cells.length) return;
    const keys = new Set(cells.map(c => `${c.row}:${c.column}`));
    document.querySelectorAll(".wall-cell").forEach(el => {
      if (keys.has(el.dataset.pos)) el.classList.add("drop-target");
    });
  }

  // Concise contextual text for the pointer-following hint and the zone
  // rail chips. Pure presentation over a `cells` value some caller already
  // produced -- it does not decide anything about routing itself.
  function dropHintText(cells, label) {
    if (!cells || !cells.length) return "";
    if (label) return `Route to ${label}`;
    if (cells.length === 1) return `Route to R${cells[0].row} C${cells[0].column}`;
    if (currentWall && cells.length === currentWall.cells.length) return "Route to Entire Wall";
    const zone = zoneForCells(currentWall, cells);
    return zone ? `Route to ${zoneLabel(zone)}` : `Route to ${cells.length} Displays`;
  }

  function showDropHint(x, y, cells, label) {
    const hint = document.getElementById("dropHint");
    if (!hint) return;
    const text = dropHintText(cells, label);
    if (!text) {
      hint.hidden = true;
      return;
    }
    hint.textContent = text;
    hint.hidden = false;
    hint.style.visibility = "hidden";
    hint.style.left = "0px";
    hint.style.top = "0px";
    const size = hint.getBoundingClientRect();
    const left = Math.min(Math.max(8, x - size.width / 2), window.innerWidth - size.width - 8);
    const top = Math.max(8, y - size.height - 14);
    hint.style.left = `${Math.round(left)}px`;
    hint.style.top = `${Math.round(top)}px`;
    hint.style.visibility = "";
  }

  function hideDropHint() {
    const hint = document.getElementById("dropHint");
    if (hint) hint.hidden = true;
  }

  // Single choke point for ending "routing-target mode" -- called on drop,
  // dragend, and leaving the stage mid-drag, so no caller can clean up only
  // part of it and leave a stale highlight or hint behind.
  function clearDropHighlight() {
    dragTargetCells = null;
    dragHighlightedChip = null;
    clearAllDropTargetHighlights();
    const stage = document.getElementById("wallStage");
    if (stage) stage.classList.remove("routing-active");
    hideDropHint();
  }

  function wireWallDragAndDrop() {
    const stage = document.getElementById("wallStage");
    if (!stage) return;
    stage.addEventListener("dragover", event => {
      if (!dragSourceId || zoneEditMode) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = "copy";
      if (stage && !stage.classList.contains("routing-active")) stage.classList.add("routing-active");
      const cells = hitTestTarget(event.clientX, event.clientY);
      setDropHighlight(cells);
      showDropHint(event.clientX, event.clientY, cells);
    });
    stage.addEventListener("dragleave", event => {
      if (!stage.contains(event.relatedTarget)) clearDropHighlight();
    });
    stage.addEventListener("drop", event => {
      event.preventDefault();
      const sourceId = event.dataTransfer.getData("text/plain") || dragSourceId;
      const targetCells = dragTargetCells;
      clearDropHighlight();
      dragSourceId = null;
      if (!sourceId || !targetCells || !targetCells.length || zoneEditMode) return;
      assignSpatialTarget(targetCells, sourceId);
    });
  }

  // ---- Zone rail: every multi-cell routing target as an explicit,
  // always-visible, individually-labeled chip -- saved zones, the on-the-
  // fly 2x2 convenience zones (autoZonesForWall()), and "Entire Wall"
  // (which is deliberately NOT a zone -- see PROJECT.md, "the four-display
  // center target is deliberately not modeled as a region" -- routing to
  // it reuses the existing wall.source_id/"Apply to Entire Wall" mechanism
  // unchanged). No two chips ever occupy the same screen space, so there is
  // no overlapping-hit-area ambiguity to resolve: whichever chip the
  // pointer is over IS the target, full stop.
  // Ordered rail target list -- "Entire Wall" first (the most common/
  // important target and the explicit whole-wall routing target), then the
  // 2x2 auto convenience zones (Top/Bottom/Left/Right, in their existing
  // relative order), then any operator-saved custom zones. This is
  // presentation order ONLY: target ids, routing semantics (wireRailTarget
  // below still calls the exact same assignSpatialTarget()/routeArmedSourceTo()
  // path regardless of position), and zonesForRouting()'s own hit-testing/
  // label-lookup order (zoneForCells(), unaffected by this) are unchanged.
  // Pulled out of renderZoneRail() as a pure function so the order itself is
  // covered by a plain unit test without needing a full DOM.
  function zoneRailTargets(wall) {
    if (!wall) return [];
    const hasAnyAssignment = (wall.cells || []).some(c => c.decoder_id);
    const auto = autoZonesForWall(wall).map(z => ({ ...z, cells: zoneCellsOf(z) }));
    const saved = (wall.zones || []).map(z => ({ ...z, cells: zoneCellsOf(z) }));
    const targets = [];
    if (hasAnyAssignment) {
      targets.push({
        id: "__entire_wall__",
        kind: "entire-wall",
        label: "Entire Wall",
        cells: wall.cells.map(c => ({ row: c.row, column: c.column }))
      });
    }
    for (const zone of auto) targets.push({ id: zone.id, kind: "auto", label: zoneLabel(zone), cells: zone.cells });
    for (const zone of saved) targets.push({ id: zone.id, kind: "saved", label: zoneLabel(zone), cells: zone.cells });
    return targets;
  }

  function renderZoneRail() {
    const rail = document.getElementById("zoneRail");
    if (!rail) return;
    if (!currentWall || zoneEditMode) {
      rail.hidden = true;
      rail.innerHTML = "";
      return;
    }
    const targets = zoneRailTargets(currentWall);
    if (!targets.length) {
      rail.hidden = true;
      rail.innerHTML = "";
      return;
    }
    rail.hidden = false;
    rail.innerHTML = "";
    const label = document.createElement("span");
    label.className = "zone-rail-label muted";
    label.textContent = "Routing targets:";
    rail.appendChild(label);
    for (const target of targets) {
      const chip = document.createElement("div");
      chip.className = target.kind === "entire-wall" ? "zone-chip entire-wall" : `zone-chip ${target.kind}`;
      chip.dataset.zoneId = target.id;
      chip.tabIndex = 0;
      chip.setAttribute("role", "button");
      chip.textContent = target.label;
      chip.title = target.kind === "entire-wall"
        ? "Set the whole wall to one source -- breaks out of any zone/override, same as Apply to Entire Wall"
        : `Route a source here to affect: ${target.cells.map(c => `R${c.row}C${c.column}`).join(", ")}`;
      wireRailTarget(chip, target.cells, target.label);
      rail.appendChild(chip);
    }
  }

  // One chip, wired for BOTH the drag/drop path (dragover/drop, exactly the
  // same events the grid cells use) and the non-drag click path (click
  // while a source is armed -- see armSourceForNonDragRouting()). Both
  // paths end at the SAME assignSpatialTarget() call the grid drop uses.
  function wireRailTarget(chip, cells, label) {
    chip.addEventListener("dragover", event => {
      if (!dragSourceId) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = "copy";
      setDropHighlight(cells, chip);
      showDropHint(event.clientX, event.clientY, cells, label);
    });
    chip.addEventListener("dragleave", () => {
      if (dragHighlightedChip !== chip) return;
      chip.classList.remove("drop-target");
      dragHighlightedChip = null;
      dragTargetCells = null;
    });
    chip.addEventListener("drop", event => {
      event.preventDefault();
      const sourceId = event.dataTransfer.getData("text/plain") || dragSourceId;
      clearDropHighlight();
      dragSourceId = null;
      if (!sourceId) return;
      assignSpatialTarget(cells, sourceId, label);
    });
    chip.addEventListener("click", () => {
      if (!armedSourceId) return;
      routeArmedSourceTo(cells, label);
    });
  }

  // ---- Non-drag routing path: click a source, then click a target. The route
  // starts at once -- there is no pending card and no second confirmation. It
  // calls the exact same assignSpatialTarget() the drag/drop path calls, never a
  // second routing engine; authorization, online checks, member verification
  // and read-back all happen on the server as before.

  function armSourceForNonDragRouting(sourceId, tile) {
    const alreadyArmed = armedSourceId === sourceId;
    armedSourceId = alreadyArmed ? null : sourceId;
    document.querySelectorAll(".source-tile.armed").forEach(el => el.classList.remove("armed"));
    if (!alreadyArmed && tile) tile.classList.add("armed");
    updateRouteBar();
  }

  function routeArmedSourceTo(cells, label) {
    if (!armedSourceId || zoneEditMode) return;
    const sourceId = armedSourceId;
    armedSourceId = null;
    document.querySelectorAll(".source-tile.armed").forEach(el => el.classList.remove("armed"));
    updateRouteBar();
    setStatus(`Routing to ${label}...`, "modified");
    assignSpatialTarget(cells, sourceId, label);
  }

  // A one-line hint only: it names the selected source and says what the next
  // click does. It carries no button and holds no pending route.
  function updateRouteBar() {
    const bar = document.getElementById("nonDragRouteBar");
    if (!bar) return;
    if (!armedSourceId || zoneEditMode) {
      bar.hidden = true;
      return;
    }
    const source = idMap(state.encoders).get(armedSourceId);
    const sourceName = source ? deviceLabel(source) : armedSourceId;
    bar.hidden = false;
    bar.querySelector(".route-bar-text").textContent = `${sourceName} selected -- click a display or routing target to route now`;
  }

  // Dropping on one monitor: the existing per-cell override mechanism.
  // Dropping on the "Entire Wall" chip: equivalent to setting the toolbar's
  // "Wall Video Source", reusing that mechanism exactly rather than
  // modeling the whole wall as a region. Dropping on any other multi-cell
  // target (a saved zone or an on-the-fly 2x2 convenience zone): a genuine
  // region, the one case the existing per-cell model could not already
  // express. This is the SAME function the non-drag Route path calls.
  function assignSpatialTarget(cells, sourceId, label) {
    if (!currentWall || !cells || !cells.length) return;
    syncWallFromForm();

    if (cells.length === 1) {
      const cell = currentWall.cells.find(c => c.row === cells[0].row && c.column === cells[0].column);
      if (!cell) return;
      currentWall.regions = (currentWall.regions || []).filter(r =>
        !r.cells.some(c => c.row === cell.row && c.column === cell.column));
      cell.source_mode = "override";
      cell.override_source_id = sourceId;
      cell.override_source_snapshot = compactDeviceSnapshot(idMap(state.encoders).get(sourceId)) || null;
      replaceCurrentInState();
      liveRouteSource("cell", cell);
      return;
    }

    if (cells.length === currentWall.cells.length) {
      currentWall.regions = [];
      const wallSourceSelect = document.getElementById("wallSource");
      const hasOption = wallSourceSelect && Array.from(wallSourceSelect.options).some(o => o.value === sourceId);
      if (!hasOption) {
        currentWall.source_id = sourceId;
        currentWall.source_snapshot = compactDeviceSnapshot(idMap(state.encoders).get(sourceId)) || currentWall.source_snapshot || null;
        replaceCurrentInState();
        renderSelectors();
      }
      if (wallSourceSelect) wallSourceSelect.value = sourceId;
      routeSelectedWallSource();
      return;
    }

    const claimed = new Set(cells.map(c => `${c.row}:${c.column}`));
    const regions = (currentWall.regions || []).filter(r => !r.cells.some(c => claimed.has(`${c.row}:${c.column}`)));
    const region = {
      id: newId().replace("wall-", "region-"),
      cells: cells.map(c => ({ row: c.row, column: c.column })).sort((a, b) => a.row - b.row || a.column - b.column),
      source_id: sourceId,
      source_snapshot: compactDeviceSnapshot(idMap(state.encoders).get(sourceId)) || null
    };
    regions.push(region);
    currentWall.regions = regions;
    // A cell that was independently overridden and is now claimed by a
    // region no longer needs that override -- the region takes precedence
    // either way, but leaving stale override data behind would resurface
    // it if the region were later removed.
    for (const cell of currentWall.cells) {
      if (claimed.has(`${cell.row}:${cell.column}`) && cell.source_mode === "override") {
        cell.source_mode = "wall";
        cell.override_source_id = "";
        cell.override_source_snapshot = null;
      }
    }
    replaceCurrentInState();
    liveRouteRegion(region);
  }

  async function liveRouteRegion(region) {
    replaceCurrentInState();
    const seq = ++liveRouteSeq;
    const targets = (currentWall.cells || []).filter(c =>
      c.decoder_id && region.cells.some(rc => rc.row === c.row && rc.column === c.column));
    if (!targets.length) {
      toast("Assign decoders to this region before routing source", false);
      requestRender();
      return;
    }
    for (const target of targets) target.live_source_pending = true;
    requestRender();
    setStatus("Syncing region source...", "modified");
    try {
      await api("/api/video_wall/source", { wall: currentWall, scope: "region", region_id: region.id });
      await saveWallQuiet();
      if (seq === liveRouteSeq) {
        reports = {};
        setStatus("Source synced", "healthy");
        // Honest partial-applicability reporting: a zone/region may include
        // physical coordinates with no decoder assigned (see PROJECT.md,
        // "UNASSIGNED CELLS") -- targets already excludes those (filtered
        // above), so if region.cells is bigger than targets, say so rather
        // than claiming every member cell was routed.
        const unassigned = region.cells.length - targets.length;
        toast(unassigned > 0
          ? `Routed ${targets.length} of ${region.cells.length} display(s) in this zone -- ${unassigned} unassigned`
          : "Region source synced", true);
        refreshWall({ silent: true }).catch(() => {});
        // Post-route preview burst (only the cells this route actually
        // touched -- see burstRefreshPreviewsForCells() above): the route
        // itself already completed and is not delayed by this in any way.
        burstRefreshPreviewsForCells(targets);
      }
    } catch (e) {
      if (seq === liveRouteSeq) {
        setStatus("Route failed", "error");
        toast(e.message, false);
      }
    } finally {
      // Only clear the flag on the cells THIS call marked pending -- not
      // every cell in the wall. Two live-route calls can be in flight at
      // once (e.g. a region route started while a prior single-cell route
      // is still pending); clearing indiscriminately would erase the other
      // call's still-in-progress busy indicator early, even though the seq
      // guard above already keeps their actual status/toast updates separate.
      for (const target of targets) delete target.live_source_pending;
      requestRender();
    }
  }

  // Pure: every one of `wall`'s cells that belongs to `region` -- the exact
  // set that must ALL become an explicit override pinned to the region's
  // former source when it's dissolved, regardless of which single cell the
  // operator clicked to detach. Extracted so this correctness property
  // (every member cell, not just the clicked one) is directly unit-testable
  // without a live DOM/network -- see tests/video_wall_model.test.js.
  function cellsInRegion(wall, region) {
    return (wall.cells || []).filter(c =>
      (region.cells || []).some(rc => rc.row === c.row && rc.column === c.column));
  }

  // "Detach from region" dissolves the whole region (it needs >=2 cells by
  // construction, so detaching one cell always means "end the region" --
  // see renderDetails()). EVERY cell that was in the region must end up a
  // genuine, device-correct override, not just the one the operator
  // clicked -- a sibling cell left on its old source_mode ("wall",
  // typically, since a region's source is separate from a cell's own mode)
  // would make the software believe it shows the wall source while its
  // physical decoder is still actually showing whatever the region was
  // routing. The region and every affected cell's source_mode/override_*
  // fields are updated FIRST, then a real scope="cell" call is issued for
  // EACH affected cell -- the exact same call+scope a single already-
  // detached cell has always used (_vw_apply_source_cell's override branch:
  // vmenable:false, direct SelectVideoStream route) -- rather than re-
  // affirming the region's own enable:true crop-matrix state, which would
  // leave the device looking like it's still in a region long after the
  // software stopped modeling it that way.
  async function detachRegionAndRoute(region) {
    replaceCurrentInState();
    const seq = ++liveRouteSeq;
    const affectedCells = cellsInRegion(currentWall, region);
    currentWall.regions = (currentWall.regions || []).filter(r => r.id !== region.id);
    for (const cell of affectedCells) {
      cell.source_mode = "override";
      cell.override_source_id = region.source_id || "";
      cell.override_source_snapshot = region.source_snapshot || null;
    }
    const targets = affectedCells.filter(c => c.decoder_id);
    if (!targets.length) {
      // No decoders assigned in this region at all -- nothing on a device
      // to keep in sync; just update the model.
      replaceCurrentInState();
      requestRender();
      return;
    }
    for (const target of targets) target.live_source_pending = true;
    requestRender();
    setStatus("Detaching region...", "modified");
    try {
      await Promise.all(targets.map(cell =>
        api("/api/video_wall/source", { wall: currentWall, scope: "cell", row: cell.row, column: cell.column })));
      await saveWallQuiet();
      if (seq === liveRouteSeq) {
        reports = {};
        setStatus("Source synced", "healthy");
        toast(targets.length > 1
          ? `Detached region -- ${targets.length} displays re-pinned to their previous source`
          : "Detached from region", true);
        refreshWall({ silent: true }).catch(() => {});
      }
    } catch (e) {
      if (seq === liveRouteSeq) {
        setStatus("Route failed", "error");
        toast(e.message, false);
      }
    } finally {
      for (const target of targets) delete target.live_source_pending;
      requestRender();
    }
  }

  // ---- Edit Zones mode ---------------------------------------------------
  //
  // An explicit, separate mode for the selected wall -- not a permanent
  // editing chrome. While active: source routing (drag palette, rail
  // clicks) is suspended, existing zones are clearly outlined on the grid,
  // and the operator can create/rename/redefine/delete zones. EVERYTHING
  // here operates on `zoneDraft`, a working COPY of currentWall.zones taken
  // when the mode is entered -- zero device writes and zero persistence
  // happen until Save explicitly calls saveWallQuiet() (itself a metadata-
  // only call: see /api/video_wall/save, which never touches a device).
  // Cancel discards zoneDraft outright.

  function currentZoneDraftRect() {
    if (!zoneDraftAnchor || !zoneDraftHover) return null;
    return {
      row1: Math.min(zoneDraftAnchor.row, zoneDraftHover.row),
      row2: Math.max(zoneDraftAnchor.row, zoneDraftHover.row),
      column1: Math.min(zoneDraftAnchor.column, zoneDraftHover.column),
      column2: Math.max(zoneDraftAnchor.column, zoneDraftHover.column)
    };
  }

  function enterZoneEditMode() {
    if (!currentWall) return;
    zoneEditMode = true;
    zoneDraft = (currentWall.zones || []).map(z => ({ ...z }));
    zoneDraftAnchor = null;
    zoneDraftHover = null;
    zoneDraftRedefineId = null;
    // Source routing is fully suspended in this mode -- including a source
    // already armed via the non-drag path before "Edit Zones" was clicked.
    // Without this, the Route bar stayed visible (and its button enabled)
    // throughout zone editing, and clicking it would fire a real device
    // write, directly contradicting this mode's zero-device-writes guarantee.
    armSourceForNonDragRouting(null, null);
    closeInspector();
    document.getElementById("wall-page")?.classList.add("zone-edit-active");
    requestRender(true);
  }

  function exitZoneEditMode() {
    zoneEditMode = false;
    zoneDraft = null;
    zoneDraftAnchor = null;
    zoneDraftHover = null;
    zoneDraftRedefineId = null;
    document.getElementById("wall-page")?.classList.remove("zone-edit-active");
    requestRender(true);
  }

  async function saveZoneEdits() {
    if (!currentWall || !zoneDraft) return;
    currentWall.zones = cleanZones(zoneDraft, currentWall.rows, currentWall.columns);
    replaceCurrentInState();
    exitZoneEditMode();
    try {
      await saveWallQuiet(); // metadata-only write -- see /api/video_wall/save; no device call
      toast("Zones saved", true);
    } catch (e) {
      toast(e.message, false);
    }
    requestRender(true);
  }

  function cancelZoneEdits() {
    exitZoneEditMode();
  }

  function commitZoneDraw(row, column) {
    if (!zoneDraftAnchor || !zoneDraft) return;
    const rect = {
      row1: Math.min(zoneDraftAnchor.row, row), row2: Math.max(zoneDraftAnchor.row, row),
      column1: Math.min(zoneDraftAnchor.column, column), column2: Math.max(zoneDraftAnchor.column, column)
    };
    if (zoneDraftRedefineId) {
      const idx = zoneDraft.findIndex(z => z.id === zoneDraftRedefineId);
      if (idx >= 0) zoneDraft[idx] = { ...zoneDraft[idx], ...rect };
    } else {
      zoneDraft.push({ id: newId().replace("wall-", "zone-"), name: "", ...rect });
    }
    zoneDraftAnchor = null;
    zoneDraftHover = null;
    zoneDraftRedefineId = null;
    requestRender(true);
  }

  function beginRedefineZone(zoneId) {
    const zone = (zoneDraft || []).find(z => z.id === zoneId);
    if (!zone) return;
    zoneDraftRedefineId = zoneId;
    zoneDraftAnchor = { row: zone.row1, column: zone.column1 };
    zoneDraftHover = { row: zone.row2, column: zone.column2 };
    requestRender(true);
  }

  function deleteZoneDraft(zoneId) {
    if (!zoneDraft) return;
    zoneDraft = zoneDraft.filter(z => z.id !== zoneId);
    if (zoneDraftRedefineId === zoneId) { zoneDraftAnchor = null; zoneDraftHover = null; zoneDraftRedefineId = null; }
    requestRender(true);
  }

  function renameZoneDraft(zoneId, name) {
    if (!zoneDraft) return;
    const zone = zoneDraft.find(z => z.id === zoneId);
    if (zone) zone.name = name;
  }

  function renderZoneEditorPanel() {
    const panel = document.getElementById("zoneEditorPanel");
    const sourcesSection = document.getElementById("sourcesSection");
    if (!panel) return;
    if (!zoneEditMode || !currentWall) {
      panel.hidden = true;
      if (sourcesSection) sourcesSection.hidden = false;
      return;
    }
    if (sourcesSection) sourcesSection.hidden = true;
    panel.hidden = false;
    const list = (zoneDraft || []).map(zone => {
      const cells = zoneCellsOf(zone);
      const unassignedCount = cells.filter(c => {
        const cell = currentWall.cells.find(wc => wc.row === c.row && wc.column === c.column);
        return !cell || !cell.decoder_id;
      }).length;
      return `
        <div class="zone-editor-row ${zoneDraftRedefineId === zone.id ? "redefining" : ""}" data-zone-id="${escapeHtml(zone.id)}">
          <input type="text" class="zone-name-input" placeholder="${escapeHtml(zoneLabel({ ...zone, name: "" }))}" value="${escapeHtml(zone.name || "")}" data-action="rename-zone">
          <span class="muted zone-editor-range">R${zone.row1}C${zone.column1}–R${zone.row2}C${zone.column2}${unassignedCount ? ` · ${unassignedCount} unassigned` : ""}</span>
          <button type="button" class="btn-secondary" data-action="redefine-zone">Redefine</button>
          <button type="button" class="btn-secondary danger" data-action="delete-zone">Delete</button>
        </div>`;
    }).join("");
    panel.innerHTML = `
      <div class="panel-title"><h3>Edit Zones</h3></div>
      <p class="muted" style="margin:0 0 8px;font-size:12px;">Drag across cells (or click one corner, then the other) to draw a rectangular zone. No device changes happen until you Save.</p>
      <div id="zoneEditorList">${list || `<div class="empty">No zones yet -- draw one on the grid.</div>`}</div>
      <div class="modal-buttons" style="margin-top:10px;">
        <button type="button" class="btn-secondary" id="zoneEditCancelBtn">Cancel</button>
        <button type="button" id="zoneEditSaveBtn">Save Zones</button>
      </div>
    `;
  }

  function wireZoneEditing() {
    const editBtn = document.getElementById("editZonesBtn");
    if (editBtn) editBtn.addEventListener("click", () => {
      if (zoneEditMode) return;
      enterZoneEditMode();
    });

    const grid = document.getElementById("wallGrid");
    if (grid) {
      // Pointer Events + setPointerCapture(), not mousedown/window-level
      // mousemove+mouseup: capture guarantees pointermove/pointerup/
      // pointercancel are delivered to `grid` no matter where the pointer
      // physically ends up (outside the browser window, over another
      // element, a dialog stealing focus, etc). Plain window-level mouse
      // listeners have no such guarantee -- a mouseup that never reaches
      // `window` would leak that pair of listeners, which then stack with
      // the next draw and both keep firing (flicker, stale state).
      grid.addEventListener("pointerdown", event => {
        if (!zoneEditMode) return;
        const cellEl = event.target.closest(".wall-cell");
        if (!cellEl) return;
        event.preventDefault();
        const [row, column] = cellEl.dataset.pos.split(":").map(Number);

        // Re-mousedown on the exact cell that is already armed: cancel the
        // arm rather than starting a new (degenerate, same-cell) rectangle.
        if (zoneDraftAnchor && zoneDraftAnchor.row === row && zoneDraftAnchor.column === column) {
          zoneDraftAnchor = null;
          zoneDraftHover = null;
          requestRender(true);
          return;
        }

        // If a corner is already armed from an earlier plain click
        // (click-first-corner-then-second-corner), THAT corner remains the
        // rectangle's first corner -- this pointerdown (and wherever the
        // pointer is released) resolves it, rather than starting a fresh
        // rectangle from here.
        const resolvingArmedAnchor = !!zoneDraftAnchor;
        const anchor = zoneDraftAnchor || { row, column };
        zoneDraftAnchor = anchor;
        zoneDraftHover = { row, column };
        requestRender(true);

        const pointerId = event.pointerId;
        try { grid.setPointerCapture(pointerId); } catch { /* unsupported pointer type -- fall through, still cleaned up on pointerup */ }

        let moved = false;
        const onMove = moveEvent => {
          if (moveEvent.pointerId !== pointerId) return;
          const el = document.elementFromPoint(moveEvent.clientX, moveEvent.clientY);
          const hoverEl = el ? el.closest(".wall-cell") : null;
          if (!hoverEl) return;
          const [hr, hc] = hoverEl.dataset.pos.split(":").map(Number);
          if (zoneDraftHover && zoneDraftHover.row === hr && zoneDraftHover.column === hc) return;
          moved = true;
          zoneDraftHover = { row: hr, column: hc };
          requestRender(true);
        };
        const onEnd = endEvent => {
          if (endEvent.pointerId !== pointerId) return;
          grid.removeEventListener("pointermove", onMove);
          grid.removeEventListener("pointerup", onEnd);
          grid.removeEventListener("pointercancel", onEnd);
          try { grid.releasePointerCapture(pointerId); } catch { /* already released/never captured */ }
          if (endEvent.type === "pointercancel") {
            // The gesture was aborted (e.g. focus stolen mid-draw) --
            // discard the in-progress draft rather than guessing a rectangle.
            zoneDraftAnchor = null;
            zoneDraftHover = null;
            requestRender(true);
            return;
          }
          const el = document.elementFromPoint(endEvent.clientX, endEvent.clientY);
          const upEl = el ? el.closest(".wall-cell") : null;
          if (!upEl) { zoneDraftHover = { row, column }; requestRender(true); return; }
          const [ur, uc] = upEl.dataset.pos.split(":").map(Number);
          const releasedWhereItStarted = ur === row && uc === column;
          if (releasedWhereItStarted && !resolvingArmedAnchor && !moved) {
            // A plain click on a fresh cell with no prior armed anchor: arm
            // it and wait for a second click elsewhere.
            zoneDraftHover = { row, column };
            requestRender(true);
            return;
          }
          commitZoneDraw(ur, uc);
        };
        grid.addEventListener("pointermove", onMove);
        grid.addEventListener("pointerup", onEnd);
        grid.addEventListener("pointercancel", onEnd);
      });
    }

    const panel = document.getElementById("zoneEditorPanel");
    if (panel) {
      panel.addEventListener("input", event => {
        const input = event.target.closest("[data-action='rename-zone']");
        const row = event.target.closest(".zone-editor-row");
        if (!input || !row) return;
        renameZoneDraft(row.dataset.zoneId, input.value);
      });
      panel.addEventListener("click", event => {
        const row = event.target.closest(".zone-editor-row");
        if (event.target.closest("#zoneEditCancelBtn")) { cancelZoneEdits(); return; }
        if (event.target.closest("#zoneEditSaveBtn")) { saveZoneEdits().catch(e => toast(e.message, false)); return; }
        if (!row) return;
        if (event.target.closest("[data-action='redefine-zone']")) beginRedefineZone(row.dataset.zoneId);
        if (event.target.closest("[data-action='delete-zone']")) deleteZoneDraft(row.dataset.zoneId);
      });
    }

    document.addEventListener("keydown", event => {
      if (event.key === "Escape" && zoneEditMode) cancelZoneEdits();
    });
  }

  // ---- Selected Display inspector (contextual flyout, tasks 4/5/8) ------
  //
  // Replaces the old permanent third grid column. All of the editing power
  // (Decoder/Source/Rotation selects, bezel override, Identify, diffs) is
  // unchanged -- renderDetails() still builds it -- only the container is
  // now a fixed-position card anchored next to the selected monitor,
  // opened by selecting a cell and dismissed explicitly, so the wall
  // workspace keeps the horizontal space at idle.

  // Docked to the right edge of the viewport, near the top of the wall
  // stage, rather than anchored pixel-for-pixel next to the selected cell.
  // An anchored flyout risks landing on top of a sibling monitor (and, on
  // a drop target, blocking pointer events for a later click) depending on
  // exactly where the selected cell sits in the grid; a fixed dock avoids
  // that whole class of overlap bugs while still being purely contextual
  // (hidden until a cell is selected, dismissed on demand) rather than a
  // permanent reserved grid column. Which cell it describes is unambiguous
  // from the grid's own .wall-cell.selected highlight plus the "Position"
  // row inside the inspector itself.
  // Below INSPECTOR_DOCK_SIDE_MIN_WIDTH the wall workspace can't both
  // reflow for a side-docked panel AND keep a 2x2 wall's own minimum grid
  // width without the grid's own scrollbar kicking in (see the matching
  // @media rule in ui/wall.html) -- dock to the bottom of the viewport
  // there instead, which never competes with the grid for width.
  const INSPECTOR_DOCK_SIDE_MIN_WIDTH = 1360;

  function positionInspector() {
    const inspector = document.getElementById("displayInspector");
    const stage = document.getElementById("wallStage");
    if (!inspector) return;
    const margin = 16;
    if (window.innerWidth < INSPECTOR_DOCK_SIDE_MIN_WIDTH) {
      // Below this width the matching @media rule in ui/wall.html switches
      // #displayInspector to position:static, placing it in normal document
      // flow right after the wall workspace -- no inline positioning needed
      // (or wanted; it would be a no-op for a statically positioned box).
      return;
    }
    const stageTop = stage ? stage.getBoundingClientRect().top : 96;
    const top = Math.max(margin, Math.min(stageTop, 140));
    inspector.style.transform = "none";
    inspector.style.bottom = "auto";
    inspector.style.left = "auto";
    inspector.style.right = `${margin}px`;
    inspector.style.top = `${Math.round(top)}px`;
  }

  // The wall workspace reflows (.wall-page.inspector-open, in ui/wall.html)
  // to make room on the right rather than letting the docked inspector sit
  // on top of -- and block clicks on -- whatever cell happens to render
  // underneath it.
  function reflowForInspector(open) {
    const page = document.querySelector(".wall-page");
    if (page) page.classList.toggle("inspector-open", open);
  }

  function openInspector(cellEl) {
    const inspector = document.getElementById("displayInspector");
    if (!inspector || !cellEl) return;
    inspectorOpen = true;
    inspector.hidden = false;
    reflowForInspector(true);
    positionInspector();
  }

  function closeInspector() {
    inspectorOpen = false;
    const inspector = document.getElementById("displayInspector");
    if (inspector) inspector.hidden = true;
    reflowForInspector(false);
  }

  function repositionInspectorIfOpen() {
    if (!inspectorOpen) return;
    const inspector = document.getElementById("displayInspector");
    const cellEl = document.querySelector(`.wall-cell[data-pos="${selectedPos}"]`);
    if (!inspector || !cellEl) {
      closeInspector();
      return;
    }
    positionInspector();
  }

  // ---- Video Wall Mode (per decoder) ------------------------------------
  // The decoder's own wall-mode flag, as the decoder reports it. Saved wall
  // membership says which decoders SHOULD take part; it never says whether the
  // flag is on, so this is shown as Unknown until the decoder has been read.
  function wallModeFor(cell, report) {
    const did = cell && cell.decoder_id;
    if (!did) return { state: "none" };
    const learned = wallMode[did];
    if (learned && learned.pending) return { state: "pending" };
    if (learned && typeof learned.enabled === "boolean") return { state: learned.enabled ? "enabled" : "disabled", error: learned.error || "" };
    const vw = report && report.video_wall;
    if (vw && typeof vw.vmenable === "boolean") return { state: vw.vmenable ? "enabled" : "disabled", error: (learned && learned.error) || "" };
    return { state: "unknown", error: (learned && learned.error) || "" };
  }

  function wallModeRowHtml(cell, report) {
    const mode = wallModeFor(cell, report);
    if (mode.state === "none") {
      return `<div class="kv"><span>Video Wall Mode</span><span class="muted">Assign a decoder first</span></div>`;
    }
    const busy = mode.state === "pending";
    const opt = (value, text, selected, disabled) => `<option value="${value}"${selected ? " selected" : ""}${disabled ? " disabled" : ""}>${text}</option>`;
    const unknown = mode.state === "unknown" || busy;
    const note = busy ? "Applying and reading back..."
      : mode.state === "unknown" ? (mode.error ? `Unknown: ${mode.error}` : "Unknown: not read from the decoder yet")
      : (mode.error ? mode.error : "As reported by the decoder");
    return `<div class="kv"><span>Video Wall Mode</span><span>
        <select data-action="wall-mode" aria-label="Video Wall Mode"${busy ? " disabled" : ""}>
          ${unknown ? opt("", busy ? "Applying..." : "Unknown", true, true) : ""}
          ${opt("enabled", "Enabled", mode.state === "enabled", false)}
          ${opt("disabled", "Disabled", mode.state === "disabled", false)}
        </select>
        <div class="muted" style="font-size:11px;margin-top:3px;">${escapeHtml(note)}${busy ? "" : ` <button type="button" class="btn-secondary" data-action="wall-mode-read" style="margin-left:6px;padding:1px 8px;font-size:11px;">Read</button>`}</div>
      </span></div>`;
  }

  async function setDecoderWallMode(cell, enabled) {
    const did = cell && cell.decoder_id;
    if (!did) return;
    const label = deviceLabel(findDeviceWithSnapshot(did, state.decoders, cell.decoder_snapshot) || { id: did });
    wallMode[did] = { pending: true };
    renderDetails();
    try {
      const data = await api("/api/video_wall/decoder_mode", { decoder_id: did, enabled, wall_id: currentWall && currentWall.id });
      wallMode[did] = { enabled: !!(data.wall_mode && data.wall_mode.enabled), vwmatrix: data.wall_mode && data.wall_mode.vwmatrix };
      toast(`Video Wall mode ${enabled ? "enabled" : "disabled"} on ${label} (confirmed by the decoder)`, true);
      reports = {};
    } catch (e) {
      // Show what the decoder actually holds, not what was asked for.
      const held = e.data && e.data.wall_mode;
      wallMode[did] = held && typeof held.enabled === "boolean"
        ? { enabled: held.enabled, vwmatrix: held.vwmatrix, error: `Not changed: ${e.message}` }
        : { error: e.message };
      toast(`Video Wall mode not changed on ${label}: ${e.message}`, false);
    }
    requestRender();
    renderDetails();
  }

  async function readDecoderWallMode(cell) {
    const did = cell && cell.decoder_id;
    if (!did) return;
    wallMode[did] = { pending: true };
    renderDetails();
    try {
      const data = await api("/api/video_wall/decoder_mode", { decoder_id: did });
      wallMode[did] = { enabled: !!data.wall_mode.enabled, vwmatrix: data.wall_mode.vwmatrix };
    } catch (e) {
      wallMode[did] = { error: e.message };
    }
    renderDetails();
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
    const region = cellRegion(currentWall, cell);
    const sourceDevice = effectiveSourceDevice(currentWall, cell);
    const source = sourceDevice ? sourceLabel(sourceDevice) : "";
    const perDisplay = !document.getElementById("applyBezelAll").checked;
    const display = { ...currentWall.display, ...(cell.display || {}) };
    details.innerHTML = `
      <div class="kv"><span>Position</span><span>Row ${cell.row}, Column ${cell.column}</span></div>
      <div class="kv"><span>Decoder</span><span><select data-action="decoder">${optionHtml(decoderOptionsForCell(cell), cell.decoder_id, "Unassigned")}</select></span></div>
      <div class="kv"><span>IP</span><span>${escapeHtml(decoder ? decoder.ip : "")}</span></div>
      ${region ? `
      <div class="kv"><span>Source</span><span>${escapeHtml(source || "Not selected")} <button type="button" class="btn-secondary" data-action="clear-region" style="margin-left:8px;">Detach from region</button></span></div>
      <div class="kv"><span>Mode</span><span>Part of a ${region.cells.length}-display region (${region.cells.map(c => `R${c.row}C${c.column}`).join(", ")})</span></div>
      ` : `
      <div class="kv"><span>Source</span><span><select data-action="source" data-last="${escapeHtml(selectedSourceId(cell))}">${sourceOptionsForCell(cell)}</select></span></div>
      <div class="kv"><span>Mode</span><span>${cell.source_mode === "override" ? "Override source" : "Wall video"}</span></div>
      `}
      <div class="kv"><span>Rotation</span><span><select data-action="rotation">${[0, 90, 180, 270].map(v => `<option value="${v}"${Number(cell.rotation || 0) === v ? " selected" : ""}>${v}</option>`).join("")}</select></span></div>
      <div class="kv"><span>Reported</span><span>${escapeHtml(report.online ? "Online" : (cell.decoder_id ? "Not refreshed/offline" : "No decoder"))}</span></div>
      ${wallModeRowHtml(cell, report)}
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

  // Device Reports (task 6): the always-open per-device list is now behind
  // a "Details" disclosure toggle; renderReports() keeps populating that
  // list exactly as before (diagnostic capability is unchanged), it just no
  // longer forces the panel open. A one-line summary ("4/4 decoders
  // online") is always visible so the operator can tell at a glance
  // whether anything needs a look, without expanding it.
  function reportsSummaryText() {
    const entries = Object.entries(reports);
    if (!entries.length) return "Not refreshed";
    const online = entries.filter(([, report]) => report.online).length;
    return `${online}/${entries.length} decoder(s) online`;
  }

  function renderReports() {
    const el = document.getElementById("reports");
    const count = document.getElementById("reportCount");
    const entries = Object.entries(reports);
    if (count) count.textContent = reportsSummaryText();
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

  function snapshotForDirtyCheck(wall) {
    if (!wall) return "";
    const clone = JSON.parse(JSON.stringify(wall));
    for (const cell of clone.cells || []) delete cell.live_source_pending;
    return JSON.stringify(clone);
  }

  function markWallSaved(wall) {
    lastSavedWallSnapshot = snapshotForDirtyCheck(wall);
  }

  function isCurrentWallDirty() {
    if (!currentWall) return false;
    return snapshotForDirtyCheck(currentWall) !== lastSavedWallSnapshot;
  }

  function updateSaveBadge() {
    const badge = document.getElementById("saveWallBadge");
    if (badge) badge.hidden = !isCurrentWallDirty();
  }

  function render() {
    renderSelectors();
    syncFormFromWall();
    renderSourcePalette();
    renderGrid();
    syncDisplayFormFromSelected();
    renderReports();
    setStatus(statusFromReports(currentWall, reports));
    updateSaveBadge();
    updateRouteBar();
  }

  function replaceCurrentInState() {
    if (!currentWall) return;
    const idx = state.walls.findIndex(w => w.id === currentWall.id);
    if (idx >= 0) state.walls[idx] = currentWall;
    else state.walls.push(currentWall);
  }

  async function loadState() {
    const data = await api("/api/video_wall/state");
    lastStateFilePath = data.state_file || "";
    state = {
      walls: (data.walls || []).map(normalizeWall),
      encoders: data.encoders || [],
      decoders: data.decoders || []
    };
    const remembered = rememberedWallId();
    currentWall = state.walls.find(w => w.id === remembered) || state.walls[0] || createWall("Video Wall 1", 2, 2);
    if (!state.walls.length) state.walls.push(currentWall);
    rememberSelectedWall(currentWall.id);
    markWallSaved(currentWall);
    requestRender();
    refreshWall({ silent: true }).catch(e => toast(e.message, false));
    detectExistingWall().catch(() => {});
  }

  // ---- Detecting an existing Video Wall already configured on hardware --
  //
  // GET-only: reconstructs whatever the decoders themselves already report
  // (see PROJECT.md, "Video Wall: detecting an existing configuration").
  // Never writes a device or video_walls.json -- the operator has to act
  // (View Detected Layout, then Save) before anything is persisted.

  function wallFromDetectedCandidate(candidate) {
    const wall = createWall(
      candidate.matched_saved_wall ? candidate.matched_saved_wall.name : `Detected ${candidate.rows}x${candidate.columns}`,
      candidate.rows, candidate.columns
    );
    for (const [pos, member] of Object.entries(candidate.members || {})) {
      const [row, column] = pos.split(":").map(Number);
      const cell = wall.cells.find(c => c.row === row && c.column === column);
      if (!cell) continue;
      cell.decoder_id = member.device_id;
      cell.decoder_snapshot = compactDeviceSnapshot(idMap(state.decoders).get(member.device_id)) ||
        { id: member.device_id, hostname: member.label, ip: member.ip, label: member.label };
      cell.rotation = clampInt(member.rotation, 0, 270, 0);
      // Detected source state is informational only here -- carried onto
      // the cell as an override so it displays correctly on the canvas,
      // but nothing is written to a device by simply viewing this.
      if (member.source) {
        cell.source_mode = "override";
        const matchedEncoder = (state.encoders || []).find(e => e.streamname === member.source);
        if (matchedEncoder) {
          cell.override_source_id = matchedEncoder.id;
          cell.override_source_snapshot = compactDeviceSnapshot(matchedEncoder);
        }
      }
      if (member.bezel) cell.display = { ...member.bezel };
    }
    return wall;
  }

  // Qualitative statuses only -- DETECTED/POSSIBLE/PARTIAL/AMBIGUOUS/
  // CONFLICT, plus the "known wall changed" diff case -- never a numeric
  // confidence score (see PROJECT.md, "Video Wall: site discovery").
  function candidateStatusLabel(candidate) {
    if (candidate.known_wall_changed) return "Changed";
    switch (candidate.candidate_status) {
      case "POSSIBLE": return "Possible";
      case "PARTIAL": return "Partial";
      case "AMBIGUOUS": return "Ambiguous";
      case "CONFLICT": return "Conflict";
      case "DETECTED": return "Detected";
      default: return candidate.candidate_status || "";
    }
  }

  function candidateStatusClass(candidate) {
    if (candidate.known_wall_changed) return "status-changed";
    return `status-${(candidate.candidate_status || "").toLowerCase()}`;
  }

  function detectionSummaryText(candidate) {
    const counts = `${candidate.member_count} of ${candidate.expected_count} displays`;
    if (candidate.known_wall_changed) {
      return `"${candidate.known_wall_changed.wall_name}" has changed — ${candidate.rows} x ${candidate.columns} — ${counts}`;
    }
    const label = candidate.candidate_status === "AMBIGUOUS" ? "Ambiguous Video Wall configuration"
      : candidate.candidate_status === "CONFLICT" ? "Video Wall configuration conflict"
      : candidate.candidate_status === "PARTIAL" ? "Partial Video Wall"
      : "Possible Video Wall";
    return `${label} — ${candidate.rows} x ${candidate.columns} — ${counts}`;
  }

  function candidateDetailText(candidate) {
    if (candidate.known_wall_changed) return candidate.known_wall_changed.diff.join("; ");
    if (candidate.ambiguous_reason) return candidate.ambiguous_reason;
    const missingText = (candidate.missing || []).length
      ? `Missing: ${candidate.missing.map(m => `R${m.row} C${m.column}`).join(", ")}` : "";
    const conflictText = (candidate.conflicts || []).length
      ? candidate.conflicts.map(c => `R${c.row} C${c.column} claimed by ${c.claimants.length} decoders`).join("; ") : "";
    return [missingText, conflictText].filter(Boolean).join(" — ") ||
      (candidate.matched_saved_wall ? `Matches saved wall "${candidate.matched_saved_wall.name}"` : "Not yet saved as a LumaSuite configuration");
  }

  // ---- "Existing Video Walls Detected" -- compact site-discovery section -
  //
  // Lists every candidate that still needs a decision (data.candidates
  // already filters/classifies -- see _vw_classify_candidates in
  // lumaserver.py). Never renders a candidate that is already an
  // unchanged, confirmed saved wall, and never re-shows a candidate the
  // operator explicitly Ignored unless it materially changed since.

  function renderCandidatesSection(candidates) {
    latestCandidates = candidates || [];
    const banner = document.getElementById("detectionBanner");
    if (!banner) return;
    const reviewable = latestCandidates.filter(c => c.needs_review);
    if (!reviewable.length) { banner.style.display = "none"; banner.innerHTML = ""; return; }
    banner.style.display = "flex";
    banner.innerHTML = `<div class="detect-section-title">Existing Video Walls Detected</div>` +
      reviewable.map(candidate => `
        <div class="detect-item ${candidateStatusClass(candidate)}">
          <div>
            <div class="detect-title"><span class="detect-status-pill">${escapeHtml(candidateStatusLabel(candidate))}</span>${escapeHtml(detectionSummaryText(candidate))}</div>
            <div class="detect-detail">${escapeHtml(candidateDetailText(candidate))}</div>
          </div>
          <div class="detect-actions">
            <button type="button" class="btn-secondary" data-candidate-action="review" data-fingerprint="${escapeHtml(candidate.fingerprint)}">Review</button>
            <button type="button" class="btn-secondary" data-candidate-action="ignore" data-fingerprint="${escapeHtml(candidate.fingerprint)}">Ignore</button>
          </div>
        </div>`).join("");
  }

  async function ignoreCandidate(candidate) {
    await api("/api/video_wall/candidates/ignore", {
      fingerprint: candidate.fingerprint,
      rows: candidate.rows,
      columns: candidate.columns,
      member_count: candidate.member_count,
      status: candidate.candidate_status
    });
    latestCandidates = latestCandidates.filter(c => c.fingerprint !== candidate.fingerprint);
    renderCandidatesSection(latestCandidates);
    toast("Candidate ignored — LumaSuite won't ask about this exact configuration again", true);
  }

  async function detectExistingWall() {
    const data = await api("/api/video_wall/detect");
    state.encoders = data.encoders || state.encoders;
    state.decoders = data.decoders || state.decoders;
    renderCandidatesSection(data.candidates || []);
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
    markWallSaved(currentWall);
    requestRender();
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
    markWallSaved(currentWall);
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
    requestRender();
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
        // Post-route preview burst -- see the matching comment in
        // liveRouteRegion() and burstRefreshPreviewsForCells() above.
        // Covers both the single-cell and "wall" (Entire Wall) scopes:
        // `targets` is already exactly the set of cells this call routed.
        burstRefreshPreviewsForCells(targets);
      }
    } catch (e) {
      if (seq === liveRouteSeq) {
        setStatus("Route failed", "error");
        toast(e.message, false);
      }
    } finally {
      // See the matching comment in liveRouteRegion(): only clear pending on
      // this call's own targets, not every cell in the wall.
      for (const target of targets) delete target.live_source_pending;
      requestRender();
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
      requestRender();
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

  async function liveApplyRotation(cell) {
    if (!cell || !cell.decoder_id) {
      toast("Assign a decoder before applying rotation", false);
      requestRender();
      return;
    }
    syncWallFromForm();
    replaceCurrentInState();
    setStatus("Applying rotation...", "modified");
    try {
      const data = await api("/api/video_wall/rotation", {
        wall: currentWall,
        row: cell.row,
        column: cell.column
      });
      await saveWallQuiet();
      reports = {};
      const count = data.success_count || Object.values(data.results || {}).filter(r => r.ok).length;
      toast(count ? "Rotation applied" : "Rotation update sent", true);
      refreshWall({ silent: true }).catch(() => {});
    } catch (e) {
      setStatus("Rotation apply failed", "error");
      toast(e.message, false);
    }
  }

  async function refreshWall(options = {}) {
    const data = await api("/api/video_wall/refresh", { id: currentWall.id });
    state.encoders = data.encoders || state.encoders;
    state.decoders = data.decoders || state.decoders;
    reports = data.reports || {};
    wallMode = {};
    requestRender();
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

  // Plain-language aggregate of an apply: complete only when every member's
  // wall mode and configuration was read back from its decoder.
  function applyOutcomeText(data) {
    const total = data.members != null ? data.members : Object.keys(data.results || {}).length;
    const done = data.succeeded != null ? data.succeeded : Object.values(data.results || {}).filter(r => r && r.ok).length;
    const failed = data.failed_members || summarizeResultFailures(data).map(reason => ({ label: "", reason }));
    if (!total) return { ok: false, text: "Nothing to apply: no displays have a decoder assigned" };
    if (!failed.length) return { ok: true, text: `Video wall applied and verified on ${done} of ${total} display(s)` };
    const first = failed[0];
    const who = first.label ? `${first.label}: ${first.reason}` : first.reason;
    const more = failed.length > 1 ? ` (+${failed.length - 1} more)` : "";
    return done
      ? { ok: false, text: `Partial: ${done} of ${total} display(s) verified. ${who}${more}` }
      : { ok: false, text: `Failed: no display could be verified. ${who}${more}` };
  }

  async function applyWall() {
    await saveWallQuiet();
    const data = await api("/api/video_wall/apply", { id: currentWall.id }, { allowOkFalse: true });
    const outcome = applyOutcomeText(data);
    toast(outcome.text, outcome.ok);
    setStatus(outcome.ok ? "Video wall applied" : (data.status === "partial" ? "Partial" : "Apply failed"), outcome.ok ? "healthy" : "error");
    reports = {};
    wallMode = {};
    requestRender();
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
    markWallSaved(currentWall);
    reports = {};
    requestRender();
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
    requestRender();
    toast(`Imported ${state.walls.length} wall config(s)`, true);
    refreshWall({ silent: true }).catch(() => {});
  }

  // =====================================================================
  // Wall Actions (...) menu + Wall Details popover (top-bar reorganization
  // pass). Every action inside the menu already existed (Apply Saved
  // Wall, Refresh From Devices, Save, Export/Import, Delete) -- this only
  // groups them and relocates the raw video_walls.json path out of
  // permanent top-bar space, per the operator-facing spec.
  // =====================================================================

  function closeWallActionsMenu() {
    const dropdown = document.getElementById("wallActionsDropdown");
    const btn = document.getElementById("wallActionsBtn");
    if (dropdown) dropdown.hidden = true;
    if (btn) btn.setAttribute("aria-expanded", "false");
  }

  function openWallDetails() {
    const body = document.getElementById("wallDetailsBody");
    const backdrop = document.getElementById("wallDetailsBackdrop");
    if (!body || !backdrop) return;
    if (!currentWall) {
      body.innerHTML = `<div class="empty">No wall selected.</div>`;
      backdrop.classList.add("open");
      return;
    }
    const assigned = (currentWall.cells || []).filter(c => c.decoder_id).length;
    const total = (currentWall.cells || []).length;
    const sourceDevice = idMap(state.encoders).get(currentWall.source_id) || currentWall.source_snapshot;
    const rows = [
      ["Wall ID", currentWall.id],
      ["Name", currentWall.name],
      ["Dimensions", `${currentWall.rows} x ${currentWall.columns}`],
      ["Displays assigned", `${assigned} of ${total}`],
      ["Wall Source", sourceLabel(sourceDevice)],
      ["Active regions", String((currentWall.regions || []).length)],
      ["Config file", lastStateFilePath || "(unknown -- reload the page)"]
    ];
    body.innerHTML = rows.map(([label, value]) =>
      `<div class="kv"><span>${escapeHtml(label)}</span><span>${escapeHtml(value)}</span></div>`).join("");
    backdrop.classList.add("open");
  }

  // =====================================================================
  // Create/Edit Wall wizard (top-bar reorganization pass).
  //
  // wizardWall is a plain wall object built with the SAME createWall()/
  // normalizeWall()/resizeWall() helpers used everywhere else in this
  // file -- the wall data model itself is not reinvented here. Nothing in
  // this section calls api()/fetch() or touches a device: every mutation
  // is local to wizardWall until Step 2's Create Wall/Save Changes button
  // calls the existing saveWall() pipeline. Cancel, Back, the backdrop,
  // and Escape all discard wizardWall with zero writes of any kind.
  // =====================================================================

  const WIZ_PREVIEW_BOX = { width: 420, height: 240 };

  function wizCellSize(rows, columns) {
    const maxCellW = WIZ_PREVIEW_BOX.width / columns;
    const maxCellH = WIZ_PREVIEW_BOX.height / rows;
    return Math.max(18, Math.min(maxCellW, maxCellH, 90));
  }

  // Shared by both steps: a 1x5 wall renders visibly wide, a 5x1 wall
  // visibly tall, a 2x2 roughly square, purely from the actual pixel cell
  // size computed above -- not a fixed aspect-ratio box, which would
  // misrepresent very long/thin layouts.
  function renderMiniGrid(container, rows, columns, cellRenderer) {
    if (!container) return;
    const size = wizCellSize(rows, columns);
    const showLabels = size >= 40;
    container.style.gridTemplateColumns = `repeat(${columns}, ${size}px)`;
    container.style.gridTemplateRows = `repeat(${rows}, ${size}px)`;
    container.innerHTML = "";
    for (let row = 1; row <= rows; row++) {
      for (let column = 1; column <= columns; column++) {
        const cellEl = document.createElement("div");
        cellEl.className = "wiz-cell";
        cellEl.dataset.row = String(row);
        cellEl.dataset.column = String(column);
        cellRenderer(cellEl, row, column, showLabels);
        container.appendChild(cellEl);
      }
    }
  }

  // Rejects, rather than clamps -- Step 1's Next must be blocked on a
  // genuinely invalid value, not silently coerced to something valid.
  // MAX_GRID (16) is the same ceiling normalizeWall()/resizeWall() already
  // enforce everywhere else on this page; not a new limit invented here.
  function parseGridDimension(value) {
    if (String(value == null ? "" : value).trim() === "") return null;
    const n = Number.parseInt(value, 10);
    if (!Number.isFinite(n) || n < 1 || n > MAX_GRID || String(n) !== String(value).trim()) return null;
    return n;
  }

  function renderWizardPreview() {
    const rows = parseGridDimension(document.getElementById("wizRows").value);
    const columns = parseGridDimension(document.getElementById("wizCols").value);
    const errorEl = document.getElementById("wizStep1Error");
    const nextBtn = document.getElementById("wizNextBtn");
    const summary = document.getElementById("wizPreviewSummary");
    if (!rows || !columns) {
      errorEl.textContent = `Rows and Columns must each be a whole number from 1 to ${MAX_GRID}.`;
      errorEl.style.display = "block";
      nextBtn.disabled = true;
      document.getElementById("wizPreviewGrid").innerHTML = "";
      summary.textContent = "";
      return;
    }
    errorEl.style.display = "none";
    nextBtn.disabled = false;
    renderMiniGrid(document.getElementById("wizPreviewGrid"), rows, columns, (cellEl, row, column, showLabels) => {
      cellEl.textContent = showLabels ? `R${row} C${column}` : "";
    });
    const count = rows * columns;
    summary.textContent = `${rows} x ${columns} · ${count} display${count === 1 ? "" : "s"}`;
  }

  // state.decoders is already CS31-excluded upstream (_vw_units_snapshot(),
  // PROJECT.md sec. 24) -- the wizard reuses that same list rather than
  // inventing its own eligibility rule.
  function wizardEligibleDecoders() {
    return state.decoders || [];
  }

  function wizardCellAt(row, column) {
    return (wizardWall.cells || []).find(c => c.row === row && c.column === column) || null;
  }

  function wizardDecoderAssignedCell(decoderId) {
    return (wizardWall.cells || []).find(c => c.decoder_id === decoderId) || null;
  }

  // Pure version (explicit wall/decoders, no module state) so this is
  // independently unit-testable the same way the rest of this file's
  // model logic is (see window.__videoWallTest). The one place decoder
  // placement actually happens -- used by both the drag/drop path and the
  // non-drag click-to-arm/click-to-place path below, so duplicate-
  // assignment and rearrangement/swap semantics live in exactly one
  // function. Moving an already-placed decoder onto an empty cell vacates
  // its old cell; moving it onto an occupied cell swaps the two decoders
  // (never a silent duplicate).
  function placeDecoderInWall(wall, decoders, decoderId, targetCell) {
    if (!decoderId || !targetCell) return wall;
    const decoder = (decoders || []).find(d => d.id === decoderId);
    const sourceCell = (wall.cells || []).find(c => c.decoder_id === decoderId);
    const destOccupantId = targetCell.decoder_id || "";
    const destOccupant = destOccupantId ? (decoders || []).find(d => d.id === destOccupantId) : null;
    if (sourceCell && sourceCell !== targetCell) {
      sourceCell.decoder_id = destOccupantId;
      sourceCell.decoder_snapshot = destOccupant ? compactDeviceSnapshot(destOccupant) : null;
    }
    targetCell.decoder_id = decoderId;
    targetCell.decoder_snapshot = decoder ? compactDeviceSnapshot(decoder) : targetCell.decoder_snapshot;
    return wall;
  }

  function wizardPlaceDecoder(decoderId, targetCell) {
    placeDecoderInWall(wizardWall, wizardEligibleDecoders(), decoderId, targetCell);
  }

  function wizardUnassignCell(cell) {
    if (!cell) return;
    cell.decoder_id = "";
    cell.decoder_snapshot = null;
  }

  // Pure version, explicit params -- see placeDecoderInWall() above for
  // why. New-wall convenience only -- Edit Wall always starts from the
  // wall's own real saved assignments instead (see openWallWizard()).
  // Suggests a decoder for a cell only when its live vwmatrix already
  // claims that exact (rows, columns, row, column) AND no other eligible
  // decoder claims the same cell; an ambiguous claim is left unassigned
  // rather than guessed, mirroring _vw_detect_walls()'s own conflict
  // handling. Never uses discovery order, IP, streamname, or vmenable.
  function suggestDetectedAssignmentsFor(wall, decoders, reportMap) {
    const claims = new Map();
    for (const decoder of decoders || []) {
      const vw = reportMap && reportMap[decoder.id] && reportMap[decoder.id].video_wall;
      if (!vw || !vw.rows || !vw.columns || !vw.row || !vw.column) continue;
      if (Number(vw.rows) !== wall.rows || Number(vw.columns) !== wall.columns) continue;
      const key = `${vw.row}:${vw.column}`;
      if (!claims.has(key)) claims.set(key, []);
      claims.get(key).push(decoder.id);
    }
    for (const cell of wall.cells || []) {
      const claimants = claims.get(`${cell.row}:${cell.column}`) || [];
      if (claimants.length === 1) {
        const decoder = (decoders || []).find(d => d.id === claimants[0]);
        cell.decoder_id = claimants[0];
        cell.decoder_snapshot = decoder ? compactDeviceSnapshot(decoder) : null;
      }
    }
    return wall;
  }

  function suggestDetectedAssignments(wall) {
    return suggestDetectedAssignmentsFor(wall, wizardEligibleDecoders(), reports);
  }

  function renderWizardStep2() {
    renderWizardReviewPanel();
    const decoders = wizardEligibleDecoders();
    const assigned = (wizardWall.cells || []).filter(c => c.decoder_id).length;
    const total = (wizardWall.cells || []).length;
    document.getElementById("wizAssignStatus").textContent = `${assigned} of ${total} displays assigned`;

    const list = document.getElementById("wizDecoderList");
    list.innerHTML = decoders.length ? "" : `<div class="empty">No eligible decoders discovered.</div>`;
    for (const decoder of decoders) {
      const cell = wizardDecoderAssignedCell(decoder.id);
      const tile = document.createElement("div");
      tile.className = "wiz-decoder-tile" +
        (decoder.id === wizardArmedDecoderId ? " armed" : "") +
        (cell ? " placed" : "");
      tile.draggable = true;
      tile.dataset.decoderId = decoder.id;
      tile.innerHTML = `<div class="wiz-decoder-name">${escapeHtml(deviceLabel(decoder))}</div>` +
        `<div class="wiz-decoder-meta">${cell ? `Assigned: R${cell.row} C${cell.column}` : "Unassigned"}</div>`;
      list.appendChild(tile);
    }

    renderMiniGrid(document.getElementById("wizAssignGrid"), wizardWall.rows, wizardWall.columns, (cellEl, row, column) => {
      const cell = wizardCellAt(row, column);
      const decoder = cell && cell.decoder_id
        ? (decoders.find(d => d.id === cell.decoder_id) || cell.decoder_snapshot)
        : null;
      cellEl.classList.toggle("assigned", !!(cell && cell.decoder_id));
      cellEl.innerHTML = `<span class="wiz-cell-pos">R${row} C${column}</span>` +
        `<span class="wiz-cell-decoder">${decoder ? escapeHtml(deviceLabel(decoder)) : "Unassigned"}</span>` +
        (cell && cell.decoder_id ? `<span class="wiz-cell-rotation">` +
          `<select data-wiz-rotation="${row}:${column}">${[0, 90, 180, 270].map(v =>
            `<option value="${v}"${Number(cell.rotation || 0) === v ? " selected" : ""}>${v}°</option>`).join("")}</select>` +
          `<button type="button" class="wiz-unassign-btn" data-wiz-unassign="${row}:${column}">Unassign</button>` +
          `</span>` : "");
    });
  }

  function wizardOrphanedCells(wall, rows, columns) {
    return (wall.cells || []).filter(c => c.decoder_id && (c.row > rows || c.column > columns));
  }

  function setWizardStep(step) {
    document.getElementById("wizardStep1").hidden = step !== 1;
    document.getElementById("wizardStep2").hidden = step !== 2;
  }

  function closeWallWizard() {
    document.getElementById("wallWizardBackdrop").classList.remove("open");
    wizardMode = null;
    wizardWall = null;
    wizardEditingId = null;
    wizardArmedDecoderId = null;
    wizardReviewCandidate = null;
    document.getElementById("wizReviewPanel").style.display = "none";
    document.getElementById("wizIgnoreBtn").style.display = "none";
  }

  // Plain-language evidence summary for the Review dialog's "Why?" panel --
  // checkmarks/tildes/warnings, never a numeric confidence score (see
  // PROJECT.md evidence hierarchy). Reuses the exact same candidate object
  // the candidates-section list already rendered; builds no new model.
  function wizardReviewWhyText(candidate) {
    const lines = [];
    lines.push(`✓ Topology (vwmatrix): ${candidate.member_count} of ${candidate.expected_count} decoders report a matching ${candidate.rows}x${candidate.columns} position -- this is the only field LumaSuite treats as membership evidence.`);
    if (candidate.matched_saved_wall) {
      lines.push(`✓ Saved wall match: this exact set of decoders was previously confirmed as "${candidate.matched_saved_wall.name}".`);
    } else {
      lines.push(`~ No saved wall has this exact set of decoders yet -- this would be a new saved configuration.`);
    }
    lines.push(`~ Current source (streamname) is shown per-display for context only -- it is not used to decide membership, and is known to vary across a genuinely intact wall.`);
    if ((candidate.missing || []).length) {
      lines.push(`⚠ Missing: ${candidate.missing.map(m => `R${m.row} C${m.column}`).join(", ")} -- no decoder currently claims ${candidate.missing.length === 1 ? "this position" : "these positions"}.`);
    }
    if ((candidate.conflicts || []).length) {
      lines.push(`⚠ Conflict: ${candidate.conflicts.map(c => `R${c.row} C${c.column} claimed by ${c.claimants.length} decoders`).join("; ")}.`);
    }
    if (candidate.ambiguous_reason) {
      lines.push(`⚠ ${candidate.ambiguous_reason}`);
    }
    if (candidate.known_wall_changed) {
      lines.push(`⚠ Changed since last confirmed: ${candidate.known_wall_changed.diff.join("; ")}`);
    }
    return lines.join("\n");
  }

  function renderWizardReviewPanel() {
    const panel = document.getElementById("wizReviewPanel");
    if (!wizardReviewCandidate) { panel.style.display = "none"; return; }
    panel.style.display = "block";
    document.getElementById("wizReviewSummary").textContent = detectionSummaryText(wizardReviewCandidate);
    document.getElementById("wizReviewDetail").textContent = candidateDetailText(wizardReviewCandidate);
    document.getElementById("wizReviewWhy").textContent = wizardReviewWhyText(wizardReviewCandidate);
    document.getElementById("wizReviewWhy").style.display = "none";
  }

  function openWallWizard(mode, candidate) {
    wizardArmedDecoderId = null;
    wizardReviewCandidate = null;
    if (mode === "review" && candidate) {
      wizardMode = "review";
      wizardReviewCandidate = candidate;
      // A candidate that already matches a saved wall (including a
      // "known wall changed" one) is an update to that SAME saved wall,
      // not a new one -- otherwise this is a brand-new saved wall.
      wizardEditingId = candidate.matched_saved_wall ? candidate.matched_saved_wall.id : null;
      wizardWall = wallFromDetectedCandidate(candidate);
      document.getElementById("wallWizardTitle").textContent = "Review Detected Video Wall";
      document.getElementById("wizFinishBtn").textContent = "Confirm & Save";
    } else if (mode === "edit" && currentWall) {
      wizardMode = "edit";
      wizardEditingId = currentWall.id;
      // Deep-cloned so every experiment in the wizard (dimensions, drag,
      // rotation) is fully isolated from the live currentWall/state.walls
      // until Save Changes explicitly commits it -- Cancel/Back/Escape
      // leave the real wall completely untouched.
      wizardWall = normalizeWall(JSON.parse(JSON.stringify(currentWall)));
      document.getElementById("wallWizardTitle").textContent = "Edit Video Wall";
      document.getElementById("wizFinishBtn").textContent = "Save Changes";
    } else {
      wizardMode = "create";
      wizardEditingId = null;
      wizardWall = createWall(`Video Wall ${state.walls.length + 1}`, 2, 2);
      document.getElementById("wallWizardTitle").textContent = "Create Video Wall";
      document.getElementById("wizFinishBtn").textContent = "Create Wall";
    }
    document.getElementById("wizName").value = wizardWall.name;
    document.getElementById("wizRows").value = wizardWall.rows;
    document.getElementById("wizCols").value = wizardWall.columns;
    document.getElementById("wizIgnoreBtn").style.display = wizardMode === "review" ? "inline-block" : "none";
    setWizardStep(1);
    renderWizardPreview();
    document.getElementById("wallWizardBackdrop").classList.add("open");
  }

  async function wizardGoNext() {
    const rows = parseGridDimension(document.getElementById("wizRows").value);
    const columns = parseGridDimension(document.getElementById("wizCols").value);
    if (!rows || !columns) return;
    const orphaned = wizardOrphanedCells(wizardWall, rows, columns);
    if (orphaned.length) {
      const summary = orphaned.map(c => ({
        label: `R${c.row} C${c.column}`,
        value: deviceLabel(c.decoder_snapshot || wizardEligibleDecoders().find(d => d.id === c.decoder_id))
      }));
      const ok = await window.lumaDialog.confirm({
        title: "Shrinking this wall will unassign displays",
        message: `Reducing to ${rows} x ${columns} removes ${orphaned.length} assigned display${orphaned.length === 1 ? "" : "s"} from this wall's layout:`,
        summary,
        confirmText: "Continue and unassign",
        danger: true
      });
      if (!ok) return;
    }
    const invalidatedZones = zonesInvalidatedByResize(wizardWall, rows, columns);
    if (invalidatedZones.length) {
      const summary = invalidatedZones.map(z => ({ label: zoneLabel(z), value: `R${z.row1}C${z.column1}–R${z.row2}C${z.column2}` }));
      const ok = await window.lumaDialog.confirm({
        title: "Shrinking this wall will remove saved zones",
        message: `Reducing to ${rows} x ${columns} no longer fits ${invalidatedZones.length} saved routing zone${invalidatedZones.length === 1 ? "" : "s"}. They will be deleted, not silently truncated:`,
        summary,
        confirmText: "Continue and remove",
        danger: true
      });
      if (!ok) return;
    }
    wizardWall.name = document.getElementById("wizName").value || wizardWall.name;
    // resizeWall() is the SAME dimension-change helper createWall()/save/
    // detect already use elsewhere on this page -- valid existing
    // coordinates are preserved automatically; only cells outside the new
    // bounds (already confirmed above, if any were assigned) are dropped.
    wizardWall = resizeWall(wizardWall, rows, columns);
    if (wizardMode === "create" && !(wizardWall.cells || []).some(c => c.decoder_id)) {
      suggestDetectedAssignments(wizardWall);
    }
    wizardArmedDecoderId = null;
    setWizardStep(2);
    renderWizardStep2();
  }

  function wizardGoBack() {
    wizardArmedDecoderId = null;
    setWizardStep(1);
    renderWizardPreview();
  }

  async function wizardFinish() {
    wizardWall.name = document.getElementById("wizName").value || wizardWall.name;
    const finalWall = normalizeWall(wizardWall);
    if ((wizardMode === "edit" || wizardMode === "review") && wizardEditingId) finalWall.id = wizardEditingId;
    // "Confirm & Save Detected Wall" -- local metadata only (see
    // PROJECT.md, "Confirm & Save"). This save call is the SAME
    // /api/video_wall/save every other wall write already used; it never
    // calls a device-mutating endpoint (Set/source/bezel/rotation/apply).
    if (wizardMode === "review") {
      finalWall.origin = "detected";
      finalWall.confirmed_at = Math.floor(Date.now() / 1000);
      finalWall.confirmed_fingerprint = (wizardReviewCandidate && wizardReviewCandidate.device_set_fingerprint) || "";
    }
    const wasNew = wizardMode === "create" || (wizardMode === "review" && !wizardEditingId);
    const reviewedFingerprint = wizardMode === "review" && wizardReviewCandidate ? wizardReviewCandidate.fingerprint : null;
    currentWall = finalWall;
    replaceCurrentInState();
    if (wasNew && !state.walls.some(w => w.id === finalWall.id)) state.walls.push(finalWall);
    rememberSelectedWall(finalWall.id);
    reports = {};
    selectedPos = "1:1";
    closeWallWizard();
    requestRender();
    try {
      await saveWall();
      if (reviewedFingerprint) {
        latestCandidates = latestCandidates.filter(c => c.fingerprint !== reviewedFingerprint);
        renderCandidatesSection(latestCandidates);
      }
      refreshWall({ silent: true }).catch(() => {});
    } catch (e) {
      toast(e.message, false);
    }
  }

  function wireWallWizard() {
    document.getElementById("wizNextBtn").addEventListener("click", () => wizardGoNext().catch(e => toast(e.message, false)));
    document.getElementById("wizBackBtn").addEventListener("click", wizardGoBack);
    document.getElementById("wizFinishBtn").addEventListener("click", () => wizardFinish().catch(e => toast(e.message, false)));
    document.getElementById("wizReviewWhyToggle").addEventListener("click", () => {
      const why = document.getElementById("wizReviewWhy");
      why.style.display = why.style.display === "none" ? "block" : "none";
    });
    document.getElementById("wizIgnoreBtn").addEventListener("click", () => {
      if (!wizardReviewCandidate) return;
      const candidate = wizardReviewCandidate;
      closeWallWizard();
      ignoreCandidate(candidate).catch(e => toast(e.message, false));
    });
    for (const id of ["wizCancelBtn1", "wizCancelBtn2"]) {
      document.getElementById(id).addEventListener("click", closeWallWizard);
    }
    document.getElementById("wallWizardBackdrop").addEventListener("click", event => {
      if (event.target.id === "wallWizardBackdrop") closeWallWizard();
    });
    document.addEventListener("keydown", event => {
      if (event.key === "Escape" && document.getElementById("wallWizardBackdrop").classList.contains("open")) {
        closeWallWizard();
      }
    });
    for (const id of ["wizRows", "wizCols"]) {
      document.getElementById(id).addEventListener("input", renderWizardPreview);
    }

    // Non-drag assignment: click a decoder tile to "arm" it, then click a
    // destination cell to place it there (a second, required path since
    // drag/drop cannot be the only one). Clicking the already-armed tile
    // again disarms it.
    document.getElementById("wizDecoderList").addEventListener("click", event => {
      const tile = event.target.closest(".wiz-decoder-tile");
      if (!tile) return;
      const id = tile.dataset.decoderId;
      wizardArmedDecoderId = wizardArmedDecoderId === id ? null : id;
      renderWizardStep2();
    });
    document.getElementById("wizAssignGrid").addEventListener("click", event => {
      if (event.target.closest("select, button")) return;
      const cellEl = event.target.closest(".wiz-cell");
      if (!cellEl) return;
      const cell = wizardCellAt(Number(cellEl.dataset.row), Number(cellEl.dataset.column));
      if (!cell) return;
      if (wizardArmedDecoderId) {
        wizardPlaceDecoder(wizardArmedDecoderId, cell);
        wizardArmedDecoderId = null;
      } else if (cell.decoder_id) {
        // Clicking an already-assigned cell with nothing armed arms ITS
        // decoder instead, so a subsequent click on another cell moves
        // (or swaps) it -- the same click-driven flow works for both
        // initial placement and rearrangement.
        wizardArmedDecoderId = cell.decoder_id;
      }
      renderWizardStep2();
    });
    document.getElementById("wizAssignGrid").addEventListener("change", event => {
      const select = event.target.closest("select[data-wiz-rotation]");
      if (!select) return;
      const [row, column] = select.dataset.wizRotation.split(":").map(Number);
      const cell = wizardCellAt(row, column);
      if (cell) cell.rotation = clampInt(select.value, 0, 270, 0);
    });
    document.getElementById("wizAssignGrid").addEventListener("click", event => {
      const btn = event.target.closest("[data-wiz-unassign]");
      if (!btn) return;
      const [row, column] = btn.dataset.wizUnassign.split(":").map(Number);
      const cell = wizardCellAt(row, column);
      wizardUnassignCell(cell);
      if (wizardArmedDecoderId && !wizardDecoderAssignedCell(wizardArmedDecoderId)) {
        // leave it armed -- still a valid pending placement
      }
      renderWizardStep2();
    });

    // Drag/drop path -- reuses the same wizardPlaceDecoder()/wizardCellAt()
    // functions the click path uses, so both paths share one set of
    // duplicate/rearrangement rules.
    document.getElementById("wizDecoderList").addEventListener("dragstart", event => {
      const tile = event.target.closest(".wiz-decoder-tile");
      if (!tile) return;
      event.dataTransfer.setData("text/plain", tile.dataset.decoderId);
      event.dataTransfer.effectAllowed = "move";
    });
    const wizAssignGrid = document.getElementById("wizAssignGrid");
    wizAssignGrid.addEventListener("dragover", event => {
      const cellEl = event.target.closest(".wiz-cell");
      if (!cellEl) return;
      event.preventDefault();
      cellEl.classList.add("drop-target");
    });
    wizAssignGrid.addEventListener("dragleave", event => {
      const cellEl = event.target.closest(".wiz-cell");
      if (cellEl) cellEl.classList.remove("drop-target");
    });
    wizAssignGrid.addEventListener("drop", event => {
      const cellEl = event.target.closest(".wiz-cell");
      if (!cellEl) return;
      event.preventDefault();
      const decoderId = event.dataTransfer.getData("text/plain");
      const cell = wizardCellAt(Number(cellEl.dataset.row), Number(cellEl.dataset.column));
      if (decoderId && cell) wizardPlaceDecoder(decoderId, cell);
      wizardArmedDecoderId = null;
      renderWizardStep2();
    });
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
    document.addEventListener("focusout", () => {
      window.setTimeout(flushDeferredRender, 150);
    });

    document.getElementById("wallSelect").addEventListener("change", event => {
      currentWall = state.walls.find(w => w.id === event.target.value) || currentWall;
      rememberSelectedWall(currentWall?.id);
      markWallSaved(currentWall);
      reports = {};
      selectedPos = "1:1";
      requestRender(true);
      refreshWall({ silent: true }).catch(() => {});
    });
    document.getElementById("newWallBtn").addEventListener("click", () => {
      closeWallActionsMenu();
      openWallWizard("create");
    });
    document.getElementById("editWallBtn").addEventListener("click", () => {
      closeWallActionsMenu();
      if (!currentWall) return;
      openWallWizard("edit");
    });
    document.getElementById("saveWallBtn").addEventListener("click", () => {
      closeWallActionsMenu();
      saveWall().catch(e => toast(e.message, false));
    });
    document.getElementById("refreshWallBtn").addEventListener("click", () => {
      closeWallActionsMenu();
      refreshWall().catch(e => toast(e.message, false));
    });
    document.getElementById("applyWallBtn").addEventListener("click", () => {
      closeWallActionsMenu();
      applyWall().catch(e => toast(e.message, false));
    });
    document.getElementById("deleteWallBtn").addEventListener("click", () => {
      closeWallActionsMenu();
      deleteWall().catch(e => toast(e.message, false));
    });
    document.getElementById("identifyBtn").addEventListener("click", () => blinkSelectedDecoder());
    document.getElementById("routeWallSourceBtn").addEventListener("click", () => routeSelectedWallSource());
    document.getElementById("exportWallConfigBtn").addEventListener("click", () => {
      closeWallActionsMenu();
      exportWallConfig().catch(e => toast(e.message, false));
    });
    document.getElementById("importWallConfigBtn").addEventListener("click", () => {
      closeWallActionsMenu();
      document.getElementById("importWallConfigFile").click();
    });

    // ---- Wall Actions (...) menu -----------------------------------
    const wallActionsBtn = document.getElementById("wallActionsBtn");
    const wallActionsDropdown = document.getElementById("wallActionsDropdown");
    wallActionsBtn.addEventListener("click", event => {
      event.stopPropagation();
      const open = wallActionsDropdown.hidden;
      wallActionsDropdown.hidden = !open;
      wallActionsBtn.setAttribute("aria-expanded", String(open));
    });
    document.addEventListener("click", event => {
      if (!wallActionsDropdown.hidden && !event.target.closest(".wall-actions-menu")) closeWallActionsMenu();
    });

    // ---- Wall Details modal (relocated video_walls.json path + basic
    // diagnostics out of permanent top-bar space, per the top-bar
    // reorganization) ---------------------------------------------------
    document.getElementById("wallDetailsBtn").addEventListener("click", () => {
      closeWallActionsMenu();
      openWallDetails();
    });
    document.getElementById("wallDetailsCloseBtn").addEventListener("click", () => {
      document.getElementById("wallDetailsBackdrop").classList.remove("open");
    });
    document.getElementById("wallDetailsBackdrop").addEventListener("click", event => {
      if (event.target.id === "wallDetailsBackdrop") event.currentTarget.classList.remove("open");
    });

    wireWallWizard();
    const wallPreviewToggle = document.getElementById("wallPreviewToggle");
    if (wallPreviewToggle) {
      wallPreviewEnabled = loadWallPreviewEnabled();
      wallPreviewOpacity = loadWallPreviewOpacity();
      wallPreviewRefreshSeconds = loadWallPreviewRefreshSeconds();
      wallPreviewToggle.checked = wallPreviewEnabled;
      updateWallPreviewControls();
      wallPreviewToggle.addEventListener("change", () => {
        wallPreviewEnabled = wallPreviewToggle.checked;
        if (wallPreviewEnabled) wallPreviewRefreshTimestamp = Date.now();
        saveWallPreviewEnabled(wallPreviewEnabled);
        updateWallPreviewControls();
        if (!wallPreviewEnabled) stopWallPreviewRefresh();
        renderGrid();
      });
    }
    const wallPreviewOpacitySlider = document.getElementById("wallPreviewOpacity");
    if (wallPreviewOpacitySlider) {
      wallPreviewOpacitySlider.value = String(wallPreviewOpacity);
      wallPreviewOpacitySlider.addEventListener("input", () => {
        wallPreviewOpacity = clampInt(wallPreviewOpacitySlider.value, 20, 100, DEFAULT_WALL_PREVIEW_OPACITY);
        saveWallPreviewOpacity(wallPreviewOpacity);
        renderGrid();
      });
    }
    const wallPreviewRefreshSlider = document.getElementById("wallPreviewRefreshSeconds");
    if (wallPreviewRefreshSlider) {
      wallPreviewRefreshSlider.value = String(wallPreviewRefreshSeconds);
      wallPreviewRefreshSlider.addEventListener("input", () => {
        wallPreviewRefreshSeconds = clampInt(wallPreviewRefreshSlider.value, 2, 10, DEFAULT_WALL_PREVIEW_REFRESH_SECONDS);
        saveWallPreviewRefreshSeconds(wallPreviewRefreshSeconds);
        updateWallPreviewControls();
        if (wallPreviewEnabled) scheduleWallPreviewRefresh();
      });
    }
    document.getElementById("importWallConfigFile").addEventListener("change", event => {
      importWallConfig(event.target.files?.[0]).catch(e => toast(e.message, false));
      event.target.value = "";
    });

    // Name/Rows/Columns inputs and their change handler were removed from
    // this page's toolbar (top-bar reorganization pass) -- editing them
    // now only happens through the Create/Edit Wall wizard (wireWallWizard()).
    for (const id of ["totalWidth", "totalHeight", "topBorder", "bottomBorder", "leftBorder", "rightBorder"]) {
      document.getElementById(id).addEventListener("change", () => liveApplyBezel());
    }
    // Deliberately no "change" listener here: picking a Wall Source is a
    // proposal, not an action. Only the explicit "Apply to Entire Wall"
    // button (routeWallSourceBtn, wired below) commits it -- routing on
    // mere selection was the pre-existing behavior and directly
    // contradicted the operator-facing contract of that button existing
    // at all (see PROJECT.md, top-bar reorganization pass).

    document.getElementById("wallGrid").addEventListener("click", event => {
      if (event.target.closest("select, input, button, option, label")) return;
      const cellEl = event.target.closest(".wall-cell");
      if (!cellEl) return;
      if (zoneEditMode) return; // grid clicks are captured for zone drawing while editing (see wireZoneEditing())
      if (armedSourceId) {
        // Non-drag routing path: a source is armed, so this click picks a
        // single-cell target instead of opening the inspector -- see
        // routeArmedSourceTo(), which calls the SAME assignSpatialTarget()
        // the drag/drop path calls, and routes immediately.
        const [row, column] = cellEl.dataset.pos.split(":").map(Number);
        routeArmedSourceTo([{ row, column }], `R${row} C${column}`);
        return;
      }
      selectedPos = cellEl.dataset.pos;
      // Task 9's preferred flow: click a monitor's decoder/rotation chip to
      // jump straight into that field, not just select the cell. The
      // inspector itself remains the single place the value is actually
      // changed -- this only opens it and focuses the right control.
      const focusAction = event.target.closest("[data-action='open-decoder']") ? "decoder"
        : event.target.closest("[data-action='open-rotation']") ? "rotation" : null;
      renderGrid();
      syncDisplayFormFromSelected();
      const freshCellEl = document.querySelector(`.wall-cell[data-pos="${selectedPos}"]`);
      openInspector(freshCellEl || cellEl);
      if (focusAction) {
        window.requestAnimationFrame(() => {
          const select = document.querySelector(`#details select[data-action="${focusAction}"]`);
          if (!select) return;
          select.focus();
          if (typeof select.showPicker === "function") {
            try { select.showPicker(); } catch { /* not all browsers support showPicker() on select */ }
          }
        });
      }
    });

    // Decoder assignment, per-cell source fallback and rotation now live in
    // the Selected Display panel -- the grid cell itself is a monitor
    // visual, a click target and a drop target, not a form.
    document.getElementById("details").addEventListener("change", event => {
      const select = event.target.closest("select[data-action]");
      const cell = selectedCell();
      if (select && cell) {
        if (select.dataset.action === "decoder") {
          cell.decoder_id = select.value;
          wallMode = {};
          cell.decoder_snapshot = compactDeviceSnapshot(idMap(state.decoders).get(cell.decoder_id)) || cell.decoder_snapshot || null;
          reports = {};
          replaceCurrentInState();
          renderGrid();
          setStatus("Unsaved changes", "modified");
          scheduleAutoSaveRefresh();
          return;
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
        if (select.dataset.action === "wall-mode") {
          if (select.value === "enabled" || select.value === "disabled") {
            setDecoderWallMode(cell, select.value === "enabled").catch(e => toast(e.message, false));
          }
          return;
        }
        if (select.dataset.action === "rotation") {
          cell.rotation = clampInt(select.value, 0, 270, 0);
          replaceCurrentInState();
          liveApplyRotation(cell);
          return;
        }
      }
      const input = event.target.closest("input[data-action='cell-bezel']");
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

    document.getElementById("details").addEventListener("click", event => {
      const readBtn = event.target.closest("[data-action='wall-mode-read']");
      if (readBtn && selectedCell()) {
        readDecoderWallMode(selectedCell()).catch(e => toast(e.message, false));
        return;
      }
      const btn = event.target.closest("[data-action='clear-region']");
      const cell = selectedCell();
      if (!btn || !cell || !currentWall) return;
      const region = cellRegion(currentWall, cell);
      if (!region) return;
      // detachRegionAndRoute() (not just the clicked cell) keeps every
      // sibling cell in the dissolved region in sync with what its decoder
      // is actually showing -- see its own comment for why.
      detachRegionAndRoute(region).catch(e => toast(e.message, false));
    });

    wireSourcePalette();
    wireWallDragAndDrop();
    wireZoneEditing();
    window.addEventListener("resize", () => {
      repositionInspectorIfOpen();
    });

    // ---- Contextual inspector dismissal (task 5) -----------------------
    const inspectorCloseBtn = document.getElementById("inspectorCloseBtn");
    if (inspectorCloseBtn) inspectorCloseBtn.addEventListener("click", closeInspector);
    document.addEventListener("click", event => {
      if (!inspectorOpen) return;
      const inspector = document.getElementById("displayInspector");
      if (!inspector || inspector.contains(event.target)) return;
      // A click on a wall cell is handled by the grid's own click listener
      // above (which may keep the inspector open on a different cell) --
      // only an outside click should dismiss it here.
      if (event.target.closest(".wall-cell")) return;
      closeInspector();
    });

    // ---- Device Reports compact summary/details toggle (task 6) -------
    const reportsToggleBtn = document.getElementById("reportsToggleBtn");
    if (reportsToggleBtn) {
      reportsToggleBtn.addEventListener("click", () => {
        const panel = document.getElementById("reports");
        if (!panel) return;
        const willShow = panel.hidden;
        panel.hidden = !willShow;
        reportsToggleBtn.setAttribute("aria-expanded", String(willShow));
        reportsToggleBtn.textContent = willShow ? "Hide" : "Details";
      });
    }

    // ---- Global cleanup for the source hover preview (task 1/12) ------
    // A drag starting anywhere (not just on the tile itself), the window
    // losing focus, the page scrolling, or Escape all end the preview the
    // same way closeSourcePreview() does everywhere else -- one function,
    // every exit path.
    document.addEventListener("dragstart", closeSourcePreview, true);
    window.addEventListener("scroll", closeSourcePreview, true);
    window.addEventListener("blur", closeSourcePreview);
    document.addEventListener("keydown", event => {
      if (event.key !== "Escape") return;
      closeSourcePreview();
      closeInspector();
    });

    document.getElementById("detectionBanner").addEventListener("click", event => {
      const btn = event.target.closest("[data-candidate-action]");
      if (!btn) return;
      const candidate = latestCandidates.find(c => c.fingerprint === btn.dataset.fingerprint);
      if (!candidate) return;
      if (btn.dataset.candidateAction === "review") {
        openWallWizard("review", candidate);
      } else if (btn.dataset.candidateAction === "ignore") {
        ignoreCandidate(candidate).catch(e => toast(e.message, false));
      }
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
      statusFromReports,
      cleanRegions,
      cellRegion,
      regionGeometry,
      effectiveGeometryAndSource,
      parseGridDimension,
      wizCellSize,
      placeDecoderInWall,
      suggestDetectedAssignmentsFor,
      wizardOrphanedCells,
      cleanZones,
      zoneCellsOf,
      zoneLabel,
      zonesInvalidatedByResize,
      autoZonesForWall,
      zonesForRouting,
      zoneRailTargets,
      zoneForCells,
      cellsInRegion,
      wallFromDetectedCandidate,
      candidateStatusLabel,
      candidateStatusClass,
      detectionSummaryText,
      candidateDetailText,
      wizardReviewWhyText,
      // Post-route preview-refresh burst (UX improvement pass): exposed
      // read-only/pure pieces plus one narrow test-only setter for the
      // module-scoped `wallPreviewEnabled` flag, which is otherwise only
      // ever toggled via the (absent in tests) #wallPreviewToggle checkbox.
      decoderPreviewBaseUrl,
      previewUrlWithCacheBuster,
      refreshWallPreviewImages,
      burstRefreshPreviewsForCells,
      // Routing / wall-mode behaviour hooks, so the click path and the
      // Video Wall Mode control can be driven end to end in a synthetic DOM.
      armSourceForNonDragRouting,
      routeArmedSourceTo,
      renderDetails,
      wallModeFor,
      setDecoderWallMode,
      readDecoderWallMode,
      applyOutcomeText,
      applyWall,
      __setWallStateForTest: (wall, enc, dec, sel) => { currentWall = wall; state = { walls: [wall], encoders: enc, decoders: dec }; if (sel) selectedPos = sel; wallMode = {}; reports = {}; },
      __getWallModeForTest: () => wallMode,
      PREVIEW_BURST_DELAYS_MS,
      __setWallPreviewEnabledForTest: value => { wallPreviewEnabled = !!value; }
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
