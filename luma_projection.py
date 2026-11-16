"""What a restricted routing User is allowed to see.

A projection built from *named fields*, never a filter over the administrator's
device objects. A field nobody added here cannot appear in a response, so a new
key put on a cached device record tomorrow does not become visible to a User by
accident. The tests assert the exact key sets, not merely that secrets are
absent.

Nothing in a projection carries: a device address, a hostname that was not
deliberately chosen as a name, a serial number, a model, firmware, a stream name,
a password, or any internal configuration. A source or display a User has not
been granted does not appear at all, and is never named -- not even as
"another source".

No device I/O happens here. Everything is computed from state the host already
holds, so serving a projection costs nothing however many browsers ask.

A display group is its own resource. The projection says how many displays it
has and what they are showing, and never lists its members: holding a group does
not make its members individually routable, so they are not described to the
User as individual displays.
"""
from __future__ import annotations

import luma_liveness
import luma_mac
import luma_walls

#: The exact keys of each projected object. Tests compare equality, so a key
#: added to a projection without updating this table fails.
SOURCE_FIELDS = frozenset({"id", "name", "online"})
DESTINATION_FIELDS = frozenset({"id", "name", "online", "showing"})
GROUP_FIELDS = frozenset({"id", "name", "members", "online_members", "degraded",
                          "showing"})
#: A saved video wall as a User sees it: its name and how many displays answer,
#: never which decoders they are.
WALL_FIELDS = GROUP_FIELDS
SHOWING_FIELDS = frozenset({"mode", "source_id", "source_name"})
STATE_FIELDS = frozenset({"ok", "rev", "role", "username", "sources", "destinations",
                          "groups", "walls", "routing"})
SESSION_FIELDS = frozenset({"ok", "role", "username"})

#: `mode` values of what a display (or group) is showing. Plain words in the
#: JSON; the browser turns each into a human phrase.
MODE_NONE = "none"            # nothing is routed
MODE_SOURCE = "source"        # a source this account may use
MODE_OTHER = "other"          # something, but not a source this account may use
MODE_MIXED = "mixed"          # group members disagree
MODE_UNAVAILABLE = "unavailable"   # group has no member that can be read


def _kind(unit) -> str:
    """'source', 'display' or '' for a cached unit."""
    kind = str(unit.get("type") or unit.get("role") or "").strip().lower()
    if "encoder" in kind:
        return "source"
    if "decoder" in kind or "switcher" in kind:
        return "display"
    return ""


def unit_kind(unit) -> str:
    return _kind(unit)


def unit_identity(unit) -> str:
    """The stable identity of a cached unit: its MAC, or '' if it has none.

    A unit without a hardware identifier has no identity and is therefore never
    assignable, nameable or routable by a User -- falling back to its address
    would let a permission follow whatever answers there next.
    """
    return _mac_only(unit.get("mac"))


def _mac_only(value) -> str:
    """A canonical MAC, or '' -- an address is rejected, not converted.

    `_normalise_identity` folds separators, so it will happily turn some inputs
    into a plausible identifier; an IP address must never become one.
    """
    return luma_mac.normalise_mac(value)


def canonical_identity(value) -> str:
    """Public name for the strict MAC canonicaliser."""
    return _mac_only(value)


def is_online(unit) -> bool:
    """A unit is online when its last successful read said so and it is still answering.

    The one derivation every surface uses. `luma_liveness` records, from the shared
    matrix refresh, how many refreshes in a row went unanswered; a unit past its
    limit is offline whatever the scan that found it once said.
    """
    if luma_liveness.is_offline(unit):
        return False
    return str(unit.get("status") or "").strip().lower() == "idle"


def is_offline(unit) -> bool:
    """True only when the unit is *determined* to be offline.

    The administrator's operations (the matrix, the Video Wall page) are refused for a unit
    that has stopped answering, but never for one whose state is merely unrecorded: a unit
    with no status at all (an old cache) is not called offline here. `is_online` is the strict
    reading the User page presents; this is the reading a refusal may rest on.
    """
    if luma_liveness.is_offline(unit):
        return True
    status = str(unit.get("status") or "").strip().lower()
    return bool(status) and status != "idle"


