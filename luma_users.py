"""Accounts, sessions and the security audit trail for remote access.

Three stores with deliberately different lifetimes:

    users.json      on disk, survives restart      who exists, and what they may do
    sessions        in memory, dropped on restart  who is currently logged in
    security audit  on disk, bounded               what happened

Sessions are server-side and opaque rather than signed tokens carrying claims.
The brief requires that an Admin disabling an account takes effect immediately,
and a self-contained token cannot do that without a revocation list -- which is
a session store with extra steps and a worse failure mode. Dropping every
session on restart is the safe default and means no session material is ever
written to disk.

Nothing in this module decides whether a *request* is allowed. It answers who
somebody is and what they have been granted; `luma_access` holds the route
policy and the gate joins them.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from pathlib import Path

from werkzeug.security import check_password_hash, generate_password_hash


import luma_mac
import luma_retention
import luma_secret
import luma_store

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

ROLE_ADMIN = "admin"
ROLE_USER = "user"
ROLES = (ROLE_ADMIN, ROLE_USER)

#: Idle timeout and absolute cap. Both enforced server-side on every request.
IDLE_TIMEOUT_SECONDS = 8 * 60 * 60
ABSOLUTE_TIMEOUT_SECONDS = 24 * 60 * 60

#: How a session was established. Not decoration: the gate refuses
#: administrative routes to a link-established session, and the audit says
#: which of the two happened.
BY_PASSWORD = "password"
BY_DIRECT_LINK = "direct_link"
SESSION_METHODS = (BY_PASSWORD, BY_DIRECT_LINK)

#: A link-established session does NOT expire with time or idleness.
#:
#: It used to expire sooner than a password session -- two hours idle, twelve
#: absolute -- on the reasoning that opening the link again makes a new one, so
#: nothing was lost. That reasoning is wrong for the thing these links are
#: actually for. A Direct Access link is a wall panel or a lectern tablet: it
#: sits untouched overnight, over a weekend, over a holiday, and the person who
#: walks up to it has no password and no reason to know it needs one. An
#: expiring session turned that panel into a sign-in page nobody could answer.
#:
#: Removing the timeout takes nothing away, because it never protected
#: anything. Whoever holds the URL can mint a fresh session at any moment by
#: opening it, so an old session grants exactly what the URL already grants.
#: What does the protecting is revocation, and that is enforced on every single
#: request rather than by waiting: `resolve_session` re-reads the account and
#: drops the session the instant it is disabled or deleted, `set_role` and
#: `set_permissions` revoke, and turning the link off or regenerating it
#: revokes every session it opened in the same critical section that replaces
#: the token.
#:
#: Password sessions are untouched and keep their 8-hour idle / 24-hour
#: absolute limits. The two are different because the credentials are
#: different: a password was typed by somebody who knew it and can type it
#: again, so ending their session costs them one sign-in. A link holder has no
#: second factor to fall back on -- the URL is the whole credential -- so
#: ending their session costs them the panel.
DIRECT_SESSIONS_EXPIRE = False

#: How many characters of a token may appear in a log line or an audit record.
#: Enough to correlate two entries, far too few to be a credential.
TOKEN_HINT_CHARS = 6

#: Login throttling. Counted per username and per source address, so neither a
#: password spray across accounts nor a dictionary attack on one is free.
MAX_FAILURES = 5

#: How much of a typed username is kept for the throttle and the audit. A sign-in
#: is the one request anybody on the network can make, and the name is theirs to
#: choose: unbounded, it is stored in the throttle for the lockout window and written
#: to an audit log that is rewritten on every flush.
MAX_RECORDED_USERNAME = 128

#: Above this many throttle entries the expired ones are dropped on the next failure.
THROTTLE_SWEEP_AT = 1024

#: Most (name, source) records the throttle will hold. Expiry alone bounds growth only
#: by how fast a caller can fail -- each failure costs a key derivation, so that is
#: slow, but it is a rate and not a limit. At this size the record that has failed
#: least recently, and has not yet reached the lockout, is dropped to make room: the
#: records that actually lock someone out are never the ones evicted.
THROTTLE_MAX_KEYS = 8192
LOCKOUT_SECONDS = 15 * 60

#: The audit log is bounded twice over (see luma_retention): by age -- 30 days
#: by default -- and by file size, both configurable by an administrator. An
#: unbounded security log is its own outage, and a failed login is something an
#: attacker can produce at will. `luma_retention.HARD_MAX_ENTRIES` is the
#: ceiling beneath both.
AUDIT_MAX_ENTRIES = luma_retention.HARD_MAX_ENTRIES

#: How often the audit file is rewritten at most. Every event used to rewrite
#: the whole file -- up to 5000 entries -- while holding the lock that
#: authentication also needs, so a brute-force attempt was simultaneously a
#: denial of service against sign-in. Entries are kept in memory and flushed on
#: this cadence, and always immediately for the events somebody would come
#: looking for.
AUDIT_FLUSH_SECONDS = 5.0

#: Events written through immediately rather than waiting for the cadence.
#: Chosen because these are what an administrator reconstructs an incident
#: from, and losing the last few seconds of them to a crash would matter.
#:
#: These must be the names the store actually emits. An earlier list named
#: `user.set_role`, `user.set_permissions` and `user.set_password`, which are
#: *actions* and never events, so a password, role or permission change was held
#: back for the flush cadence like any routine entry -- the opposite of the intent.
AUDIT_IMMEDIATE_EVENTS = frozenset({
    "bootstrap.admin_created", "user.create", "user.deleted",
    "user.role_changed", "user.permissions_changed", "user.password_changed",
    "user.disabled", "user.enabled",
    "user.standard_routing_enabled", "user.standard_routing_disabled",
    "user.matrix_routing_enabled", "user.matrix_routing_disabled",
    "direct_access.enabled", "direct_access.regenerated", "direct_access.disabled",
    "remote_access.enabled", "remote_access.disabled",
    "audit.retention_changed",
    "certificate.generated", "certificate.configured", "certificate.removed",
    "backup.restored",
})

#: Never written to the audit log or returned by any API, whatever a caller
#: puts in a request body. Mirrors the server's existing audit redaction set.
SECRET_FIELDS = frozenset({
    "password", "passwd", "pwd", "new_password", "old_password",
    "current_password", "confirm_password", "fallback_password",
    "token", "secret", "session", "csrf", "csrf_token", "password_hash",
})


def _now() -> float:
    return time.time()


def _atomic_write_json(path: Path, payload) -> bool:
    """Unique temp file, fsync, os.replace -- the rule the project already has.

    A shared temp name lets two writers collide; on Windows os.replace fails
    with a sharing violation while another writer holds the file.
    """
    tmp = path.with_suffix(f"{path.suffix}.{os.getpid()}-{threading.get_ident()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        return True
    except Exception as exc:
        log.error("[USERS] could not write %s: %s", path.name, type(exc).__name__)
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        return False


_dummy_hash_value = None
_dummy_hash_lock = threading.Lock()


def dummy_hash() -> str:
    """A stored hash that matches nothing, made once and then reused.

    A sign-in for a name that does not exist still has to be checked against
    something, or the answer comes back faster and says the name is not real. It used
    to be checked against a hash made on the spot, which is a second key derivation
    on top of the check: an unknown name cost about twice a known one, so the
    difference this was meant to remove was larger than the one it removed -- and a
    caller choosing names paid the server a double derivation each time.
    """
    global _dummy_hash_value
    with _dummy_hash_lock:
        if _dummy_hash_value is None:
            _dummy_hash_value = hash_password(secrets.token_urlsafe(16))
        return _dummy_hash_value


def hash_password(password: str) -> str:
    """Salted scrypt, via werkzeug.

    Chosen because Flask already brings werkzeug, so remote access adds no
    dependency to a PyInstaller build; because scrypt is memory-hard, which a
    plain SHA is not; and because it is not custom cryptography.

    The method is named rather than left to the library's default. Werkzeug made
    scrypt its default only in 3.0; the 2.3.7 this application is built with
    defaults to PBKDF2, so relying on the default meant the shipped build hashed
    with a different algorithm from the one that was tested and documented.
    """
    return generate_password_hash(password, method="scrypt")


def verify_password(stored_hash: str, password: str) -> bool:
    if not stored_hash or password is None:
        return False
    try:
        return check_password_hash(stored_hash, password)
    except Exception:
        # A malformed stored hash must read as "wrong password", never as an
        # exception that a caller might treat as a pass.
        return False


def redact(payload):
    """Recursively replace secret-bearing fields before anything records them."""
    if isinstance(payload, dict):
        return {k: ("***" if str(k).lower() in SECRET_FIELDS else redact(v))
                for k, v in payload.items()}
    if isinstance(payload, (list, tuple)):
        return [redact(v) for v in payload]
    return payload


class Permissions:
    """What one restricted account may route. Devices are named by stable identity.

    There is exactly one capability -- routing audio and video as one operation --
    so there is no feature list to hold. What varies is *which* equipment: the
    sources it may send, the displays it may drive, and the display groups it may
    drive as a group.

    Keyed by MAC wherever one is known, never by address: identity must not
    depend on an IP when a hardware identifier exists, and a permission that
    followed an address would silently transfer to whatever answers there next.

    A group grant is its own resource. Holding a group grants the group action and
    nothing else: it does not add the group's members to `destinations`, so the
    members are not individually routable and are not listed to this account as
    individual displays.
    """

    __slots__ = ("sources", "destinations", "groups", "walls",
                 "standard_routing", "matrix_routing")

    def __init__(self, sources=(), destinations=(), groups=(), walls=(),
                 standard_routing=True, matrix_routing=False):
        # Which routing interfaces the account may use: the card interface
        # ("standard") and the permission-filtered matrix. Drag and drop is not a
        # third capability -- it is the standard interface's other way to press
        # Route. An account created before this existed has neither key and keeps
        # exactly what it had: standard on, matrix off. Neither on is not a state an
        # account can hold, so it falls back to standard rather than to no routing.
        self.standard_routing = bool(standard_routing)
        self.matrix_routing = bool(matrix_routing)
        if not (self.standard_routing or self.matrix_routing):
            self.standard_routing = True
        # An identity the normaliser refuses (it reads as an IPv4 address) normalises to "", which is
        # dropped here: "" must never be a member, or every unreadable identity would match it.
        self.sources = frozenset(i for i in (_normalise_identity(s) for s in sources if s) if i)
        self.destinations = frozenset(i for i in (_normalise_identity(d) for d in destinations if d) if i)
        self.groups = frozenset(_normalise_group_id(g) for g in groups if g)
        # A saved video wall is its own resource, named by its stable wall id. Holding
        # one grants routing to the wall and nothing else: its member decoders are
        # neither added to `destinations` nor made individually routable.
        self.walls = frozenset(_normalise_group_id(w) for w in walls if w)

    def to_json(self) -> dict:
        return {"sources": sorted(self.sources),
                "destinations": sorted(self.destinations),
                "groups": sorted(self.groups),
                "walls": sorted(self.walls),
                "standard_routing": self.standard_routing,
                "matrix_routing": self.matrix_routing}

    def replace(self, **changes) -> "Permissions":
        """A copy with some categories changed, so no caller has to name them all."""
        return Permissions(changes.get("sources", self.sources),
                           changes.get("destinations", self.destinations),
                           changes.get("groups", self.groups),
                           changes.get("walls", self.walls),
                           changes.get("standard_routing", self.standard_routing),
                           changes.get("matrix_routing", self.matrix_routing))

    @classmethod
    def from_json(cls, raw) -> "Permissions":
        raw = raw if isinstance(raw, dict) else {}
        return cls(_as_list(raw.get("sources")),
                   _as_list(raw.get("destinations")),
                   _as_list(raw.get("groups")),
                   _as_list(raw.get("walls")),
                   _flag(raw.get("standard_routing"), True),
                   _flag(raw.get("matrix_routing"), False))

    def may_use_source(self, identity) -> bool:
        wanted = _normalise_identity(identity)
        return bool(wanted) and wanted in self.sources

    def may_control_destination(self, identity) -> bool:
        wanted = _normalise_identity(identity)
        return bool(wanted) and wanted in self.destinations

    def may_control_group(self, group_id) -> bool:
        return _normalise_group_id(group_id) in self.groups

    def may_control_wall(self, wall_id) -> bool:
        return _normalise_group_id(wall_id) in self.walls

    def may_use_interface(self, name) -> bool:
        """Whether the account may use one routing interface ("standard"/"matrix")."""
        if name == "matrix":
            return self.matrix_routing
        if name == "standard":
            return self.standard_routing
        return False

    def interfaces(self) -> dict:
        return {"standard": self.standard_routing, "matrix": self.matrix_routing}


_INTERFACE_EVENTS = {
    ("standard", True): "user.standard_routing_enabled",
    ("standard", False): "user.standard_routing_disabled",
    ("matrix", True): "user.matrix_routing_enabled",
    ("matrix", False): "user.matrix_routing_disabled",
}


def _flag(value, default):
    """A boolean from untrusted input. Absent or not a boolean means the default."""
    return value if isinstance(value, bool) else default


def _as_list(value):
    """A permission list from untrusted input: a list of strings, or nothing."""
    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    return [v for v in value if isinstance(v, str)]


def _normalise_group_id(group_id) -> str:
    """A group id is opaque text; fold only case and surrounding space."""
    return str(group_id or "").strip().lower()


def _normalise_identity(identity) -> str:
    """One spelling for a device identity.

    MACs arrive from different code paths as `02:00:5E:..`, `02-00-5e-..` and
    unseparated. A permission check that compared spellings would deny a
    permitted device, so they are folded here -- and only here, so there is one
    normaliser rather than one per call site.
    """
    text = str(identity or "").strip().lower()
    if not text:
        return ""
    if luma_mac.looks_like_ipv4(text):
        return ""            # an address is not an identity, in any spelling
    mac = luma_mac.normalise_mac(text)
    return mac if mac else text


class DirectAccess:
    """A per-account passwordless link, off unless an administrator turns it on.

    Possession of the link is the credential, so this is deliberately narrow:

      * it exists only on a restricted account. An administrator's account may
        never carry one, and a session created from a link is refused
        administrative routes even if the account is promoted afterwards.
      * it carries no permissions of its own. It names an account, and every
        request is authorised against that account as it is at that moment, so
        an administrator taking a capability or a device away takes it away
        from the link in the same instant.
      * exactly one token is live at a time. Regenerating replaces it; there is
        no history of previously valid links.

    Stored as a digest for lookup and comparison, plus a protected copy so the
    operator can be shown the same link again later. `luma_secret` says what
    that protection actually is on this platform -- it is DPAPI on Windows and
    the record says so, rather than the product implying more than it did.
    """

    __slots__ = ("enabled", "digest", "blob", "protection", "created_at")

    def __init__(self, enabled=False, digest="", blob="", protection="",
                 created_at=None):
        self.enabled = bool(enabled)
        self.digest = str(digest or "")
        self.blob = str(blob or "")
        self.protection = str(protection or "")
        self.created_at = created_at or 0

    @property
    def active(self) -> bool:
        return bool(self.enabled and self.digest)

    def to_json(self) -> dict:
        return {"enabled": self.enabled, "digest": self.digest,
                "blob": self.blob, "protection": self.protection,
                "created_at": self.created_at}

    @classmethod
    def from_json(cls, raw) -> "DirectAccess":
        raw = raw if isinstance(raw, dict) else {}
        return cls(enabled=raw.get("enabled"), digest=raw.get("digest"),
                   blob=raw.get("blob"), protection=raw.get("protection"),
                   created_at=raw.get("created_at"))

    def public(self) -> dict:
        """What an API may return about it. Never the token, never the blob."""
        return {"enabled": self.active,
                "protection": self.protection or "",
                "created_at": self.created_at or 0}


def direct_token_digest(token) -> str:
    """The lookup key for a link token.

    SHA-256 rather than a password hash: the token is 256 bits of output from
    the system random source, so there is nothing to brute force and a
    deliberately slow hash would only make every request slower.
    """
    text = str(token or "")
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Account:
    __slots__ = ("id", "username", "password_hash", "role", "enabled",
                 "permissions", "created_at", "password_changed_at",
                 "direct_access")

    def __init__(self, id, username, password_hash, role, enabled=True,
                 permissions=None, created_at=None, password_changed_at=None,
                 direct_access=None):
        self.id = id
        self.username = username
        self.password_hash = password_hash
        self.role = role if role in ROLES else ROLE_USER
        self.enabled = bool(enabled)
        self.permissions = permissions or Permissions()
        self.created_at = created_at or _now()
        self.password_changed_at = password_changed_at or self.created_at
        self.direct_access = direct_access or DirectAccess()

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN

    def to_json(self) -> dict:
        return {"id": self.id, "username": self.username,
                "password_hash": self.password_hash, "role": self.role,
                "enabled": self.enabled,
                "permissions": self.permissions.to_json(),
                "created_at": self.created_at,
                "password_changed_at": self.password_changed_at,
                "direct_access": self.direct_access.to_json()}

    def public(self) -> dict:
        """What an API may return. Never the hash, never anything derived from it."""
        return {"id": self.id, "username": self.username, "role": self.role,
                "enabled": self.enabled,
                "permissions": self.permissions.to_json(),
                "created_at": self.created_at,
                # Whether a link exists, and nothing that would let anybody
                # reconstruct one. Retrieving the link itself is a separate,
                # deliberate request.
                "direct_access": self.direct_access.public()}

    @classmethod
    def from_json(cls, raw) -> "Account":
        return cls(id=raw.get("id"), username=raw.get("username"),
                   password_hash=raw.get("password_hash"),
                   role=raw.get("role"), enabled=raw.get("enabled", True),
                   permissions=Permissions.from_json(raw.get("permissions")),
                   created_at=raw.get("created_at"),
                   password_changed_at=raw.get("password_changed_at"),
                   direct_access=DirectAccess.from_json(
                       raw.get("direct_access")))


class Session:
    __slots__ = ("token", "user_id", "created_at", "last_seen", "csrf_token",
                 "method")

    def __init__(self, token, user_id, csrf_token, method=BY_PASSWORD):
        self.token = token
        self.user_id = user_id
        self.csrf_token = csrf_token
        # How the holder proved who they were. Recorded because the two are
        # not equivalent: a password was typed by somebody who knew it, and a
        # link was opened by whoever has the link. The gate refuses
        # administrative routes to the second whatever the account's role has
        # since become.
        self.method = method if method in SESSION_METHODS else BY_PASSWORD
        self.created_at = _now()
        self.last_seen = self.created_at

    @property
    def from_direct_link(self) -> bool:
        return self.method == BY_DIRECT_LINK

    def expired(self, now=None) -> bool:
        now = now or _now()
        if self.from_direct_link:
            # Never, by time or idleness. A link-established session ends when
            # somebody ends it: the account disabled or deleted, the role
            # changed, the permissions changed, the link turned off or
            # regenerated. Every one of those revokes immediately and is
            # re-checked on every request, so waiting adds nothing.
            #
            # A wall panel is the ordinary case for these links, and it is
            # idle by definition. Expiring its session put a sign-in page in
            # front of somebody who has no password to give it.
            return DIRECT_SESSIONS_EXPIRE
        return (now - self.last_seen > IDLE_TIMEOUT_SECONDS
                or now - self.created_at > ABSOLUTE_TIMEOUT_SECONDS)


class UserStore:
    """Accounts on disk, sessions in memory, one lock over both.

    One lock rather than two because the operations that matter span them: an
    Admin disabling an account must revoke its sessions in the same critical
    section, or a request already in flight is authorised against an account
    that no longer exists.
    """

    def __init__(self, path: Path, audit_path: Path | None = None):
        self._path = Path(path)
        self._audit_path = Path(audit_path) if audit_path else None
        self._lock = threading.RLock()
        self._accounts: dict[str, Account] = {}
        self._sessions: dict[str, Session] = {}
        self._failures: dict[str, list] = {}
        self._audit: list = []
        self._audit_sizes: list = []
        self._audit_retention = luma_retention.defaults()
        self._audit_dirty = False
        self._audit_flushed_at = 0.0
        self._load()
        self._load_audit()

    # ---- persistence -------------------------------------------------
    def _load(self) -> None:
        with self._lock:
            self._accounts = {}
            if not self._path.exists():
                # No file means no accounts, which is the correct state for a
                # local-only installation that has never enabled remote access.
                return
            try:
                raw = json.loads(luma_store.read_text_retrying(self._path))
            except luma_store.ReadFailed as exc:
                # The file could not be read (not: its contents are bad). It is left where it
                # is rather than renamed away; this process starts with no accounts and says so.
                log.error("[USERS] %s could not be read (%s); leaving it in place",
                          self._path.name, type(exc.__cause__ or exc).__name__)
                return
            except Exception as exc:
                # A malformed store quarantines and degrades to empty rather
                # than crashing, and never silently becomes "no restrictions".
                log.error("[USERS] %s is unreadable (%s); quarantining",
                          self._path.name, type(exc).__name__)
                try:
                    self._path.replace(self._path.with_suffix(".corrupt"))
                except OSError:
                    pass
                return
            for entry in (raw or {}).get("users") or []:
                try:
                    account = Account.from_json(entry)
                    if account.id and account.username:
                        self._accounts[account.id] = account
                except Exception:
                    log.warning("[USERS] skipping an unreadable account record")

    def _persist(self) -> bool:
        with self._lock:
            return _atomic_write_json(self._path, {
                "version": SCHEMA_VERSION,
                "users": [a.to_json() for a in self._accounts.values()]})

    # ---- bootstrap ---------------------------------------------------
    @property
    def has_admin(self) -> bool:
        with self._lock:
            return any(a.is_admin and a.enabled for a in self._accounts.values())

    @property
    def is_empty(self) -> bool:
        with self._lock:
            return not self._accounts

    # ---- accounts ----------------------------------------------------
    def find_by_username(self, username) -> Account | None:
        target = str(username or "").strip().lower()
        with self._lock:
            for account in self._accounts.values():
                if account.username.lower() == target:
                    return account
        return None

    def get(self, user_id) -> Account | None:
        with self._lock:
            return self._accounts.get(user_id)

    def list_accounts(self) -> list:
        with self._lock:
            return [a.public() for a in
                    sorted(self._accounts.values(), key=lambda a: a.username.lower())]

    def create(self, username, password, role, permissions=None,
               *, actor=None, audit=True) -> dict:
        username = str(username or "").strip()
        if not username:
            return {"ok": False, "error": "username required"}
        if len(str(password or "")) < 8:
            return {"ok": False, "error": "password must be at least 8 characters"}
        if role not in ROLES:
            return {"ok": False, "error": "role must be admin or user"}
        with self._lock:
            if self.find_by_username(username):
                return {"ok": False, "error": "that username already exists"}
            account = Account(
                id=secrets.token_urlsafe(12), username=username,
                password_hash=hash_password(password), role=role,
                permissions=permissions or Permissions())
            self._accounts[account.id] = account
            self._persist()
            if audit:
                self.audit("user.create", actor=actor,
                           detail={"username": username, "role": role})
            return {"ok": True, "user": account.public()}

    def set_enabled(self, user_id, enabled: bool, *, actor=None) -> dict:
        with self._lock:
            account = self._accounts.get(user_id)
            if not account:
                return {"ok": False, "error": "no such user"}
            if not enabled and account.is_admin and self._last_enabled_admin(account):
                return {"ok": False,
                        "error": "this is the only enabled administrator"}
            account.enabled = bool(enabled)
            self._persist()
            if not enabled:
                # Immediately, in the same critical section: a disabled account
                # must not survive in a session that is already open.
                self.revoke_all_for_user(user_id, _locked=True)
            self.audit("user.enabled" if enabled else "user.disabled",
                       actor=actor, detail={"username": account.username})
            return {"ok": True, "user": account.public()}

    def _last_enabled_admin(self, account: Account) -> bool:
        others = [a for a in self._accounts.values()
                  if a.is_admin and a.enabled and a.id != account.id]
        return not others

    def set_password(self, user_id, password, *, actor=None) -> dict:
        if len(str(password or "")) < 8:
            return {"ok": False, "error": "password must be at least 8 characters"}
        with self._lock:
            account = self._accounts.get(user_id)
            if not account:
                return {"ok": False, "error": "no such user"}
            account.password_hash = hash_password(password)
            account.password_changed_at = _now()
            self._persist()
            # Every password-established session for this account ends. A password change
            # that left old sessions alive would not be a password change. A session a
            # Direct Access link opened follows the link's own lifecycle, not the password's.
            self.revoke_password_sessions(user_id, _locked=True)
            self.audit("user.password_changed", actor=actor,
                       detail={"username": account.username})
            return {"ok": True}

    def set_role(self, user_id, role, *, actor=None) -> dict:
        if role not in ROLES:
            return {"ok": False, "error": "role must be admin or user"}
        with self._lock:
            account = self._accounts.get(user_id)
            if not account:
                return {"ok": False, "error": "no such user"}
            if account.is_admin and role != ROLE_ADMIN and self._last_enabled_admin(account):
                return {"ok": False, "error": "this is the only enabled administrator"}
            account.role = role
            if role == ROLE_ADMIN:
                # An administrator never carries a passwordless link. Cleared
                # here as well as refused at validation, so the record agrees
                # with the rule rather than merely being overruled by it.
                account.direct_access = DirectAccess()
            self._persist()
            self.revoke_all_for_user(user_id, _locked=True)
            self.audit("user.role_changed", actor=actor,
                       detail={"username": account.username, "role": role})
            return {"ok": True, "user": account.public()}

    def set_permissions(self, user_id, permissions: Permissions,
                        *, actor=None) -> dict:
        with self._lock:
            account = self._accounts.get(user_id)
            if not account:
                return {"ok": False, "error": "no such user"}
            before = account.permissions
            account.permissions = permissions
            self._persist()
            for name in ("standard", "matrix"):
                now_on = permissions.may_use_interface(name)
                if before.may_use_interface(name) != now_on:
                    self.audit(_INTERFACE_EVENTS[(name, now_on)], actor=actor,
                               detail={"username": account.username})
            # Nothing is revoked: every request resolves the account and asks
            # its *current* permissions, so a removal binds the very next
            # request on the session already open, and a grant appears without
            # anyone signing in again. Ending the session would only make the
            # person sign in to see what the server already enforces.
            self.audit("user.permissions_changed", actor=actor,
                       detail={"username": account.username,
                               "permissions": permissions.to_json()})
            return {"ok": True, "user": account.public()}

    def delete(self, user_id, *, actor=None) -> dict:
        with self._lock:
            account = self._accounts.get(user_id)
            if not account:
                return {"ok": False, "error": "no such user"}
            if account.is_admin and self._last_enabled_admin(account):
                return {"ok": False, "error": "this is the only enabled administrator"}
            del self._accounts[user_id]
            self._persist()
            self.revoke_all_for_user(user_id, _locked=True)
            self.audit("user.deleted", actor=actor,
                       detail={"username": account.username})
            return {"ok": True}

    # ---- authentication ---------------------------------------------
    def _throttle_key(self, username, source) -> tuple:
        return (str(username or "").strip().lower()[:MAX_RECORDED_USERNAME],
                str(source or "")[:MAX_RECORDED_USERNAME])

    def locked_out(self, username, source) -> bool:
        key = self._throttle_key(username, source)
        cutoff = _now() - LOCKOUT_SECONDS
        with self._lock:
            recent = [t for t in self._failures.get(key, []) if t > cutoff]
            if recent:
                self._failures[key] = recent
            else:
                # Not an empty list: a name nobody has failed with is not a record.
                # Keeping one for every name ever asked about grows without limit.
                self._failures.pop(key, None)
            return len(recent) >= MAX_FAILURES

    def _record_failure(self, username, source) -> None:
        key = self._throttle_key(username, source)
        with self._lock:
            if len(self._failures) >= THROTTLE_SWEEP_AT:
                cutoff = _now() - LOCKOUT_SECONDS
                for stale in [k for k, times in self._failures.items()
                              if not times or times[-1] <= cutoff]:
                    del self._failures[stale]
            if key not in self._failures and len(self._failures) >= THROTTLE_MAX_KEYS:
                evictable = [(times[-1], k) for k, times in self._failures.items()
                             if len(times) < MAX_FAILURES]
                if evictable:
                    del self._failures[min(evictable)[1]]
                else:
                    # Every record is a lockout. Nothing is dropped to make room for a
                    # name that has failed once; it is simply not remembered.
                    return
            self._failures.setdefault(key, []).append(_now())

    def record_failure(self, name, source) -> None:
        """Count a refusal that is not a password attempt against the throttle.

        A Direct Access link has no username to attribute a refusal to -- by
        design, since every refusal answers identically -- so it is counted
        under a fixed name that no account can have. Same counter, same
        lockout, same per-source scoping.
        """
        self._record_failure(name, source)

    def authenticate(self, username, password, source=None) -> dict:
        """Verify a credential. Never says which half was wrong."""
        if self.locked_out(username, source):
            self.audit("login.throttled", actor=None,
                       detail={"username": str(username or "")[:MAX_RECORDED_USERNAME],
                               "source": source})
            return {"ok": False, "error": "too many attempts; try again later",
                    "throttled": True}

        account = self.find_by_username(username)
        # Verify even when the account does not exist, so the response time does
        # not say whether a username is real. Against a hash made once, not one made
        # now: see dummy_hash. Made before the check either way, so the first sign-in
        # after a start is not the slow one only when the name is unknown.
        dummy = dummy_hash()
        stored = account.password_hash if account else dummy
        good = verify_password(stored, password)

        if not account or not good or not account.enabled:
            self._record_failure(username, source)
            reason = ("disabled" if account and good and not account.enabled
                      else "bad_credentials")
            self.audit("login.failure", actor=None,
                       detail={"username": str(username or "")[:MAX_RECORDED_USERNAME],
                               "source": source,
                               "reason": reason})
            return {"ok": False, "error": "invalid username or password"}

        with self._lock:
            self._failures.pop(self._throttle_key(username, source), None)
        session = self.open_session(account.id)
        self.audit("login.success", actor=account,
                   detail={"username": account.username, "source": source})
        return {"ok": True, "user": account.public(),
                "token": session.token, "csrf_token": session.csrf_token}

    # ---- sessions ----------------------------------------------------
    def open_session(self, user_id, method=BY_PASSWORD) -> Session:
        """A fresh token every time: logging in never reuses an existing id."""
        session = Session(token=secrets.token_urlsafe(32), user_id=user_id,
                          csrf_token=secrets.token_urlsafe(32), method=method)
        with self._lock:
            self._sessions[session.token] = session
        return session

    # ---- direct access links -----------------------------------------
    #
    # An administrator may give one restricted account a URL that works without
    # a password, for a tablet on a lectern or a phone in a classroom. Four
    # things keep that from being a hole:
    #
    #   * it is off by default and per account, and an administrator turns it
    #     on deliberately,
    #   * it can never exist on an administrator's account,
    #   * the token names an account and carries no permissions, so everything
    #     is still authorised against that account as it is now,
    #   * and every way of changing the account -- disabling it, deleting it,
    #     changing its role, password or permissions -- already revokes its
    #     sessions, so a change takes effect on the next request rather than
    #     whenever a session happens to lapse.

    def enable_direct_access(self, user_id, *, actor=None) -> dict:
        """Turn the feature on and mint the first token."""
        return self._mint_direct_token(user_id, actor=actor,
                                       event="direct_access.enabled")

    def regenerate_direct_access(self, user_id, *, actor=None) -> dict:
        """Replace the token. Every URL handed out before this stops working."""
        with self._lock:
            account = self._accounts.get(user_id)
            if account is None or not account.direct_access.active:
                return {"ok": False, "error": "direct access is not enabled"}
        return self._mint_direct_token(user_id, actor=actor,
                                       event="direct_access.regenerated")

    def _mint_direct_token(self, user_id, *, actor, event) -> dict:
        token = secrets.token_urlsafe(32)
        blob, protection = luma_secret.protect(token)
        with self._lock:
            account = self._accounts.get(user_id)
            if account is None:
                return {"ok": False, "error": "no such user"}
            if account.is_admin:
                # Stated as a refusal rather than a filter in the UI, because a
                # passwordless administrator URL is the one outcome this whole
                # feature must not be able to produce.
                return {"ok": False,
                        "error": "direct access links are for restricted users "
                                 "only"}
            account.direct_access = DirectAccess(
                enabled=True, digest=direct_token_digest(token),
                blob=blob, protection=protection, created_at=_now())
            self._persist()
            # The previous token's sessions end here, in the same critical
            # section that replaced it. Regenerating a link that left the old
            # one's sessions running would not have replaced anything.
            self.revoke_direct_sessions(user_id, _locked=True)
            self.audit(event, actor=actor,
                       detail={"username": account.username,
                               "protection": protection})
            return {"ok": True, "token": token, "user": account.public()}

    def disable_direct_access(self, user_id, *, actor=None) -> dict:
        with self._lock:
            account = self._accounts.get(user_id)
            if account is None:
                return {"ok": False, "error": "no such user"}
            account.direct_access = DirectAccess()
            self._persist()
            self.revoke_direct_sessions(user_id, _locked=True)
            self.audit("direct_access.disabled", actor=actor,
                       detail={"username": account.username})
            return {"ok": True, "user": account.public()}

    def direct_access_token(self, user_id):
        """The live token for an account, or None.

        Returns None both when there is no link and when the stored copy cannot
        be read on this machine -- a blob protected for another Windows account
        or another computer. The caller says "regenerate the link", which is
        true and is the only thing that would help.
        """
        with self._lock:
            account = self._accounts.get(user_id)
            if account is None or not account.direct_access.active:
                return None
            record = account.direct_access
        recovered = luma_secret.unprotect(record.blob, record.protection)
        if not recovered:
            return None
        # The blob and the digest are written together; if they disagree the
        # record is damaged and showing the link would hand out one that does
        # not work.
        if not hmac.compare_digest(direct_token_digest(recovered),
                                   record.digest):
            log.warning("[DIRECT] stored link for %s does not match its "
                        "digest; treating as unreadable", account.username)
            return None
        return recovered

    def resolve_direct_token(self, token):
        """The account a link token names, or None. Never says why not.

        Deliberately one answer for every failure: an unknown token, a token
        whose account was deleted, disabled, promoted to administrator, or had
        its link turned off. Distinguishing them would let somebody holding an
        old URL learn things about an account they have no business knowing.
        """
        digest = direct_token_digest(token)
        if not digest:
            return None
        with self._lock:
            for account in self._accounts.values():
                record = account.direct_access
                if not record.active:
                    continue
                if not hmac.compare_digest(record.digest, digest):
                    continue
                if not account.enabled or account.is_admin:
                    return None
                return account
        return None

    # ---- what is assigned, and to whom --------------------------------
    def assigned_devices(self) -> dict:
        """identity -> sorted usernames that were given that device.

        Every way an account can be given a device counts: as a source or as a
        display. (Group membership is held by the group store, which has its own
        answer to "who references this device".)

        Disabled accounts are included. An account that is off is one an
        administrator can turn back on, and its assignments are still theirs.
        """
        assigned: dict = {}
        with self._lock:
            for account in self._accounts.values():
                if account.is_admin:
                    # An administrator has no per-device grants -- the role is
                    # the grant -- so there is nothing here to protect.
                    continue
                permissions = account.permissions
                identities = set(permissions.sources) | set(permissions.destinations)
                for identity in identities:
                    if identity:
                        assigned.setdefault(identity, set()).add(account.username)
        return {identity: sorted(names) for identity, names in assigned.items()}

    def direct_access_usernames(self) -> set:
        """Usernames whose passwordless link is live (their grants are reachable by link)."""
        with self._lock:
            return {a.username for a in self._accounts.values()
                    if not a.is_admin and a.direct_access.active}

    def hold(self):
        """The store's own lock, for an operation that must not interleave with an edit.

        Clear Units classifies devices as assigned or not and then removes the unassigned
        ones; holding this across both is what stops a grant added in between from
        losing its device. Re-entrant, so the store's own methods still work inside it.
        """
        return self._lock

    def forget_devices(self, identities) -> dict:
        """Take cleared devices out of every account's grants, all-or-nothing.

        Returns {"changed": accounts touched, "before": a snapshot for undo()}.
        Held under the store lock for the whole read-modify-write, and the file
        is written once; if that write fails the in-memory grants are put back.
        """
        gone = {_normalise_identity(i) for i in identities if i}
        with self._lock:
            before = {a.id: a.permissions for a in self._accounts.values()}
            changed = 0
            for account in self._accounts.values():
                p = account.permissions
                if gone & (p.sources | p.destinations):
                    account.permissions = p.replace(
                        sources=p.sources - gone, destinations=p.destinations - gone)
                    changed += 1
            if changed and not self._persist():
                for account in self._accounts.values():
                    account.permissions = before[account.id]
                return {"ok": False, "changed": 0, "before": before}
            return {"ok": True, "changed": changed, "before": before}

    def restore_permissions(self, before) -> None:
        """Put back what forget_devices() snapshotted (rollback of a failed clear)."""
        with self._lock:
            for account in self._accounts.values():
                if account.id in before:
                    account.permissions = before[account.id]
            self._persist()

    def permission_revision(self, account, extra="") -> str:
        """A short digest of everything that decides what this account sees.

        Cheap and read-only: the User page compares it to decide whether to
        re-read its state, so a change made by an administrator reaches an open
        page without the page asking the devices anything.
        """
        with self._lock:
            live = self._accounts.get(account.id)
            if live is None or not live.enabled:
                return ""
            payload = json.dumps([live.role, live.permissions.to_json(), extra],
                                 sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def forget_group(self, group_id) -> int:
        """Take a deleted group out of every account's grants. Returns how many changed.

        A grant that names a group that no longer exists would silently apply to
        nothing today, and to a different group if an id were ever reused; removing
        it keeps the stored permissions meaning what they say.
        """
        gid = _normalise_group_id(group_id)
        changed = 0
        with self._lock:
            for account in self._accounts.values():
                if gid in account.permissions.groups:
                    account.permissions = account.permissions.replace(
                        groups=account.permissions.groups - {gid})
                    changed += 1
            if changed:
                self._persist()
        return changed

    def forget_walls(self, keep_ids) -> int:
        """Drop wall grants whose wall no longer exists. Returns accounts changed.

        Called after a saved wall is deleted or the saved set is replaced, so a
        grant never outlives its wall -- and never attaches to a different wall
        that happens to reuse an id. `keep_ids` is every wall id that still exists.
        """
        keep = {_normalise_group_id(w) for w in keep_ids if w}
        changed = 0
        with self._lock:
            for account in self._accounts.values():
                stale = account.permissions.walls - keep
                if stale:
                    account.permissions = account.permissions.replace(
                        walls=account.permissions.walls - stale)
                    changed += 1
            if changed:
                self._persist()
        return changed

    def accounts_with_wall(self, wall_id) -> list:
        wid = _normalise_group_id(wall_id)
        with self._lock:
            return sorted(a.username for a in self._accounts.values()
                          if wid in a.permissions.walls)

    def accounts_with_group(self, group_id) -> list:
        """Usernames granted this group, for the warning before deleting it."""
        gid = _normalise_group_id(group_id)
        with self._lock:
            return sorted(a.username for a in self._accounts.values()
                          if gid in a.permissions.groups)

    def revoke_password_sessions(self, user_id, _locked=False) -> int:
        """End password-established sessions only, leaving a link-opened one alone."""
        def _do():
            doomed = [t for t, s in self._sessions.items()
                      if s.user_id == user_id and not s.from_direct_link]
            for token in doomed:
                self._sessions.pop(token, None)
            return len(doomed)
        if _locked:
            return _do()
        with self._lock:
            return _do()

    def revoke_direct_sessions(self, user_id, _locked=False) -> int:
        """End link-established sessions only, leaving a password one alone."""
        def _do():
            doomed = [t for t, s in self._sessions.items()
                      if s.user_id == user_id and s.from_direct_link]
            for token in doomed:
                self._sessions.pop(token, None)
            return len(doomed)
        if _locked:
            return _do()
        with self._lock:
            return _do()

    #: What a session of an account depends on. A session is a credential issued for the account
    #: as it stood, so after a restore it may continue only if every one of these is unchanged.
    #: One entry per line so that each can be mutation-tested on its own.
    _SECURITY_FIELDS = (
        "password",            # the hash a password session was made with
        "role",                # what the account may reach at all
        "enabled",             # an account that was switched off and comes back on is another account
        "direct",              # the Direct Access link a link session was opened by (digest, if live)
        "sources",             # the sources it may send
        "destinations",        # the displays it may drive
        "groups",              # the display groups it may drive
        "walls",               # the video walls it may drive
        "standard_routing",    # the card interface
        "matrix_routing",      # the permission-filtered matrix
    )

    @staticmethod
    def _security_state(account) -> dict:
        """The fields of `_SECURITY_FIELDS` for one account, as comparable values."""
        state = dict(account.permissions.to_json())
        state["password"] = account.password_hash
        state["role"] = account.role
        state["enabled"] = bool(account.enabled)
        state["direct"] = account.direct_access.digest if account.direct_access.active else ""
        return state

    def credential_fingerprints(self) -> dict:
        """account id -> what a session of that account depends on, as it is right now.

        Taken just before a restore replaces the accounts, so the restore can tell which
        sessions belong to an account that came back different. Hashes and digests are
        compared, never shown or logged.
        """
        with self._lock:
            return {a.id: self._security_state(a) for a in self._accounts.values()}

    def revoke_sessions_outdated_by(self, before: dict) -> dict:
        """End every session whose account is no longer security-equivalent to the one it was opened for.

        Called after a restore has replaced the accounts. An existing session survives only when
        its account came back the same in every respect that matters: password, role,
        permissions (sources, displays, groups, walls, routing interfaces), enabled state and
        Direct Access link. Any difference ends every session of that account, password and link
        alike, administrator or user -- a restore that changed what someone may do is not a reason
        to leave them signed in as the person they were. An account the backup does not hold, or
        holds disabled, loses every session. An account that came back exactly as it was keeps
        its sessions; signing everybody out for a restore that did not touch them is a cost with
        no security benefit.

        Returns how many ended, by the first reason that applied: gone, password, direct, changed.
        """
        ended = {"gone": 0, "password": 0, "direct": 0, "changed": 0}
        with self._lock:
            for token, session in list(self._sessions.items()):
                account = self._accounts.get(session.user_id)
                was = (before or {}).get(session.user_id)
                if account is None or not account.enabled or not isinstance(was, dict):
                    reason = "gone"
                else:
                    now = self._security_state(account)
                    differs = [f for f in self._SECURITY_FIELDS if now.get(f) != was.get(f)]
                    if not differs:
                        reason = ""
                    elif session.from_direct_link and "direct" in differs:
                        reason = "direct"
                    elif not session.from_direct_link and "password" in differs:
                        reason = "password"
                    else:
                        reason = "changed"
                if reason:
                    self._sessions.pop(token, None)
                    ended[reason] += 1
        return ended

    def resolve_session(self, token) -> tuple:
        """(account, session) for a live session, or (None, None).

        Re-checks the account on every request rather than trusting what was
        true at login: an account disabled or deleted a moment ago must not keep
        working until its session happens to expire.
        """
        if not token:
            return (None, None)
        with self._lock:
            session = self._sessions.get(token)
            if session is None:
                return (None, None)
            if session.expired():
                self._sessions.pop(token, None)
                return (None, None)
            account = self._accounts.get(session.user_id)
            if account is None or not account.enabled:
                self._sessions.pop(token, None)
                return (None, None)
            session.last_seen = _now()
            return (account, session)

    def revoke(self, token) -> None:
        with self._lock:
            self._sessions.pop(token, None)

    def revoke_all_for_user(self, user_id, _locked=False) -> int:
        def _do():
            doomed = [t for t, s in self._sessions.items() if s.user_id == user_id]
            for token in doomed:
                self._sessions.pop(token, None)
            return len(doomed)
        if _locked:
            return _do()
        with self._lock:
            return _do()

    def active_session_count(self) -> int:
        with self._lock:
            live = {t: s for t, s in self._sessions.items() if not s.expired()}
            self._sessions = live
            return len(live)

    def revoke_every_session(self) -> int:
        """Used when remote access is disabled: nothing issued remotely survives."""
        with self._lock:
            count = len(self._sessions)
            self._sessions.clear()
            return count

    # ---- audit -------------------------------------------------------
    def _load_audit(self) -> None:
        """Read the audit back so a restart does not blank the view.

        A damaged file costs the history and nothing else: the application
        starts, and the log begins again rather than failing.
        """
        if self._audit_path is None or not self._audit_path.exists():
            return
        try:
            raw = json.loads(self._audit_path.read_text(encoding="utf-8"))
            entries = (raw or {}).get("entries") or []
            with self._lock:
                # A malformed saved setting is the default, never a crash and
                # never an unbounded log.
                self._audit_retention = luma_retention.from_saved(
                    (raw or {}).get("retention"))
                self._audit = [e for e in entries if isinstance(e, dict)]
                self._audit_sizes = [self._entry_bytes(e) for e in self._audit]
                self._prune_audit_locked()
        except Exception as exc:
            log.warning("[AUDIT] could not read the existing log (%s); "
                        "starting a new one", type(exc).__name__)

    def audit(self, event, actor=None, detail=None) -> None:
        """Record a security-relevant event, with secrets stripped first.

        Named fields are recorded; a request body is never dumped verbatim,
        because a caller can put a credential in one.

        The file is written outside the lock and no more often than the flush
        cadence, so recording an event costs an append rather than a rewrite of
        everything that came before it.
        """
        name = str(event)
        entry = {"at": _now(), "event": name,
                 "actor": getattr(actor, "username", None),
                 "actor_id": getattr(actor, "id", None),
                 "detail": redact(detail or {})}

        snapshot = None
        with self._lock:
            self._audit.append(entry)
            self._audit_sizes.append(self._entry_bytes(entry))
            # Bounded by age and by size: an unbounded security log is its own
            # outage.
            self._prune_audit_locked()
            self._audit_dirty = True
            due = (name in AUDIT_IMMEDIATE_EVENTS
                   or _now() - self._audit_flushed_at >= AUDIT_FLUSH_SECONDS)
            if due and self._audit_path is not None:
                snapshot = self._audit_envelope_locked()
                self._audit_flushed_at = _now()
                self._audit_dirty = False

        # Outside the lock: writing the whole log must not hold up a sign-in,
        # and this is the path an attacker can drive hardest.
        if snapshot is not None:
            _atomic_write_json(self._audit_path, snapshot)

    @staticmethod
    def _entry_bytes(entry) -> int:
        try:
            return luma_retention.entry_size(len(json.dumps(entry, separators=(",", ":"))))
        except (TypeError, ValueError):
            return luma_retention.entry_size(0)

    def _prune_audit_locked(self) -> None:
        """Apply both retention limits. Caller holds the lock."""
        settings = self._audit_retention
        if len(self._audit_sizes) != len(self._audit):
            # Something replaced the list without its sizes; derive them again
            # rather than trusting a parallel list that no longer lines up.
            self._audit_sizes = [self._entry_bytes(e) for e in self._audit]
        self._audit, self._audit_sizes = luma_retention.prune(
            self._audit, self._audit_sizes, _now(),
            settings["days"], settings["max_bytes"])

    def _audit_envelope_locked(self) -> dict:
        return {"version": SCHEMA_VERSION,
                "retention": dict(self._audit_retention),
                "entries": list(self._audit)}

    def audit_retention(self) -> dict:
        """The current limits, what is in use, and the bounds an edit must respect."""
        with self._lock:
            return {**self._audit_retention,
                    "defaults": luma_retention.defaults(),
                    "limits": luma_retention.limits(),
                    "entries": len(self._audit),
                    "used_bytes": sum(self._audit_sizes)}

    def set_audit_retention(self, days=None, max_bytes=None, *, actor=None) -> dict:
        """Change the limits, prune at once, and say so in the log itself.

        Raises `luma_retention.RetentionError` for a value out of bounds; nothing
        is changed in that case. A shorter period or a smaller size removes the
        entries it no longer allows immediately -- the administrator asked for
        exactly that, and waiting for the next event would leave them in place.
        """
        with self._lock:
            settings = luma_retention.validate(days, max_bytes, self._audit_retention)
            before = dict(self._audit_retention)
            self._audit_retention = settings
            self._prune_audit_locked()
            self._audit_dirty = True
        self.audit("audit.retention_changed", actor=actor,
                   detail={"days": settings["days"], "max_bytes": settings["max_bytes"],
                           "previous_days": before["days"],
                           "previous_max_bytes": before["max_bytes"]})
        self.flush_audit()
        return self.audit_retention()

    def flush_audit(self) -> bool:
        """Write anything still held in memory. Called at shutdown."""
        with self._lock:
            if not self._audit_dirty or self._audit_path is None:
                return True
            snapshot = self._audit_envelope_locked()
            self._audit_dirty = False
            self._audit_flushed_at = _now()
        return _atomic_write_json(self._audit_path, snapshot)

    def audit_entries(self, limit=200, event=None, actor=None,
                      search=None) -> list:
        """The most recent entries, newest first, optionally filtered.

        Filtering happens here rather than in the browser so a page never has
        to hold the whole log to show a slice of it.
        """
        needle = str(search or "").strip().lower()
        wanted_event = str(event or "").strip().lower()
        wanted_actor = str(actor or "").strip().lower()
        with self._lock:
            entries = list(self._audit)

        selected = []
        for item in reversed(entries):          # newest first
            if wanted_event and wanted_event not in str(item.get("event", "")).lower():
                continue
            if wanted_actor and wanted_actor != str(item.get("actor") or "").lower():
                continue
            if needle and needle not in json.dumps(item).lower():
                continue
            selected.append(item)
            if len(selected) >= int(limit):
                break
        return selected

    def audit_summary(self) -> dict:
        """Counts an administrator glances at before reading rows."""
        with self._lock:
            entries = list(self._audit)
        counts: dict = {}
        actors = set()
        for item in entries:
            counts[item.get("event", "")] = counts.get(item.get("event", ""), 0) + 1
            if item.get("actor"):
                actors.add(item["actor"])
        return {"total": len(entries), "capacity": AUDIT_MAX_ENTRIES,
                "retention": self.audit_retention(),
                "accounts": sorted(actors),
                "events": sorted(counts)}
