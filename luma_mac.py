"""What a hardware address looks like, decided in one place.

An identity normaliser that strips separators will turn almost anything into a MAC: `192-168-100-100`
and `192:168:100:100` are twelve hex digits once the separators are gone, and became
a plausible hardware address and were then accepted as an unknown display. An address is not an identity, and a
permissive normaliser is an attack surface on identity, so the shape is checked *before* anything is
stripped:

* six pairs of hex digits with one consistent separator, `:` or `-`;
* Cisco dotted notation, three groups of four hex digits;
* twelve contiguous hex digits;

and anything that reads as an IPv4 address in any separator spelling (four groups of one to three
decimal digits) is refused outright, whatever it would have become.
"""
import re

_PAIRS = re.compile(r"^[0-9a-f]{2}([:-])(?:[0-9a-f]{2}\1){4}[0-9a-f]{2}\Z")
_DOTTED = re.compile(r"^[0-9a-f]{4}\.[0-9a-f]{4}\.[0-9a-f]{4}\Z")
_PLAIN = re.compile(r"^[0-9a-f]{12}\Z")
_FOUR_DECIMAL_GROUPS = re.compile(r"^\d{1,3}[^0-9a-z]\d{1,3}[^0-9a-z]\d{1,3}[^0-9a-z]\d{1,3}\Z")


def looks_like_ipv4(value) -> bool:
    """Four decimal groups of one to three digits, however they are separated."""
    return bool(_FOUR_DECIMAL_GROUPS.match(str(value if value is not None else "").strip().lower()))


def normalise_mac(value) -> str:
    """The canonical lower-case colon-separated form, or "" when the text is not a hardware address."""
    if not isinstance(value, str):
        return ""
    text = value.strip().lower()
    if not text or looks_like_ipv4(text):
        return ""
    if _PAIRS.match(text):
        compact = text[0:2] + text[3:5] + text[6:8] + text[9:11] + text[12:14] + text[15:17]
    elif _DOTTED.match(text):
        compact = text.replace(".", "")
    elif _PLAIN.match(text):
        compact = text
    else:
        return ""
    return ":".join(compact[i:i + 2] for i in range(0, 12, 2))
