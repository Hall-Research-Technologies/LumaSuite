// The Light background override: one colour, applied through tokens, kept with the appearance.
//
// The control is generic -- nothing in it depends on a product -- and exists in both applications so
// the Appearance section reads the same. What is fenced here:
//
//   * a stored colour reaches BOTH <html> and <body> (the light palette is declared on both, and a value
//     on one is shadowed on the other), and an invalid or absent one reaches neither;
//   * an untouched installation is untouched: no attribute, no property, the palette it always had;
//   * the colour is validated as a colour before it reaches a custom property;
//   * every other appearance change keeps it, and only Reset Appearance drops it;
//   * the derived surfaces are tokens whose mixes sum to 100 (a smaller sum multiplies alpha, and every
//     surface built on it goes translucent) and the override is stated for <html> and <body> alike.
//
// The boot script is executed, not read.
'use strict';
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const ROOT = path.join(__dirname, '..');
const read = rel => fs.readFileSync(path.join(ROOT, rel), 'utf8').split('\r\n').join('\n');
const BOOT = read('ui/theme-boot.js');
const SETTINGS = read('ui/settings.js');
const FAMILY = read('ui/family.css');
const strip = src => src.replace(/\/\*[\s\S]*?\*\//g, '').split('\n').map(l => l.split('//')[0]).join('\n');

let passed = 0;
function scenario(name, fn) {
  try { fn(); passed++; } catch (err) { console.error('FAILED: ' + name + '\n  ' + (err && err.stack || err)); process.exitCode = 1; }
}

function host() {
  const props = {}, attrs = {}, classes = new Set();
  return {
    props, attrs, classes,
    style: {setProperty: (k, v) => { props[k] = v; }, removeProperty: k => { delete props[k]; }},
    setAttribute: (k, v) => { attrs[k] = String(v); }, removeAttribute: k => { delete attrs[k]; },
    getAttribute: k => (k in attrs ? attrs[k] : null),
    classList: {toggle: (c, on) => { if (on) classes.add(c); else classes.delete(c); }},
  };
}

function boot(stored) {
  const html = host(), body = host();
  const window = {};
  const sandbox = {window, document: {documentElement: html, body: null},
    localStorage: {getItem: k => (k === 'lumasuite.appearance' && stored !== undefined ? JSON.stringify(stored) : null)}};
  vm.createContext(sandbox);
  vm.runInContext(BOOT, sandbox);
  return {html, body, window, mirror: () => { sandbox.document.body = body; window.__lumaMirrorBody(); }};
}

scenario('a stored colour reaches <html>, and <body> once it exists', () => {
  const b = boot({theme: 'light', density: 'classic', preset: 'default', lightBackground: '#dfe9d6'});
  assert.strictEqual(b.html.props['--light-bg'], '#dfe9d6');
  assert.strictEqual(b.html.attrs['data-light-bg'], '#dfe9d6');
  b.mirror();
  assert.strictEqual(b.body.props['--light-bg'], '#dfe9d6', 'a value on <html> alone is shadowed on <body>');
});

scenario('an untouched installation gets no attribute and no property', () => {
  for (const stored of [undefined, {theme: 'light'}, {theme: 'light', lightBackground: '#f7f8fb'},
                        {theme: 'light', lightBackground: '#F7F8FB'}]) {
    const b = boot(stored);
    assert.ok(!('--light-bg' in b.html.props), JSON.stringify(stored));
    assert.strictEqual(b.html.attrs['data-light-bg'], undefined, JSON.stringify(stored));
  }
});

scenario('a value that is not a colour never reaches a custom property', () => {
  for (const bad of ['red', 'url(javascript:1)', '#12', '#12345', '#gggggg', '', 12, null, {a: 1},
                     '#fff; background: url(x)', 'expression(1)']) {
    const b = boot({theme: 'light', lightBackground: bad});
    assert.ok(!('--light-bg' in b.html.props), 'accepted ' + JSON.stringify(bad));
    assert.strictEqual(b.html.attrs['data-light-bg'], undefined, JSON.stringify(bad));
  }
});

scenario('a short hex is expanded and the case is normalised', () => {
  const b = boot({theme: 'light', lightBackground: '#ABC'});
  assert.strictEqual(b.html.props['--light-bg'], '#aabbcc');
});

scenario('applying a colour later, or the default, adds and removes it cleanly', () => {
  const b = boot({theme: 'light'});
  b.window.__lumaApplyLightBackground('#e0e8f0');
  assert.strictEqual(b.html.attrs['data-light-bg'], '#e0e8f0');
  b.window.__lumaApplyLightBackground('#f7f8fb');
  assert.strictEqual(b.html.attrs['data-light-bg'], undefined);
  assert.ok(!('--light-bg' in b.html.props));
  assert.strictEqual(b.window.__lumaNormalizeLightBackground('nope'), null);
});

scenario('the boot script reports the colour in the appearance it publishes', () => {
  const b = boot({theme: 'light', lightBackground: '#dfe9d6'});
  assert.strictEqual(b.window.__lumaAppearance.lightBackground, '#dfe9d6');
  assert.strictEqual(boot({}).window.__lumaAppearance.lightBackground, '#f7f8fb');
});

scenario('every appearance change keeps the colour; only Reset Appearance drops it', () => {
  const code = strip(SETTINGS);
  const calls = code.split('persistAppearance(').slice(1).map(c => c.slice(0, c.indexOf(');')));
  assert.ok(calls.length >= 5, 'expected the density, preset, theme, colour and reset calls');
  for (const call of calls) {
    if (call.startsWith('appearance')) continue;                     // the definition's own parameter list
    const merges = call.startsWith('Object.assign({}, ');
    const reset = /density: "classic", preset: "default"/.test(call);
    assert.ok(merges || reset, 'builds an appearance without carrying the colour: ' + call);
    if (reset) assert.ok(/lightBackground: "#f7f8fb"/.test(call), 'reset must restore the default colour');
  }
});

scenario('a typed value is validated before it is saved, and a refusal says why', () => {
  const code = strip(SETTINGS);
  const commit = code.slice(code.indexOf('function commitLightBackground'), code.indexOf('function wireLightBackground'));
  assert.ok(/__lumaNormalizeLightBackground/.test(commit));
  assert.ok(commit.indexOf('if (!colour)') > -1 && commit.indexOf('if (!colour)') < commit.indexOf('persistAppearance('),
    'the colour is checked before anything is saved');
  assert.ok(/Not a colour\. Use a hex value/.test(commit));
});

scenario('the derived surfaces are tokens, summing to 100, for <html> and <body> alike', () => {
  const css = strip(FAMILY);
  const at = css.indexOf('html.light[data-light-bg]');
  assert.ok(at > 0, 'the override block is missing');
  const block = css.slice(at, css.indexOf('.light-bg-choice {'));
  assert.ok(/html\.light\[data-light-bg\]/.test(block), '<html> is not covered');
  assert.ok(/html\[data-light-bg\] body\.light/.test(block), '<body> is not covered');
  const mixes = block.match(/color-mix\([^;]+\)/g) || [];
  assert.strictEqual(mixes.length, 3);
  for (const mix of mixes) {
    const percentages = (mix.match(/(\d+)%/g) || []).map(p => parseInt(p, 10));
    assert.strictEqual(percentages.reduce((a, b) => a + b, 0), 100, 'mix does not sum to 100: ' + mix);
  }
  assert.ok(!/#[0-9a-fA-F]{3,6}\b(?!\s*(?:\d+%|\)))/.test(block.replace(/#ffffff|#000000/g, '')),
    'a literal colour is in the override block');
  assert.ok(/--surface:\s+var\(--light-bg\)/.test(block));
});

scenario('the Appearance section carries the control', () => {
  assert.ok(/id="cfg_light_background"/.test(SETTINGS));
  assert.ok(/renderLightBackground\(\);/.test(strip(SETTINGS)));
  assert.ok(/wireLightBackground\(\);/.test(strip(SETTINGS)));
  assert.ok(/aria-label="Light mode background"/.test(SETTINGS), 'the picker needs an accessible name');
  assert.ok(/aria-label="Light mode background hex value"/.test(SETTINGS));
});

if (!process.exitCode) console.log('light background: ' + passed + ' scenarios passed');
