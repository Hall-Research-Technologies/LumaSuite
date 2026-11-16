const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "..", "ui", "wall.js"), "utf8");
const sandbox = {
  window: {},
  document: { addEventListener() {} },
  console,
  Date,
  Math,
  Number,
  String,
  Map,
  Set,
  Array,
  Object
};

vm.createContext(sandbox);
vm.runInContext(source, sandbox);

const {
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
  cellsInRegion,
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
  wallFromDetectedCandidate,
  candidateStatusLabel,
  candidateStatusClass,
  detectionSummaryText,
  candidateDetailText,
  wizardReviewWhyText
} = sandbox.window.__videoWallTest;

const plain = value => JSON.parse(JSON.stringify(value));

assert.equal(wallDeviceId({ mac: "02:00:5E:10:00:4A", ip: "198.51.100.53" }), "mac:02:00:5e:10:00:4a");
assert.equal(wallDeviceId({ serialnumber: "ABC123", ip: "198.51.100.53" }), "serialnumber:abc123");
assert.equal(sourceLabel({ ip: "203.0.113.41", hostname: "Encoder", streamname: "MainStream" }), "203.0.113.41 - MainStream");
assert.equal(sourceLabel({ ip: "203.0.113.41", hostname: "Encoder" }), "203.0.113.41 - Encoder");

const wall = createWall("Lobby", 2, 2);
assert.equal(wall.cells.length, 4);
assert.deepEqual(plain(wall.cells.map(c => [c.row, c.column])), [[1, 1], [1, 2], [2, 1], [2, 2]]);

wall.cells[0].decoder_id = "mac:first";
wall.cells[3].decoder_id = "mac:last";
const grown = resizeWall(wall, 3, 2);
assert.equal(grown.cells.length, 6);
assert.equal(grown.cells.find(c => c.row === 1 && c.column === 1).decoder_id, "mac:first");
assert.equal(grown.cells.find(c => c.row === 2 && c.column === 2).decoder_id, "mac:last");
assert.deepEqual(plain(grown.cells.find(c => c.row === 3 && c.column === 2)), {
  row: 3,
  column: 2,
  decoder_id: "",
  source_mode: "wall",
  override_source_id: "",
  rotation: 0,
  display: {},
  decoder_snapshot: null,
  override_source_snapshot: null
});

const normalized = normalizeWall({
  rows: 1,
  columns: 2,
  display: { topborder: 2 },
  cells: [{ row: 1, column: 2, decoder_id: "mac:two", source_mode: "override", rotation: 90 }]
});
assert.equal(normalized.display.topborder, 2);
assert.equal(normalized.cells[1].decoder_id, "mac:two");
assert.equal(normalized.cells[1].source_mode, "override");
assert.equal(normalized.cells[1].rotation, 90);

const reports = {
  "mac:first": { online: true, video_wall: { rows: 2, columns: 2, row: 1, column: 1, vmenable: true }, rotation: 0 },
  "mac:last": { online: true, video_wall: { rows: 2, columns: 2, row: 2, column: 2, vmenable: true }, rotation: 0 }
};
assert.equal(statusFromReports(wall, {}), "Not refreshed");
assert.deepEqual(plain(cellDiffs(wall, wall.cells[0], reports["mac:first"])), []);
assert.deepEqual(plain(cellDiffs(wall, wall.cells[3], reports["mac:last"])), []);
assert.equal(statusFromReports(wall, reports), "Healthy");

const modifiedReports = {
  ...reports,
  "mac:last": { online: true, video_wall: { rows: 2, columns: 2, row: 1, column: 2, vmenable: true }, rotation: 0 }
};
assert.equal(statusFromReports(wall, modifiedReports), "Modified");

const overrideWall = normalizeWall({ ...wall, cells: wall.cells.map((c, i) => i === 0 ? { ...c, source_mode: "override" } : c) });
assert.equal(statusFromReports(overrideWall, reports), "Override");

