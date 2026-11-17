"""Activity text: what happened, in words an administrator can read.

The audit log records machine events (`user.permissions_changed`) and machine
reasons (`group_not_permitted`). Neither belongs in front of a person. Every
event and every reason the application can emit has a sentence here, and anything
that does not -- an event added tomorrow -- falls back to English derived from its
own name, never to the raw id.

The test suite derives the set of events the source can emit (including any built
by concatenation) and fails when one has no label, so the map cannot quietly fall
behind the code.
"""
from __future__ import annotations

EVENT_LABELS = {
    "login.success": "Signed in",
    "login.failure": "Sign-in failed",
    "login.throttled": "Sign-in blocked after repeated failures",
    "logout": "Signed out",
    "bootstrap.admin_created": "First administrator created",
    "user.create": "Account created",
    "user.enabled": "Account enabled",
    "user.disabled": "Account disabled",
    "user.password_changed": "Password changed",
    "user.role_changed": "Account type changed",
    "user.permissions_changed": "Access changed",
    "user.deleted": "Account deleted",
    "user.standard_routing_enabled": "Standard routing turned on",
    "user.standard_routing_disabled": "Standard routing turned off",
    "user.matrix_routing_enabled": "Matrix routing turned on",
    "user.matrix_routing_disabled": "Matrix routing turned off",
    "direct_access.enabled": "Direct access link created",
    "direct_access.regenerated": "Direct access link replaced",
    "direct_access.disabled": "Direct access link turned off",
    "direct_access.used": "Direct access link opened",
    "remote_access.enabled": "Remote access turned on",
    "remote_access.disabled": "Remote access turned off",
    "remote_access.restored": "Remote access restored at start-up",
    "certificate.generated": "Certificate created",
    "certificate.configured": "Certificate chosen",
    "certificate.removed": "Certificate removed",
    "access.denied": "Request refused",
    "route": "Routed a source to a display",
    "route_group": "Routed a source to a display group",
    "route_multi": "Routed a source to several displays",
    "unroute": "Unrouted displays",
    "route_matrix": "Routed from the matrix",
    "route.refused_offline": "A route was refused because a device is offline",
    "device.aliases_changed": "Device names changed",
    "group.created": "Display group created",
    "group.updated": "Display group changed",
    "group.deleted": "Display group deleted",
    "units.cleared": "Known devices cleared",
    "units.cleared_unassigned": "Unassigned devices cleared",
    "units.removed_unassigned": "Selected unassigned devices removed",
    "backup.exported": "Backup exported",
    "backup.restored": "Backup restored",
    "audit.retention_changed": "Activity retention changed",
}

#: Why a request was refused, as a sentence about the person's situation.
REASON_LABELS = {
    "no_session": "they were not signed in",
    "csrf": "the page had been open too long",
    "not_admin": "that area is for administrators",
    "direct_link_not_admin": "a direct access link cannot reach the administrative area",
    "local_only": "that action works only on the computer running LumaSuite",
    "source_not_permitted": "they do not have access to that source",
    "destination_not_permitted": "they do not have access to that display",
    "group_not_permitted": "they do not have access to that display group",
    "wall_not_permitted": "they do not have access to that video wall",
    "interface_not_permitted": "their account is not set up to route that way",
    "device_offline": "that device is offline",
    "not_permitted": "it is not permitted",
}

#: How a route attempt ended.
RESULT_LABELS = {
    "verified": "confirmed on the display",
    "not_verified": "sent, but the display did not confirm it",
    "unreachable": "the device did not answer",
    "failed": "it did not work",
    "not_found": "the device was not found",
    "busy": "the display was busy",
    "refused": "it was refused",
    "offline": "the device is offline",
    "partial": "only some of its displays changed",
}


def humanise(identifier) -> str:
    """English from an internal id: `some.thing_changed` -> `Some thing changed`."""
    text = " ".join(str(identifier or "").replace(".", " ").replace("_", " ")
                    .replace(":", " ").split())
    return (text[:1].upper() + text[1:]) if text else "Something happened"


def event_label(event) -> str:
    return EVENT_LABELS.get(str(event or ""), humanise(event))


def reason_label(reason) -> str:
    text = str(reason or "")
    if text in REASON_LABELS:
        return REASON_LABELS[text]
    # An unknown reason is explained as English, never shown as the id.
    return humanise(text).lower() if text else ""


def _who(entry) -> str:
    return str(entry.get("actor") or "").strip()


