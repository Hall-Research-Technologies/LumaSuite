"""Transport security for the remote listener.

Remote access sends a password and then carries a session cookie, so on a
routable network it needs TLS. Two ways to get a certificate, in the order an
administrator should prefer them:

  1. **Supplied.** A certificate and private key the site already has, from its
     own CA or a real issuer. This needs nothing beyond the standard library:
     `ssl` loads the pair and `make_server` takes the context.

  2. **Generated.** A self-signed certificate LumaSuite creates for the address
     it is about to serve. This needs `cryptography`, and that is the one
     dependency this adds -- 11 MB, almost all of it a Rust extension, on a
     33 MB executable. It is carried because an AV technician setting up a
     classroom will not have a certificate, and without this they would stay on
     plain HTTP, which is the thing TLS was added to stop. If the library is
     absent, generation is unavailable and supplied certificates still work.

  3. A reverse proxy in front, which is a deployment choice rather than code.

Nothing here invents cryptography. Generation is `cryptography`'s own
certificate builder with conservative parameters; serving is the standard
library's TLS.

Two rules hold throughout:

  * **A private key is a secret.** It is never returned by an API, never
    logged, never copied anywhere a browser can reach, and never bundled into
    the executable. What this module publishes about a certificate is its
    subject, its validity and its fingerprint -- never key material.

  * **A failure is never a downgrade.** If configured TLS cannot start, the
     remote listener does not start. It never quietly becomes HTTP, because an
     administrator who configured HTTPS and got HTTP would have no way of
     knowing.
"""
from __future__ import annotations

import datetime
import hashlib
import ipaddress
import logging
import os
import platform
import re
import socket
import ssl
from pathlib import Path

log = logging.getLogger(__name__)

#: How long a generated certificate is good for, in CALENDAR years from the moment
#: it is generated (see `add_calendar_years`). Long enough not to be a recurring
#: chore, short enough to be replaced within the life of a room.
GENERATED_VALIDITY_YEARS = 3

#: Identity written into every certificate LumaSuite generates. The common name
#: is the Windows machine name; these two are fixed.
CERT_ORGANIZATION = "Hall Research"
CERT_ORGANIZATIONAL_UNIT = "LumaSuite"

#: X.509 upper bounds (RFC 5280): CN 64 characters; a DNS name 253, each label 63.
_CN_MAX = 64
_DNS_MAX = 253
_LABEL_MAX = 63

#: File names inside the data directory. The key sits beside the certificate
#: and is created with an owner-only mode where the platform honours it.
CERT_FILE = "remote_cert.pem"
KEY_FILE = "remote_key.pem"


class TlsError(Exception):
    """A certificate or key could not be used. Carries an operator sentence."""


def add_calendar_years(moment, years=GENERATED_VALIDITY_YEARS):
    """`moment` plus whole calendar years: same month, day and time, year + `years`.

    The one rule for a date that does not exist in the target year: **29 February
    becomes 28 February**. The certificate then ends in the same month it began and
    never later than N calendar years, which is the safe direction for an expiry.
    Every other date, including month ends (31 January, 30 April), maps to itself.
    """
    target = moment.year + int(years)
    try:
        return moment.replace(year=target)
    except ValueError:                       # 29 February into a common year
        return moment.replace(year=target, day=28)


def machine_name() -> str:
    """The Windows machine name, made safe to put in a certificate.

    See `sanitise_machine_name` for the rules.
    """
    return sanitise_machine_name(_raw_machine_name())


def _raw_machine_name() -> str:
    for source in (socket.gethostname, platform.node):
        try:
            value = str(source() or "").strip()
        except Exception:
            value = ""
        if value:
            return value
    return ""


def sanitise_machine_name(raw) -> str:
    """Letters, digits, hyphen, underscore and dot only (anything else becomes a
    hyphen); separators trimmed from the ends; bounded to the X.509 limits (a
    64-character common name, 63-character DNS labels, 253 characters in all). An
    empty result falls back to "lumasuite" rather than a certificate with no name.
    """
    text = re.sub(r"[^A-Za-z0-9._-]", "-", str(raw or "").strip())
    text = text.strip(".-_")
    labels = [label[:_LABEL_MAX].strip(".-_") for label in text.split(".")]
    text = ".".join(label for label in labels if label)[:_DNS_MAX]
    text = text[:_CN_MAX].strip(".-_")
    return text or "lumasuite"