const dedupeWall = createWall("Dedupe", 1, 3);
dedupeWall.cells[0].decoder_id = "mac:a";
dedupeWall.cells[1].decoder_id = "mac:b";
const decoders = [{ id: "mac:a" }, { id: "mac:b" }, { id: "mac:c" }];
assert.deepEqual(
  plain(availableDecodersForCell(decoders, dedupeWall.cells, dedupeWall.cells[2]).map(d => d.id)),
  ["mac:c"]
);
assert.deepEqual(
  plain(availableDecodersForCell(decoders, dedupeWall.cells, dedupeWall.cells[0]).map(d => d.id)),
  ["mac:a", "mac:c"]
);

const snapshotWall = createWall("Snapshots", 1, 2);
snapshotWall.source_id = "mac:enc";
snapshotWall.cells[0].decoder_id = "mac:dec";
snapshotWall.cells[1].source_mode = "override";
snapshotWall.cells[1].override_source_id = "mac:override";
const enriched = enrichWallWithSnapshots(
  snapshotWall,
  [
    { id: "mac:enc", ip: "203.0.113.41", mac: "enc", hostname: "Encoder" },
    { id: "mac:override", ip: "203.0.113.43", mac: "override", hostname: "Override" }
  ],
  [{ id: "mac:dec", ip: "198.51.100.51", mac: "dec", hostname: "Decoder" }]
);
assert.equal(enriched.source_snapshot.ip, "203.0.113.41");
assert.equal(enriched.cells[0].decoder_snapshot.ip, "198.51.100.51");
assert.equal(enriched.cells[1].override_source_snapshot.ip, "203.0.113.43");

snapAllDisplaysToWallVideo(enriched);
assert.deepEqual(
  plain(enriched.cells.map(c => [c.source_mode, c.override_source_id, c.override_source_snapshot])),
  [["wall", "", null], ["wall", "", null]]
);

// ---- Spatial routing: regions ------------------------------------------
// crosshairZoneCells() itself is not exercised here -- it reads the
// module-private `currentWall`, which this sandbox never sets -- but the
// live drag-and-drop path (all 9 targets: 4 single-display, 4 boundary
// pairs, 1 center) was validated end-to-end in a real browser against a
// real 2x2 wall; see the Video Wall spatial-routing completion report.

const twoByTwo = createWall("Regions", 2, 2);

assert.deepEqual(
  plain(cleanRegions([{ id: "top", cells: [{ row: 1, column: 1 }, { row: 1, column: 2 }], source_id: "enc-a" }], 2, 2)),
  [{ id: "top", cells: [{ row: 1, column: 1 }, { row: 1, column: 2 }], source_id: "enc-a", source_snapshot: null }]
);
assert.deepEqual(plain(cleanRegions([{ id: "solo", cells: [{ row: 1, column: 1 }], source_id: "enc-a" }], 2, 2)), []);
assert.deepEqual(
  plain(cleanRegions([{ id: "lshape", cells: [{ row: 1, column: 1 }, { row: 1, column: 2 }, { row: 2, column: 1 }], source_id: "enc-a" }], 2, 2)),
  []
);
assert.deepEqual(plain(cleanRegions("not-an-array", 2, 2)), []);
// Overlap: the second region loses, entirely, rather than splitting the cell.
const overlapping = cleanRegions([
  { id: "r1", cells: [{ row: 1, column: 1 }, { row: 1, column: 2 }], source_id: "enc-a" },
  { id: "r2", cells: [{ row: 1, column: 1 }, { row: 2, column: 1 }], source_id: "enc-b" }
], 2, 2);
assert.equal(overlapping.length, 1);
assert.equal(overlapping[0].id, "r1");

twoByTwo.regions = cleanRegions(
  [{ id: "top", cells: [{ row: 1, column: 1 }, { row: 1, column: 2 }], source_id: "enc-a" }], 2, 2);
const r1c1 = twoByTwo.cells.find(c => c.row === 1 && c.column === 1);
const r1c2 = twoByTwo.cells.find(c => c.row === 1 && c.column === 2);
const r2c1 = twoByTwo.cells.find(c => c.row === 2 && c.column === 1);

assert.deepEqual(plain(cellRegion(twoByTwo, r1c1)), plain(twoByTwo.regions[0]));
assert.equal(cellRegion(twoByTwo, r2c1), null);
assert.deepEqual(plain(regionGeometry(twoByTwo.regions[0], r1c1)), { rows: 1, columns: 2, row: 1, column: 1 });
assert.deepEqual(plain(regionGeometry(twoByTwo.regions[0], r1c2)), { rows: 1, columns: 2, row: 1, column: 2 });

