"""A mutation harness: a test of the tests.

Each entry breaks one security predicate on purpose -- a text substitution in a
production file -- and requires the named tests to fail. An assertion nobody has
watched fail is an assertion nobody has verified.

    python tests/mutate_security.py                 run every mutation
    python tests/mutate_security.py -k gate.        only ids containing "gate."
    python tests/mutate_security.py --check         verify every anchor matches exactly once
    python tests/mutate_security.py --coverage      verify every suite is watched by a mutation

A SURVIVOR (a mutation no test noticed) is investigated, never waved through. It is one
of three things -- a real test gap, a badly aimed mutation, or a genuinely redundant
control -- and which one has to be established rather than assumed.

Rules this file keeps:
  * the file is restored in a `finally`, and a recovery copy is kept beside it, so a
    killed run never leaves a deliberately defective source behind (the next run
    restores any it finds before doing anything else);
  * sources here are LF; an anchor is matched exactly and must match exactly once;
  * each suite's WHOLE output is recorded, not its last line: a runner that greps the
    summary reports every mutation as uncaught, which reads exactly like tests that
    do not work;
  * the harness is itself validated: `--coverage` fails if a suite it relies on is not
    broken by at least one mutation, because a suite it cannot see fail would report
    that suite's tests as worthless.
"""
import argparse
import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
NL = chr(10)
Q = chr(34)

# Suites: (label, command). A selector narrows a Python suite to the classes that matter,
# so a mutation costs seconds, not the whole file.
def py(file, *selectors):
    return ("python:" + file + (":" + ",".join(selectors) if selectors else ""),
            [PY, os.path.join("tests", file)] + list(selectors))


def node(file):
    return ("node:" + file, ["node", os.path.join("tests", file)])


# route_multi and route_check carry near-identical grant checks, so an anchor inside either has to
# begin in text only the one has: route_multi's check follows its "walls are not available" refusal.
MULTI_CHECK_HEAD = ("error\": \"Video walls are not available.\"}), 404\n\n        def _check(account):\n"
                    "            if not account.permissions.may_use_interface(interface):\n"
                    "                return \"interface_not_permitted\"\n")
MULTI_CHECK_BODY = MULTI_CHECK_HEAD + ("            if not account.permissions.may_use_source(source_id):\n"
                                      "                return \"source_not_permitted\"\n")

