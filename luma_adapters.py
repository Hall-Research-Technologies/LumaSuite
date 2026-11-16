"""Network adapters, with an identity that survives an address change.

Remote Access is assigned to an ADAPTER, not to an address. An operator picks
"the bench network", and the machine's address on it is a detail that can
change -- a DHCP lease expires, a cable moves to a different port, somebody
sets a static address. None of that should mean the listener silently stops
coming back.

So every adapter carries a stable identity, and that identity is what gets
persisted. Three things are deliberately NOT identity:

  * The friendly name. On Windows "Ethernet 3" is renameable in Settings, and
    on any platform two machines can use the same word for different hardware.
  * The IP address. It is the thing that changes; using it as identity is the
    defect this module exists to fix. Worse, an address can be reassigned to a
    defect this module exists to fix. Worse, an address can be reassigned to a
    different adapter entirely -- on the machine this was found on, the
    previously configured address belonged to a Wi-Fi adapter and the
    current one to an Ethernet adapter, on the same subnet. Same network to
    a person, different hardware to Windows.
  * Position in the list. Enumeration order is not stable and never was.

What IS identity, per platform:

  * Windows: the interface GUID. It is the key Windows itself files the
    adapter under in the registry, it survives renaming, re-addressing,
    driver reloads and reboots, and it is what `Get-NetAdapter` reports as
    `InterfaceGuid`. Where that cannot be read, the MAC address is the
    fallback -- stable per card, though a card can be replaced and some
    virtual adapters share one, which is why it is second rather than first.
  * Everything else: the interface name. Unlike the Windows friendly name,
    `eth0` / `en0` IS the operating system's own handle for the interface.

Enumeration is read-only. Nothing here binds, configures or changes anything.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import re
import subprocess
import sys

import luma_mac

log = logging.getLogger(__name__)

#: How an identity was established, best first. Reported so a caller can say
#: "this selection is weakly identified" rather than pretending otherwise.
BY_GUID = "guid"
BY_MAC = "mac"
BY_NAME = "name"

_POWERSHELL = r"""
$ErrorActionPreference = 'SilentlyContinue'
$rows = @()
foreach ($ip in (Get-NetIPAddress -AddressFamily IPv4)) {
  $ad = Get-NetAdapter -InterfaceIndex $ip.InterfaceIndex -ErrorAction SilentlyContinue
  $rows += [PSCustomObject]@{
    alias   = [string]$ip.InterfaceAlias
    address = [string]$ip.IPAddress
    prefix  = [int]$ip.PrefixLength
    ifindex = [int]$ip.InterfaceIndex
    guid    = if ($ad) { [string]$ad.InterfaceGuid } else { '' }
    mac     = if ($ad) { [string]$ad.MacAddress } else { '' }
    status  = if ($ad) { [string]$ad.Status } else { '' }
    virtual = if ($ad) { [bool]$ad.Virtual } else { $false }
  }
}
ConvertTo-Json -InputObject @($rows) -Compress -Depth 3
"""


def _hidden_kwargs() -> dict:
    """Keep a console window from flashing on a packaged Windows build."""
    if sys.platform != "win32":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return {"startupinfo": startupinfo,
            "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _normalise_mac(value) -> str:
    mac = luma_mac.normalise_mac(value if isinstance(value, str) else str(value or ""))
    return mac.replace(":", "")


def _normalise_guid(value) -> str:
    text = str(value or "").strip().lower()
    return text if re.fullmatch(r"\{?[0-9a-f-]{32,38}\}?", text) else ""


def identity_for(guid="", mac="", name="") -> tuple:
    """(id, how) -- the strongest identity available for one adapter.

    Returned as a pair so a caller can tell a GUID-backed selection from one
    resting on a name, and say so rather than implying they are equally solid.
    """
    normalised = _normalise_guid(guid)
    if normalised:
        return "guid:" + normalised.strip("{}"), BY_GUID
    normalised = _normalise_mac(mac)
    if normalised:
        return "mac:" + normalised, BY_MAC
    text = str(name or "").strip().lower()
    return ("name:" + text, BY_NAME) if text else ("", BY_NAME)


def _usable(address: str) -> bool:
    """Whether an address is worth offering at all.

    Link-local and loopback are excluded because neither is a network another
    device can reach LumaSuite on. Nothing is excluded for being virtual, or
    for what it is called: an operator running a bench behind a Hyper-V switch
    is entitled to choose it, and the whole point of this list is that the
    choice is theirs.
    """
    try:
        parsed = ipaddress.ip_address(str(address).strip())
    except ValueError:
        return False
    return not (parsed.is_loopback or parsed.is_link_local
                or parsed.is_multicast or parsed.is_reserved
                or parsed.is_unspecified)


def _entry(name, address, prefix=None, netmask=None, guid="", mac="",
           status="", virtual=False) -> dict | None:
    if not _usable(address):
        return None
    try:
        if netmask:
            network = ipaddress.IPv4Network(f"{address}/{netmask}", strict=False)
        else:
            network = ipaddress.IPv4Network(f"{address}/{int(prefix)}",
                                            strict=False)
    except Exception:
        return None
    identity, how = identity_for(guid, mac, name)
    if not identity:
        return None
    return {
        "id": identity,
        "id_from": how,
        "name": str(name or "").strip() or identity,
        "address": str(address).strip(),
        "netmask": str(network.netmask),
        "prefix": network.prefixlen,
        "cidr": f"{network.network_address}/{network.prefixlen}",
        "scan": (f"{str(network.network_address).rsplit('.', 1)[0]}.1-254"
                 if network.prefixlen <= 24
                 else f"{network.network_address}/{network.prefixlen}"),
        "status": str(status or "").strip(),
        "up": str(status or "").strip().lower() in ("", "up"),
        "virtual": bool(virtual),
        # What a person should read. The identity is never shown: a GUID in
        # front of an operator is noise they cannot act on.
        "label": f"{str(name or '').strip()} — {str(address).strip()}",
    }


def _windows_powershell() -> list:
    """Windows, with the identity Windows itself uses."""
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-Command", _POWERSHELL],
            capture_output=True, text=True, timeout=25, **_hidden_kwargs())
    except Exception as exc:
        log.info("[ADAPTERS] PowerShell enumeration unavailable: %s",
                 type(exc).__name__)
        return []
    raw = (completed.stdout or "").strip()
    if not raw:
        return []
    try:
        rows = json.loads(raw)
    except ValueError:
        log.info("[ADAPTERS] PowerShell returned something that is not JSON")
        return []
    if isinstance(rows, dict):
        rows = [rows]
    found = []
    for row in rows or []:
        entry = _entry(row.get("alias"), row.get("address"),
                       prefix=row.get("prefix"), guid=row.get("guid"),
                       mac=row.get("mac"), status=row.get("status"),
                       virtual=row.get("virtual"))
        if entry:
            found.append(entry)
    return found


def _windows_ipconfig() -> list:
    """Fallback for a machine where PowerShell cannot be run.

    `ipconfig /all` carries the Physical Address, so an identity is still
    available -- weaker than a GUID, and recorded as such.
    """
    try:
        text = subprocess.check_output(
            ["ipconfig", "/all"], text=True, encoding="utf-8",
            errors="ignore", **_hidden_kwargs())
    except Exception:
        return []
    found = []
    for block in re.split(r"\r?\n\r?\n", text):
        name = re.search(r"(?mi)^(?:.*adapter)\s+(.+?):\s*$", block)
        address = re.search(r"(?mi)^\s*IPv4[^:]*:\s*([0-9]+(?:\.[0-9]+){3})",
                            block)
        netmask = re.search(r"(?mi)^\s*Subnet\s*Mask[^:]*:\s*"
                            r"([0-9]+(?:\.[0-9]+){3})", block)
        physical = re.search(r"(?mi)^\s*Physical\s*Address[^:]*:\s*"
                             r"([0-9A-Fa-f-]{11,})", block)
        disconnected = re.search(
            r"(?mi)^\s*Media\s*State\s*.*:\s*Media\s*disconnected\s*$", block)
        if not (name and address and netmask):
            continue
        entry = _entry(name.group(1), address.group(1),
                       netmask=netmask.group(1),
                       mac=physical.group(1) if physical else "",
                       status="Disconnected" if disconnected else "Up")
        if entry:
            found.append(entry)
    return found


def _posix() -> list:
    """Linux and macOS, where the interface name is the operating system's."""
    try:
        import psutil
    except ImportError:
        return []
    try:
        addresses = psutil.net_if_addrs()
        stats = psutil.net_if_stats()
    except Exception:
        return []
    found = []
    for name, entries in (addresses or {}).items():
        for item in entries:
            entry = _entry_posix(name, item, stats)
            if entry:
                found.append(entry)
    return found


