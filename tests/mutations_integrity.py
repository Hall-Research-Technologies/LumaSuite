"""Mutations for Display Group integrity: identity at ingest and identity at route time.

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


NL = chr(10)
INGEST = py("test_group_integrity.py", "IngestTests")
IDENTITY = py("test_group_integrity.py", "RouteTimeIdentityTests")
LIFECYCLE = py("test_group_integrity.py", "GroupLifecycleTests")
WALLID = py("test_group_integrity.py", "WallMemberIdentityTests")
STARTUP = py("test_group_integrity.py", "StartupLoadDedupeTests")
REACH = py("test_reach_rescan.py")

INTEGRITY_MUTATIONS = [
    # ---- a rescan that found a unit is a fresh answer from it (soak L-03) ----------------------
    ("reach.rescan-does-not-clear", "lumaserver.py",
     "        if answered_as_itself:" + NL + "            luma_liveness.record(cache_units[ip], True)",
     "        if False:" + NL + "            luma_liveness.record(cache_units[ip], True)",
     "a display a rescan just found is still refused as offline until the next refresh",
     [REACH]),
    ("reach.clear-records-a-miss", "lumaserver.py",
     "            luma_liveness.record(cache_units[ip], True)",
     "            luma_liveness.record(cache_units[ip], False)",
     "the rescan notes the unit as unanswered instead of answered",
     [REACH]),
    ("reach.clear-without-mac-proof", "lumaserver.py",
     "        answered_as_itself = bool(mac_new) and luma_projection.unit_identity(existing_unit) == mac_new",
     "        answered_as_itself = True",
     "any scan of an existing row clears its offline state, whoever answered",
     [REACH]),
    ("reach.clear-without-record-match", "lumaserver.py",
     "        answered_as_itself = bool(mac_new) and luma_projection.unit_identity(existing_unit) == mac_new",
     "        answered_as_itself = bool(mac_new)",
     "a scan that reports a MAC clears a row that was never shown to be that unit",
     [REACH]),
    # ---- route time ---------------------------------------------------------------------------
    ("group.identity-mismatch-accepted", "luma_remote_api.py",
     '    return None if found == identity else "identity_mismatch"',
     "    return None",
     "a device that reports a different hardware address is still written to",
     [IDENTITY, LIFECYCLE]),
    ("group.identity-unconfirmed-accepted", "luma_remote_api.py",
     '    if not found:' + NL + '        return "identity_unconfirmed"',
     '    if not found:' + NL + '        return None',
     "a device that will not say who it is is still written to",
     [IDENTITY]),
    ("group.source-not-confirmed", "luma_remote_api.py",
     "                reason = _confirm_identity(decoder_ip, identity) or _source_reason(encoder_ip)",
     "                reason = _confirm_identity(decoder_ip, identity)",
     "a re-leased source address is routed to every member",
     [IDENTITY]),
    ("group.single-route-skips-identity", "luma_remote_api.py",
     "                unproven = (_confirm_identity(decoder_ip, destination_id)" + NL +
     "                            or _confirm_identity(encoder_ip, source_id))",
     "                unproven = None",
     "the single-display route writes without confirming who answers",
     [IDENTITY]),
    ("group.ambiguous-identity-guessed", "luma_remote_api.py",
     "                if identity in index and index[identity] != unit[" + '"ip"' + "]:" + NL +
     "                    index[identity] = None" + NL +
     "                elif identity not in index:" + NL +
     "                    index[identity] = unit[" + '"ip"' + "]",
     "                index[identity] = unit[" + '"ip"' + "]",
     "an identity held under two addresses resolves to whichever the cache listed last",
     [IDENTITY]),
    ("group.intent-not-logged-at-info", "luma_remote_api.py",
     '        log.info("[ROUTE-INTENT] rule=%s plane=%s source=%s groups=%s walls=%s displays=%s",',
     '        log.debug("[ROUTE-INTENT] rule=%s plane=%s source=%s groups=%s walls=%s displays=%s",',
     "the audit line naming the intended identities is not emitted",
     [IDENTITY, LIFECYCLE]),
    ("group.intent-omits-the-displays", "luma_remote_api.py",
     '                 ",".join(wall_ids) or "-", ",".join(display_ids) or "-")',
     '                 ",".join(wall_ids) or "-", "-")',
     "the audit line does not name the displays that will be routed",
     [IDENTITY, LIFECYCLE]),
    ("group.silent-unit-reads-as-failed", "luma_remote_api.py",
     '                    "result": "unreachable" if reason == "identity_unconfirmed" else "failed",',
     '                    "result": "failed",',
     "a unit that did not answer is reported as a conflict instead of as not answering",
     [IDENTITY]),


    # ---- ingest ---------------------------------------------------------------------------------
    ("group.ingest-unprovable-merged", "lumaserver.py",
     '            return {"action": "skipped", "moved_from": None, "mac": ""}',
     "            pass",
     "a scan with no hardware address is merged into an identified row it cannot be shown to match",
     [INGEST]),
    ("group.ingest-different-serial-merged", "lumaserver.py",
     "        if serial_new and serial_held and serial_new != serial_held:",
     "        if False:",
     "a different serial number still inherits the cached hardware identity",
     [INGEST]),
    ("group.ingest-stale-index-trusted", "lumaserver.py",
     "        if old_row is not None and luma_projection.unit_identity(old_row) == mac_new:",
     "        if old_row is not None:",
     "a stale MAC index deletes the row of the unit that took the address",
     [INGEST, LIFECYCLE]),
    ("group.ingest-foreign-row-merged", "lumaserver.py",
     "            existing_unit, existed = {}, False        # another device's row: not this one's",
     "            pass",
     "a different device at a held address is merged into the departed unit's row",
     [INGEST]),
    ("group.ingest-index-spelling", "lumaserver.py",
     "        cached_mac = luma_projection.unit_identity(cached_unit)" + NL +
     "        if cached_mac:" + NL +
     "            index[cached_mac] = cached_ip",
     '        cached_mac = (cached_unit.get("mac") or "").strip().lower()' + NL +
     "        if cached_mac:" + NL +
     "            index[cached_mac] = cached_ip",
     "a MAC spelled with other separators is not recognised as the unit already cached",
     [INGEST]),
    ("group.merge-disk-duplicates-identity", "lumaserver.py",
     "                if disk_mac and disk_mac in known:" + NL +
     "                    continue",
     "                if disk_mac and disk_mac in known:" + NL +
     "                    pass",
     "a disk row for a unit already held at another address gives one identity two addresses",
     [INGEST]),

    # ---- saved Video Wall members and the startup load -------------------------------------------
    ("wall.member-identity-not-confirmed", "lumaserver.py",
     "        reason = _wall_identity_refusal(unit, units)" + NL + "        if reason:" + NL +
     '            logging.warning("[wall-route] a member',
     "        reason = None" + NL + "        if reason:" + NL +
     '            logging.warning("[wall-route] a member',
     "a saved wall writes to a member without asking who is at its address",
     [WALLID]),
    ("wall.source-not-confirmed", "lumaserver.py",
     "        source_reason = _wall_identity_refusal(source_unit, units)",
     "        source_reason = None",
     "a re-leased source address supplies another encoder's stream to every member",
     [WALLID]),
    ("wall.page-route-unconfirmed", "lumaserver.py",
     "    if not confirmed:" + NL + "        reason = _wall_identity_refusal(unit)",
     "    if False:" + NL + "        reason = _wall_identity_refusal(unit)",
     "the Video Wall page's Sync Source writes without asking who is at the address",
     [WALLID]),
    ("wall.helper-ambiguity-ignored", "lumaserver.py",
     "    if len(addresses) > 1:" + NL + '        return "ambiguous_identity"',
     "    if False:" + NL + '        return "ambiguous_identity"',
     "the Video Wall page routes a member whose identity is held under two addresses",
     [WALLID]),
    ("wall.helper-mismatch-accepted", "lumaserver.py",
     "    return luma_remote_api.confirm_identity(_identify_for_route, unit[" + '"ip"' + "], identity)",
     "    return None",
     "a wall member is never asked who it is",
     [WALLID]),
    ("wall.intent-not-logged", "luma_remote_api.py",
     '            log.info("[ROUTE-INTENT] rule=%s plane=%s source=%s wall=%s members=%s",',
     '            log.debug("[ROUTE-INTENT] rule=%s plane=%s source=%s wall=%s members=%s",',
     "the saved-wall route does not name the members it will change",
     [WALLID]),
    ("wall.intent-omits-members", "luma_remote_api.py",
     '                     rule, plane, source_id, wall_id, ",".join(members) or "-")',
     '                     rule, plane, source_id, wall_id, "-")',
     "the saved-wall audit line carries no member identities",
     [WALLID]),
    ("wall.page-intent-omits-members", "lumaserver.py",
     '                 ",".join(sorted(v or "unknown" for v in intended.values())))',
     '                 "-")',
     "the Video Wall page's audit line carries no member identities",
     [WALLID]),
    ("startup.duplicate-identity-kept", "lumaserver.py",
     "        if identity and identity in taken_identities:" + NL + "            continue",
     "        if False:" + NL + "            continue",
     "a stale persisted row for a unit that moved survives a restart",
     [STARTUP]),
    ("startup.oldest-row-wins", "lumaserver.py",
     "    for row in reversed(list(rows or [])):",
     "    for row in list(rows or []):",
     "the stale persisted row is preferred to the newer one",
     [STARTUP]),
]
