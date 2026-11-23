"""HTTP surface for remote access: auth, bootstrap, user management, user state.

Registered onto the existing application rather than living in it, so the new
trust-sensitive surface is reviewable in one file instead of being scattered
through ten thousand lines of device code.

Every route added here is classified in luma_access.ROUTES; the fence in
tests/test_remote_access.py fails if one is not, so nothing registered here can
reach the network without somebody having decided who may call it.
"""
from __future__ import annotations

import logging
import pathlib
import threading
import time

from flask import Response, copy_current_request_context, g, jsonify, request

import luma_adapters
import luma_audit_text
import luma_gate
import luma_groups
import luma_inventory
import luma_walls
import luma_locks
import luma_previewstats
import luma_projection
import luma_retention
import luma_remote
import luma_request
import luma_user_matrix
import luma_users
from luma_gate import SESSION_COOKIE
from luma_users import Permissions

log = logging.getLogger(__name__)


#: The name refused Direct Access attempts are throttled under.
#:
#: Not a username, and deliberately one that cannot be created: throttling is
#: per name and per source address, and a link refusal has no account to
#: attribute itself to -- that is the whole point of answering the same way
#: whatever the reason.
_DIRECT_THROTTLE_KEY = "\x00direct-access-link"


#: What somebody sees when a Direct Access link no longer works.
#:
#: One sentence, and deliberately the same sentence for every reason: an
#: unknown token, a revoked one, a disabled account, a deleted account, an
#: account promoted to administrator. Whoever is holding an old URL learns only
#: that it does not work, which is all they are entitled to know.
#:
#: Self-contained, so it needs no stylesheet, no script and no image, and
#: renders on a phone with nothing else available.
_LINK_REFUSED = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Link not valid &mdash; LumaSuite</title>
<style>
 :root { color-scheme: dark light; }
 body { font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
        margin: 0; min-height: 100vh; display: flex; align-items: center;
        justify-content: center; background: #16181d; color: #e6e8ee; }
 main { max-width: 28rem; padding: 2rem; text-align: center; }
 h1 { font-size: 1.2rem; margin: 0 0 .6rem; }
 p { margin: 0; color: #a9b0c0; }
</style></head>
<body><main>
<h1>This LumaSuite access link is no longer valid</h1>
<p>Ask whoever set up this device for a new link, or sign in with a username
and password.</p>
</main></body></html>
"""


def _client_address():
    """Best available source address, for throttling and the audit trail only.

    Never used for a trust decision -- that is the listener's job. Recording it
    is still worth doing: "five failures from one address" is the shape of an
    attack, and the audit log is where it becomes visible.
    """
    return request.environ.get("REMOTE_ADDR") or "unknown"


def _principal():
    return getattr(g, "principal", None)


def _is_local():
    return bool(getattr(g, "is_local", False))


def register(app, *, store, listener, aliases, groups, load_cache, route_av,
             observe, remote_enabled, set_remote_state, remote_config,
             locks, tls, cert_paths, tls_context_for, certificate_state,
             preview_fetch, user_shell, walls=None, route_wall=None, route_mode=None,
             identify=None, verify_route=None, unroute=None):
    """Attach the remote-access API.

    Everything the module needs is passed in rather than imported from the
    server, so this file has no opinion about how devices are read and cannot
    accidentally start reading them.
    """

    # ---------------------------------------------------------------- auth
    @app.route("/api/auth/login", methods=["POST"])
    def api_auth_login():
        data = luma_request.json_object()
        username = str(data.get("username") or "").strip()
        password = str(data.get("password") or "")
        result = store.authenticate(username, password, source=_client_address())
        if not result["ok"]:
            status = 429 if result.get("throttled") else 401
            return jsonify({"ok": False, "error": result["error"]}), status

        body = {"ok": True, "user": result["user"], "csrf_token": result["csrf_token"]}
        response = jsonify(body)
        # HttpOnly: the session token is never readable by page script, so an
        # injected script cannot exfiltrate it. The CSRF token is returned in
        # the body on purpose -- it has to be readable to be echoed back, and it
        # is useless without the cookie.
        response.set_cookie(
            SESSION_COOKIE, result["token"],
            httponly=True, samesite="Lax",
            secure=request.is_secure, path="/")
        return response

    @app.route("/api/auth/logout", methods=["POST"])
    def api_auth_logout():
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            store.revoke(token)
            store.audit("logout", actor=_principal(), detail={})
        response = jsonify({"ok": True})
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @app.route("/api/auth/session", methods=["GET"])
    def api_auth_session():
        account = _principal()
        if account is None:
            # The local operator has no account and needs none. Saying so lets
            # one page serve both without the browser guessing.
            return jsonify({"ok": True, "local": True, "role": "admin",
                            "username": None,
                            # Presentation only: whether the shell offers the
                            # Remote Access area. The server gates it either way.
                            "remote_enabled": bool(remote_enabled())})
        # The CSRF token for the session this request arrived on.
        #
        # The sign-in page keeps the one it was handed, but a session opened by
        # a Direct Access link never went through that page -- it was redirected
        # straight to the shell -- so without this every action it tried would
        # be refused for a missing token. Returning it is not a weakening: it is
        # meant to be readable by the page, and it is useless without the
        # HttpOnly cookie that is never readable at all.
        _account, session = store.resolve_session(
            request.cookies.get(SESSION_COOKIE))
        return jsonify({**luma_projection.session_summary(account),
                        "local": False,
                        "remote_enabled": bool(remote_enabled()),
                        "csrf_token": session.csrf_token if session else "",
                        # So a page can tell the operator how they got here, and
                        # so a test can prove the two are distinguishable.
                        "method": session.method if session else ""})

    # ----------------------------------------------------------- bootstrap
    @app.route("/api/bootstrap/state", methods=["GET"])
    def api_bootstrap_state():
        return jsonify({"ok": True,
                        "needs_admin": not store.has_admin,
                        "accounts": 0 if store.is_empty else len(store.list_accounts())})

    @app.route("/api/bootstrap/admin", methods=["POST"])
    def api_bootstrap_admin():
        """Create the first administrator.

        Classified local-only, so this is reachable from the machine running
        LumaSuite and from nowhere else. That is the whole security property:
        somebody at the host already has full control of the application, and a
        remote client can never reach this route to make itself an
        administrator.

        It also refuses once an administrator exists, so it cannot be used a
        second time to add one.
        """
        if store.has_admin:
            return jsonify({"ok": False,
                            "error": "an administrator already exists"}), 409
        data = luma_request.json_object()
        username = str(data.get("username") or "").strip()
        password = str(data.get("password") or "")
        confirm = str(data.get("confirm") or "")
        if password != confirm:
            return jsonify({"ok": False, "error": "passwords do not match"}), 400
        # The generic account-created event is suppressed: creating the
        # first administrator on a machine that had none is a distinct and
        # more meaningful thing, and recording both says one action happened
        # twice.
        result = store.create(username, password, luma_users.ROLE_ADMIN,
                              audit=False)
        if not result["ok"]:
            return jsonify(result), 400
        store.audit("bootstrap.admin_created", actor=None,
                    detail={"username": username})
        return jsonify(result)

    # ------------------------------------------------------ remote access
    @app.route("/api/remote_access", methods=["GET"])
    def api_remote_access_state():
        # Enumerated with a stable identity, so a selection can survive the
        # address changing. `usable_bind_addresses` still has the final say on
        # what may be bound -- this adds identity, it does not widen the gate.
        try:
            enumerated = luma_adapters.enumerate_adapters()
        except Exception:
            enumerated = []
        adapters = [a for a in enumerated
                    if luma_remote.is_usable_remote_address(a.get("address"))]
        state = listener.describe()
        saved = remote_config()
        # Configured and running are different facts, and an installed system
        # needs to be able to tell them apart: a saved address whose adapter has
        # gone away leaves remote access configured and unavailable, which is
        # neither "on" nor "off".
        configured = bool(saved.get("enabled"))
        # Available means the chosen ADAPTER is here, not that the address it
        # had last time is. That distinction is the defect this fixes.
        chosen = saved.get("adapter_id") or ""
        available = (any(a.get("id") == chosen for a in adapters) if chosen
                     else any(a.get("address") == saved.get("address")
                              for a in adapters))
        # What the certificate is, never what the key is.
        certificate = certificate_state()
        # HTTP until the administrator chooses otherwise: a certificate made for
        # one DHCP address stops matching when the lease changes, so a first-run
        # HTTPS default would be a connection no browser accepts a day later.
        # HTTPS stays one choice away, and turning plaintext on still needs the
        # administrator's explicit acknowledgement in the request.
        transport = saved.get("transport") or "http"
        _context, tls_error = tls_context_for(saved)

        # Does the certificate actually cover the address being served?
        #
        # A certificate made for one adapter and then used on another is a
        # connection no browser will accept without an override, and Settings
        # previously said "Self-signed certificate in place. For <the other
        # address>" as though that were a satisfactory state of affairs.
        # Decided from the SANs; see luma_tls.covers.
        #
        # The address the certificate has to cover is the one being served if
        # the listener is up, and otherwise the one the chosen adapter carries
        # now. Resolved here from the list already enumerated above rather than
        # through the restore path, because reading a page must not be what
        # re-persists a configuration.
        effective = state.get("host") or ""
        if not effective and chosen:
            effective = luma_adapters.resolve_address(
                chosen, saved.get("address") or "", adapters)[0]
        elif not effective:
            effective = saved.get("address") or ""
        certificate = dict(certificate)
        certificate["for_address"] = effective
        certificate["matches_address"] = bool(
            effective and certificate.get("readable")
            and tls.covers(certificate, effective))
        certificate["generated"] = not bool(saved.get("cert_path"))
        # Whether a TLS context can really be built from the configured pair,
        # asked of the loader the listener itself uses and whatever transport is
        # currently selected. A certificate file that parses is not a certificate
        # that can serve: its key may be missing or belong to another one, and
        # "active" must not be claimed for it. (Same field, same meaning, as
        # OmniSuite's state, so the page can reason about both identically.)
        _https_context, https_error = tls_context_for(
            dict(saved, transport="https"))
        certificate["usable"] = bool(certificate.get("present")) and not https_error
        certificate["unusable_reason"] = https_error if certificate.get("present") else ""
        return jsonify({
            "ok": True,
            **state,
            "adapters": adapters,
            "configured": configured,
            "configured_address": saved.get("address") or "",
            "configured_adapter": saved.get("adapter_id") or "",
            "configured_adapter_name": saved.get("adapter_name") or "",
            # The listener is bound to one address and its adapter now carries another. Said
            # plainly; never acted on (no rebind, no new certificate).
            "address_moved": _address_moved(state, saved, enumerated),
            # Direct links last handed out for another address than the one now
            # being served. Names only; never a token or a URL.
            "direct_links_stale": _stale_direct_links(),
            "configured_available": bool(configured and available),
            "restore_error": saved.get("restore_error") or "",
            "has_admin": store.has_admin,
            "sessions": store.active_session_count(),
            # The transport that is configured, whether it can start, and what
            # is actually serving. All three, because they can differ: a
            # configuration asking for HTTPS whose certificate is unusable is
            # neither "secure" nor "insecure", it is "not running".
            "transport": transport,
            "encrypted": bool(state.get("secure")),
            "tls_error": tls_error,
            "certificate": certificate,
        })

    @app.route("/api/remote_access", methods=["POST"])
    def api_remote_access_set():
        data = luma_request.json_object()
        # Only a boolean is an answer. A body that does not say (an empty object, a list that was
        # refused upstream, a field of the wrong kind) must never be read as "off": that turned a
        # malformed request into a command that stopped Remote Access and signed everyone out.
        if not isinstance(data.get("enabled"), bool):
            return jsonify({"ok": False, "reason": "bad_body",
                            "error": "Say whether remote access should be on or off "
                                     "(\"enabled\": true or false)."}), 400
        want = data["enabled"]
        explicit_port = None
        if want and data.get("port") is not None:
            # The same rule as the local application port, decided before anything changes or is
            # saved. The listener used to fall back to some other port and report this one.
            try:
                explicit_port = luma_remote.validate_port(data.get("port"))
            except luma_remote.PortError as exc:
                return jsonify({"ok": False, "reason": "bad_port", "error": str(exc)}), 400
        if not want:
            listener.stop()
            revoked = store.revoke_every_session()
            store.audit("remote_access.disabled", actor=_principal(),
                        detail={"sessions_revoked": revoked})
            set_remote_state(False, None, None)
            return jsonify({"ok": True, **listener.describe()})

        if not store.has_admin:
            return jsonify({
                "ok": False,
                "error": "create an administrator account before enabling "
                         "remote access"}), 409
        address = str(data.get("address") or "").strip()
        adapter_id = str(data.get("adapter_id") or "").strip()
        port = explicit_port or request.environ.get("SERVER_PORT") or 8080
        transport = "http" if data.get("transport") == "http" else "https"

        # An adapter chosen by identity decides the address, so a stale one in
        # the request body cannot send the listener somewhere else.
        adapter_name = ""
        if adapter_id:
            try:
                enumerated = luma_adapters.enumerate_adapters()
            except Exception:
                enumerated = []
            resolved, why = luma_adapters.resolve_address(adapter_id, address,
                                                          enumerated)
            if not resolved:
                return jsonify({
                    "ok": False,
                    "error": ("That network adapter is no longer available on "
                              "this computer." if why == "adapter missing"
                              else "That adapter has several addresses; "
                                   "choose which one to use.")}), 400
            address = resolved
            adapter_name = next((a["name"] for a in enumerated
                                 if a.get("id") == adapter_id), "")
        else:
            # No identity offered: adopt the one that owns this address, if
            # exactly one does. Anything else is refused rather than guessed.
            try:
                enumerated = luma_adapters.enumerate_adapters()
            except Exception:
                enumerated = []
            owner = luma_adapters.adopt_saved_address(address, enumerated)
            if owner is not None:
                adapter_id, adapter_name = owner["id"], owner["name"]

        # Turning on plaintext remote access is possible, but only when the
        # administrator says so in as many words. It is not a default and it is
        # not reachable by omitting a field, nor by a field that merely reads as
        # true ("yes", "false", "true", 1): only the boolean true is an answer.
        if transport == "http" and data.get("accept_insecure") is not True:
            return jsonify({
                "ok": False,
                "error": "Remote access without HTTPS sends sign-in details in "
                         "clear. Confirm you intend that, or configure a "
                         "certificate.",
                "needs_confirmation": True}), 400

        # A certificate the administrator supplied was saved when they chose it. Turning
        # remote access on must keep using it: reading the paths only from this request
        # (which the Settings page does not send) ignored the chosen certificate and then
        # overwrote the saved choice with blanks.
        saved_choice = remote_config()
        proposed = {"transport": transport,
                    "cert_path": str(data.get("cert_path") or saved_choice.get("cert_path") or ""),
                    "key_path": str(data.get("key_path") or saved_choice.get("key_path") or "")}
        context, tls_error = tls_context_for(proposed)
        if tls_error:
            # Never start in the clear because TLS could not be arranged, and
            # never make a certificate or choose HTTP on the administrator's
            # behalf. The refusal says WHICH kind of problem it is, so the page
            # can offer the decision (create one, use HTTP, or stop). A
            # certificate the site supplied is never offered a replacement:
            # that case is reported as itself.
            described = certificate_state()
            supplied = bool(proposed["cert_path"])
            # (Missing and unreadable keep the one reason this product has
            # always used; only the supplied case is new, because it is the one
            # the page must not answer by offering to create a certificate.)
            reason = ("supplied_certificate_unusable" if supplied
                      else "no_usable_certificate")
            return jsonify({"ok": False, "error": tls_error,
                            "needs_certificate": not supplied,
                            "reason": reason,
                            "certificate": described}), 400

        # A certificate for one address, about to be served on another.
        #
        # This is reachable in two ordinary ways: the administrator changes
        # which adapter Remote Access uses, or the chosen adapter's own address
        # changes and the listener correctly follows it. Either way a
        # certificate generated for the old address is no longer valid for the
        # new one, and starting anyway would advertise HTTPS at an address no
        # browser will accept while Settings reported normal secure operation.
        #
        # LumaSuite's OWN certificate is refused rather than quietly reissued:
        # regenerating changes the fingerprint, and the fingerprint is the one
        # thing an administrator has to go on when their browser warns. So the
        # action is theirs, it is one button, and this says which button.
        #
        # A SUPPLIED certificate is never touched and never blocks. It may
        # legitimately carry a DNS name the site reaches LumaSuite by, which
        # this has no way of knowing about; refusing would break a correct
        # deployment. The mismatch is reported instead, and the status says so.
        if transport == "https":
            described = certificate_state()
            supplied = bool(proposed["cert_path"])
            if (not supplied and described.get("readable")
                    and not tls.covers(described, address)):
                return jsonify({
                    "ok": False,
                    "error": (
                        "The certificate LumaSuite created is for "
                        f"{(described.get('san_ips') or [None])[0] or described.get('subject') or 'another address'}, not "
                        f"for {address}. Create one for this network, then "
                        "turn remote access on."),
                    "needs_certificate": True,
                    "reason": "certificate_mismatch",
                    "certificate": {**described, "for_address": address,
                                    "matches_address": False,
                                    "generated": True}}), 400

        cert_names = []
        if transport == "https":
            cert_names = (tls.describe(proposed["cert_path"] or str(cert_paths[0])).get("san") or [])
        result = listener.start(address, port, ssl_context=context, allowed_names=cert_names)
        if not result.get("ok"):
            return jsonify(result), 400
        store.audit("remote_access.enabled", actor=_principal(),
                    detail={"address": address, "port": int(port),
                            "transport": transport, "adapter": adapter_name})
        set_remote_state(True, address, int(port), transport,
                         proposed["cert_path"], proposed["key_path"],
                         adapter_id, adapter_name)
        return jsonify({**result, "certificate": certificate_state()})

    @app.route("/api/admin/certificate", methods=["POST"])
    def api_admin_certificate():
        """Create a self-signed certificate, or point at a supplied pair.

        Nothing about a private key comes back out of here. The response
        describes the certificate -- subject, issuer, validity, fingerprint --
        which is what an administrator needs to confirm the thing their browser
        warned about is the one LumaSuite is serving.
        """
        data = luma_request.json_object()
        action = str(data.get("action") or "").strip()

        if action == "generate":
            address = str(data.get("address") or "").strip()
            cert_path, key_path = cert_paths
            # A supplied certificate belongs to the site and is never replaced
            # from here. Removing it is a separate, deliberate action.
            if remote_config().get("cert_path"):
                return jsonify({"ok": False, "error": (
                    "A certificate you supplied is in use. LumaSuite will not "
                    "replace it. Remove it first if you want LumaSuite to "
                    "create one.")}), 409
            # Regenerating is explicit (this request) and never automatic; the
            # outgoing fingerprint is reported so the change is visible.
            previous = tls.describe(cert_path).get("fingerprint") or ""
            try:
                described = tls.generate_self_signed(address, cert_path, key_path)
            except tls.TlsError as exc:
                return jsonify({"ok": False, "error": str(exc)}), 400
            store.audit("certificate.generated", actor=_principal(),
                        detail={"address": address,
                                "fingerprint": described.get("fingerprint"),
                                "previous_fingerprint": previous})
            # Stored as the configured pair so a restart uses it.
            saved = remote_config()
            set_remote_state(saved.get("enabled"), saved.get("address"),
                             saved.get("port"), "https", "", "",
                             saved.get("adapter_id"), saved.get("adapter_name"))
            return jsonify({"ok": True, "certificate": certificate_state(),
                            "previous_fingerprint": previous})

        if action == "use":
            cert_path = str(data.get("cert_path") or "").strip()
            key_path = str(data.get("key_path") or "").strip()
            try:
                tls.load_context(cert_path, key_path)
            except tls.TlsError as exc:
                return jsonify({"ok": False, "error": str(exc)}), 400
            tls.restrict_existing(key_path)
            saved = remote_config()
            set_remote_state(saved.get("enabled"), saved.get("address"),
                             saved.get("port"), "https", cert_path, key_path,
                             saved.get("adapter_id"), saved.get("adapter_name"))
            store.audit("certificate.configured", actor=_principal(),
                        detail={"certificate": pathlib.Path(cert_path).name})
            return jsonify({"ok": True, "certificate": certificate_state()})

        if action == "remove":
            # Not while it is in use: removing the certificate under a running
            # HTTPS listener would drop every signed-in browser and leave the
            # machine unreachable, which is not something to do as a side
            # effect of tidying up.
            if listener.running and listener.describe().get("secure"):
                return jsonify({
                    "ok": False,
                    "error": "Turn remote access off before removing the "
                             "certificate it is using."}), 409

            saved = remote_config()
            cert_path, key_path = cert_paths
            removed = []
            # Only LumaSuite's own pair is deleted. A certificate the site
            # supplied belongs to the site: that selection is cleared so
            # LumaSuite stops using it, and the files are left where they are.
            if not saved.get("cert_path"):
                for path in (pathlib.Path(cert_path), pathlib.Path(key_path)):
                    try:
                        if path.exists():
                            path.unlink()
                            removed.append(path.name)
                    except OSError as exc:
                        return jsonify({
                            "ok": False,
                            "error": f"Could not remove {path.name}: "
                                     f"{exc.strerror or exc}"}), 500
            set_remote_state(saved.get("enabled"), saved.get("address"),
                             saved.get("port"), saved.get("transport"),
                             "", "",
                             saved.get("adapter_id"), saved.get("adapter_name"))
            store.audit("certificate.removed", actor=_principal(),
                        detail={"files": removed,
                                "supplied": bool(saved.get("cert_path"))})
            return jsonify({"ok": True, "removed": removed,
                            "certificate": certificate_state()})

        return jsonify({"ok": False, "error": "unknown action"}), 400

    # -------------------------------------------------- admin: accounts
    @app.route("/api/admin/users", methods=["GET"])
    def api_admin_users():
        return jsonify({"ok": True, "users": store.list_accounts()})

    def _interfaces_refusal(raw):
        """A refusal when a request switches both routing interfaces off, else None.

        An account that may route must have a way to do it. Said here, on the
        explicit request, because Permissions itself quietly keeps Standard when it
        reads a record with neither (a restored backup must never lock someone out).
        """
        if (isinstance(raw, dict) and raw.get("standard_routing") is False
                and raw.get("matrix_routing") is False):
            return jsonify({"ok": False, "reason": "no_routing_interface",
                            "error": "Choose at least one routing interface."}), 400
        return None

    @app.route("/api/admin/users", methods=["POST"])
    def api_admin_create_user():
        data = luma_request.json_object()
        if str(data.get("role") or luma_users.ROLE_USER) == luma_users.ROLE_USER:
            refusal = _interfaces_refusal(data.get("permissions"))
            if refusal is not None:
                return refusal
        # The store records this, with the actor passed down. It is the
        # layer that knows what was actually created -- the request body is
        # what was asked for, which is not the same thing when it was refused
        # or normalised.
        result = store.create(
            str(data.get("username") or "").strip(),
            str(data.get("password") or ""),
            str(data.get("role") or luma_users.ROLE_USER),
            _permissions_from(data.get("permissions")),
            actor=_principal())
        if not result["ok"]:
            return jsonify(result), 400
        return jsonify(result)

    @app.route("/api/admin/users/<user_id>", methods=["POST"])
    def api_admin_update_user(user_id):
        """One endpoint, one explicit action, named in the body.

        `action` rather than a merge of whatever fields arrived: a permission
        change and a password reset need different audit entries and different
        session consequences, and a partial-update endpoint makes it easy to
        perform one while believing you performed the other.
        """
        data = luma_request.json_object()
        action = str(data.get("action") or "").strip()
        actor = _principal()

        # Each of these records its own event, in the store, naming the
        # account rather than its id and saying what actually changed --
        # "Account disabled", not "set_enabled". The actor is passed down
        # because it is the one thing only this layer knows.
        if action == "set_enabled":
            result = store.set_enabled(user_id, bool(data.get("enabled")),
                                       actor=actor)
        elif action == "set_role":
            result = store.set_role(user_id, str(data.get("role") or ""),
                                    actor=actor)
        elif action == "set_password":
            result = store.set_password(user_id, str(data.get("password") or ""),
                                        actor=actor)
        elif action == "set_permissions":
            target = store.get(user_id)
            if target is not None and not target.is_admin:
                refusal = _interfaces_refusal(data.get("permissions"))
                if refusal is not None:
                    return refusal
            result = store.set_permissions(
                user_id, _permissions_from(data.get("permissions")), actor=actor)
        elif action == "delete":
            result = store.delete(user_id, actor=actor)
        else:
            return jsonify({"ok": False, "error": "unknown action"}), 400

        if not result.get("ok"):
            return jsonify(result), 400
        return jsonify(result)

    # ------------------------------------------------- direct access links
    #
    # A passwordless URL for one restricted account. Off by default, turned on
    # per account by an administrator, and never available on an
    # administrator's own account.
    #
    # The token is a bearer credential, so it appears in exactly two places: in
    # the link handed to whoever asked for it, and hashed in the account
    # record. It is never logged, never audited, and never put in a response
    # that was not a deliberate request for the link itself.

    def _direct_link(token):
        """The URL to hand out, built on the address remote access advertises.

        Never a loopback URL: the whole point is that somebody opens it from a
        phone. If remote access is not running there is no origin to build one
        from, and that is reported rather than papered over with an address
        that would not work.
        """
        state = listener.describe()
        if not state.get("enabled") or not state.get("host"):
            return "", ("Remote access is off, so this link cannot be reached "
                        "from another device yet. It will work once remote "
                        "access is turned on.")
        return (f"{state.get('scheme') or 'https'}://{state['host']}:"
                f"{state['port']}/access/{token}"), ""

    def _direct_payload(account, token):
        link, note = _direct_link(token) if token else ("", "")
        body = {"ok": True,
                "direct_access": account.direct_access.public(),
                "username": account.username,
                # Empty when the stored copy cannot be read on this machine.
                # The page says "regenerate", which is the only thing that
                # helps.
                "link": link,
                "recoverable": bool(token),
                "note": note}
        if token and not link:
            body["note"] = note
        return body

    def _current_origin():
        """scheme://host:port while remote access is serving, else empty."""
        state = listener.describe()
        if not state.get("enabled") or not state.get("host"):
            return ""
        return (f"{state.get('scheme') or 'https'}://{state['host']}:"
                f"{state['port']}")

    def _address_moved(state, saved, adapters=None):
        """Where Remote Access is bound versus where its chosen adapter is NOW, or None.

        Derived from the live interface list on every call (no polling, nothing written, no
        interface changed). Only a positive finding is reported: the listener is up, the chosen
        adapter is here, and it no longer carries the address the listener is bound to. An
        adapter that is missing, or one with several addresses that still includes the bound one,
        is not "moved". Nothing is rebound and nothing is reissued by saying so.
        """
        host = state.get("host") or ""
        chosen = saved.get("adapter_id") or ""
        if not (state.get("enabled") and host and chosen):
            return None
        try:
            live = adapters if adapters is not None else luma_adapters.enumerate_adapters()
        except Exception:
            return None
        current, _why = luma_adapters.resolve_address(chosen, host, live)
        if not current or current == host:
            return None
        return {"from": host, "to": current,
                "message": (f"This computer's address changed from {host} to {current}. "
                            f"Remote Access is still listening on {host}. Turn Remote Access "
                            "off and on to move it.")}

    def _stale_direct_links():
        """Usernames whose direct link was last handed out for another address.

        Derived from the current listener and what each link was issued for, on
        every call, so a late change is seen without anybody rescanning. An
        origin that was never recorded is unknown and is not counted: unknown is
        not the same as changed.
        """
        origin = _current_origin()
        if not origin:
            return []
        return sorted(name for name, issued in store.direct_issued_origins().values()
                      if issued and issued != origin)

    def _admin_direct_payload(account, token):
        """The admin's view of a link: the payload plus where it points now.

        The token is unchanged by a change of address and still names the same
        account, so the NEW link is simply this one built on the new origin. What
        is added is the fact that the address moved: `address_changed`, the
        origin the link was last handed out for, and the origin it is for now. The
        old URL is never rebuilt or returned -- it carries the token, and there is
        no reason to produce a second copy of a credential.
        """
        body = _direct_payload(account, token)
        origin = _current_origin()
        issued = account.direct_access.issued_origin
        body["link_origin"] = origin
        moved = _address_moved(listener.describe(), remote_config())
        # While the listener is still on the old address the link below IS the address that works;
        # it must not be read as current at the new one, so the move is stated beside it.
        body["address_moved"] = moved
        body["issued_origin"] = issued
        body["address_changed"] = bool(token and origin and issued
                                       and issued != origin)
        if token and origin and not issued:
            # A link from before the address was recorded: learn it now, on the
            # request that is the administrator looking at it. Never overwrites.
            store.note_direct_origin(account.id, origin)
        return body

    @app.route("/api/admin/users/<user_id>/direct_access",
               methods=["GET", "POST"])
    def api_admin_direct_access(user_id):
        actor = _principal()
        if request.method == "GET":
            account = store.get(user_id)
            if account is None:
                return jsonify({"ok": False, "error": "no such user"}), 404
            if not account.direct_access.active:
                return jsonify({"ok": True, "username": account.username,
                                "direct_access": account.direct_access.public(),
                                "link": "", "recoverable": False, "note": ""})
            return jsonify(_admin_direct_payload(
                account, store.direct_access_token(user_id)))

        data = luma_request.json_object()
        action = str(data.get("action") or "").strip()
        if action == "enable":
            result = store.enable_direct_access(user_id, actor=actor,
                                                origin=_current_origin())
        elif action == "regenerate":
            result = store.regenerate_direct_access(user_id, actor=actor,
                                                    origin=_current_origin())
        elif action == "acknowledge_address":
            # The administrator saying the new link has been passed on. Records
            # the address only; the token, its sessions and everything else are
            # left exactly as they are.
            origin = _current_origin()
            if not origin:
                return jsonify({"ok": False, "error": (
                    "Remote access is off, so there is no address to "
                    "acknowledge yet.")}), 400
            if not store.note_direct_origin(user_id, origin, actor=actor,
                                            acknowledge=True):
                account = store.get(user_id)
                if account is None or not account.direct_access.active:
                    return jsonify({"ok": False,
                                    "error": "direct access is not enabled"}), 400
            account = store.get(user_id)
            return jsonify(_admin_direct_payload(
                account, store.direct_access_token(user_id)))
        elif action == "disable":
            result = store.disable_direct_access(user_id, actor=actor)
        else:
            return jsonify({"ok": False, "error": "unknown action"}), 400
        if not result.get("ok"):
            return jsonify({"ok": False, "error": result["error"]}), 400

        account = store.get(user_id)
        if action == "disable":
            return jsonify({"ok": True, "username": account.username,
                            "direct_access": account.direct_access.public(),
                            "link": "", "recoverable": False, "note": ""})
        # The freshly minted token comes back from the store rather than being
        # read again, so the one moment it exists in memory is this one.
        return jsonify(_admin_direct_payload(account, result["token"]))

    @app.route("/access/<token>", methods=["GET"])
    def direct_access_entry(token):
        """Open a restricted session from a bearer link and serve the panel.

        The token is exchanged for an ordinary server-side session, and that
        session is what authorises everything afterwards: the token is never
        sent again, never put in an API payload, and never stored by the page.

        The shell is served *at this URL* rather than redirected to /user, so
        the link stays bookmarkable and a refresh works. That is a deliberate
        trade: the credential remains in the address bar, and therefore in a
        history and in anything that screenshots the screen. It is accepted
        because the URL is the credential by design -- there is nothing else
        to present -- and it is bounded by `Referrer-Policy: no-referrer`, by
        the link being revocable and replaceable in one action, and by the
        session it opens being barred from the administrative surface however
        the account's role later changes.
        """
        source = _client_address()
        # A refused link is throttled like a refused password, for two
        # reasons. It bounds the cost of asking; and without it, requests for
        # `/access/<anything>` would write an audit entry each, pushing the
        # sign-ins and refusals an administrator needs out of a history that
        # keeps a fixed amount and discards the oldest.
        # The token is resolved *before* the throttle is consulted. A throttle on
        # refused links exists to bound the cost of asking and to keep guesses
        # from flooding the audit log; a 256-bit token cannot be guessed, so a
        # link that is valid is never refused for what other links did. Checking
        # that an old link is dead (a few refusals from one browser) used to lock
        # the address out, and the freshly regenerated link was refused too --
        # which looks exactly like "regenerate does not work".
        account = store.resolve_direct_token(token)
        if account is None:
            if store.locked_out(_DIRECT_THROTTLE_KEY, source):
                return Response(_LINK_REFUSED, status=404, mimetype="text/html")
            # One answer for every reason: unknown, revoked, disabled, deleted,
            # promoted. Saying which would tell whoever has an old URL
            # something about an account they have no business knowing.
            store.record_failure(_DIRECT_THROTTLE_KEY, source)
            store.audit("direct_access.used", actor=None,
                        detail={"result": "refused"})
            return Response(_LINK_REFUSED, status=404, mimetype="text/html")

        session = store.open_session(account.id,
                                     method=luma_users.BY_DIRECT_LINK)
        store.audit("direct_access.used", actor=account,
                    detail={"result": "accepted",
                            "username": account.username,
                            "method": luma_users.BY_DIRECT_LINK})
        # The shell itself, at this URL. Served through the same helper the
        # /user route uses, so there is one copy of the page rather than a
        # second one that could drift.
        response = app.make_response(user_shell())
        response.set_cookie(
            SESSION_COOKIE, session.token,
            httponly=True, samesite="Lax",
            secure=request.is_secure, path="/")
        # Belt and braces over the global policy: this response's own URL is a
        # credential, and it must not be cached by a shared proxy or restored
        # into somebody else's tab from a back-forward cache.
        response.headers["Cache-Control"] = "no-store, private"
        return response

    @app.route("/api/admin/devices", methods=["GET"])
    def api_admin_devices():
        """Devices an administrator can assign or name, by stable identity.

        The identity is what a permission stores, so the assignment UI and the
        enforcement agree by construction rather than by both happening to
        normalise the same way.

        An administrator gets the device's own name, model, address,
        location and description alongside the alias: two displays both called
        "Front Display" are only distinguishable by what is underneath them.

        Location and description come from the device's own systeminfo, which
        the cache already holds -- no extra read is issued to obtain them, and
        nothing here writes them back.
        """
        # Who was given each device. Derived once, here, so the page renders
        # it rather than parsing a key format that belongs to the store --
        # and so Clear Units and this list can never disagree about what is
        # assigned.
        try:
            assigned = store.assigned_devices()
        except Exception:
            assigned = {}
        rows_now = load_cache() or []
        try:
            depends = luma_inventory.build(rows_now, store, groups, walls)
        except Exception:
            depends = {}
        devices = []
        for unit in rows_now:
            identity = luma_projection.unit_identity(unit)
            if not identity:
                continue
            kind = luma_projection.unit_kind(unit)
            device_name = unit.get("hostname") or unit.get("name") or identity
            devices.append({
                "id": identity,
                # The device's own name, always, so an administrator can see
                # what they are labelling.
                "device_name": device_name,
                "alias": aliases.alias_for(identity),
                # What to show in a list: the alias where one is set.
                "name": aliases.display_name(identity, device_name),
                "model": unit.get("model") or "",
                "ip": unit.get("ip") or "",
                # Device-provided, from systeminfo the cache already holds.
                # Presented beside the alias so an administrator naming two
                # identical units can tell which is which -- and kept in
                # separate fields from the alias, because one belongs to
                # LumaSuite and these belong to the equipment.
                "location": luma_projection.device_location(unit),
                "description": luma_projection.device_description(unit),
                "role": {"source": "encoder", "display": "decoder"}.get(kind, "other"),
                # The groups that contain this display, so removing it from the
                # inventory can say what it would leave behind.
                "in_groups": [g for g in (grp["name"] for grp in groups.all()
                                          if identity in grp["members"])],
                # Usernames only. An administrator can already list accounts,
                # and this says which of them would lose equipment if the
                # inventory were cleared.
                "assigned_to": assigned.get(identity, []),
                # Everything that depends on this unit -- accounts, groups and saved
                # walls -- from the one derivation Clear Units itself uses.
                "assignments": depends.get(identity) or {
                    "users": [], "direct_access": [], "groups": [], "walls": [],
                    "wall_reasons": [], "wall_access": [], "count": 0},
            })
        devices.sort(key=lambda d: (d["role"], d["name"].lower()))
        return jsonify({"ok": True, "devices": devices,
                        "duplicate_aliases": aliases.duplicates()})

    @app.route("/api/admin/aliases", methods=["GET"])
    def api_admin_aliases():
        return jsonify({"ok": True, "aliases": aliases.all(),
                        "duplicate_aliases": aliases.duplicates()})

    @app.route("/api/admin/aliases", methods=["POST"])
    def api_admin_set_aliases():
        """Name devices, or clear a name with an empty string.

        Takes a mapping so one editing session is one save. Nothing is written
        to any device: this is LumaSuite's own label for a piece of equipment
        and the device hostname is untouched.
        """
        data = luma_request.json_object()
        mapping = data.get("aliases")
        if not isinstance(mapping, dict):
            return jsonify({"ok": False, "error": "aliases must be an object"}), 400
        result = aliases.set_many(mapping)
        if not result.get("ok"):
            return jsonify(result), 400
        store.audit("device.aliases_changed", actor=_principal(),
                    detail={"count": result.get("applied", 0)})
        return jsonify({"ok": True, "applied": result.get("applied", 0),
                        "aliases": aliases.all(),
                        "duplicate_aliases": aliases.duplicates()})

    @app.route("/api/admin/audit", methods=["GET"])
    def api_admin_audit():
        """The activity log, newest first, filtered server-side and written in words.

        Every entry carries a `text` sentence -- what happened, to whom -- so the
        page never shows an internal event name or reason. Entries are already
        redacted when they are recorded; there is no second pass here that could
        be forgotten.
        """
        try:
            limit = max(1, min(500, int(request.args.get("limit") or 200)))
        except (TypeError, ValueError):
            limit = 200

        names = {}
        for unit in load_cache() or []:
            identity = luma_projection.unit_identity(unit)
            if identity:
                names[identity] = aliases.display_name(
                    identity, luma_projection._device_name(unit, identity))

        def namer(identity):
            return names.get(luma_projection.canonical_identity(identity), "a device")

        entries = []
        for item in store.audit_entries(limit=limit,
                                        event=request.args.get("event"),
                                        actor=request.args.get("actor"),
                                        search=request.args.get("q")):
            entry = dict(item)
            entry["label"] = luma_audit_text.event_label(item.get("event"))
            entry["text"] = luma_audit_text.describe(item, namer)
            entry.pop("detail", None)          # the sentence is the answer
            entries.append(entry)
        summary = store.audit_summary()
        summary["events"] = [{"id": e, "label": luma_audit_text.event_label(e)}
                             for e in summary.get("events", [])]
        return jsonify({"ok": True, "entries": entries, "summary": summary,
                        "retention": store.audit_retention()})

    @app.route("/api/admin/audit/retention", methods=["POST"])
    def api_admin_audit_retention():
        """How long the Activity log is kept and how large it may grow.

        Administrative, because it decides how much security history survives.
        Only the two named fields are read; anything out of bounds is refused
        with the sentence that names the bound, and nothing is changed.
        """
        data = luma_request.json_object()
        try:
            retention = store.set_audit_retention(
                data.get("days"), data.get("max_bytes"), actor=_principal())
        except luma_retention.RetentionError as refusal:
            return jsonify({"ok": False, "error": str(refusal)}), 400
        return jsonify({"ok": True, "retention": retention})

    # ----------------------------------------------------- display groups
    #
    # Administrative: define which displays a group contains. Membership is by
    # stable identity, and nothing here routes anything.

    def _group_payload(group):
        """A group as an administrator sees it: members named, never addressed."""
        by_identity = {}
        for unit in load_cache() or []:
            identity = luma_projection.unit_identity(unit)
            if identity and luma_projection.unit_kind(unit) == "display":
                by_identity[identity] = unit
        members = []
        for identity in group["members"]:
            unit = by_identity.get(identity)
            name = (aliases.display_name(identity, luma_projection._device_name(unit, identity))
                    if unit is not None else aliases.display_name(identity, "Display not found"))
            members.append({"id": identity, "name": name, "known": unit is not None})
        return {"id": group["id"], "name": group["name"], "members": members,
                "granted_to": store.accounts_with_group(group["id"])}

    @app.route("/api/admin/groups", methods=["GET"])
    def api_admin_groups():
        return jsonify({"ok": True,
                        "groups": [_group_payload(g) for g in groups.all()]})

    @app.route("/api/admin/groups", methods=["POST"])
    def api_admin_create_group():
        data = luma_request.json_object()
        result = groups.create(data.get("name"), data.get("members") or [])
        if not result.get("ok"):
            return jsonify(result), 400
        store.audit("group.created", actor=_principal(), detail={
            "name": result["group"]["name"],
            "displays": len(result["group"]["members"])})
        return jsonify({"ok": True, "group": _group_payload(result["group"])})

    @app.route("/api/admin/walls", methods=["GET"])
    def api_admin_walls():
        """The saved video walls an account may be given, by name.

        Read-only: a wall is created and edited on the Video Wall page. Only saved
        walls exist here, so there is nothing to grant that has not been configured
        and validated by an administrator already.
        """
        units = load_cache() or []
        rows = []
        for wall in (walls.all() if walls is not None else []):
            wid = luma_walls.wall_id(wall)
            counts = luma_walls.summary(wall, units, walls.device_id_of)
            rows.append({"id": wid, "name": str(wall.get("name") or "Video Wall"),
                         "members": counts["members"],
                         "online_members": counts["online_members"],
                         "granted_to": store.accounts_with_wall(wid)})
        rows.sort(key=lambda r: (r["name"].lower(), r["id"]))
        return jsonify({"ok": True, "walls": rows})

    @app.route("/api/admin/groups/<group_id>", methods=["POST"])
    def api_admin_update_group(group_id):
        """One endpoint, one named action: rename, set_members or delete."""
        data = luma_request.json_object()
        action = str(data.get("action") or "").strip()
        actor = _principal()
        if action == "delete":
            holders = store.accounts_with_group(group_id)
            result = groups.delete(group_id)
            if not result.get("ok"):
                return jsonify(result), (404 if result.get("missing") else 400)
            store.forget_group(group_id)
            store.audit("group.deleted", actor=actor, detail={
                "name": result["group"]["name"], "accounts_affected": holders})
            return jsonify({"ok": True, "accounts_affected": holders})
        if action == "rename":
            result = groups.update(group_id, name=data.get("name"))
        elif action == "set_members":
            result = groups.update(group_id, members=data.get("members") or [])
        else:
            return jsonify({"ok": False, "error": "unknown action"}), 400
        if not result.get("ok"):
            return jsonify(result), (404 if result.get("missing") else 400)
        store.audit("group.updated", actor=actor, detail={
            "name": result["group"]["name"], "action": action,
            "displays": len(result["group"]["members"])})
        return jsonify({"ok": True, "group": _group_payload(result["group"])})

    # ---------------------------------------------------- user surface
    #
    # Every handler below consults the account *as it is now*, before waiting for
    # a lock and again after it. Authorisation belongs at the operation boundary:
    # between the gate admitting a request and a device being written to, an
    # administrator may have disabled the account or taken the display away.

    @app.route("/api/user/state", methods=["GET"])
    def api_user_state():
        """What this account may see. Derived from state the host already holds.

        A shared, single-flight refresh keeps the displayed routes honest without
        every open page asking the devices: ten browsers cost one device read per
        refresh window, not ten.
        """
        observed = None
        try:
            observed = observe()
        except Exception as exc:                      # never fail the page for this
            log.info("[USER] shared refresh failed: %s", type(exc).__name__)
        account = _current_account()
        if account is _REVOKED:
            return jsonify({"ok": False, "error": "your sign-in is no longer valid"}), 401
        body = luma_projection.user_state(
            account, load_cache() or [], aliases, groups, walls)
        body["rev"] = _revision_for(account)
        body["routing"] = _interfaces_of(account)
        if isinstance(observed, dict):
            body["observed"] = observed               # how old what was read is
        return jsonify(body)

    def _interfaces_of(account):
        """Which routing interfaces this account may use. An administrator has both."""
        if account is None or account.is_admin:
            return {"standard": True, "matrix": True}
        return account.permissions.interfaces()

    @app.route("/api/user/matrix", methods=["GET"])
    def api_user_matrix():
        """The matrix this account may use, built from its own grants only.

        Refused outright for an account without the matrix interface, so the page
        cannot be made to show it by asking. Shares the one device refresh the
        Standard page uses; nothing here reads a device itself.
        """
        account = _current_account()
        if account is _REVOKED:
            return jsonify({"ok": False, "error": "your sign-in is no longer valid"}), 401
        if (account is not None and not account.is_admin
                and not account.permissions.may_use_interface("matrix")):
            return _denied(account, "interface_not_permitted", "/api/user/matrix")
        observed = None
        try:
            observed = observe()
        except Exception as exc:                      # never fail the page for this
            log.info("[USER] shared refresh failed: %s", type(exc).__name__)
        body = luma_user_matrix.user_matrix(
            account, load_cache() or [], aliases, groups, walls)
        body["rev"] = _revision_for(account)
        if isinstance(observed, dict):
            body["observed"] = observed               # how old what was read is
        return jsonify(body)

    @app.route("/api/user/revision", methods=["GET"])
    def api_user_revision():
        """A digest of what this account may see. No device is read.

        The page compares it with the one it last rendered from and re-reads its
        state only when it differs, so an administrator's change reaches an open
        page without the page polling the equipment. It is never an
        authorisation input: every operation still asks the account as it is.
        """
        account = _current_account()
        if account is _REVOKED:
            return jsonify({"ok": False, "error": "your sign-in is no longer valid"}), 401
        return jsonify({"ok": True, "rev": _revision_for(account)})

    def _revision_for(account):
        if account is None or account.is_admin:
            return "all"
        perms = account.permissions
        present = set()
        for unit in load_cache() or []:
            identity = luma_projection.unit_identity(unit)
            if identity:
                present.add(identity)
        group_rows, ids = [], set(perms.sources) | set(perms.destinations)
        for group in groups.all():
            if perms.may_control_group(group["id"]):
                group_rows.append([group["id"], group["name"], list(group["members"])])
                ids.update(group["members"])
        wall_rows = []
        if walls is not None:
            units = load_cache() or []
            for wall in walls.all():
                if perms.may_control_wall(luma_walls.wall_id(wall)):
                    row = luma_walls.fingerprint(wall, units, walls.device_id_of)
                    wall_rows.append(row)
                    ids.update(i for _c, i, _u in
                               luma_walls.members(wall, units, walls.device_id_of) if i)
        named = aliases.all()
        extra = {"groups": group_rows, "walls": wall_rows,
                 "names": {i: named[i] for i in sorted(ids) if i in named},
                 "present": sorted(ids & present)}
        return store.permission_revision(account, extra)

    def _preview_target(account, args):
        """(identity, kind, refusal) for a preview request, authorised first."""
        source = luma_projection.canonical_identity(args.get("source"))
        display = luma_projection.canonical_identity(args.get("display"))
        if bool(source) == bool(display):
            return None, None, (jsonify({"ok": False,
                                         "error": "name one source or one display"}), 400)
        identity, kind = (source, "source") if source else (display, "display")
        admin = account is None or account.is_admin
        if not admin:
            allowed = (account.permissions.may_use_source(identity) if kind == "source"
                       else account.permissions.may_control_destination(identity))
            if not allowed:
                reason = ("source_not_permitted" if kind == "source"
                          else "destination_not_permitted")
                return None, None, _denied(account, reason, "/api/user/preview")
        return identity, kind, None

    @app.route("/api/user/preview", methods=["GET"])
    def api_user_preview():
        """One still image of a permitted device, fetched by the server.

        The browser never learns a device address and never contacts one. The
        permission check comes before the lookup, so an identity that was not
        granted gets the same refusal whether or not it exists. The answer is an
        image, or `{ok: false, reason}` with HTTP 200 -- a missing picture is a
        normal state, not an error, and never a routing gate.
        """
        account = _current_account()
        if account is _REVOKED:
            return jsonify({"ok": False, "error": "your sign-in is no longer valid"}), 401
        identity, kind, refusal = _preview_target(account, request.args)
        if refusal is not None:
            return refusal
        fresh = request.args.get("fresh") == "1"
        luma_previewstats.note("requests", kind + (".fresh" if fresh else ""))
        address = _address_index().get(identity)
        if not address:
            luma_previewstats.note("answers", "unavailable")
            return jsonify({"ok": False, "reason": "preview_unavailable"})
        result = preview_fetch(kind, address, fresh=fresh)
        if not result or not result.get("image"):
            luma_previewstats.note("answers", "unavailable")
            return jsonify({"ok": False,
                            "reason": (result or {}).get("reason") or "preview_unavailable"})
        luma_previewstats.note("answers", "image")
        response = Response(result["image"], mimetype=result.get("type") or "image/jpeg")
        response.headers["Cache-Control"] = "no-store, private"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.route("/api/preview_stats", methods=["GET", "POST"])
    def api_preview_stats():
        """Preview traffic counters for a measurement run. Local operator only.

        404 unless the host was started with LUMA_PREVIEW_STATS=1, so an ordinary
        installation has no such surface. GET reads the counters; POST zeroes them.
        """
        if not luma_previewstats.enabled():
            return jsonify({"ok": False, "error": "not available"}), 404
        if request.method == "POST":
            luma_previewstats.reset()
        return jsonify({"ok": True, "counts": luma_previewstats.snapshot()})

    def _online_index():
        """{identity: online?} from the cache as it is now: no device is read."""
        found = {}
        for unit in load_cache() or []:
            identity = luma_projection.unit_identity(unit)
            if identity:
                found[identity] = luma_projection.is_online(unit)
        return found

    def _is_offline(identity):
        """True only for a unit the host knows and has found not answering."""
        return _online_index().get(identity) is False

    def _offline_refusal(*identities):
        store.audit("route.refused_offline", actor=_current_account(),
                    detail={"count": len([i for i in identities if _is_offline(i)])})
        return jsonify({"ok": False, "result": "offline", "reason": "device_offline",
                        "error": _ROUTE_SENTENCES["offline"]}), 409

    def _project_route(outcome):
        """Only what a User may be told about a route attempt.

        Never an address, a stream name or a device's own error text.
        """
        ok = bool(outcome.get("ok"))
        status = outcome.get("status") or ("verified" if ok else "failed")
        body = {"ok": ok, "result": status,
                "audio": outcome.get("audio") or "unknown"}
        if not ok:
            body["reason"] = outcome.get("reason") or status
            body["error"] = (_REFUSED_SENTENCES.get(body["reason"]) if status == "refused" else None)                 or _ROUTE_SENTENCES.get(status, _ROUTE_SENTENCES["failed"])
        return body, (200 if ok else {"unreachable": 503, "not_verified": 502,
                                      "not_found": 404, "offline": 409,
                                      "conflict": 409}.get(status, 502))

    @app.route("/api/user/route", methods=["POST"])
    def api_user_route():
        """Route one permitted source to one permitted display.

        Built on the application's own verified A/V route; nothing about routing
        is reimplemented here. Audio and video are one operation -- there is no
        separate audio permission or control.
        """
        data = luma_request.json_object()
        if not isinstance(data, dict):
            return jsonify({"ok": False, "error": "source and destination required"}), 400
        source_id = luma_projection.canonical_identity(data.get("source"))
        destination_id = luma_projection.canonical_identity(data.get("destination"))
        if not source_id or not destination_id:
            return jsonify({"ok": False, "error": "source and destination required"}), 400

        def _check(account):
            if not account.permissions.may_use_interface("standard"):
                return "interface_not_permitted"
            if not account.permissions.may_use_source(source_id):
                return "source_not_permitted"
            if not account.permissions.may_control_destination(destination_id):
                return "destination_not_permitted"
            return None

        plane, bad = _standard_plane(data)
        if bad is not None:
            return bad
        refusal = _revalidate(_check, "/api/user/route")     # before the wait
        if refusal is not None:
            return refusal
        return _locked_single(source_id, destination_id, _check, plane)

    def _standard_plane(data):
        """What a Standard Routing request carries: (plane, None) or (None, a 400 response).

        `audio_follows_video` is a routing preference, not a permission: there is no grant for
        it and the server does not store it. True (the default, and what an old page sends by
        saying nothing) routes audio and video together. False makes a video route change the
        video alone and leaves the display's audio as it is. Anything that is not a boolean is
        refused rather than guessed at, so a malformed value can never silently mean "on" or "off".
        """
        follows = data.get("audio_follows_video", True)
        if not isinstance(follows, bool):
            return None, (jsonify({"ok": False, "reason": "bad_preference",
                                   "error": "Audio follows video must be on or off."}), 400)
        if follows:
            return luma_user_matrix.MODE_AV, None
        if route_mode is None:
            return None, (jsonify({"ok": False, "reason": "not_supported",
                                   "error": "That is not available."}), 404)
        return luma_user_matrix.MODE_VIDEO, None

    def _locked_single(source_id, destination_id, check, plane=luma_user_matrix.MODE_AV):
        began = time.monotonic()
        try:
            with locks.hold(locks.decoder(destination_id)):
                refusal = _revalidate(check, "/api/user/route")   # after the wait
                if refusal is not None:
                    return refusal
                address = _address_index()
                encoder_ip = address.get(source_id)
                decoder_ip = address.get(destination_id)
                if not encoder_ip or not decoder_ip:
                    return jsonify({"ok": False, "result": "not_found",
                                    "error": "That device is not currently known."}), 404
                # Offline is decided here, on the server, from the same derivation the
                # page uses; a disabled card is only a courtesy.
                if _is_offline(source_id) or _is_offline(destination_id):
                    return _offline_refusal(source_id, destination_id)
                log.info("[ROUTE-INTENT] rule=/api/user/route plane=%s source=%s displays=%s",
                         plane, source_id, destination_id)
                unproven = (_confirm_identity(decoder_ip, destination_id)
                            or _confirm_identity(encoder_ip, source_id))
                if unproven:
                    log.warning("[ROUTE] /api/user/route: an address was not confirmed (%s); "
                                "nothing was sent", unproven)
                    silent = unproven == "identity_unconfirmed"      # upstream, not a conflict
                    return jsonify({"ok": False, "result": "unreachable" if silent else "failed",
                                    "reason": unproven,
                                    "error": _REFUSED_SENTENCES[unproven]}), (503 if silent else 409)
                if plane == luma_user_matrix.MODE_AV:
                    outcome = route_av(decoder_ip, encoder_ip)
                else:
                    outcome = route_mode(decoder_ip, plane, encoder_ip)
                body, status = _project_route(outcome)
                store.audit("route", actor=_current_account(), detail={
                    "source": source_id, "destination": destination_id,
                    "result": body["result"], "audio": body["audio"]})
                log.info("[USER-ROUTE] waited+routed %.2fs",
                         time.monotonic() - began)
                return jsonify(body), status
        except luma_locks.ResourceBusy:
            return jsonify({"ok": False, "result": "busy", "reason": "resource_busy",
                            "error": "That display is busy with another change. "
                                     "Try again in a moment."}), 409

    @app.route("/api/user/route_group", methods=["POST"])
    def api_user_route_group():
        """Route one permitted source to every display in a permitted group.

        One action for the person. The members are resolved and authorised first and then
        routed together in one bounded parallel batch (`_run_batch`, the engine every
        multi-display route shares): each is locked, routed and verified on its own. A member
        that fails does not stop the rest and nothing already routed is undone.
        Membership is read now, so editing a group changes what it controls at
        once. Holding the group is the authority -- the members need not be
        individually granted, and they are never named by identity in the reply.
        """
        data = luma_request.json_object()
        if not isinstance(data, dict):
            return jsonify({"ok": False, "error": "source and group required"}), 400
        source_id = luma_projection.canonical_identity(data.get("source"))
        group_id = str(data.get("group") or "").strip().lower()
        if not source_id or not group_id:
            return jsonify({"ok": False, "error": "source and group required"}), 400

        def _check(account):
            if not account.permissions.may_use_interface("standard"):
                return "interface_not_permitted"
            if not account.permissions.may_use_source(source_id):
                return "source_not_permitted"
            if not account.permissions.may_control_group(group_id):
                return "group_not_permitted"
            return None

        plane, bad = _standard_plane(data)
        if bad is not None:
            return bad
        refusal = _revalidate(_check, "/api/user/route_group")
        if refusal is not None:
            return refusal
        group = groups.get(group_id)
        if group is None:
            return jsonify({"ok": False, "error": "That group is not available."}), 404
        if _is_offline(source_id):
            return _offline_refusal(source_id)

        summary = _run_batch("/api/user/route_group", _check, source_id,
                             group["members"], [], plane, _display_names(),
                             group_ids=[group["id"]])
        store.audit("route_group", actor=_current_account(), detail={
            "source": source_id, "group": group["id"], "group_name": group["name"],
            "routed": summary["routed"], "total": summary["total"],
            "audio_follows_video": plane == luma_user_matrix.MODE_AV,
            "results": [{"display": r.get("display"), "ok": r.get("ok"),
                         "result": r.get("result")} for r in summary["results"]]})
        # What the person is told: counts and per-display names and outcomes --
        # never an identity or an address.
        return jsonify({
            "ok": summary["ok"], "routed": summary["routed"], "total": summary["total"],
            "group": group["name"], "timing": summary["timing"],
            "results": [dict({"index": r["index"], "name": r.get("name") or "Display",
                              "ok": bool(r.get("ok")), "result": r.get("result") or "failed",
                              "audio": r.get("audio") or "unknown"}, **_declined_fields(r))
                        for r in summary["results"]]})

    #: What one request may name. Bounded before anything is resolved, so a hostile
    #: list costs a 400 and not a loop.
    #: The one plane that has no source: every display in the request is put into the unrouted state.
    UNROUTE_PLANE = "unroute"
    MULTI_MAX_DISPLAYS = 64
    MULTI_MAX_GROUPS = 16
    MULTI_MAX_WALLS = 16
    MULTI_MAX_DESTINATIONS = 256

    def _bounded_list(value, limit, normalise):
        """A deduplicated, bounded list of normalised strings, or None if malformed."""
        if value is None:
            return []
        if not isinstance(value, list) or len(value) > limit:
            return None
        found = []
        for item in value:
            if not isinstance(item, str):
                return None
            item = normalise(item)
            if not item:
                return None
            if item not in found:
                found.append(item)
        return found

    def _resolve_destinations(display_ids, group_ids):
        """Physical destinations for a selection, in order, each once.

        Individually chosen displays come first, then each group's members, so the
        order a person clicked is the order they are routed. A display named twice,
        or named and also in a chosen group, is one destination: routing it twice
        would only repeat a write the display has already confirmed.
        Returns (ordered identities, missing group id or None).
        """
        ordered = list(display_ids)
        for group_id in group_ids:
            group = groups.get(group_id)
            if group is None:
                return [], group_id
            for member in group["members"]:
                if member not in ordered:
                    ordered.append(member)
        return ordered, None

    @app.route("/api/user/route_multi", methods=["POST"])
    def api_user_route_multi():
        """Route one permitted source to several permitted displays, groups and walls.

        This is the one routing path the page uses, whether the person pressed Route
        or dropped a source on a destination. The selection is checked in full before
        anything is written: one identity the account holds no grant for refuses the
        whole request (403), exactly as the single route does, so a request is never
        half-authorised. Displays need their own grant, groups need the group grant
        and walls need the wall grant -- holding a group or a wall does not let a
        person pick its members individually, and holding a member grants neither.
        Destinations are resolved by hardware identity, deduplicated, then routed
        strictly one after another: lock, route, read back, report. A destination
        that fails does not stop the rest, and nothing already routed is undone.
        A destination that is offline is reported as such and never written to.
        """
        data = luma_request.json_object()
        if not isinstance(data, dict):
            return jsonify({"ok": False,
                            "error": "source, displays, groups and walls required"}), 400
        source_id = luma_projection.canonical_identity(data.get("source"))
        display_ids = _bounded_list(data.get("displays"), MULTI_MAX_DISPLAYS,
                                    luma_projection.canonical_identity)
        group_ids = _bounded_list(data.get("groups"), MULTI_MAX_GROUPS,
                                  lambda g: g.strip().lower())
        wall_ids = _bounded_list(data.get("walls"), MULTI_MAX_WALLS,
                                 lambda w: w.strip().lower())
        if not source_id or display_ids is None or group_ids is None or wall_ids is None:
            return jsonify({"ok": False,
                            "error": "source, displays, groups and walls required"}), 400
        # Which interface asked, and what it wants done. Both are validated against a
        # closed list before anything is resolved. The interface is only a name for the
        # audit and a statement of which grant must be held; it never widens anything,
        # because the grant is checked from the account whichever name was sent.
        interface = data.get("interface", "standard")
        mode = data.get("mode", luma_user_matrix.MODE_AV)
        if (interface not in ("standard", "matrix")
                or mode not in (luma_user_matrix.MODE_AV, luma_user_matrix.MODE_VIDEO,
                                luma_user_matrix.MODE_AUDIO)):
            return jsonify({"ok": False, "reason": "unknown_mode",
                            "error": "That way of routing does not exist."}), 400
        if mode != luma_user_matrix.MODE_AV and interface != "matrix":
            return jsonify({"ok": False, "reason": "unknown_mode",
                            "error": "Video and audio are routed separately "
                                     "only from the matrix."}), 400
        # The one plane this request routes. A matrix request names it with `mode`. A Standard
        # request is always "audio and video" unless the person turned "Audio follows video" off,
        # in which case it is video alone; the preference is ignored by the matrix, whose modes
        # are explicit and are never changed by it.
        if interface == "standard":
            plane, bad = _standard_plane(data)
            if bad is not None:
                return bad
        else:
            plane = mode
        if plane != luma_user_matrix.MODE_AV and route_mode is None:
            return jsonify({"ok": False, "reason": "not_supported",
                            "error": "That is not available."}), 404
        if not display_ids and not group_ids and not wall_ids:
            return jsonify({"ok": False,
                            "error": "choose at least one display, group or wall"}), 400
        if wall_ids and (walls is None or route_wall is None):
            return jsonify({"ok": False, "error": "Video walls are not available."}), 404

        def _check(account):
            if not account.permissions.may_use_interface(interface):
                return "interface_not_permitted"
            if not account.permissions.may_use_source(source_id):
                return "source_not_permitted"
            for identity in display_ids:
                if not account.permissions.may_control_destination(identity):
                    return "destination_not_permitted"
            for group_id in group_ids:
                if not account.permissions.may_control_group(group_id):
                    return "group_not_permitted"
            for wall_id in wall_ids:
                if not account.permissions.may_control_wall(wall_id):
                    return "wall_not_permitted"
            return None

        refusal = _revalidate(_check, "/api/user/route_multi")     # before any wait
        if refusal is not None:
            return refusal
        ordered, missing = _resolve_destinations(display_ids, group_ids)
        if missing is not None:
            return jsonify({"ok": False, "error": "That group is not available."}), 404
        for wall_id in wall_ids:
            if walls.get(wall_id) is None:
                return jsonify({"ok": False, "error": "That video wall is not available."}), 404
        if len(ordered) + len(wall_ids) > MULTI_MAX_DESTINATIONS:
            return jsonify({"ok": False, "error": "too many displays in one request"}), 400
        if _is_offline(source_id):
            return _offline_refusal(source_id)

        summary = _run_batch("/api/user/route_multi", _check, source_id,
                             ordered, wall_ids, plane, _display_names(),
                             group_ids=group_ids)
        store.audit("route_matrix" if interface == "matrix" else "route_multi",
                    actor=_current_account(), detail={
            "source": source_id, "groups": group_ids, "walls": wall_ids,
            "mode": plane, "audio_follows_video": plane == luma_user_matrix.MODE_AV,
            "routed": summary["routed"], "total": summary["total"],
            "results": [{"display": r.get("display"),
                         "name": r.get("name") if r.get("kind") == "wall" else None,
                         "ok": r.get("ok"), "result": r.get("result")}
                        for r in summary["results"]]})
        unit_word = "destination" if wall_ids else "display"
        # Counts, display names and outcomes -- never an identity or an address.
        rows = []
        for r in summary["results"]:
            row = {"index": r["index"], "name": r.get("name") or "Display",
                   "ok": bool(r.get("ok")), "result": r.get("result") or "failed",
                   "audio": r.get("audio") or "unknown"}
            if r.get("kind") == "wall":
                row.update(kind="wall", routed=r.get("routed", 0), total=r.get("total", 0))
            row.update(_declined_fields(r))
            rows.append(row)
        return jsonify({
            "ok": summary["ok"], "routed": summary["routed"], "total": summary["total"],
            "timing": summary["timing"],
            "targets": _target_states(display_ids, group_ids, wall_ids, ordered, summary["results"],
                                      plane),
            "summary": "%d of %d %s routed" % (summary["routed"], summary["total"],
                                               unit_word if summary["total"] == 1
                                               else unit_word + "s"),
            "results": rows})

    @app.route("/api/user/unroute", methods=["POST"])
    def api_user_unroute():
        """Unroute permitted displays and groups: destinations only, no source.

        The same selection model as a route: individual displays and Display Groups, any mix.
        The whole request is authorised before anything is written (one identity the account
        holds no grant for refuses all of it), groups are resolved to members now, each physical
        display is written once, and each display is its own operation: lock, re-check the
        account, verify the hardware identity, write, read back. One that fails, is offline or is
        busy does not stop the others, and the answer counts what really became of each.
        Video walls are not part of this request.
        """
        data = luma_request.json_object()
        if not isinstance(data, dict):
            return jsonify({"ok": False, "error": "displays and groups required"}), 400
        display_ids = _bounded_list(data.get("displays"), MULTI_MAX_DISPLAYS,
                                    luma_projection.canonical_identity)
        group_ids = _bounded_list(data.get("groups"), MULTI_MAX_GROUPS,
                                  lambda g: g.strip().lower())
        if display_ids is None or group_ids is None:
            return jsonify({"ok": False, "error": "displays and groups required"}), 400
        if data.get("walls"):
            return jsonify({"ok": False, "reason": "not_supported",
                            "error": "A video wall cannot be unrouted from here."}), 400
        if not display_ids and not group_ids:
            return jsonify({"ok": False, "error": "choose at least one display or group"}), 400
        if unroute is None:
            return jsonify({"ok": False, "reason": "not_supported",
                            "error": "That is not available."}), 404

        def _check(account):
            if not account.permissions.may_use_interface("standard"):
                return "interface_not_permitted"
            for identity in display_ids:
                if not account.permissions.may_control_destination(identity):
                    return "destination_not_permitted"
            for group_id in group_ids:
                if not account.permissions.may_control_group(group_id):
                    return "group_not_permitted"
            return None

        refusal = _revalidate(_check, "/api/user/unroute")          # before any wait
        if refusal is not None:
            return refusal
        ordered, missing = _resolve_destinations(display_ids, group_ids)
        if missing is not None:
            return jsonify({"ok": False, "error": "That group is not available."}), 404
        if len(ordered) > MULTI_MAX_DESTINATIONS:
            return jsonify({"ok": False, "error": "too many displays in one request"}), 400
        summary = _run_batch("/api/user/unroute", _check, None, ordered, [], UNROUTE_PLANE,
                             _display_names(), group_ids=group_ids)
        store.audit("unroute", actor=_current_account(), detail={
            "groups": group_ids, "routed": summary["routed"], "total": summary["total"],
            "results": [{"display": r.get("display"), "ok": r.get("ok"), "result": r.get("result")}
                        for r in summary["results"]]})
        rows = []
        for r in summary["results"]:
            row = {"index": r["index"], "name": r.get("name") or "Display",
                   "ok": bool(r.get("ok")), "result": r.get("result") or "failed"}
            row.update(_declined_fields(r))
            rows.append(row)
        return jsonify({
            "ok": summary["ok"], "unrouted": summary["routed"], "total": summary["total"],
            "timing": summary["timing"],
            "targets": _target_states(display_ids, group_ids, [], ordered, summary["results"],
                                      UNROUTE_PLANE),
            "summary": "%d of %d %s unrouted" % (summary["routed"], summary["total"],
                                                 "display" if summary["total"] == 1 else "displays"),
            "results": rows})

    _WALL_PREFIX = "wall:"

    def _state_of(row, plane):
        """One destination's honest state: verified, unconfirmed or failed.

        `unconfirmed` is a route that was written and whose read-back did not agree *yet*
        (the device answered, with another stream). It is not a failure and is not shown as
        one: the page looks again, read-only, and only then calls it verified or failed. An
        audio-only route has no video stream to look at again, so it is not offered one.
        """
        if row.get("ok"):
            return "verified"
        if (row.get("result") == "not_verified" and plane != luma_user_matrix.MODE_AUDIO
                and plane != UNROUTE_PLANE and row.get("kind") != "wall"):
            return "unconfirmed"
        return "failed"

    def _worst(states):
        for word in ("failed", "unconfirmed"):
            if word in states:
                return word
        return "verified"

    def _target_states(display_ids, group_ids, wall_ids, ordered, results, plane):
        """What became of each thing the person chose, for the page to mark individually.

        The page never has to guess which row belonged to which choice. A target is named by
        its position in the list the page sent for its kind (the page sends each choice once)
        -- never by an identity, because a restricted response carries none. A group is the
        worst of its members. Rows are in the order of `ordered` (displays, then each group's members,
        each once) followed by the walls, which is how the batch was built.
        """
        by_display = {}
        for position, identity in enumerate(ordered):
            if position < len(results):
                by_display[identity] = _state_of(results[position], plane)
        targets = [{"kind": "display", "index": n,
                    "state": by_display.get(identity, "failed")}
                   for n, identity in enumerate(display_ids)]
        for n, group_id in enumerate(group_ids):
            group = groups.get(group_id)
            members = list((group or {}).get("members") or [])
            targets.append({"kind": "group", "index": n,
                            "state": _worst([by_display.get(m, "failed") for m in members]
                                            or ["failed"])})
        for n in range(len(wall_ids)):
            at = len(ordered) + n
            targets.append({"kind": "wall", "index": n,
                            "state": _state_of(results[at], plane) if at < len(results) else "failed"})
        return targets

    @app.route("/api/user/route_check", methods=["POST"])
    def api_user_route_check():
        """Look again, read-only, at displays a route reported as not yet confirmed.

        Writes nothing and takes no lock: it is the same read-back a route makes, asked a
        little later, so a display that was merely slow to report the stream it had taken is
        confirmed and one that really did not take it is reported as that. The caller needs
        exactly the grants a route to the same destinations needs -- this is not a way to
        look at displays or sources an account may not use -- and each display's identity is
        confirmed before it is read, as it is before a write. Walls are not looked at again.
        """
        if verify_route is None:
            return jsonify({"ok": False, "reason": "not_supported",
                            "error": "That is not available."}), 404
        data = luma_request.json_object()
        if not isinstance(data, dict):
            return jsonify({"ok": False, "error": "source, displays and groups required"}), 400
        source_id = luma_projection.canonical_identity(data.get("source"))
        display_ids = _bounded_list(data.get("displays"), MULTI_MAX_DISPLAYS,
                                    luma_projection.canonical_identity)
        group_ids = _bounded_list(data.get("groups"), MULTI_MAX_GROUPS,
                                  lambda g: g.strip().lower())
        interface = data.get("interface", "standard")
        if (not source_id or display_ids is None or group_ids is None
                or interface not in ("standard", "matrix")
                or (not display_ids and not group_ids)):
            return jsonify({"ok": False, "error": "source, displays and groups required"}), 400

        def _check(account):
            if not account.permissions.may_use_interface(interface):
                return "interface_not_permitted"
            if not account.permissions.may_use_source(source_id):
                return "source_not_permitted"
            for identity in display_ids:
                if not account.permissions.may_control_destination(identity):
                    return "destination_not_permitted"
            for group_id in group_ids:
                if not account.permissions.may_control_group(group_id):
                    return "group_not_permitted"
            return None

        refusal = _revalidate(_check, "/api/user/route_check")
        if refusal is not None:
            return refusal
        ordered, missing = _resolve_destinations(display_ids, group_ids)
        if missing is not None:
            return jsonify({"ok": False, "error": "That group is not available."}), 404
        if len(ordered) > MULTI_MAX_DESTINATIONS:
            return jsonify({"ok": False, "error": "too many displays in one request"}), 400

        def look(index, identity):
            refusal = _revalidate(_check, "/api/user/route_check")
            if refusal is not None:
                return {"ok": False, "result": "refused"}
            address = _address_index()
            encoder_ip, decoder_ip = address.get(source_id), address.get(identity)
            if not encoder_ip or not decoder_ip:
                return {"ok": False, "result": "not_found"}
            if _is_offline(identity) or _is_offline(source_id):
                return {"ok": False, "result": "offline"}
            if _confirm_identity(decoder_ip, identity) or _confirm_identity(encoder_ip, source_id):
                return {"ok": False, "result": "failed"}
            seen = verify_route(decoder_ip, encoder_ip) or {}
            status = seen.get("status")
            return {"ok": status == "verified",
                    "result": status if status in ("verified", "not_verified", "unreachable")
                    else "failed"}

        # Reads only, so no route engine and no lock: a bounded pool, in the order asked.
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=max(1, min(6, len(ordered)))) as pool:
            futures = [pool.submit(copy_current_request_context(
                lambda i=i, who=who: look(i, who))) for i, who in enumerate(ordered)]
            looked = []
            for future in futures:
                try:
                    looked.append(future.result())
                except Exception:
                    looked.append({"ok": False, "result": "failed"})
        plane = luma_user_matrix.MODE_VIDEO
        targets = _target_states(display_ids, group_ids, [], ordered, looked, plane)
        # `unconfirmed` is still the answer while the display disagrees: how many looks are
        # worth taking before calling it a failure is the page's budget, not the server's.
        return jsonify({"ok": all(t["state"] == "verified" for t in targets), "targets": targets})

    #: What a person is told when the engine declined before writing anything.
    _REFUSED_SENTENCES = {
        "audio_not_available": "That source's audio is not available to this display.",
        "identity_mismatch": "That address now belongs to a different device, so nothing was sent. "
                             "Scan again.",
        "identity_unconfirmed": "The device did not confirm which unit it is, so nothing was sent.",
        "ambiguous_identity": "LumaSuite holds more than one address for that unit, so nothing "
                              "was sent. Scan again.",
    }

    def _declined_fields(result):
        """The reason and the sentence for a display the engine declined before writing.

        Only a reason this module has a sentence for is passed on, so the response never carries
        an engine's own text. A route that succeeded, or failed for another reason, adds nothing.
        """
        reason = result.get("reason") or ""
        sentence = _REFUSED_SENTENCES.get(reason)
        if result.get("ok") or not sentence:
            return {}
        return {"reason": reason, "message": sentence}

    _ROUTE_SENTENCES = {
        "not_verified": "The command was sent, but the display did not confirm it.",
        "unreachable": "That device did not answer.",
        "not_found": "That device is not currently known.",
        "conflict": "The inventory changed while this ran. Refresh and check the display.",
        "offline": "That device is offline.",
        "busy": "That display is busy with another change. Try again in a moment.",
        "failed": "That did not work.",
    }

    # ------------------------------------------------------- the shared routing engine
    def _display_names():
        """identity -> the name a person knows the display by, from the cache as it is now."""
        names = {}
        for unit in load_cache() or []:
            identity = luma_projection.unit_identity(unit)
            if identity:
                names[identity] = aliases.display_name(
                    identity, luma_projection._device_name(unit, identity))
        return names

    def _run_batch(rule, check, source_id, display_ids, wall_ids, plane, names, group_ids=()):
        """Route one source to displays and saved walls: the one engine behind every
        multi-destination route (Standard, Display Group, saved Video Wall, Matrix row).

        1. RESOLVE   The caller has authorised the whole request and resolved groups to
                     members. Here a wall is expanded -- server side, now -- into its member
                     decoders, each a separate operation. Nothing is written yet.
        2. PREPARE   One operation per decoder: the resource it locks and what it will do.
                     A wall that cannot be routed at all (source offline or silent, no
                     members) is answered here and writes nothing.
        3. DISPATCH  Every operation goes into one bounded parallel batch
                     (`luma_groups.run_parallel`). Each takes its own display lock, with
                     the account re-checked before the wait and again on the far side of
                     it; a lock is a wait, and permission can be withdrawn during one.
        4. VERIFY    Each operation reads its decoder back (inside the route).
        5. AGGREGATE A display is its own row. A wall is one row whose members' outcomes are
                     counted truthfully: all verified, partial, or why none was.

        A destination that fails, is offline or is busy does not stop the others and nothing
        already routed is undone. Locks are never nested across operations -- each one holds a
        single display -- so parallel dispatch cannot deadlock, however destinations overlap.
        """
        def _name(identity):
            return names.get(identity, "Display")

        def _refused(op):
            return {"ok": False, "result": "refused", **op["label"]}

        source_proof = {"lock": threading.Lock(), "reason": None, "done": False}

        def _source_reason(encoder_ip):
            """Is the source address still the source? Asked once per request, not per display."""
            with source_proof["lock"]:
                if not source_proof["done"]:
                    source_proof["reason"] = _confirm_identity(encoder_ip, source_id)
                    source_proof["done"] = True
                return source_proof["reason"]

        def _not_confirmed(identity, reason):
            log.warning("[ROUTE] %s: a display's address was not confirmed (%s); nothing was sent",
                        rule, reason)
            # A unit that would not answer is "did not answer", as it was before the read.
            return {"ok": False,
                    "result": "unreachable" if reason == "identity_unconfirmed" else "failed",
                    "reason": reason, "display": identity, "name": _name(identity)}

        def unroute_run(identity):
            def run():
                decoder_ip = _address_index().get(identity)
                if not decoder_ip:
                    return {"ok": False, "result": "not_found", "display": identity,
                            "name": _name(identity)}
                if _is_offline(identity):
                    return {"ok": False, "result": "offline", "display": identity,
                            "name": _name(identity)}
                reason = _confirm_identity(decoder_ip, identity)
                if reason:
                    return _not_confirmed(identity, reason)
                outcome = unroute(decoder_ip)
                status = outcome.get("status") or ("verified" if outcome.get("ok") else "failed")
                if status == "refused":
                    status = "failed"
                return {"ok": bool(outcome.get("ok")), "result": status,
                        "reason": outcome.get("reason") or "", "audio": "unchanged",
                        "display": identity, "name": _name(identity)}
            return run

        def display_run(identity):
            if plane == UNROUTE_PLANE:
                return unroute_run(identity)

            def run():
                address = _address_index()
                encoder_ip = address.get(source_id)
                decoder_ip = address.get(identity)
                if not encoder_ip or not decoder_ip:
                    return dict({"ok": False, "result": "not_found", "display": identity,
                                 "name": _name(identity)},
                                **({"reason": "ambiguous_identity"}
                                   if identity in address and not decoder_ip else {}))
                if _is_offline(identity):
                    return {"ok": False, "result": "offline", "display": identity,
                            "name": _name(identity)}
                reason = _confirm_identity(decoder_ip, identity) or _source_reason(encoder_ip)
                if reason:
                    return _not_confirmed(identity, reason)
                if plane == luma_user_matrix.MODE_AV:
                    outcome = route_av(decoder_ip, encoder_ip)
                else:
                    # The one existing audio/video-separate engine, unchanged.
                    outcome = route_mode(decoder_ip, plane, encoder_ip)
                status = outcome.get("status") or ("verified" if outcome.get("ok")
                                                   else "failed")
                if status == "refused":
                    # The engine declined (for example a switcher with no audio path, or a
                    # source with no audio for this display). That is not a refusal of this
                    # person's permission.
                    status = "failed"
                return {"ok": bool(outcome.get("ok")), "result": status,
                        "reason": outcome.get("reason") or "",
                        "audio": ("unknown" if plane == luma_user_matrix.MODE_AV
                                  else "unchanged") if not outcome.get("audio")
                        else outcome.get("audio"),
                        "display": identity, "name": _name(identity)}
            return run

        entries = [(k, None) for k in display_ids] + [(_WALL_PREFIX + w, w) for w in wall_ids]
        # What is about to be written to, by hardware identity, before anything is. Fields
        # are named; no address, no credential and no request body.
        log.info("[ROUTE-INTENT] rule=%s plane=%s source=%s groups=%s walls=%s displays=%s",
                 rule, plane, source_id, ",".join(group_ids) or "-",
                 ",".join(wall_ids) or "-", ",".join(display_ids) or "-")
        ops = []                # one per decoder write: {entry, resources, run, label}
        walls_of = {}             # entry index -> {plan, name, ops: [op indexes]} | {early: row}
        for entry_index, (key, wall_id) in enumerate(entries):
            if wall_id is None:
                ops.append({"entry": entry_index, "resources": [locks.decoder(key)],
                            "run": display_run(key),
                            "label": {"display": key, "name": _name(key)}})
                continue
            current = walls.get(wall_id)
            wall_name = str((current or {}).get("name") or "Video wall")
            if current is None:
                walls_of[entry_index] = {"early": {"ok": False, "result": "not_found",
                                                   "kind": "wall", "name": wall_name}}
                continue
            planner = getattr(route_wall, "plan", None)
            units = load_cache() or []
            members = [i for _c, i, _u in luma_walls.members(current, units, walls.device_id_of)
                       if i]
            # The decoders this wall will write to, by hardware identity, before any is.
            log.info("[ROUTE-INTENT] rule=%s plane=%s source=%s wall=%s members=%s",
                     rule, plane, source_id, wall_id, ",".join(members) or "-")
            if planner is None:
                # A wall router with no member-level plan is one operation over all its members
                # (the same single-lock-set behaviour a wall always had).
                def whole(current=current, wall_id=wall_id):
                    def run():
                        fresh = walls.get(wall_id)
                        if fresh is None:
                            return {"ok": False, "result": "not_found", "kind": "wall",
                                    "name": wall_name}
                        outcome = route_wall(fresh, source_id, plane)
                        return {"ok": bool(outcome.get("ok")),
                                "result": outcome.get("status") or (
                                    "verified" if outcome.get("ok") else "failed"),
                                "kind": "wall", "name": str(fresh.get("name") or wall_name),
                                "routed": int(outcome.get("routed") or 0),
                                "total": int(outcome.get("total") or 0), "audio": "unknown"}
                    return run
                ops.append({"entry": entry_index, "whole_wall": True,
                            "resources": [locks.decoder(i) for i in members],
                            "run": whole(), "label": {"kind": "wall", "name": wall_name}})
                continue
            plan = planner(current, source_id, plane)
            early = plan.early
            if early is not None:
                walls_of[entry_index] = {"early": dict(early, kind="wall", name=wall_name)}
                continue
            indexes = []
            for member in plan.members:
                indexes.append(len(ops))
                ops.append({"entry": entry_index, "wall_member": True,
                            "resources": ([locks.decoder(member.identity)]
                                          if member.identity else []),
                            "run": (lambda m=member: m.run()),
                            "label": {"kind": "wall", "name": wall_name}})
            walls_of[entry_index] = {"plan": plan, "name": wall_name, "ops": indexes}

        def execute(index, op):
            refusal = _revalidate(check, rule)                   # before the wait
            if refusal is not None:
                return _refused(op)
            try:
                with locks.hold(*op["resources"]):
                    refusal = _revalidate(check, rule)           # after the wait
                    if refusal is not None:
                        return _refused(op)
                    return op["run"]()
            except luma_locks.ResourceBusy:
                return {"ok": False, "result": "busy", **op["label"]}

        # Each operation runs on a worker thread, which has no request of its own: the copy is
        # what lets `_revalidate` read the session as it is at that moment. One copy per call.
        op_results = luma_groups.run_parallel(
            ops, execute, wrap=lambda fn: copy_current_request_context(fn))
        by_op = op_results["results"]

        rows = []
        for entry_index, (key, wall_id) in enumerate(entries):
            info = walls_of.get(entry_index)
            if wall_id is None:
                row = dict(by_op[next(i for i, o in enumerate(ops) if o["entry"] == entry_index)])
            elif info is not None and "early" in info:
                row = dict(info["early"])
            elif info is not None:
                member_rows = [by_op[i] for i in info["ops"]]
                row = dict(info["plan"].finish(member_rows), kind="wall", name=info["name"])
                row.setdefault("audio", "unknown")
                row["result"] = row.get("status") or ("verified" if row.get("ok") else "failed")
                row["ok"] = bool(row.get("ok"))
                row["t_start"] = min(r["t_start"] for r in member_rows)
                row["t_end"] = max(r["t_end"] for r in member_rows)
            else:                                    # a whole-wall operation
                row = dict(by_op[next(i for i, o in enumerate(ops) if o["entry"] == entry_index)])
            row["index"] = entry_index
            rows.append(row)
        routed = sum(1 for r in rows if r.get("ok"))
        return {"ok": bool(rows) and routed == len(rows), "routed": routed, "total": len(rows),
                "results": rows, "timing": op_results["timing"]}

    # ------------------------------------------------------------ helpers
    _REVOKED = object()

    def _current_account():
        """The account as it stands right now, not as the gate found it.

        Returns the live account, None for the local operator, or _REVOKED if the
        session no longer resolves.
        """
        if getattr(g, "is_local", False):
            return None
        token = request.cookies.get(SESSION_COOKIE)
        account, _session = store.resolve_session(token)
        return account if account is not None else _REVOKED

    def _revalidate(check, rule):
        """Still allowed? Returns None to proceed, or the response to send instead.

        Called before waiting for a lock and again after, because acquiring a lock
        is a wait and the far side of it is the last moment before a device is
        written to.
        """
        account = _current_account()
        if account is _REVOKED:
            return jsonify({"ok": False, "error": "your sign-in is no longer valid"}), 401
        if account is not None and not account.is_admin:
            reason = check(account)
            if reason is not None:
                return _denied(account, reason, rule)
        return None

    def _denied(account, reason, rule="/api/user/route"):
        """A refusal that says which refusal it is, from the gate's one sentence map."""
        store.audit("access.denied", actor=account,
                    detail={"rule": rule, "reason": reason})
        return jsonify({"ok": False,
                        "error": luma_gate.DENIAL_SENTENCES.get(reason, "not permitted"),
                        "reason": reason}), 403

    def _address_index():
        """identity -> current address, from host-authoritative state.

        An identity the cache holds under more than one address maps to None: which of
        them is the display is not something the cache can say, and picking one by
        dictionary order is how a stale row's address ended up written to. None reads as
        "not resolvable" to every caller (`.get()` is falsy) and `in` tells it apart from
        an unknown identity.
        """
        index = {}
        for unit in load_cache() or []:
            identity = luma_projection.unit_identity(unit)
            if identity and unit.get("ip"):
                if identity in index and index[identity] != unit["ip"]:
                    index[identity] = None
                elif identity not in index:
                    index[identity] = unit["ip"]
        return index

    def _confirm_identity(address, identity):
        """See `confirm_identity`: the one check every write to a known identity makes."""
        return confirm_identity(identify, address, identity)


def confirm_identity(identify, address, identity):
    """None when the device at `address` is shown to be `identity`, else the reason.

    The cache maps an identity to an address, and an address can be re-leased to another
    display between scans. Every write to a known identity therefore asks the device at
    that address for its hardware address first and refuses on anything but an exact
    match. A device that cannot say is refused too: unproven is not permission. Without
    an `identify` callable (a build that cannot read one) nothing changes.

    Module level so that every route -- the restricted ones, the saved Video Wall member
    route and the Video Wall page -- makes the same check rather than a copy of it.
    """
    if identify is None:
        return None
    try:
        found = luma_projection.canonical_identity(identify(address))
    except Exception as exc:                      # a read that failed is a refusal, not a guess
        log.warning("[ROUTE] identity read raised %s", type(exc).__name__)
        found = ""
    if not found:
        return "identity_unconfirmed"
    return None if found == identity else "identity_mismatch"


def _permissions_from(raw) -> Permissions:
    """Build a Permissions from request data, ignoring anything unrecognised.

    A malformed payload yields an empty grant rather than an error or a partial
    one: the safe failure for a permission is "none".
    """
    return Permissions.from_json(raw)