def _entry_posix(name, item, stats) -> dict | None:
    """One POSIX address. The interface name is the identity here."""
    import socket as _socket
    if getattr(item, "family", None) != _socket.AF_INET:
        return None
    entry = _entry(name, getattr(item, "address", ""),
                   netmask=getattr(item, "netmask", ""),
                   mac="", status="Up" if (name in (stats or {})
                                           and stats[name].isup) else "Down")
    if entry:
        # On POSIX the name IS the operating system's handle, so it outranks
        # the MAC -- a card swap keeps eth0, which is what an operator means.
        entry["id"] = "name:" + str(name).strip().lower()
        entry["id_from"] = BY_NAME
    return entry


# ---- Which adapter is the sensible default --------------------------------
#
# Used ONLY to order a list and to choose when the operator has chosen nothing.
# A saved selection is never run through this: an operator who picked a virtual
# switch on purpose keeps it.

#: Name fragments that mark an adapter that is not a physical network port.
_VIRTUAL_TOKENS = (
    "virtual", "vethernet", "hyper-v", "vmware", "vmnet", "vbox", "virtualbox",
    "loopback", "bluetooth", "wi-fi direct", "wifi direct", "local area connection*",
    "vpn", "wireguard", "wg0", "tailscale", "zerotier", "hamachi", "openvpn",
    "nordlynx", "anyconnect", "fortinet", "globalprotect", "pangp", "ppp",
    "docker", "veth", "wsl", "npcap", "tap-windows", "tunnel", "isatap",
    "teredo", "6to4", "pseudo",
)
_VIRTUAL_PREFIXES = ("lo", "tun", "tap", "utun", "awdl", "llw", "br-", "virbr",
                     "docker", "veth", "vmnet", "vboxnet", "ap", "bridge", "gif",
                     "stf", "ipsec", "ppp", "zt", "tailscale", "wg")
