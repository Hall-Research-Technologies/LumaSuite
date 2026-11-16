"""The one way a request body becomes a dictionary.

Every handler used to read its body with its own `request.get_json(silent=True) or {}`. That is
safe for an object and for nothing else: a body of `[]`, `5`, `"x"` or `true` reached handlers
that call `.get` on it and answered 500 with Python's own words, and a body that was simply wrong
(`[]`, `null`, `{`) turned into `{}`, which a handler that treats an absent field as "off" or
"empty" then obeyed -- `[]` switched Remote Access off and signed everyone out, and saved an
empty Video Wall.

So the rule is stated once:

* an empty body is an empty object (the caller sent nothing; handlers still check what they need);
* a JSON body must be an object, and anything else is refused with 400 and a plain sentence;
* a body that claims to be JSON and is not parseable is refused the same way;
* a body that does not claim to be JSON is ignored, as it always was (uploads, forms).

`install` applies the same rule before any handler runs, so a route that has not been converted
is still covered, and a handler that wants the dictionary asks `json_object()`.
"""
from flask import jsonify, request

import luma_remote

UNSAFE_METHODS = ("POST", "PUT", "PATCH", "DELETE")
NOT_AN_OBJECT = "The request body must be a JSON object."
NOT_JSON = "The request body is not valid JSON."


#: Fields that handlers read and treat as text (they call `.strip()`, build URLs and file names from
#: them, or compare them to stored identities). Sent as a number, a list or an object they used to end
#: in an exception, and in a few places in a half-applied change. The names are the ones the handlers
#: read; `tests/test_request_bodies.py` derives the field names from the source and sends every one of
#: them with a wrong kind to every route, so a handler that starts reading a new text field and is
#: not listed here is found by that test rather than by an operator.
TEXT_FIELDS = frozenset((
    "action", "adapter_id", "address", "audio_encoder_ip", "cert_path", "decoder_id", "decoder_ip",
    "encoder_ip", "fallback_password", "file", "firmware", "hostname", "id", "interface", "ip",
    "key_path", "mac", "method", "name", "newpassword", "oldpassword", "passphrase", "password",
    "region_id", "role", "server", "source_id", "streamname", "timezone", "transport", "username",
    "video_encoder_ip", "vwmatrix", "wall_id"))
#: Fields that must be a list when present.
LIST_FIELDS = frozenset(("ips", "members", "displays", "groups", "walls"))
#: ...except where a single address as text has always been accepted (the gate answers for it by name).
LIST_OR_TEXT = frozenset(("ips",))


class BodyError(Exception):
    """A request body that cannot be used. The message is safe to show."""

    def __init__(self, message):
        super().__init__(message)
        self.message = message


def _check_kinds(body):
    for key, value in body.items():
        if value is None:
            continue
        if key in TEXT_FIELDS and not isinstance(value, str):
            raise BodyError('The field "%s" must be text.' % key)
        if key in LIST_FIELDS and not isinstance(value, list) and not (key in LIST_OR_TEXT and isinstance(value, str)):
            raise BodyError('The field "%s" must be a list.' % key)


def json_object(force=False):
    """The request's JSON body as a dict; {} when there is no body; BodyError otherwise."""
    if request.method not in UNSAFE_METHODS:
        return {}
    claims_json = bool(request.is_json) or force
    if not claims_json:
        return {}
    parsed = request.get_json(silent=True, force=force)
    if isinstance(parsed, dict):
        _check_kinds(parsed)
        return parsed
    raw = request.get_data(cache=True)
    if not raw or not raw.strip():
        return {}
    if parsed is None and raw.strip() != b"null":
        raise BodyError(NOT_JSON)
    raise BodyError(NOT_AN_OBJECT)


def refusal(error):
    return jsonify({"ok": False, "reason": "bad_body", "error": error.message}), 400


def install(app, remote_limit):
    """Refuse an unusable JSON body before any handler, and give handlers the 400 for it.

    `remote_limit` is the byte limit of the network-facing listener. It is applied here, before
    the body is read, because this hook may be the first thing to read it.
    """

    @app.errorhandler(BodyError)
    def _bad_body(error):
        return refusal(error)

    @app.before_request
    def _check_body():
        if request.method not in UNSAFE_METHODS:
            return None
        if not luma_remote.is_local_request(request.environ):
            request.max_content_length = remote_limit
        try:
            json_object()
        except BodyError as error:
            return refusal(error)
        return None

    @app.after_request
    def _remote_headers(response):
        """Browser-facing hardening for what the network-facing listener serves.

        Deliberately conservative: no Content-Security-Policy, because the pages use inline script
        and style and a policy that breaks them is worse than none. `setdefault`, so a page that set
        a stricter value of its own (the no-referrer pages) keeps it.
        """
        if not luma_remote.is_local_request(request.environ):
            response.headers.setdefault("X-Content-Type-Options", "nosniff")
            response.headers.setdefault("X-Frame-Options", "DENY")
            response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response

    return app


def internal_error(exc, status=500):
    """The answer for an unexpected failure: what happened goes to the log, not to the client.

    The text of a Python exception names modules, attributes and sometimes values; it is of no use to
    an operator and tells a stranger how the program is built. The log has it, with the traceback.
    """
    import logging
    logging.getLogger(__name__).error("[%s %s] unexpected failure: %s", request.method, request.path,
                                      type(exc).__name__, exc_info=exc)
    return jsonify({"ok": False, "reason": "internal_error",
                    "error": "That did not work. The reason is in the LumaSuite log."}), status
