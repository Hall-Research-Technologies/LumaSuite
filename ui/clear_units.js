/*
 * Clear Units, in one place.
 *
 * Both pages that offer it (Device Manager and Remote Access > Device names) call
 * window.lumaClearUnits.run(). It asks the server what Remote Access depends on, asks the
 * operator in the shared dialog, and sends one request. The decision about what is removed
 * is the server's: this module only describes it, so a changed page cannot clear an
 * assigned unit.
 *
 *   Remote Access off  -> the ordinary confirmation; everything is cleared.
 *   Remote Access on   -> Cancel / View Assignments / Clear Unassigned Units. View
 *                         Assignments shows what is keeping each unit and then returns to
 *                         the question. There is no shortcut that turns Remote Access off.
 *
 * Remove Selected is the same decision for a chosen few: the server keeps every selected unit
 * Remote Access depends on, removes the rest, and says which it kept and why. That flow lives
 * here too (removeSelected) so it can be driven and measured without a page.
 *
 * Native dialogs are never used. If the shared dialog is missing the action is refused,
 * because the wording is the point of asking.
 */
(function () {
  "use strict";
  if (window.lumaClearUnits) return;

  var STYLE_ID = "lumaClearUnitsStyle";
  var MODAL_ID = "lumaAssignmentsBackdrop";

  function esc(value) {
    return String(value == null ? "" : value);
  }

  function plural(n, one, many) {
    return n + " " + (n === 1 ? one : many);
  }

  // The one-line summary used everywhere an assignment count is shown:
  // "Unassigned", "1 User", "2 Users \u00b7 1 Group \u00b7 1 Wall".
  function summaryText(record) {
    record = record || {};
    var parts = [];
    var users = (record.users || []).length;
    var groups = (record.groups || []).length;
    var walls = (record.walls || []).length;
    if (users) parts.push(plural(users, "User", "Users"));
    if (groups) parts.push(plural(groups, "Group", "Groups"));
    if (walls) parts.push(plural(walls, "Wall", "Walls"));
    return parts.length ? parts.join(" \u00b7 ") : "Unassigned";
  }

  function ensureStyle() {
    if (document.getElementById(STYLE_ID)) return;
    var style = document.createElement("style");
    style.id = STYLE_ID;
    style.textContent =
      "#" + MODAL_ID + "{position:fixed;inset:0;z-index:59000;display:flex;overflow:auto;" +
        "background:var(--backdrop-veil);padding:16px}" +
      "#" + MODAL_ID + "[hidden]{display:none}" +
      "#" + MODAL_ID + " .assign-card{margin:auto;width:min(960px,100%);background:var(--surface-raised);" +
        "color:var(--text);border:1px solid var(--edge);border-radius:var(--panel-radius);padding:18px 20px;" +
        "box-shadow:0 12px 40px rgba(0,0,0,.35)}" +
      "#" + MODAL_ID + " h3{margin:0 0 6px}" +
      "#" + MODAL_ID + " .assign-note{margin:0 0 12px;color:var(--text-muted)}" +
      "#" + MODAL_ID + " table{width:100%;border-collapse:collapse;font-size:.92em}" +
      "#" + MODAL_ID + " th,#" + MODAL_ID + " td{text-align:left;padding:6px 8px;vertical-align:top;" +
        "border-bottom:1px solid var(--edge)}" +
      "#" + MODAL_ID + " .assign-actions{display:flex;justify-content:flex-end;margin-top:14px}" +
      "#" + MODAL_ID + " .assign-empty{padding:10px 0;color:var(--text-muted)}";
    document.head.appendChild(style);
  }

  function make(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text) node.textContent = text;
    if (tag === "button") node.type = "button";
    return node;
  }

  function cell(row, text) {
    var td = document.createElement("td");
    td.textContent = esc(text);
    row.appendChild(td);
    return td;
  }

  var DEFAULT_TITLE = "Units kept while Remote Access is on";
  var DEFAULT_NOTE = "Each unit below is in use by Remote Access, so Clear Units keeps it. Remove the " +
    "assignment, or turn Remote Access off in Settings, to clear it.";

  // The View Assignments modal. Resolves when the operator closes it. `options.title` and
  // `options.note` let another action (Remove Selected) say the same thing in its own words.
  function showAssignments(units, options) {
    options = options || {};
    ensureStyle();
    var backdrop = document.getElementById(MODAL_ID);
    if (!backdrop) {
      backdrop = document.createElement("div");
      backdrop.id = MODAL_ID;
      backdrop.hidden = true;
      var card = make("div", "assign-card");
      card.setAttribute("role", "dialog");
      card.setAttribute("aria-modal", "true");
      card.setAttribute("aria-labelledby", "lumaAssignmentsTitle");
      var title = make("h3", "", DEFAULT_TITLE);
      title.id = "lumaAssignmentsTitle";
      card.appendChild(title);
      card.appendChild(make("p", "assign-note", DEFAULT_NOTE));
      card.appendChild(make("div", "assign-body"));
      var actions = make("div", "assign-actions");
      actions.appendChild(make("button", "btn assign-close", "Close"));
      card.appendChild(actions);
      backdrop.appendChild(card);
      document.body.appendChild(backdrop);
    }
    // Said again on every opening: a previous caller's wording must not outlive its call.
    backdrop.querySelector("h3").textContent = options.title || DEFAULT_TITLE;
    backdrop.querySelector(".assign-note").textContent = options.note || DEFAULT_NOTE;
    var body = backdrop.querySelector(".assign-body");
    body.textContent = "";
    if (!units.length) {
      var empty = document.createElement("div");
      empty.className = "assign-empty";
      empty.textContent = "No unit is assigned to a user, group or saved video wall.";
      body.appendChild(empty);
    } else {
      var table = document.createElement("table");
      var head = document.createElement("thead");
      var headRow = document.createElement("tr");
      ["Device", "Type", "Friendly name", "Device name", "Assignments", "Users", "Display Groups",
       "Saved Video Walls", "Wall access"].forEach(function (label) {
        var th = document.createElement("th");
        th.textContent = label;
        headRow.appendChild(th);
      });
      head.appendChild(headRow);
      table.appendChild(head);
      var tbody = document.createElement("tbody");
      units.forEach(function (unit) {
        var row = document.createElement("tr");
        cell(row, unit.name);
        cell(row, unit.type);
        cell(row, unit.friendly_name || "\u2014");
        cell(row, unit.device_name);
        cell(row, summaryText(unit));
        var users = (unit.users || []).map(function (name) {
          return (unit.direct_access || []).indexOf(name) >= 0 ? name + " (Direct Access)" : name;
        });
        cell(row, users.join(", ") || "\u2014");
        cell(row, (unit.groups || []).join(", ") || "\u2014");
        // Why a wall holds this unit ("Member of wall X", "Source of wall X"), and who
        // reaches that wall: a wall grant is not a grant of its members.
        cell(row, (unit.wall_reasons && unit.wall_reasons.length ? unit.wall_reasons : unit.walls || [])
          .join(", ") || "\u2014");
        cell(row, (unit.wall_access || []).join(", ") || "\u2014");
        tbody.appendChild(row);
      });
      table.appendChild(tbody);
      body.appendChild(table);
    }
    var opener = document.activeElement;
    return new Promise(function (resolve) {
      var close = backdrop.querySelector(".assign-close");
      function done() {
        backdrop.hidden = true;
        close.removeEventListener("click", done);
        document.removeEventListener("keydown", onKey, true);
        backdrop.removeEventListener("click", onBackdrop);
        if (opener && opener.focus) { try { opener.focus(); } catch (e) { /* ignore */ } }
        resolve();
      }
      function onKey(event) {
        if (event.key === "Escape") { event.preventDefault(); done(); }
      }
      function onBackdrop(event) { if (event.target === backdrop) done(); }
      close.addEventListener("click", done);
      document.addEventListener("keydown", onKey, true);
      backdrop.addEventListener("click", onBackdrop);
      backdrop.hidden = false;
      window.setTimeout(function () { try { close.focus(); } catch (e) { /* ignore */ } }, 0);
    });
  }

  var inFlight = false;

  /*
   * run({api, onDone, onError}) -> Promise<result|null>
   *   api(path, {method, body}) -> Promise<{ok, body}>   the page's own request helper
   *   onDone(body)   the server's answer to a clear that happened
   *   onError(text)  a refusal or failure, in words
   */
  function run(options) {
    options = options || {};
    var api = options.api;
    var onDone = options.onDone || function () {};
    var onError = options.onError || function () {};
    if (!window.lumaConfirm) { onError("The confirmation dialog is not available."); return Promise.resolve(null); }
    // One at a time: a second click while the first dialog is open is the same decision.
    if (inFlight) return Promise.resolve(null);
    inFlight = true;

    function release() { inFlight = false; }

    return api("/api/clear_units/assignments", {method: "GET"}).then(function (view) {
      var info = view && view.body ? view.body : {};
      if (!view || !view.ok || info.ok === false) {
        onError((info && info.error) || "Could not read what Remote Access is using.");
        return null;
      }
      if (!info.remote_access) {
        return window.lumaConfirm({
          title: "Clear discovered units?",
          message: "This removes all currently discovered units from the application and removes their " +
            "assignments from users and display groups. Devices can be rediscovered by scanning again.",
          confirmText: "Clear Units", cancelText: "Cancel", danger: true
        }).then(function (yes) { return yes === true ? send() : null; });
      }
      function ask() {
        var kept = info.protected | 0;
        var message = "Remote Access is enabled. Unassigned discovered units will be removed. Units " +
          "assigned to users, groups, or other Remote Access resources will be kept. To remove assigned " +
          "units, disable Remote Access in Settings first.";
        var question = {
          title: "Clear discovered units?",
          message: message,
          summary: [
            {label: "Will be removed", value: plural(info.clearable | 0, "unit", "units")},
            {label: "Will be kept", value: plural(kept, "assigned unit", "assigned units")}
          ],
          confirmText: "Clear Unassigned Units", cancelText: "Cancel", danger: true
        };
        if (kept > 0) question.secondary = {text: "View Assignments", value: "assignments"};
        return window.lumaConfirm(question).then(function (answer) {
          if (answer === "assignments") {
            return showAssignments(info.units || []).then(ask);
          }
          return answer === true ? send() : null;
        });
      }
      return ask();
    }).then(function (value) { release(); return value; },
            function (error) { release(); onError(String(error && error.message || error)); return null; });

    function send() {
      return api("/api/clear_units", {method: "POST", body: {}}).then(function (r) {
        if (!r || !r.ok) {
          onError((r && r.body && r.body.error) || "That did not work.");
          return null;
        }
        onDone(r.body);
        return r.body;
      });
    }
  }

  var removing = false;

  /*
   * removeSelected({ips, api, onDone, onError}) -> Promise<result|null>
   *
   * Asks what Remote Access depends on only to word the question truthfully, then sends one
   * request. The server decides which selected units go. A unit it kept is shown with what
   * holds it, whether the answer was a partial removal or a refusal of all of them.
   */
  function removeSelected(options) {
    options = options || {};
    var api = options.api;
    var ips = options.ips || [];
    var onDone = options.onDone || function () {};
    var onError = options.onError || function () {};
    if (!ips.length) { onError("Select at least one unit."); return Promise.resolve(null); }
    if (!window.lumaConfirm) { onError("The confirmation dialog is not available."); return Promise.resolve(null); }
    // Taken before the question opens, so two clicks are one decision.
    if (removing) return Promise.resolve(null);
    removing = true;
    function release() { removing = false; }
    var count = ips.length;

    return api("/api/clear_units/assignments", {method: "GET"}).then(function (view) {
      var info = view && view.body ? view.body : {};
      if (!view || !view.ok || info.ok === false) {
        onError((info && info.error) || "Could not read what Remote Access is using.");
        return null;
      }
      var protectedNote = !!info.remote_access;
      return window.lumaConfirm({
        title: "Remove selected units?",
        message: protectedNote
          ? "Remote Access is enabled. Of the " + plural(count, "selected unit", "selected units") +
            ", those assigned to users, groups, or saved video walls will be kept; the others will be " +
            "removed. To remove assigned units, disable Remote Access in Settings first."
          : "This removes " + plural(count, "selected unit", "selected units") + " from the application " +
            "and removes their assignments from users and display groups. Units can be rediscovered by " +
            "scanning again.",
        confirmText: protectedNote ? "Remove Unassigned Selected" : "Remove",
        cancelText: "Cancel", danger: true
      }).then(function (yes) {
        if (yes !== true) return null;
        return api("/api/remove_units", {method: "POST", body: {ips: ips}}).then(function (r) {
          var body = (r && r.body) || {};
          var kept = body.kept || [];
          function afterwards(value) { return kept.length ? showAssignments(kept, {
            title: "Selected units kept while Remote Access is on",
            note: "Remote Access is using each unit below, so it was not removed. Remove the " +
              "assignment, or turn Remote Access off in Settings, to remove it."
          }).then(function () { return value; }) : value; }
          if (!r || !r.ok) {
            onError(body.error || "That did not work.");
            return afterwards(null);
          }
          onDone(body);
          return afterwards(body);
        });
      });
    }).then(function (value) { release(); return value; },
            function (error) { release(); onError(String(error && error.message || error)); return null; });
  }

  // Past-tense summary for the status line / toast.
  function describe(result) {
    result = result || {};
    var cleared = result.cleared | 0;
    var kept = result.kept | 0;
    if (result.scope === "unassigned") {
      return "Cleared " + plural(cleared, "unassigned unit", "unassigned units") +
        (kept ? "; kept " + plural(kept, "assigned unit", "assigned units") + "." : ".");
    }
    return "Cleared " + plural(cleared, "unit", "units") + ".";
  }

  // Past-tense summary of a Remove Selected answer.
  function describeRemoval(result) {
    result = result || {};
    var removed = result.removed | 0;
    var kept = result.protected | 0;
    return "Removed " + plural(removed, "unit", "units") +
      (kept ? "; kept " + plural(kept, "assigned unit", "assigned units") + "." : ".");
  }

  window.lumaClearUnits = {run: run, describe: describe, describeRemoval: describeRemoval,
                           removeSelected: removeSelected, summaryText: summaryText,
                           showAssignments: showAssignments};
})();
