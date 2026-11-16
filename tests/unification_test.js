// The unification pass in the browser: navigation, the Remote Access entry, Settings order,
// button roles, the default palette, the matrix and the Accounts editor.
//
// Behaviour is run where it can be (the Remote Access tab, how a scan is composed); what is
// only about markup and style is asserted from the file with comments stripped first, so an
// explanatory comment naming the very selector under test cannot satisfy an assertion.
'use strict';
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {Node} = require('./_dom');

const ROOT = path.join(__dirname, '..');
const read = rel => fs.readFileSync(path.join(ROOT, rel), 'utf8').replace(/\r\n/g, '\n');
const tick = () => new Promise(r => setTimeout(r, 5));
const settle = async (n = 8) => { for (let i = 0; i < n; i++) await tick(); };
const noComments = text => text.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '').replace(/<!--[\s\S]*?-->/g, '');

let passed = 0;
async function scenario(name, fn) {
  try { await fn(); passed++; }
  catch (err) { console.error('FAILED: ' + name + '\n  ' + (err && err.stack || err)); process.exitCode = 1; }
}

const PAGES = ['index.html', 'matrix.html', 'wall.html', 'producer.html', 'manual.html', 'admin_users.html'];

// The text of one function, balanced.
function lift(source, name) {
  const at = source.search(new RegExp('(?:async\\s+)?function\\s+' + name + '\\s*\\('));
  assert.ok(at >= 0, name + ' not found');
  let depth = 0;
  for (let i = source.indexOf('{', at); i < source.length; i++) {
    if (source[i] === '{') depth++;
    else if (source[i] === '}' && --depth === 0) return source.slice(at, i + 1);
  }
  throw new Error('unbalanced ' + name);
}

