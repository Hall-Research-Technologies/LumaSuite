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
  statusFromReports
} = sandbox.window.__videoWallTest;

const plain = value => JSON.parse(JSON.stringify(value));

assert.equal(wallDeviceId({ mac: "B8:98:B0:0F:02:4A", ip: "192.168.200.53" }), "mac:b8:98:b0:0f:02:4a");
assert.equal(wallDeviceId({ serialnumber: "ABC123", ip: "192.168.200.53" }), "serialnumber:abc123");
assert.equal(sourceLabel({ ip: "192.168.100.41", hostname: "Encoder", streamname: "MainStream" }), "192.168.100.41 - MainStream");
assert.equal(sourceLabel({ ip: "192.168.100.41", hostname: "Encoder" }), "192.168.100.41 - Encoder");

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
    { id: "mac:enc", ip: "192.168.100.41", mac: "enc", hostname: "Encoder" },
    { id: "mac:override", ip: "192.168.100.43", mac: "override", hostname: "Override" }
  ],
  [{ id: "mac:dec", ip: "192.168.200.51", mac: "dec", hostname: "Decoder" }]
);
assert.equal(enriched.source_snapshot.ip, "192.168.100.41");
assert.equal(enriched.cells[0].decoder_snapshot.ip, "192.168.200.51");
assert.equal(enriched.cells[1].override_source_snapshot.ip, "192.168.100.43");

snapAllDisplaysToWallVideo(enriched);
assert.deepEqual(
  plain(enriched.cells.map(c => [c.source_mode, c.override_source_id, c.override_source_snapshot])),
  [["wall", "", null], ["wall", "", null]]
);

console.log("video wall model tests passed");
