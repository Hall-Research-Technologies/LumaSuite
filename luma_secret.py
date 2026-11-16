"""Protecting a secret that has to be readable again later.

Most secrets in LumaSuite are one-way: a password is hashed with scrypt and
nothing ever needs the original back. A Direct Access link is not like that.
The operator has to be able to open Settings a month later and copy the same
link, so the token has to be recoverable -- which means storing it in a form
that survives a restart and is not simply sitting in a JSON file in plain text.

Nothing here invents cryptography. On Windows it is DPAPI, the operating
system's own facility for exactly this: `CryptProtectData` encrypts with a key
derived from the logged-in user's credentials and held by the OS, so the blob is
useless to another account on the machine and useless on another machine. That
is the established platform mechanism, it needs no dependency, and LumaSuite
already runs as the operator's own user.

Where DPAPI is not available the honest answer is that there is no equivalent
facility in the standard library, and this says so rather than pretending:
`protect()` reports which protection was actually applied, the caller records
it, and the operator-facing surface can state it. An obfuscation invented here
would look like protection and be none.

Two rules:

  * `protect` never raises. A secret that cannot be protected is still a secret
    the product has to store, and failing the operation would leave the account
    in a half-configured state. It returns the weaker protection and says so.
  * `unprotect` returns None rather than raising on anything it cannot read --
    a blob from another user, another machine, or a damaged file. The caller
    treats that as "this link cannot be shown; regenerate it", which is true
    and is recoverable.
"""
from __future__ import annotations

import base64
import logging
import sys

log = logging.getLogger(__name__)

#: What `protect` managed to do. Stored alongside the blob so a record written
#: on one platform is read correctly on another.
DPAPI = "dpapi"
PLAIN = "plain"


def available() -> bool:
    """Whether real at-rest protection exists on this platform."""
    return _dpapi() is not None


def describe() -> str:
    """One sentence for a report or a diagnostic. Never a secret."""
    if available():
        return ("Windows DPAPI (CryptProtectData), scoped to the user account "
                "running LumaSuite")
    return ("no platform secret store on this system; recoverable secrets are "
            "held in the data directory with no additional encryption")


def protect(text: str) -> tuple:
    """(blob, protection). The blob is base64 text, safe for a JSON file."""
    raw = (text or "").encode("utf-8")
    api = _dpapi()
    if api is not None:
        try:
            return base64.b64encode(api[0](raw)).decode("ascii"), DPAPI
        except Exception as exc:
            # Fall through rather than fail: see the note above.
            log.warning("[SECRET] DPAPI protect failed (%s); storing "
                        "unprotected", type(exc).__name__)
    return base64.b64encode(raw).decode("ascii"), PLAIN


def unprotect(blob: str, protection: str):
    """The original text, or None if this blob cannot be read here."""
    if not blob:
        return None
    try:
        raw = base64.b64decode(blob.encode("ascii"), validate=True)
    except Exception:
        return None
    if protection == DPAPI:
        api = _dpapi()
        if api is None:
            # Written on Windows, read somewhere else. Nothing to be done, and
            # guessing would be worse than saying so.
            return None
        try:
            return api[1](raw).decode("utf-8")
        except Exception as exc:
            log.info("[SECRET] DPAPI unprotect failed: %s", type(exc).__name__)
            return None
    try:
        return raw.decode("utf-8")
    except Exception:
        return None


# ----------------------------------------------------------------- DPAPI
_CACHED = None


def _dpapi():
    """(protect, unprotect) bound to CryptProtectData, or None."""
    global _CACHED
    if _CACHED is not None:
        return _CACHED or None
    if not sys.platform.startswith("win"):
        _CACHED = False
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class Blob(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD),
                        ("pbData", ctypes.POINTER(ctypes.c_char))]

        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        def _blob(data):
            buffer = ctypes.create_string_buffer(data, len(data))
            return Blob(len(data), ctypes.cast(buffer,
                                               ctypes.POINTER(ctypes.c_char)))

        def _read(blob):
            try:
                return ctypes.string_at(blob.pbData, blob.cbData)
            finally:
                kernel32.LocalFree(blob.pbData)

        def encrypt(data):
            out = Blob()
            # A description string is stored with the blob in the clear, so it
            # names the feature and nothing else.
            if not crypt32.CryptProtectData(
                    ctypes.byref(_blob(data)),
                    ctypes.c_wchar_p("LumaSuite direct access"),
                    None, None, None, 0, ctypes.byref(out)):
                raise OSError(ctypes.get_last_error(), "CryptProtectData")
            return _read(out)

        def decrypt(data):
            out = Blob()
            if not crypt32.CryptUnprotectData(
                    ctypes.byref(_blob(data)), None, None, None, None, 0,
                    ctypes.byref(out)):
                raise OSError(ctypes.get_last_error(), "CryptUnprotectData")
            return _read(out)

        # Prove it works before claiming it does. A build where the call is
        # present but refused must report no protection rather than storing
        # something it cannot read back.
        if decrypt(encrypt(b"lumasuite")) != b"lumasuite":
            raise OSError("DPAPI round trip did not return what went in")
        _CACHED = (encrypt, decrypt)
        return _CACHED
    except Exception as exc:
        log.info("[SECRET] DPAPI unavailable: %s", type(exc).__name__)
        _CACHED = False
        return None