# (id, file, anchor, replacement, what it breaks, suites that must notice)
MUTATIONS = [
    # ---- who is local -----------------------------------------------------------------
    ("listener.everything-is-local", "luma_remote.py",
     "    return REMOTE if value == REMOTE else LOCAL",
     "    return LOCAL",
     "every request reads as local",
     [py("test_remote_security.py", "LocalVersusRemoteTests", "ListenerTaggingTests")]),
    ("listener.bind-tag-inverted", "luma_remote.py",
     "    return LOCAL if is_loopback_address(host) else REMOTE",
     "    return REMOTE if is_loopback_address(host) else LOCAL",
     "a network listener earns local trust",
     [py("test_remote_security.py", "LocalVersusRemoteTests", "ListenerTaggingTests")]),
    ("listener.trust-remote-addr", "luma_remote.py",
     "    value = environ.get(LISTENER_KEY)\n    return REMOTE if value == REMOTE else LOCAL",
     '    return LOCAL if environ.get("REMOTE_ADDR") in ("127.0.0.1", "::1") else REMOTE',
     "locality decided from the claimed peer address instead of the accepting socket",
     [py("test_remote_security.py", "LocalVersusRemoteTests")]),

    # ---- classification -----------------------------------------------------------------
    ("classify.unknown-is-public", "luma_access.py",
     "    return ROUTES.get(key, Access.LOCAL_ONLY)",
     "    return ROUTES.get(key, Access.PUBLIC)",
     "an unclassified route is open",
     [py("test_access_fence.py", "RouteClassificationFenceTests"),
      py("test_remote_security.py", "AuthorizationTests")]),
    ("classify.list-dir-admin", "luma_access.py",
     '    "/api/list_dir": Access.LOCAL_ONLY,',
     '    "/api/list_dir": Access.ADMIN,',
     "the host filesystem listing is reachable by a remote administrator",
     [py("test_access_fence.py", "RouteClassificationFenceTests")]),
    ("classify.open-folder-admin", "luma_access.py",
     '    "/api/open_firmware_folder": Access.LOCAL_ONLY,',
     '    "/api/open_firmware_folder": Access.ADMIN,',
     "a remote administrator can open a folder on the host computer",
     [py("test_access_fence.py", "RouteClassificationFenceTests"),
      py("test_device_manager_parity.py", "FirmwareFolderTests")]),
    ("firmware-folder.drive-root-accepted", "lumaserver.py",
     "    if os.path.dirname(path) == path:\n        return None\n    return path if os.path.isdir(path) else \"\"",
     "    return path if os.path.isdir(path) else \"\"",
     "the stock drive-root default is opened as though the operator had chosen it",
     [py("test_device_manager_parity.py", "FirmwareFolderTests")]),
    ("netstatus.any-interface-is-the-av-lan", "luma_netstatus.py",
     '        if isinstance(item, dict) and isinstance(item.get("lanid"), str) \\\n                and item["lanid"].strip().lower() in AV_LAN_IDS:',
     '        if isinstance(item, dict) and isinstance(item.get("lanid"), str):',
     "the second port's speed is reported as the AV LAN's",
     [py("test_luma_netstatus.py", "ParseTests")]),
    ("netstatus.down-link-keeps-its-speed", "luma_netstatus.py",
     "    speed = normalize_speed(raw_speed) if link_up is not False else None",
     "    speed = normalize_speed(raw_speed)",
     "a link that is down is shown with a speed it cannot have",
     [py("test_luma_netstatus.py", "ParseTests")]),
    ("netstatus.switcher-is-asked", "luma_netstatus.py",
     '    if "switcher" in role:\n        return None',
     '    if False:\n        return None',
     "an undocumented call is made to a CS31",
     [py("test_luma_netstatus.py", "ReadTests")]),
    ("netstatus.control-characters-kept", "luma_netstatus.py",
     '_CONTROL = re.compile(r"[\\x00-\\x1f\\x7f]")',
     '_CONTROL = re.compile(r"(?!)")',
     "device text carrying control characters reaches the browser",
     [py("test_luma_netstatus.py", "ParseTests")]),
    ("netstatus.merge-instead-of-replace", "lumaserver.py",
     "        _replace_net_status(cache_units[ip], info)\n",
     "",
     "a neighbour that went away is kept from the last read",
     [py("test_luma_netstatus.py", "ScanKeepsItTests")]),
    ("classify.ui-traversal-allowed", "luma_access.py",
     '    if (not name or "/" in name or "\\\\" in name or ".." in name\n            or "\\x00" in name):\n        return Access.ADMIN',
     "    if not name:\n        return Access.ADMIN",
     "a path with separators or parent references is judged as a plain file name",
     [py("test_access_fence.py", "UiFileClassificationTests")]),
    ("classify.ui-everything-public", "luma_access.py",
     "    if name in PUBLIC_UI_FILES:\n        return Access.PUBLIC",
     "    if name:\n        return Access.PUBLIC",
     "every file under ui/ is public",
     [py("test_access_fence.py", "UiFileClassificationTests"),
      py("test_remote_security.py", "AuthorizationTests")]),
    ("classify.method-override-widens", "luma_access.py",
     "    override = METHOD_ROUTES.get((method, key))\n    if override is not None:\n        return override",
     '    override = METHOD_ROUTES.get((method, key)) or ({"/api/config": Access.PUBLIC}.get(key))\n    if override is not None:\n        return override',
     "an override widens a rule",
     [py("test_remote_security.py", "AuthorizationTests", "CsrfTests")]),

    # ---- the gate -------------------------------------------------------------------------
    ("gate.local-only-allows-remote", "luma_gate.py",
     '    if access is Access.LOCAL_ONLY:\n        return Decision(False, 404, "local_only")',
     "    if False:\n        return Decision(False, 404, \"local_only\")",
     "a local-only route is reachable from the network",
     [py("test_remote_security.py", "AuthorizationTests")]),
    ("gate.remote-body-unlimited", "luma_gate.py",
     "            request.max_content_length = REMOTE_MAX_BODY_BYTES\n",
     "            pass\n",
     "a stranger on the network chooses how much body the gate reads before the access decision",
     [py("test_remote_body_limit.py", "RemoteBodyLimit")]),
    ("gate.body-limit-on-the-local-listener-too", "luma_gate.py",
     "        if luma_remote.is_local_request(request.environ):\n            g.principal = None",
     "        request.max_content_length = REMOTE_MAX_BODY_BYTES\n        if luma_remote.is_local_request(request.environ):\n            g.principal = None",
     "the local listener is limited, so a large local restore would be refused",
     [py("test_remote_body_limit.py", "RemoteBodyLimit")]),
    ("gate.no-session-allowed", "luma_gate.py",
     '    if account is None:\n        return Decision(False, 401, "no_session")',
     "    if False:\n        return Decision(False, 401, \"no_session\")",
     "a request with no session is admitted",
     [py("test_remote_security.py", "AuthorizationTests")]),
    ("gate.csrf-off", "luma_gate.py",
     "    if method in UNSAFE_METHODS:\n        presented",
     "    if False:\n        presented",
     "state-changing requests need no CSRF token",
     [py("test_remote_security.py", "CsrfTests")]),
    ("gate.csrf-presence-only", "luma_gate.py",
     "        if not presented or presented != session.csrf_token:",
     "        if not presented:",
     "any CSRF token is accepted so long as one is present",
     [py("test_remote_security.py", "CsrfTests")]),
    ("gate.direct-link-reaches-admin", "luma_gate.py",
     "    if from_link and access is Access.ADMIN:",
     "    if False:",
     "a Direct Access session reaches the administrative surface",
     [py("test_remote_security.py", "DirectAccessTests")]),
    ("gate.user-is-admin", "luma_gate.py",
     "        if not account.is_admin:\n            return Decision(False, 403, \"not_admin\", account)",
     "        if False:\n            return Decision(False, 403, \"not_admin\", account)",
     "a restricted user passes an administrative route",
     [py("test_remote_security.py", "AuthorizationTests")]),
    ("gate.admin-shortcut-for-links", "luma_gate.py",
     "        if account.is_admin and not from_link:",
     "        if account.is_admin:",
     "a promoted account's link session skips the per-resource check",
     [py("test_remote_security.py", "DirectAccessTests"), py("test_user_routing.py", "PermissionTests")]),
    ("gate.source-not-checked", "luma_gate.py",
     '        if not account.permissions.may_use_source(identity):\n            return Decision(False, 403, "source_not_permitted", account)',
     "        pass",
     "any source may be used (the gate alone: the handlers repeat this check, so only a test of the gate sees it)",
     [py("test_remote_security.py", "GateEvaluateTests")]),
    ("gate.destination-not-checked", "luma_gate.py",
     '        if not account.permissions.may_control_destination(identity):\n            return Decision(False, 403, "destination_not_permitted", account)',
     "        pass",
     "any display may be driven (the gate alone)",
     [py("test_remote_security.py", "GateEvaluateTests")]),
    ("gate.group-not-checked", "luma_gate.py",
     '        if not account.permissions.may_control_group(group_id):\n            return Decision(False, 403, "group_not_permitted", account)',
     "        pass",
     "any display group may be driven (the gate alone)",
     [py("test_remote_security.py", "GateEvaluateTests")]),
    ("gate.remote-off-allows", "luma_gate.py",
     "        if not enabled_getter():",
     "        if False:",
     "remote requests are admitted while remote access is off",
     [py("test_remote_security.py", "LocalVersusRemoteTests")]),

    # ---- accounts and sessions ------------------------------------------------------------
    ("session.disabled-still-works", "luma_users.py",
     "            if account is None or not account.enabled:\n                self._sessions.pop(token, None)",
     "            if account is None:\n                self._sessions.pop(token, None)",
     "a disabled account keeps its session",
     [py("test_remote_security.py", "SessionLifecycleTests")]),
    ("session.never-expires", "luma_users.py",
     "        return (now - self.last_seen > IDLE_TIMEOUT_SECONDS\n                or now - self.created_at > ABSOLUTE_TIMEOUT_SECONDS)",
     "        return False",
     "password sessions never expire",
     [py("test_remote_security.py", "SessionLifecycleTests")]),
    ("session.no-absolute-limit", "luma_users.py",
     "                or now - self.created_at > ABSOLUTE_TIMEOUT_SECONDS)",
     "                or False)",
     "an active password session lasts forever",
     [py("test_remote_security.py", "SessionLifecycleTests")]),
    ("login.no-lockout", "luma_users.py",
     "MAX_FAILURES = 5",
     "MAX_FAILURES = 10 ** 9",
     "repeated failures are never throttled",
     [py("test_remote_security.py", "LoginHardeningTests")]),
    ("login.unknown-user-is-cheap", "luma_users.py",
     "        good = verify_password(stored, password)",
     "        good = verify_password(stored, password) if account else False",
     "an unknown username skips the password verification",
     [py("test_remote_security.py", "LoginHardeningTests")]),
    ("login.disabled-is-distinct", "luma_users.py",
     '            return {"ok": False, "error": "invalid username or password"}',
     '            return {"ok": False, "error": ("account disabled" if reason == "disabled" else "invalid username or password")}',
     "a disabled account is announced",
     [py("test_remote_security.py", "LoginHardeningTests")]),
    ("account.last-admin-removable", "luma_users.py",
     "        return not others",
     "        return False",
     "the only administrator can be disabled, demoted or deleted",
     [py("test_remote_security.py", "SessionLifecycleTests")]),
    ("account.short-password-allowed", "luma_users.py",
     "    def set_password(self, user_id, password, *, actor=None) -> dict:" + NL + "        if len(str(password or \"\")) < 8:",
     "    def set_password(self, user_id, password, *, actor=None) -> dict:" + NL + "        if len(str(password or \"\")) < 0:",
     "a password of any length is accepted when it is changed",
     [py("test_remote_security.py", "LoginHardeningTests")]),
    ("account.password-change-keeps-old-sessions", "luma_users.py",
     "            self.revoke_password_sessions(user_id, _locked=True)" + NL + "            self.audit(" + Q + "user.password_changed" + Q,
     "            pass" + NL + "            self.audit(" + Q + "user.password_changed" + Q,
     "a password change leaves the account's old password sessions alive",
     [py("test_account_lifecycle.py", "SessionRevocationTests")]),
    ("account.password-change-ends-link-session", "luma_users.py",
     "            self.revoke_password_sessions(user_id, _locked=True)" + NL + "            self.audit(" + Q + "user.password_changed" + Q,
     "            self.revoke_all_for_user(user_id, _locked=True)" + NL + "            self.audit(" + Q + "user.password_changed" + Q,
     "a password change also ends sessions a Direct Access link opened",
     [py("test_account_lifecycle.py", "DirectAccessLifecycleTests")]),
    ("perm.source-any", "luma_users.py",
     "        return bool(wanted) and wanted in self.sources",
     "        return True",
     "every source is permitted",
     [py("test_user_routing.py", "PermissionTests", "PreviewTests")]),
    ("perm.destination-any", "luma_users.py",
     "        return bool(wanted) and wanted in self.destinations",
     "        return True",
     "every display is permitted",
     [py("test_user_routing.py", "PermissionTests", "PreviewTests")]),
    ("perm.group-any", "luma_users.py",
     "        return _normalise_group_id(group_id) in self.groups",
     "        return True",
     "every group is permitted",
     [py("test_display_groups.py", "GroupRoutingTests")]),
    ("project.group-members-listed-as-displays", "luma_projection.py",
     "    display_units = {i: u for i, u in index.items()" + NL + "                     if _kind(u) == " + chr(34) + "display" + chr(34) + " and may_drive(i)}",
     "    granted = {m for g in groups.all() if admin or perms.may_control_group(g[" + chr(34) + "id" + chr(34) + "]) for m in g[" + chr(34) + "members" + chr(34) + "]}" + NL +
     "    display_units = {i: u for i, u in index.items()" + NL + "                     if _kind(u) == " + chr(34) + "display" + chr(34) + " and (may_drive(i) or i in granted)}",
     "a granted group's members are listed to the user as individual displays",
     [py("test_display_groups.py", "GroupRoutingTests"), py("test_user_routing.py", "ProjectionTests")]),
    ("preview.handler-permission-skipped", "luma_remote_api.py",
     "            if not allowed:" + NL + "                reason = (" + chr(34) + "source_not_permitted" + chr(34) + " if kind == " + chr(34) + "source" + chr(34),
     "            if False:" + NL + "                reason = (" + chr(34) + "source_not_permitted" + chr(34) + " if kind == " + chr(34) + "source" + chr(34),
     "the preview handler's own permission check is removed (the gate's is already out of the way)",
     [py("test_user_routing.py", "HandlerSecondLineTests")]),
    ("route.handler-source-check-skipped", "luma_remote_api.py",
     "        def _check(account):" + NL + "            if not account.permissions.may_use_interface(" + chr(34) + "standard" + chr(34) + "):" + NL +
     "                return " + chr(34) + "interface_not_permitted" + chr(34) + NL +
     "            if not account.permissions.may_use_source(source_id):" + NL +
     "                return " + chr(34) + "source_not_permitted" + chr(34) + NL + "            if not account.permissions.may_control_destination(destination_id):",
     "        def _check(account):" + NL + "            if not account.permissions.may_use_interface(" + chr(34) + "standard" + chr(34) + "):" + NL +
     "                return " + chr(34) + "interface_not_permitted" + chr(34) + NL + "            if False:" + NL +
     "                return " + chr(34) + "source_not_permitted" + chr(34) + NL + "            if not account.permissions.may_control_destination(destination_id):",
     "the route handler no longer checks the source itself",
     [py("test_user_routing.py", "HandlerSecondLineTests")]),
    ("direct.admin-may-have-a-link", "luma_users.py",
     "        return self.role == ROLE_ADMIN",
     "        return False",
     "an administrator is treated as a restricted user",
     [py("test_remote_security.py", "AuthorizationTests", "DirectAccessTests", "SessionLifecycleTests")]),
    ("direct.sessions-expire-wrongly", "luma_users.py",
     "DIRECT_SESSIONS_EXPIRE = False",
     "DIRECT_SESSIONS_EXPIRE = True",
     "a Direct Access session expires",
     [py("test_remote_security.py", "DirectAccessTests")]),
    ("audit.password-not-redacted", "luma_users.py",
     "    if isinstance(payload, (list, tuple)):\n        return [redact(v) for v in payload]",
     "    if isinstance(payload, (list, tuple)):\n        return list(payload)",
     "lists are not redacted",
     [py("test_server_hardening.py", "AuditTextTests")]),
    ("audit.immediate-names-stale", "luma_users.py",
     '    "user.role_changed", "user.permissions_changed", "user.password_changed",',
     '    "user.set_role", "user.set_permissions", "user.set_password",',
     "the immediate-flush list names events that are never emitted",
     [py("test_server_hardening.py", "AuditTextTests")]),

    # ---- the user surface -----------------------------------------------------------------
    ("route.no-recheck-after-wait", "luma_remote_api.py",
     '                refusal = _revalidate(check, "/api/user/route")   # after the wait',
     "                refusal = None",
     "permissions are not re-read after waiting for the display",
     [py("test_user_routing.py", "PermissionTests")]),
    ("route.no-display-lock", "luma_remote_api.py",
     "            with locks.hold(locks.decoder(destination_id)):\n                refusal = _revalidate(check",
     "            with __import__('contextlib').nullcontext():\n                refusal = _revalidate(check",
     "two operators can interleave inside one display's transaction",
     [py("test_user_routing.py", "PermissionTests")]),
    ("preview.permission-skipped", "luma_remote_api.py",
     "            if not allowed:\n                reason = (\"source_not_permitted\" if kind == \"source\"",
     "            if False:\n                reason = (\"source_not_permitted\" if kind == \"source\"",
     "a preview is fetched for any device (with the gate out of the way)",
     [py("test_user_routing.py", "HandlerSecondLineTests")]),
    ("group.stops-on-first-failure", "luma_groups.py",
     "        results.append(dict(outcome, index=index))",
     "        results.append(dict(outcome, index=index))\n        if not outcome.get(\"ok\"):\n            break",
     "a failed member stops the remaining members",
     [py("test_display_groups.py", "SequentialRunTests", "GroupRoutingTests")]),
    ("group.members-listed-to-users", "luma_projection.py",
     '        group_rows.append({"id": group["id"], "name": group["name"],\n                           "members": total, "online_members": online_count,',
     '        group_rows.append({"id": group["id"], "name": group["name"],\n                           "members": total, "member_ids": list(group["members"]), "online_members": online_count,',
     "a group's members are listed to a user",
     [py("test_user_routing.py", "ProjectionTests"), py("test_display_groups.py", "GroupRoutingTests")]),
    ("group.not-filtered-by-grant", "luma_projection.py",
     '        if not admin and not perms.may_control_group(group["id"]):\n            continue',
     "        pass",
     "every group is shown to every user",
     [py("test_user_routing.py", "ProjectionTests")]),
    ("project.address-leaks", "luma_projection.py",
     '    sources = [{"id": i, "name": source_names[i], "online": is_online(u)}',
     '    sources = [{"id": i, "name": source_names[i], "online": is_online(u), "ip": u.get("ip")}',
     "a source's address is sent to a user",
     [py("test_user_routing.py", "ProjectionTests")]),
    ("project.ungranted-listed", "luma_projection.py",
     "    source_units = {i: u for i, u in index.items()\n                    if _kind(u) == \"source\" and may_use(i)}",
     "    source_units = {i: u for i, u in index.items()\n                    if _kind(u) == \"source\"}",
     "every source is listed to every user",
     [py("test_user_routing.py", "ProjectionTests")]),
    ("project.address-as-identity", "luma_projection.py",
     "    return luma_mac.normalise_mac(value)",
     "    compact = str(value or '').lower().replace(':', '').replace('-', '').replace('.', '')\n"
     "    return ':'.join(compact[i:i + 2] for i in range(0, 12, 2)) if len(compact) == 12 else ''",
     "an address can be accepted as an identity",
     [py("test_mac_shape.py", "EveryNormaliserAgreesTests", "GroupsAndPermissionsTests")]),

    # ---- routing verification and credentials -----------------------------------------------
    ("route.verify-always-true", "lumaserver.py",
     "    verified = observed == streamname",
     "    verified = True",
     "a route is reported successful whatever the display holds",
     [py("test_route_verification.py", "VerifiedRouteTests")]),
    ("route.cache-told-before-verify", "lumaserver.py",
     "        if verified:\n            dec_unit[\"routed_encoder_ip\"] = encoder_ip",
     "        if True:\n            dec_unit[\"routed_encoder_ip\"] = encoder_ip",
     "the cache records the expected route even when it was not verified",
     [py("test_route_verification.py", "VerifiedRouteTests")]),
    ("route.write-repeated", "lumaserver.py",
     "    for attempt in range(ROUTE_READBACK_ATTEMPTS):\n        observed, raw = read_back()",
     "    for attempt in range(ROUTE_READBACK_ATTEMPTS):\n        set_resp = _ws_call_auth(decoder_ip, \"SelectVideoStream.Set\", {\"selecttype\": 0, \"streamname\": streamname, \"password\": \"x\"}, timeout) if attempt else set_resp\n        observed, raw = read_back()",
     "the write is repeated to get a confirmation",
     [py("test_route_verification.py", "VerifiedRouteTests")]),
    ("route.users-fall-back-to-cache", "lumaserver.py",
     "        return _route_av(decoder_ip, encoder_ip, allow_cached_stream=False, check_audio=True)",
     "        return _route_av(decoder_ip, encoder_ip, allow_cached_stream=True, check_audio=True)",
     "a restricted route consumes a cached stream name",
     [py("test_route_verification.py", "UserRouteUsesTheSameEngineTests")]),
    ("config.password-to-remote", "lumaserver.py",
     "        if include_pw and not luma_remote.is_local_request(request.environ):",
     "        if False:",
     "device passwords are returned to a remote administrator",
     [py("test_server_hardening.py", "CredentialNonDisclosureTests")]),
    ("upgrade.remote-absolute-path", "lumaserver.py",
     "    elif firmware and (os.path.isabs(str(firmware))",
     "    elif False and (os.path.isabs(str(firmware))",
     "a remote administrator can name a file anywhere on the host",
     [py("test_server_hardening.py", "FirmwareHardeningTests")]),
    ("upgrade.remote-credential-override", "lumaserver.py",
     "    if local_request:\n        if data.get(\"username\"):",
     "    if True:\n        if data.get(\"username\"):",
     "a remote request overwrites the stored device credentials",
     [py("test_server_hardening.py", "FirmwareHardeningTests")]),
    ("files.remote-sees-host-path", "lumaserver.py",
     "    if not luma_remote.is_local_request(request.environ):\n        # File names only",
     "    if False:\n        # File names only",
     "a remote administrator is told where the firmware folder is",
     [py("test_server_hardening.py", "FirmwareHardeningTests")]),

    # ---- Clear Units and persistence -------------------------------------------------------
    ("clear.merge-resurrects-cleared-units", "lumaserver.py",
     "            if generation != cache_generation:\n                return 0\n            known = {",
     "            known = {",
     "a cache read that began before Clear Units puts the cleared units back",
     [py("test_server_hardening.py", "PersistenceTests")]),
    ("clear.keeps-user-grants", "luma_users.py",
     "                    account.permissions = p.replace(\n                        sources=p.sources - gone, destinations=p.destinations - gone)",
     "                    account.permissions = p.replace(\n                        sources=p.sources, destinations=p.destinations)",
     "Clear Units leaves devices assigned to accounts",
     [py("test_server_hardening.py", "ClearUnitsCascadeTests")]),
    ("clear.keeps-group-members", "luma_groups.py",
     "                kept = [m for m in g[\"members\"] if m not in gone]",
     "                kept = [m for m in g[\"members\"]]",
     "Clear Units leaves cleared displays inside display groups",
     [py("test_server_hardening.py", "ClearUnitsCascadeTests")]),
    ("clear.keeps-names", "luma_aliases.py",
     "            removed = [k for k in gone if k in self._aliases]",
     "            removed = []",
     "Clear Units leaves friendly names for cleared devices",
     [py("test_server_hardening.py", "ClearUnitsCascadeTests")]),
    ("clear.no-rollback-on-write-failure", "lumaserver.py",
     "            cache_units.update(before_units)\n            undo()",
     "            cache_units.update(before_units)",
     "a failed inventory write leaves assignments cleaned for devices that remain",
     [py("test_server_hardening.py", "ClearUnitsCascadeTests")]),
    ("clear.remove-ignores-assignments", "lumaserver.py",
     "        result, undo = _forget_devices(identities)\n        if not result[\"ok\"]:\n            return jsonify({\"ok\": False, \"error\": \"Accounts and display groups could not \"\n                                                  \"be updated, so nothing was removed.\"}), 500",
     "        result, undo = ({\"ok\": True, \"accounts\": 0, \"groups\": 0, \"names\": 0}, (lambda: None))",
     "removing individual units leaves their assignments",
     [py("test_server_hardening.py", "ClearUnitsCascadeTests")]),
    # ---- Clear Units that keeps what Remote Access uses; the restore contract ---------------
    ("inventory.on-clears-assigned", "lumaserver.py",
     "    if REMOTE.enabled():\n        return _clear_unassigned()\n    return _clear_full()",
     "    return _clear_full()",
     "Clear Units removes assigned units while Remote Access is on",
     [py("test_clear_assigned.py", "RemoteOnTests", "ManipulatedRequestTests")]),
    ("inventory.ignores-user-assignments", "luma_inventory.py",
     "    for identity, names in (store.assigned_devices() or {}).items():",
     "    for identity, names in {}.items():",
     "a unit given to a user is classed as unassigned",
     [py("test_clear_assigned.py", "RemoteOnTests", "AssignmentViewTests")]),
    ("inventory.ignores-group-members", "luma_inventory.py",
     "    for name, members in (groups.referenced_identities() or {}).items():",
     "    for name, members in {}.items():",
     "a display in a display group is classed as unassigned",
     [py("test_clear_assigned.py", "RemoteOnTests", "AssignmentViewTests")]),
    ("seam.clear-forgets-matrix-grants", "luma_inventory.py",
     "    for identity, names in (store.assigned_devices() or {}).items():",
     "    for identity, names in {}.items():",
     "Clear Units removes the units a Matrix account was granted, and the rows with them",
     [py("test_remote_matrix_integration.py", "ClearAgainstMatrixTests")]),
    ("seam.clear-forgets-wall-members", "luma_inventory.py",
     "            for _cell, identity, _unit in luma_walls.members(wall, unit_list, walls.device_id_of):",
     "            for _cell, identity, _unit in ():",
     "Clear Units removes the displays of a saved wall a Matrix account was granted",
     [py("test_remote_matrix_integration.py", "ClearAgainstMatrixTests")]),
    ("inventory.ignores-wall-members", "luma_inventory.py",
     "                if identity:\n                    record = found.setdefault(identity, _empty())\n                    record[\"walls\"].add(label)\n                    record[\"wall_reasons\"].add(\"Member of wall \"",
     "                if False:\n                    record = found.setdefault(identity, _empty())\n                    record[\"walls\"].add(label)\n                    record[\"wall_reasons\"].add(\"Member of wall \"",
     "a decoder in a saved video wall is classed as unassigned",
     [py("test_clear_assigned.py", "RemoteOnTests", "AssignmentViewTests")]),
    ("inventory.ignores-wall-source", "luma_inventory.py",
     "            if source is not None:\n",
     "            if False:\n",
     "the source a saved video wall uses is classed as unassigned",
     [py("test_clear_assigned.py", "RemoteOnTests", "AssignmentViewTests")]),
    ("inventory.identity-by-address", "luma_inventory.py",
     "        identity = luma_projection.unit_identity(unit)\n        (kept if identity and identity in assigned else doomed).append(ip)",
     "        (kept if ip in assigned else doomed).append(ip)",
     "protection follows an address, so a unit that moved loses it",
     [py("test_clear_assigned.py", "RemoteOnTests")]),
    ("inventory.identityless-units-kept", "luma_inventory.py",
     "(kept if identity and identity in assigned else doomed)",
     "(kept if not identity or identity in assigned else doomed)",
     "a unit with no hardware identity can never be cleared",
     [py("test_clear_assigned.py", "RemoteOnTests")]),
    ("inventory.direct-access-not-named", "luma_inventory.py",
     "        record[\"direct_access\"] = record[\"users\"] & direct",
     "        record[\"direct_access\"] = set()",
     "an account reachable by a Direct Access link is not marked as such",
     [py("test_clear_assigned.py", "AssignmentViewTests")]),
    ("inventory.no-lock-held", "lumaserver.py",
     "    removal is undone.\n    \"\"\"\n    global cache_generation\n    with REMOTE.store.hold(), REMOTE.groups.hold(), luma_store.lock_for(VIDEO_WALL_STATE_FILE):\n",
     "    removal is undone.\n    \"\"\"\n    global cache_generation\n    if True:\n",
     "an assignment added between classifying and removing loses its device",
     [py("test_clear_assigned.py", "RaceTests")]),
    ("inventory.generation-not-bumped", "lumaserver.py",
     "                before_units = dict(cache_units)\n                cache_generation += 1\n                for ip in doomed:\n                    cache_units.pop(ip, None)\n                if not luma_store.atomic_write_json(CACHE_FILE, list(cache_units.values())):\n                    cache_units.clear()\n                    cache_units.update(before_units)\n                    REMOTE.aliases.restore(names[\"before\"])\n                    logging.warning(\"[clear_units]",
     "                before_units = dict(cache_units)\n                for ip in doomed:\n                    cache_units.pop(ip, None)\n                if not luma_store.atomic_write_json(CACHE_FILE, list(cache_units.values())):\n                    cache_units.clear()\n                    cache_units.update(before_units)\n                    REMOTE.aliases.restore(names[\"before\"])\n                    logging.warning(\"[clear_units]",
     "a read that began before the clear can put the cleared units back",
     [py("test_clear_assigned.py", "RaceTests")]),
    ("inventory.names-of-cleared-kept", "lumaserver.py",
     "                names = REMOTE.aliases.forget_many(doomed_identities)\n                if not names[\"ok\"]:\n                    logging.warning(\"[clear_units]",
     "                names = {\"ok\": True, \"removed\": 0, \"before\": {}}\n                if not names[\"ok\"]:\n                    logging.warning(\"[clear_units]",
     "a cleared unit keeps its friendly name",
     [py("test_clear_assigned.py", "RemoteOnTests")]),
    ("inventory.no-rollback-of-names", "lumaserver.py",
     "                    REMOTE.aliases.restore(names[\"before\"])\n                    logging.warning(\"[clear_units] failed to write the inventory; nothing cleared\")",
     "                    logging.warning(\"[clear_units] failed to write the inventory; nothing cleared\")",
     "a failed inventory write leaves the names of units that remain removed",
     [py("test_clear_assigned.py", "RaceTests")]),
    ("inventory.assignments-open-to-users", "luma_access.py",
     "    \"/api/clear_units/assignments\": Access.ADMIN,",
     "    \"/api/clear_units/assignments\": Access.USER,",
     "a restricted user can read what each device is used for",
     [py("test_clear_assigned.py", "ManipulatedRequestTests")]),
    ("inventory.view-ignores-remote-state", "lumaserver.py",
     "    remote_on = bool(REMOTE.enabled())\n    assigned = _inventory_assignments()",
     "    remote_on = True\n    assigned = _inventory_assignments()",
     "the confirmation claims protection while Remote Access is off",
     [py("test_clear_assigned.py", "AssignmentViewTests")]),
    ("inventory.unassigned-clear-not-audited", "lumaserver.py",
     "        REMOTE.store.audit(\"units.cleared_unassigned\", actor=getattr(g, \"principal\", None),",
     "        REMOTE.store.audit(\"units.cleared\", actor=getattr(g, \"principal\", None),",
     "clearing only the unassigned units is recorded as a full clear",
     [py("test_clear_assigned.py", "AuditTests")]),
    ("inventory.device-list-without-summary", "luma_remote_api.py",
     "            depends = luma_inventory.build(rows_now, store, groups, walls)",
     "            depends = {}",
     "the Device names page shows every device as unassigned",
     [py("test_clear_assigned.py", "AssignmentViewTests")]),
    ("inventory.ui.view-assignments-clears", "ui/clear_units.js",
     "            return showAssignments(info.units || []).then(ask);",
     "            return showAssignments(info.units || []).then(send);",
     "closing View Assignments clears the units",
     [node("clear_units_test.js")]),
    ("inventory.ui.third-button-always", "ui/clear_units.js",
     "        if (kept > 0) question.secondary = {text: \"View Assignments\", value: \"assignments\"};",
     "        question.secondary = {text: \"View Assignments\", value: \"assignments\"};",
     "View Assignments is offered when nothing is assigned",
     [node("clear_units_test.js")]),
    ("inventory.ui.no-single-flight", "ui/clear_units.js",
     "    if (inFlight) return Promise.resolve(null);\n",
     "",
     "two clicks open two stacked questions",
     [node("clear_units_test.js")]),
    ("inventory.ui.flight-not-released", "ui/clear_units.js",
     "    function release() { inFlight = false; }",
     "    function release() { }",
     "the first empty or failed answer disables Clear Units for the life of the page",
     [node("clear_units_test.js")]),
    ("inventory.ui.cancel-sends", "ui/clear_units.js",
     "        }).then(function (yes) { return yes === true ? send() : null; });",
     "        }).then(function (yes) { return send(); });",
     "Cancel clears the units",
     [node("clear_units_test.js")]),
    ("inventory.ui.disable-shortcut", "ui/clear_units.js",
     "          confirmText: \"Clear Unassigned Units\", cancelText: \"Cancel\", danger: true",
     "          confirmText: \"Clear Unassigned Units\", cancelText: \"Cancel\", danger: false",
     "the destructive answer is the keyboard default",
     [node("remote_pages_test.js"), node("clear_units_test.js")]),
    ("inventory.ui.dialog-third-is-true", "ui/dialogs.js",
     "      function onExtra() { finish(String(secondary.value)); }",
     "      function onExtra() { finish(true); }",
     "the third action is read as the primary one",
     [node("dialog_field_test.js")]),
    ("inventory.ui.dialog-extra-lingers", "ui/dialogs.js",
     "    extraBtn.hidden = !secondary;\n",
     "    if (secondary) extraBtn.hidden = false;\n",
     "a third button from one caller stays for the next",
     [node("dialog_field_test.js")]),
    ("inventory.ui.device-manager-assumes-empty", "ui/index.html",
     "        if (result && result.scope === \"unassigned\" && result.kept) {",
     "        if (false) {",
     "Device Manager empties its list although assigned units were kept",
     [py("test_frontend_contract.py", "FrontendContractTests")]),
    ("inventory.ui.focus-never-refreshes", "ui/admin_users.js",
     "    if (typeof window.addEventListener === 'function') window.addEventListener('focus', refreshAssignments);\n",
     "",
     "the Device names summary goes stale after a change on another page",
     [node("remote_pages_test.js")]),
    ("inventory.ui.refresh-unthrottled", "ui/admin_users.js",
     "      if (now - lastRefresh < 2000) return;\n",
     "",
     "every focus and visibility event re-reads the inventory",
     [node("remote_pages_test.js")]),
    ("inventory.ui.settings-line-always", "ui/remote_settings.js",
     "    els.protect.hidden = !enabled;",
     "    els.protect.hidden = false;",
     "the protection line is shown while Remote Access is off",
     [node("remote_pages_test.js")]),
    ("restore.absence-ignored", "luma_backup.py",
     "            if section.name in gone:\n",
     "            if False:\n",
     "a record the backup does not hold survives the restore",
     [py("test_backup_rules.py", "ApplyTests", "EndToEndTests")]),
    ("restore.legacy-backup-removes", "luma_backup.py",
     "    if not isinstance(managed, list):\n        return []",
     "    if not isinstance(managed, list):\n        managed = [s.name for s in SECTIONS]",
     "a backup that never said what it managed deletes local configuration",
     [py("test_backup_rules.py", "ApplyTests", "EndToEndTests")]),
    ("restore.managed-not-recorded", "luma_backup.py",
     "        \"managed\": [section.name for section in SECTIONS],",
     "        \"managed\": [],",
     "a new backup does not say what it managed",
     [py("test_backup_rules.py", "RuleTableTests", "EndToEndTests")]),
    ("restore.summary-hides-removal", "luma_backup.py",
     "        \"will_remove\": sections_to_remove(payload, data_dir),",
     "        \"will_remove\": [],",
     "the confirmation does not say what will be removed",
     [py("test_backup_rules.py", "ApplyTests", "EndToEndTests")]),
    ("restore.migration-overwrites-choice", "luma_backup.py",
     "        if \"standard_routing\" in permissions or \"matrix_routing\" in permissions:\n            continue",
     "        if False:\n            continue",
     "migration overwrites an administrator's routing-interface choice",
     [py("test_backup_rules.py", "MigrationTests")]),
    ("restore.migration-grants-matrix", "luma_backup.py",
     "        permissions[\"matrix_routing\"] = False\n        filled += 1",
     "        permissions[\"matrix_routing\"] = True\n        filled += 1",
     "an account from before the setting is handed Matrix routing",
     [py("test_backup_rules.py", "MigrationTests", "EndToEndTests")]),
    ("restore.migration-drops-standard", "luma_backup.py",
     "        permissions[\"standard_routing\"] = True\n        permissions[\"matrix_routing\"] = False",
     "        permissions[\"standard_routing\"] = False\n        permissions[\"matrix_routing\"] = False",
     "an account from before the setting loses Standard routing",
     [py("test_backup_rules.py", "MigrationTests", "EndToEndTests")]),
    ("restore.credentials-removed-by-absence", "luma_backup.py",
     "    Section(\"device_credentials\", \"producer_config.json\", on_absent=\"keep\",",
     "    Section(\"device_credentials\", \"producer_config.json\", on_absent=\"remove\",",
     "a restore deletes the device sign-in because the backup held none",
     [py("test_backup_rules.py", "RuleTableTests", "ApplyTests")]),
    ("restore.remote-removed-by-absence", "luma_backup.py",
     "    Section(\"remote_access\", \"remote_access.json\", on_absent=\"keep\",",
     "    Section(\"remote_access\", \"remote_access.json\", on_absent=\"remove\",",
     "a restore deletes this computer's remote access settings because the backup held none",
     [py("test_backup_rules.py", "RuleTableTests", "ApplyTests")]),
    ("permissions.not-applied-to-live-account", "luma_users.py",
     "            account.permissions = permissions\n            self._persist()\n            for name in",
     "            account.permissions = account.permissions\n            self._persist()\n            for name in",
     "a permission change is not applied to the live account",
     [py("test_account_lifecycle.py", "LivePermissionTests")]),
    ("directlink.throttle-before-token", "luma_remote_api.py",
     "        account = store.resolve_direct_token(token)\n        if account is None:\n            if store.locked_out(_DIRECT_THROTTLE_KEY, source):",
     "        if store.locked_out(_DIRECT_THROTTLE_KEY, source):\n            return Response(_LINK_REFUSED, status=404, mimetype=\"text/html\")\n        account = store.resolve_direct_token(token)\n        if account is None:\n            if store.locked_out(_DIRECT_THROTTLE_KEY, source):",
     "a throttled address refuses the freshly regenerated link too",
     [py("test_account_lifecycle.py", "DirectAccessLifecycleTests")]),
    ("directlink.regenerate-keeps-old-sessions", "luma_users.py",
     "            self.revoke_direct_sessions(user_id, _locked=True)\n            self.audit(event, actor=actor,",
     "            self.audit(event, actor=actor,",
     "regenerating a link leaves the old link's sessions running",
     [py("test_account_lifecycle.py", "DirectAccessLifecycleTests")]),
    ("revision.ignores-group-members", "luma_remote_api.py",
     "                group_rows.append([group[\"id\"], group[\"name\"], list(group[\"members\"])])",
     "                group_rows.append([group[\"id\"], group[\"name\"], []])",
     "an open page is not told that a group's displays changed",
     [py("test_account_lifecycle.py", "LivePermissionTests")]),
    ("persist.stale-write-after-clear", "lumaserver.py",
     "            if generation != cache_generation:\n                logging.info(",
     "            if False:\n                logging.info(",
     "a write that predates a clear resurrects the cleared units",
     [py("test_server_hardening.py", "PersistenceTests")]),
    ("persist.merge-overwrites-memory", "lumaserver.py",
     "                if not ip or ip in cache_units:",
     "                if not ip:",
     "the disk merge replaces live state",
     [py("test_server_hardening.py", "PersistenceTests")]),
    ("store.no-quarantine", "luma_store.py",
     "        os.replace(path, target)",
     "        pass",
     "a malformed file is left in place and read again",
     [py("test_display_groups.py", "GroupStoreTests"), py("test_server_hardening.py", "PersistenceTests")]),
    ("cache.sweep-per-request", "lumaserver.py",
     "        start = (not _cache_verify_state[\"running\"]\n                 and time.monotonic() - _cache_verify_state[\"finished\"] >= CACHE_VERIFY_MIN_INTERVAL)",
     "        start = True",
     "every page load starts its own sweep of every device",
     [py("test_server_hardening.py", "CacheTrafficTests")]),

    # ---- the preview proxy: a device is a stranger on the network -------------------------
    ("preview.follows-redirects", "lumaserver.py",
     "    kwargs[" + Q + "allow_redirects" + Q + "] = False",
     "    kwargs.setdefault(" + Q + "allow_redirects" + Q + ", True)",
     "a device's redirect steers the server to another URL (SSRF)",
     [py("test_preview_proxy.py", "RedirectTests")]),
    ("preview.relays-other-formats", "lumaserver.py",
     "def _valid_jpeg(data: bytes) -> bool:",
     "def _valid_jpeg(data: bytes) -> bool:" + NL + "    return True",
     "a PNG, SVG, GIF or BMP is relayed as image/jpeg",
     [py("test_preview_proxy.py", "ContentTests")]),
    ("preview.address-unchecked", "lumaserver.py",
     "    if not _preview_address_ok(address):" + NL + "        logging.info(" + Q + "[preview] %s: refused",
     "    if False:" + NL + "        logging.info(" + Q + "[preview] %s: refused",
     "a cached address that is not an ordinary device address is fetched from",
     [py("test_preview_proxy.py", "AddressTests")]),
    ("preview.deadline-not-per-byte", "lumaserver.py",
     "    read1 = getattr(resp.raw, " + Q + "read1" + Q + ", None)",
     "    read1 = None",
     "a dribbling device holds a server thread open",
     [py("test_preview_proxy.py", "BoundsTests")]),

    # ---- the local listener answers only to its own address ------------------
    ("host.guard-off", "luma_remote.py",
     "        if self._listener == LOCAL and not is_loopback_host_header(",
     "        if False and not is_loopback_host_header(",
     "a rebound DNS name reaches the local listener with local trust",
     [py("test_host_header.py", "RealListenerTests")]),
    ("host.any-name-is-localhost", "luma_remote.py",
     '    if host == "localhost":',
     "    if True:",
     "any Host header is accepted as this computer",
     [py("test_host_header.py", "HostHeaderTable")]),
    ("host.missing-host-accepted", "luma_remote.py",
     "    host = str(value or " + Q + Q + ").strip().lower()" + NL + "    if not host:" + NL + "        return False",
     "    host = str(value or " + Q + Q + ").strip().lower()" + NL + "    if not host:" + NL + "        return True",
     "a request with no Host header is trusted",
     [py("test_host_header.py", "HostHeaderTable")]),

    # ---- host folders are chosen on the host --------------------------------
    ("config.remote-folder-choice", "lumaserver.py",
     '            if wanted and os.path.abspath(wanted) != os.path.abspath(str(CONFIG.get(key) or "")):',
     '            if False and os.path.abspath(wanted) != os.path.abspath(str(CONFIG.get(key) or "")):',
     "a signed-in remote session names any host folder as an asset folder",
     [py("test_remote_config_paths.py", "FolderChoiceTests")]),
    ("asset.serves-any-file", "producer2_routes.py",
     '    if os.path.splitext(filename)[1].lower() not in _ASSET_IMAGE_TYPES:',
     '    if False:',
     "the picture route hands over a configuration file from the chosen folder",
     [py("test_remote_config_paths.py", "AssetRouteTests")]),
    ("config.hash-to-remote", "lumaserver.py",
     '            "password_hash": pw_hash if luma_remote.is_local_request(request.environ) else "",',
     '            "password_hash": pw_hash,',
     "a remote caller is handed an unsalted hash of the device password",
     [py("test_remote_config_paths.py", "ConfigReadTests")]),

    # ---- a backup header chooses how much work the restore does ----------------
    ("backup.kdf-cost-unbounded", "luma_backup.py",
     '            or n * r * p > SCRYPT_N * SCRYPT_R * SCRYPT_P):',
     '            or False):',
     "a hostile header holds a thread for minutes before the passphrase is checked",
     [py("test_backup_kdf_bounds.py", "Bounds")]),
    ("backup.nonce-size-unchecked", "luma_backup.py",
     '    if (dklen != KEY_BYTES or len(nonce) != NONCE_BYTES',
     '    if (dklen != KEY_BYTES',
     "a wrong-size nonce escapes as a bare ValueError from the cipher",
     [py("test_backup_kdf_bounds.py", "Bounds")]),

    ("backup.salt-size-unchecked", "luma_backup.py",
     '    if len(salt) != SALT_BYTES or not nonce:',
     '    if not salt or not nonce:',
     "a megabyte of salt is hashed before the passphrase is checked",
     [py("test_backup_kdf_bounds.py", "Bounds")]),
    ("backup.header-size-unbounded", "luma_backup.py",
     '    if len(parts[1]) > MAX_HEADER_BYTES:',
     '    if False:',
     "an arbitrarily large header is parsed before anything is verified",
     [py("test_backup_kdf_bounds.py", "Bounds")]),

    ("login.unknown-user-double-derivation", "luma_users.py",
     "        stored = account.password_hash if account else dummy",
     "        stored = account.password_hash if account else hash_password(secrets.token_urlsafe(16))",
     "an unknown username costs two key derivations to a known one's one",
     [py("test_remote_security.py", "LoginHardeningTests")]),

    # ---- what a stranger can make the sign-in remember is bounded ---------------
    ("throttle.empty-record-kept", "luma_users.py",
     "            if recent:",
     "            if True:",
     "every name asked about leaves a record in the throttle",
     [py("test_throttle_bounds.py", "ThrottleBounds")]),
    ("throttle.bounds-removed", "luma_users.py",
     "MAX_RECORDED_USERNAME = 128",
     "MAX_RECORDED_USERNAME = 10 ** 9",
     "a typed name is kept whole in the throttle and the audit",
     [py("test_throttle_bounds.py", "ThrottleBounds")]),
    ("throttle.no-hard-cap", "luma_users.py",
     '            if key not in self._failures and len(self._failures) >= THROTTLE_MAX_KEYS:',
     '            if False:',
     "a flood of distinct names inside the window grows the throttle without limit",
     [py("test_throttle_bounds.py", "ThrottleBounds")]),
    ("throttle.evicts-lockouts", "luma_users.py",
     '                             if len(times) < MAX_FAILURES]',
     '                             if True]',
     "a flood of junk names releases a real lockout to make room",
     [py("test_throttle_bounds.py", "ThrottleBounds")]),
    ("throttle.never-swept", "luma_users.py",
     "            if len(self._failures) >= THROTTLE_SWEEP_AT:",
     "            if False:",
     "expired throttle records are never dropped",
     [py("test_throttle_bounds.py", "ThrottleBounds")]),

    # ---- a group route is a sequence of verified routes ----------------------
    ("groups.members-not-awaited", "luma_groups.py",
     "            outcome = route_one(index, identity)",
     "            threading.Thread(target=route_one, args=(index, identity)).start(); outcome = {" + Q + "ok" + Q + ": True}",
     "members are started without waiting for the previous one to finish",
     [py("test_group_scale.py", "SizeTests"), py("test_display_groups.py", "SequentialRunTests")]),
    ("groups.stops-at-first-failure", "luma_groups.py",
     "        results.append(dict(outcome, index=index))",
     "        results.append(dict(outcome, index=index))" + NL + "        if not outcome.get(" + Q + "ok" + Q + "): break",
     "one failed display stops every display after it",
     [py("test_display_groups.py", "SequentialRunTests")]),
    ("groups.parallel-entry-failure-escapes", "luma_groups.py",
     "            outcome = {\"ok\": False, \"reason\": \"error\"}\n        return dict(outcome, index=index, t_start=started",
     "            raise\n        return dict(outcome, index=index, t_start=started",
     "one display that raises in a parallel route fails the whole batch instead of only itself",
     [py("test_group_scale.py", "SizeTests")]),

    # ---- where a remote caller may make the server connect ----------------------
    ("dest.gate-not-wired", "luma_wiring.py",
     "        luma_gate.install(app, lambda: self.store, self.enabled, destination_check)",
     "        luma_gate.install(app, lambda: self.store, self.enabled, None)",
     "the destination rule is never consulted for a remote request",
     [py("test_remote_destinations.py")]),
    ("dest.control-accepts-unknown", "luma_destinations.py",
     "    if text not in known:",
     "    if False:",
     "a stored credential is sent to an address the server never found",
     [py("test_remote_destinations.py")]),
    ("dest.discovery-allows-public", "luma_destinations.py",
     "                or address.is_reserved or address.is_global",
     "                or address.is_reserved",
     "a remote scan may probe the public Internet with the scan credential",
     [py("test_remote_destinations.py")]),
    ("dest.accepts-ipv6", "luma_destinations.py",
     "        return ipaddress.IPv4Address(text)",
     "        return ipaddress.ip_address(text)",
     "an IPv6 literal is treated as a valid destination",
     [py("test_remote_destinations.py")]),
    ("dest.list-checks-first-only", "luma_destinations.py",
     "        for item in value:",
     "        for item in value[:1]:",
     "only the first address of a list is judged; the rest are contacted",
     [py("test_remote_destinations.py")]),
    ("dest.ip-key-forgotten", "luma_destinations.py",
     '    "ip", "ips", "targets", "decoder_ip", "encoder_ip", "source_ip",',
     '    "ips", "targets", "decoder_ip", "encoder_ip", "source_ip",',
     "the most common body key is no longer examined",
     [py("test_remote_destinations.py")]),
    ("dest.ws-redirect-followed", "lumaserver.py",
     NL + "            ws = create_connection(url, timeout=timeout, redirect_limit=0)",
     NL + "            ws = create_connection(url, timeout=timeout)",
     "a device's redirect carries the credential to wherever it points",
     [py("test_remote_destinations.py")]),
    ("dest.http-redirect-followed", "lumaserver.py",
     'verify=bool(CONFIG.get("http_verify_tls", False)), allow_redirects=False)',
     'verify=bool(CONFIG.get("http_verify_tls", False)))',
     "a 307 re-posts the old and new device passwords to the Location host",
     [py("test_remote_destinations.py")]),
    ("dest.expansion-materialises", "lumaserver.py",
     "ips.extend(str(h) for h in itertools.islice(net.hosts(), 4096))",
     "ips.extend(str(h) for h in list(net.hosts())[:4096])",
     "a wide prefix is built in full before being cut down",
     [py("test_remote_destinations.py")]),

    # ---- a restore is the old configuration or the new one -------------------
    ("restore.created-files-kept", "luma_backup.py",
     "    names = ([section.filename for section in SECTIONS] if present is not None",
     "    names = (list(touched) if present is not None",
     "a file the restore created is left behind by its own rollback",
     [py("test_restore_atomicity.py")]),
    ("restore.stale-snapshot-kept", "luma_backup.py",
     "    for stale in [section.filename for section in SECTIONS] + [MANIFEST]:",
     "    for stale in []:",
     "an earlier restore's snapshot files are left to mislead a manual recovery",
     [py("test_restore_atomicity.py")]),
    ("restore.rollback-in-place", "luma_backup.py",
     "                _atomic_copy(saved, target)",
     "                shutil.copy2(saved, target)",
     "an interrupted rollback truncates the file it was recovering",
     [py("test_restore_atomicity.py")]),
    ("restore.stores-not-held", "lumaserver.py",
     "        with REMOTE.store._lock, REMOTE.aliases._lock, REMOTE.groups._lock:",
     "        if True:",
     "an account, name or group change in the middle writes the old state over the restore",
     [py("test_restore_atomicity.py")]),
    ("restore.other-writes-allowed", "lumaserver.py",
     "    if not (_restore_in_progress or _restart_required):",
     "    if True:",
     "configuration is written by anything while a restore runs",
     [py("test_restore_atomicity.py")]),
    ("restore.restart-not-latched", "lumaserver.py",
     "        _restart_required = True\n        return jsonify(",
     "        _restart_required = False\n        return jsonify(",
     "after a reload failure the next save overwrites the restored files",
     [py("test_restore_atomicity.py")]),
    ("restore.incomplete-rollback-claimed", "lumaserver.py",
     '        status = "FAILED \\u2014 ROLLED BACK" if not failures else',
     '        status = "FAILED \\u2014 ROLLED BACK" if True else',
     "a rollback that failed is reported as having succeeded",
     [py("test_restore_atomicity.py")]),
    ("restore.concurrent-restore", "lumaserver.py",
     "    if not _restore_lock.acquire(blocking=False):",
     "    if False:",
     "two restores run at once",
     [py("test_restore_atomicity.py")]),

    # ---- HTTPS and remote access ----------------------------------------------------------
    ("tls.plaintext-without-asking", "luma_remote_api.py",
     "        if transport == \"http\" and data.get(\"accept_insecure\") is not True:",
     "        if False:",
     "plain HTTP is reachable without an explicit acknowledgement",
     [py("test_remote_access_api.py", "EnablingTests")]),
    ("tls.truthy-value-acknowledges-plaintext", "luma_remote_api.py",
     "        if transport == \"http\" and data.get(\"accept_insecure\") is not True:",
     "        if transport == \"http\" and not data.get(\"accept_insecure\"):",
     "a string such as \"false\" or \"yes\", or the number 1, turns plain HTTP on",
     [py("test_remote_access_api.py", "EnablingTests")]),
    ("tls.acknowledgement-is-not-needed-when-absent", "luma_remote_api.py",
     "        if transport == \"http\" and data.get(\"accept_insecure\") is not True:",
     "        if transport == \"http\" and \"accept_insecure\" not in data:",
     "any value at all, including null, acknowledges plain HTTP",
     [py("test_remote_access_api.py", "EnablingTests")]),
    ("tls.downgrade-on-error", "luma_remote_api.py",
     "        if tls_error:\n            # Never start in the clear because TLS could not be arranged.",
     "        if False:\n            # Never start in the clear because TLS could not be arranged.",
     "HTTPS that cannot be arranged starts anyway",
     [py("test_remote_access_api.py", "EnablingTests")]),
    ("tls.cookie-never-secure", "luma_remote_api.py",
     "            httponly=True, samesite=\"Lax\",\n            secure=request.is_secure, path=\"/\")\n        return response",
     "            httponly=True, samesite=\"Lax\",\n            secure=False, path=\"/\")\n        return response",
     "the session cookie is never marked Secure over TLS",
     [py("test_remote_security.py", "LoginHardeningTests")]),
    ("cert.matches-anything", "luma_tls.py",
     "def covers(described, address) -> bool:",
     "def covers(described, address) -> bool:\n    return True",
     "any certificate is taken to cover any address",
     [py("test_remote_access_api.py", "CertificateTests")]),
    ("cert.uses-common-name", "luma_tls.py",
     "def covers(described, address) -> bool:",
     "def covers(described, address) -> bool:\n    return described.get(\"subject\") == address",
     "the common name decides validity",
     [py("test_remote_access_api.py", "CertificateTests")]),
    ("restore.accepts-missing-admin", "luma_wiring.py",
     "        if not self.store.has_admin:\n            self.restore_error",
     "        if False:\n            self.restore_error",
     "remote access is restored with nobody able to sign in",
     [py("test_remote_access_api.py", "RestoreAtStartupTests")]),

    # ---- backup ---------------------------------------------------------------------------
    ("backup.weak-passphrase", "luma_backup.py",
     "    if not isinstance(passphrase, str) or len(passphrase) < MIN_PASSPHRASE:",
     "    if not isinstance(passphrase, str):",
     "a backup passphrase of any length is accepted",
     [py("test_server_hardening.py", "BackupContainerTests")]),
    ("backup.key-is-carried", "luma_backup.py",
     "    Section(\"production_studio\", \"producer_state.json\"),",
     "    Section(\"production_studio\", \"producer_state.json\"),\n    Section(\"private_key\", \"remote_key.pem\"),",
     "the private key is carried in a backup",
     [py("test_server_hardening.py", "BackupContainerTests")]),
    ("backup.restore-not-local", "luma_access.py",
     '    "/api/backup/restore": Access.LOCAL_ONLY,',
     '    "/api/backup/restore": Access.ADMIN,',
     "a remote administrator can restore a backup",
     [py("test_access_fence.py", "RouteClassificationFenceTests"), py("test_server_hardening.py", "BackupRestoreEndpointTests")]),

    # ---- the browser ----------------------------------------------------------------------
    ("ui.user-page-polls", "ui/user.js",
     "  start();\n}());",
     "  setInterval(refresh, 5000);\n  start();\n}());",
     "the routing page polls",
     [node("user_page_test.js")]),
    ("ui.user-page-innerhtml", "ui/user.js",
     "    host.replaceChildren();\n    if (!state.sources.length) {",
     "    host.innerHTML = '';\n    if (!state.sources.length) {",
     "the routing page builds markup from device text",
     [node("user_page_test.js")]),
    ("ui.route-single-flight", "ui/user.js",
     "    var free = chosen.filter(function (t) { return !state.routing[targetKey(t)]; });",
     "    var free = chosen.slice();",
     "a target that is still being written to is sent a second request",
     [node("user_page_test.js"), node("user_route_states_test.js")]),
    ("ui.group-left-out-of-request", "ui/user.js",
     "                groups: free.filter(function (t) { return t.kind === 'group'; }).map(function (t) { return t.id; }),",
     "                groups: [],",
     "a chosen group is left out of the request",
     [node("user_page_test.js")]),
    ("ui.status-code-shown", "ui/user.js",
     "    return (body && body.error) || 'That did not work.';\n  }",
     "    return 'Error ' + res.status;\n  }",
     "a status number is shown instead of a sentence",
     [node("user_page_test.js")]),
    ("ui.login-open-redirect", "ui/login.js",
     "    var safe = /^\\/(?!\\/)[A-Za-z0-9/_.-]*(\\?[A-Za-z0-9/_.=&%-]*)?$/;",
     "    var safe = /^.*$/;",
     "sign-in follows any next address",
     [node("remote_pages_test.js")]),
    ("ui.csrf-to-other-origins", "ui/csrf.js",
     "    if (!UNSAFE[method] || !sameOrigin(input)",
     "    if (!UNSAFE[method]",
     "the CSRF token is sent to other origins",
     [node("remote_pages_test.js")]),
    ("ui.delete-without-confirmation", "ui/admin_users.js",
     "          if (yes) act(user, 'delete', {});",
     "          act(user, 'delete', {});",
     "Cancel does not stop an account deletion",
     [node("remote_pages_test.js")]),
    ("ui.confirm-falls-back-to-native", "ui/admin_users.js",
     "    if (!window.lumaConfirm) return Promise.resolve(false);",
     "    if (!window.lumaConfirm) return Promise.resolve(true);",
     "a missing confirmation approves the action",
     [node("remote_pages_test.js")]),
    ("ui.plaintext-acknowledged-implicitly", "ui/remote_settings.js",
     "transport: transport, accept_insecure: transport === 'http'}}",
     "transport: transport, accept_insecure: true}}",
     "the page always acknowledges plain HTTP",
     [node("remote_pages_test.js")]),
    ("ui.remote-sees-password-eye", "ui/remote_settings.js",
     "      if (node) node.hidden = !isLocal;",
     "      if (node) node.hidden = false;",
     "a remote administrator is offered controls that only work locally",
     [node("remote_pages_test.js")]),
    # ---- unification pass: routed scans, breakaway, retention, firmware schedule ---------
    ("scan.range-count-unbounded", "luma_scanplan.py",
     "    if len(parts) > MAX_RANGES:",
     "    if False:",
     "any number of ranges accepted",
     [py("test_unification.py", "ScanPlanTests", "ScanEndpointTests")]),
    ("scan.per-range-bound-off", "luma_scanplan.py",
     "        if count > MAX_RANGE_ADDRESSES:",
     "        if False:",
     "a /8 or /16 is accepted as one range",
     [py("test_unification.py", "ScanPlanTests", "ScanEndpointTests")]),
    ("scan.total-bound-off", "luma_scanplan.py",
     "        if total > MAX_SCAN_ADDRESSES:",
     "        if False:",
     "many legal ranges add up to an unbounded scan",
     [py("test_unification.py", "ScanPlanTests", "ScanEndpointTests")]),
    ("breakaway.audio-name-unchecked", "luma_breakaway.py",
     "        if want_audio not in known:",
     "        if False:",
     "an audio name the decoder does not list is written and subscribes to nothing",
     [py("test_unification.py", "BreakawayTests", "BreakawayEndpointTests")]),
    ("breakaway.no-address-accepted", "luma_breakaway.py",
     '_NO_ADDRESS = ("", "0.0.0.0", None)',
     "_NO_ADDRESS = ()",
     "a 0.0.0.0 audio readback reads as a real subscription",
     [py("test_unification.py", "BreakawayTests", "BreakawayEndpointTests")]),
    ("route.matrix-open-to-users", "luma_access.py",
     '    "/api/route_matrix": Access.ADMIN,',
     '    "/api/route_matrix": Access.AUTHENTICATED,',
     "a restricted User can call the administrator breakaway route",
     [py("test_unification.py", "BreakawayEndpointTests"), py("test_access_fence.py")]),
    ("destinations.encoder-ip-keys-ungated", "luma_destinations.py",
     '    "destination_ip", "device_ip", "video_encoder_ip", "audio_encoder_ip",',
     '    "destination_ip", "device_ip",',
     "breakaway's second and third device addresses skip the remote destination gate",
     [py("test_unification.py", "BreakawayEndpointTests"), py("test_remote_destinations.py")]),
    ("retention.days-bound-off", "luma_retention.py",
     '    if not MIN_RETENTION_DAYS <= result["days"] <= MAX_RETENTION_DAYS:',
     "    if False:",
     "any retention period is accepted",
     [py("test_unification.py", "RetentionRuleTests", "RetentionApiTests")]),
    ("retention.size-bound-off", "luma_retention.py",
     '    if not MIN_MAX_BYTES <= result["max_bytes"] <= MAX_MAX_BYTES:',
     "    if False:",
     "any log size cap is accepted",
     [py("test_unification.py", "RetentionRuleTests", "RetentionApiTests")]),
    ("firmware.interval-shortened", "lumaserver.py",
     "HALL_FIRMWARE_CHECK_INTERVAL = 7 * 24 * 3600",
     "HALL_FIRMWARE_CHECK_INTERVAL = 24 * 3600",
     "the firmware check runs daily, not every seven days",
     [py("test_unification.py", "FirmwareScheduleTests")]),
    ("firmware.always-retry-interval", "lumaserver.py",
     "        wait = HALL_FIRMWARE_RETRY_INTERVAL if failed else HALL_FIRMWARE_CHECK_INTERVAL",
     "        wait = HALL_FIRMWARE_RETRY_INTERVAL",
     "a successful check is repeated every retry interval, hammering the source",
     [py("test_unification.py", "FirmwareScheduleTests")]),
    # ---- Check Now, the Preview column, the passphrase dialog, the version status (fork G) ----------
    ("firmware.check-now-open-to-everyone", "luma_access.py",
     '    "/api/firmware_status/check": Access.ADMIN,',
     '    "/api/firmware_status/check": Access.AUTHENTICATED,',
     "any signed-in account can make the application ask Hall Research",
     [py("test_firmware_check_now.py", "CheckNowTests")]),
    ("firmware.check-now-overlaps", "lumaserver.py",
     "    if _hall_check_gate.locked():\n        return False\n    worker = threading.Thread(",
     "    worker = threading.Thread(",
     "a second press while a check runs reports that it started one",
     [py("test_firmware_check_now.py", "CheckNowTests")]),
    ("firmware.gate-held-after-a-failed-check", "lumaserver.py",
     "    try:\n        _fetch_hall_firmware_catalog()\n    finally:\n        _hall_check_gate.release()\n    return True",
     "    _fetch_hall_firmware_catalog()\n    _hall_check_gate.release()\n    return True",
     "the first failed check disables Check Now for the life of the process",
     [py("test_firmware_check_now.py", "CheckNowTests")]),
    ("firmware.running-never-reported", "lumaserver.py",
     '"error": last_error, "stale": stale, "running": _hall_check_gate.locked(),',
     '"error": last_error, "stale": stale, "running": False,',
     "the page cannot tell that a check is still in progress",
     [py("test_firmware_check_now.py", "CheckNowTests")]),
    ("firmware.reading-starts-a-check", "lumaserver.py",
     "    whatever is already known (possibly nothing yet).\"\"\"\n    _ensure_hall_firmware_lookup_started()",
     "    whatever is already known (possibly nothing yet).\"\"\"\n    _ensure_hall_firmware_lookup_started()\n    start_explicit_hall_check()",
     "opening Settings asks Hall Research",
     [py("test_firmware_check_now.py", "CheckNowTests")]),
    ("ui.check-now-second-press-posts", "ui/firmware-summary.js",
     "    if (!button || button.disabled) return Promise.resolve(null);",
     "    if (!button) return Promise.resolve(null);",
     "pressing Check Now twice asks twice",
     [node("firmware_check_test.js")]),
    ("ui.check-now-failure-reads-as-success", "ui/firmware-summary.js",
     "    if (look.state === 'unavailable' || look.stale || !fresh) {",
     "    if (false) {",
     "a check that could not reach Hall Research is announced as complete",
     [node("firmware_check_test.js")]),
    ("ui.check-now-watch-unbounded", "ui/firmware-summary.js",
     "  var POLL_LIMIT = 30;",
     "  var POLL_LIMIT = 3000;",
     "a check that never finishes is watched forever",
     [node("firmware_check_test.js")]),
    ("ui.settings-open-checks", "ui/firmware-summary.js",
     "  document.addEventListener('luma:settings-open', function () { refresh(); });",
     "  document.addEventListener('luma:settings-open', function () { check(); });",
     "opening Settings starts a check",
     [node("firmware_check_test.js")]),
    ("unitpreview.hover-ignores-the-switch", "ui/unit-preview.js",
     "      if (stateOf(cell) !== 'on') return;\n      var url = thumbURL(cell.dataset && cell.dataset.ip);\n      if (!url) return;\n      ensure();",
     "      var url = thumbURL(cell.dataset && cell.dataset.ip);\n      if (!url) return;\n      ensure();",
     "hovering asks a unit with its thumbnail off for a picture",
     [node("unit_preview_test.js")]),
    ("unitpreview.popup-ignores-the-switch", "ui/unit-preview.js",
     "    if (state !== 'on') { env.say(whyNot(state)); return 'refused'; }",
     "    if (false) { env.say(whyNot(state)); return 'refused'; }",
     "the popup opens for a unit with its thumbnail off",
     [node("unit_preview_test.js")]),
    ("unitpreview.refresh-overlaps", "ui/unit-preview.js",
     "        if (stopped || busy) return false;",
     "        if (stopped) return false;",
     "a slow unit is asked again before it answered, piling requests up",
     [node("unit_preview_test.js")]),
    ("unitpreview.hover-timer-survives-close", "ui/unit-preview.js",
     "      if (timer) { env.clearInterval(timer); timer = null; }\n      if (loader) { loader.stop(); loader = null; }",
     "      if (loader) { loader.stop(); loader = null; }",
     "closing the hover card leaves its refresh running",
     [node("unit_preview_test.js")]),
    ("unitpreview.address-unchecked", "ui/unit-preview.js",
     "    return IPV4.test(String(ip)) ? 'http://' + ip + '/thumbnail/thumbnail1.jpg' : '';",
     "    return 'http://' + ip + '/thumbnail/thumbnail1.jpg';",
     "text that is not an address becomes part of a request and of the popup's script",
     [node("unit_preview_test.js")]),
    ("unitpreview.popup-script-breakout", "ui/unit-preview.js",
     "    var literal = JSON.stringify(String(url)).replace(/</g, '\\\\u003c');",
     "    var literal = JSON.stringify(String(url));",
     "an address can end the popup's script element",
     [node("unit_preview_test.js")]),
    ("unitpreview.popup-refresh-survives-close", "ui/unit-preview.js",
     "      'addEventListener(\"pagehide\",function(){clearInterval(h)})})();' +",
     "      '})();' +",
     "closing the popup window leaves its refresh running",
     [node("unit_preview_test.js")]),
    ("unitpreview.word-not-keyboard-reachable", "ui/index.html",
     "      pv.setAttribute('role', 'button'); pv.tabIndex = 0;",
     "",
     "the word Preview cannot be reached from the keyboard",
     [node("preview_checkbox_test.js")]),
    ("backup.controls-offered-remotely", "ui/remote_settings.js",
     "b2.hidden = !isLocal; b2.disabled = !isLocal; } });",
     "b2.hidden = false; b2.disabled = false; } });",
     "a remote administrator is offered Export and Restore",
     [node("remote_pages_test.js")]),
    ("backup.short-passphrase-accepted", "ui/remote_settings.js",
     "      if (pass.length < BACKUP_MIN_PASSPHRASE) {",
     "      if (false) {",
     "a backup is made under a passphrase shorter than the rule",
     [node("remote_pages_test.js")]),
    ("backup.restore-without-the-decision", "ui/remote_settings.js",
     "            if (!yes) { backupStatus('Nothing was changed.'); return null; }",
     "            if (false) { backupStatus('Nothing was changed.'); return null; }",
     "Cancel on the restore decision restores anyway",
     [node("remote_pages_test.js")]),
    ("dialog.passphrase-kept-in-the-box", "ui/dialogs.js",
     "          fieldInput.value = \"\";\n        }\n        if (result === true && options.suppressKey",
     "        }\n        if (result === true && options.suppressKey",
     "the typed passphrase stays in the dialog after it closes",
     [node("dialog_field_test.js")]),
    ("dialog.passphrase-delivered-on-third-action", "ui/dialogs.js",
     "          if (result === true && typeof field.onValue === \"function\") field.onValue(fieldInput.value);",
     "          if (result && typeof field.onValue === \"function\") field.onValue(fieldInput.value);",
     "the third action hands the typed passphrase to the caller",
     [node("dialog_field_test.js")]),
    ("dialog.passphrase-delivered-on-cancel", "ui/dialogs.js",
     "          if (result === true && typeof field.onValue === \"function\") field.onValue(fieldInput.value);",
     "          if (typeof field.onValue === \"function\") field.onValue(fieldInput.value);",
     "a cancelled dialog still hands the passphrase to the caller",
     [node("dialog_field_test.js")]),
    ("settings.current-not-green", "ui/settings.js",
     "      statusEl.className = \"update-status ok\";",
     "      statusEl.className = \"update-status\";",
     "an application that is current is not shown as current",
     [node("settings_parity_test.js")]),
    ("settings.failed-check-reads-current", "ui/settings.js",
     "    } else if (data.state === \"current\") {",
     "    } else if (data.state !== \"update_available\") {",
     "a check that could not say is shown as up to date",
     [node("settings_parity_test.js")]),
    ("settings.update-available-not-red", "ui/family.css",
     ".update-status.update-available { color: var(--state-error-text);",
     ".update-status.update-available { color: var(--state-warning-text);",
     "an available application update is not shown in the error colour",
     [py("test_state_text_contrast.py", "StateTextRulesUseTheTokensTests")]),
    # ---- one source to several displays and groups ----------------------------------------
    ("multi.display-duplicates-kept", "luma_remote_api.py",
     "            if item not in found:\n                found.append(item)",
     "            found.append(item)",
     "a display named twice in the request is routed twice",
     [py("test_user_multi.py", "DestinationResolutionTests")]),
    ("multi.group-member-duplicates-kept", "luma_remote_api.py",
     "                if member not in ordered:\n                    ordered.append(member)",
     "                ordered.append(member)",
     "a display that is in a chosen group and chosen on its own is routed twice",
     [py("test_user_multi.py", "DestinationResolutionTests")]),
    ("multi.source-grant-skipped", "luma_remote_api.py",
     MULTI_CHECK_HEAD +
     "            if not account.permissions.may_use_source(source_id):\n"
     "                return \"source_not_permitted\"\n"
     "            for identity in display_ids:",
     MULTI_CHECK_HEAD +
     "            for identity in display_ids:",
     "a source the account was not given is routed",
     [py("test_user_multi.py", "PermissionTests", "HandlerSecondLineTests")]),
    ("multi.display-grant-skipped", "luma_remote_api.py",
     MULTI_CHECK_BODY +
     "            for identity in display_ids:\n"
     "                if not account.permissions.may_control_destination(identity):",
     MULTI_CHECK_BODY +
     "            for identity in []:\n"
     "                if not account.permissions.may_control_destination(identity):",
     "a display the account was not given is routed (a group member picked on its own)",
     [py("test_user_multi.py", "PermissionTests", "HandlerSecondLineTests")]),
    ("multi.group-grant-skipped", "luma_remote_api.py",
     MULTI_CHECK_BODY +
     "            for identity in display_ids:\n"
     "                if not account.permissions.may_control_destination(identity):\n"
     "                    return \"destination_not_permitted\"\n"
     "            for group_id in group_ids:\n"
     "                if not account.permissions.may_control_group(group_id):",
     MULTI_CHECK_BODY +
     "            for identity in display_ids:\n"
     "                if not account.permissions.may_control_destination(identity):\n"
     "                    return \"destination_not_permitted\"\n"
     "            for group_id in []:\n"
     "                if not account.permissions.may_control_group(group_id):",
     "a group the account was not given is routed because it holds a granted display",
     [py("test_user_multi.py", "PermissionTests", "HandlerSecondLineTests")]),
    ("multi.stops-at-first-failure", "luma_groups.py",
     "        results.append(dict(outcome, index=index))\n",
     "        results.append(dict(outcome, index=index))\n"
     "        if not outcome.get(\"ok\"):\n            break\n",
     "the destinations after a failed one are never attempted",
     [py("test_user_multi.py", "SequencingTests"), py("test_display_groups.py")]),
    ("multi.list-length-unbounded", "luma_remote_api.py",
     "        if not isinstance(value, list) or len(value) > limit:",
     "        if not isinstance(value, list):",
     "an unbounded list of displays is expanded",
     [py("test_user_multi.py", "RequestShapeTests")]),
    ("multi.gate-ignores-display-lists", "luma_gate.py",
     '"decoder", "decoder_id", "displays")',
     '"decoder", "decoder_id")',
     "the gate no longer reads the list of displays in a request",
     [py("test_user_multi.py", "PermissionTests")]),
    ("multi.gate-ignores-group-lists", "luma_gate.py",
     'GROUP_KEYS = ("group", "group_id", "groups")',
     'GROUP_KEYS = ("group", "group_id")',
     "the gate no longer reads the list of groups in a request",
     [py("test_user_multi.py", "PermissionTests")]),
    ("multi.route-closed-to-users", "luma_access.py",
     '    "/api/user/route_multi": Access.USER,',
     '    "/api/user/route_multi": Access.ADMIN,',
     "a restricted User cannot reach the multi-display route",
     [py("test_user_multi.py", "RouteTableTests", "SequencingTests"), py("test_access_fence.py")]),
    # ---- previews ------------------------------------------------------------------------
    ("preview.switcher-is-asked-for-a-picture", "lumaserver.py",
     "            return []\n        return [thumb]",
     "            return [thumb]\n        return [thumb]",
     "a CS31 is asked for a picture it does not serve",
     [py("test_user_multi.py", "SwitcherPreviewTests")]),
    ("preview.counters-on-by-default", "luma_previewstats.py",
     '    return os.environ.get("LUMA_PREVIEW_STATS", "") == "1"',
     "    return True",
     "preview traffic counters are on without being asked for",
     [py("test_user_multi.py", "PreviewCounterTests")]),
    ("preview.counters-readable-remotely", "luma_access.py",
     '    "/api/preview_stats": Access.LOCAL_ONLY,',
     '    "/api/preview_stats": Access.AUTHENTICATED,',
     "any signed-in account can read the preview counters",
     [py("test_user_multi.py", "PreviewCounterTests")]),
    ("preview.counter-table-unbounded", "luma_previewstats.py",
     "        if label in _counts or len(_counts) < _MAX_KEYS:",
     "        if True:",
     "the counter table grows without bound",
     [py("test_user_multi.py", "PreviewCounterTests")]),
    # ---- the restricted page --------------------------------------------------------------
    ("userpage.picture-fetched-again-on-click", "ui/user.js",
     "      if (state.preview && have) { shot.setAttribute('data-have', '1'); paint(shot, have); }\n",
     "",
     "choosing a card asks the device for the picture it already has",
     [node("user_page_test.js")]),
    ("userpage.preview-off-keeps-pictures", "ui/user.js",
     "        shot.replaceChildren();\n        shot.classList.add('empty');\n        shot.setAttribute('data-note', 'Preview off');\n",
     "        shot.setAttribute('data-note', 'Preview off');\n",
     "switching Preview off leaves the pictures on screen",
     [node("user_page_test.js")]),
    ("userpage.source-is-multi-select", "ui/user.js",
     "      state.source = state.source === id ? '' : id;",
     "      state.source = id;",
     "choosing the chosen source again no longer clears it",
     [node("user_page_test.js")]),
    ("userpage.displays-sent-in-reverse", "ui/user.js",
     "      state.targets = state.targets.concat([{kind: kind, id: id}]);",
     "      state.targets = [{kind: kind, id: id}].concat(state.targets);",
     "destinations are sent in the opposite order to the one chosen",
     [node("user_page_test.js")]),
    # ---- saved video walls, offline units, the five-second pictures and the drop ------------
    ("wall.perm-any", "luma_users.py",
     "        return _normalise_group_id(wall_id) in self.walls",
     "        return True",
     "every saved wall is permitted",
     [py("test_user_walls.py", "PermissionModelTests", "RoutingTests")]),
    ("wall.perm-is-the-group-list", "luma_users.py",
     "        return _normalise_group_id(wall_id) in self.walls",
     "        return _normalise_group_id(wall_id) in self.groups",
     "a wall is judged by the display-group grant",
     [py("test_user_walls.py", "PermissionModelTests", "RoutingTests")]),
    ("wall.grant-not-persisted", "luma_users.py",
     '                "groups": sorted(self.groups),\n                "walls": sorted(self.walls),',
     '                "groups": sorted(self.groups),',
     "a wall grant is lost the next time the accounts are saved",
     [py("test_user_walls.py", "PermissionModelTests", "LifecycleTests")]),
    ("wall.gate-ignores-walls", "luma_gate.py",
     '    for wall_id in _identities_in(payload, WALL_KEYS):\n        if not account.permissions.may_control_wall(wall_id):\n            return Decision(False, 403, "wall_not_permitted", account)\n',
     "",
     "the gate does not read the walls a request names",
     [py("test_user_walls.py", "RoutingTests")]),
    ("wall.handler-ignores-walls", "luma_remote_api.py",
     '            for wall_id in wall_ids:\n                if not account.permissions.may_control_wall(wall_id):\n                    return "wall_not_permitted"\n            return None',
     "            return None",
     "the routing handler does not check the wall grant itself",
     [py("test_user_walls.py", "SecondLineTests")]),
    ("wall.shown-to-every-user", "luma_projection.py",
     "            if not admin and not perms.may_control_wall(wid):\n                continue\n",
     "",
     "every saved wall is listed to every user",
     [py("test_user_walls.py", "ProjectionTests")]),
    ("wall.members-listed-to-users", "luma_projection.py",
     '            wall_rows.append({"id": wid, "name": str(wall.get("name") or "Video Wall"),\n                              "members": total,',
     '            wall_rows.append({"id": wid, "name": str(wall.get("name") or "Video Wall"), "member_ids": identities,\n                              "members": total,',
     "a wall's decoders are listed to a user",
     [py("test_user_walls.py", "ProjectionTests")]),
    ("wall.revision-ignores-walls", "luma_remote_api.py",
     '        extra = {"groups": group_rows, "walls": wall_rows,',
     '        extra = {"groups": group_rows,',
     "a rename, a membership change or a deleted wall does not reach an open User page",
     [py("test_user_walls.py", "LifecycleTests")]),
    ("wall.revision-ignores-members", "luma_walls.py",
     '            [[int(c.get("row") or 0), int(c.get("column") or 0), identity or str(c.get("decoder_id"))]\n             for c, identity, _u in rows],',
     "            [],",
     "a wall that swaps one display for another looks unchanged to an open page",
     [py("test_user_walls.py", "LifecycleTests")]),
    ("wall.grants-survive-a-deleted-wall", "lumaserver.py",
     "    # A grant to a wall that is gone must not remain: the User loses it at once.\n    _forget_missing_wall_grants()\n",
     "",
     "a grant outlives the wall it names",
     [py("test_user_walls.py", "TheRealRoutesTests")]),
    ("wall.grants-survive-an-import", "lumaserver.py",
     '    _save_video_wall_state(data)\n    _forget_missing_wall_grants()\n',
     '    _save_video_wall_state(data)\n',
     "an imported set that drops a wall leaves the grants to it",
     [py("test_user_walls.py", "TheRealRoutesTests")]),
    ("offline.is-online-ignores-liveness", "luma_projection.py",
     "    if luma_liveness.is_offline(unit):\n        return False\n",
     "",
     "a unit that stopped answering is still called online",
     [py("test_user_walls.py", "OfflineTests", "TheRealRoutesTests")]),
    ("offline.one-miss-is-offline", "luma_liveness.py",
     "MISS_LIMIT = 2",
     "MISS_LIMIT = 1",
     "a single dropped read disables a working unit",
     [py("test_user_walls.py", "OfflineTests", "TheRealRoutesTests")]),
    ("offline.answer-does-not-clear", "luma_liveness.py",
     '    misses = 0 if answered else int(previous.get("misses") or 0) + 1',
     '    misses = int(previous.get("misses") or 0) + (0 if answered else 1)',
     "a unit that answers again stays offline",
     [py("test_user_walls.py", "OfflineTests", "TheRealRoutesTests")]),
    ("offline.refresh-does-not-record", "lumaserver.py",
     "        with cache_lock:\n            luma_liveness.record(unit, answered)\n",
     "",
     "the shared refresh never notes which units answered",
     [py("test_user_walls.py", "TheRealRoutesTests")]),
    ("offline.multi-source-not-checked", "luma_remote_api.py",
     '            return jsonify({"ok": False, "error": "too many displays in one request"}), 400\n        if _is_offline(source_id):\n            return _offline_refusal(source_id)',
     '            return jsonify({"ok": False, "error": "too many displays in one request"}), 400',
     "a route from an offline source is attempted",
     [py("test_user_walls.py", "OfflineTests")]),
    ("offline.single-route-not-checked", "luma_remote_api.py",
     "                if _is_offline(source_id) or _is_offline(destination_id):\n                    return _offline_refusal(source_id, destination_id)\n",
     "",
     "a single route to an offline display is attempted",
     [py("test_user_walls.py", "OfflineTests")]),
    ("offline.admin-route-not-checked", "lumaserver.py",
     '    if _offline_among(decoder_ip, encoder_ip):\n        return jsonify({"ok": False, "reason": "device_offline", "status": "offline",\n                        "error": "That device is offline."}), 409\n',
     "",
     "the administrator's matrix routes to or from an offline unit",
     [py("test_user_walls.py", "TheRealRoutesTests")]),
    ("offline.admin-breakaway-not-checked", "lumaserver.py",
     '        if _offline_among(*needed):\n            return jsonify({"ok": False, "reason": "device_offline", "status": "offline",\n                            "error": "That device is offline."}), 409\n',
     "",
     "the administrator's breakaway route reaches an offline unit",
     [py("test_user_walls.py", "TheRealRoutesTests")]),
    ("offline.wall-page-writes-offline-display", "lumaserver.py",
     '    if unit.get("online") is False:\n        # A display that is not answering is never written to, whoever asks.',
     '    if False:\n        # A display that is not answering is never written to, whoever asks.',
     "the Video Wall page writes to a display that is not answering",
     [py("test_user_walls.py", "TheRealRoutesTests")]),
    ("offline.wall-apply-continues-after-offline", "lumaserver.py",
     "    if not unit or source_result.get(\"offline\"):\n        return source_result",
     "    if not unit:\n        return source_result",
     "applying a wall also sets bezel and rotation on an offline display",
     [py("test_user_walls.py", "TheRealRoutesTests")]),
    ("offline.served-units-lack-the-flag", "lumaserver.py",
     '        served = [dict(u, online=not luma_projection.is_offline(u)) for u in rows]\n',
     '        served = [dict(u) for u in rows]\n',
     "the matrix is not told which units are offline",
     [py("test_user_walls.py", "TheRealRoutesTests")]),
    ("userpage.wall-omitted-from-request", "ui/user.js",
     "      if (sent.walls.length) body.walls = sent.walls;",
     "",
     "routing to a wall sends no wall",
     [node("user_page_test.js")]),
    ("userpage.everything-is-available", "ui/user.js",
     "    return kind === 'group' || kind === 'wall' ? item.online_members > 0 : !!item.online;",
     "    return true;",
     "an offline source, display, group or wall can be chosen and dropped on",
     [node("user_page_test.js")]),
    ("userpage.drop-accepted-on-offline", "ui/user.js",
     "      if (!online || !canDrop(kind)) return;       // not preventDefault: the drop is refused",
     "      if (!canDrop(kind)) return;",
     "an offline destination accepts a dragged source",
     [node("user_page_test.js")]),
    ("userpage.offline-source-draggable", "ui/user.js",
     "      if (!online) return;\n      button.setAttribute('draggable', 'true');",
     "      button.setAttribute('draggable', 'true');",
     "an offline source can be dragged",
     [node("user_page_test.js")]),
    ("userpage.drop-routes-the-selection-not-the-target", "ui/user.js",
     "      routeTo(held.source, [{kind: kind, id: item.id}]);",
     "      routeTo(held.source, state.targets.concat([{kind: kind, id: item.id}]));",
     "a drop also routes whatever happened to be selected",
     [node("user_page_test.js")]),
    ("userpage.drop-reads-identity-from-the-event", "ui/user.js",
     "      routeTo(held.source, [{kind: kind, id: item.id}]);",
     "      routeTo((event.dataTransfer && event.dataTransfer.getData('text/plain')) || held.source, [{kind: kind, id: item.id}]);",
     "the dragged source is taken from the drag event instead of the page",
     [node("user_page_test.js")]),
    ("userpage.preview-off-keeps-the-timer", "ui/user.js",
     "      if (observer) { observer.disconnect(); observer = null; }\n      stopPictureWork();\n      visibleShots = {};",
     "      if (observer) { observer.disconnect(); observer = null; }\n      visibleShots = {};",
     "turning Preview off leaves the picture timer running",
     [node("user_page_test.js")]),
    ("userpage.hidden-page-keeps-the-timer", "ui/user.js",
     "      if (document.hidden) { stopPictureWork(); return; }\n      refreshNow(Object.keys(visibleShots));",
     "      if (document.hidden) { return; }\n      refreshNow(Object.keys(visibleShots));",
     "a hidden page keeps refreshing pictures",
     [node("user_page_test.js")]),
    ("userpage.pagehide-keeps-the-timer", "ui/user.js",
     "    window.addEventListener('pagehide', stopPictureWork);",
     "",
     "leaving the page leaves the picture timer running",
     [node("user_page_test.js")]),
    ("userpage.pictures-not-single-flight", "ui/user.js",
     "    if (shotInflight[key]) {\n      // Its turn has not come yet",
     "    if (false) {\n      // Its turn has not come yet",
     "a slow device is asked again before it has answered",
     [node("user_page_test.js")]),
    ("userpage.route-does-not-refresh-pictures", "ui/user.js",
     "      refreshNow(ids.map(function (id) { return 'display:' + id; }));",
     "",
     "a route leaves the routed display's old picture on screen until the next tick",
     [node("user_page_test.js")]),
    ("userpage.refresh-timer-doubles", "ui/user.js",
     "    stopShotTimer();\n    startShotTimer();\n  }\n\n  function showNoPreview",
     "    startShotTimer();\n    shotTimer = null;\n    startShotTimer();\n  }\n\n  function showNoPreview",
     "every route adds another picture timer",
     [node("user_page_test.js")]),
    ("userpage.offscreen-cards-still-refresh", "ui/user.js",
     "        if (!entry.isIntersecting) { delete visibleShots[key]; return; }",
     "        if (!entry.isIntersecting) { return; }",
     "cards scrolled out of view keep refreshing",
     [node("user_page_test.js")]),
    ("userpage.cadence-forces-fresh", "ui/user.js",
     "    Object.keys(visibleShots).forEach(function (key) { refreshShot(key, false); });",
     "    Object.keys(visibleShots).forEach(function (key) { refreshShot(key, true); });",
     "the cadence defeats the server's shared picture cache",
     [node("user_page_test.js")]),
    ("adminwalls.permissions-omit-walls", "ui/admin_users.js",
     "                       groups: pick.groups.values(), walls: pick.walls.values(),",
     "                       groups: pick.groups.values(),",
     "a wall ticked in the editor is never saved",
     [node("admin_walls_test.js")]),
    ("adminwalls.walls-share-the-display-list", "ui/admin_users.js",
     "    pick.walls = checklist('assign_walls',",
     "    pick.walls = checklist('assign_displays',",
     "walls are chosen in the displays list",
     [node("admin_walls_test.js")]),
    ("matrix.offline-cell-selectable", "ui/matrix.js",
     "${offline ? ' disabled aria-disabled=\"true\"' : ''}>${dot}",
     ">${dot}",
     "a matrix cell with an offline end can be chosen",
     [node("matrix_indicator_test.js")]),
    ("userpage.rebuild-forgets-what-is-visible", "ui/user.js",
     "    Object.keys(visibleShots).forEach(function (key) { if (!present[key]) delete visibleShots[key]; });",
     "    visibleShots = {};",
     "a route's own re-render forgets which pictures are on screen, so the routed display is not refreshed at once",
     [node("user_page_test.js")]),
    ("userpage.highlight-drops-on-child-leave", "ui/user.js",
     "      if (depth === 0) button.classList.remove('drop-over');",
     "      button.classList.remove('drop-over');",
     "the destination loses its highlight as the pointer crosses its own children",
     [node("user_page_test.js")]),
    ("offline.unrecorded-status-is-offline", "luma_projection.py",
     '    return bool(status) and status != "idle"',
     '    return status != "idle"',
     "a unit with no recorded status is refused by the administrator's gates",
     [py("test_user_walls.py", "TheRealRoutesTests")]),
    ("offline.explicit-offline-status-ignored", "luma_projection.py",
     '    return bool(status) and status != "idle"',
     "    return False",
     "a unit whose status says it is not idle is still routed to by the administrator",
     [py("test_user_walls.py", "TheRealRoutesTests")]),
]