// Regression: "Detach from region" must resync EVERY cell in the dissolved
// region, not just the one the operator clicked -- cellsInRegion() is the
// pure lookup detachRegionAndRoute() uses to find them all. A prior defect
// only fixed the clicked cell, leaving a sibling modeled as "wall video"
// while its physical decoder kept showing whatever the region was routing.
assert.deepEqual(
  plain(cellsInRegion(twoByTwo, twoByTwo.regions[0]).map(c => [c.row, c.column])),
  [[1, 1], [1, 2]]
);
assert.equal(cellsInRegion(twoByTwo, twoByTwo.regions[0]).length, twoByTwo.regions[0].cells.length,
  "every member cell of the region must be found, not just one");
// A cell outside the region must never be swept in.
assert.ok(!cellsInRegion(twoByTwo, twoByTwo.regions[0]).some(c => c.row === 2 && c.column === 1));

// A region wins over the cell's own source_mode ("wall" here); a cell
// outside the region is unaffected and still resolves from the whole wall.
twoByTwo.source_id = "wall-src";
assert.deepEqual(plain(effectiveGeometryAndSource(twoByTwo, r1c1)), { rows: 1, columns: 2, row: 1, column: 1, enabled: true, sourceId: "enc-a" });
assert.deepEqual(plain(effectiveGeometryAndSource(twoByTwo, r2c1)), { rows: 2, columns: 2, row: 2, column: 1, enabled: true, sourceId: "wall-src" });

const overrideCell = { ...r2c1, source_mode: "override", override_source_id: "override-src" };
assert.deepEqual(plain(effectiveGeometryAndSource(twoByTwo, overrideCell)), { rows: 2, columns: 2, row: 2, column: 1, enabled: false, sourceId: "override-src" });

// The old 2x2-only crosshair is retired; the same four convenience pair
// targets are now generated on the fly by autoZonesForWall() (never
// persisted -- see PROJECT.md's "2x2 DEFAULT ZONES" decision) and resolved
// generically by zoneForCells(), which works for ANY wall size, not just 2x2.
assert.equal(zoneForCells(twoByTwo, [{ row: 1, column: 1 }, { row: 1, column: 2 }]).name, "Top Row");
assert.equal(zoneForCells(twoByTwo, [{ row: 2, column: 1 }, { row: 2, column: 2 }]).name, "Bottom Row");
assert.equal(zoneForCells(twoByTwo, [{ row: 1, column: 1 }, { row: 2, column: 1 }]).name, "Left Column");
assert.equal(zoneForCells(twoByTwo, [{ row: 1, column: 2 }, { row: 2, column: 2 }]).name, "Right Column");
assert.equal(autoZonesForWall(twoByTwo).length, 4);
assert.equal(autoZonesForWall(createWall("NotSquare", 1, 3)).length, 0, "auto convenience zones only exist for exactly 2x2");

// ---- Saved zone definitions: a SEPARATE, distinct lifecycle from regions -
//
// wall.regions = live/active routing state, cleared by "Apply to Entire
// Wall" (asserted above via snapAllDisplaysToWallVideo). wall.zones = saved,
// reusable rectangle DEFINITIONS that survive that clear -- see PROJECT.md,
// "the most architecturally important decision in this task".

