"""Route classification and the authorization primitives behind remote access.

The point of this module is that **a route with no classification is denied**.

Before remote access existed, every LumaSuite route was anonymous and the only
thing that made that safe was the server being reachable from the local computer.
Enabling remote access removes that control from every route at once, so the gate
cannot be a decorator applied to the routes someone remembered to decorate --
`/api/list_dir` enumerates the host filesystem and `/api/producer/jsonrpc` will
reboot a device or change its password, and neither is a route anyone would think
to protect while writing a routing page.

So the table below is exhaustive and the fence in tests/test_access_fence.py
asserts it stays that way: every rule Flask has registered must appear here, and
anything unclassified is both a test failure and, at runtime, a denial.

Nothing here decides policy on its own. `classify()` answers "who may call this",
and the caller enforces it.
"""
from __future__ import annotations

from enum import Enum


class Access(Enum):
    """Who may call a route. Matched exactly, never by ordering."""

    #: No session required: sign-in, the licence, and the assets a sign-in page needs.
    PUBLIC = "public"
    #: Any authenticated principal, whatever their role.
    AUTHENTICATED = "authenticated"
    #: Authenticated, role=admin. Remote administrators can call these.
    ADMIN = "admin"
    #: Only from the local listener. These act on the host computer rather than on
    #: AV devices -- browsing its filesystem, bootstrapping the first account,
    #: exporting or restoring a backup -- so they are not reachable from the
    #: network even with an Administrator session.
    LOCAL_ONLY = "local_only"
    #: Reachable by a restricted routing User, subject to per-device and per-group
    #: permission checks made by the caller. There is one capability -- routing
    #: audio and video as one operation -- so there is no capability to name.
    USER = "user"


#: Every route the application registers, and who may call it.
ROUTES: dict = {
    # ---- public -----------------------------------------------------
    "/login": Access.PUBLIC,
    "/api/auth/login": Access.PUBLIC,
    "/access/<token>": Access.PUBLIC,
    "/license": Access.PUBLIC,
    "/licenses/<path:name>": Access.PUBLIC,
    "/favicon.ico": Access.PUBLIC,
    "/static/<path:filename>": Access.PUBLIC,
    # `/ui/<file>` is classified per file by `classify_ui_file`; this entry is
    # the fallback and is the restrictive one.
    "/ui/<path:filename>": Access.ADMIN,

    # ---- any signed-in account --------------------------------------
    "/api/auth/logout": Access.AUTHENTICATED,
    "/api/auth/session": Access.AUTHENTICATED,
    "/api/app_version": Access.AUTHENTICATED,
    "/user": Access.AUTHENTICATED,

    # ---- restricted routing users -----------------------------------
    "/api/user/state": Access.USER,
    "/api/user/revision": Access.USER,
    "/api/user/route": Access.USER,
    "/api/user/route_group": Access.USER,
    "/api/user/route_multi": Access.USER,
    "/api/user/unroute": Access.USER,
    "/api/user/route_check": Access.USER,
    "/api/user/matrix": Access.USER,
    "/api/user/preview": Access.USER,

    # ---- local only: act on the host computer -----------------------
    "/api/bootstrap/state": Access.LOCAL_ONLY,
    "/api/bootstrap/admin": Access.LOCAL_ONLY,
    "/api/backup/export": Access.LOCAL_ONLY,
    "/api/backup/inspect": Access.LOCAL_ONLY,
    "/api/backup/restore": Access.LOCAL_ONLY,
    "/api/list_dir": Access.LOCAL_ONLY,
    "/api/open_firmware_folder": Access.LOCAL_ONLY,
    "/api/preview_stats": Access.LOCAL_ONLY,
    "/api/local_port": Access.LOCAL_ONLY,
    "/api/local_port/<action>": Access.LOCAL_ONLY,

    # ---- administrators: the normal LumaSuite interface -------------
    "/": Access.ADMIN,
    "/admin/users": Access.ADMIN,
    "/api/remote_access": Access.ADMIN,
    "/api/admin/users": Access.ADMIN,
    "/api/admin/users/<user_id>": Access.ADMIN,
    "/api/admin/users/<user_id>/direct_access": Access.ADMIN,
    "/api/admin/devices": Access.ADMIN,
    "/api/admin/aliases": Access.ADMIN,
    "/api/admin/groups": Access.ADMIN,
    "/api/admin/walls": Access.ADMIN,
    "/api/admin/groups/<group_id>": Access.ADMIN,
    "/api/admin/audit": Access.ADMIN,
    "/api/admin/audit/retention": Access.ADMIN,
    "/api/admin/certificate": Access.ADMIN,

    "/api/adapters": Access.ADMIN,
    "/api/cache": Access.ADMIN,
    "/api/heartbeat": Access.ADMIN,
    "/api/clear_units": Access.ADMIN,
    "/api/clear_units/assignments": Access.ADMIN,
    "/api/remove_units": Access.ADMIN,
    "/api/export_csv": Access.ADMIN,
    "/api/config": Access.ADMIN,
    "/api/scan": Access.ADMIN,
    "/api/thumbnail": Access.ADMIN,
    "/api/tcp_probe": Access.ADMIN,
    "/api/ping": Access.ADMIN,
    "/api/version": Access.ADMIN,
    "/api/hostname": Access.ADMIN,
    "/api/get_streamname": Access.ADMIN,
    "/api/set_hostname": Access.ADMIN,
    "/api/set_streamname": Access.ADMIN,
    "/api/set_dante_power": Access.ADMIN,
    "/api/set_hdcp": Access.ADMIN,
    "/api/ntp_profile": Access.ADMIN,
    "/api/set_ntp": Access.ADMIN,
    "/api/reset": Access.ADMIN,
    "/api/route_matrix": Access.ADMIN,
    "/api/refresh_matrix_state": Access.ADMIN,
    "/api/firmware_check": Access.ADMIN,
    # Firmware: names a file in the configured folder. The handlers refuse an
    # absolute path, a parent reference and a credential override from a remote
    # caller, and never return a host path to one.
    "/api/files": Access.ADMIN,
    "/api/upgrade": Access.ADMIN,
    "/api/firmware_status": Access.ADMIN,
    "/api/firmware_status/check": Access.ADMIN,
    "/api/update_check": Access.ADMIN,
    "/api/switcher_tune": Access.ADMIN,

    "/api/video_wall/state": Access.ADMIN,
    "/api/video_wall/save": Access.ADMIN,
    "/api/video_wall/delete": Access.ADMIN,
    "/api/video_wall/import": Access.ADMIN,
    "/api/video_wall/detect": Access.ADMIN,
    "/api/video_wall/candidates_summary": Access.ADMIN,
    "/api/video_wall/candidates/ignore": Access.ADMIN,
    "/api/video_wall/refresh": Access.ADMIN,
    "/api/video_wall/source": Access.ADMIN,
    "/api/video_wall/bezel": Access.ADMIN,
    "/api/video_wall/rotation": Access.ADMIN,
    "/api/video_wall/apply": Access.ADMIN,
    "/api/video_wall/decoder_mode": Access.ADMIN,

    "/api/producer/jsonrpc": Access.ADMIN,
    "/api/producer/change_password": Access.ADMIN,
    "/api/producer/test_device": Access.ADMIN,
    "/api/slate_upload": Access.ADMIN,
    "/api/slate_default": Access.ADMIN,
    "/api/producer2/state": Access.ADMIN,
    "/api/producer2/assets": Access.ADMIN,
    "/api/producer2/push/image": Access.ADMIN,
    "/api/producer2/push/logo": Access.ADMIN,
    "/api/producer2/asset/<kind>/<path:filename>": Access.ADMIN,
}

