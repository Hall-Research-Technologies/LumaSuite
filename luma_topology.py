"""Which network switch each LumaStream unit is plugged into, from the LLDP neighbour it reports.

Derived information, never a poll of its own. The scan already asks every unit for its AV LAN
status (`luma_netstatus`), and that one read carries the LLDP neighbour; this module only reads
what the inventory already holds. Nothing here touches a device or the network.

The semantics are OmniSuite's, reproduced rather than redesigned:

* A switch is identified by the **chassis ID** its neighbours report -- never by subnet, address,
  adapter or hostname. The system name is a label for people and decides nothing.
* A unit with no usable LLDP (never read, no link, no neighbour, malformed) is **unknown**. It is
  counted, but it never forms a group, never becomes a second switch and is never "different".
* Units are grouped by chassis. The **primary** switch is the one with strictly the most units.
  On a tie there is no primary and therefore no "different" unit: nothing is designated.
* Every unit that reports a switch other than the primary is on a **different switch**.
* The model holds N switches. Two or more known switches is `multi_switch`.
* The acknowledgement of the advisory belongs to the lifetime of the discovered inventory: it is
  reset by clearing the inventory and by nothing else (not a scan, not a new switch appearing).

Carried over from OmniSuite: a neighbour that is one of our own units (exact management address or
exact MAC, never a name) is not a switch; the chain is followed to the switch and an unresolvable
chain is unknown, never "different". Not carried over: the separate daisy-chain banner and the
USB-extender rows (no LumaStream counterpart has been observed).

Informational only. Nothing here changes routing, discovery, eligibility or any device.
"""
from __future__ import annotations

import ipaddress
import os
import re
from typing import Any, Dict, Iterable, List, Optional

import luma_store

_MAC_LIKE = re.compile(r"^[0-9a-f]{12}$")

#: Why a unit has no switch. Strings for diagnostics, never shown as a classification.
REASON_NOT_READ = "not_read"          # the unit has not reported its network status
REASON_NO_LINK = "no_link"            # the AV LAN reported no link
REASON_NO_NEIGHBOUR = "no_neighbour"  # linked, but no LLDP neighbour was reported
REASON_MALFORMED = "malformed"        # a neighbour was reported without a usable chassis ID
REASON_UNRESOLVED = "unresolved_chain"  # the neighbour is another of our units and the chain does
                                        # not reach a switch (loop, too deep, or a silent unit)

#: A chain deeper than this is a misreading or a topology nobody should be building.
MAX_CHAIN_HOPS = 8

PRIMARY = "primary"
DIFFERENT = "different"
TIED = "tied"        # on a known switch, but no switch is primary
UNKNOWN = "unknown"


def normalize_chassis_id(value: Any) -> str:
    """Canonical form of a chassis ID, for grouping only. "" when there is nothing to group on.

    A switch reports one chassis with cosmetic differences -- case, and `:`/`-`/`.` separators in a
    MAC -- and two units behind one switch must not look like two switches because of punctuation.
    Only a value that is recognisably a MAC has its separators stripped; anything else keeps its
    shape, because a chassis ID may equally be a network address or a locally assigned string, and
    those must not be mangled into false equality.

    The permissive stripping is the dangerous part: `198.51.100.100` stripped of its dots is twelve
    decimal digits, which is also a well-formed MAC. An address is therefore recognised first and
    kept as an address. An all-zero or all-ones MAC is a placeholder, not an identity.
    """
    if isinstance(value, bool) or not isinstance(value, str):
        return ""
    text = value.strip().lower()
    if not text:
        return ""
    try:
        ipaddress.ip_address(text)
        return text
    except ValueError:
        pass
    stripped = re.sub(r"[\s:.\-]", "", text)
    if _MAC_LIKE.match(stripped):
        if stripped in ("000000000000", "ffffffffffff"):
            return ""
        return stripped
    return re.sub(r"\s+", " ", text)


def _ip_key(address: Any):
    """Numeric ordering for addresses; anything else after them, by text."""
    try:
        return (0, int(ipaddress.IPv4Address(str(address).strip())), "")
    except (ipaddress.AddressValueError, ValueError):
        return (1, 0, str(address))