# The routing-interface choice and the User Matrix keep their mutations beside their tests.
sys.path.insert(0, os.path.join(ROOT, "tests"))
from mutations_matrix import MATRIX_MUTATIONS  # noqa: E402
MUTATIONS += MATRIX_MUTATIONS
from mutations_routing import ROUTING_MUTATIONS  # noqa: E402
MUTATIONS += ROUTING_MUTATIONS
from mutations_integrity import INTEGRITY_MUTATIONS  # noqa: E402
MUTATIONS += INTEGRITY_MUTATIONS
from mutations_requests import REQUEST_MUTATIONS  # noqa: E402
MUTATIONS += REQUEST_MUTATIONS
from mutations_restore import RESTORE_MUTATIONS, CANARIES  # noqa: E402
MUTATIONS += RESTORE_MUTATIONS

NIGHT_A_MUTATIONS = [
    ('remove.selected-ignores-assignments', 'lumaserver.py',
     '    if REMOTE.enabled():\n        return _remove_selected_unassigned(ips_to_remove)\n    try:',
     '    try:',
     'Remove Selected removes units Remote Access is using',
     [py("test_remove_selected.py", "RemoteOnTests")]),
    ('remove.selected-no-409', 'lumaserver.py',
     '    if protected and not doomed:\n        body.update',
     '    if False and protected and not doomed:\n        body.update',
     'Remove Selected reports success when every selected unit was kept',
     [py("test_remove_selected.py", "RemoteOnTests")]),
    ('remove.selected-lock-dropped', 'lumaserver.py',
     '    wanted = [ip for ip in ips_to_remove if isinstance(ip, str)]\n    with REMOTE.store.hold(), REMOTE.groups.hold(), luma_store.lock_for(VIDEO_WALL_STATE_FILE):',
     '    wanted = [ip for ip in ips_to_remove if isinstance(ip, str)]\n    with luma_store.lock_for(VIDEO_WALL_STATE_FILE):',
     'an assignment added while Remove Selected decides is not waited for',
     [py("test_remove_selected.py", "RaceTests")]),
    ('remove.selected-names-not-restored', 'lumaserver.py',
     '                    cache_units.update(before_units)\n                    REMOTE.aliases.restore(names["before"])\n                    logging.warning("[remove_units] failed to write the inventory; "',
     '                    cache_units.update(before_units)\n                    logging.warning("[remove_units] failed to write the inventory; "',
     'a failed inventory write loses the friendly names of units that stay',
     [py("test_remove_selected.py", "FailureTests")]),
    ('remove.selected-audit-dropped', 'lumaserver.py',
     '    if doomed or protected:\n        try:\n            REMOTE.store.audit("units.removed_unassigned"',
     '    if False:\n        try:\n            REMOTE.store.audit("units.removed_unassigned"',
     'a Remove Selected leaves no activity entry',
     [py("test_remove_selected.py", "AuditTests")]),
    ('inventory.wall-member-reason-dropped', 'luma_inventory.py',
     '                    record["wall_reasons"].add("Member of wall " + label)\n',
     '',
     'a wall member does not say why it is protected',
     [py("test_remove_selected.py", "WallSummaryTests")]),
    ('inventory.wall-source-reason-dropped', 'luma_inventory.py',
     '                    record["wall_reasons"].add("Source of wall " + label)\n',
     '',
     "a wall's source does not say why it is protected",
     [py("test_remove_selected.py", "WallSummaryTests")]),
    ('inventory.wall-access-dropped', 'luma_inventory.py',
     '                    record["wall_reasons"].add("Member of wall " + label)\n                    record["wall_access"].update(holders)\n',
     '                    record["wall_reasons"].add("Member of wall " + label)\n',
     'the accounts holding a wall are not named beside its members',
     [py("test_remove_selected.py", "WallSummaryTests")]),
    ('remove.ui.cancel-sends', 'ui/clear_units.js',
     '        if (yes !== true) return null;\n        return api("/api/remove_units"',
     '        return api("/api/remove_units"',
     'Cancel removes the selected units',
     [node("remove_selected_test.js")]),
    ('remove.ui.no-single-flight', 'ui/clear_units.js',
     '    if (removing) return Promise.resolve(null);\n',
     '',
     'two clicks open two stacked questions',
     [node("remove_selected_test.js")]),
    ('remove.ui.flight-not-released', 'ui/clear_units.js',
     '    function release() { removing = false; }',
     '    function release() { }',
     'the first answer disables Remove Selected for the life of the page',
     [node("remove_selected_test.js")]),
    ('remove.ui.kept-not-shown', 'ui/clear_units.js',
     'function afterwards(value) { return kept.length ? showAssignments(kept, {',
     'function afterwards(value) { return false ? showAssignments(kept, {',
     'a unit Remote Access kept is not explained',
     [node("remove_selected_test.js")]),
    ('remove.ui.refusal-reported-done', 'ui/clear_units.js',
     '          if (!r || !r.ok) {\n            onError(body.error || "That did not work.");\n            return afterwards(null);\n          }\n          onDone(body);',
     '          if (false) {\n            onError(body.error || "That did not work.");\n            return afterwards(null);\n          }\n          onDone(body);',
     'a refusal is reported as a removal',
     [node("remove_selected_test.js")]),
    ('remove.ui.wording-leaks', 'ui/clear_units.js',
     '    backdrop.querySelector("h3").textContent = options.title || DEFAULT_TITLE;\n',
     '',
     "one caller's heading stays on the next caller's view",
     [node("remove_selected_test.js")]),
]
MUTATIONS += NIGHT_A_MUTATIONS

