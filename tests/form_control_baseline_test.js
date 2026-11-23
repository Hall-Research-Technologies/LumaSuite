// Form controls take their colours from the theme on every page.
//
// The defect: in Dark, the Matrix page (and the Settings dialog on it) painted WHITE text boxes, selects
// and password fields, because the control rules lived in other pages' own styles and a page that loaded
// none of them fell back to the browser's painting. Measured in a real browser before the fix: all 21
// controls on /ui/matrix.html reported rgb(255, 255, 255) with Dark selected; after it, none did.
//
// A unit test cannot compute styles, so this pins what the browser measurement depends on:
//   * one baseline in family.css (which every page loads), written in semantic tokens, never a literal;
//   * wrapped in :where() so it has no specificity and every page-specific rule still wins;
//   * covering every kind of control that paints, and leaving checkboxes, radios, range, colour and
//     buttons to their own treatment;
//   * every page with controls loads family.css;
//   * the dialog's controls share one size, so the dialog is the same height on every page.
'use strict';
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.join(__dirname, '..');
const read = rel => fs.readFileSync(path.join(ROOT, rel), 'utf8').split('\r\n').join('\n');
const strip = css => css.replace(/\/\*[\s\S]*?\*\//g, '');
const FAMILY = strip(read('ui/family.css'));

let passed = 0;
function scenario(name, fn) {
  try { fn(); passed++; } catch (err) { console.error('FAILED: ' + name + '\n  ' + (err && err.stack || err)); process.exitCode = 1; }
}

const start = FAMILY.indexOf(':where(select, textarea,');
assert.ok(start > 0, 'the baseline is missing');
const block = FAMILY.slice(start, FAMILY.indexOf('}', start) + 1);

scenario('the baseline paints from tokens and never from a literal', () => {
  for (const d of ['background-color: var(--surface-elevated)', 'color: var(--text)', 'border: 1px solid var(--edge)']) {
    assert.ok(block.includes(d), d + ' is missing');
  }
  assert.ok(!/#[0-9a-fA-F]{3,8}\b/.test(block), 'a literal colour in the baseline');
  assert.ok(!/\brgba?\(|\bhsla?\(/.test(block), 'a literal colour in the baseline');
  assert.ok(!/\b(white|black)\b/.test(block), 'a colour keyword in the baseline');
});

scenario('it has no specificity of its own', () => {
  const selector = block.slice(0, block.indexOf('{'));
  const outside = selector.replace(/:where\((?:[^()]|\([^()]*\))*\)/g, '');
  assert.ok(selector.trim().startsWith(':where('));
  assert.ok(!/[A-Za-z.#\[]/.test(outside), 'something outside :where() adds specificity: ' + outside);
});

scenario('every kind of control that paints is covered, and only the self-styled ones are left out', () => {
  assert.ok(block.includes('select') && block.includes('textarea'));
  const excluded = new Set([...block.matchAll(/:not\(\[type=(\w+)\]\)/g)].map(m => m[1]));
  assert.deepStrictEqual([...excluded].sort(), ['button', 'checkbox', 'color', 'hidden', 'image', 'radio', 'range', 'reset', 'submit']);
  for (const painted of ['text', 'password', 'number', 'search', 'email', 'url', 'file', 'tel', 'date']) {
    assert.ok(!excluded.has(painted), painted + ' is excluded');
  }
});

scenario('the native control scheme follows the page in both palettes', () => {
  assert.ok(block.includes('color-scheme: dark'));
  assert.ok(/:where\(\.light\) :where\(select, textarea, input\)\s*\{\s*color-scheme: light/.test(FAMILY));
});

scenario('every page with controls loads the stylesheet that carries the baseline', () => {
  const pages = fs.readdirSync(path.join(ROOT, 'ui')).filter(f => f.endsWith('.html'));
  assert.ok(pages.length >= 7);
  let checked = 0;
  for (const page of pages) {
    const html = read('ui/' + page);
    if (!(/<(input|select|textarea)\b/.test(html) || /settings\.js/.test(html))) continue;
    checked++;
    assert.ok(/href="\/ui\/family\.css(\?[^"]*)?"/.test(html), page + ' has controls but does not load family.css');
  }
  assert.ok(checked >= 7, 'only ' + checked + ' pages were checked');
});

scenario('the dialog shares one control size on every page', () => {
  const m = /\.modal select, \.modal input\[type=text\], \.modal input\[type=number\], \.modal input\[type=password\] \{([^}]*)\}/.exec(FAMILY);
  assert.ok(m, 'no shared size rule');
  for (const d of ['font-size: 14px', 'padding: 6px 8px', 'border-radius: 6px']) assert.ok(m[1].includes(d), d);
});

if (!process.exitCode) console.log('form control baseline: ' + passed + ' scenarios passed');
