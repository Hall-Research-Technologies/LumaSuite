"""Mutations for the routing-interface choice and the restricted User's Matrix.

Each entry breaks one predicate the feature depends on and names the suites that must
notice. They live beside `mutate_security.py` (which appends them to its own list) so the
two files never need editing in the same place. The entry format is the harness's:

    (id, file, anchor, replacement, what it breaks, suites that must notice)

An anchor must match exactly once, in an LF file.
"""
import os
import sys

PY = sys.executable
BS = chr(92)


def py(file, *selectors):
    return ("python:" + file + (":" + ",".join(selectors) if selectors else ""),
            [PY, os.path.join("tests", file)] + list(selectors))


def node(file):
    return ("node:" + file, ["node", os.path.join("tests", file)])


MATRIX = py("test_user_matrix.py")
PAGE = node("user_matrix_page_test.js")
FOCUS = node("user_focus_test.js")
NL = chr(10)
# route_multi and route_check carry near-identical grant checks; an anchor inside either has to begin
# in text only that one has. route_multi's follows its "walls are not available" refusal, route_check's
# follows its own request-shape refusal.
WALLS_REFUSAL = ('error": "Video walls are not available."}), 404' + NL + NL +
                 '        def _check(account):' + NL)
CHECK_REFUSAL = ('                or (not display_ids and not group_ids)):' + NL +
                 '            return jsonify({"ok": False, "error": "source, displays and groups required"}), 400' + NL + NL +
                 '        def _check(account):' + NL)
CHECK = py("test_route_states.py", "RouteCheckAuthorisationTests", "RouteCheckSecondLineTests")