NIGHT_A_RESTORE_MUTATIONS = [
    ('restore.sessions-not-revoked', 'lumaserver.py',
     '        ended = REMOTE.store.revoke_sessions_outdated_by(fingerprints)\n',
     '        ended = {"gone": 0, "password": 0, "direct": 0}\n',
     'a session made with a replaced password or link survives the restore',
     [py("test_restore_sessions.py", 'CredentialsRestoredToOtherValuesTests')]),
    # AccountsTheBackupDoesNotHoldTests is deliberately not listed above: a removed account's
    # session is ended by three independent layers (the restore's revocation, the per-request
    # account lookup, and the caller's refusal of a missing account), so no single mutation can
    # break it. It guards the outward behaviour; it is not mutation-proven, and says so here.
    ('restore.accounts-kept-when-absent', 'luma_backup.py',
     '    Section("accounts", "users.json", on_absent="remove",',
     '    Section("accounts", "users.json", on_absent="keep",',
     'a backup with no accounts leaves the current accounts in place',
     [py("test_restore_sessions.py", 'ABackupWithNoAccountsTests')]),
    ('restore.removed-accounts-unnamed', 'luma_backup.py',
     '        if key not in incoming:\n            out["removed"].append(names[key])\n',
     '        if key not in incoming:\n            pass\n',
     'the inspection does not name the accounts that will be removed',
     [py("test_restore_sessions.py", 'TheRestoreSaysWhatItWillDoToAccountsTests'), py("test_restore_sessions.py", 'ABackupWithNoAccountsTests')]),
    ('restore.replaced-accounts-unnamed', 'luma_backup.py',
     '        elif _account_sig(incoming[key]) != _account_sig(current[key]):\n',
     '        elif False:\n',
     'the inspection does not name the accounts a restore replaces',
     [py("test_restore_sessions.py", 'TheRestoreSaysWhatItWillDoToAccountsTests')]),
    ('restore.removal-note-dropped', 'luma_backup.py',
     '    if changes["removed"]:\n        notes.append(',
     '    if False:\n        notes.append(',
     'the confirmation carries no sentence about accounts being removed',
     [py("test_restore_sessions.py", 'TheRestoreSaysWhatItWillDoToAccountsTests')]),
    ('restore.recovery-note-untrue', 'lumaserver.py',
     '            "No administrator account exists now. On this computer, open LumaSuite and create one "\n',
     '            "If nobody knows the restored administrator passwords, set a new one in Accounts on this computer. "\n',
     'the recovery note does not say that no administrator exists',
     [py("test_restore_sessions.py", 'ABackupWithNoAccountsTests')]),
    ('restore.ui.accounts-row-dropped', 'ui/remote_settings.js',
     "          if ((accounts.removed || []).length) {\n            rows.push({label: 'Accounts removed'",
     "          if (false) {\n            rows.push({label: 'Accounts removed'",
     'the restore confirmation does not list the accounts it removes',
     [node("remote_pages_test.js")]),
    ('restore.ui.message-silent', 'ui/remote_settings.js',
     '                     ((accounts.removed || []).length\n',
     '                     ((accounts.removed || []).length && false\n',
     'the restore question does not say accounts will be removed',
     [node("remote_pages_test.js")]),
]
MUTATIONS += NIGHT_A_RESTORE_MUTATIONS