assert.deepEqual(
  plain(cleanZones([{ id: "top", name: "Top Row", row1: 1, column1: 1, row2: 1, column2: 3 }], 1, 3)),
  [{ id: "top", name: "Top Row", row1: 1, column1: 1, row2: 1, column2: 3 }]
);
// A single-cell "zone" is redundant (every cell is already a 1x1 target).
assert.deepEqual(plain(cleanZones([{ id: "z", row1: 1, column1: 1, row2: 1, column2: 1 }], 2, 2)), []);
// Out of bounds is dropped outright, never clamped/guessed.
assert.deepEqual(plain(cleanZones([{ id: "z", row1: 1, column1: 1, row2: 5, column2: 5 }], 3, 3)), []);
// Reversed corners normalize the same as top-left-to-bottom-right.
assert.deepEqual(
  plain(cleanZones([{ id: "z", row1: 2, column1: 2, row2: 1, column2: 1 }], 2, 2))[0],
  { id: "z", name: "", row1: 1, column1: 1, row2: 2, column2: 2 }
);
// Overlapping zone DEFINITIONS are explicitly allowed (unlike regions).
const overlappingZones = cleanZones([
  { id: "top", name: "Top Row", row1: 1, column1: 1, row2: 1, column2: 3 },
  { id: "left", name: "Left 2x2", row1: 1, column1: 1, row2: 2, column2: 2 },
  { id: "all", name: "Entire Wall", row1: 1, column1: 1, row2: 3, column2: 3 }
], 3, 3);
assert.equal(overlappingZones.length, 3, "overlapping saved zones must all be kept, unlike regions");

assert.deepEqual(plain(zoneCellsOf({ row1: 1, column1: 1, row2: 1, column2: 3 })),
  [{ row: 1, column: 1 }, { row: 1, column: 2 }, { row: 1, column: 3 }]);
assert.deepEqual(plain(zoneCellsOf({ row1: 2, column1: 2, row2: 3, column2: 3 })),
  [{ row: 2, column: 2 }, { row: 2, column: 3 }, { row: 3, column: 2 }, { row: 3, column: 3 }]);

assert.equal(zoneLabel({ name: "Top Row", row1: 1, column1: 1, row2: 1, column2: 3 }), "Top Row");
const fallbackLabel = zoneLabel({ name: "", row1: 1, column1: 1, row2: 1, column2: 3 });
assert.ok(fallbackLabel.includes("1x3") && fallbackLabel.includes("R1C1") && fallbackLabel.includes("R1C3"),
  "an unnamed zone must still get an automatic, informative label -- never a blank name");
// Representative larger sizes from the task spec's 1x1-8x8 range.
for (const [r1, c1, r2, c2, expectDims] of [
  [1, 1, 2, 2, "2x2"], [1, 1, 3, 3, "3x3"], [1, 1, 4, 4, "4x4"], [1, 1, 5, 5, "5x5"], [1, 1, 8, 8, "8x8"],
  [4, 4, 5, 5, "2x2"] // bottom-right-touching, not top-left
]) {
  const label = zoneLabel({ name: "", row1: r1, column1: c1, row2: r2, column2: c2 });
  assert.ok(label.includes(expectDims), `expected ${expectDims} in label for R${r1}C${c1}-R${r2}C${c2}, got "${label}"`);
}

// Dimension-change invalidation: a zone that would no longer fit must be
// flagged (by the wizard's confirmation, before resizeWall runs), never
// silently truncated by cleanZones() alone.
const dimWall = createWall("DimTest", 3, 3);
dimWall.zones = cleanZones([
  { id: "fits", row1: 1, column1: 1, row2: 2, column2: 2 },
  { id: "wont-fit", row1: 1, column1: 1, row2: 3, column2: 3 }
], 3, 3);
const invalidated = zonesInvalidatedByResize(dimWall, 2, 2);
assert.equal(invalidated.length, 1);
assert.equal(invalidated[0].id, "wont-fit");
// A zone whose coordinates remain valid survives a resize unchanged.
const resizedDimWall = resizeWall(dimWall, 2, 2);
assert.equal(resizedDimWall.zones.length, 1);
assert.equal(resizedDimWall.zones[0].id, "fits");

// Backward compatibility: a wall saved before zones existed loads cleanly
// with an empty zones array, never crashes, never fabricates a zone.
const legacyWall = normalizeWall({ id: "w1", rows: 2, columns: 2, cells: [] });
assert.deepEqual(plain(legacyWall.zones), []);

// zonesForRouting() combines saved + auto zones, each carrying derived cells.
const routingWall = createWall("Routing", 2, 2);
routingWall.zones = cleanZones([{ id: "custom", name: "Custom", row1: 1, column1: 1, row2: 2, column2: 1 }], 2, 2);
const routingTargets = zonesForRouting(routingWall);
assert.equal(routingTargets.length, 5); // 1 saved + 4 auto (2x2)
assert.ok(routingTargets.find(z => z.id === "custom" && z.cells.length === 2));
assert.ok(routingTargets.every(z => Array.isArray(z.cells) && z.cells.length >= 2));