_WIFI = re.compile(r"(wi-?fi|wlan|wireless|\bwl[a-z0-9]+|\bwlp)", re.I)
_ETHERNET = re.compile(r"(ethernet|\beth\d|\ben[opsx]\d|\bem\d|\bgbe)", re.I)

KIND_ETHERNET = "ethernet"
KIND_WIFI = "wifi"
KIND_OTHER = "other"
KIND_VIRTUAL = "virtual"


def adapter_kind(row: dict) -> str:
    """ethernet, wifi, other (physical, unrecognised) or virtual."""
    name = str((row or {}).get("name") or "").strip().lower()
    if (row or {}).get("virtual") or any(t in name for t in _VIRTUAL_TOKENS):
        return KIND_VIRTUAL
    if name.startswith(_VIRTUAL_PREFIXES) and not _ETHERNET.search(name):
        return KIND_VIRTUAL
    if _WIFI.search(name):
        return KIND_WIFI
    if _ETHERNET.search(name):
        return KIND_ETHERNET
    return KIND_OTHER


_KIND_ORDER = {KIND_ETHERNET: 0, KIND_WIFI: 1, KIND_OTHER: 2, KIND_VIRTUAL: 3}


def _routable(row: dict) -> bool:
    """A usable unicast IPv4 address (the list already drops loopback and link-local)."""
    address = str((row or {}).get("address") or (row or {}).get("ip") or "")
    return _usable(address)