NIGHT_A_HAMMER_MUTATIONS = [
    ('night.noop-clear-bumps-generation', 'lumaserver.py',
     '            if doomed:\n                names = REMOTE.aliases.forget_many(doomed_identities)\n                if not names["ok"]:\n                    logging.warning("[clear_units] device names',
     '            if True:\n                names = REMOTE.aliases.forget_many(doomed_identities)\n                if not names["ok"]:\n                    logging.warning("[clear_units] device names',
     'a Clear that removes nothing discards a scan that is still running',
     [py("test_night_inventory.py", "GenerationTests")]),
    ('night.remove-selected-keeps-generation', 'lumaserver.py',
     '                before_units = dict(cache_units)\n                cache_generation += 1\n                for ip in doomed:\n                    cache_units.pop(ip, None)\n                if not luma_store.atomic_write_json(CACHE_FILE, list(cache_units.values())):\n                    cache_units.clear()\n                    cache_units.update(before_units)\n                    REMOTE.aliases.restore(names["before"])\n                    logging.warning("[remove_units]',
     '                before_units = dict(cache_units)\n                for ip in doomed:\n                    cache_units.pop(ip, None)\n                if not luma_store.atomic_write_json(CACHE_FILE, list(cache_units.values())):\n                    cache_units.clear()\n                    cache_units.update(before_units)\n                    REMOTE.aliases.restore(names["before"])\n                    logging.warning("[remove_units]',
     'a scan that began before Remove Selected puts the removed units back',
     [py("test_night_inventory.py", "GenerationTests")]),
    ('hammer.audit-carries-password', 'luma_users.py',
     '                self.audit("user.create", actor=actor,\n                           detail={"username": username, "role": role})',
     '                self.audit("user.create", actor=actor,\n                           detail={"username": username, "role": role, "note": password})',
     'the activity record carries the password an account was created with',
     [py("test_secrets_hammer.py", "SecretsHammerTests")]),
    ('hammer.audit-carries-link-token', 'luma_users.py',
     '                       detail={"username": account.username,\n                               "protection": protection})',
     '                       detail={"username": account.username,\n                               "protection": protection, "note": token})',
     'the activity record carries a Direct Access token',
     [py("test_secrets_hammer.py", "SecretsHammerTests")]),
    ('hammer.request-lines-logged', 'app_launcher.py',
     '            log.setLevel(logging.ERROR)',
     '            log.setLevel(logging.INFO)',
     'request lines, which carry a link token, are written to the log',
     [py("test_secrets_hammer.py", "SecretsHammerTests")]),
    ('hammer.restore-skips-rollback', 'luma_backup.py',
     'def rollback(',
     'def _unused_rollback(',
     'a failed restore cannot put the files back',
     [py("test_restore_failure_stages.py", "EveryStageTests")]),
]
MUTATIONS += NIGHT_A_HAMMER_MUTATIONS