// ---- Retiring the crosshair: no leftover DEAD CODE (explanatory comments
// mentioning the old approach for context are fine; the functions/DOM hooks
// it used to hit-test/position/style itself must actually be gone).
const wallJsSourceForCrosshairCheck = fs.readFileSync(path.join(__dirname, "..", "ui", "wall.js"), "utf8");
for (const deadSymbol of ["positionCrosshair", "crosshairZoneCells", "crosshairZoneForCells", "crosshairDragging", "getElementById(\"wallCrosshair\")"]) {
  assert.ok(!wallJsSourceForCrosshairCheck.includes(deadSymbol), `wall.js must not still reference ${deadSymbol}`);
}
assert.ok(!/id="wallCrosshair"|class="wall-cross-zone|wall-crosshair\s*\{/.test(fs.readFileSync(path.join(__dirname, "..", "ui", "wall.html"), "utf8")),
  "the old crosshair markup/CSS must be fully retired from wall.html, not left dead");

// Regression: the Decoder/Source/Rotation <select> elements in the Selected
// Display panel (rendered by renderDetails() into a ".kv" row) have no
// explicit width, and a native <select> otherwise shrinks-to-fit its longest
// <option> text (e.g. "203.0.113.43 - room-a") rather than the grid
// column's minmax(0,1fr) track. Verified live (browser automation) at both
// 1280x720 and 1440x900, all Light/Dark x Classic/Compact combinations: this
// overflowed the panel by a fixed ~25px and produced page-wide horizontal
// scroll on every combination until ".kv select { width:100% }" was added.
// This is a static presence check (no headless browser in this test file's
// pure-logic sandbox) guarding against the rule being silently removed.
const wallHtmlSource = fs.readFileSync(path.join(__dirname, "..", "ui", "wall.html"), "utf8");
assert.match(wallHtmlSource, /\.kv\s+select\s*\{[^}]*width\s*:\s*100%/,
  ".kv select must be constrained to its row's width or it overflows the Selected Display panel");

// ---- Create/Edit Wall wizard: pure logic (top-bar reorganization pass) --

// Apply to Entire Wall must dissolve regions, not just cell overrides --
// a region wins over a cell's own source_mode (effectiveGeometryAndSource,
// asserted above), and the backend's /api/video_wall/source scope="wall"
// handler deliberately excludes any cell still in a region, so leaving
// wall.regions populated here would silently survive an "apply to entire
// wall" click on both the device and the saved config.
const regionedWall = createWall("Regioned", 2, 2);
regionedWall.regions = cleanRegions(
  [{ id: "top", cells: [{ row: 1, column: 1 }, { row: 1, column: 2 }], source_id: "enc-a" }], 2, 2);
assert.equal(regionedWall.regions.length, 1);
snapAllDisplaysToWallVideo(regionedWall);
assert.deepEqual(plain(regionedWall.regions), []);

// Step 1 validation: reject, don't clamp -- Next must stay blocked on a
// genuinely invalid value.
assert.equal(parseGridDimension("2"), 2);
assert.equal(parseGridDimension("16"), 16);
assert.equal(parseGridDimension("0"), null);
assert.equal(parseGridDimension("-1"), null);
assert.equal(parseGridDimension("17"), null);
assert.equal(parseGridDimension(""), null);
assert.equal(parseGridDimension("abc"), null);
assert.equal(parseGridDimension("2.5"), null);

// Mini preview orientation: a 1x5 wall must render visibly wide (total
// rendered width > height), 5x1 visibly tall, 2x2 roughly square -- from
// the actual pixel cell size, not a fixed aspect-ratio box.
function renderedBox(rows, columns) {
  const size = wizCellSize(rows, columns);
  return { width: columns * size, height: rows * size };
}
const wide1x5 = renderedBox(1, 5);
assert.ok(wide1x5.width > wide1x5.height, "1x5 must render wider than tall");
const tall5x1 = renderedBox(5, 1);
assert.ok(tall5x1.height > tall5x1.width, "5x1 must render taller than wide");
const square2x2 = renderedBox(2, 2);
assert.equal(square2x2.width, square2x2.height, "2x2 must render square");
const wider2x4 = renderedBox(2, 4);
assert.ok(wider2x4.width > wider2x4.height, "2x4 must render wider than tall");