(async () => {
  // =========================================================== main navigation
  for (const page of PAGES) {
    await scenario('navigation on ' + page + ' is text only, in the agreed order, with no decoration', () => {
      const html = noComments(read('ui/' + page));
      const tabs = [...html.matchAll(/<(?:a|span) class="nav-tab[^"]*"[^>]*>([\s\S]*?)<\/(?:a|span)>/g)].map(m => m[1]);
      assert.strictEqual(tabs.length, 4, page);
      const labels = tabs.map(t => t.replace(/<span id="vwBadge"[\s\S]*?<\/span>/, '').replace(/<[^>]+>/g, '').replace(/\s+/g, ' ').trim());
      assert.deepStrictEqual(labels, ['Device Manager', 'Matrix Routing', 'Production Studio', 'Video Wall'], page);
      // Not an emoji, not a symbol, not an image: the text is the whole of it.
      tabs.forEach(t => {
        assert.ok(!/[^\x00-\x7f]/.test(t), page + ' nav tab carries a non-ASCII glyph: ' + t.trim());
        assert.ok(!/<img|<svg|<i /.test(t), page + ' nav tab carries an image');
      });
      assert.ok(!/Display Groups/i.test(html.slice(html.indexOf('class="nav-tabs"'), html.indexOf('id="header_gear"') > 0 ? html.indexOf('id="header_gear"') : html.indexOf('id="gear"'))),
        'Display Groups is not a top-level tab');
    });
    await scenario(page + ' uses the same Settings gear markup', () => {
      const html = read('ui/' + page);
      const gear = html.match(/<button id="(?:header_gear|gear)" class="gear-btn"[^>]*>([\s\S]*?)<\/button>/);
      assert.ok(gear, page);
      assert.ok(/<svg width="20" height="20" viewBox="0 0 24 24"/.test(gear[1]), page + ' gear is not the shared icon');
      assert.ok(/aria-label="Settings"/.test(gear[0]), page + ' gear needs an accessible name');
    });
  }

  await scenario('Video Wall keeps its dynamic count badge, because it carries state', () => {
    const html = read('ui/index.html');
    assert.ok(/<span id="vwBadge" class="vw-nav-badge"/.test(html));
    assert.ok(/refreshVideoWallBadge/.test(html));
  });

  await scenario('the shared nav styling uses the same geometry as the family tabs and names no hue', () => {
    const css = noComments(read('ui/family.css'));
    const block = css.match(/\.nav-tabs\s*\{[^}]*\}\s*\.nav-tab\s*\{[^}]*\}/)[0];
    assert.ok(/padding:\s*8px 16px/.test(block));
    assert.ok(/font-size:\s*14px/.test(block) && /font-weight:\s*500/.test(block));
    assert.ok(!/#[0-9a-f]{3,8}\b|rgb\(|hsl\(/i.test(block), 'the nav block names a hue');
    assert.ok(!/background:\s*var\(--accent\)/.test(block), 'a selected tab is a tint with an edge, not an accent block');
    // Selected state: one rule, from tokens, a fill and a bottom edge.
    const selected = css.match(/\.nav-tab\[aria-current="page"\],\s*\.nav-tab\.active\s*\{[^}]*\}/)[0];
    assert.ok(/--selected-tint-strong/.test(selected) && /inset 0 -2px 0 0 var\(--selected-fill\)/.test(selected));
  });

  // ================================================= the Remote Access navigation entry
  function nav(session, opts = {}) {
    const tabs = new Node('div');
    ['Device Manager', 'Matrix Routing'].forEach(label => {
      const a = new Node('a'); a.className = 'nav-tab'; a.textContent = label; tabs.appendChild(a);
    });
    const header = new Node('div');
    const gear = new Node('button'); gear.id = 'header_gear'; header.appendChild(gear);
    const byId = () => tabs.querySelectorAll('a').find(n => n.id === 'nav_remote_access') || null;
    const doc = {readyState: 'complete', addEventListener() {},
      querySelector: sel => (sel === '.nav-tabs' ? tabs : (sel.includes('header_gear') ? gear : null)),
      getElementById: id => (id === 'nav_remote_access' ? byId() : null),
      createElement: t => new Node(t)};
    const live = {session};
    const sandbox = {document: doc, location: {pathname: opts.path || '/ui/index.html', replace() {}}, Promise, JSON,
      fetch: () => Promise.resolve({ok: true, json: () => Promise.resolve(live.session)}),
      window: {lumaSession: () => Promise.resolve(live.session), lumaForgetCsrf() {}, addEventListener() {}}};
    sandbox.window.window = sandbox.window;
    vm.createContext(sandbox);
    vm.runInContext(read('ui/adminnav.js'), sandbox);
    return {tabs, live, byId, refresh: () => sandbox.window.lumaRefreshNav()};
  }

  await scenario('the Remote Access tab appears for an administrator while remote access is on', async () => {
    const n = nav({role: 'admin', local: true, remote_enabled: true});
    await settle();
    const tab = n.byId();
    assert.ok(tab, 'tab present');
    assert.strictEqual(tab.getAttribute('href') || tab.href, '/admin/users');
    assert.strictEqual(tab.textContent, 'Remote Access');
    assert.ok(!tab.classList.contains('active'), 'not the current page here');
  });

  await scenario('there is no Remote Access tab while it is off: navigation that does nothing is hidden', async () => {
    const n = nav({role: 'admin', local: true, remote_enabled: false});
    await settle();
    assert.strictEqual(n.byId(), null);
    assert.strictEqual(n.tabs.children.length, 2);
  });

  await scenario('a restricted user is never offered it, even if the flag were true', async () => {
    const n = nav({role: 'user', local: false, username: 'alex', remote_enabled: true});
    await settle();
    assert.strictEqual(n.byId(), null);
  });

  await scenario('the tab follows Settings turning remote access on and off without a reload', async () => {
    const n = nav({role: 'admin', local: true, remote_enabled: false});
    await settle();
    assert.strictEqual(n.byId(), null);
    n.live.session = {role: 'admin', local: true, remote_enabled: true};
    await n.refresh(); await settle();
    assert.ok(n.byId());
    n.live.session = {role: 'admin', local: true, remote_enabled: false};
    await n.refresh(); await settle();
    assert.strictEqual(n.byId(), null);
  });

  await scenario('on the Remote Access page itself the tab is present and current, even with remote access off', async () => {
    const n = nav({role: 'admin', local: true, remote_enabled: false}, {path: '/admin/users'});
    await settle();
    const tab = n.byId();
    assert.ok(tab && tab.classList.contains('active'));
    assert.strictEqual(tab.getAttribute('aria-current'), 'page');
  });

  await scenario('Settings refreshes the navigation after it turns remote access on or off', () => {
    const script = noComments(read('ui/remote_settings.js'));
    assert.strictEqual((script.match(/window\.lumaRefreshNav\(\)/g) || []).length, 2);
  });

  // ============================================================ the Accounts editor
  await scenario('account editing is ordered: role, username, password, Direct Access, then what they may use', () => {
    const html = noComments(read('ui/admin_users.html'));
    const order = ['id="u_role"', 'id="u_name"', 'id="u_password"', 'id="direct_section"',
                   'id="assign_sources"', 'id="assign_displays"', 'id="assign_groups"'].map(k => html.indexOf(k));
    order.forEach(i => assert.ok(i > 0));
    assert.deepStrictEqual(order, [...order].sort((a, b) => a - b), 'sections are out of order');
  });

  await scenario('Direct Access is worded as the other product words it and offers copy, regenerate and off', () => {
    const html = noComments(read('ui/admin_users.html'));
    assert.ok(html.includes('Anyone with this link can use the controls assigned to this account.'));
    assert.ok(html.includes('Treat the link like a password.'));
    assert.ok(/id="direct_regen"[^>]*>Regenerate</.test(html));
    assert.ok(/id="direct_copy"[^>]*>Copy link</.test(html));
    assert.ok(/id="direct_enabled"/.test(html));
    const script = noComments(read('ui/admin_users.js'));
    assert.ok(!/Replace…/.test(script) && !/'Replace'/.test(script), 'old wording remains');
    assert.ok(/'regenerate'/.test(script) && /'disable'/.test(script) && /'enable'/.test(script));
  });

  await scenario('the Remote Access page holds Accounts, Device names, Display groups and Activity', () => {
    const html = noComments(read('ui/admin_users.html'));
    const tabs = [...html.matchAll(/role="tab"[^>]*>([^<]+)</g)].map(m => m[1]);
    assert.deepStrictEqual(tabs, ['Accounts', 'Device names', 'Display groups', 'Activity']);
    assert.ok(/<title>Remote Access - LumaSuite<\/title>/.test(html));
    assert.ok(/Administrators use the normal LumaSuite pages\. Users see only/.test(html));
  });

  await scenario('Activity offers retention days and a size limit, with the defaults stated', () => {
    const html = noComments(read('ui/admin_users.html'));
    assert.ok(/id="ret_days"[^>]*min="1"[^>]*max="365"/.test(html));
    assert.ok(/id="ret_mb"[^>]*min="1"[^>]*max="64"/.test(html));
    assert.ok(/defaults are\s+30 days and 4 MB/.test(html));
    const script = read('ui/admin_users.js');
    assert.ok(script.includes("'/api/admin/audit/retention'"));
  });

  // ==================================================================== Settings
  await scenario('Settings sections run: device communication, firmware, studio, appearance, remote access, backup, application', () => {
    const source = noComments(read('ui/settings.js'));
    const markup = source.slice(source.indexOf('function markup()'), source.indexOf('function injectMarkup'));
    const headings = [...markup.matchAll(/<h4>([^<]+)<\/h4>/g)].map(m => m[1]);
    assert.deepStrictEqual(headings, ['Device Communication', 'Firmware', 'Production Studio', 'Appearance', 'Application Information']);
    // Remote Access and Backup and Restore are injected at the extension point, between
    // Appearance and Application, in that order.
    const at = id => markup.indexOf(id);
    assert.ok(at('<h4>Appearance</h4>') < at('id="cfg_extension_point"'));
    assert.ok(at('id="cfg_extension_point"') < at('<h4>Application Information</h4>'));
    const remote = noComments(read('ui/remote_settings.js'));
    assert.ok(remote.indexOf("'Remote Access'") < remote.indexOf("'Backup and Restore'"));
    // Which section each control lives in.
    const sectionOf = id => { const i = at('id="' + id + '"'); return headings.filter(h => at('<h4>' + h + '</h4>') < i).pop(); };
    assert.strictEqual(sectionOf('cfg_username'), 'Device Communication');
    assert.strictEqual(sectionOf('cfg_firmware_path'), 'Firmware');
    assert.strictEqual(sectionOf('cfg_theme'), 'Appearance');
    assert.strictEqual(sectionOf('cfg_preset_choice'), 'Appearance');
    assert.strictEqual(sectionOf('cfg_app_version'), 'Application Information');
    assert.strictEqual(sectionOf('cfg_imagestream_path'), 'Production Studio');
  });

  await scenario('every control the old Settings had is still there', () => {
    const markup = read('ui/settings.js');
    ['cfg_app_version', 'cfg_update_check', 'cfg_theme', 'cfg_username', 'cfg_password', 'cfg_fallback_password',
     'cfg_ws_port', 'cfg_firmware_path', 'cfg_imagestream_path', 'cfg_logos_path', 'cfg_density_choice',
     'cfg_preset_choice', 'cfg_reset_appearance', 'cfg_user_guide', 'cfg_save', 'cfg_close'].forEach(id =>
      assert.strictEqual((markup.match(new RegExp('id="' + id + '"', 'g')) || []).length, 1, id));
  });

  // ======================================================================== buttons
  await scenario('Device Manager buttons carry a role, not a hue', () => {
    const html = noComments(read('ui/index.html'));
    const buttons = [...html.matchAll(/<button\b[^>]*>/g)].map(m => m[0]);
    buttons.forEach(b => assert.ok(!/style="[^"]*background:\s*#/.test(b), 'literal background on ' + b.slice(0, 80)));
    const role = id => (buttons.find(b => b.includes('id="' + id + '"')) || '');
    assert.ok(/class="btn"/.test(role('scan')), 'Scan is the primary action: no role class');
    assert.ok(/danger/.test(role('remove_sel')) && /danger/.test(role('clear_units')), 'destructive actions are danger');
    assert.ok(/danger/.test(role('reboot_sel')), 'Reboot Selected is destructive, as in Omni');
    ['stop_scan', 'password_sync_btn', 'ntp_sync_btn', 'slate_manager_btn', 'export_units', 'pw_pw'].forEach(id => {
      if (id !== 'pw_pw') assert.ok(/secondary/.test(role(id)), id + ' is secondary');
    });
    assert.ok(/icon-only/.test(role('refresh_files')) && /aria-label=/.test(role('refresh_files')));
    // The scan button's colour is no longer painted by script either.
    assert.ok(!/stop_scan"\)\.style\.background/.test(html));
  });

  await scenario('the shared roles exist and are built from semantic tokens only', () => {
    const css = noComments(read('ui/family.css'));
    for (const role of ['primary', 'secondary', 'success', 'warn', 'danger']) {
      const rule = css.match(new RegExp('\\.btn\\.' + role + '[^{]*\\{[^}]*\\}'));
      assert.ok(rule, role + ' role missing');
      // Pure black in a colour-mix() darkens a token's own edge; it is not a hue.
      assert.ok(!/#[0-9a-f]{3,8}\b|rgb\(|hsl\(/i.test(rule[0].replace(/#000000/g, '')), role + ' names a hue');
    }
    assert.ok(/--state-success/.test(css.match(/\.btn\.success[^{]*\{[^}]*\}/)[0]));
    assert.ok(/--state-warning/.test(css.match(/\.btn\.warn[^{]*\{[^}]*\}/)[0]));
    assert.ok(/--state-error/.test(css.match(/\.btn\.danger[^{]*\{[^}]*\}/)[0]));
    assert.ok(/--action-primary/.test(css.match(/\.btn\.primary[^{]*\{[^}]*\}/)[0]));
    assert.ok(/--control-disabled-opacity/.test(css), 'disabled is muted from a token');
  });

  await scenario('hover is a translucent veil, never filter: brightness()', () => {
    for (const file of ['ui/family.css', 'ui/matrix.css', 'ui/index.html']) {
      const text = noComments(read(file));
      assert.ok(!/:hover[^{]*\{[^}]*filter:\s*brightness/.test(text), file + ' still brightens on hover');
    }
  });

  // =================================================================== default palette
  await scenario('the Default preset is Hallway blue in both modes and means the same here as in OmniSuite', () => {
    const boot = read('ui/theme-boot.js');
    const preset = boot.match(/id: "default"[\s\S]*?\} \},/)[0];
    assert.ok(/hint: "Hallway blue"/.test(preset));
    const colours = preset.match(/colors: \{([^}]*)\}/)[1];
    const expected = {accent: '#1e90ff', selection: '#1e90ff', success: '#4ade80', warning: '#eab308',
                      error: '#e5534b', info: '#38bdf8', surface: '#4a5561'};
    for (const [key, value] of Object.entries(expected)) {
      assert.ok(new RegExp(key + ': "' + value + '"').test(colours), key + ' differs from the shared reference');
    }
    const css = noComments(read('ui/family.css'));
    assert.ok(/--action-primary:\s*#1e90ff/.test(css.slice(0, css.indexOf('html.light'))));
    assert.ok(!/--action-primary:/.test(css.slice(css.indexOf('html.light, body.light'), css.indexOf('html, body {'))),
      'light mode keeps the same accent');
  });

  await scenario('light and dark both define every surface and state the roles read', () => {
    const css = noComments(read('ui/family.css'));
    const dark = css.slice(css.indexOf(':root {'), css.indexOf('html.light, body.light'));
    const light = css.slice(css.indexOf('html.light, body.light'), css.indexOf('html, body {'));
    for (const token of ['--surface', '--surface-raised', '--surface-elevated', '--text', '--text-muted', '--edge',
                         '--state-success', '--state-warning', '--state-error', '--state-info']) {
      assert.ok(dark.includes(token + ':'), 'dark lacks ' + token);
      assert.ok(light.includes(token + ':'), 'light lacks ' + token);
    }
    // The aliases are restated in light mode so they track it (the light-mode trap).
    ['--bg', '--card', '--fg', '--accent', '--border'].forEach(a => assert.ok(light.includes(a + ':'), a));
  });

  await scenario('the presets are still selectable: ten palettes', () => {
    const boot = read('ui/theme-boot.js');
    assert.strictEqual((boot.match(/\{ id: "[a-z-]+", name: "/g) || []).length, 10);
  });

  // ================================================================= the routing matrix
  await scenario('the matrix and its filters are centred when they fit and scroll inside their frame when they do not', () => {
    const css = noComments(read('ui/matrix.css'));
    const area = css.match(/\.matrix-area\s*\{[^}]*\}/)[0];
    assert.ok(/display:\s*flex/.test(area) && /flex-direction:\s*column/.test(area) && /align-items:\s*center/.test(area));
    const wrap = css.match(/\.matrix-wrap\s*\{[^}]*\}/)[0];
    assert.ok(/width:\s*max-content/.test(wrap), 'it is as wide as its content');
    assert.ok(/max-width:\s*100%/.test(wrap), 'and never wider than the content area');
    assert.ok(/overflow:\s*auto/.test(wrap), 'so a large one scrolls instead of breaking the page');
    assert.ok(/margin:\s*0 auto/.test(wrap));
    assert.ok(!/max-width:\s*96vw/.test(wrap), 'the old viewport-based bound is gone');
    const filters = css.match(/\.matrix-filters\s*\{[^}]*\}/)[0];
    assert.ok(/justify-content:\s*center/.test(filters) && /flex-wrap:\s*wrap/.test(filters));
    const html = noComments(read('ui/matrix.html'));
    assert.ok(/<div class="matrix-filters">/.test(html));
    assert.ok(html.indexOf('class="matrix-wrap"') < html.indexOf('class="matrix-filters"'));
  });

  await scenario('the matrix offers AV, Video and Audio modes, AV by default, and says what a click does', () => {
    const html = noComments(read('ui/matrix.html'));
    const modes = [...html.matchAll(/class="modebtn[^"]*"[^>]*data-mode="(\w+)"[^>]*aria-pressed="(\w+)"[^>]*>([^<]+)</g)];
    assert.deepStrictEqual(modes.map(m => [m[1], m[2], m[3]]),
      [['av', 'true', 'AV (Audio + Video)'], ['video', 'false', 'Video'], ['audio', 'false', 'Audio']]);
    const script = noComments(read('ui/matrix.js'));
    assert.ok(/let routeMode = 'av'/.test(script));
    assert.ok(/const postData = \{ decoder_ip: targetIp, encoder_ip: encIp, mode: routeMode \}/.test(script));
    assert.ok(/aria-pressed/.test(lift(script, 'setMode')), 'a selected mode is set as state, not only painted');
    const css = noComments(read('ui/matrix.css'));
    assert.ok(/\.mode \.modebtn\.active,\s*\.mode \.modebtn\[aria-pressed="true"\]/.test(css));
  });

  await scenario('an audio-only click leaves the video ring where the display has it, and works on the selected cell', () => {
    const script = noComments(read('ui/matrix.js'));
    assert.ok(/if \(routeMode !== 'audio'\) applyRouteSelection\(targets, encIp\)/.test(script));
    assert.ok(/routeMode === 'audio' && e\.target\.classList[\s\S]{0,80}route-radio[\s\S]{0,80}preventDefault\(\)/.test(script));
    assert.ok(/class="dot/.test(lift(script, 'routeCellHtml')), 'the audio selection is drawn inside the video ring');
  });

  // ============================================================ scanning, composed
  await scenario('a scan is the adapter network plus the additional networks, each once, in one request', () => {
    const source = read('ui/index.html');
    const text = lift(source, 'composeScanTargets');
    const run = (adapter, extra) => {
      const ctx = {$: sel => ({value: sel === '#targets' ? extra : adapter}), Set, Array, String};
      vm.createContext(ctx);
      vm.runInContext(text, ctx);
      return ctx.composeScanTargets();
    };
    assert.strictEqual(run('192.0.2.1-254', ''), '192.0.2.1-254');
    assert.strictEqual(run('192.0.2.1-254', '198.51.100.0/24'), '192.0.2.1-254,198.51.100.0/24');
    assert.strictEqual(run('192.0.2.1-254', '198.51.100.0/24, 203.0.113.1-9;198.51.100.0/24'),
      '192.0.2.1-254,198.51.100.0/24,203.0.113.1-9');
    assert.strictEqual(run('', '198.51.100.0/24'), '198.51.100.0/24', 'additional networks alone');
    assert.strictEqual(run('192.0.2.1-254', '192.0.2.1-254'), '192.0.2.1-254', 'the same range is one range');
    assert.strictEqual(run('', ''), '');
  });

  await scenario('the scan control keeps the adapter picker, the explicit range, Scan and Stop', () => {
    const html = noComments(read('ui/index.html'));
    for (const id of ['adapters', 'targets', 'scan', 'stop_scan', 'clear_units', 'clear_targets', 'scan_status']) {
      assert.ok(html.includes('id="' + id + '"'), id);
    }
    // Labelled Targets, as in OmniSuite; what it does is explained in the tooltip and the manual.
    assert.ok(/<label for="targets" class="small">[\s\S]*?>Targets<\/span><\/label>/.test(html));
  });

  // ============================================================= clear units, firmware
  await scenario('opening Device Manager asks no question and covers nothing', () => {
    const html = noComments(read('ui/index.html'));
    assert.ok(!/Clear Units For Optimal Performance/.test(html));
    assert.ok(!/clear_units_prompt_seen/.test(html));
    assert.ok(!/check_firmware_sel|Check Local Firmware Files/.test(html));
    ['id="files"', 'id="upgrade_sel"', 'id="refresh_files"'].forEach(k => assert.ok(html.includes(k), k));
  });

  console.log(passed + ' scenarios passed' + (process.exitCode ? ' (with failures)' : ''));
})();