MATRIX_MUTATIONS = [
    # ---- which interface an account holds ---------------------------------------------------
    ("iface.default-matrix-on", "luma_users.py",
     '_flag(raw.get("matrix_routing"), False))',
     '_flag(raw.get("matrix_routing"), True))',
     "an account stored before the setting existed is given the Matrix",
     [py("test_user_matrix.py", "InterfacePermissionTests")]),
    ("iface.neither-falls-back-to-matrix", "luma_users.py",
     "        if not (self.standard_routing or self.matrix_routing):" + chr(10) +
     "            self.standard_routing = True",
     "        if not (self.standard_routing or self.matrix_routing):" + chr(10) +
     "            self.matrix_routing = True",
     "a record holding neither interface is read as Matrix-only instead of Standard",
     [py("test_user_matrix.py", "InterfacePermissionTests")]),
    ("iface.flag-not-a-boolean-believed", "luma_users.py",
     "    return value if isinstance(value, bool) else default",
     "    return bool(value) if value is not None else default",
     "a string or a number is accepted as an interface choice",
     [py("test_user_matrix.py", "InterfacePermissionTests")]),
    ("iface.round-trip-drops-flags", "luma_users.py",
     '                "standard_routing": self.standard_routing,' + chr(10) +
     '                "matrix_routing": self.matrix_routing}',
     '                }',
     "the choice is not stored, so it neither persists nor reaches the revision digest",
     [py("test_user_matrix.py", "InterfacePermissionTests", "InterfaceEnforcementTests")]),
    ("iface.unknown-name-permitted", "luma_users.py",
     "        return False" + chr(10) + chr(10) + "    def interfaces(self) -> dict:",
     "        return True" + chr(10) + chr(10) + "    def interfaces(self) -> dict:",
     "an interface name nobody defined is permitted",
     [py("test_user_matrix.py", "InterfacePermissionTests")]),
    ("iface.neither-accepted-by-the-admin-api", "luma_remote_api.py",
     '        if (isinstance(raw, dict) and raw.get("standard_routing") is False' + chr(10) +
     '                and raw.get("matrix_routing") is False):',
     "        if False:",
     "an account can be saved with no routing interface at all",
     [py("test_user_matrix.py", "AdminControlTests")]),
    ("iface.change-not-audited", "luma_users.py",
     "                if before.may_use_interface(name) != now_on:",
     "                if False:",
     "enabling or disabling an interface leaves no entry in the activity log",
     [py("test_user_matrix.py", "AdminControlTests")]),

    # ---- the interface is checked on every request ------------------------------------------
    ("iface.route-multi-grant-unchecked", "luma_remote_api.py",
     WALLS_REFUSAL + '            if not account.permissions.may_use_interface(interface):' + chr(10) +
     '                return "interface_not_permitted"',
     WALLS_REFUSAL + '            if False:' + chr(10) +
     '                return "interface_not_permitted"',
     "routing through route_multi no longer needs the interface grant",
     [py("test_user_matrix.py", "InterfaceEnforcementTests", "AuthorisationTests")]),
    ("iface.matrix-endpoint-open", "luma_remote_api.py",
     '                and not account.permissions.may_use_interface("matrix")):',
     '                and False):',
     "an account without the Matrix can read it",
     [py("test_user_matrix.py", "InterfaceEnforcementTests", "DirectAccessTests")]),
    ("iface.single-route-grant-unchecked", "luma_remote_api.py",
     '            if not account.permissions.may_use_interface("standard"):' + chr(10) +
     '                return "interface_not_permitted"' + chr(10) +
     '            if not account.permissions.may_use_source(source_id):' + chr(10) +
     '                return "source_not_permitted"' + chr(10) +
     '            if not account.permissions.may_control_destination(destination_id):',
     '            if not account.permissions.may_use_source(source_id):' + chr(10) +
     '                return "source_not_permitted"' + chr(10) +
     '            if not account.permissions.may_control_destination(destination_id):',
     "a Matrix-only account can use the single-display Standard route",
     [py("test_user_matrix.py", "InterfaceEnforcementTests")]),
    ("iface.group-route-grant-unchecked", "luma_remote_api.py",
     '            if not account.permissions.may_use_interface("standard"):' + chr(10) +
     '                return "interface_not_permitted"' + chr(10) +
     '            if not account.permissions.may_use_source(source_id):' + chr(10) +
     '                return "source_not_permitted"' + chr(10) +
     '            if not account.permissions.may_control_group(group_id):',
     '            if not account.permissions.may_use_source(source_id):' + chr(10) +
     '                return "source_not_permitted"' + chr(10) +
     '            if not account.permissions.may_control_group(group_id):',
     "a Matrix-only account can use the group Standard route",
     [py("test_user_matrix.py", "InterfaceEnforcementTests")]),
    ("iface.video-only-from-standard", "luma_remote_api.py",
     '        if mode != luma_user_matrix.MODE_AV and interface != "matrix":',
     "        if False:",
     "video-only and audio-only can be asked for through the Standard interface",
     [py("test_user_matrix.py", "InterfaceEnforcementTests")]),
    ("iface.matrix-route-audited-as-standard", "luma_remote_api.py",
     '        store.audit("route_matrix" if interface == "matrix" else "route_multi",',
     '        store.audit("route_multi",',
     "a Matrix route is recorded as an ordinary one",
     [py("test_user_matrix.py", "RoutingTests")]),
    ("iface.matrix-endpoint-public", "luma_access.py",
     '    "/api/user/matrix": Access.USER,',
     '    "/api/user/matrix": Access.PUBLIC,',
     "the matrix endpoint is classified public",
     [py("test_access_fence.py", "RouteClassificationFenceTests")]),

    # ---- the projection is the account's own grants -----------------------------------------
    ("matrix.source-grant-ignored", "luma_user_matrix.py",
     "        return True if admin else perms.may_use_source(identity)",
     "        return True",
     "every source becomes a column for every account",
     [py("test_user_matrix.py", "ProjectionTests", "LiveChangeTests")]),
    ("matrix.display-grant-ignored", "luma_user_matrix.py",
     "        return True if admin else perms.may_control_destination(identity)",
     "        return True",
     "every display becomes a row for every account",
     [py("test_user_matrix.py", "ProjectionTests")]),
    ("matrix.group-grant-ignored", "luma_user_matrix.py",
     '        if not admin and not perms.may_control_group(group["id"]):',
     "        if False:",
     "every display group becomes a row for every account",
     [py("test_user_matrix.py", "ProjectionTests", "LiveChangeTests")]),
    ("matrix.wall-grant-ignored", "luma_user_matrix.py",
     "            if not admin and not perms.may_control_wall(wid):",
     "            if False:",
     "every saved wall becomes a row for every account",
     [py("test_user_matrix.py", "ProjectionTests", "LiveChangeTests")]),
    ("matrix.unpermitted-source-named", "luma_user_matrix.py",
     "    return source if may_use(source) else OTHER",
     "    return source",
     "a display showing a source the account may not use is reported as showing it",
     [py("test_user_matrix.py", "CellStateTests")]),
    ("matrix.audio-follows-video-stream", "luma_user_matrix.py",
     "        audio_of[identity] = _visible(audio_by_stream.get(" + chr(10) +
     '            _audio_stream_of_display(unit), ""), may_use)',
     "        audio_of[identity] = _visible(video_by_stream.get(" + chr(10) +
     '            luma_projection._stream_of_display(unit), ""), may_use)',
     "audio is read from the video stream, so a breakaway can never appear",
     [py("test_user_matrix.py", "CellStateTests")]),
    ("matrix.partial-reported-as-all", "luma_user_matrix.py",
     '        return "all" if total and n == total else ("partial" if n else "none")',
     '        return "all" if n else "none"',
     "a group with some members on a source is reported as wholly on it",
     [py("test_user_matrix.py", "CellStateTests")]),
    ("matrix.offline-member-counted", "luma_user_matrix.py",
     "        online_members = [m for m in member_ids" + chr(10) +
     "                          if m in index and luma_projection.is_online(index[m])]",
     "        online_members = [m for m in member_ids if m in index]",
     "a member that is not answering is counted as holding the source",
     [py("test_user_matrix.py", "CellStateTests")]),
    ("matrix.mixed-hidden", "luma_user_matrix.py",
     "        elif len(tokens) > 1:" + chr(10) + '            state = "mixed"',
     "        elif False:" + chr(10) + '            state = "mixed"',
     "members that disagree are summarised as one state",
     [py("test_user_matrix.py", "CellStateTests")]),

    # ---- the page -------------------------------------------------------------------------
    ("ui.remembered-view-believed", "ui/user.js",
     "    var view = viewAllowed(want) ? want",
     "    var view = want ? want",
     "a remembered interface is shown even after the account lost it",
     [PAGE]),
    ("ui.tabs-always-hidden", "ui/user.js",
     "    $('mode_tabs').hidden = !both;",
     "    $('mode_tabs').hidden = true;",
     "an account with both interfaces cannot reach the second one",
     [PAGE]),
    ("ui.tabs-always-shown", "ui/user.js",
     "    $('mode_tabs').hidden = !both;",
     "    $('mode_tabs').hidden = false;",
     "an account with one interface is offered a second tab",
     [PAGE]),
    ("ui.matrix-cell-names-standard", "ui/user.js",
     "      body.interface = 'matrix';" + NL + "      body.mode = state.mode;",
     "      body.interface = 'standard';" + NL + "      body.mode = state.mode;",
     "a Matrix click is sent as a Standard one",
     [PAGE]),
    ("ui.mode-ignored", "ui/user.js",
     "      body.interface = 'matrix';" + NL + "      body.mode = state.mode;",
     "      body.interface = 'matrix';" + NL + "      body.mode = 'av';",
     "the Video and Audio choices are not sent",
     [PAGE]),
    ("ui.offline-cell-pressable", "ui/user.js",
     "    if (!source.online || !row.online_members) return false;",
     "    if (false) return false;",
     "a cell with an offline source or display can be pressed",
     [PAGE]),
    ("ui.wall-mode-limit-dropped", "ui/user.js",
     "    return row.modes.indexOf(state.mode) >= 0;",
     "    return true;",
     "a wall cell can be pressed in a mode it does not have",
     [PAGE]),
    ("ui.hidden-standard-still-renders", "ui/user.js",
     "    if (state.view !== 'standard') return;",
     "    if (false) return;",
     "the Standard cards are built and watched while the Matrix is the only interface",
     [PAGE]),
    ("ui.matrix-403-ignored", "ui/user.js",
     "      if (answer.res.status === 403) {            // the interface was taken away under us",
     "      if (false) {            // the interface was taken away under us",
     "a refusal from the matrix leaves the person on a page that no longer works",
     [PAGE]),
    ("ui.interface-choice-not-saved", "ui/admin_users.js",
     "                       standard_routing: $('u_standard').checked,",
     "                       standard_routing: true,",
     "the administrator's Standard choice is not sent",
     [node("admin_routing_interface_test.js")]),
    ("ui.interface-neither-sent", "ui/admin_users.js",
     "    if (role !== 'admin' && !permissions.standard_routing && !permissions.matrix_routing) {",
     "    if (false) {",
     "an account can be saved from the editor with no routing interface",
     [node("admin_routing_interface_test.js")]),
    # ---- keyboard focus survives a route -----------------------------------------------------
    ("focus.cell-disabled-while-busy", "ui/user.js",
     "    button.disabled = !usable;",
     "    button.disabled = !usable || state.busy;",
     "the cell just pressed is disabled while its route runs, so it drops focus to the page",
     [FOCUS]),
    ("focus.standard-render-forgets", "ui/user.js",
     "    watchPreviews();" + NL + "    restoreFocus(held);" + NL + "  }",
     "    watchPreviews();" + NL + "  }",
     "the Standard cards are rebuilt and the focused one is not found again",
     [FOCUS]),
    ("focus.grid-rebuild-forgets", "ui/user.js",
     "    restoreFocus(held);" + NL + "  }" + NL + NL + "  function loadMatrix() {",
     "  }" + NL + NL + "  function loadMatrix() {",
     "a rebuilt matrix grid is not given its focused cell back",
     [FOCUS]),
    # Retired, not repointed: "focus.dialog-close-not-restored" and "focus.route-end-forgets" broke the two
    # restoreFocus(opener) calls at the end of a Standard route. Routing no longer disables the Route button or
    # replaces the page while a route runs, so focus is never stranded there; removing BOTH calls together was
    # measured to leave user_focus_test.js green (13/13). They are a harmless second line behind render()'s own
    # restoreFocus(held), and a mutation nothing can fail is noise that reads as a gap.
    ("focus.steals-from-the-person", "ui/user.js",
     "    if (!token || !focusStranded()) return;",
     "    if (!token) return;",
     "focus is pulled back to the opener even after the person moved somewhere else",
     [FOCUS]),
    ("focus.found-by-first-part-only", "ui/user.js",
     "      if (have[0] === want[0] && have[1] === want[1]) return node;",
     "      if (have[0] === want[0]) return node;",
     "a control is found again by the first part of its identity only, so the wrong cell takes focus",
     [FOCUS]),
    # ---- the second look is authorised exactly like a route ----------------------------------
    ("check.interface-grant-skipped", "luma_remote_api.py",
     CHECK_REFUSAL + '            if not account.permissions.may_use_interface(interface):' + NL +
     '                return "interface_not_permitted"',
     CHECK_REFUSAL + '            if False:' + NL + '                return "interface_not_permitted"',
     "the later look at a route no longer needs the interface grant", [CHECK]),
    ("check.source-grant-skipped", "luma_remote_api.py",
     CHECK_REFUSAL + '            if not account.permissions.may_use_interface(interface):' + NL +
     '                return "interface_not_permitted"' + NL +
     '            if not account.permissions.may_use_source(source_id):' + NL +
     '                return "source_not_permitted"',
     CHECK_REFUSAL + '            if not account.permissions.may_use_interface(interface):' + NL +
     '                return "interface_not_permitted"',
     "the later look reads a display routed from a source the account was not given", [CHECK]),
    ("check.display-grant-skipped", "luma_remote_api.py",
     CHECK_REFUSAL + '            if not account.permissions.may_use_interface(interface):' + NL +
     '                return "interface_not_permitted"' + NL +
     '            if not account.permissions.may_use_source(source_id):' + NL +
     '                return "source_not_permitted"' + NL +
     '            for identity in display_ids:',
     CHECK_REFUSAL + '            if not account.permissions.may_use_interface(interface):' + NL +
     '                return "interface_not_permitted"' + NL +
     '            if not account.permissions.may_use_source(source_id):' + NL +
     '                return "source_not_permitted"' + NL +
     '            for identity in []:',
     "the later look reads a display the account was not given", [CHECK]),
    ("check.group-grant-skipped", "luma_remote_api.py",
     CHECK_REFUSAL + '            if not account.permissions.may_use_interface(interface):' + NL +
     '                return "interface_not_permitted"' + NL +
     '            if not account.permissions.may_use_source(source_id):' + NL +
     '                return "source_not_permitted"' + NL +
     '            for identity in display_ids:' + NL +
     '                if not account.permissions.may_control_destination(identity):' + NL +
     '                    return "destination_not_permitted"' + NL +
     '            for group_id in group_ids:',
     CHECK_REFUSAL + '            if not account.permissions.may_use_interface(interface):' + NL +
     '                return "interface_not_permitted"' + NL +
     '            if not account.permissions.may_use_source(source_id):' + NL +
     '                return "source_not_permitted"' + NL +
     '            for identity in display_ids:' + NL +
     '                if not account.permissions.may_control_destination(identity):' + NL +
     '                    return "destination_not_permitted"' + NL +
     '            for group_id in []:',
     "the later look reads the members of a group the account was not given", [CHECK]),
]
