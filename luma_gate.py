"""The authorization gate: one check, in front of every route.

This is deliberately a `before_request` hook and not a decorator. A decorator
protects the routes somebody remembered to decorate, and the measured problem
here is precisely the routes nobody would think of: `/api/list_dir` enumerates
the host filesystem and `/api/producer/jsonrpc` will reboot a device, and
neither is a route you would decorate while writing a routing page.

So the gate runs for every request, consults the exhaustive table in
`luma_access`, and denies anything it cannot find an explicit rule for.

    arrived on the local listener?         -> local operator, allowed
    public route?                          -> allowed
    valid session?                         -> else 401
    CSRF valid for a state-changing verb?  -> else 403
    established by a Direct Access link,
    and this an administrative route?      -> else 403
    role permitted for this route?         -> else 403
    role=user: every device and group named
    in the request permitted to this account? -> else 403

The last step cannot be skipped. Being a routing user says an account may route;
it does not say *what* it may route. Source, destination and group are checked
against the account before the routing service is called, so crafting an HTTP
request gets the same answer as pressing a button.

Nothing here trusts a header for locality. Whether a request is local is a fact
about which listener accepted the connection (see `luma_remote`).
"""
from __future__ import annotations

import logging
from urllib.parse import quote

from flask import Response, g, jsonify, redirect, request

import luma_access
import luma_remote
import luma_request
from luma_access import Access

log = logging.getLogger(__name__)

#: Verbs that change something and therefore need a CSRF token.
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Header the browser sends the CSRF token in.
CSRF_HEADER = "X-LumaSuite-CSRF"

#: Cookie carrying the session token.
SESSION_COOKIE = "lumasuite_session"

#: Body keys naming a source device on the User routing endpoints.
SOURCE_KEYS = ("source", "source_id", "encoder", "encoder_id")

#: Body keys naming one destination (display).
DESTINATION_KEYS = ("destination", "destination_id", "display", "display_id",
                    "decoder", "decoder_id", "displays")

#: Body keys naming a display group.
GROUP_KEYS = ("group", "group_id", "groups")

#: Body keys naming a saved video wall.
WALL_KEYS = ("wall", "wall_id", "walls")

#: Largest request body the network-facing listener will read. It is applied before the
#: gate looks at the body, because the body is parsed before the access decision: a
#: urlencoded body is read whole (Werkzeug bounds it only by this limit) and a multipart
#: file part is spooled to disk, so an unauthenticated caller would otherwise choose how
#: much memory and disk to spend. Remote bodies are small JSON, and the one large one an
#: administrator sends is a 1280x720 or 1920x1080 slate image. The local listener is not
#: limited, so a backup restore from the local computer is exactly as it was.
REMOTE_MAX_BODY_BYTES = 16 * 1024 * 1024

#: Where an unauthenticated browser is sent.
LOGIN_PATH = "/login"


def wants_a_page(rule, method, environ) -> bool:
    """Whether this request is a browser navigating, rather than a program calling.

    A person who opens the remote URL in a browser must be given the sign-in
    page. A program calling an endpoint must be given a status code and JSON it
    can act on. Three conditions, all required, so this can never hand HTML to an
    API caller:

      * a GET or HEAD, because a navigation is never a POST here
      * a rule outside `/api/`, so every endpoint keeps its JSON contract
      * the client asks for HTML, and in preference to JSON

    `Sec-Fetch-Mode: navigate` settles it outright where the browser sends it.
    """
    if str(method or "").upper() not in ("GET", "HEAD"):
        return False
    if str(rule or "").startswith("/api/"):
        return False
    mode = (environ.get("HTTP_SEC_FETCH_MODE") or "").lower()
    if mode:
        return mode == "navigate"
    accept = (environ.get("HTTP_ACCEPT") or "").lower()
    if "text/html" not in accept:
        return False
    if "application/json" in accept:
        return accept.index("text/html") < accept.index("application/json")
    return True


def login_redirect(environ):
    """Send a browser to the sign-in page, remembering where it was going."""
    wanted = environ.get("PATH_INFO") or "/"
    query = environ.get("QUERY_STRING") or ""
    if query:
        wanted = wanted + "?" + query
    # The sign-in page validates this again before using it: an open redirect on
    # a sign-in page is how somebody is sent to a convincing copy of it.
    return redirect(LOGIN_PATH + "?next=" + quote(wanted, safe="/"), code=302)


