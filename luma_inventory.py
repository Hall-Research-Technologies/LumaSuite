"""Which discovered units Remote Access depends on, derived once.

Clear Units while Remote Access is on removes only the units nothing refers to. What
"refers to" means is decided here and nowhere else, and every surface that shows it
(the Clear Units confirmation, View Assignments, the Device names page) consumes the
object this builds, so they cannot disagree.

A unit is assigned when its stable hardware identity appears in:

  * an account's source or display grants (Direct Access shares the account's grants:
    the link names the account, so a unit reachable by link is a unit the account holds),
  * a Display Group's members,
  * a saved Video Wall: its decoder cells, and its source.

Identity is the canonical MAC. An address, a hostname or a friendly name never decides
it, so a unit that moved address is the same unit. A unit with no MAC has no identity,
can be assigned to nothing, and is therefore clearable.
"""
from __future__ import annotations

import luma_projection
import luma_walls

#: Exact keys of one device's assignment record. Tests compare equality.
ASSIGNMENT_FIELDS = frozenset({"users", "direct_access", "groups", "walls", "wall_reasons",
                               "wall_access", "count"})


def _empty():
    return {"users": set(), "direct_access": set(), "groups": set(), "walls": set(),
            "wall_reasons": set(), "wall_access": set()}


def build(units, store, groups, walls) -> dict:
    """identity -> {users, direct_access, groups, walls, count}, only for assigned identities.

    Reads the three stores as they are now. The caller decides whether to hold their
    locks across the read and whatever it does next.
    """
    found: dict = {}
    for identity, names in (store.assigned_devices() or {}).items():
        found.setdefault(identity, _empty())["users"].update(names)
    direct = set()
    try:
        direct = store.direct_access_usernames()
    except Exception:
        direct = set()
    for record in found.values():
        record["direct_access"] = record["users"] & direct
    for name, members in (groups.referenced_identities() or {}).items():
        # referenced_identities is identity -> [group names]
        found.setdefault(name, _empty())["groups"].update(members)
    if walls is not None:
        unit_list = list(units or [])
        by_device = {}
        for unit in unit_list:
            try:
                by_device[walls.device_id_of(unit)] = unit
            except Exception:
                continue
        for wall in walls.all():
            wid = luma_walls.wall_id(wall)
            label = str(wall.get("name") or wid)
            # Who holds this wall directly. A wall grant is not a grant of its members,
            # so these names say who reaches the wall, not who owns the device.
            try:
                holders = set(store.accounts_with_wall(wid) or [])
            except Exception:
                holders = set()
            for _cell, identity, _unit in luma_walls.members(wall, unit_list, walls.device_id_of):
                if identity:
                    record = found.setdefault(identity, _empty())
                    record["walls"].add(label)
                    record["wall_reasons"].add("Member of wall " + label)
                    record["wall_access"].update(holders)
            source = by_device.get(str(wall.get("source_id") or ""))
            if source is not None:
                identity = luma_projection.unit_identity(source)
                if identity:
                    record = found.setdefault(identity, _empty())
                    record["walls"].add(label)
                    record["wall_reasons"].add("Source of wall " + label)
                    record["wall_access"].update(holders)
    out = {}
    for identity, record in found.items():
        out[identity] = {
            "users": sorted(record["users"]),
            "direct_access": sorted(record["direct_access"]),
            "groups": sorted(record["groups"]),
            "walls": sorted(record["walls"]),
            "wall_reasons": sorted(record["wall_reasons"]),
            "wall_access": sorted(record["wall_access"]),
            "count": len(record["users"]) + len(record["groups"]) + len(record["walls"]),
        }
    return out


def split(cache_units: dict, assigned: dict):
    """(kept_ips, doomed_ips): which cache rows Clear Units may remove.

    A row is kept when its identity is assigned. Rows without an identity are never
    assigned. Two rows sharing one identity are both kept, because the identity is.
    """
    kept, doomed = [], []
    for ip, unit in cache_units.items():
        identity = luma_projection.unit_identity(unit)
        (kept if identity and identity in assigned else doomed).append(ip)
    return sorted(kept), sorted(doomed)
