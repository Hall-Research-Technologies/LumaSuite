"""The restricted User's routing matrix, built only from what the account may use.

The matrix is a projection of one account's grants, never the administrator's
matrix with rows hidden: a source or destination the account holds no grant for is
not looked at, so it cannot leak through a header, a count or a cell. It is built
from the same host-held unit records and the same identity/online derivations the
Standard routing page uses, so the two pages cannot disagree about a unit.

    columns   the sources this account may use
    rows      one per destination: an individual display, a display group, or a
              saved video wall -- each a distinct type, each granted separately.
              A group or wall is one row; its members are never expanded into
              rows of their own (holding a group grants the group, not its
              members).

A cell says what the destination's decoders report *now*: video and audio are
separate facts, as they are in the application's own matrix (ring = video, dot =
audio), so a breakaway shows as two different columns lit in one row. For a group
or a wall a cell is "all" only when every member holds that source; some members is
"partial", none is "none". The row says whether its members agree: "all", "partial"
(they agree, but some are not answering), "mixed" (they differ) or "unavailable"
(none answers). Nothing here invents one state to stand for members that differ.

Pure functions of their inputs: no device is read here.
"""
from __future__ import annotations

import luma_projection
import luma_walls

MODE_AV = "av"
MODE_VIDEO = "video"
MODE_AUDIO = "audio"

#: What a row can be asked to do. A saved wall is an aggregate of its member decoders and is
#: routed member by member in whichever mode is chosen (audio and video, video only with each
#: member's audio kept, or audio only with each member's video kept).
MODES_FOR_ROW = {"display": (MODE_AV, MODE_VIDEO, MODE_AUDIO),
                 "group": (MODE_AV, MODE_VIDEO, MODE_AUDIO),
                 "wall": (MODE_AV, MODE_VIDEO, MODE_AUDIO)}


def _audio_stream_of_display(unit) -> str:
    dec = unit.get("dec") if isinstance(unit.get("dec"), dict) else {}
    audio = dec.get("audio") if isinstance(dec.get("audio"), dict) else {}
    name = audio.get("streamname")
    return name.strip() if isinstance(name, str) else ""


def _audio_stream_of_source(unit) -> str:
    enc = unit.get("enc") if isinstance(unit.get("enc"), dict) else {}
    audio = enc.get("audio") if isinstance(enc.get("audio"), dict) else {}
    name = audio.get("streamname")
    return name.strip() if isinstance(name, str) else ""


def _audio_source_by_stream(index) -> dict:
    """audio streamname -> identity, only where exactly one encoder claims it."""
    seen: dict = {}
    for identity, unit in index.items():
        if luma_projection.unit_kind(unit) != "source":
            continue
        name = _audio_stream_of_source(unit)
        if name:
            seen.setdefault(name, []).append(identity)
    return {name: ids[0] for name, ids in seen.items() if len(ids) == 1}


#: What a display is holding that the account may not name. It is not a column, so no
#: cell ever lights for it, but it is not "nothing" either: a group half on a stranger's
#: source and half unrouted is mixed, and two displays on two strangers' sources are the
#: same answer ("something else") because the account is not told which.
OTHER = "*"


def _visible(source, may_use) -> str:
    if not source:
        return ""
    return source if may_use(source) else OTHER


def _cell(members, total, video_of, audio_of, source_id) -> dict:
    """One cell. `members` are the destinations that are answering; `total` is how
    many the row has. A member that is not answering holds nothing we can claim, so
    it can make a cell "partial" but never "all"."""
    video_n = sum(1 for m in members if video_of.get(m) == source_id)
    audio_n = sum(1 for m in members if audio_of.get(m) == source_id)

    def word(n):
        return "all" if total and n == total else ("partial" if n else "none")
    return {"video": word(video_n), "audio": word(audio_n),
            "video_n": video_n, "audio_n": audio_n}


