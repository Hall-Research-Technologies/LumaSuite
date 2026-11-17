"""Remote access wiring: the stores, the listener, and the routes, in one place.

`lumaserver.py` is a large device-facing module. Everything about *who may use
it* is assembled here and handed the handful of device-facing callables it needs,
so the trust-sensitive surface is reviewable in a few files and this module has no
opinion about how devices are read.

Remote access is off by default and off for every installation that updates into
this build. The optional second listener is created but never started until an
administrator turns remote access on. The gate is installed unconditionally,
because a request that arrived on the local listener is allowed through it
untouched -- which is what keeps local behaviour identical rather than merely
similar.
"""
from __future__ import annotations

import logging
from pathlib import Path

from flask import send_from_directory

import luma_adapters
import luma_aliases
import luma_gate
import luma_groups
import luma_locks
import luma_projection
import luma_remote
import luma_remote_api
import luma_store
import luma_tls
import luma_users

log = logging.getLogger(__name__)


class RemoteSystem:
    """Everything remote access needs, built once for one application."""

    def __init__(self, app, *, data_dir, ui_dir, default_port, load_cache, route_av,
                 observe, preview_fetch, destination_check=None, walls=None,
                 route_wall=None, route_mode=None, identify=None, verify_route=None):
        self.app = app
        self.data_dir = Path(data_dir)
        self.ui_dir = Path(ui_dir)
        self.default_port = int(default_port)

        self.users_file = self.data_dir / "users.json"
        self.audit_file = self.data_dir / "security_audit.json"
        self.aliases_file = self.data_dir / "device_aliases.json"
        self.groups_file = self.data_dir / "display_groups.json"
        self.preference_file = self.data_dir / "remote_access.json"
        # Where a certificate and key live if LumaSuite manages them: the runtime
        # data directory, never the package, so a clean installation contains no
        # key material at all.
        self.cert_file = self.data_dir / luma_tls.CERT_FILE
        self.key_file = self.data_dir / luma_tls.KEY_FILE

        self.store = luma_users.UserStore(self.users_file, self.audit_file)
        self.aliases = luma_aliases.AliasStore(self.aliases_file)
        self.groups = luma_groups.GroupStore(self.groups_file, luma_projection.canonical_identity)
        self.listener = luma_remote.RemoteListener(app)
        # One short lock per display, so two operators cannot interleave inside
        # one device transaction. Narrow on purpose.
        self.locks = luma_locks.ResourceLocks()
        self.restore_error = ""

        self._load_cache = load_cache
        luma_gate.install(app, lambda: self.store, self.enabled, destination_check)

        luma_remote_api.register(
            app, store=self.store, listener=self.listener, aliases=self.aliases,
            groups=self.groups, load_cache=load_cache, route_av=route_av,
            observe=observe, remote_enabled=self.enabled,
            set_remote_state=self.save_preference, remote_config=self.last_choice,
            locks=self.locks, tls=luma_tls,
            cert_paths=(str(self.cert_file), str(self.key_file)),
            tls_context_for=self.tls_context, certificate_state=self.certificate_state,
            preview_fetch=preview_fetch, user_shell=self._user_shell,
            walls=walls, route_wall=route_wall, route_mode=route_mode,
            identify=identify, verify_route=verify_route)
        self._register_pages()

    # ---------------------------------------------------------------- state
    def enabled(self) -> bool:
        """Whether the remote listener is currently answering."""
        return self.listener.running

    def state(self) -> dict:
        state = self.listener.describe()
        state["has_admin"] = self.store.has_admin
        state["sessions"] = self.store.active_session_count()
        return state

    # --------------------------------------------------------- preference
    def save_preference(self, enabled, address, port, transport=None, cert_path=None,
                        key_path=None, adapter_id=None, adapter_name=None):
        """Remember what the operator chose, including how to secure it.

        Its own file, not the device configuration: that one carries credentials
        and a remote-access setting has no business in it. Only paths are stored;
        key material stays in its own file and nothing here ever holds it.
        """
        self.restore_error = ""
        previous = self.last_choice()
        luma_store.atomic_write_json(self.preference_file, {
            "enabled": bool(enabled),
            # The adapter is the choice. The address is what it had when the choice
            # was made -- kept so the same address is preferred if the adapter
            # still has it, and so Settings can say what was configured.
            "adapter_id": adapter_id if adapter_id is not None else previous.get("adapter_id", ""),
            "adapter_name": adapter_name if adapter_name is not None else previous.get("adapter_name", ""),
            "address": address or "",
            "port": int(port) if port else None,
            "transport": transport or previous.get("transport") or "https",
            "cert_path": cert_path if cert_path is not None else previous.get("cert_path", ""),
            "key_path": key_path if key_path is not None else previous.get("key_path", ""),
        })

    def last_choice(self) -> dict:
        """The stored preference, plus why a restore failed if one did.

        Three states an installed system has to distinguish, and "off" is only
        one: never configured, explicitly disabled, and configured but the
        selected network is not here today.
        """
        # Never configured: HTTP is what Settings offers first, because a certificate
        # generated for one DHCP address stops matching when the lease changes.
        # Starting plaintext still needs the administrator's explicit
        # acknowledgement, and a preference that was stored without a transport
        # reads as https below (the safe direction).
        saved = {"enabled": False, "adapter_id": "", "adapter_name": "", "address": "",
                 "port": None, "transport": "http", "cert_path": "", "key_path": ""}
        stored = luma_store.read_json(self.preference_file, default=None)
        if isinstance(stored, dict):
            saved.update({
                "enabled": bool(stored.get("enabled")),
                "adapter_id": str(stored.get("adapter_id") or ""),
                "adapter_name": str(stored.get("adapter_name") or ""),
                "address": str(stored.get("address") or ""),
                "port": stored.get("port"),
                # An absent or unreadable transport reads as https: the safe
                # direction is to refuse to start rather than serve in clear.
                "transport": "http" if stored.get("transport") == "http" else "https",
                "cert_path": str(stored.get("cert_path") or ""),
                "key_path": str(stored.get("key_path") or ""),
            })
        saved["restore_error"] = self.restore_error
        return saved

    # ------------------------------------------------------------------ TLS
    def tls_context(self, saved):
        """(context, error) for a configuration. HTTPS that cannot start never degrades."""
        if (saved or {}).get("transport") == "http":
            return None, ""
        cert = (saved or {}).get("cert_path") or str(self.cert_file)
        key = (saved or {}).get("key_path") or str(self.key_file)
        try:
            return luma_tls.load_context(cert, key), ""
        except luma_tls.TlsError as exc:
            return None, str(exc)

    def certificate_state(self) -> dict:
        """What Settings shows about the certificate. Never key material."""
        saved = self.last_choice()
        described = luma_tls.describe(saved.get("cert_path") or str(self.cert_file))
        described["path_configured"] = bool(saved.get("cert_path"))
        described["can_generate"] = luma_tls.generation_available()
        return described

    # -------------------------------------------------------------- restore
    def _address_for(self, saved):
        """Which address a saved configuration should bind now, or why it cannot.

        The operator chose an ADAPTER; this follows it to whatever address it
        carries today, so a DHCP change does not leave remote access looking for
        an address nobody has. An address-only configuration is adopted only when
        exactly one adapter owns that address, and never guessed at.
        """
        try:
            adapters = luma_adapters.enumerate_adapters()
        except Exception as exc:
            log.info("[REMOTE] adapter enumeration failed: %s", type(exc).__name__)
            adapters = []
        adapter_id = (saved or {}).get("adapter_id") or ""
        address = (saved or {}).get("address") or ""
        named = (saved or {}).get("adapter_name") or adapter_id or "that network"
        if not adapter_id:
            adopted = luma_adapters.adopt_saved_address(address, adapters)
            if adopted is None:
                return "", (f"Remote access is configured for {address}, but which network "
                            "adapter that was cannot be established on this computer. "
                            "Choose the network again to continue.")
            self.save_preference(True, adopted["address"], (saved or {}).get("port"),
                                 transport=(saved or {}).get("transport"),
                                 cert_path=(saved or {}).get("cert_path"),
                                 key_path=(saved or {}).get("key_path"),
                                 adapter_id=adopted["id"], adapter_name=adopted["name"])
            return adopted["address"], ""
        resolved, reason = luma_adapters.resolve_address(adapter_id, address, adapters)
        if reason == "adapter missing":
            return "", (f"Remote access is configured for {named}, but that network "
                        "adapter is not available on this computer right now.")
        if reason == "ambiguous":
            return "", (f"{named} has more than one address and none of them is the one "
                        "that was chosen. Choose the network again.")
        if resolved and resolved != address:
            self.save_preference(True, resolved, (saved or {}).get("port"),
                                 transport=(saved or {}).get("transport"),
                                 cert_path=(saved or {}).get("cert_path"),
                                 key_path=(saved or {}).get("key_path"),
                                 adapter_id=adapter_id, adapter_name=named)
        return resolved, ""

    def restore(self) -> dict:
        """Bring back a remote listener the administrator explicitly asked for.

        An unattended host should not need somebody to re-enable remote access
        after every power cut, but restoring a network listener from a file is
        exactly the convenience that quietly exposes a machine, so every one of
        these must hold: the preference says enabled; an administrator exists
        (bootstrap is local-only); a concrete usable address; the adapter is here
        today; and TLS can be arranged when HTTPS was chosen. A failure is a
        status to report, never a startup failure, and no other adapter is tried.
        """
        self.restore_error = ""
        saved = self.last_choice()
        if not saved.get("enabled"):
            return {"restored": False, "reason": "not enabled"}
        if not self.store.has_admin:
            self.restore_error = ("Remote access is configured, but no administrator "
                                  "account exists.")
            return {"restored": False, "reason": "no administrator"}
        address, adapter_error = self._address_for(saved)
        if adapter_error:
            self.restore_error = adapter_error
            return {"restored": False, "reason": "adapter unavailable"}
        if not luma_remote.is_usable_remote_address(address):
            self.restore_error = "The saved remote address is not usable."
            return {"restored": False, "reason": "unusable address"}
        port = saved.get("port") or self.default_port
        context, tls_error = self.tls_context(saved)
        if tls_error:
            self.restore_error = (f"Remote access is configured for HTTPS on {address}, but "
                                  f"the certificate could not be used: {tls_error}")
            return {"restored": False, "reason": "tls unavailable"}
        # A certificate LumaSuite generated for the old address does not cover the
        # new one. Starting anyway would advertise HTTPS at an address no browser
        # accepts, so the listener stays down and Settings says which button fixes
        # it. A supplied certificate is the site's own and is never judged this way.
        if saved.get("transport") != "http" and not saved.get("cert_path"):
            described = luma_tls.describe(str(self.cert_file))
            if described.get("readable") and not luma_tls.covers(described, address):
                self.restore_error = (
                    f"Remote access is configured for {address}, but the certificate "
                    f"LumaSuite created is for {described.get('subject') or 'another address'}. "
                    "Create a certificate for this network in Settings, then turn remote "
                    "access on again.")
                return {"restored": False, "reason": "certificate address mismatch"}
        names = []
        if saved.get("transport") != "http":
            names = luma_tls.describe(saved.get("cert_path") or str(self.cert_file)).get("san") or []
        result = self.listener.start(address, port, ssl_context=context, allowed_names=names)
        if not result.get("ok"):
            self.restore_error = (f"Remote access is configured for {address}, but the "
                                  f"listener could not start: {result.get('error', 'unknown error')}")
            return {"restored": False, "reason": result.get("error")}
        self.store.audit("remote_access.restored", actor=None,
                         detail={"address": address, "port": int(port),
                                 "transport": saved.get("transport")})
        return {"restored": True, "address": address, "port": int(port),
                "transport": saved.get("transport")}

    # ---------------------------------------------------------------- pages
    def _send_page(self, name):
        response = send_from_directory(str(self.ui_dir), name)
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    def _user_shell(self):
        return self._send_page("user.html")

    def _register_pages(self):
        app = self.app

        @app.route("/login", methods=["GET"])
        def login_page():
            return self._send_page("login.html")

        @app.route("/user", methods=["GET"])
        def user_page():
            return self._send_page("user.html")

        @app.route("/admin/users", methods=["GET"])
        def admin_users_page():
            return self._send_page("admin_users.html")
