// The Matrix page's side of the stale-request contract (LUMA-REL-002), and the route table it must agree with.
//
// A page built from an inventory that Clear Units has since emptied must not act on it. The server refuses
// a route that carries a revision from before the clear (409) and one for a unit it does not hold (404);
// the page's part is to remember the revision it was built from and send it back with every route.
//
// This is source-level (comments stripped first, so an explanatory comment cannot satisfy an assertion):
// the behaviour in a real browser is in the evidence run, and the server's half is test_clear_no_resurrection.py.
'use strict';
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.join(__dirname, '..');
const read = f => fs.readFileSync(path.join(ROOT, f), 'utf8').replace(/\r\n/g, '\n');
const strip = s => s.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:'"`\\])\/\/.*$/gm, '$1');

const matrix = strip(read('ui/matrix.js'));
const server = read('lumaserver.py');
let passed = 0;
const scenario = (name, fn) => { fn(); passed++; console.log('ok   ' + name); };

scenario('the page keeps the inventory revision the server sent with the units', () => {
  assert.match(matrix, /inventoryEpoch\s*=\s*typeof data\.inventory_epoch === 'string' \? data\.inventory_epoch : null/);
});

scenario('every route the page sends carries that revision when it has one', () => {
  const at = matrix.indexOf('const postData = { decoder_ip: targetIp, encoder_ip: encIp, mode: routeMode };');
  assert.ok(at >= 0, 'the route request is not built where expected');
  const next = matrix.slice(at, at + 400);
  assert.match(next, /if \(inventoryEpoch\) postData\.inventory_epoch = inventoryEpoch;/);
  // and what was built is what is sent
  assert.match(matrix.slice(at), /body:\s*JSON\.stringify\(postData\)/);
});

scenario('the revision is read where the units are read, not invented by the page', () => {
  // read from the answer (checked and taken) and sent: nowhere else
  assert.strictEqual((matrix.match(/inventory_epoch/g) || []).length, 3,
    'the page must read the revision from the units answer and send it, and do nothing else with it');
});

scenario('the endpoint the page posts to is a route the server has', () => {
  assert.match(matrix, /fetch\('\/api\/route_matrix'/);
  assert.match(server, /@APP\.post\("\/api\/route_matrix"\)/);
  assert.match(server, /@APP\.get\("\/api\/cache"\)/);
});

scenario('the server serves the revision on the read the page uses', () => {
  assert.match(server, /jsonify\(\{"units": served, "inventory_epoch": _inventory_epoch\(\)/);
});

console.log(passed + ' scenarios passed');
