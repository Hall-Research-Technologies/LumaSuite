"""A unit's own thumbnail switch, as `ThumbnailStatus.Get` / `ThumbnailStatus.Set` expose it.

Measured on authorised hardware: encoders (E4211) and decoders (D4111) answer the SAME two methods,
`ThumbnailStatus.Get` -> `{"thumbnailenable": bool}` and `ThumbnailStatus.Set {"thumbnailenable":
bool}` (the reply echoes the value). While the switch is off `/thumbnail/thumbnail1.jpg` answers
HTTP 500, so the switch is what decides whether a picture exists at all.

Not asked of a CS31 switcher: no method list has been seen from one, and an undocumented call is
not made to find out. A reply that is not a plain boolean is "unknown", never a normalised guess.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional

GET_METHOD = "ThumbnailStatus.Get"
SET_METHOD = "ThumbnailStatus.Set"


def parse_state(reply: Any) -> Optional[bool]:
    """The switch as the unit reported it, or None when the reply says nothing usable."""
    if not isinstance(reply, dict) or reply.get("error") is not None:
        return None
    result = reply.get("result")
    if not isinstance(result, dict):
        return None
    value = result.get("thumbnailenable")
    return value if isinstance(value, bool) else None


def supported(role: Any, model: Any = "") -> bool:
    text = f"{role or ''} {model or ''}".lower()
    return "switcher" not in text and "cs31" not in text


def read(call: Callable[..., Any], ip: str, timeout: float) -> Optional[bool]:
    return parse_state(call(ip, GET_METHOD, {}, timeout))


def write_verified(call: Callable[..., Any], ip: str, enable: bool, timeout: float) -> Dict[str, Any]:
    """Write, then read the unit back. Success is the read-back agreeing, never the reply."""
    if not isinstance(enable, bool):
        return {"ok": False, "reason": "invalid", "enabled": None}
    before = read(call, ip, timeout)
    if before is None:
        return {"ok": False, "reason": "unreadable", "enabled": None}
    if before == enable:
        return {"ok": True, "enabled": before, "changed": False, "verified": True}
    reply = call(ip, SET_METHOD, {"thumbnailenable": enable}, timeout)
    if reply is None:
        # The reply may have been lost after the unit acted: read before deciding.
        after = read(call, ip, timeout)
        if after == enable:
            return {"ok": True, "enabled": after, "changed": True, "verified": True}
        return {"ok": False, "reason": "no_answer", "enabled": after}
    after = read(call, ip, timeout)
    if after is None:
        return {"ok": False, "reason": "unverified", "enabled": None}
    if after != enable:
        return {"ok": False, "reason": "not_held", "enabled": after}
    return {"ok": True, "enabled": after, "changed": True, "verified": True}