# ---- a page read does not wait on a unit that does not answer ------------------------------
LATENCY_MUTATIONS = [
    ("latency.silence-tries-every-path", "lumaserver.py",
     "        if silent or time.monotonic() >= deadline:",
     "        if time.monotonic() >= deadline:",
     "a unit that never answers is asked on every path",
     [py("test_matrix_latency.py", "WsCallSilenceTests")]),
    ("latency.no-call-deadline", "lumaserver.py",
     "        if silent or time.monotonic() >= deadline:",
     "        if silent:",
     "a unit that rejects every path slowly holds the call for every path",
     [py("test_matrix_latency.py", "WsCallSilenceTests")]),
    ("latency.rejection-ends-call", "lumaserver.py",
     '            if isinstance(e, (TimeoutError, OSError)) or type(e).__name__ == "WebSocketTimeoutException":',
     "            if True:",
     "a unit that says 'not this path' is never asked on the right one",
     [py("test_matrix_latency.py", "WsCallSilenceTests")]),
    ("latency.audio-asked-after-silence", "lumaserver.py",
     "                         if resp is not None else None)",
     "                         if True else None)",
     "a silent encoder is asked a second question and costs the refresh twice as much",
     [py("test_matrix_latency.py", "RefreshCostTests")]),
    ("latency.page-read-waits-for-sweep", "lumaserver.py",
     '    return _observe_cache.get_bounded("matrix", _refresh_matrix_state_core, OBSERVE_WAIT_SECONDS)',
     '    return _observe_cache.get_bounded("matrix", _refresh_matrix_state_core, OBSERVE_WAIT_SECONDS * 1000)',
     "a page read waits for the whole device sweep again",
     [py("test_matrix_latency.py", "ServerObservationTests")]),
    ("latency.stale-refresh-stored", "luma_statecache.py",
     "            if not failed and (self._epoch, self._generation.get(key, 0)) == generation:",
     "            if not failed:",
     "a read that began before a write is handed to the next caller as current",
     [py("test_matrix_latency.py", "BoundedObservationTests")]),
    ("latency.readers-each-refresh", "luma_statecache.py",
     "            waiting = self._inflight.get(key)\n            if waiting is None:\n                waiting = threading.Event()\n"
     "                self._inflight[key] = waiting\n                worker = threading.Thread(",
     "            waiting = None\n            if waiting is None:\n                waiting = threading.Event()\n"
     "                self._inflight[key] = waiting\n                worker = threading.Thread(",
     "twelve open pages cost twelve device sweeps",
     [py("test_matrix_latency.py", "BoundedObservationTests")]),
    ("latency.state-hides-age", "luma_remote_api.py",
     '        body["routing"] = _interfaces_of(account)\n        if isinstance(observed, dict):',
     '        body["routing"] = _interfaces_of(account)\n        if False:',
     "the Standard page is not told how old what it shows is",
     [py("test_matrix_latency.py", "ObservedBodyTests")]),
    ("latency.matrix-hides-age", "luma_remote_api.py",
     '        body["rev"] = _revision_for(account)\n        if isinstance(observed, dict):',
     '        body["rev"] = _revision_for(account)\n        if False:',
     "the Matrix page is not told how old what it shows is",
     [py("test_matrix_latency.py", "ObservedBodyTests")]),
]
MUTATIONS += LATENCY_MUTATIONS

# ---- the Matrix route when permission is withdrawn while it waits for the lock -----------------
ADVERSARIAL_MUTATIONS = [
]
MUTATIONS += ADVERSARIAL_MUTATIONS

# ---- routing against a model that holds state: the final state is the assertion ---------------
TORTURE_MUTATIONS = [
    ("torture.audio-not-remembered", "lumaserver.py",
     '                _remember_decoder_audio(decoder_ip, audio_resp["result"])',
     "                pass",
     "a routed display's audio is not recorded, so the matrix draws the dot on the old source",
     [py("test_user_av_convergence.py", "TheCacheAgreesWithTheDeviceTests")]),
    ("torture.audio-name-kept-stale", "lumaserver.py",
     '        held["streamname"] = name.strip()',
     '        held["streamname"] = held.get("streamname") or name.strip()',
     "the audio a scan saw outlives the audio the display now reports",
     [py("test_user_av_convergence.py", "TheCacheAgreesWithTheDeviceTests")]),
    ("torture.follow-flag-dropped", "lumaserver.py",
     '            held["followvideoenable"] = follows',
     "            pass",
     "the cache forgets whether a display's audio follows its video",
     [py("test_user_av_convergence.py", "TheCacheAgreesWithTheDeviceTests")]),
    ("torture.display-grant-never-checked", "luma_remote_api.py",
     MULTI_CHECK_HEAD +
     '            if not account.permissions.may_use_source(source_id):\n'
     '                return "source_not_permitted"\n'
     '            for identity in display_ids:',
     MULTI_CHECK_HEAD +
     '            if not account.permissions.may_use_source(source_id):\n'
     '                return "source_not_permitted"\n'
     '            for identity in ():',
     "a display the account lost is still written to",
     [py("test_routing_torture.py", "PermissionBetweenRoutesTests")]),
    ("torture.silence-asked-again", "lumaserver.py",
     "        if silent or time.monotonic() >= deadline:",
     "        if time.monotonic() >= deadline:",
     "a unit that never answers is asked once per path on the real transport",
     [py("test_routing_torture.py", "TransportFailureTests")]),
]
MUTATIONS += TORTURE_MUTATIONS

# ---- a restricted User's A/V: both planes, one implementation, breakaway preserved ------------
AV = "test_user_av_convergence.py"
AV_MUTATIONS = [
    ("av.users-route-bypasses-the-engine", "lumaserver.py",
     "        if not is_switcher:\n"
     "            outcome = _route_breakaway(decoder_ip, luma_breakaway.MODE_USER_AV,",
     "        if False:\n"
     "            outcome = _route_breakaway(decoder_ip, luma_breakaway.MODE_USER_AV,",
     "a User's A/V goes back to moving the video alone and leaves a breakaway's audio behind",
     [py(AV, "ConvergedAVTests")]),
    ("av.audio-not-written-when-not-following", "luma_breakaway.py",
     "            # Audio first, as in a breakaway, so a failure leaves the video where it was.\n"
     '            if before["follow"] is not False or before["audio"] != want_audio:',
     "            # Audio first, as in a breakaway, so a failure leaves the video where it was.\n"
     "            if False:",
     "a display left not following keeps its audio on the old source after an A/V route",
     [py(AV, "ConvergedAVTests")]),
    ("av.follow-rewritten-on-a-following-display", "luma_breakaway.py",
     'mode == MODE_USER_AV and before["follow"] is not True)',
     "mode == MODE_USER_AV)",
     "an A/V route writes audio and turns off follow on a display that was following video",
     [py(AV, "ConvergedAVTests")]),
    ("av.audio-never-verified", "luma_breakaway.py",
     '            if after["audio"] != want_audio or after["follow"] is not False:\n'
     '                return False, "readback_mismatch"\n'
     '        elif after["follow"] is not True:',
     '            if after["follow"] is not False:\n'
     '                return False, "readback_mismatch"\n'
     '        elif after["follow"] is not True:',
     "an A/V route is called verified although the display did not take the audio",
     [py(AV, "GroupsOfflineAndPartialFailureTests")]),
    ("av.source-without-audio-not-set-aside", "luma_breakaway.py",
     "        if not want_audio:\n"
     "            # Decided before anything is written: the caller may route the video the ordinary way.",
     "        if False:\n"
     "            # Decided before anything is written: the caller may route the video the ordinary way.",
     "a source with no audio stream can no longer be routed at all",
     [py(AV, "WhatThePlanesCanDoTests")]),
    ("av.audio-not-put-back-after-a-failed-video", "luma_breakaway.py",
     '        ok = dec.set_audio(before["audio"], before["follow"]) and ok',
     "        pass",
     "a failed A/V route leaves the audio it already moved on the new source",
     [py(AV, "WhatThePlanesCanDoTests")]),
    ("av.users-mode-offered-to-the-administrator", "luma_breakaway.py",
     "MODES = (MODE_AV, MODE_VIDEO, MODE_AUDIO, MODE_BREAKAWAY)\n",
     'MODES = (MODE_AV, MODE_VIDEO, MODE_AUDIO, MODE_BREAKAWAY, "user_av")\n',
     "the administrator's endpoint accepts the restricted User's route as a mode",
     [py(AV, "BreakawayIsPreservedTests")]),
    ("av.engine-cache-audio-not-recorded", "lumaserver.py",
     '\n                audio["streamname"] = after.get("audio", "")\n',
     "\n                pass\n",
     "the matrix draws a User's audio dot on the source the display held before the route",
     [py(AV, "TheCacheAgreesWithTheDeviceTests")]),
    ("av.engine-cache-follow-not-recorded", "lumaserver.py",
     '\n                    audio["followvideoenable"] = after["follow"]\n                audio["ip"]',
     '\n                    pass\n                audio["ip"]',
     "the cache forgets whether the display's audio follows its video after a route",
     [py(AV, "TheCacheAgreesWithTheDeviceTests")]),
    ("av.rollback-writes-audio-that-follows", "luma_breakaway.py",
     '                     or (before["follow"] is not True and current["audio"] != before["audio"]))',
     '                     or current["audio"] != before["audio"])',
     "a rolled-back route on a following display writes an audio setting that changes nothing",
     [py("test_routing_torture.py", "FailureInjectionTests")]),
    ("av.refusal-says-nothing", "luma_remote_api.py",
     '(_REFUSED_SENTENCES.get(body["reason"]) if status == "refused" else None)',
     "None",
     "a route refused because the display cannot receive that audio says only that it did not work",
     [py(AV, "WhatThePlanesCanDoTests")]),
]
MUTATIONS += AV_MUTATIONS