// Decoder assignment: fresh placement, move-to-empty, and swap (never a
// silent duplicate) all go through the one placeDecoderInWall() function.
const wizDecoders = [{ id: "dec-a", ip: "10.0.0.1" }, { id: "dec-b", ip: "10.0.0.2" }];
const wizWall = createWall("Wizard", 1, 2);
const [wc1, wc2] = wizWall.cells;
placeDecoderInWall(wizWall, wizDecoders, "dec-a", wc1);
assert.equal(wc1.decoder_id, "dec-a");
assert.equal(wc2.decoder_id, "");
// Moving dec-a onto the still-empty wc2 vacates wc1 -- never duplicated.
placeDecoderInWall(wizWall, wizDecoders, "dec-a", wc2);
assert.equal(wc1.decoder_id, "");
assert.equal(wc2.decoder_id, "dec-a");
assert.equal(wizDecoders.filter(d => wizWall.cells.some(c => c.decoder_id === d.id)).length, 1);
// Placing dec-b into the now-empty wc1, then swapping the two cells.
placeDecoderInWall(wizWall, wizDecoders, "dec-b", wc1);
placeDecoderInWall(wizWall, wizDecoders, "dec-a", wc1); // dec-a (in wc2) -> wc1, swaps with dec-b
assert.equal(wc1.decoder_id, "dec-a");
assert.equal(wc2.decoder_id, "dec-b");

// Dimension-decrease orphan detection (Edit Wall / wizard Step 1 -> 2).
const shrinkWall = createWall("Shrink", 2, 2);
shrinkWall.cells.find(c => c.row === 2 && c.column === 2).decoder_id = "dec-a";
assert.equal(wizardOrphanedCells(shrinkWall, 2, 2).length, 0);
assert.equal(wizardOrphanedCells(shrinkWall, 1, 1).length, 1);
assert.equal(wizardOrphanedCells(shrinkWall, 1, 2).length, 1);

// Detected-coordinate prepopulation: unambiguous claim -> prepopulated;
// two decoders claiming the same cell -> left unassigned (never guessed).
const detectDecoders = [{ id: "dec-ul", ip: "10.0.0.10" }, { id: "dec-ur", ip: "10.0.0.11" }, { id: "dec-x", ip: "10.0.0.12" }];
const detectReports = {
  "dec-ul": { video_wall: { rows: 2, columns: 2, row: 1, column: 1 } },
  "dec-ur": { video_wall: { rows: 2, columns: 2, row: 1, column: 2 } },
  "dec-x": { video_wall: { rows: 2, columns: 2, row: 1, column: 2 } } // conflicts with dec-ur at R1C2
};
const newWall = createWall("New", 2, 2);
suggestDetectedAssignmentsFor(newWall, detectDecoders, detectReports);
assert.equal(newWall.cells.find(c => c.row === 1 && c.column === 1).decoder_id, "dec-ul");
assert.equal(newWall.cells.find(c => c.row === 1 && c.column === 2).decoder_id, "", "ambiguous R1C2 claim must be left unassigned, not guessed");
assert.equal(newWall.cells.find(c => c.row === 2 && c.column === 1).decoder_id, "");
// A decoder whose live coordinate is for a DIFFERENT wall size never
// prepopulates a mismatched wall.
const mismatchWall = createWall("Mismatch", 1, 2);
suggestDetectedAssignmentsFor(mismatchWall, detectDecoders, detectReports);
assert.ok(mismatchWall.cells.every(c => !c.decoder_id));