def describe(entry, namer=None) -> str:
    """One sentence for one audit entry. `namer(identity)` turns an id into a name."""
    event = str(entry.get("event") or "")
    detail = entry.get("detail") if isinstance(entry.get("detail"), dict) else {}
    namer = namer or (lambda identity: "a device")
    subject = str(detail.get("username") or "").strip()

    if event == "access.denied":
        why = reason_label(detail.get("reason"))
        return "A request was refused" + (f" because {why}." if why else ".")
    if event == "route":
        return (f"Routed {namer(detail.get('source'))} to "
                f"{namer(detail.get('destination'))}"
                + _result_suffix(detail))
    if event == "route_group":
        results = detail.get("results") if isinstance(detail.get("results"), list) else []
        routed = detail.get("routed")
        total = detail.get("total")
        head = (f"Routed {namer(detail.get('source'))} to the group "
                f"{str(detail.get('group_name') or 'a display group')}")
        if isinstance(routed, int) and isinstance(total, int):
            head += f": {routed} of {total} displays routed"
        failed = [r for r in results if isinstance(r, dict) and not r.get("ok")]
        if failed:
            head += " (" + "; ".join(
                f"{namer(r.get('display'))}: "
                f"{RESULT_LABELS.get(str(r.get('result')), reason_label(r.get('result')))}"
                for r in failed[:6]) + ")"
        return head + "."
    if event in ("route_multi", "route_matrix"):
        results = detail.get("results") if isinstance(detail.get("results"), list) else []
        routed = detail.get("routed")
        total = detail.get("total")
        walled = bool(detail.get("walls"))
        head = (f"Routed {namer(detail.get('source'))} to several "
                + ("destinations" if walled else "displays"))
        if event == "route_matrix":
            word = {"video": "video only", "audio": "audio only"}.get(
                str(detail.get("mode")), "audio and video")
            head = (f"Matrix: routed {word} from {namer(detail.get('source'))} to "
                    + ("destinations" if walled else "displays"))
        if isinstance(routed, int) and isinstance(total, int):
            head += f": {routed} of {total} " + ("destinations" if walled else "displays") + " routed"
        failed = [r for r in results if isinstance(r, dict) and not r.get("ok")]
        if failed:
            head += " (" + "; ".join(
                f"{(namer(r.get('display')) if r.get('display') else str(r.get('name') or 'a video wall'))}: "
                f"{RESULT_LABELS.get(str(r.get('result')), reason_label(r.get('result')))}"
                for r in failed[:6]) + ")"
        return head + "."
    if event == "user.create":
        kind = "an administrator" if detail.get("role") == "admin" else "a user"
        return f"Created the account {subject} as {kind}." if subject else "Account created."
    if event.startswith("user.") and subject:
        return f"{event_label(event)}: {subject}."
    if event.startswith("direct_access.") and subject:
        return f"{event_label(event)} for {subject}."
    if event == "device.aliases_changed":
        count = detail.get("count")
        return (f"Changed the names of {count} device{'s' if count != 1 else ''}."
                if isinstance(count, int) else "Changed device names.")
    if event in ("group.created", "group.updated", "group.deleted"):
        name = str(detail.get("name") or "").strip()
        return f"{event_label(event)}: {name}." if name else event_label(event) + "."
    if event == "audit.retention_changed":
        days, size = detail.get("days"), detail.get("max_bytes")
        if isinstance(days, int) and isinstance(size, int):
            unit = (f"{size // (1024 * 1024)} MB" if size >= 1024 * 1024
                    else f"{size // 1024} KB")
            return (f"Activity is now kept for {days} day{'s' if days != 1 else ''}, "
                    f"up to {unit}.")
        return "Activity retention changed."
    if event == "units.cleared":
        kept = detail.get("retained")
        if isinstance(kept, int) and kept:
            return f"Cleared the known devices, keeping {kept} that people were given."
        return "Cleared the known devices."
    if event == "units.cleared_unassigned":
        cleared, kept = detail.get("cleared"), detail.get("kept")
        if isinstance(cleared, int) and isinstance(kept, int):
            return (f"Cleared {cleared} unassigned device{'s' if cleared != 1 else ''}; "
                    f"{kept} assigned device{'s' if kept != 1 else ''} kept.")
        return "Cleared the unassigned devices."
    if event == "units.removed_unassigned":
        removed, protected = detail.get("removed"), detail.get("protected")
        if isinstance(removed, int) and isinstance(protected, int):
            return (f"Removed {removed} selected device{'s' if removed != 1 else ''}; "
                    f"{protected} assigned device{'s' if protected != 1 else ''} kept.")
        return "Removed the selected unassigned devices."
    return event_label(event) + "."


def _result_suffix(detail) -> str:
    result = str(detail.get("result") or "")
    if not result:
        return ""
    return ", " + RESULT_LABELS.get(result, reason_label(result))