def generation_available() -> bool:
    """Whether a self-signed certificate can be made on this build."""
    try:
        import cryptography  # noqa: F401
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- loading
def load_context(cert_path, key_path) -> ssl.SSLContext:
    """Build a server TLS context, or raise TlsError saying what is wrong.

    The distinctions matter to whoever has to fix it: a missing file, a file
    that is not a certificate, and a key that does not match the certificate
    are three different mistakes with three different remedies.
    """
    cert = Path(cert_path or "")
    key = Path(key_path or "")
    if not cert_path or not key_path:
        raise TlsError("No certificate and key have been chosen.")
    if not cert.exists():
        raise TlsError(f"The certificate file was not found: {cert.name}")
    if not key.exists():
        raise TlsError(f"The private key file was not found: {key.name}")

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    # No SSLv3, no TLS 1.0/1.1. Everything in use since 2020 speaks 1.2.
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        context.load_cert_chain(str(cert), str(key))
    except ssl.SSLError as exc:
        message = str(exc).lower()
        if "key values mismatch" in message or "mismatch" in message:
            raise TlsError("That private key does not match that certificate.")
        if "bad decrypt" in message or "password" in message:
            raise TlsError("That private key appears to be encrypted. Supply a "
                           "key without a passphrase.")
        raise TlsError("That certificate and key could not be read. Check they "
                       "are PEM files and that the key is not encrypted.")
    except (OSError, ValueError) as exc:
        raise TlsError(f"That certificate or key could not be read "
                       f"({type(exc).__name__}).")
    return context


def describe(cert_path, now=None) -> dict:
    """What Settings shows about a certificate. Never any key material.

    Read with the standard library so this works whether or not the generation
    dependency is present.
    """
    cert = Path(cert_path or "")
    if not cert_path or not cert.exists():
        return {"present": False}

    try:
        pem = cert.read_text(encoding="utf-8")
        der = ssl.PEM_cert_to_DER_cert(pem)
    except Exception as exc:
        return {"present": True, "readable": False,
                "error": f"Not a readable PEM certificate ({type(exc).__name__})."}

    described = {
        "present": True,
        "readable": True,
        # A fingerprint is safe to show and is how somebody confirms the
        # certificate their browser warned about is the one LumaSuite is using.
        "fingerprint": hashlib.sha256(der).hexdigest()[:32].upper(),
    }
    try:
        # _ssl has a decoder that needs no third-party library.
        import ssl as _ssl
        details = _ssl._ssl._test_decode_cert(str(cert))
        subject = dict(x[0] for x in details.get("subject", ()) if x)
        issuer = dict(x[0] for x in details.get("issuer", ()) if x)
        # The names the certificate is actually valid for.
        #
        # Every browser in use decides this from the subjectAltName extension
        # and ignores the common name entirely, so anything the application
        # says about whether a certificate matches an address has to be decided
        # from here too. Reporting the CN and comparing the CN would agree with
        # itself and disagree with the browser, which is the worst of both.
        described["san"] = [str(value) for _kind, value
                            in details.get("subjectAltName", ()) if value]
        # The addresses the certificate names, for messages that must say which
        # network it was made for now that the common name is the machine name.
        described["san_ips"] = [str(value) for kind, value
                                in details.get("subjectAltName", ())
                                if kind == "IP Address" and value]
        described.update({
            "subject": subject.get("commonName") or "",
            "organization": subject.get("organizationName") or "",
            "organizational_unit": subject.get("organizationalUnitName") or "",
            "issuer": issuer.get("commonName") or "",
            "not_after": details.get("notAfter") or "",
            "not_before": details.get("notBefore") or "",
            "self_signed": bool(subject) and subject == issuer,
        })
        if described["not_after"]:
            # The certificate's own timestamp is UTC; compare against an aware
            # UTC now rather than the deprecated naive utcnow().
            expires = datetime.datetime.strptime(
                described["not_after"], "%b %d %H:%M:%S %Y %Z").replace(
                    tzinfo=datetime.timezone.utc)
            current = now or datetime.datetime.now(datetime.timezone.utc)
            remaining = (expires - current).days
            described["days_remaining"] = remaining
            described["expired"] = remaining < 0
    except Exception as exc:
        log.info("[TLS] certificate details unavailable: %s", type(exc).__name__)
    return described


