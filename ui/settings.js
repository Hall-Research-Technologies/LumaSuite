/*
 * Shared LumaSuite Settings module (Hall Research application family).
 * One implementation, injected into every page, reading and writing the
 * single /api/config endpoint plus the client-side appearance store -- so
 * the state an operator sees is identical on every screen.
 *
 * Layout/structure follows the Hall family's actual reference
 * Configuration dialog (OmniSuite ui/settings.js/settings.css) directly --
 * a flat list of rows (no section headers except Appearance), Theme as a
 * labelled Dark/Light select, editable file-chooser path inputs, and the
 * same footer arrangement -- populated only with the settings LumaSuite
 * itself has. No OmniStream device/protocol behavior is reproduced here;
 * this only reuses shared, general-purpose UI: markup, CSS and the
 * appearance system's presentation data (see ui/theme-boot.js).
 *
 * Depends on ui/dialogs.js (toast, lumaConfirm) and ui/theme-boot.js
 * (window.__lumaAppearance, window.LUMA_PRESETS) being loaded first.
 */
(function () {
  "use strict";

  var PASSWORD_MASK = "••••••••";
  var RELEASES_URL = "https://github.com/Hall-Research-Technologies/LumaSuite/releases";
  var BACKDROP_ID = "cfg_backdrop";

  function isAllowedPassword(value) {
    var str = String(value || "");
    return str.length >= 6 && /^[A-Za-z0-9!@#$%^&*]+$/.test(str);
  }
  window.isAllowedPassword = window.isAllowedPassword || isAllowedPassword;

  function el(id) { return document.getElementById(id); }

  // Exposed globally: the Password Sync modal (unchanged in this pass --
  // see AGENTS.md/PROJECT.md, Password Sync functional redesign is
  // deferred) uses the same reveal-toggle pattern for its own password
  // fields and previously had its own copy of this helper.
  function wirePasswordToggle(buttonId, inputId, revealCallback) {
    var btn = el(buttonId), input = el(inputId);
    if (!btn || !input) return;
    btn.addEventListener("click", function (e) {
      e.preventDefault();
      var show = input.type === "password";
      input.type = show ? "text" : "password";
      btn.textContent = show ? "🙈" : "👁️";
      if (show && typeof revealCallback === "function") {
        Promise.resolve(revealCallback(input)).catch(function () {});
      }
    });
  }
  window.wirePasswordToggle = window.wirePasswordToggle || wirePasswordToggle;

  // ---- Markup --------------------------------------------------------
  //
  // Flat modal-row list, matching the reference Configuration dialog: no
  // section dividers except Appearance. Only rows LumaSuite genuinely
  // needs are present -- no Password Hash (an implementation detail, not
  // an operator-facing setting; the backend still computes/returns it,
  // see PROJECT.md), no Parallel Uploads/Concurrency (accepted by no
  // backend code path in LumaSuite -- decorative, so it is not shown),
  // no Timeout (LumaSuite's CONFIG["TIMEOUT"] is real but is not
  // currently a settable field via /api/config, and adding one is a
  // backend change out of scope for a UI correction pass).

  function markup() {
    return (
      '<div id="' + BACKDROP_ID + '" class="modal-backdrop">' +
        '<div class="modal">' +
          "<h3>Settings</h3>" +
          '<div class="settings-section first">' +
            "<h4>Device Communication</h4>" +
          '<div class="modal-row">' +
            '<label for="cfg_username">Username</label>' +
            '<input id="cfg_username" type="text" autocomplete="username"/>' +
          "</div>" +
          '<div class="modal-row">' +
            '<label for="cfg_password">Password</label>' +
            '<div style="display:flex;gap:6px;flex:1;">' +
              '<input id="cfg_password" type="password" autocomplete="current-password" style="flex:1;"/>' +
              '<button id="cfg_pw_toggle" class="btn small secondary" type="button" aria-label="Show password">👁️</button>' +
            "</div>" +
          "</div>" +
          '<div class="modal-row" style="align-items:flex-start;">' +
            '<label for="cfg_fallback_password">Fallback Password</label>' +
            '<div style="flex:1;">' +
              '<div style="display:flex;gap:6px;">' +
                '<input id="cfg_fallback_password" type="password" autocomplete="off" style="flex:1;"/>' +
                '<button id="cfg_fallback_pw_toggle" class="btn small secondary" type="button" aria-label="Show fallback password">👁️</button>' +
              "</div>" +
              '<div class="small" style="color:var(--text-muted);margin-top:4px;">' +
                "Tried only if Password is genuinely rejected. Leave blank to disable." +
              "</div>" +
            "</div>" +
          "</div>" +
          '<div class="modal-row">' +
            '<label for="cfg_ws_port">WS Port</label>' +
            '<select id="cfg_ws_port" style="width:140px;">' +
              '<option value="80">80 (ws/http)</option>' +
              '<option value="443">443 (wss/https)</option>' +
            "</select>" +
          "</div>" +
          "</div>" +
          '<div class="settings-section">' +
            "<h4>Firmware</h4>" +
          '<div class="modal-row">' +
            '<label for="cfg_firmware_path">Firmware Path</label>' +
            '<div class="file-chooser">' +
              '<button id="cfg_firmware_browse" class="file-chooser-btn" type="button">Choose Folder</button>' +
              '<input id="cfg_firmware_path" type="text" class="file-chooser-path" placeholder="No folder selected" aria-label="Firmware folder"/>' +
            "</div>" +
          "</div>" +
          '<div class="modal-row" style="align-items:flex-start;">' +
            "<label>Availability</label>" +
            '<div style="flex:1;min-width:0;">' +
              '<div id="cfg_fw_status" class="update-status muted">Checking Hall Research…</div>' +
              '<div id="cfg_fw_detail" class="small" style="color:var(--text-muted);margin-top:4px;"></div>' +
              '<div class="update-actions" style="margin-top:6px;">' +
                '<button id="cfg_fw_check" class="btn small secondary" type="button">Check Now</button>' +
              "</div>" +
            "</div>" +
          "</div>" +
          "</div>" +
          '<div class="settings-section">' +
            "<h4>Production Studio</h4>" +
          '<div class="modal-row">' +
            '<label for="cfg_imagestream_path">Static Image Path</label>' +
            '<div class="file-chooser">' +
              '<button id="cfg_imagestream_browse" class="file-chooser-btn" type="button">Choose Folder</button>' +
              '<input id="cfg_imagestream_path" type="text" class="file-chooser-path" placeholder="No folder selected" aria-label="Static image folder"/>' +
            "</div>" +
          "</div>" +
          '<div class="modal-row">' +
            '<label for="cfg_logos_path">Logo Path</label>' +
            '<div class="file-chooser">' +
              '<button id="cfg_logos_browse" class="file-chooser-btn" type="button">Choose Folder</button>' +
              '<input id="cfg_logos_path" type="text" class="file-chooser-path" placeholder="No folder selected" aria-label="Logo folder"/>' +
            "</div>" +
          "</div>" +
          "</div>" +
          '<div class="settings-section">' +
            "<h4>Appearance</h4>" +
          '<div class="modal-row">' +
            "<label>Theme</label>" +
            '<select id="cfg_theme" style="flex:1;">' +
              '<option value="dark">Dark</option>' +
              '<option value="light">Light</option>' +
            "</select>" +
          "</div>" +
            '<div class="modal-row" style="align-items:flex-start;">' +
              "<label>Layout</label>" +
              '<div style="flex:1 1 auto;">' +
                '<div id="cfg_density_choice" class="template-choice"></div>' +
                '<div class="small" style="margin-top:6px;color:var(--text-muted);">' +
                  "Light and dark are chosen separately above and apply to every layout." +
                "</div>" +
              "</div>" +
            "</div>" +
            '<div class="modal-row" style="align-items:flex-start;">' +
              "<label>Colour preset</label>" +
              '<div style="flex:1 1 auto;"><div id="cfg_preset_choice" class="preset-choice"></div></div>' +
            "</div>" +
            '<div class="modal-row" style="align-items:flex-start;">' +
              "<label>Light background</label>" +
              '<div style="flex:1 1 auto;"><div id="cfg_light_background" class="light-bg-choice"></div></div>' +
            "</div>" +
          "</div>" +
          '<div id="cfg_extension_point"></div>' +
          '<div class="settings-section">' +
            "<h4>Application Information</h4>" +
          '<div class="modal-row" style="align-items:flex-start;">' +
            "<label>Version</label>" +
            '<div style="flex:1 1 auto;min-width:0;">' +
              '<input id="cfg_app_version" type="text" readonly style="width:100%;"/>' +
              '<div id="cfg_update_status" class="update-status muted">Checking for updates...</div>' +
              '<div class="update-actions">' +
                '<button id="cfg_update_check" class="btn small secondary" type="button">Check Now</button>' +
                '<a id="cfg_release_link" class="btn small secondary" href="' + RELEASES_URL + '" target="_blank" rel="noopener noreferrer">Releases</a>' +
                '<a id="cfg_asset_link" class="btn small" target="_blank" rel="noopener noreferrer" hidden>Download</a>' +
              "</div>" +
            "</div>" +
          "</div>" +
          '<div class="modal-row cfg-about">' +
            "<label>About</label>" +
            '<div class="cfg-about-text">' +
              "<div>Copyright &copy; 2026 Hall Research Technologies LLC</div>" +
              '<div class="cfg-about-links">' +
                '<a href="/license" target="_blank" rel="noopener noreferrer">License</a>' +
                '<a href="/ui/manual.html" target="_blank" rel="noopener noreferrer">User Guide</a>' +
              "</div>" +
            "</div>" +
          "</div>" +
          "</div>" +
          '<div id="cfg_error" class="modal-error"></div>' +
          '<div class="modal-buttons">' +
            '<button id="cfg_user_guide" class="btn secondary" type="button" style="margin-right:auto;">User Guide</button>' +
            '<button id="cfg_reset_appearance" class="btn secondary" type="button" ' +
              'title="Restore the default layout, palette and colours. Devices, routes and settings are untouched.">' +
              "Reset Appearance" +
            "</button>" +
            '<button id="cfg_close" class="btn secondary" type="button">Close</button>' +
            '<button id="cfg_save" class="btn" type="button">Save</button>' +
          "</div>" +
        "</div>" +
      "</div>" +
      '<div id="cfg_browser_backdrop" class="modal-backdrop" style="z-index:20001;">' +
        '<div class="modal" style="width:640px;">' +
          "<h3>Select Folder</h3>" +
          '<div class="modal-row">' +
            '<button id="cfg_browser_up" class="btn small secondary" type="button">Up</button>' +
            '<input id="cfg_browser_path" type="text" style="flex:1" readonly />' +
            '<button id="cfg_browser_select" class="btn small" type="button">Select</button>' +
            '<button id="cfg_browser_cancel" class="btn small secondary" type="button">Cancel</button>' +
          "</div>" +
          '<div id="cfg_browser_list" style="max-height:320px;overflow:auto;border:1px solid var(--edge);padding:8px;background:var(--surface-elevated);border-radius:8px;"></div>' +
        "</div>" +
      "</div>"
    );
  }

  function injectMarkup() {
    if (el(BACKDROP_ID)) return;
    var wrap = document.createElement("div");
    wrap.innerHTML = markup();
    while (wrap.firstChild) document.body.appendChild(wrap.firstChild);
  }

  // ---- Appearance (layout/theme/preset) -------------------------------

  function currentAppearance() {
    return window.__lumaAppearance || { theme: "dark", density: "classic", preset: "default" };
  }

  function presetList() {
    return window.LUMA_PRESETS || [{ id: "default", name: "Default", hint: "Hallway blue", swatch: ["#1e90ff"] }];
  }

  function persistAppearance(appearance) {
    window.__lumaAppearance = appearance;
    try {
      localStorage.setItem("lumasuite.appearance", JSON.stringify(appearance));
      // Keep the legacy "dark" key in sync: every pre-existing
      // applyTheme()/toggleDark() call site across the app reads only
      // this key, and must keep seeing a consistent value.
      localStorage.setItem("dark", appearance.theme === "dark" ? "true" : "false");
    } catch (e) {}
    var html = document.documentElement;
    html.classList.toggle("light", appearance.theme === "light");
    html.classList.toggle("dark", appearance.theme !== "light");
    html.setAttribute("data-density", appearance.density);
    html.setAttribute("data-preset", appearance.preset);
    document.body.classList.toggle("light", appearance.theme === "light");
    document.body.classList.toggle("dark", appearance.theme !== "light");
    if (typeof window.__lumaApplyPresetColors === "function") {
      window.__lumaApplyPresetColors(appearance.preset);
    }
    if (typeof window.__lumaApplyLightBackground === "function") {
      window.__lumaApplyLightBackground(appearance.lightBackground);
    }
  }

  function reflectAppearanceControls() {
    var a = currentAppearance();
    var densityChoice = el("cfg_density_choice");
    if (densityChoice) {
      Array.prototype.forEach.call(densityChoice.querySelectorAll("button"), function (b) {
        b.setAttribute("aria-pressed", String(b.getAttribute("data-density") === a.density));
      });
    }
    var presetChoice = el("cfg_preset_choice");
    if (presetChoice) {
      Array.prototype.forEach.call(presetChoice.querySelectorAll("button"), function (b) {
        b.setAttribute("aria-pressed", String(b.getAttribute("data-preset") === a.preset));
      });
    }
    var themeSelect = el("cfg_theme");
    if (themeSelect) themeSelect.value = a.theme === "light" ? "light" : "dark";
    reflectLightBackground();
  }

  // ---- Light background ------------------------------------------------
  // One picker and its hex value, saved with the rest of the appearance. It sets the light-mode page
  // background; the surfaces around it are derived in CSS. A value that is not a colour is refused with
  // a sentence and never reaches a custom property.
  function lightBackgroundNote(a) {
    return a.theme === "light" ? "Applies to Light theme." : "Saved for Light theme. Dark is unaffected.";
  }

  function renderLightBackground() {
    var host = el("cfg_light_background");
    if (!host) return;
    var a = currentAppearance();
    var value = a.lightBackground || "#f7f8fb";
    host.innerHTML =
      '<input type="color" id="cfg_light_bg_picker" aria-label="Light mode background">' +
      '<input type="text" id="cfg_light_bg_hex" class="color-hex" spellcheck="false" aria-label="Light mode background hex value">' +
      '<span class="light-bg-note" id="cfg_light_bg_note"></span>';
    el("cfg_light_bg_picker").value = value;
    el("cfg_light_bg_hex").value = value;
    el("cfg_light_bg_note").textContent = lightBackgroundNote(a);
  }

  function reflectLightBackground() {
    var a = currentAppearance();
    var picker = el("cfg_light_bg_picker"), hex = el("cfg_light_bg_hex"), note = el("cfg_light_bg_note");
    if (picker) picker.value = a.lightBackground || "#f7f8fb";
    if (hex) hex.value = a.lightBackground || "#f7f8fb";
    if (note) note.textContent = lightBackgroundNote(a);
  }

  function commitLightBackground(raw) {
    var normalize = window.__lumaNormalizeLightBackground;
    var colour = typeof normalize === "function" ? normalize(raw) : null;
    var note = el("cfg_light_bg_note");
    if (!colour) {
      if (note) note.textContent = "Not a colour. Use a hex value such as #eef2f5.";
      if (typeof window.__lumaApplyLightBackground === "function") {
        window.__lumaApplyLightBackground(currentAppearance().lightBackground);
      }
      return;
    }
    persistAppearance(Object.assign({}, currentAppearance(), { lightBackground: colour }));
    reflectAppearanceControls();
  }

  function wireLightBackground() {
    var host = el("cfg_light_background");
    if (!host) return;
    host.addEventListener("input", function (e) {
      // Previewing as the picker moves costs nothing; nothing is saved until the choice is made.
      if (e.target && e.target.id === "cfg_light_bg_picker" && typeof window.__lumaApplyLightBackground === "function") {
        window.__lumaApplyLightBackground(e.target.value);
      }
    });
    host.addEventListener("change", function (e) {
      if (e.target && (e.target.id === "cfg_light_bg_picker" || e.target.id === "cfg_light_bg_hex")) {
        commitLightBackground(e.target.value.trim());
      }
    });
  }

  function renderDensityChoice() {
    var host = el("cfg_density_choice");
    if (!host) return;
    var densities = [
      { id: "classic", name: "Classic", hint: "The familiar LumaSuite layout" },
      { id: "compact", name: "Compact", hint: "Denser tables, quieter controls" }
    ];
    host.innerHTML = densities.map(function (d) {
      return '<button type="button" data-density="' + d.id + '" aria-pressed="false">' +
        '<span class="template-name">' + d.name + '</span>' +
        '<span class="template-hint">' + d.hint + '</span>' +
        '</button>';
    }).join("");
  }

  function renderPresetChoice() {
    var host = el("cfg_preset_choice");
    if (!host) return;
    host.innerHTML = presetList().map(function (preset) {
      var swatches = (preset.swatch || []).map(function (colour) {
        return '<i style="background:' + colour + ';"></i>';
      }).join("");
      return '<button type="button" data-preset="' + preset.id + '" title="' + preset.hint + '" aria-pressed="false">' +
        '<span class="preset-swatches">' + swatches + '</span>' +
        '<span class="preset-name">' + preset.name + '</span>' +
        '</button>';
    }).join("");
  }

  function wireAppearanceControls() {
    var densityChoice = el("cfg_density_choice");
    if (densityChoice) {
      densityChoice.addEventListener("click", function (e) {
        var btn = e.target.closest("button[data-density]");
        if (!btn) return;
        var a = currentAppearance();
        persistAppearance(Object.assign({}, a, { density: btn.getAttribute("data-density") }));
        reflectAppearanceControls();
      });
    }
    var presetChoice = el("cfg_preset_choice");
    if (presetChoice) {
      presetChoice.addEventListener("click", function (e) {
        var btn = e.target.closest("button[data-preset]");
        if (!btn) return;
        var a = currentAppearance();
        persistAppearance(Object.assign({}, a, { preset: btn.getAttribute("data-preset") }));
        reflectAppearanceControls();
      });
      // Previewing on hover costs nothing and is the quickest way to see a
      // palette; leaving restores whatever is actually selected.
      presetChoice.addEventListener("mouseover", function (e) {
        var btn = e.target.closest("button[data-preset]");
        if (!btn || typeof window.__lumaApplyPresetColors !== "function") return;
        window.__lumaApplyPresetColors(btn.getAttribute("data-preset"));
      });
      presetChoice.addEventListener("mouseleave", function () {
        if (typeof window.__lumaApplyPresetColors === "function") {
          window.__lumaApplyPresetColors(currentAppearance().preset);
        }
      });
    }
    var themeSelect = el("cfg_theme");
    if (themeSelect) {
      themeSelect.addEventListener("change", function () {
        var a = currentAppearance();
        persistAppearance(Object.assign({}, a, { theme: themeSelect.value === "light" ? "light" : "dark" }));
        reflectAppearanceControls();
      });
    }
    var resetBtn = el("cfg_reset_appearance");
    if (resetBtn) {
      resetBtn.addEventListener("click", function () {
        try { localStorage.removeItem("lumasuite.appearance"); } catch (e) {}
        persistAppearance({ theme: "dark", density: "classic", preset: "default", lightBackground: "#f7f8fb" });
        reflectAppearanceControls();
        window.toast && window.toast("Appearance reset to defaults", "ok");
      });
    }
    var guideBtn = el("cfg_user_guide");
    if (guideBtn) {
      guideBtn.addEventListener("click", function (e) {
        e.preventDefault();
        window.open("/ui/manual.html", "_blank", "noopener");
      });
    }
  }

  // ---- Version / update check ----------------------------------------

  function renderUpdateState(data) {
    var statusEl = el("cfg_update_status");
    var assetLink = el("cfg_asset_link");
    var releaseLink = el("cfg_release_link");
    if (!statusEl) return;
    if (releaseLink && data && data.html_url) releaseLink.href = data.html_url;

    if (!data || !data.ok) {
      statusEl.textContent = "Unable to check for updates.";
      statusEl.className = "update-status muted";
      if (assetLink) assetLink.hidden = true;
      return;
    }
    if (data.state === "update_available") {
      statusEl.textContent = "UPDATE AVAILABLE — " + data.latest_version;
      statusEl.className = "update-status update-available";
      if (assetLink) {
        if (data.asset_url) { assetLink.href = data.asset_url; assetLink.hidden = false; }
        else assetLink.hidden = true;
      }
      if (!data.asset_url && data.package_message) {
        // The release is real but there is nothing to download for this computer. Say so, and leave
        // the release page link, rather than offering a build that would not run.
        statusEl.textContent += ". " + data.package_message;
      }
      try {
        var ack = localStorage.getItem("lumasuite.update_ack_version");
        if (ack !== data.latest_version) setGearAlert(true);
      } catch (e) {}
    } else if (data.state === "current") {
      statusEl.textContent = "No update available — " + data.installed_version + " is current.";
      statusEl.className = "update-status ok";
      if (assetLink) assetLink.hidden = true;
    } else {
      statusEl.textContent = "Unable to check for updates.";
      statusEl.className = "update-status muted";
      if (assetLink) assetLink.hidden = true;
    }
  }

  function runUpdateCheck(force, quiet) {
    if (!quiet) {
      var statusEl = el("cfg_update_status");
      if (statusEl) { statusEl.textContent = "Checking for updates..."; statusEl.className = "update-status muted"; }
    }
    return fetch("/api/update_check" + (force ? "?force=1" : ""), { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!quiet) renderUpdateState(data);
        else if (data && data.ok && data.state === "update_available") {
          try {
            var ack = localStorage.getItem("lumasuite.update_ack_version");
            if (ack !== data.latest_version) setGearAlert(true);
          } catch (e) {}
        }
        return data;
      })
      .catch(function () {
        if (!quiet) renderUpdateState(null);
        return null;
      });
  }

  function setGearAlert(on) {
    Array.prototype.forEach.call(document.querySelectorAll(".gear-btn"), function (g) {
      g.classList.toggle("update-alert", !!on);
    });
  }

  function acknowledgeUpdate() {
    fetch("/api/update_check", { cache: "no-store" }).then(function (r) { return r.json(); }).then(function (data) {
      if (data && data.ok && data.latest_version) {
        try { localStorage.setItem("lumasuite.update_ack_version", data.latest_version); } catch (e) {}
      }
      setGearAlert(false);
    }).catch(function () {});
  }

  // ---- Folder browser --------------------------------------------------

  function openServerFolderBrowser(startPath, targetInputId) {
    var backdrop = el("cfg_browser_backdrop");
    var list = el("cfg_browser_list");
    var pathInput = el("cfg_browser_path");
    var selectBtn = el("cfg_browser_select");
    var upBtn = el("cfg_browser_up");
    if (!backdrop || !list || !pathInput) return;
    var current = startPath && startPath.trim() ? startPath : "";

    function load(p) {
      var url = "/api/list_dir" + (p ? ("?path=" + encodeURIComponent(p)) : "");
      list.textContent = "Loading...";
      fetch(url).then(function (r) { return r.json(); }).then(function (j) {
        if (!j.ok) { list.textContent = "Error: " + (j.error || ""); return; }
        current = j.path || p || "";
        pathInput.value = current;
        list.innerHTML = "";
        (j.entries || []).forEach(function (ent) {
          if (!ent.is_dir) return;
          var row = document.createElement("div");
          row.style.padding = "6px 8px";
          row.style.cursor = "pointer";
          row.textContent = ent.name;
          row.onclick = function () {
            var base = (current || "").replace(/\\/g, "/");
            var next = base.endsWith("/") || base === "" ? base + ent.name : base + "/" + ent.name;
            load(next);
          };
          list.appendChild(row);
        });
      }).catch(function (e) { list.textContent = "Error: " + String(e); });
    }

    upBtn.onclick = function () {
      if (!current) return;
      var tmp = String(current).replace(/\\/g, "/");
      if (tmp === "/" || tmp === "") return;
      var parts = tmp.split("/").filter(function (p) { return p.length > 0; });
      if (parts.length <= 1) { load("/"); return; }
      parts.pop();
      load("/" + parts.join("/"));
    };
    el("cfg_browser_cancel").onclick = function () { backdrop.classList.remove("open"); };
    selectBtn.onclick = function () {
      var targetInput = el(targetInputId);
      if (targetInput) targetInput.value = pathInput.value;
      backdrop.classList.remove("open");
    };
    backdrop.classList.add("open");
    load(current || "");
  }

  // ---- Open / populate / save ------------------------------------------

  function populateFromConfig() {
    return fetch("/api/config", { cache: "no-store" }).then(function (r) { return r.json(); }).then(function (cfg) {
      if (!cfg) return;
      var pwInput = el("cfg_password"); if (pwInput) pwInput.value = PASSWORD_MASK;
      var fbInput = el("cfg_fallback_password"); if (fbInput) fbInput.value = cfg.has_fallback_password ? PASSWORD_MASK : "";
      var userEl = el("cfg_username"); if (userEl && cfg.username) userEl.value = cfg.username;
      if (cfg.ws_port) { var wsEl = el("cfg_ws_port"); if (wsEl) wsEl.value = String(cfg.ws_port); }
      if (cfg.firmware_path) el("cfg_firmware_path").value = cfg.firmware_path;
      if (cfg.imagestream_path) el("cfg_imagestream_path").value = cfg.imagestream_path;
      if (cfg.logos_path) el("cfg_logos_path").value = cfg.logos_path;
    }).catch(function () {});
  }

  function updateAppVersionField() {
    return fetch("/api/app_version", { cache: "no-store" }).then(function (r) { return r.json(); }).then(function (d) {
      var version = d && d.ok && d.version ? d.version : null;
      var el2 = el("cfg_app_version");
      if (el2) el2.value = version ? ("v" + version) : "v?.?.?";
      if (version) window.__lumaAppVersion = version;
      return version;
    }).catch(function () {
      var el2 = el("cfg_app_version");
      if (el2) el2.value = "v?.?.?";
      return null;
    });
  }

  // The firmware availability summary lives in its own module and reads only what is already
  // known; it is loaded the first time Settings opens and refreshes itself on every open after.
  function loadFirmwareSummary() {
    if (window.lumaFirmwareSummary) return;
    var script = document.createElement("script");
    script.src = "/ui/firmware-summary.js?v=1";
    script.onload = function () { if (window.lumaFirmwareSummary) window.lumaFirmwareSummary.refresh(); };
    document.head.appendChild(script);
  }

  function open() {
    var backdrop = el(BACKDROP_ID);
    if (!backdrop) return;
    backdrop.classList.add("open");
    var pwInput = el("cfg_password"); if (pwInput) pwInput.type = "password";
    var pwToggle = el("cfg_pw_toggle"); if (pwToggle) pwToggle.textContent = "👁️";
    var fbInput = el("cfg_fallback_password"); if (fbInput) fbInput.type = "password";
    var fbToggle = el("cfg_fallback_pw_toggle"); if (fbToggle) fbToggle.textContent = "👁️";
    var err = el("cfg_error"); if (err) { err.style.display = "none"; err.textContent = ""; }
    reflectAppearanceControls();
    updateAppVersionField();
    populateFromConfig();
    runUpdateCheck(false, false);
    acknowledgeUpdate();
    loadFirmwareSummary();
    // Sections built by other modules (Remote Access) refresh their own state.
    try { document.dispatchEvent(new CustomEvent("luma:settings-open")); } catch (e) {}
  }

  function close() {
    var backdrop = el(BACKDROP_ID);
    if (backdrop) backdrop.classList.remove("open");
  }

  function save() {
    var passwordValue = el("cfg_password").value;
    var passwordChanged = passwordValue && passwordValue !== PASSWORD_MASK;
    var errDiv = el("cfg_error");
    if (passwordChanged && !isAllowedPassword(passwordValue)) {
      if (errDiv) {
        errDiv.textContent = "Password must be at least 6 characters and may only include A-Z, a-z, 0-9, and !@#$%^&*";
        errDiv.style.display = "block";
      }
      return;
    }

    var fallbackInput = el("cfg_fallback_password");
    var fallbackValue = fallbackInput ? fallbackInput.value : "";
    var fallbackChanged = fallbackValue !== PASSWORD_MASK;
    // Unlike Password, an empty Fallback Password is a valid choice: it
    // means "no Fallback Password configured" (the feature is disabled
    // for devices whose Primary is rejected).
    if (fallbackChanged && fallbackValue && !isAllowedPassword(fallbackValue)) {
      if (errDiv) {
        errDiv.textContent = "Fallback Password must be at least 6 characters and may only include A-Z, a-z, 0-9, and !@#$%^&*";
        errDiv.style.display = "block";
      }
      return;
    }

    var payload = {
      username: (el("cfg_username") && el("cfg_username").value) || "admin",
      ws_port: parseInt((el("cfg_ws_port") && el("cfg_ws_port").value) || "80", 10),
      logos_path: el("cfg_logos_path").value,
      firmware_path: el("cfg_firmware_path").value,
      imagestream_path: el("cfg_imagestream_path").value
    };
    if (passwordChanged) payload.password = passwordValue;
    if (fallbackChanged) payload.fallback_password = fallbackValue;
    if (errDiv) { errDiv.style.display = "none"; errDiv.textContent = ""; }

    fetch("/api/config", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) })
      .then(function (r) {
        return r.json().catch(function () { return null; }).then(function (j) { return { ok: r.ok, body: j }; });
      })
      .then(function (result) {
        if (!result.ok) {
          var msg = "Save failed";
          var j = result.body;
          if (j && j.error === "invalid_paths" && Array.isArray(j.details)) {
            msg = "Invalid paths: " + j.details.map(function (d) { return d.key + ": " + d.path; }).join("; ");
          } else if (j && j.error) {
            msg = "Error: " + j.error;
          }
          if (errDiv) { errDiv.textContent = msg; errDiv.style.display = "block"; }
          else window.toast && window.toast(msg, "error");
          return;
        }
        try {
          if (typeof appConfig !== "undefined" && appConfig && typeof appConfig === "object") {
            if (passwordChanged) appConfig.password = payload.password;
            if (fallbackChanged) appConfig.fallback_password = payload.fallback_password;
            appConfig.ws_port = payload.ws_port;
          }
        } catch (e) {}
        close();
        window.toast && window.toast("Settings saved", "ok");
      })
      .catch(function (e) {
        var msg = "Network error: " + (e && e.message ? e.message : String(e));
        if (errDiv) { errDiv.textContent = msg; errDiv.style.display = "block"; }
        else window.toast && window.toast(msg, "error");
      });
  }

  // ---- Wire-up -----------------------------------------------------

  function wireGear() {
    var gears = document.querySelectorAll("#header_gear, #gear, [data-luma-gear]");
    Array.prototype.forEach.call(gears, function (g) {
      g.classList.add("gear-btn");
      if (!g.title) g.title = "Settings";
      if (!g.getAttribute("aria-label")) g.setAttribute("aria-label", "Settings");
      g.addEventListener("click", open);
    });
  }

  function init() {
    injectMarkup();
    renderDensityChoice();
    renderPresetChoice();
    renderLightBackground();
    wireGear();
    wireAppearanceControls();
    wireLightBackground();
    reflectAppearanceControls();

    wirePasswordToggle("cfg_pw_toggle", "cfg_password", function (inp) {
      return fetch("/api/config?include_password=1", { cache: "no-store" }).then(function (r) { return r.json(); }).then(function (cfg2) {
        if (cfg2 && typeof cfg2.password === "string") inp.value = cfg2.password;
      });
    });
    wirePasswordToggle("cfg_fallback_pw_toggle", "cfg_fallback_password", function (inp) {
      return fetch("/api/config?include_password=1", { cache: "no-store" }).then(function (r) { return r.json(); }).then(function (cfg2) {
        if (cfg2 && typeof cfg2.fallback_password === "string") inp.value = cfg2.fallback_password;
      });
    });

    var closeBtn = el("cfg_close"); if (closeBtn) closeBtn.addEventListener("click", close);
    var backdrop = el(BACKDROP_ID);
    if (backdrop) backdrop.addEventListener("click", function (e) { if (e.target === backdrop) close(); });
    var saveBtn = el("cfg_save"); if (saveBtn) saveBtn.addEventListener("click", save);

    var fbtn = el("cfg_firmware_browse");
    if (fbtn) fbtn.addEventListener("click", function () { openServerFolderBrowser(el("cfg_firmware_path").value, "cfg_firmware_path"); });
    var ibtn = el("cfg_imagestream_browse");
    if (ibtn) ibtn.addEventListener("click", function () { openServerFolderBrowser(el("cfg_imagestream_path").value, "cfg_imagestream_path"); });
    var lbtn = el("cfg_logos_browse");
    if (lbtn) lbtn.addEventListener("click", function () { openServerFolderBrowser(el("cfg_logos_path").value, "cfg_logos_path"); });

    var checkBtn = el("cfg_update_check");
    if (checkBtn) checkBtn.addEventListener("click", function () { runUpdateCheck(true, false); });

    // One check per page load (not a timer) so the gear can show the
    // attention pulse without requiring the operator to open Settings
    // first. This fires once, on load, never repeats.
    updateAppVersionField().then(function () {
      runUpdateCheck(false, true);
    });

    // The Remote Access section is built by its own module into the extension
    // point above. Loaded once, here, so every page that has Settings has it.
    if (!document.getElementById("luma_remote_settings_script")) {
      var rs = document.createElement("script");
      rs.id = "luma_remote_settings_script";
      rs.src = "/ui/remote_settings.js";
      document.body.appendChild(rs);
    }
    // The local application port (the launcher's listener, not Remote Access), local computer only.
    if (!document.getElementById("luma_port_settings_script")) {
      var ps = document.createElement("script");
      ps.id = "luma_port_settings_script";
      ps.src = "/ui/port_settings.js";
      document.body.appendChild(ps);
    }

    window.lumaSettings = { open: open, close: close, isAllowedPassword: isAllowedPassword, renderUpdateState: renderUpdateState };
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