def switch_of(unit: Any) -> Dict[str, Any]:
    """The switch one unit reports, or why it reports none.

    Returns `{"known": True, "id", "raw", "name", "mgmt_ip", "port"}` or
    `{"known": False, "reason": ...}`. Reads only the stored `net` record -- the same one the
    LLDP hover panel shows -- and trusts none of it: every shape is checked.
    """
    net = unit.get("net") if isinstance(unit, dict) else None
    if not isinstance(net, dict):
        return {"known": False, "reason": REASON_NOT_READ}
    if net.get("link_up") is False:
        return {"known": False, "reason": REASON_NO_LINK}
    lldp = net.get("lldp")
    if lldp is None:
        return {"known": False, "reason": REASON_NO_NEIGHBOUR}
    if not isinstance(lldp, dict):
        return {"known": False, "reason": REASON_MALFORMED}
    raw = lldp.get("ChassisID")
    identity = normalize_chassis_id(raw)
    if not identity:
        # A neighbour that did not say which chassis it is cannot be grouped, and a name alone
        # is a label, not an identity.
        return {"known": False, "reason": REASON_MALFORMED if lldp else REASON_NO_NEIGHBOUR}

    def text(key):
        item = lldp.get(key)
        return item.strip() if isinstance(item, str) and item.strip() else ""

    return {"known": True, "id": identity, "raw": str(raw).strip(), "name": text("SysName"),
            "mgmt_ip": text("MgmtIP"), "port": text("PortDescr") or text("PortID")}


def _resolve_chain(rows) -> Dict[str, Dict[str, Any]]:
    """address -> {"sw": the switch the unit is really behind, "via": [addresses], "resolution"}.

    An LLDP neighbour is not necessarily a switch: a unit that is cabled through another unit
    reports *that unit* as its neighbour, and grouping on that would invent a second switch. A
    neighbour is recognised as one of our own units only by exact identity -- its advertised
    management address equal to a discovered unit's address, or its chassis ID equal to a
    discovered unit's MAC. Never by name. The chain is followed (bounded, loop-protected) to the
    first neighbour that is not ours; that is the switch. Where it cannot be followed the unit's
    switch is unknown -- an unresolved chain is missing information, never a different switch.
    A unit with no switch of its own stays as it is.
    """
    by_ip = {address: sw for address, _unit, sw in rows}
    by_mac: Dict[str, str] = {}
    for address, unit, _sw in rows:
        mac = normalize_chassis_id(unit.get("mac"))
        if mac:
            by_mac.setdefault(mac, address)

    def neighbour(sw):
        mgmt = sw.get("mgmt_ip") or ""
        return mgmt if mgmt in by_ip else by_mac.get(sw["id"])

    out: Dict[str, Dict[str, Any]] = {}
    for address, _unit, sw in rows:
        if not sw["known"]:
            out[address] = {"sw": sw, "via": [], "resolution": "none"}
            continue
        current, via, seen, resolution = sw, [], {address}, None
        for _ in range(MAX_CHAIN_HOPS + 1):
            nxt = neighbour(current)
            if nxt is None:
                resolution = "direct" if not via else "resolved_through_device"
                break
            nxt_sw = by_ip[nxt]
            if nxt in seen or not nxt_sw["known"]:
                resolution = "unresolved"
                break
            seen.add(nxt)
            via.append(nxt)
            current = nxt_sw
        else:
            resolution = "unresolved"
        if resolution == "unresolved":
            out[address] = {"sw": {"known": False, "reason": REASON_UNRESOLVED}, "via": via,
                            "resolution": resolution}
        else:
            out[address] = {"sw": current, "via": via, "resolution": resolution}
    return out