// Regression guard: picking a Wall Source must never itself route/apply
// anything -- only the explicit "Apply to Entire Wall" button click may
// call routeSelectedWallSource(). A prior pass had a wallSource "change"
// listener that auto-routed the whole wall on mere selection.
const wallJsSource = fs.readFileSync(path.join(__dirname, "..", "ui", "wall.js"), "utf8");
assert.ok(
  !/getElementById\(["']wallSource["']\)\.addEventListener\(["']change["'][\s\S]{0,120}routeSelectedWallSource/.test(wallJsSource),
  "wallSource dropdown must not auto-route on change -- only the Apply to Entire Wall button may"
);

// Regression guard: permanent Wall Name/Rows/Columns editing must stay
// out of the main workspace -- only the Create/Edit Wall wizard's own
// wizName/wizRows/wizCols fields may exist.
assert.ok(!/id="wallName"|id="rowsInput"|id="colsInput"/.test(wallHtmlSource),
  "Wall Name/Rows/Columns must not reappear as permanent top-bar controls");
assert.ok(/id="wizName"/.test(wallHtmlSource) && /id="wizRows"/.test(wallHtmlSource) && /id="wizCols"/.test(wallHtmlSource),
  "the Create/Edit Wall wizard must own Name/Rows/Columns editing");

// ---- Site-discovery candidate rendering (frontend mirror of
// lumaserver._vw_classify_candidates -- see test_video_wall_candidates.py
// for the backend classification logic itself; these are the pure
// presentation helpers the "Existing Video Walls Detected" section and
// the Review dialog's Why? panel use). ----

const possibleCandidate = {
  rows: 2, columns: 2, status: "complete", candidate_status: "POSSIBLE",
  member_count: 4, expected_count: 4, missing: [], conflicts: [], ambiguous_reason: null,
  matched_saved_wall: null, known_wall_changed: null, fingerprint: "fp1", device_set_fingerprint: "dsfp1",
  members: {
    "1:1": { device_id: "ul", label: "ul", ip: "198.51.100.51", rotation: 180, source: "stream-main" },
    "1:2": { device_id: "ur", label: "ur", ip: "198.51.100.68", rotation: 180, source: "stream-main" },
    "2:1": { device_id: "ll", label: "ll", ip: "203.0.113.53", rotation: 0, source: "stream-main" },
    "2:2": { device_id: "lr", label: "lr", ip: "198.51.100.54", rotation: 0, source: "stream-main" }
  }
};

assert.equal(candidateStatusLabel(possibleCandidate), "Possible");
assert.equal(candidateStatusClass(possibleCandidate), "status-possible");
assert.ok(/Possible Video Wall/.test(detectionSummaryText(possibleCandidate)));
assert.ok(/Not yet saved/.test(candidateDetailText(possibleCandidate)));
assert.ok(!/streamname alone/.test(wizardReviewWhyText(possibleCandidate)));
// Never a numeric confidence score anywhere in the Why? text.
assert.ok(!/%|confidence/i.test(wizardReviewWhyText(possibleCandidate)),
  "Why? evidence text must be qualitative, never a numeric confidence score");

const ambiguousCandidate = {
  ...possibleCandidate, status: "conflict", candidate_status: "AMBIGUOUS",
  ambiguous_reason: "8 decoders contain 2x2 Video Wall configuration; LumaSuite cannot reliably determine which decoders belong to each physical wall.",
  conflicts: [{ row: 1, column: 1, claimants: ["a1", "b1"] }]
};
assert.equal(candidateStatusLabel(ambiguousCandidate), "Ambiguous");
assert.equal(candidateStatusClass(ambiguousCandidate), "status-ambiguous");
assert.ok(/cannot reliably determine/.test(candidateDetailText(ambiguousCandidate)));

const changedCandidate = {
  ...possibleCandidate, candidate_status: "DETECTED",
  matched_saved_wall: { id: "wall-1", name: "Lobby 2x2" },
  known_wall_changed: { wall_id: "wall-1", wall_name: "Lobby 2x2", diff: ["ul: moved from R1 C1 to R1 C2"] }
};
assert.equal(candidateStatusLabel(changedCandidate), "Changed");
assert.equal(candidateStatusClass(changedCandidate), "status-changed");
assert.ok(/has changed/.test(detectionSummaryText(changedCandidate)));
assert.ok(/moved from/.test(candidateDetailText(changedCandidate)));

// wallFromDetectedCandidate (the pre-existing single-wall detection
// helper, reused unchanged by the new multi-candidate Review dialog) still
// derives its default name from rows/columns, never from streamname.
const built = wallFromDetectedCandidate(possibleCandidate);
assert.equal(built.rows, 2);
assert.equal(built.columns, 2);
assert.ok(!/stream-main/i.test(built.name), "default detected-wall name must never be inferred from streamname");
assert.equal(built.cells.find(c => c.row === 1 && c.column === 1).decoder_id, "ul");

// ---------------------------------------------------------------------
// zoneRailTargets(): the zone-rail presentation ORDER (UX improvement --
// "Entire Wall must be first"). This is order-only: it must not change
// target ids, routing semantics, or zonesForRouting()'s own (unordered-
// significant) return value used elsewhere for hit-testing/labeling.
// ---------------------------------------------------------------------
{
  const railWall = createWall("Lobby", 2, 2);
  railWall.cells[0].decoder_id = "mac:ul"; // at least one assignment so "Entire Wall" is offered
  railWall.zones = [
    { id: "zone-custom", name: "My Zone", row1: 1, column1: 1, row2: 1, column2: 1 }
  ];
  const targets = zoneRailTargets(railWall);
  assert.deepEqual(plain(targets.map(t => t.id)), [
    "__entire_wall__", "auto-top", "auto-bottom", "auto-left", "auto-right", "zone-custom"
  ], "Entire Wall must be first, followed by the 2x2 auto zones, then saved/custom zones");
  assert.equal(targets[0].label, "Entire Wall");
  assert.equal(targets[0].kind, "entire-wall");
  assert.deepEqual(plain(targets[0].cells.map(c => `${c.row}:${c.column}`)).sort(),
    ["1:1", "1:2", "2:1", "2:2"], "Entire Wall target must still cover every cell (whole-wall semantics unchanged)");
  assert.equal(targets[1].label, "Top Row");
  assert.equal(targets[4].label, "Right Column");
  assert.equal(targets[5].label, "My Zone");

  // Target ids/labels/cell membership for the pre-existing auto zones and
  // the saved zone are byte-for-byte identical to what zonesForRouting()
  // already produced before this change -- only the rail's presentation
  // order moved, nothing about what a target IS.
  const unordered = zonesForRouting(railWall);
  for (const t of targets.filter(t => t.kind !== "entire-wall")) {
    const match = unordered.find(z => z.id === t.id);
    assert.ok(match, `zonesForRouting() must still expose target id ${t.id}`);
    assert.deepEqual(plain(t.cells), plain(match.cells));
  }

  // A 2x2 wall with no decoder assigned anywhere and no saved zones still
  // offers its four auto convenience zones (autoZonesForWall() has never
  // depended on decoder assignment -- unchanged by this pass), but withholds
  // "Entire Wall" specifically, matching the pre-existing
  // `hasAnyAssignment` gate that only ever applied to that one chip.
  const emptyWall = createWall("Empty", 2, 2);
  assert.deepEqual(plain(zoneRailTargets(emptyWall).map(t => t.id)),
    ["auto-top", "auto-bottom", "auto-left", "auto-right"]);

  // A wall with no decoder assigned AND no dimensions that qualify for auto
  // zones (non-2x2) and no saved zones offers no rail targets at all
  // (matches the pre-existing hidden-rail behavior).
  const emptyWide = createWall("Empty Row", 1, 3);
  assert.equal(zoneRailTargets(emptyWide).length, 0);

  // Larger, non-2x2 walls: no auto convenience zones exist (autoZonesForWall
  // is 2x2-only, unchanged by this pass), so "Entire Wall" is immediately
  // followed by saved zones with no gap in between.
  const wideWall = createWall("Lobby Row", 1, 3);
  wideWall.cells[0].decoder_id = "mac:a";
  wideWall.zones = [{ id: "zone-w", name: "Right Two", row1: 1, column1: 2, row2: 1, column2: 3 }];
  const wideTargets = zoneRailTargets(wideWall);
  assert.deepEqual(plain(wideTargets.map(t => t.id)), ["__entire_wall__", "zone-w"]);
}

console.log("video wall model tests passed");