# ---- the administrator's Matrix page asks the devices once for every page, and not while hidden -----
ADMIN_POLL_MUTATIONS = [
    ("poll.refresh-sweeps-per-request", "lumaserver.py",
     '        updated = _observe_cache.get("matrix", _refresh_matrix_state_core)',
     "        updated = _refresh_matrix_state_core()",
     "every open Matrix page costs the devices a sweep of its own again",
     [py("test_admin_matrix_poll.py", "SharedSweepTests")]),
    ("poll.force-ignored", "lumaserver.py",
     '    if isinstance(body, dict) and body.get("force") is True:\n'
     '        _observe_cache.invalidate("matrix")',
     '    if False:\n'
     '        _observe_cache.invalidate("matrix")',
     "the Refresh button is answered from a read that is already a few seconds old",
     [py("test_admin_matrix_poll.py", "SharedSweepTests")]),
    ("poll.any-truthy-force", "lumaserver.py",
     '    if isinstance(body, dict) and body.get("force") is True:',
     '    if isinstance(body, dict) and body.get("force"):',
     "a field that merely looks true forces a sweep",
     [py("test_admin_matrix_poll.py", "SharedSweepTests")]),
    ("poll.route-keeps-the-remembered-read", "lumaserver.py",
     '    finally:\n'
     '        _observe_cache.invalidate("matrix")          # the next read sees what this just did',
     '    finally:\n'
     '        pass          # the next read sees what this just did',
     "the read after an administrator's plain route is served from before it",
     [py("test_admin_matrix_poll.py", "AdministratorRouteTests")]),
    ("poll.mode-route-keeps-the-remembered-read", "lumaserver.py",
     '        finally:\n'
     '            _observe_cache.invalidate("matrix")      # the next read sees what this just did',
     '        finally:\n'
     '            pass      # the next read sees what this just did',
     "the read after an administrator's named-mode route is served from before it",
     [py("test_admin_matrix_poll.py", "AdministratorRouteTests")]),
    ("poll.hidden-page-keeps-polling", "ui/matrix.js",
     "    if (document.hidden) return;\n    refreshAndRenderMatrix();\n  }, 10000);",
     "    refreshAndRenderMatrix();\n  }, 10000);",
     "a Matrix tab nobody is looking at sweeps the lab every ten seconds",
     [py("test_admin_matrix_poll.py", "PageHiddenTests")]),
    ("poll.return-does-not-read", "ui/matrix.js",
     "    if (!document.hidden) refreshAndRenderMatrix();\n  });",
     "    if (!document.hidden) { /* nothing */ }\n  });",
     "a page brought back to the front shows what it knew when it was hidden until the next tick",
     [py("test_admin_matrix_poll.py", "PageHiddenTests")]),
    ("poll.refresh-button-not-forced", "ui/matrix.js",
     "      await refreshAndRenderMatrix(true);",
     "      await refreshAndRenderMatrix();",
     "the Refresh button is answered from a shared read that may be several seconds old",
     [py("test_admin_matrix_poll.py", "PageHiddenTests")]),
]
MUTATIONS += ADMIN_POLL_MUTATIONS

# ---- a CS31 switcher is read by the matrix refresh, so it is online while it answers -----------
SWITCHER_POLL_MUTATIONS = [
    ("switcher.poll-branch-by-role-only", "lumaserver.py",
     "            if device_type == 'switcher' or 'decoder' in role:",
     "            if 'decoder' in role:",
     "the refresh never reads a switcher (its type is not a decoder role), so it counts a miss every sweep",
     [py("test_switcher_poll.py")]),
    ("switcher.answered-tied-to-streamname", "lumaserver.py",
     '                        answered = True\n'
     '                        name = resp["result"].get("streamname")\n'
     '                        if isinstance(name, str) and name.strip():\n'
     '                            streamname = name.strip()',
     '                        name = resp["result"].get("streamname")\n'
     '                        if isinstance(name, str) and name.strip():\n'
     '                            streamname = name.strip()\n'
     '                            answered = True',
     "an idle switcher with no stream selected reads as not answering and goes offline",
     [py("test_switcher_poll.py", "AnsweringSwitcherTests", "UserRouteTests")]),
    ("switcher.poll-uses-the-decoder-call", "lumaserver.py",
     '                    resp = _ws_call_switcher_no_password(ip, "SelectVideoStream.Get", {}, poll_timeout)',
     '                    resp = _ws_call_auth(ip, "SelectVideoStream.Get", {"password": CONFIG.get("password", "password")}, poll_timeout)',
     "a switcher is sent the password-bearing decoder call it does not take",
     [py("test_switcher_poll.py", "ReadOnceTests")]),
    ("switcher.poll-reads-twice", "lumaserver.py",
     '                    resp = _ws_call_switcher_no_password(ip, "SelectVideoStream.Get", {}, poll_timeout)',
     '                    _ws_call_switcher_no_password(ip, "SelectVideoStream.Get", {}, poll_timeout)\n'
     '                    resp = _ws_call_switcher_no_password(ip, "SelectVideoStream.Get", {}, poll_timeout)',
     "a second round trip to every switcher on every refresh",
     [py("test_switcher_poll.py", "ReadOnceTests")]),
    ("switcher.non-object-reply-counts-as-answer", "lumaserver.py",
     '                    resp = _ws_call_switcher_no_password(ip, "SelectVideoStream.Get", {}, poll_timeout)\n'
     '                    if resp and "result" in resp and isinstance(resp["result"], dict):',
     '                    resp = _ws_call_switcher_no_password(ip, "SelectVideoStream.Get", {}, poll_timeout)\n'
     '                    if resp and "result" in resp:',
     "a reply that is not an object is taken for an answer",
     [py("test_switcher_poll.py", "AnsweringSwitcherTests")]),
]
MUTATIONS += SWITCHER_POLL_MUTATIONS

# ---- pictures on the User page never take every connection a route needs ---------------------------
PICTURE_CAP = [node("user_page_test.js")]
PICTURE_CAP_MUTATIONS = [
    ("picturecap.no-cap", "ui/user.js",
     "      if (pictureActive < PICTURE_MAX) { entry.run(); return; }",
     "      { entry.run(); return; }",
     "seven cards would put seven requests in flight and a route would wait behind slow pictures",
     PICTURE_CAP),
    ("picturecap.cap-is-six", "ui/user.js",
     "  var PICTURE_MAX = 3;",
     "  var PICTURE_MAX = 6;",
     "the pictures could again take every connection the browser allows",
     PICTURE_CAP),
    ("picturecap.slot-never-released", "ui/user.js",
     "          pictureActive--;\n          var next = pictureWaiting.shift();",
     "          var next = pictureWaiting.shift();",
     "after three pictures nothing would ever be fetched again",
     PICTURE_CAP),
    ("picturecap.queue-not-in-order", "ui/user.js",
     "          var next = pictureWaiting.shift();",
     "          var next = pictureWaiting.pop();",
     "the cards asked for first would be the last to get a picture",
     PICTURE_CAP),
    ("picturecap.failure-keeps-the-turn", "ui/user.js",
     "        started.then(done, function () { done(null); });",
     "        started.then(done);",
     "one picture that failed would hold its turn and the rest would never be fetched",
     PICTURE_CAP),
    ("picturecap.release-strands-the-queue", "ui/user.js",
     "    pictureWaiting.splice(0).forEach(function (entry) { entry.skip(); });",
     "    pictureWaiting.splice(0);",
     "a released picture would never be answered, so its card could never be asked for again",
     PICTURE_CAP),
    ("picturecap.hidden-keeps-the-queue", "ui/user.js",
     "      if (document.hidden) { stopPictureWork(); return; }\n      refreshNow(",
     "      if (document.hidden) { stopShotTimer(); return; }\n      refreshNow(",
     "a hidden page would go on fetching every picture that was waiting",
     PICTURE_CAP),
    ("picturecap.pagehide-keeps-the-queue", "ui/user.js",
     "    window.addEventListener('pagehide', stopPictureWork);",
     "    window.addEventListener('pagehide', stopShotTimer);",
     "a page that was left would go on fetching every picture that was waiting",
     PICTURE_CAP),
    ("picturecap.sign-out-keeps-the-queue", "ui/user.js",
     "  function toLogin() {\n    stopPictureWork();",
     "  function toLogin() {\n    stopShotTimer();",
     "a session that ended would go on fetching pictures it can no longer be given",
     PICTURE_CAP),
    ("picturecap.preview-off-keeps-the-queue", "ui/user.js",
     "      stopPictureWork();\n      visibleShots = {};",
     "      stopShotTimer();\n      visibleShots = {};",
     "switching Preview off would not stop the pictures already waiting",
     PICTURE_CAP),
    ("picturecap.route-picture-waits-its-turn", "ui/user.js",
     "      if (fresh && promotePicture(key)) { shotFresh[key] = true; return; }",
     "      if (false && promotePicture(key)) { shotFresh[key] = true; return; }",
     "the picture of a display a route just changed would queue behind every cadence picture",
     PICTURE_CAP),
]
MUTATIONS += PICTURE_CAP_MUTATIONS

# ---- a restore ends the session of any account that is no longer security-equivalent -----------
def _field(name, text, suites):
    """One tuple line of `_SECURITY_FIELDS` removed: that field is no longer compared."""
    return ("restore.field-%s-ignored" % name, "luma_users.py",
            ('        "%s",' % name).ljust(31) + "# " + text + NL, "",
            "a session survives a restore that changed the account's %s" % name.replace("_", " "), suites)


_CRED = py("test_restore_sessions.py", "CredentialsRestoredToOtherValuesTests", "AdministratorsThatChangedTests")
_ROLE = py("test_restore_sessions.py", "AdministratorsThatChangedTests", "OtherAccountFieldsTests")
_PERMS = py("test_restore_sessions.py", "EachPermissionFieldAloneTests")
RESTORE_SESSION_MUTATIONS = [
    ("restore.password-sessions-survive", "luma_users.py",
     '                        reason = "password"\n                    else:',
     '                        reason = ""\n                    else:',
     "a password session survives the password being restored to another value",
     [py("test_restore_sessions.py", "CredentialsRestoredToOtherValuesTests")]),
    ("restore.link-sessions-survive", "luma_users.py",
     '                        reason = "direct"\n                    elif not session',
     '                        reason = ""\n                    elif not session',
     "a link session survives the link being replaced or removed by the restore",
     [py("test_restore_sessions.py", "CredentialsRestoredToOtherValuesTests")]),
    ("restore.other-changes-survive", "luma_users.py",
     '                    else:\n                        reason = "changed"\n',
     '                    else:\n                        reason = ""\n',
     "a session survives a change of role, permissions or enabled state that is not a password or link",
     [_PERMS, py("test_restore_sessions.py", "AdministratorsThatChangedTests")]),
    ("restore.revokes-everyone", "luma_users.py",
     '                    if not differs:\n                        reason = ""',
     '                    if False:\n                        reason = ""',
     "a restore signs out accounts it did not change, administrators included",
     [py("test_restore_sessions.py", "AccountsThatDidNotChangeTests", "UnchangedAccountsKeepEverythingTests")]),
    ("restore.unknown-before-state-survives", "luma_users.py",
     "                if account is None or not account.enabled or not isinstance(was, dict):",
     "                if account is None or not account.enabled:",
     "an account with no recorded earlier state is not treated as a different account",
     [py("test_restore_sessions.py", "OtherAccountFieldsTests")]),
    _field("password", "the hash a password session was made with", [_CRED]),
    _field("role", "what the account may reach at all", [_ROLE]),
    _field("enabled", "an account that was switched off and comes back on is another account",
           [py("test_restore_sessions.py", "OtherAccountFieldsTests")]),
    _field("direct", "the Direct Access link a link session was opened by (digest, if live)", [_CRED]),
    _field("sources", "the sources it may send", [_PERMS]),
    _field("destinations", "the displays it may drive", [_PERMS]),
    _field("groups", "the display groups it may drive", [_PERMS]),
    _field("walls", "the video walls it may drive", [_PERMS]),
    _field("standard_routing", "the card interface", [_PERMS]),
    _field("matrix_routing", "the permission-filtered matrix", [_PERMS]),
    ("restore.permissions-not-fingerprinted", "luma_users.py",
     "        state = dict(account.permissions.to_json())\n",
     "        state = {}\n",
     "no permission is part of what a session depends on",
     [_PERMS]),
    ("restore.role-not-fingerprinted", "luma_users.py",
     '        state["role"] = account.role\n',
     "",
     "the role is missing from what a session depends on, on both sides of the comparison",
     [_ROLE]),
    ("restore.password-not-fingerprinted", "luma_users.py",
     '        state["password"] = account.password_hash\n',
     "",
     "the password hash is missing from what a session depends on, on both sides of the comparison",
     [_CRED]),
    ("restore.request-keeps-sessions", "lumaserver.py",
     '        ended = REMOTE.store.revoke_sessions_outdated_by(fingerprints)\n',
     '        ended = ({"gone": 0, "password": 0, "direct": 0, "changed": 0}\n'
     '                 if request.values.get("keep_sessions") else REMOTE.store.revoke_sessions_outdated_by(fingerprints))\n',
     "a field of the restore request keeps the sessions of an account that changed",
     [py("test_restore_sessions.py", "NothingTheRequestCarriesKeepsASessionTests")]),
    ("restore.second-line-matrix-gate-removed", "luma_remote_api.py",
     '                and not account.permissions.may_use_interface("matrix")):\n            return _denied(account, "interface_not_permitted", "/api/user/matrix")',
     '                and False):\n            return _denied(account, "interface_not_permitted", "/api/user/matrix")',
     "a session that survives meets a restored account that is not judged on the request",
     [py("test_restore_sessions.py", "TheRestoredAccountIsAlsoJudgedLiveTests")]),
    ("restore.inspection-forgets-role", "luma_backup.py",
     '    return (entry.get("password_hash"), entry.get("role"), bool(entry.get("enabled", True)),',
     '    return (entry.get("password_hash"), None, bool(entry.get("enabled", True)),',
     "the confirmation does not name an account whose role the restore changes",
     [py("test_restore_sessions.py", "TheRestoreNamesWhoWillBeSignedOutTests")]),
    ("restore.inspection-forgets-permissions", "luma_backup.py",
     '            json.dumps(entry.get("permissions") or {}, sort_keys=True),\n',
     '            "",\n',
     "the confirmation does not name an account whose permissions the restore changes",
     [py("test_restore_sessions.py", "TheRestoreNamesWhoWillBeSignedOutTests")]),
]
MUTATIONS += RESTORE_SESSION_MUTATIONS

# ---- the reason an engine declined reaches the person on every way of routing --------------------
SENTENCE_SUITE = py("test_user_av_convergence.py", "TheSentenceReachesEverySurfaceTests")
REFUSAL_SENTENCE_MUTATIONS = [
    ("refusal.multi-rows-drop-the-sentence", "luma_remote_api.py",
     'total=r.get("total", 0))\n            row.update(_declined_fields(r))\n',
     'total=r.get("total", 0))\n',
     "a drop, a multi-select and the Matrix say nothing about why the engine declined",
     [SENTENCE_SUITE]),
    ("refusal.group-rows-drop-the-sentence", "luma_remote_api.py",
     '                              "audio": r.get("audio") or "unknown"}, **_declined_fields(r))',
     '                              "audio": r.get("audio") or "unknown"})',
     "a display group's rows carry no sentence",
     [SENTENCE_SUITE]),
    ("refusal.unknown-reason-is-passed-on", "luma_remote_api.py",
     '        sentence = _REFUSED_SENTENCES.get(reason)\n        if result.get("ok")',
     '        sentence = _REFUSED_SENTENCES.get(reason) or reason\n        if result.get("ok")',
     "a reason with no sentence of its own is shown to the person as the engine's internal name",
     [SENTENCE_SUITE]),
    ("refusal.page-ignores-the-sentence", "ui/user.js",
     "    if (r.message) return String(r.message);\n",
     "",
     "the page says 'Did not work' for a display the engine declined with a reason",
     [node("user_page_test.js")]),
]
MUTATIONS += REFUSAL_SENTENCE_MUTATIONS


