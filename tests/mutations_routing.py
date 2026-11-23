"""Mutations for the one multi-destination routing engine, the wall breakaway and the
"Audio follows video" preference.

The first group is the earlier routing mutations, retargeted at the code that replaced the three
separate per-destination loops (a display, a group member and a wall were each sequenced by their own
handler code; they are now one `_run_batch` with one `execute`). The ids are kept so history reads
straight through. Three that described a rule this release removed or merged were deleted rather
than repointed -- a wall refusing video-only (the wall now supports it), a wall row advertising only
one mode, and the group-specific copy of "outcome drops the reason" (the code is now shared with the
multi route, which keeps its own mutation).

The second group is new: the parallel dispatch, the member-level wall operation and the preference.

Entry format is the harness's:

    (id, file, anchor, replacement, what it breaks, suites that must notice)

An anchor must match exactly once, in an LF file.
"""
import os
import sys

PY = sys.executable


def py(file, *selectors):
    return ("python:" + file + (":" + ",".join(selectors) if selectors else ""),
            [PY, os.path.join("tests", file)] + list(selectors))


def node(file):
    return ("node:" + file, ["node", os.path.join("tests", file)])


NL = chr(10)
SENTENCE_SUITE = py("test_user_av_convergence.py", "TheSentenceReachesEverySurfaceTests")
PARALLEL = py("test_route_parallel.py")
WALLS = py("test_route_parallel.py", "WallBreakawayTests")
PREF = py("test_route_parallel.py", "AudioFollowsVideoTests")
SPREAD = py("test_route_parallel.py", "StartSpreadTests")
BATCH = py("test_route_parallel.py", "BatchSemanticsTests")
PAGE = node("user_matrix_page_test.js")
STANDARD_PAGE = node("user_page_test.js")

AFTER_WAIT_ANCHOR = ('                with locks.hold(*op["resources"]):' + NL +
                     '                    refusal = _revalidate(check, rule)           # after the wait')
AFTER_WAIT_BROKEN = ('                with locks.hold(*op["resources"]):' + NL +
                     '                    refusal = None           # after the wait')
OFFLINE_DISPLAY_ANCHOR = ('                                   if identity in address and not decoder_ip else {}))' + NL +
                          '                if _is_offline(identity):' + NL +
                          '                    return {"ok": False, "result": "offline", "display": identity,' + NL +
                          '                            "name": _name(identity)}')
OFFLINE_DISPLAY_BROKEN = ('                                   if identity in address and not decoder_ip else {}))' + NL +
                          '                if False:' + NL +
                          '                    return {"ok": False, "result": "offline", "display": identity,' + NL +
                          '                            "name": _name(identity)}')
WALL_OFFLINE_ANCHOR = ('        if not luma_projection.is_online(unit):' + NL +
                       '            return {"ok": False, "result": "offline"}')
WALL_OFFLINE_BROKEN = ('        if False:' + NL +
                       '            return {"ok": False, "result": "offline"}')

