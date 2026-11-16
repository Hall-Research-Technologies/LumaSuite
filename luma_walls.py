"""Saved video walls as something a restricted account may be given.

A wall is saved configuration owned by the administrator (video_walls.json). This
module only reads it: it lists what an account may be shown and works out who the
members are. It never creates or edits a wall, and it holds no device calls.

Members are named by hardware identity. A wall stores each cell's decoder as the
application's own device id (`mac:..`); `members()` turns that into the same canonical
identity every other grant uses, through the units the host actually holds, so a
decoder that changed address is still the same member and one that was cleared is
reported as missing rather than guessed at.
"""
from __future__ import annotations


def _projection():
    import luma_projection
    return luma_projection


def wall_id(wall) -> str:
    return str((wall or {}).get("id") or "").strip().lower()


def cells(wall):
    """The cells of a wall that name a decoder, in grid order."""
    found = [c for c in ((wall or {}).get("cells") or [])
             if isinstance(c, dict) and c.get("decoder_id")]
    return sorted(found, key=lambda c: (int(c.get("row") or 0), int(c.get("column") or 0)))


def members(wall, units, device_id_of):
    """[(cell, identity or '', unit or None)] for every decoder cell of the wall.

    `device_id_of(unit)` is the application's single device-id derivation. A cell
    whose decoder is not in `units` has identity '' and unit None: missing.
    """
    by_device = {}
    for unit in units or []:
        try:
            by_device[device_id_of(unit)] = unit
        except Exception:
            continue
    out = []
    for cell in cells(wall):
        unit = by_device.get(str(cell.get("decoder_id")))
        identity = _projection().unit_identity(unit) if unit is not None else ""
        out.append((cell, identity or "", unit))
    return out


def summary(wall, units, device_id_of) -> dict:
    """Counts a person may be told: how many displays, how many answering."""
    rows = members(wall, units, device_id_of)
    online = sum(1 for _c, _i, unit in rows
                 if unit is not None and _projection().is_online(unit))
    return {"members": len(rows), "online_members": online,
            "degraded": online < len(rows)}


def fingerprint(wall, units, device_id_of):
    """What a revision digest needs to notice: name, shape and membership."""
    rows = members(wall, units, device_id_of)
    return [wall_id(wall), str(wall.get("name") or ""),
            int(wall.get("rows") or 0), int(wall.get("columns") or 0),
            [[int(c.get("row") or 0), int(c.get("column") or 0), identity or str(c.get("decoder_id"))]
             for c, identity, _u in rows],
            str(wall.get("source_id") or "")]


class WallDirectory:
    """Read access to the saved walls, handed to the remote layer.

    `load()` returns the saved wall dicts as stored now (never a copy kept here, so
    a rename or a membership change is visible to the very next request) and
    `device_id_of` is the application's one device-id derivation.
    """

    def __init__(self, load, device_id_of):
        self._load = load
        self.device_id_of = device_id_of

    def all(self):
        try:
            return [w for w in (self._load() or []) if isinstance(w, dict) and wall_id(w)]
        except Exception:
            return []

    def get(self, wid):
        wid = str(wid or "").strip().lower()
        for wall in self.all():
            if wall_id(wall) == wid:
                return wall
        return None