#: Methods whose access differs from the rule's default. A rule that needs an
#: entry here is usually a rule that should have been two. An override may only
#: narrow what the table says, never widen it (asserted in the tests).
METHOD_ROUTES: dict = {}

#: Files under `ui/` an unauthenticated browser must be able to load: what the
#: sign-in page and the restricted routing page are built from. Nothing here
#: carries device data or a control that acts on a device; the page that uses
#: them is gated, and so is every API they call.
PUBLIC_UI_FILES = frozenset({
    "family.css", "theme-boot.js", "dialogs.js", "csrf.js", "remote.css",
    "login.js", "user.js", "remember.js", "favicon.ico",
})
#: Name prefixes of public image assets (the product logo, favicons).
PUBLIC_UI_PREFIXES = ("companylogo", "favicon")
PUBLIC_UI_IMAGE_SUFFIXES = (".png", ".ico", ".svg")


def classify_ui_file(filename: str) -> Access:
    """Who may fetch one file under `/ui/`.

    Allow-listed, never deny-listed: a file added to `ui/` tomorrow is an
    administrator's file until someone decides otherwise. Anything with a path
    separator or a parent reference in it is administrative by definition.
    """
    name = str(filename or "")
    if (not name or "/" in name or "\\" in name or ".." in name
            or "\x00" in name):
        return Access.ADMIN
    if name in PUBLIC_UI_FILES:
        return Access.PUBLIC
    low = name.lower()
    if low.startswith(PUBLIC_UI_PREFIXES) and low.endswith(PUBLIC_UI_IMAGE_SUFFIXES):
        return Access.PUBLIC
    return Access.ADMIN


def classify(rule, method="GET", path_args=None) -> Access:
    """Who may call `rule` with `method`. An unknown rule is LOCAL_ONLY.

    LOCAL_ONLY rather than ADMIN because it is the safest answer the table has:
    a route nobody has classified is unreachable from the network until somebody
    decides, which is the default-deny the module exists to provide.
    """
    key = str(rule or "")
    method = str(method or "GET").upper()
    if key == "/ui/<path:filename>" and path_args:
        return classify_ui_file(path_args.get("filename"))
    override = METHOD_ROUTES.get((method, key))
    if override is not None:
        return override
    return ROUTES.get(key, Access.LOCAL_ONLY)
