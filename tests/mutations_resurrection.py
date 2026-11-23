"""Mutations for LUMA-REL-002 (a cleared unit is never recreated by a stale request).

    (id, file, anchor, replacement, what it breaks, suites that must notice)

An anchor must match exactly once, in an LF file.
"""
import os
import sys

PY = sys.executable
NL = chr(10)


def py(file, *selectors):
    return ("python:" + file + (":" + ",".join(selectors) if selectors else ""),
            [PY, os.path.join("tests", file)] + list(selectors))


def node(file):
    return ("node:" + file, ["node", os.path.join("tests", file)])


SERVER = py("test_clear_no_resurrection.py")
PAGE = node("clear_stale_route_test.js")

RESURRECTION_MUTATIONS = [
    ("res.route-av-recreates-the-record", "lumaserver.py",
     "        dec_unit = _unit_if_same(decoder_ip, decoder_ref)" + NL + "        if dec_unit is not None:" + NL + "            if observed:",
     "        dec_unit = cache_units.setdefault(decoder_ip, {})" + NL + "        if dec_unit is not None:" + NL + "            if observed:",
     "the verified route stores a partial record under the address of a unit that was cleared (the reported defect)",
     [SERVER]),
    ("res.route-av-no-entry-check", "lumaserver.py",
     "    if decoder_ref is None:" + NL + "        result.update(status=\"not_found\", reason=\"unknown_unit\"",
     "    if False:" + NL + "        result.update(status=\"not_found\", reason=\"unknown_unit\"",
     "the verified route sends commands to a display the inventory does not hold",
     [SERVER]),
    ("res.admin-route-no-unknown-check", "lumaserver.py",
     "    if stale is not None:" + NL + "        return stale" + NL + "    if _unknown_among(decoder_ip):",
     "    if stale is not None:" + NL + "        return stale" + NL + "    if False:",
     "a plain route to a unit that is not in the inventory is not refused as unknown",
     [SERVER]),
    ("res.admin-breakaway-no-unknown-check", "lumaserver.py",
     "        if _unknown_among(decoder_ip):" + NL + "            return _unknown_unit_response()",
     "        if False:" + NL + "            return _unknown_unit_response()",
     "a breakaway route to a unit that is not in the inventory is not refused as unknown",
     [SERVER]),
    ("res.breakaway-no-entry-check", "lumaserver.py",
     "    if decoder_ref is None:" + NL + "        return {\"ok\": False, \"status\": \"not_found\"",
     "    if False:" + NL + "        return {\"ok\": False, \"status\": \"not_found\"",
     "the breakaway engine runs against a display the inventory does not hold",
     [SERVER]),
    ("res.other-device-at-the-address-is-written", "lumaserver.py",
     "    if identity and now and now != identity:",
     "    if False:",
     "a result is recorded on whichever device now answers at the address",
     [SERVER]),
    ("res.unprovable-identity-is-written", "lumaserver.py",
     "    return unit if (identity and now == identity) else None",
     "    return unit",
     "after a clear, a unit that cannot be shown to be the same one is written anyway",
     [SERVER]),
    ("res.breakaway-writes-without-identity", "lumaserver.py",
     "            unit = _unit_if_same(decoder_ip, decoder_ref)" + NL + "            if isinstance(unit, dict):" + NL + "                dec = unit[\"dec\"]",
     "            unit = cache_units.get(decoder_ip)" + NL + "            if isinstance(unit, dict):" + NL + "                dec = unit[\"dec\"]",
     "a breakaway result is written onto a different unit that took the address",
     [SERVER]),
    ("res.breakaway-silent-when-unit-gone", "lumaserver.py",
     "                outcome = dict(outcome, ok=False, status=\"conflict\", reason=\"inventory_changed\",",
     "                outcome = dict(outcome, ok=outcome.get(\"ok\"), status=outcome.get(\"status\"), reason=\"inventory_changed\",",
     "a route whose display left the inventory is still reported as verified",
     [SERVER]),
    ("res.stale-revision-accepted", "lumaserver.py",
     "    if token != _inventory_epoch():",
     "    if False:",
     "a request from before a clear is acted on",
     [SERVER]),
    ("res.malformed-revision-accepted", "lumaserver.py",
     "    if not isinstance(token, str) or not token or len(token) > 64:",
     "    if False:",
     "a malformed revision is judged as stale (409) or ignored instead of refused as malformed (400)",
     [SERVER]),
    ("res.revision-never-moves", "lumaserver.py",
     "        before_units = dict(cache_units)" + NL + "        cache_generation += 1" + NL + "        cache_units.clear()" + NL + "        if not luma_store.atomic_write_json(CACHE_FILE, [])",
     "        before_units = dict(cache_units)" + NL + "        cache_units.clear()" + NL + "        if not luma_store.atomic_write_json(CACHE_FILE, [])",
     "Clear Units does not retire the revision that pages hold",
     [SERVER]),
    ("res.revision-not-served-on-cache", "lumaserver.py",
     "jsonify({\"units\": served, \"inventory_epoch\": _inventory_epoch(), **_topology_payload(rows)})",
     "jsonify({\"units\": served, **_topology_payload(rows)})",
     "the page is never told which inventory it was built from",
     [SERVER, PAGE]),
    ("res.conflict-is-502-on-admin", "lumaserver.py",
     "                     \"not_found\": 404, \"conflict\": 409}",
     "                     \"not_found\": 404}",
     "a route that lost its display mid-flight is a 5xx, as if the server had failed",
     [SERVER]),
    ("res.conflict-is-502-for-users", "luma_remote_api.py",
     "                                      \"conflict\": 409}.get(status, 502))",
     "                                      }.get(status, 502))",
     "a User's route that lost its display mid-flight is a 5xx",
     [SERVER]),
    ("res.credential-slot-by-address-kept", "lumaserver.py",
     "        for key in [k for k in _device_credential_cache if k.startswith(\"ip:\")]:",
     "        for key in []:",
     "the working-credential slot of a cleared unit is kept under its address",
     [SERVER]),
    ("res.preview-throttle-by-address-kept", "lumaserver.py",
     "        _preview_fresh_at.clear()" + NL + "    _preview_cache.invalidate()",
     "        pass" + NL + "    _preview_cache.invalidate()",
     "the preview throttle of cleared addresses is kept",
     [SERVER]),
    ("res.page-forgets-the-revision", "ui/matrix.js",
     "    inventoryEpoch = typeof data.inventory_epoch === 'string' ? data.inventory_epoch : null;",
     "    inventoryEpoch = null;",
     "the Matrix page never remembers which inventory it was built from",
     [PAGE]),
    ("res.page-does-not-send-the-revision", "ui/matrix.js",
     "          if (inventoryEpoch) postData.inventory_epoch = inventoryEpoch;",
     "",
     "the Matrix page sends routes without the revision, so a stale page is never refused",
     [PAGE]),
]