def device_location(unit) -> str:
    return _systeminfo(unit, "location")


def device_description(unit) -> str:
    return _systeminfo(unit, "description")


def _systeminfo(unit, key) -> str:
    """Location/description from the device's own systeminfo, when the cache has it."""
    for holder in (unit, (unit.get("details") or {})):
        info = holder.get("systeminfo") if isinstance(holder, dict) else None
        if isinstance(info, dict):
            config = info.get("config") if isinstance(info.get("config"), dict) else info
            value = config.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _device_name(unit, identity) -> str:
    """The device's own name, as a last resort: hostname, then model."""
    for key in ("hostname", "name", "model"):
        value = unit.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "Unnamed device"


def _friendly_names(units_by_identity, aliases) -> dict:
    """identity -> display label, disambiguated only where a label repeats.

    Within the set being shown, so a person who can see one "Front Display" sees
    it named exactly as typed. A repeat is told apart by the device's location,
    then its description, then its own hostname -- and finally by an ordinal,
    because a list of two identical labels is a list nobody can use. An address or
    a MAC is never used.
    """
    base = {}
    for identity, unit in units_by_identity.items():
        base[identity] = aliases.display_name(identity, _device_name(unit, identity))
    groups: dict = {}
    for identity, label in base.items():
        groups.setdefault(label.lower(), []).append(identity)
    result = dict(base)
    for members in groups.values():
        if len(members) < 2:
            continue
        for identity in members:
            unit = units_by_identity[identity]
            extra = (device_location(unit) or device_description(unit)
                     or str(unit.get("hostname") or "").strip())
            if extra and extra.lower() != base[identity].lower():
                result[identity] = f"{base[identity]} ({extra})"
        # Still identical after that? Number them, in identity order so the
        # numbering is stable from one request to the next.
        seen: dict = {}
        for identity in sorted(members):
            seen.setdefault(result[identity].lower(), []).append(identity)
        for same in seen.values():
            if len(same) > 1:
                for n, identity in enumerate(same, 1):
                    result[identity] = f"{result[identity]} ({n})"
    return result


def _stream_of_display(unit) -> str:
    """The stream a display is selecting, from what the host last read."""
    dec = unit.get("dec") if isinstance(unit.get("dec"), dict) else {}
    video = dec.get("video") if isinstance(dec.get("video"), dict) else {}
    name = video.get("streamname")
    if not (isinstance(name, str) and name.strip()):
        name = unit.get("routed_streamname")
    return name.strip() if isinstance(name, str) else ""


def _stream_of_source(unit) -> str:
    enc = unit.get("enc") if isinstance(unit.get("enc"), dict) else {}
    video = enc.get("video") if isinstance(enc.get("video"), dict) else {}
    name = video.get("streamname")
    return name.strip() if isinstance(name, str) else ""


def _source_by_stream(units_by_identity) -> dict:
    """streamname -> identity, only where exactly one encoder claims the name.

    Two encoders with one stream name are ambiguous, and an ambiguous match is
    reported as "something else" rather than guessed.
    """
    seen: dict = {}
    for identity, unit in units_by_identity.items():
        if _kind(unit) != "source":
            continue
        name = _stream_of_source(unit)
        if name:
            seen.setdefault(name, []).append(identity)
    return {name: ids[0] for name, ids in seen.items() if len(ids) == 1}


def _showing_token(unit, by_stream, may_use):
    """What one display is showing, as (mode, source_identity)."""
    name = _stream_of_display(unit)
    if not name:
        return (MODE_NONE, "")
    source = by_stream.get(name)
    if source and may_use(source):
        return (MODE_SOURCE, source)
    return (MODE_OTHER, "")