def user_matrix(account, units, aliases, groups, walls=None) -> dict:
    """The matrix for one account (None = the local operator, who may use everything)."""
    index = {}
    for unit in units or []:
        identity = luma_projection.unit_identity(unit)
        if identity and luma_projection.unit_kind(unit):
            index[identity] = unit

    admin = account is None or account.is_admin
    perms = None if admin else account.permissions
    interfaces = ({"standard": True, "matrix": True} if admin else perms.interfaces())

    def may_use(identity):
        return True if admin else perms.may_use_source(identity)

    def may_drive(identity):
        return True if admin else perms.may_control_destination(identity)

    source_units = {i: u for i, u in index.items()
                    if luma_projection.unit_kind(u) == "source" and may_use(i)}
    display_units = {i: u for i, u in index.items()
                     if luma_projection.unit_kind(u) == "display" and may_drive(i)}
    source_names = luma_projection._friendly_names(source_units, aliases)
    display_names = luma_projection._friendly_names(display_units, aliases)

    # What each display holds, resolved against *every* encoder (so a stream that two
    # encoders both claim stays ambiguous) and then reduced to the sources this
    # account may name. A source it may not use is "something else", never a name.
    video_by_stream = luma_projection._source_by_stream(index)
    audio_by_stream = _audio_source_by_stream(index)
    video_of, audio_of = {}, {}
    for identity, unit in index.items():
        if luma_projection.unit_kind(unit) != "display":
            continue
        video_of[identity] = _visible(video_by_stream.get(
            luma_projection._stream_of_display(unit), ""), may_use)
        audio_of[identity] = _visible(audio_by_stream.get(
            _audio_stream_of_display(unit), ""), may_use)

    source_ids = sorted(source_units, key=lambda i: (source_names[i].lower(), i))
    columns = [{"id": i, "name": source_names[i],
                "online": luma_projection.is_online(source_units[i])} for i in source_ids]

    def aggregate_row(kind, rid, name, member_ids, member_total):
        online_members = [m for m in member_ids
                          if m in index and luma_projection.is_online(index[m])]
        tokens = {(video_of.get(m, ""), audio_of.get(m, "")) for m in online_members}
        if not online_members:
            state = "unavailable"
        elif len(tokens) > 1:
            state = "mixed"
        elif len(online_members) < member_total:
            state = "partial"
        else:
            state = "all"
        return {"kind": kind, "id": rid, "name": name,
                "members": member_total, "online_members": len(online_members),
                "degraded": len(online_members) < member_total, "state": state,
                "modes": list(MODES_FOR_ROW[kind]),
                "cells": {s: _cell(online_members, member_total, video_of, audio_of, s)
                          for s in source_ids}}

    rows = []
    if walls is not None:
        for wall in walls.all():
            wid = luma_walls.wall_id(wall)
            if not admin and not perms.may_control_wall(wid):
                continue
            wall_rows = luma_walls.members(wall, units, walls.device_id_of)
            identities = [i for _c, i, _u in wall_rows if i]
            rows.append(aggregate_row("wall", wid, str(wall.get("name") or "Video Wall"),
                                      identities, len(wall_rows)))
    for group in groups.all():
        if not admin and not perms.may_control_group(group["id"]):
            continue
        members = list(group["members"])
        rows.append(aggregate_row("group", group["id"], group["name"], members, len(members)))
    for identity in sorted(display_units, key=lambda i: (display_names[i].lower(), i)):
        online = luma_projection.is_online(display_units[identity])
        rows.append({"kind": "display", "id": identity, "name": display_names[identity],
                     "members": 1, "online_members": 1 if online else 0,
                     "degraded": False, "state": "all" if online else "unavailable",
                     "modes": list(MODES_FOR_ROW["display"]),
                     "cells": {s: _cell([identity] if online else [], 1, video_of, audio_of, s)
                               for s in source_ids}})

    order = {"wall": 0, "group": 1, "display": 2}
    rows.sort(key=lambda r: (order[r["kind"]], r["name"].lower(), r["id"]))
    return {"ok": True, "role": "admin" if admin else "user",
            "interfaces": interfaces, "sources": columns, "rows": rows}
