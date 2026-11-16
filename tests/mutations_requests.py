"""Mutations for request-body handling, identity shape, listener hardening, log hygiene and the update matcher.

Entry format is the harness's:

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


BODIES = py("test_request_bodies.py", "NoBodyActsAsACommandTests", "RemoteAccessPortTests", "UnexpectedFailureTests")
MATRIX = py("test_request_bodies.py", "EveryRouteTests")
MAC = py("test_mac_shape.py")
LISTENER = py("test_remote_listener_hardening.py")
LOGS = py("test_log_hygiene.py")
ASSETS = py("test_update_assets.py")

REQUEST_MUTATIONS = [
    ("req.non-object-accepted", "luma_request.py",
     "    raise BodyError(NOT_AN_OBJECT)", "    return {}",
     "a list, number, string or boolean body is read as an empty request", [BODIES, MATRIX]),
    ("req.bad-json-accepted", "luma_request.py",
     "        raise BodyError(NOT_JSON)", "        return {}",
     "unparseable JSON is read as an empty request", [BODIES, MATRIX]),
    ("req.kind-check-off", "luma_request.py",
     "        if key in TEXT_FIELDS and not isinstance(value, str):", "        if False:",
     "a text field sent as a number reaches the handler and ends in an exception", [MATRIX]),
    ("req.install-hook-off", "luma_request.py",
     "        if request.method not in UNSAFE_METHODS:" + chr(10) + "            return None" + chr(10) +
     "        if not luma_remote.is_local_request(request.environ):",
     "        if True:" + chr(10) + "            return None" + chr(10) +
     "        if not luma_remote.is_local_request(request.environ):",
     "the early body check is gone, so a route that never calls the helper is unguarded", [MATRIX]),
    ("remote.enabled-not-required", "luma_remote_api.py",
     '        if not isinstance(data.get("enabled"), bool):', "        if False:",
     "a body that does not say 'enabled' switches Remote Access off and signs everyone out", [BODIES]),
    ("remote.port-unvalidated", "luma_remote_api.py",
     '                explicit_port = luma_remote.validate_port(data.get("port"))',
     '                explicit_port = data.get("port")',
     "an out-of-range port is accepted, reported and saved", [BODIES]),
    ("wall.empty-wall-saved", "lumaserver.py",
     '    if not (isinstance(wall.get("name"), str) and wall["name"].strip()',
     '    if False and not (isinstance(wall.get("name"), str) and wall["name"].strip()',
     "a body that describes no wall is stored as an empty Video Wall", [BODIES]),
    ("mac.identity-keeps-an-ipv4-spelling", "luma_users.py",
     "    if luma_mac.looks_like_ipv4(text):" + chr(10) + "        return " + chr(34) + chr(34),
     "    if False:" + chr(10) + "        return " + chr(34) + chr(34),
     "an address spelled with dashes is kept as an identity string instead of refused", [MAC]),
    ("mac.mixed-separators-accepted", "luma_mac.py",
     "([:-])(?:[0-9a-f]{2}" + BS + "1){4}", "([:-])(?:[0-9a-f]{2}[:-]){4}",
     "02:00-5e:10-00:4a is accepted as one address", [MAC]),
    ("perm.unreadable-identity-is-a-member", "luma_users.py",
     "        self.sources = frozenset(i for i in (_normalise_identity(s) for s in sources if s) if i)",
     "        self.sources = frozenset(_normalise_identity(s) for s in sources if s)",
     "an identity the normaliser refuses becomes the empty member that every unreadable identity matches", [MAC]),
    ("host.check-off", "luma_remote.py",
     "        if (self._listener == REMOTE and self._host_check is not None",
     "        if (False and self._host_check is not None",
     "the network listener serves any Host header", [LISTENER]),
    ("host.port-ignored", "luma_remote.py",
     "    if bound and port is not None and port != int(bound[1]):", "    if False:",
     "the right name on the wrong port is served", [LISTENER]),
    ("header.server-names-the-framework", "luma_remote.py",
     '            return "LumaSuite"', "            return super().version_string()",
     "the Server header names Werkzeug and Python again", [LISTENER]),
    ("header.nosniff-missing", "luma_request.py",
     '            response.headers.setdefault("X-Content-Type-Options", "nosniff")', "            pass",
     "remote responses lose nosniff", [LISTENER]),
    ("log.scan-body-dumped", "lumaserver.py",
     '    logging.debug("[api_scan] fields received: %s", sorted(str(k) for k in data))',
     '    logging.debug(f"[api_scan] received data: {data}")',
     "the scan request body, with any credential in it, is logged", [LOGS]),
    ("log.upgrade-body-dumped", "lumaserver.py",
     '    logging.debug("[api_upgrade] fields received: %s", sorted(str(k) for k in data))',
     '    logging.debug(f"[api_upgrade] received data: {data}")',
     "the upgrade body, which carries the device password, is logged", [LOGS]),
    ("update.suffix-ignored", "lumaserver.py",
     'name.startswith("lumasuite-") and name.endswith(suffix)', 'name.startswith("lumasuite-") and name.endswith("")',
     "any file of the release is offered to every platform", [ASSETS]),
    ("error.detail-leaks", "luma_request.py",
     '                    "error": "That did not work. The reason is in the LumaSuite log."}), status',
     '                    "error": str(exc)}), status',
     "an unexpected failure answers with the exception's own words", [BODIES]),
]