def _showing_json(token, names) -> dict:
    mode, source = token
    return {"mode": mode,
            "source_id": source if mode == MODE_SOURCE else "",
            "source_name": names.get(source, "") if mode == MODE_SOURCE else ""}


def user_state(account, units, aliases, groups, walls=None) -> dict:
    """Everything one account may see, and nothing else.

    `account` is None for the local operator and an Account otherwise; an
    administrator sees all equipment, a User only what they were granted.
    """
    index = {}
    for unit in units or []:
        identity = unit_identity(unit)
        if identity and _kind(unit):
            index[identity] = unit

    admin = account is None or account.is_admin
    perms = None if admin else account.permissions

    def may_use(identity):
        return True if admin else perms.may_use_source(identity)

    def may_drive(identity):
        return True if admin else perms.may_control_destination(identity)

    source_units = {i: u for i, u in index.items()
                    if _kind(u) == "source" and may_use(i)}
    display_units = {i: u for i, u in index.items()
                     if _kind(u) == "display" and may_drive(i)}
    source_names = _friendly_names(source_units, aliases)
    display_names = _friendly_names(display_units, aliases)
    by_stream = _source_by_stream(index)

    sources = [{"id": i, "name": source_names[i], "online": is_online(u)}
               for i, u in source_units.items()]
    destinations = [{"id": i, "name": display_names[i], "online": is_online(u),
                     "showing": _showing_json(
                         _showing_token(u, by_stream, may_use), source_names)}
                    for i, u in display_units.items()]

    def aggregate(identities):
        """(total, online, showing) across a set of member displays."""
        online_tokens = []
        online_count = 0
        for member in identities:
            unit = index.get(member)
            if unit is not None and is_online(unit):
                online_count += 1
                online_tokens.append(_showing_token(unit, by_stream, may_use))
        if not online_tokens:
            showing = {"mode": MODE_UNAVAILABLE, "source_id": "", "source_name": ""}
        else:
            distinct = set(online_tokens)
            if len(distinct) > 1:
                showing = {"mode": MODE_MIXED, "source_id": "", "source_name": ""}
            else:
                only = next(iter(distinct))
                showing = _showing_json(only, source_names)
        return len(identities), online_count, showing

    group_rows = []
    for group in groups.all():
        if not admin and not perms.may_control_group(group["id"]):
            continue
        total, online_count, showing = aggregate(group["members"])
        group_rows.append({"id": group["id"], "name": group["name"],
                           "members": total, "online_members": online_count,
                           "degraded": online_count < total, "showing": showing})

    # A saved wall: its members are the decoders the wall itself names, counted
    # from the saved definition as it is now. A decoder the wall names that is no
    # longer in the inventory still counts as a member that is not answering.
    wall_rows = []
    if walls is not None:
        for wall in walls.all():
            wid = luma_walls.wall_id(wall)
            if not admin and not perms.may_control_wall(wid):
                continue
            rows = luma_walls.members(wall, units, walls.device_id_of)
            identities = [identity for _c, identity, _u in rows if identity]
            _total, online_count, showing = aggregate(identities)
            total = len(rows)
            wall_rows.append({"id": wid, "name": str(wall.get("name") or "Video Wall"),
                              "members": total, "online_members": online_count,
                              "degraded": online_count < total, "showing": showing})

    sources.sort(key=lambda r: (r["name"].lower(), r["id"]))
    destinations.sort(key=lambda r: (r["name"].lower(), r["id"]))
    group_rows.sort(key=lambda r: (r["name"].lower(), r["id"]))
    wall_rows.sort(key=lambda r: (r["name"].lower(), r["id"]))
    return {"ok": True,
            "role": "admin" if admin else "user",
            "username": None if account is None else account.username,
            "sources": sources, "destinations": destinations,
            "groups": group_rows, "walls": wall_rows}


def session_summary(account) -> dict:
    """Who is signed in. No permissions, no identifiers beyond the name."""
    return {"ok": True, "role": "admin" if account.is_admin else "user",
            "username": account.username}