def topology(units: Optional[Iterable[Any]]) -> Dict[str, Any]:
    """Group the inventory by the switch each unit reports. Pure; reads nothing else.

    Keys: `switch_count`, `groups` (largest first, then by chassis), `dominant_chassis_id`
    (the primary switch, "" when none), `tied`, `multi_switch`, `minority_ips` (units on a switch
    other than the primary, in address order), `units_with_lldp`, `units_without_lldp` and
    `devices`: ip -> {mac, role, state, switch_id, switch_name, port, reason}.

    `state` is `primary`, `different`, `tied` or `unknown`. The display key is the address (rows
    are keyed by it), and the MAC rides beside it as the identity.
    """
    rows = []
    for unit in units or []:
        if not isinstance(unit, dict):
            continue
        address = str(unit.get("ip") or "").strip()
        if not address:
            continue
        rows.append((address, unit, switch_of(unit)))
    rows.sort(key=lambda r: _ip_key(r[0]))
    resolved = _resolve_chain(rows)
    # What a unit reports and which switch it is behind are two facts. Grouping uses the second.
    rows = [(address, unit, resolved[address]["sw"], resolved[address], own)
            for address, unit, own in rows]

    groups: Dict[str, Dict[str, Any]] = {}
    for address, unit, sw, chain, _own in rows:
        if not sw["known"]:
            continue
        entry = groups.setdefault(sw["id"], {"chassis_id": sw["id"], "chassis_id_raw": sw["raw"],
                                             "chassis_name": "", "count": 0, "ips": []})
        entry["count"] += 1
        entry["ips"].append(address)
        # Rows are in address order, so the first name offered is the same on every call. Only a
        # unit attached straight to the switch can name it; a chained unit names the unit in front.
        if not chain["via"]:
            if not entry["chassis_name"] and sw["name"]:
                entry["chassis_name"] = sw["name"]
    ordered: List[Dict[str, Any]] = sorted(groups.values(),
                                           key=lambda g: (-g["count"], g["chassis_id"]))

    dominant, tied = "", False
    if len(ordered) > 1:
        if ordered[0]["count"] > ordered[1]["count"]:
            dominant = ordered[0]["chassis_id"]
        else:
            tied = True
    elif ordered:
        dominant = ordered[0]["chassis_id"]

    names = {g["chassis_id"]: g["chassis_name"] for g in ordered}
    devices: Dict[str, Dict[str, Any]] = {}
    minority: List[str] = []
    without = 0
    for address, unit, sw, chain, own in rows:
        role = str(unit.get("role") or unit.get("type") or "")
        record = {"mac": str(unit.get("mac") or ""), "role": role, "state": UNKNOWN,
                  "switch_id": "", "switch_name": "", "port": "", "reason": "",
                  "chained": bool(chain["via"]), "via_ip": chain["via"][0] if chain["via"] else "",
                  "chain_hops": len(chain["via"]), "resolution": chain["resolution"]}
        if sw["known"]:
            record["switch_id"] = sw["id"]
            record["switch_name"] = sw["name"] or names.get(sw["id"], "")
            # Through a chain the port belongs to the unit at the far end, not to this one.
            record["port"] = "" if chain["via"] else sw["port"]
            if tied:
                record["state"] = TIED
            elif sw["id"] == dominant:
                record["state"] = PRIMARY
            else:
                record["state"] = DIFFERENT
                minority.append(address)
        else:
            record["reason"] = sw["reason"]
            without += 1
        devices[address] = record

    return {
        "switch_count": len(ordered),
        "groups": ordered,
        "dominant_chassis_id": dominant,
        "tied": tied,
        "multi_switch": len(ordered) > 1,
        "minority_ips": minority,
        "units_with_lldp": sum(g["count"] for g in ordered),
        "units_without_lldp": without,
        "units_not_read": sum(1 for d in devices.values() if d["reason"] == REASON_NOT_READ),
        "devices": devices,
    }


# ---------------------------------------------------------------------------------------------
# The acknowledgement
# ---------------------------------------------------------------------------------------------
class AckStore:
    """Whether the operator has dismissed the multi-switch advisory.

    Functional state with its own file, never inferred from whether the banner is on screen. It
    belongs to the lifetime of the discovered inventory: `reset()` is called where the inventory is
    cleared and nowhere else. A scan, a rescan, a new switch appearing or a page load never resets
    it. A missing or damaged file reads as "not acknowledged", which shows the advisory once more
    rather than hiding a topology the operator never saw.
    """

    def __init__(self, path):
        self.path = path

    def read(self) -> Dict[str, Any]:
        data = luma_store.read_json(self.path, {})
        return data if isinstance(data, dict) else {}

    def acknowledged(self) -> bool:
        return self.read().get("acknowledged") is True

    def set(self, acknowledged: bool, now: float, covered: Optional[Iterable[str]] = None) -> bool:
        """Record the choice, and the switches it covered. True when it was stored.

        `covered` is what the operator was shown when they dismissed (chassis IDs). It is a record,
        not a rule: whether the advisory shows follows the acknowledgement alone, so a scan or a
        further switch never re-raises it (reset belongs to clearing the inventory).
        """
        return luma_store.atomic_write_json(
            self.path, {"acknowledged": bool(acknowledged),
                        "acknowledged_at": now if acknowledged else None,
                        "covered_switches": sorted(set(covered or [])) if acknowledged else []})

    def covered(self) -> List[str]:
        items = self.read().get("covered_switches")
        return [str(i) for i in items] if isinstance(items, list) else []

    def reset(self) -> None:
        with luma_store.lock_for(self.path):
            try:
                if os.path.exists(self.path):
                    os.unlink(self.path)
            except OSError:
                # Could not be removed: write the cleared state instead of leaving a dismissal
                # standing over an inventory that no longer exists.
                luma_store.atomic_write_json(self.path, {"acknowledged": False,
                                                         "acknowledged_at": None})