ROUTING_MUTATIONS = [
    # ---- retargeted: the per-destination rules, now in `execute` and `display_run` ------------
    ("group.member-lock-removed", "luma_remote_api.py",
     '                with locks.hold(*op["resources"]):' + NL +
     '                    refusal = _revalidate(check, rule)           # after the wait',
     "                with __import__('contextlib').nullcontext():" + NL +
     '                    refusal = _revalidate(check, rule)           # after the wait',
     "a group member is routed without taking its display lock",
     [py("test_display_groups.py", "GroupRoutingTests")]),
    ("group.rolls-back-on-failure", "luma_groups.py",
     '    routed = sum(1 for r in results if r.get("ok"))' + NL +
     '    return {"ok": bool(results) and routed == len(results),' + NL +
     '            "routed": routed, "total": len(results), "results": results}',
     '    if any(not r.get("ok") for r in results):' + NL +
     '        results = [dict(r, ok=False) for r in results]' + NL +
     '    routed = sum(1 for r in results if r.get("ok"))' + NL +
     '    return {"ok": bool(results) and routed == len(results),' + NL +
     '            "routed": routed, "total": len(results), "results": results}',
     "one failure marks every member failed (the sequential runner)",
     [py("test_display_groups.py", "SequentialRunTests")]),
    ("ui.route-sends-address", "ui/user.js",
     "    var body = {source: source, displays: sent.displays, groups: sent.groups};",
     "    var body = {source: source, displays: sent.displays, groups: sent.groups, ip: '198.51.100.1'};",
     "the routing page sends an address",
     [STANDARD_PAGE]),
    ("route.user-endpoint-reads-mode", "luma_remote_api.py",
     "                if plane == luma_user_matrix.MODE_AV:" + NL +
     "                    outcome = route_av(decoder_ip, encoder_ip)" + NL +
     "                else:" + NL +
     "                    outcome = route_mode(decoder_ip, plane, encoder_ip)" + NL +
     "                body, status = _project_route(outcome)",
     "                _requested_mode = data.get(\"mode\")" + NL +
     "                if _requested_mode in (\"video\", \"audio\"):" + NL +
     "                    plane = _requested_mode" + NL +
     "                if plane == luma_user_matrix.MODE_AV:" + NL +
     "                    outcome = route_av(decoder_ip, encoder_ip)" + NL +
     "                else:" + NL +
     "                    outcome = route_mode(decoder_ip, plane, encoder_ip)" + NL +
     "                body, status = _project_route(outcome)",
     "the restricted route starts to read a breakaway mode",
     [py("test_unification.py", "BreakawayEndpointTests")]),
    ("multi.not-rechecked-after-the-wait", "luma_remote_api.py",
     AFTER_WAIT_ANCHOR, AFTER_WAIT_BROKEN,
     "a permission withdrawn while a display was locked is not noticed",
     [py("test_user_multi.py", "SequencingTests")]),
    ("wall.not-rechecked-after-the-wait", "luma_remote_api.py",
     AFTER_WAIT_ANCHOR, AFTER_WAIT_BROKEN,
     "a grant withdrawn while a wall waited for its decoders is not noticed",
     [py("test_user_walls.py", "SecondLineTests")]),
    ("wall.partial-reported-ok", "luma_remote_api.py",
     '                row["ok"] = bool(row.get("ok"))',
     '                row["ok"] = True',
     "a wall that changed only some displays is reported as done",
     [WALLS]),
    ("wall.write-not-verified", "lumaserver.py",
     '            if not self._held(ip, cell):' + NL +
     '                return {"ok": False, "result": "not_verified"}',
     '            if False:' + NL +
     '                return {"ok": False, "result": "not_verified"}',
     "a decoder that accepted the wall write and does not hold it counts as routed",
     [py("test_user_walls.py", "TheRealRoutesTests")]),
    ("wall.saved-wall-source-not-recorded", "lumaserver.py",
     '                            saved["source_id"] = self.source_id' + NL,
     "                            pass" + NL,
     "the saved wall keeps a source the decoders no longer show",
     [py("test_user_walls.py", "TheRealRoutesTests")]),
    ("wall.offline-member-written-to", "lumaserver.py",
     WALL_OFFLINE_ANCHOR, WALL_OFFLINE_BROKEN,
     "an offline decoder of a wall is written to",
     [py("test_user_walls.py", "TheRealRoutesTests")]),
    ("offline.group-source-not-checked", "luma_remote_api.py",
     '            return jsonify({"ok": False, "error": "That group is not available."}), 404' + NL +
     '        if _is_offline(source_id):' + NL +
     '            return _offline_refusal(source_id)' + NL + NL +
     '        summary = _run_batch("/api/user/route_group"',
     '            return jsonify({"ok": False, "error": "That group is not available."}), 404' + NL + NL +
     '        summary = _run_batch("/api/user/route_group"',
     "a group route from an offline source is attempted",
     [py("test_user_walls.py", "OfflineTests")]),
    ("offline.multi-destination-not-checked", "luma_remote_api.py",
     OFFLINE_DISPLAY_ANCHOR, OFFLINE_DISPLAY_BROKEN,
     "an offline display in a multi-route is written to",
     [py("test_user_walls.py", "OfflineTests")]),
    ("offline.group-member-not-checked", "luma_remote_api.py",
     OFFLINE_DISPLAY_ANCHOR, OFFLINE_DISPLAY_BROKEN,
     "an offline member of a group is written to",
     [py("test_user_walls.py", "OfflineTests")]),
    ("wall.source-routed-from-the-cache", "lumaserver.py",
     '        self.stream = str(live_result.get("streamname") or "").strip()' + NL +
     '        if not self.stream:',
     '        cached = _vw_encoder_stream(self.source_id)' + NL +
     '        self.stream = str(cached.get("streamname") or "").strip() if cached.get("ok") else ""' + NL +
     '        if not self.stream:',
     "a wall is routed from the stream the cache remembers when the source does not answer",
     [py("test_user_walls.py", "TheRealRoutesTests")]),
    ("adversarial.display-route-no-recheck-after-wait", "luma_remote_api.py",
     AFTER_WAIT_ANCHOR, AFTER_WAIT_BROKEN,
     "a Matrix route that waited behind the lock writes to a display after the account lost it",
     [py("test_matrix_adversarial.py", "WaitingBehindTheLock")]),
    ("adversarial.wall-route-no-recheck-after-wait", "luma_remote_api.py",
     AFTER_WAIT_ANCHOR, AFTER_WAIT_BROKEN,
     "a Matrix route that waited behind the lock writes to a wall after its grant was withdrawn",
     [py("test_matrix_adversarial.py", "WallWaitingBehindTheLock")]),
    ("torture.decoder-lock-removed", "luma_remote_api.py",
     '                with locks.hold(*op["resources"]):' + NL +
     '                    refusal = _revalidate(check, rule)           # after the wait',
     '                if True:' + NL +
     '                    refusal = _revalidate(check, rule)           # after the wait',
     "two routes to one decoder are written at the same moment",
     [py("test_routing_torture.py", "ConcurrencyTests")]),
    ("torture.offline-display-written", "luma_remote_api.py",
     OFFLINE_DISPLAY_ANCHOR, OFFLINE_DISPLAY_BROKEN,
     "a display that has stopped answering is written to",
     [py("test_routing_torture.py", "OfflineAndReturnTests")]),
    ("unroute.offline-display-written", "luma_remote_api.py",
     '                            "name": _name(identity)}' + NL +
     '                if _is_offline(identity):' + NL +
     '                    return {"ok": False, "result": "offline", "display": identity,',
     '                            "name": _name(identity)}' + NL +
     '                if False:' + NL +
     '                    return {"ok": False, "result": "offline", "display": identity,',
     "an offline display in an unroute is written to",
     [py("test_user_unroute.py", "OfflineTests")]),
    ("torture.offline-wall-member-written", "lumaserver.py",
     WALL_OFFLINE_ANCHOR, WALL_OFFLINE_BROKEN,
     "a wall writes to a member that has stopped answering",
     [py("test_routing_torture.py", "OfflineAndReturnTests")]),
    ("refusal.multi-outcome-drops-the-reason", "luma_remote_api.py",
     '                        "reason": outcome.get("reason") or "",' + NL +
     '                        "audio": ("unknown"',
     '                        "audio": ("unknown"',
     "the engine's reason is thrown away before the rows are built",
     [SENTENCE_SUITE]),
    ("ui.matrix-preview-repeats", "ui/user.js",
     "    node.addEventListener('mouseleave', function () { release(); cancelPreview(); });",
     "    node.addEventListener('mouseleave', function () { release(); });",
     "a preview requested by a hover is not cancelled when the pointer leaves",
     [PAGE]),

    # ---- MA-005: one engine, dispatched in a bounded parallel batch --------------------------
    ("parallel.dispatch-is-sequential", "luma_groups.py",
     "        with ThreadPoolExecutor(max_workers=min(bound, len(entries)),",
     "        with ThreadPoolExecutor(max_workers=1,",
     "destinations are written one after another again",
     [SPREAD]),
    ("parallel.dispatch-unbounded", "luma_groups.py",
     "    bound = max(1, min(MAX_DISPATCH_WORKERS, int(workers or dispatch_workers())))",
     "    bound = max(1, len(entries))",
     "every destination gets its own thread: a request storm",
     [SPREAD]),
    ("parallel.no-request-context", "luma_remote_api.py",
     "            ops, execute, wrap=lambda fn: copy_current_request_context(fn))",
     "            ops, execute, wrap=None)",
     "worker threads run without the request, so the session cannot be re-read",
     [BATCH, py("test_user_multi.py", "SequencingTests")]),
    ("parallel.member-failure-aborts-batch", "luma_groups.py",
     "        except Exception as exc:                  # an entry's failure is only its own" + NL +
     '            log.warning("[GROUPS] entry %d raised %s", index, type(exc).__name__)',
     "        except Exception as exc:                  # an entry's failure is only its own" + NL +
     "            raise" + NL +
     '            log.warning("[GROUPS] entry %d raised %s", index, type(exc).__name__)',
     "one destination raising takes the whole batch with it",
     [py("test_user_multi.py", "SequencingTests")]),
    ("parallel.results-not-in-entry-order", "luma_groups.py",
     "                results[index] = future.result()",
     "                results[index] = future.result()" + NL +
     "            results.reverse()",
     "results come back in an order that is not the order chosen",
     [py("test_user_multi.py", "SequencingTests"), SPREAD]),
    ("batch.wall-members-unlocked", "luma_remote_api.py",
     "                            \"resources\": ([locks.decoder(member.identity)]" + NL +
     "                                          if member.identity else []),",
     "                            \"resources\": [],",
     "a wall member is routed without taking its display lock",
     [WALLS]),

    # ---- MA-007: the wall row is an aggregate, routed member by member in the chosen mode ----
    ("wall.video-does-not-pin-the-audio", "lumaserver.py",
     "            if self.plane == luma_breakaway.MODE_VIDEO:",
     "            if False:",
     "a video-only wall route lets each member's audio follow the picture",
     [WALLS]),
    ("wall.audio-route-also-writes-video", "lumaserver.py",
     "            if self.plane == luma_breakaway.MODE_AUDIO:" + NL +
     "                outcome = _route_mode_for_users(ip, luma_breakaway.MODE_AUDIO, source_ip)",
     "            if False:" + NL +
     "                outcome = _route_mode_for_users(ip, luma_breakaway.MODE_AUDIO, source_ip)",
     "an audio-only wall route also moves each member's picture",
     [WALLS]),
    ("wall.av-does-not-bring-the-audio", "lumaserver.py",
     "            if self.plane == luma_breakaway.MODE_AV:" + NL +
     "                # Both planes to the source",
     "            if False:" + NL +
     "                # Both planes to the source",
     "an audio+video wall route leaves a breakaway member's audio behind",
     [WALLS]),
    ("wall.audio-route-rewrites-saved-source", "lumaserver.py",
     "            if self.plane != luma_breakaway.MODE_AUDIO:",
     "            if True:",
     "an audio-only wall route changes the picture the saved wall claims to show",
     [WALLS]),
    ("wall.matrix-row-offers-av-only", "luma_user_matrix.py",
     '                 "wall": (MODE_AV, MODE_VIDEO, MODE_AUDIO)}',
     '                 "wall": (MODE_AV,)}',
     "the saved wall row cannot be routed video-only or audio-only from the Matrix",
     [WALLS]),
    ("wall.member-grant-substituted", "luma_remote_api.py",
     '            for wall_id in wall_ids:' + NL +
     '                if not account.permissions.may_control_wall(wall_id):' + NL +
     '                    return "wall_not_permitted"',
     '            for wall_id in wall_ids:' + NL +
     '                if not account.permissions.may_control_destination(wall_id):' + NL +
     '                    return "wall_not_permitted"',
     "a wall is judged by the individual-display grant, so members would need their own",
     [WALLS]),

    # ---- MA-008: Audio follows video is a Standard preference, not a permission ---------------
    ("afv.preference-ignored", "luma_remote_api.py",
     "        if follows:" + NL +
     "            return luma_user_matrix.MODE_AV, None",
     "        if True:" + NL +
     "            return luma_user_matrix.MODE_AV, None",
     "turning Audio follows video off still routes the audio",
     [PREF]),
    ("afv.matrix-reads-the-preference", "luma_remote_api.py",
     '        if interface == "standard":' + NL +
     '            plane, bad = _standard_plane(data)',
     '        if True:' + NL +
     '            plane, bad = _standard_plane(data)',
     "the toggle changes what a Matrix mode does",
     [PREF]),
    ("afv.non-boolean-accepted", "luma_remote_api.py",
     "        if not isinstance(follows, bool):",
     "        if False:",
     "a malformed preference silently means on",
     [PREF]),
    ("afv.group-endpoint-ignores-it", "luma_remote_api.py",
     '        plane, bad = _standard_plane(data)' + NL +
     '        if bad is not None:' + NL +
     '            return bad' + NL +
     '        refusal = _revalidate(_check, "/api/user/route_group")',
     '        plane, bad = luma_user_matrix.MODE_AV, None' + NL +
     '        refusal = _revalidate(_check, "/api/user/route_group")',
     "the group endpoint routes audio whatever the preference says",
     [PREF]),
    ("ui.afv-not-sent", "ui/user.js",
     "      body.audio_follows_video = state.audioFollowsVideo !== false;",
     "      body.audio_follows_video = undefined;",
     "the routing page never tells the server what the toggle says",
     [STANDARD_PAGE]),
    ("ui.afv-defaults-off", "ui/user.js",
     "    audioFollowsVideo: true,",
     "    audioFollowsVideo: false,",
     "a fresh session routes video only",
     [STANDARD_PAGE]),
    ("ui.afv-ignores-the-switch", "ui/user.js",
     "      afv.addEventListener('change', function () { state.audioFollowsVideo = !!afv.checked; });",
     "      afv.addEventListener('change', function () { });",
     "flipping the switch changes nothing",
     [STANDARD_PAGE]),
    ("ui.afv-remembered", "ui/user.js",
     "      afv.addEventListener('change', function () { state.audioFollowsVideo = !!afv.checked; });",
     "      afv.addEventListener('change', function () { state.audioFollowsVideo = !!afv.checked;" +
     " remember('audio_follows_video', afv.checked ? '' : 'off'); });",
     "the preference outlives the session in browser storage",
     [STANDARD_PAGE]),

    # ---- MA-006: the preview belongs to the routing dot ---------------------------------------
    ("ui.preview-on-the-heading", "ui/user.js",
     "      th.appendChild(sub);" + NL +
     "      hr.appendChild(th);",
     "      th.appendChild(sub);" + NL +
     "      wirePreview(th, name, s);" + NL +
     "      hr.appendChild(th);",
     "hovering a source heading brings the preview back",
     [PAGE]),
    ("ui.preview-not-on-the-dot", "ui/user.js",
     "        wirePreview(td, button, s);",
     "        void 0;",
     "the routing dot has no preview",
     [PAGE]),
    ("ui.preview-survives-the-press", "ui/user.js",
     "    button.addEventListener('mousedown', function () { pressed = true; cancelPreview(); });",
     "    button.addEventListener('mousedown', function () { pressed = true; });",
     "a preview stays up under the pointer while the dot is pressed",
     [PAGE]),
    ("ui.preview-press-refocus-restarts", "ui/user.js",
     "    button.addEventListener('focus', function () { if (!pressed) start(); });",
     "    button.addEventListener('focus', function () { start(); });",
     "the focus a mouse press causes asks for a picture",
     [PAGE]),
]