# ---- the local application port (LUMA-MA-010) ---------------------------------------------------
PORT_UNIT = py("test_local_port.py")
PORT_RECOVERY = py("test_local_port.py", "RecoveryTests", "StartupTests", "SettingsFileTests")
PORT_LAUNCHER_CHANGE = py("test_launcher_port.py", "LauncherScenarios.test_17_a_failed_change_keeps_the_saved_value_and_allows_another_attempt")
PORT_LAUNCHER_CONFLICT = py("test_launcher_port.py", "LauncherScenarios.test_7_8_9_conflict_state_has_the_exact_texts_and_four_buttons_and_never_drifts")
PORT_MUTATIONS = [
    ("port.persist-before-bind", "luma_portcontrol.py",
     "        server = thread = None\n        if not already:\n            ok, server, thread, reason = self._start(port)\n            if not ok:\n                return self._fail(reason, port, \"TCP port %d is unavailable.\" % port",
     "        self.settings.save(MODE_FIXED, port)\n        server = thread = None\n        if not already:\n            ok, server, thread, reason = self._start(port)\n            if not ok:\n                return self._fail(reason, port, \"TCP port %d is unavailable.\" % port",
     "a new fixed port is written before the server has started on it, so a failed attempt destroys the saved value",
     [PORT_RECOVERY, PORT_LAUNCHER_CHANGE]),
    ("port.alternate-overwrites-fixed", "luma_portcontrol.py",
     "            log.info(\"[PORT] alternate TCP port %s chosen",
     "            self.settings.save(MODE_FIXED, self.current_port)\n            log.info(\"[PORT] alternate TCP port %s chosen",
     "a temporary alternate port is written over the configured fixed port",
     [PORT_RECOVERY, py("test_launcher_port.py", "LauncherScenarios.test_11_12_13_14_alternate_port_runs_the_app_opens_the_browser_and_keeps_the_config")]),
    ("port.silent-drift-in-fixed-mode", "luma_portcontrol.py",
     "            return self._exact(saved.fixed_port, \"Configured TCP port %d is already in use.\" % saved.fixed_port)",
     "            return self._automatic(saved.fixed_port)",
     "a busy fixed port drifts to the next free one instead of showing the recovery state",
     [PORT_RECOVERY, PORT_LAUNCHER_CONFLICT]),
    ("port.preflight-is-authoritative", "luma_portcontrol.py",
     "        ok, server, thread, reason = self._start(port)\n        if not ok:\n            log.info(\"[PORT] could not bind the configured TCP port",
     "        if not advisory:\n            return self._fail(\"in_use\", port, busy_message)\n        ok, server, thread, reason = self._start(port)\n        if not ok:\n            log.info(\"[PORT] could not bind the configured TCP port",
     "the advisory preflight, not the real bind, decides whether the configured port can be used",
     [py("test_local_port.py", "StartupTests")]),
    ("port.corrupt-file-not-quarantined", "luma_portcontrol.py",
     "                    luma_store.quarantine(self.path)\n                    return Settings()\n        except Exception as exc:",
     "                    return Settings()\n        except Exception as exc:",
     "an unusable port file is left in place to be tripped over again and is not kept for inspection",
     [py("test_local_port.py", "SettingsFileTests")]),
    ("port.corrupt-file-stops-start", "luma_portcontrol.py",
     "                except Exception as exc:\n                    log.warning(\"[PORT] %s is unusable (%s); quarantining it and using automatic \"\n                                \"port selection\", FILE_NAME, type(exc).__name__)\n                    luma_store.quarantine(self.path)\n                    return Settings()\n        except Exception as exc:                     # even the lock or the stat failing must not stop a start\n            log.warning(\"[PORT] could not read %s (%s); using automatic port selection\",\n                        FILE_NAME, type(exc).__name__)\n            return Settings()",
     "                except ZeroDivisionError as exc:\n                    return Settings()\n        except ZeroDivisionError as exc:\n            return Settings()",
     "an unusable port file raises out of the loader, so it can stop the launcher from starting",
     [py("test_local_port.py", "SettingsFileTests"), py("test_launcher_port.py", "LauncherScenarios.test_a_corrupt_configuration_file_cannot_stop_the_launcher")]),
    ("port.out-of-range-port-accepted", "luma_portcontrol.py",
     "            port = validate_port(raw)\n        if mode == MODE_FIXED and port is None:",
     "            port = raw\n        if mode == MODE_FIXED and port is None:",
     "a saved port outside 1024-65535 is believed",
     [py("test_local_port.py", "SettingsFileTests")]),
    ("port.restart-spawns-before-release", "luma_portcontrol.py",
     "            self.stop()                            # 1. release the port the old instance holds\n",
     "            pass\n",
     "the new instance is started while the old one still holds the port",
     [py("test_local_port.py", "SettingsActionTests")]),
    ("port.remote-reaches-the-endpoint", "luma_access.py",
     "    \"/api/local_port/<action>\": Access.LOCAL_ONLY,",
     "    \"/api/local_port/<action>\": Access.ADMIN,",
     "a network listener reaches the port-changing endpoint",
     [py("test_local_port.py", "ApiAndRouteTests"), py("test_access_fence.py")]),
    ("port.handler-trusts-the-gate", "lumaserver.py",
     "    if not luma_remote.is_local_request(request.environ):\n        return jsonify({\"ok\": False, \"error\": \"not found\"}), 404\n    payload, status = luma_portcontrol.api(action, body)",
     "    payload, status = luma_portcontrol.api(action, body)",
     "the handler stops checking locality and rests on the gate alone",
     [py("test_local_port.py", "ApiAndRouteTests")]),
    ("port.prefill-uses-8090", "ui/port_settings.js",
     "field: {type: 'text', label: 'TCP port:', value: String(state.current_port),",
     "field: {type: 'text', label: 'TCP port:', value: '8090',",
     "the dialog offers 8090 instead of the port actually in use",
     [node("port_settings_test.js")]),
    ("port.settings-persist-before-confirmation", "ui/port_settings.js",
     "      if (port == null) { render(state); return; }       // Cancel: nothing changes, the box unchecks\n",
     "      if (port == null) { return post('enable', {port: state.current_port}).then(function () { render(state); }); }\n",
     "Cancel in the fixed-port dialog still saves the port",
     [node("port_settings_test.js")]),
    ("port.launcher-conflict-text", "app_launcher.py",
     "            detail = f\"Configured TCP port {port} is already in use.\"",
     "            detail = f\"Port {port} busy.\"",
     "the launcher's conflict state no longer says what the operator was told to expect",
     [PORT_LAUNCHER_CONFLICT]),
    ('port.launcher-url-stale', 'app_launcher.py',
     'self.url_text.set(f"http://{self.host}:{self.port}")\n        self.open_btn.config(state="normal")',
     'self.url_text.set(self.url_text.get() or f"http://{self.host}:{self.port}")\n        self.open_btn.config(state="normal")',
     "the launcher's URL is kept from an earlier state instead of the controller's current one",
     [py("test_launcher_port.py", "LauncherScenarios.test_d1_settings_retry_moves_the_launcher_url_status_note_and_open_browser")]),
    ('port.launcher-open-browser-stale', 'app_launcher.py',
     '        self.port = view["port"]\n        self.running = True',
     '        self.running = True',
     'Open Browser keeps the port from an earlier state, so it opens a dead address',
     [py("test_launcher_port.py", "LauncherScenarios.test_d1_settings_retry_moves_the_launcher_url_status_note_and_open_browser")]),
    ('port.launcher-observer-not-registered', 'app_launcher.py',
     '        self.ctl.add_observer(self._on_controller_change)\n',
     '',
     'the launcher never hears about a move made by Settings',
     [py("test_launcher_port.py", "LauncherScenarios.test_d1_settings_retry_moves_the_launcher_url_status_note_and_open_browser", "LauncherScenarios.test_d1_settings_make_fixed_updates_the_launcher_note")]),
    ('port.launcher-observer-not-removed', 'app_launcher.py',
     '        self.closing = True\n        self.ctl.remove_observer(self._on_controller_change)\n        self.ctl.stop()\n        if self.tray_icon:\n            self.tray_icon.stop()\n        self.root.destroy()\n        sys.exit(0)\n\n    def _exit_for_restart',
     '        self.closing = True\n        self.ctl.stop()\n        if self.tray_icon:\n            self.tray_icon.stop()\n        self.root.destroy()\n        sys.exit(0)\n\n    def _exit_for_restart',
     'the launcher stays registered with the controller after it exits',
     [py("test_launcher_port.py", "LauncherScenarios.test_d1_exit_unregisters_the_observer_and_normal_start_is_unchanged")]),
    ('port.observer-not-called-after-operation', 'luma_portcontrol.py',
     '            self._op.release()\n            self._notify()                         # every transition, after the lock is released\n',
     '            self._op.release()\n',
     'an operation (Settings retry, make fixed, change) moves the server without telling the observers',
     [py("test_local_port.py", "ObserverTests"), py("test_launcher_port.py", "LauncherScenarios.test_d1_settings_retry_moves_the_launcher_url_status_note_and_open_browser")]),
    ('port.observer-raising-breaks-controller', 'luma_portcontrol.py',
     '            except Exception as exc:\n                log.warning("[PORT] a state observer failed: %s", type(exc).__name__)\n',
     '            except ZeroDivisionError as exc:\n                log.warning("[PORT] a state observer failed: %s", type(exc).__name__)\n',
     'an observer that raises breaks the port operation that notified it',
     [py("test_local_port.py", "ObserverTests")]),
    ('port.settings-port-field-shows-current', 'ui/port_settings.js',
     'var shownPort = (fixed && s.configured_port != null) ? s.configured_port : s.current_port;',
     'var shownPort = s.current_port;',
     'with a temporary port the Settings Port field shows the temporary port as if it were the setting',
     [node("port_settings_test.js")]),
    ('port.launcher-recovery-footer-claims-running', 'app_launcher.py',
     '        self.info_label.config(text=self.RECOVERY_INFO)\n',
     '',
     'the recovery state still says the server will run until Exit',
     [py("test_launcher_port.py", "LauncherScenarios.test_d1_settings_retry_moves_the_launcher_url_status_note_and_open_browser")]),
]
MUTATIONS += PORT_MUTATIONS


# ----------------------------------------------------------------------------------------

def restore_leftovers():
    """A killed run leaves a recovery copy beside the file it was editing; put it back."""
    restored = []
    for base, _dirs, files in os.walk(ROOT):
        if any(skip in base for skip in (".git", "node_modules", "build", "dist", "__pycache__")):
            continue
        for name in files:
            if name.endswith(".mutbak"):
                backup = os.path.join(base, name)
                target = backup[:-len(".mutbak")]
                with open(backup, "rb") as handle:
                    original = handle.read()
                with open(target, "wb") as handle:
                    handle.write(original)
                os.remove(backup)
                restored.append(os.path.relpath(target, ROOT))
    return restored


def read(rel):
    with open(os.path.join(ROOT, rel), "rb") as handle:
        return handle.read().decode("utf-8")


def anchor_status(mutation):
    _id, rel, anchor, _new, _defect, _suites = mutation
    text = read(rel)
    if "\r\n" in text:
        return "file uses CRLF; anchors are LF"
    count = text.count(anchor)
    return "ok" if count == 1 else "anchor matches %d times" % count


def run_suite(command, out_dir, mid, timeout=240):
    started = time.time()
    try:
        done = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=timeout,
                              encoding="utf-8", errors="replace")
        output, code = (done.stdout or "") + (done.stderr or ""), done.returncode
    except subprocess.TimeoutExpired as exc:
        output, code = "TIMEOUT after %ss\n%s" % (timeout, (exc.stdout or b"")), 124
    name = "%s__%s.txt" % (mid, "_".join(command[-1:]).replace(os.sep, "_").replace(":", "_"))
    with open(os.path.join(out_dir, name), "w", encoding="utf-8") as handle:
        handle.write("$ %s\nexit %s in %.1fs\n\n%s" % (" ".join(command), code, time.time() - started, output))
    return code, output


def apply(mutation, out_dir):
    mid, rel, anchor, replacement, defect, suites = mutation
    path = os.path.join(ROOT, rel)
    with open(path, "rb") as handle:
        original = handle.read()
    text = original.decode("utf-8")
    assert "\r\n" not in text, "%s uses CRLF" % rel
    assert text.count(anchor) == 1, "%s: anchor matches %d times" % (mid, text.count(anchor))
    backup = path + ".mutbak"
    with open(backup, "wb") as handle:
        handle.write(original)
    caught_by, details = [], []
    try:
        with open(path, "wb") as handle:
            handle.write(text.replace(anchor, replacement).encode("utf-8"))
        for label, command in suites:
            code, output = run_suite(command, out_dir, mid)
            details.append((label, code))
            if code != 0:
                caught_by.append(label)
    finally:
        with open(path, "wb") as handle:
            handle.write(original)
        if os.path.exists(backup):
            os.remove(backup)
    return caught_by, details


def run_canaries(out):
    """Validate the harness itself. The first canary breaks something and must be caught; the second
    changes nothing and must survive. Both suites must also pass clean before either is judged."""
    out_dir = out or tempfile.mkdtemp(prefix="mutation-canary-")
    os.makedirs(out_dir, exist_ok=True)
    for _id, _rel, anchor, _new, _defect, _suites in CANARIES:
        pass
    bad = [(m[0], anchor_status(m)) for m in CANARIES if anchor_status(m) != "ok"]
    if bad:
        print("CANARY ANCHOR", bad)
        return 2
    suites = {}
    for m in CANARIES:
        for label, command in m[5]:
            suites[label] = command
    for label, command in sorted(suites.items()):
        code, _ = run_suite(command, out_dir, "baseline")
        if code != 0:
            print("BASELINE FAILS: %s" % label)
            return 3
        print("baseline passes: %s" % label)
    verdicts = []
    for mutation, must_be_caught in zip(CANARIES, (True, False)):
        who, _ = apply(mutation, out_dir)
        caught = bool(who)
        verdicts.append(caught == must_be_caught)
        print("%-24s %s (expected %s)" % (mutation[0], "caught" if caught else "survived",
                                         "caught" if must_be_caught else "survive"))
    ok = all(verdicts)
    print("HARNESS VALIDATION:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


LOCK = os.path.join(ROOT, ".mutation-run.lock")


def take_run_lock():
    """One harness run per tree at a time. A second run would measure its BASELINE against files the first is
    mutating at that moment, and report a failing baseline that is really the other run's deliberate defect
    (this is exactly what three refused sweeps in the rc3 closeout were). Returns True if the lock is ours."""
    if os.path.exists(LOCK):
        try:
            with open(LOCK, encoding="utf-8") as handle:
                other = int((handle.read() or "0").split()[0])
        except (OSError, ValueError, IndexError):
            other = 0
        alive = False
        if other:
            try:
                import psutil
                alive = psutil.pid_exists(other)
            except ImportError:
                alive = True                      # cannot tell: refuse rather than risk it
        if alive:
            print("ANOTHER HARNESS RUN (pid %d) holds %s: refusing to start a second run in the same tree" % (other, LOCK))
            return False
    with open(LOCK, "w", encoding="utf-8") as handle:
        handle.write("%d started %s" % (os.getpid(), time.strftime("%Y-%m-%d %H:%M:%S")) + chr(10))
    return True


def release_run_lock():
    try:
        os.remove(LOCK)
    except OSError:
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-k", help="only mutations whose id contains this text")
    parser.add_argument("--routing", action="store_true",
                        help="only the multi-destination routing, wall breakaway and preference mutations")
    parser.add_argument("--restore", action="store_true",
                        help="only the restore / inventory save / lock-order / read-rule mutations")
    parser.add_argument("--canary", action="store_true",
                        help="validate the harness: a deliberate break must be caught, a no-op must survive")
    parser.add_argument("--check", action="store_true", help="verify every anchor, run nothing")
    parser.add_argument("--coverage", action="store_true", help="every suite is broken by at least one mutation")
    parser.add_argument("--out", help="directory for each suite's full output")
    parser.add_argument("--skip-baseline", action="store_true",
                        help="do not run each suite clean first (a sharded sweep runs --baseline-only once instead)")
    parser.add_argument("--baseline-only", action="store_true", help="run every suite clean, mutate nothing")
    parser.add_argument("--shard", help="K/N: run only mutations whose position modulo N equals K (parallel trees)")
    args = parser.parse_args()

    if not (args.check):
        if not take_run_lock():
            return 4
        import atexit
        atexit.register(release_run_lock)
    for rel in restore_leftovers():
        print("restored an interrupted mutation of", rel)

    selected = [m for m in MUTATIONS if not args.k or args.k in m[0]]
    if args.restore:
        mine = {m[0] for m in RESTORE_MUTATIONS}
        selected = [m for m in selected if m[0] in mine]
    if args.canary:
        return run_canaries(args.out)
    if args.routing:
        mine = {m[0] for m in ROUTING_MUTATIONS}
        selected = [m for m in selected if m[0] in mine]
    if args.shard:
        k, n = (int(x) for x in args.shard.split("/"))
        selected = [m for i, m in enumerate(selected) if i % n == k]
    ids = [m[0] for m in MUTATIONS]
    assert len(ids) == len(set(ids)), "duplicate mutation ids"

    bad = [(m[0], anchor_status(m)) for m in MUTATIONS if anchor_status(m) != "ok"]
    if args.check:
        for mid, status in bad:
            print("ANCHOR", mid, status)
        print("%d mutations, %d bad anchors" % (len(MUTATIONS), len(bad)))
        return 1 if bad else 0
    if bad:
        for mid, status in bad:
            print("ANCHOR", mid, status)
        print("refusing to run: a mutation whose anchor cannot match is noise that reads as a gap")
        return 2

    out_dir = args.out or tempfile.mkdtemp(prefix="mutation-output-")
    os.makedirs(out_dir, exist_ok=True)

    # The harness is validated before it is trusted: every suite it uses must pass clean.
    suites = {}
    for m in selected:
        for label, command in m[5]:
            suites[label] = command
    if args.baseline_only:
        suites = {label: command for m in MUTATIONS for label, command in m[5]}
    failed = []
    for label, command in ([] if args.skip_baseline else sorted(suites.items())):
        code, _ = run_suite(command, out_dir, "baseline")
        if code != 0:
            print("BASELINE FAILS: %s -- a mutation cannot be judged against a failing suite" % label)
            failed.append(label)
            if not args.baseline_only:
                return 3
        elif args.baseline_only:
            print("baseline ok  %s" % label)
        sys.stdout.flush()
    if args.baseline_only:
        print("%d suites, %d failing clean" % (len(suites), len(failed)))
        return 1 if failed else 0

    survivors, caught, seen_failing = [], [], set()
    for m in selected:
        mid, rel, _a, _r, defect, _s = m
        who, details = apply(m, out_dir)
        seen_failing.update(who)
        if who:
            caught.append(mid)
            print("caught    %-38s by %s" % (mid, ", ".join(who)))
        else:
            survivors.append(mid)
            print("SURVIVED  %-38s %s -- %s" % (mid, rel, defect))
        sys.stdout.flush()

    print(NL + "%d mutations: %d caught, %d survived. Full output: %s" % (
        len(selected), len(caught), len(survivors), out_dir))
    # The harness is a test of the tests, so it has to be able to see each suite fail: a suite
    # that no mutation ever broke is a suite the harness cannot vouch for.
    blind = sorted(label for label in suites if label not in seen_failing)
    if blind:
        print("HARNESS CANNOT SEE FAILURES IN:", ", ".join(blind))
    return 1 if (survivors or blind) else 0


if __name__ == "__main__":
    sys.exit(main())
