// The A/V Matrix routing cell: the audio dot lives INSIDE the video ring, in every one of the four states.
'use strict';
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const read = f => fs.readFileSync(path.join(__dirname, '..', f), 'utf8');
function lift(source, name) {
  const at = source.search(new RegExp(String.raw`(?:async\s+)?function\s+` + name + String.raw`\s*\(`));
  assert.ok(at >= 0, name + ' not found');
  let depth = 0;
  for (let i = source.indexOf('{', at); i < source.length; i++) {
    if (source[i] === '{') depth++;
    else if (source[i] === '}' && --depth === 0) return source.slice(at, i + 1);
  }
  throw new Error('unbalanced ' + name);
}
const js = read('ui/matrix.js');
const ctx = {};
vm.createContext(ctx);
vm.runInContext(lift(js, 'esc') + '\n' + lift(js, 'routeCellHtml'), ctx);
const cell = (decV, decA, encV, encA) => ctx.routeCellHtml(
  {ip: '192.0.2.10', dec: {video: {streamname: decV}, audio: {streamname: decA}}},
  {ip: '192.0.2.20', enc: {video: {streamname: encV}, audio: {streamname: encA}}});

const states = {
  'AV together': [cell('S', 'S', 'S', 'S'), true, true],
  'video only': [cell('S', 'other', 'S', 'S'), true, false],
  'audio only': [cell('other', 'S', 'S', 'S'), false, true],
  'breakaway elsewhere': [cell('A', 'B', 'S', 'S'), false, false],
};
for (const [name, [html, video, audio]] of Object.entries(states)) {
  assert.strictEqual(/<input[^>]* checked>/.test(html), video, name + ': video ring');
  assert.strictEqual(/class="dot audio-on"/.test(html), audio, name + ': audio dot');
  // dot inside the wrapper, after the input, never a sibling of the wrapper
  assert.ok(/<span class="radio-wrap"><input[^>]*>(<span class="dot[^"]*"[^>]*><\/span>)<\/span><\/td>/.test(html), name + ': dot inside radio-wrap');
  assert.ok(html.startsWith('<td class="cell">'), name + ': cell');
}
// a breakaway cell shows the video ring on one source and the dot on ANOTHER
const bv = cell('S', 'other', 'S', 'S'), ba = cell('other', 'S', 'S', 'S');
assert.ok(/checked/.test(bv) && !/audio-on/.test(bv) && !/checked/.test(ba) && /audio-on/.test(ba));

// geometry: ring and dot are centred in the same fixed-size wrapper, independent of the cell
const css = read('ui/matrix.css').replace(/\/\*[\s\S]*?\*\//g, '');
const rule = sel => {
  const escaped = sel.split('').map(c => '.[]()'.includes(c) ? String.fromCharCode(92) + c : c).join('');
  const m = css.match(new RegExp(escaped + String.raw`\s*\{([^}]*)\}`));
  assert.ok(m, sel);
  return m[1];
};
assert.ok(/position:\s*relative/.test(rule('.radio-wrap')) && /width:\s*20px/.test(rule('.radio-wrap')));
assert.ok(/position:\s*absolute/.test(rule('.radio-wrap input')) && /left:\s*50%/.test(rule('.radio-wrap input')) && /top:\s*50%/.test(rule('.radio-wrap input')));
const dotRules = [...css.matchAll(/\.radio-wrap \.dot\s*\{([^}]*)\}/g)].map(m => m[1]).join(';');
assert.ok(/position:\s*absolute/.test(dotRules) && /left:\s*50%/.test(dotRules) && /top:\s*50%/.test(dotRules) && /translate\(-50%,\s*-50%\)/.test(dotRules));
assert.ok(/pointer-events:\s*none/.test(dotRules), 'the dot never intercepts the click meant for the ring');
assert.ok(!/\.audio-dot/.test(css) && !/audio-dot/.test(js), 'the old sibling dot is gone');
// A route needs both ends answering: a cell with an offline end cannot be chosen, and the
// server refuses it as well. Online cells are unchanged, byte for byte.
const reachable = (decOnline, encOnline) => ctx.routeCellHtml(
  {ip: '192.0.2.10', online: decOnline, dec: {video: {streamname: 'S'}, audio: {streamname: 'S'}}},
  {ip: '192.0.2.20', online: encOnline, enc: {video: {streamname: 'S'}, audio: {streamname: 'S'}}});
assert.ok(!/disabled/.test(reachable(true, true)), 'both ends answering: selectable');
assert.ok(!/disabled/.test(reachable(undefined, undefined)), 'no flag from an older server: selectable');
assert.ok(/<input[^>]* disabled aria-disabled="true">/.test(reachable(false, true)), 'an offline display');
assert.ok(/<input[^>]* disabled aria-disabled="true">/.test(reachable(true, false)), 'an offline source');
assert.ok(/class="dot audio-on"/.test(reachable(false, true)), 'the dot is still inside the wrapper when disabled');
assert.ok(/\.route-radio:disabled/.test(css), 'a disabled cell is visibly unavailable');
console.log('matrix indicator: ok');