def rank_key(row: dict) -> tuple:
    """Sort key: connected physical Ethernet, then Wi-Fi, other physical, virtual.

    A disconnected adapter sorts after every connected one of any kind, and an
    adapter without a usable address after both. Ties fall back to name and
    address so the order is the same on every call.
    """
    up = (row or {}).get("up", True)
    return (0 if up else 1,
            0 if _routable(row) else 1,
            _KIND_ORDER[adapter_kind(row)],
            str((row or {}).get("name") or "").lower(),
            str((row or {}).get("address") or (row or {}).get("ip") or ""))


def rank_adapters(rows) -> list:
    """`rows` best default first. Does not mutate its input."""
    return sorted(list(rows or []), key=rank_key)


def default_adapter(rows) -> dict | None:
    """The adapter to use when nothing was chosen, or None.

    Never a virtual, VPN or Wi-Fi Direct adapter while any physical one is
    usable -- and when only virtual adapters exist, the best of those rather
    than nothing, since an all-virtual bench is a real configuration.
    """
    ranked = rank_adapters(r for r in (rows or []) if _routable(r))
    return ranked[0] if ranked else None


def enumerate_adapters(include_down: bool = False) -> list:
    """Every adapter with an address another device could reach, newest first.

    Ordered deterministically -- by name then address -- because an operator
    choosing from a list should find the same thing in the same place, and
    enumeration order is not stable on its own.
    """
    found = []
    if sys.platform == "win32":
        found = _windows_powershell() or _windows_ipconfig()
    else:
        found = _posix()
    if not include_down:
        found = [a for a in found if a.get("up")]
    for a in found:
        a["kind"] = adapter_kind(a)
    found = rank_adapters(found)
    return found


def find_by_id(adapter_id: str, adapters=None) -> list:
    """Every address currently on that adapter. Empty when it is not here."""
    wanted = str(adapter_id or "").strip().lower()
    if not wanted:
        return []
    return [a for a in (adapters if adapters is not None
                        else enumerate_adapters())
            if a.get("id", "").lower() == wanted]


def resolve_address(adapter_id: str, preferred_address: str = "",
                    adapters=None) -> tuple:
    """Which address to bind for a saved adapter, and why not if there is none.

    Returns (address, reason). The rules, in order:

      * the adapter is not here            -> ("", "adapter missing")
      * the saved address is still on it   -> that address, unchanged
      * it now has exactly one address     -> that one; this is the case the
                                              whole module exists for, an
                                              address change on a chosen
                                              adapter
      * it has several and none match      -> ("", "ambiguous"); several
                                              addresses with no saved one to
                                              anchor to is a choice, and
                                              choosing for the operator is how
                                              a listener ends up somewhere
                                              nobody intended
    """
    matches = find_by_id(adapter_id, adapters)
    if not matches:
        return "", "adapter missing"
    wanted = str(preferred_address or "").strip()
    for candidate in matches:
        if candidate.get("address") == wanted:
            return wanted, ""
    if len(matches) == 1:
        return matches[0]["address"], ""
    return "", "ambiguous"


def adopt_saved_address(address: str, adapters=None) -> dict | None:
    """The adapter that owns `address` right now -- only if it is unambiguous.

    This is the migration path for a configuration saved before adapters had
    identities. It answers only when exactly one present adapter carries the
    address; zero means the evidence is gone, and more than one means the
    answer cannot be established. Both return None, because adopting the wrong
    adapter would silently move Remote Access to different hardware.
    """
    wanted = str(address or "").strip()
    if not wanted:
        return None
    owners = [a for a in (adapters if adapters is not None
                          else enumerate_adapters())
              if a.get("address") == wanted]
    return owners[0] if len(owners) == 1 else None