def covers(described, address) -> bool:
    """Whether a described certificate is valid for `address`.

    Decided from the subject alternative names and from nothing else. A
    certificate whose common name is the address but whose SAN is not will be
    rejected by every browser released this decade, so believing the common
    name here would produce an application that says "this is fine" about a
    connection the operator cannot open.

    A certificate whose SANs could not be read answers False. That is the fail-
    closed direction: it produces a visible "this may not match" rather than a
    silent claim that it does.
    """
    wanted = str(address or "").strip()
    if not wanted:
        return False
    names = [str(n).strip() for n in (described or {}).get("san") or []]
    if not names:
        return False
    try:
        target = ipaddress.ip_address(wanted)
    except ValueError:
        target = None
    for name in names:
        if name.lower() == wanted.lower():
            return True
        if target is not None:
            try:
                if ipaddress.ip_address(name) == target:
                    return True
            except ValueError:
                continue
    return False


def is_generated(described, cert_path, configured_cert_path="") -> bool:
    """Whether this is the certificate LumaSuite made, rather than a supplied one.

    A supplied certificate belongs to the site and is never touched. Only a
    certificate at LumaSuite's own path, with no supplied path configured, may
    be regenerated -- and even then only when an administrator asks.
    """
    if configured_cert_path:
        return False
    return bool((described or {}).get("present")) and bool(cert_path)


# ------------------------------------------------------------- generating
def generate_self_signed(address, cert_path, key_path,
                         years=GENERATED_VALIDITY_YEARS, name=None, now=None) -> dict:
    """Create a self-signed certificate for `address`.

    Identity: common name = the machine name; O = "Hall Research"; OU = "LumaSuite".
    Names: DNS:<machine name> and IP:<address> (the selected adapter's address).
    Validity: exactly `years` calendar years from `now` (the host clock, UTC, unless a
    test supplies one); NotBefore is five minutes earlier to tolerate clock skew.

    Honest about what this is worth: it stops passive interception, and it does
    not prove anything about who is answering. The first time a browser sees
    it, somebody has to accept a warning -- and that is the moment an attacker
    could substitute their own. On a trusted classroom network that is a
    reasonable trade; on anything else, supply a real certificate.
    """
    if not generation_available():
        raise TlsError(
            "This build cannot create a certificate. Supply a certificate and "
            "private key instead.")

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    text = str(address or "").strip()
    if not text:
        raise TlsError("An address is required to create a certificate.")

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    host = sanitise_machine_name(name) if name else machine_name()
    subject = x509.Name([
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, CERT_ORGANIZATION),
        x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME,
                           CERT_ORGANIZATIONAL_UNIT),
        x509.NameAttribute(NameOID.COMMON_NAME, host),
    ])

    # The address people actually type has to be in the SAN, or every modern
    # browser rejects it regardless of the common name. The machine name is
    # there as well, as a DNS name.
    alternatives = [x509.DNSName(host)]
    try:
        alternatives.append(x509.IPAddress(ipaddress.ip_address(text)))
    except ValueError:
        # Not an address: a name the caller wants covered, kept as before.
        if text.lower() != host.lower():
            alternatives.append(x509.DNSName(text))

    now = (now or datetime.datetime.now(datetime.timezone.utc)).replace(microsecond=0)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(add_calendar_years(now, years))
        .add_extension(x509.SubjectAlternativeName(alternatives), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None),
                       critical=True)
        .sign(key, hashes.SHA256())
    )

    cert_path = Path(cert_path)
    key_path = Path(key_path)
    cert_path.parent.mkdir(parents=True, exist_ok=True)

    # The key is written first and restrictively, then the certificate, so a
    # half-written pair never looks complete.
    key_bytes = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption())
    _write_private(key_path, key_bytes)
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))

    log.info("[TLS] generated a self-signed certificate for %s (%s)", text, host)
    return {"ok": True, "address": text, **describe(cert_path)}


def _write_private(path: Path, data: bytes) -> None:
    """Write key material as the owner only, where the platform allows it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # Create with a restrictive mode rather than widening it afterwards, so
    # there is no window in which the key is world-readable.
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise
    try:
        os.chmod(path, 0o600)
    except OSError:
        # Windows honours this only loosely; the file still lands in the
        # per-user data directory, which is the real protection there.
        pass


def restrict_existing(path) -> None:
    """Tighten a key file LumaSuite did not create, best effort."""
    try:
        os.chmod(str(path), 0o600)
    except OSError:
        pass
