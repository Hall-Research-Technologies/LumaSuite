"""AV LAN link speed and LLDP neighbour, as a LumaStream unit reports them.

One read gives both. Encoders answer `AllEthernetLanStatus.Get`, decoders answer
`AllEthernetStatus.Get`; the other family's name answers a JSON-RPC error. The result is a LIST of
interface objects, and the one wanted is the AV LAN: the device spells its `lanid` "ehternet".
Everything else (`audiolan`, `lan`) is the second port and is never the AV link.

    [{"lanid": "ehternet", "link": true, "mac": "...", "speed": "1000M",
      "lldp": {"ChassisID": ..., "MgmtIP": ..., "PortDescr": ..., "PortID": ..., "SysName": ...,
               "SysDescr": ..., "TTL": 120, "Capabilities": [...]}},
     {"lanid": "audiolan", "link": false}]

An interface with no link carries no `speed`, `mac` or `lldp` keys. That is "no speed to report",
not a slow link, and nothing here ever turns it into one.

Everything in the reply is device text on its way into the browser, so it is typed, bounded and
stripped of control characters before it is kept. Anything the parser cannot recognise is `None`:
the caller then leaves whatever it already knew alone, because an optional read that failed or came
back odd must never erase something verified.

Not asked of a CS31 switcher: no method list has been seen from one, and an undocumented call is
not made to find out.
"""
from __future__ import annotations

import re
import time
from typing import Any, Callable, Dict, List, Optional

ENCODER_METHOD = "AllEthernetLanStatus.Get"
DECODER_METHOD = "AllEthernetStatus.Get"

# The device's own spelling first; the correct spelling is accepted in case a later firmware fixes it.
AV_LAN_IDS = ("ehternet", "ethernet")

MAX_INTERFACES = 8
_TEXT_LIMITS = {"ChassisID": 64, "MgmtIP": 64, "MgmtIface": 16, "PortDescr": 128, "PortID": 128,
                "SysName": 128, "SysDescr": 512}
_MAX_CAPABILITIES = 16
_MAX_CAPABILITY_LENGTH = 64
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_SPEED = re.compile(r"^\s*(\d{1,6}(?:\.\d{1,3})?)\s*([a-z/]{0,12})\s*$", re.IGNORECASE)


def normalize_speed(raw: Any) -> Optional[int]:
    """Mbps as an integer, or None when `raw` is not a speed.

    1000, "1000", "1000M", "1000 Mbps", "1000Mb/s", "1G", "1 Gbps" all give 1000. A boolean is
    not a number, zero is not a speed, and a unit that is neither mega nor giga is unknown.
    """
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, (int, float)):
        value = float(raw)
        return int(round(value)) if 0 < value < 1_000_000 else None
    if not isinstance(raw, str):
        return None
    match = _SPEED.match(raw)
    if not match:
        return None
    value = float(match.group(1))
    unit = match.group(2).lower()
    if value <= 0:
        return None
    if unit == "" or unit.startswith("m"):
        mbps = value
    elif unit.startswith("g"):
        mbps = value * 1000
    else:
        return None
    return int(round(mbps)) if mbps < 1_000_000 else None


def _text(value: Any, limit: int) -> Optional[str]:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    cleaned = _CONTROL.sub(" ", str(value)).strip()
    return cleaned[:limit] if cleaned else None


def clean_lldp(raw: Any) -> Optional[Dict[str, Any]]:
    """The neighbour as the device sent it, typed and bounded; None when there is nothing usable."""
    if not isinstance(raw, dict):
        return None
    kept: Dict[str, Any] = {}
    for key, limit in _TEXT_LIMITS.items():
        text = _text(raw.get(key), limit)
        if text is not None:
            kept[key] = text
    ttl = raw.get("TTL")
    if isinstance(ttl, int) and not isinstance(ttl, bool) and 0 <= ttl <= 65535:
        kept["TTL"] = ttl
    caps = raw.get("Capabilities")
    if isinstance(caps, list):
        listed: List[str] = []
        for item in caps[:_MAX_CAPABILITIES]:
            text = _text(item, _MAX_CAPABILITY_LENGTH)
            if text is not None:
                listed.append(text)
        if listed:
            kept["Capabilities"] = listed
    return kept or None


def parse(result: Any) -> Optional[Dict[str, Any]]:
    """The AV LAN's link state from a reply's `result`, or None if it is not a recognisable reply.

    Keys: link_up (bool or None), linkspeed_mbps (int or None), linkspeed_raw (str or None),
    lldp (dict or None), switch (dict of name / chassis / mgmt_ip / port, or None).
    """
    if not isinstance(result, list) or len(result) > MAX_INTERFACES:
        return None
    entry = None
    for item in result:
        if isinstance(item, dict) and isinstance(item.get("lanid"), str) \
                and item["lanid"].strip().lower() in AV_LAN_IDS:
            entry = item
            break
    if entry is None:
        return None
    link = entry.get("link")
    link_up = link if isinstance(link, bool) else None
    raw_speed = entry.get("speed")
    raw_text = _text(raw_speed, 32) if isinstance(raw_speed, (str, int)) and not isinstance(raw_speed, bool) else None
    speed = normalize_speed(raw_speed) if link_up is not False else None
    lldp = clean_lldp(entry.get("lldp")) if link_up is not False else None
    switch = None
    if lldp:
        switch = {"name": lldp.get("SysName"), "chassis": lldp.get("ChassisID"),
                  "mgmt_ip": lldp.get("MgmtIP"), "port": lldp.get("PortDescr") or lldp.get("PortID")}
    return {"link_up": link_up, "linkspeed_mbps": speed, "linkspeed_raw": raw_text if link_up is not False else None,
            "lldp": lldp, "switch": switch}


def read(call: Callable[..., Any], ip: str, role: str, timeout: float) -> Optional[Dict[str, Any]]:
    """Ask the unit once (twice only if the first name is answered with an error).

    `call(ip, method, params, timeout)` is the application's authenticated JSON-RPC helper and
    returns the decoded reply, or None when the unit did not answer. A unit that did not answer
    is not asked again under another name: a device that is not listening is not made to listen
    by repeating the question. A switcher is not asked at all.
    """
    role = (role or "").strip().lower()
    if "switcher" in role:
        return None
    methods = (ENCODER_METHOD, DECODER_METHOD) if "encoder" in role else (DECODER_METHOD, ENCODER_METHOD)
    for method in methods:
        reply = call(ip, method, {}, timeout)
        if not isinstance(reply, dict):
            return None
        if "result" in reply and "error" not in reply:
            parsed = parse(reply["result"])
            if parsed is not None:
                parsed["read_at"] = time.time()
            return parsed
        # An error from a unit that answered: the other family's method name, so try the other.
    return None