#: A refusal a person can read. Self-contained: it needs no stylesheet, script or
#: image, so it renders whatever else is unreachable, and it cannot become a way
#: to fetch an asset without a session.
_REFUSED_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Not available &mdash; LumaSuite</title>
<style>
 :root {{ color-scheme: dark light; }}
 body {{ font: 15px/1.5 Calibri, system-ui, -apple-system, "Segoe UI", sans-serif;
        margin: 0; min-height: 100vh; display: flex; align-items: center;
        justify-content: center; background: #1d232a; color: #e9eef5; }}
 main {{ max-width: 30rem; padding: 2rem; }}
 h1 {{ font-size: 1.25rem; margin: 0 0 .5rem; }}
 p {{ margin: 0 0 1rem; color: #9aa7b4; }}
 a {{ color: #6cb6ff; }}
</style></head>
<body><main>
<h1>{heading}</h1>
<p>{detail}</p>
<p><a href="{back}">{back_label}</a></p>
</main></body></html>
"""


def refused_page(status, account=None):
    """The human answer to a refusal that is not "sign in"."""
    if status == 404:
        heading = "Not available here"
        detail = "This part of LumaSuite works only on the computer it is running on."
    else:
        heading = "You do not have access to this"
        detail = ("Your account is not permitted to open this page. If you "
                  "think it should be, ask an administrator.")
    back = "/user" if (account is not None and not account.is_admin) else "/"
    body = _REFUSED_PAGE.format(heading=heading, detail=detail, back=back,
                                back_label="Back to LumaSuite")
    return Response(body, status=status, mimetype="text/html")


class Decision:
    """Why a request was allowed or refused. Returned rather than raised so the
    caller decides the status code and the audit entry together."""

    __slots__ = ("allowed", "status", "reason", "principal")

    def __init__(self, allowed, status=200, reason="", principal=None):
        self.allowed = allowed
        self.status = status
        self.reason = reason
        self.principal = principal

    def __bool__(self):
        return self.allowed


ALLOW_LOCAL = Decision(True, 200, "local")


def _identities_in(payload, keys) -> list:
    """Every identity a request body names under `keys`, scalars and lists."""
    found = []
    if not isinstance(payload, dict):
        return found
    for key in keys:
        if key not in payload:
            continue
        value = payload[key]
        if isinstance(value, (list, tuple)):
            found.extend(str(v) for v in value if v not in (None, ""))
        elif value not in (None, "", {}):
            found.append(str(value))
    return found


def evaluate(rule, method, environ, payload, store, view_args=None) -> Decision:
    """Decide one request. Pure: no Flask globals, so it is directly testable."""
    access = luma_access.classify(rule, method, view_args)

    # 1. The local operator: a fact about which socket accepted the connection,
    #    not an inference from an address a request claims to come from.
    if luma_remote.is_local_request(environ):
        return ALLOW_LOCAL

    # Everything below here arrived from the network.

    # 2. A local-only route is never reachable remotely, whatever the role.
    if access is Access.LOCAL_ONLY:
        return Decision(False, 404, "local_only")

    # 3. The public surface.
    if access is Access.PUBLIC:
        return Decision(True, 200, "public")

    # 4. A session is required for everything else.
    token = _cookie(environ, SESSION_COOKIE)
    account, session = store.resolve_session(token) if store else (None, None)
    if account is None:
        return Decision(False, 401, "no_session")

    # 5. CSRF on anything that changes state. SameSite alone does not cover a
    #    top-level POST navigation.
    if method in UNSAFE_METHODS:
        presented = environ.get("HTTP_" + CSRF_HEADER.upper().replace("-", "_"))
        if not presented or presented != session.csrf_token:
            return Decision(False, 403, "csrf", account)

    # 6. How the holder proved who they were. A link-established session is
    #    barred from the administrative surface independently of the account's
    #    role: a role can change after a link is made, and an administrator's
    #    session must not be obtainable by opening a bookmark.
    from_link = bool(session is not None and getattr(session, "from_direct_link", False))
    if from_link and access is Access.ADMIN:
        return Decision(False, 403, "direct_link_not_admin", account)

    # 7. Role.
    if access is Access.AUTHENTICATED:
        return Decision(True, 200, "authenticated", account)
    if access is Access.ADMIN:
        if not account.is_admin:
            return Decision(False, 403, "not_admin", account)
        return Decision(True, 200, "admin", account)
    if access is Access.USER:
        # An administrator reviewing the user surface is not held to a user's
        # grants. A link-established session never takes this path, even on an
        # account since promoted: it would skip the per-device check below.
        if account.is_admin and not from_link:
            return Decision(True, 200, "admin", account)
        # 8. Per-resource authorization.
        return _check_resources(payload, account)

    # Unreachable by construction -- classify() never returns anything else --
    # but an unknown access class denies rather than falls through.
    return Decision(False, 403, "unclassified", account)


#: What a refusal means, in a sentence an operator can act on.
#:
#: One map, because a refusal can be produced either here or in the handler
#: depending on which check reaches it first, and the two must not read
#: differently for the same cause. The internal name travels beside it as
#: `reason`, for a log or a test; it is never what the operator is shown.
DENIAL_SENTENCES = {
    "csrf": "That page has been open a while. Reload it and try again.",
    "not_admin": "That area is for administrators.",
    "direct_link_not_admin":
        "A direct access link cannot reach the administrative area.",
    "source_not_permitted": "You do not have access to that source.",
    "destination_not_permitted": "You do not have access to that display.",
    "group_not_permitted": "You do not have access to that display group.",
    "wall_not_permitted": "You do not have access to that video wall.",
    "interface_not_permitted":
        "Your account is not set up to route that way. Ask an administrator.",
}


def _check_resources(payload, account) -> Decision:
    for identity in _identities_in(payload, SOURCE_KEYS):
        if not account.permissions.may_use_source(identity):
            return Decision(False, 403, "source_not_permitted", account)
    for identity in _identities_in(payload, DESTINATION_KEYS):
        if not account.permissions.may_control_destination(identity):
            return Decision(False, 403, "destination_not_permitted", account)
    for group_id in _identities_in(payload, GROUP_KEYS):
        if not account.permissions.may_control_group(group_id):
            return Decision(False, 403, "group_not_permitted", account)
    for wall_id in _identities_in(payload, WALL_KEYS):
        if not account.permissions.may_control_wall(wall_id):
            return Decision(False, 403, "wall_not_permitted", account)
    return Decision(True, 200, "permitted", account)


def _cookie(environ, name):
    """Read one cookie from the raw header, without needing a Flask request."""
    raw = environ.get("HTTP_COOKIE") or ""
    for part in raw.split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value
    return None


def install(app, store_getter, enabled_getter, destination_check=None):
    """Attach the gate to a Flask application.

    `store_getter` returns the UserStore; `enabled_getter` says whether remote
    access is on. Both are callables so the gate holds no state of its own and a
    test can drive it without a server.

    `destination_check(rule, payload)` returns None, or an object with `status`,
    `reason` and `message`, when a request names a device it may not reach. It
    runs only for a request that has already been authorized, and only on the
    network-facing listener.
    """

    @app.errorhandler(luma_request.BodyError)
    def _bad_body(error):
        # Every handler reads its body through luma_request; wherever the gate is installed, a body that
        # cannot be used is a 400 with a plain sentence, never an unhandled exception.
        return luma_request.refusal(error)

    @app.errorhandler(413)
    def _too_large(_error):
        return jsonify({"ok": False, "reason": "too_large",
                        "error": "That request is larger than this listener accepts."}), 413

    @app.before_request
    def _authorize():                      # pragma: no cover - exercised via client
        rule = str(request.url_rule.rule) if request.url_rule else request.path

        # A request that arrived locally is the existing local experience and is
        # untouched by any of this, which is what keeps LumaSuite working exactly
        # as before while remote access is off.
        if luma_remote.is_local_request(request.environ):
            g.principal = None
            g.is_local = True
            return None

        if not enabled_getter():
            # Nothing should reach the application from the network while remote
            # access is disabled; if something did, refuse it. A person can get
            # here (a saved Direct Access link opened after remote access was
            # turned off), so it is said both ways -- 404, not a redirect, because
            # there is no sign-in page to send them to either.
            if wants_a_page(rule, request.method, request.environ):
                return refused_page(404)
            return jsonify({"ok": False, "error": "remote access is disabled"}), 404

        payload = {}
        if request.method in UNSAFE_METHODS:
            request.max_content_length = REMOTE_MAX_BODY_BYTES
            try:
                payload = luma_request.json_object()
            except luma_request.BodyError as bad:
                # The gate may be the first reader of the body (it needs the fields to decide), so it
                # answers for a body that is not an object itself rather than relying on the app.
                return luma_request.refusal(bad)
            if not payload and request.form:
                payload = request.form.to_dict()
        else:
            payload = request.args.to_dict()

        store = store_getter()
        decision = evaluate(rule, request.method, request.environ, payload, store,
                            request.view_args)
        g.principal = decision.principal
        g.is_local = False

        if decision.allowed:
            refusal = destination_check(rule, payload) if destination_check else None
            if refusal is None:
                return None
            if store is not None:
                store.audit("access.denied",
                            actor=decision.principal,
                            detail={"rule": rule, "method": request.method,
                                    "reason": refusal.reason})
            return jsonify({"ok": False, "error": refusal.message,
                            "reason": refusal.reason}), refusal.status

        if store is not None and decision.reason not in ("no_session",):
            store.audit("access.denied",
                        actor=decision.principal,
                        detail={"rule": rule, "method": request.method,
                                "reason": decision.reason})

        # A person gets a page; a program gets a status code and JSON. The
        # refusal is identical either way -- this decides only how it is said.
        if wants_a_page(rule, request.method, request.environ):
            if decision.status == 401:
                return login_redirect(request.environ)
            return refused_page(decision.status, decision.principal)

        message = {401: "authentication required",
                   403: "not permitted",
                   404: "not found"}.get(decision.status, "denied")
        message = DENIAL_SENTENCES.get(decision.reason, message)
        reason = decision.reason or "denied"
        if reason == "unclassified":
            # An internal condition about the route table rather than about this
            # account: logged loudly, never disclosed.
            log.error("[GATE] %s %s is not classified in the route table",
                      request.method, rule)
            reason = "not_permitted"
        return jsonify({"ok": False, "error": message, "reason": reason}), decision.status

    return app
