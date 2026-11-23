/*
 * Blocking appearance bootstrap, loaded first in every page's <head> (no
 * defer/async) so theme/density/preset are applied to <html> before any
 * stylesheet-dependent render -- the first frame is correct instead of
 * flashing and correcting.
 *
 * Reads the consolidated "lumasuite.appearance" key when present, and
 * otherwise falls back to the legacy "dark" boolean-string key that
 * LumaSuite's existing per-page applyTheme()/toggleDark() functions have
 * always read and written. Those existing functions are unchanged and keep
 * working: they still read/write "dark" and still toggle .light/.dark on
 * <body> once it exists, which is a strict subset of what this bootstrap
 * already establishes on <html> (the bare ".light"/".dark" selectors used
 * by index.html/matrix.css/producer.html all match an ancestor <html>
 * element just as they match <body>, and CSS custom properties set on
 * <html> cascade to <body> regardless of which element carries the class).
 *
 * Colour presets (LUMA_PRESETS below): complete palettes, not a single hue,
 * adapted from the Hall Research application-family's actual reference
 * implementation (OmniSuite ui/appearance.js) -- reused only because
 * inspection confirmed they are pure presentation data (a preset name plus
 * seven hex colours), with no device/protocol logic attached. Two of
 * OmniSuite's nine semantic roles ("daisy chain" / "other switch", used for
 * its multi-switch topology view) are deliberately dropped as palette entries:
 * LumaSuite's multi-switch notice and its "Different switch" mark are drawn from
 * the info role, so every preset and both modes already apply to them.
 */
(function () {
  var LUMA_PRESETS = [
    { id: "default", name: "Default", hint: "Hallway blue",
      swatch: ["#1e90ff", "#4ade80", "#eab308", "#e5534b"],
      colors: { accent: "#1e90ff", selection: "#1e90ff", success: "#4ade80", warning: "#eab308", error: "#e5534b", info: "#38bdf8", surface: "#4a5561" } },
    { id: "slate", name: "Slate", hint: "Neutral corporate grey",
      swatch: ["#5b7186", "#4d9375", "#b58b3c", "#b8524f"],
      colors: { accent: "#5b7186", selection: "#7c94aa", success: "#4d9375", warning: "#b58b3c", error: "#b8524f", info: "#6b8fa8", surface: "#4a5561" } },
    { id: "cool", name: "Cool", hint: "Teal and slate",
      swatch: ["#0e9f9f", "#10b981", "#d9a441", "#e05252"],
      colors: { accent: "#0e9f9f", selection: "#14b8a6", success: "#10b981", warning: "#d9a441", error: "#e05252", info: "#38bdf8", surface: "#3f5560" } },
    { id: "ocean", name: "Ocean", hint: "Deep blue and cyan",
      swatch: ["#2563eb", "#06b6d4", "#e0a63a", "#dc4c4c"],
      colors: { accent: "#2563eb", selection: "#3b82f6", success: "#0891b2", warning: "#e0a63a", error: "#dc4c4c", info: "#06b6d4", surface: "#3c4f6b" } },
    { id: "forest", name: "Forest", hint: "Green and moss",
      swatch: ["#3f8f5f", "#5aa469", "#c99b3f", "#c0524a"],
      colors: { accent: "#3f8f5f", selection: "#5aa469", success: "#4c9a5a", warning: "#c99b3f", error: "#c0524a", info: "#5f9e8f", surface: "#4a5a4d" } },
    { id: "warm", name: "Warm", hint: "Amber and clay",
      swatch: ["#c07a3e", "#7fa05a", "#d99a5b", "#c0524a"],
      colors: { accent: "#c07a3e", selection: "#d09a63", success: "#7fa05a", warning: "#d99a5b", error: "#c0524a", info: "#a8875f", surface: "#665549" } },
    { id: "plum", name: "Plum", hint: "Violet and aubergine",
      swatch: ["#8b5cb8", "#5f9e7d", "#d0a04e", "#c0526b"],
      colors: { accent: "#8b5cb8", selection: "#a77fcc", success: "#5f9e7d", warning: "#d0a04e", error: "#c0526b", info: "#8f7bb5", surface: "#5b4a66" } },
    { id: "rose", name: "Rose", hint: "Rose and warm neutrals",
      swatch: ["#b4587a", "#5f9e7d", "#d99a5b", "#c0526b"],
      colors: { accent: "#b4587a", selection: "#c88ba6", success: "#5f9e7d", warning: "#d99a5b", error: "#c0526b", info: "#8f7bb5", surface: "#6b5560" } },
    { id: "soft", name: "Soft", hint: "Muted, low contrast",
      swatch: ["#6b8fb5", "#7fa88c", "#c4a875", "#b58080"],
      colors: { accent: "#6b8fb5", selection: "#8fa9c4", success: "#7fa88c", warning: "#c4a875", error: "#b58080", info: "#8fa9c4", surface: "#5a6570" } },
    { id: "contrast", name: "High contrast", hint: "Maximum legibility",
      swatch: ["#0a84ff", "#00b34a", "#ffb000", "#ff3b30"],
      colors: { accent: "#0a84ff", selection: "#0a84ff", success: "#00b34a", warning: "#ffb000", error: "#ff3b30", info: "#00c2d1", surface: "#9aa7b4" } }
  ];
  // Back-compat aliases (--accent, --ok, --video -- see family.css's own
  // "Back-compat aliases" block) are listed here alongside the semantic
  // token they mean the same thing as. Several pages predate the family
  // system and still declare these alias names directly in their own
  // embedded <style> (e.g. index.html/producer.html/matrix.css's own
  // ":root{--accent:#1e90ff...}"), at the same CSS specificity as family
  // .css's ":root{--accent: var(--action-primary)}". Dark mode is the bare
  // ":root" in both places, so that's a specificity tie broken by source
  // order -- and the page's own later <style> block wins, freezing the
  // alias at its hardcoded default no matter what preset is chosen. Light
  // mode happens to escape this only because family.css wrote its override
  // as the compound "html.light, body.light" (one specificity notch above
  // a page's bare ".light"), not because anything here treats it
  // differently. Setting these aliases directly via inline style -- which
  // always wins over any selector, in every mode -- removes the tie
  // instead of relying on winning it.
  var PRESET_TOKENS = {
    accent: ["--action-primary", "--accent", "--video"],
    selection: ["--state-selection"],
    success: ["--state-success", "--ok"],
    warning: ["--state-warning"],
    error: ["--state-error", "--danger"],
    info: ["--state-info"],
    surface: ["--edge", "--border"]
  };
  var LEGACY_PRESET_IDS = { "hall-blue": "default" };

  function presetById(id) {
    var mapped = LEGACY_PRESET_IDS[id] || id;
    for (var i = 0; i < LUMA_PRESETS.length; i++) {
      if (LUMA_PRESETS[i].id === mapped) return LUMA_PRESETS[i];
    }
    return LUMA_PRESETS[0];
  }

  // Relative luminance / contrast (WCAG formula) -- generic accessibility
  // math, not application logic, so a preset's accent is never assumed to
  // read against white.
  function relativeLuminance(hex) {
    var m = /^#([0-9a-fA-F]{6})$/.exec(hex);
    if (!m) return 0;
    var value = m[1];
    function channel(n) {
      var c = parseInt(value.substr(n, 2), 16) / 255;
      return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
    }
    return 0.2126 * channel(0) + 0.7152 * channel(2) + 0.0722 * channel(4);
  }
  function contrastRatio(a, b) {
    var la = relativeLuminance(a), lb = relativeLuminance(b);
    return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
  }
  function readableTextOn(hex) {
    return contrastRatio(hex, "#ffffff") >= contrastRatio(hex, "#101418") ? "#ffffff" : "#101418";
  }

  // The operator's light-mode page background. A generic appearance control (nothing in it is
  // specific to a product): one colour, validated as a colour before it reaches a custom property,
  // applied to both <html> and <body> because the light palette is declared on both and a value set
  // on only one would be shadowed on the other. The surfaces around it are derived in family.css
  // from `--light-bg`, so panels and headers keep their separation from the page.
  //
  // `data-light-bg` is only present when the choice differs from the default, so an installation that
  // never touched this renders exactly the palette it always did.
  var LIGHT_BG_DEFAULT = "#f7f8fb";
  function normalizeLightBg(value) {
    if (typeof value !== "string") return null;
    var hex = value.trim().toLowerCase();
    if (!/^#(?:[0-9a-f]{3}|[0-9a-f]{6})$/.test(hex)) return null;
    if (hex.length === 4) hex = "#" + hex.charAt(1) + hex.charAt(1) + hex.charAt(2) + hex.charAt(2) + hex.charAt(3) + hex.charAt(3);
    return hex;
  }
  function applyLightBackground(value) {
    var colour = normalizeLightBg(value);
    var html = document.documentElement;
    var hosts = [html, document.body].filter(Boolean);
    if (!colour || colour === LIGHT_BG_DEFAULT) {
      hosts.forEach(function (host) { host.style.removeProperty("--light-bg"); });
      html.removeAttribute("data-light-bg");
      return LIGHT_BG_DEFAULT;
    }
    hosts.forEach(function (host) { host.style.setProperty("--light-bg", colour); });
    html.setAttribute("data-light-bg", colour);
    return colour;
  }

  function applyPresetColors(id) {
    var preset = presetById(id);
    var hosts = [document.documentElement, document.body].filter(Boolean);
    hosts.forEach(function (host) {
      Object.keys(PRESET_TOKENS).forEach(function (role) {
        var value = preset.colors[role];
        if (!value) return;
        PRESET_TOKENS[role].forEach(function (token) {
          host.style.setProperty(token, value);
        });
        if (role !== "surface") {
          host.style.setProperty("--on-" + role, readableTextOn(value));
        }
      });
    });
    return preset;
  }

  try {
    var html = document.documentElement;
    var raw = null;
    try { raw = localStorage.getItem("lumasuite.appearance"); } catch (e) {}
    var appearance = null;
    if (raw) {
      try { appearance = JSON.parse(raw); } catch (e) { appearance = null; }
    }

    var theme = appearance && appearance.theme;
    if (theme !== "light" && theme !== "dark") {
      var legacyDark = null;
      try { legacyDark = localStorage.getItem("dark"); } catch (e) {}
      theme = legacyDark === "false" ? "light" : "dark";
    }

    var density = (appearance && appearance.density) || "classic";
    if (density !== "classic" && density !== "compact") density = "classic";

    var presetId = presetById((appearance && appearance.preset) || "default").id;
    var lightBg = normalizeLightBg(appearance && appearance.lightBackground) || LIGHT_BG_DEFAULT;

    html.classList.toggle("light", theme === "light");
    html.classList.toggle("dark", theme !== "light");
    html.setAttribute("data-density", density);
    html.setAttribute("data-preset", presetId);
    applyPresetColors(presetId);
    applyLightBackground(lightBg);

    window.__lumaAppearance = { theme: theme, density: density, preset: presetId, lightBackground: lightBg };
    window.__lumaApplyLightBackground = applyLightBackground;
    window.__lumaNormalizeLightBackground = normalizeLightBg;
    window.LUMA_PRESETS = LUMA_PRESETS;
    window.__lumaApplyPresetColors = applyPresetColors;
    window.__lumaReadableTextOn = readableTextOn;

    // Some existing per-page stylesheets (producer.html) key component rules
    // to "body.light"/"body.dark" specifically rather than the bare ".light"
    // class. <body> does not exist yet while this script runs in <head>, so
    // pages call this as the first statement inside <body> to mirror the
    // already-decided theme there with no observable flash.
    window.__lumaMirrorBody = function () {
      try {
        document.body.classList.toggle("light", theme === "light");
        document.body.classList.toggle("dark", theme !== "light");
        applyPresetColors(presetId);
        applyLightBackground(lightBg);
      } catch (e) {}
    };
  } catch (e) {
    /* Storage unavailable (private mode, etc.) -- fall back to CSS defaults
       (dark, classic, default preset), never throw and block the page. */
  }
})();
