"""Portable, encrypted configuration backup and disaster restore.

The problem this solves: the LumaSuite host is an ordinary PC. When it dies,
everything that made the deployment *this* deployment -- who may route what, what
each display is called, which displays form a group, the credentials the devices
answer to -- dies with it, and rebuilding it by hand means walking the site with a
notebook.

So a backup has to be portable: it must restore onto a *different* computer, which
rules out anything protected by DPAPI or bound to a machine. It carries device
credentials, so it must be encrypted and authenticated. And it must never be able
to lock a technician out of the machine in front of them.

WHAT IS IN IT, AND WHY
======================

Every persistent file was classified rather than swept up:

  configuration   the deployment's own decisions -- backed up
  machine-bound   valid only on the PC that wrote it -- carried, but applied
                  deliberately and reported, never written blindly
  regenerable     rediscovered by a scan or rebuilt on demand -- left out
  unsafe          would mislead or leak if moved -- left out, with a reason

    users.json              configuration   accounts, password hashes so passwords
                                            keep working, permissions, Direct
                                            Access records
    device_aliases.json     configuration   friendly names, keyed by MAC
    display_groups.json     configuration   display groups, members keyed by MAC
    producer_config.json    mixed           the device credentials are the whole
                                            point; the three folder paths are
                                            machine-bound
    remote_access.json      mixed           enabled/port/transport are
                                            configuration; the adapter and address
                                            name hardware that does not exist on
                                            the replacement PC
    video_walls.json        configuration   saved video walls
    producer_state.json     configuration   production studio settings
    units_cache.json        REDUCED         mostly volatile device state. Only the
                                            learned part is carried -- identity,
                                            name, model -- to be merged onto
                                            rebuilt records by MAC at the next scan.

    security_audit.json     unsafe          a record of what happened on the OLD
                                            machine; restoring it would make the
                                            new host's activity log claim events it
                                            never witnessed
    local_port.json         left out        the local application listener's fixed port (not
                                            Remote Access): a property of this
                                            PC's free ports, so it is not carried
    remote_cert.pem         unsafe          a certificate is valid for the names it
    remote_key.pem                          carries, which name the old machine's
                                            address; a private key does not travel
    *.tmp, *.corrupt        transient       leftovers

Sessions and CSRF tokens live in memory only, so there is nothing to exclude.

WHAT A RESTORE DOES TO EACH THING
=================================

The backup is authoritative for what it manages. Every piece of state is in exactly one of
three groups; the table is the contract, and `RESTORE_RULES` below holds the same words so a
test can compare them.

  REPLACED BY RESTORE   the file is replaced as a whole, so a record that exists now and is
                        not in the backup is gone afterwards -- accounts and permissions,
                        device names, display groups, saved video walls, Production Studio
                        settings, device sign-in. A section the backup's writer manages but
                        that was empty (no file) when the backup was made is REMOVED here too,
                        except the three below that must never be removed.
  PRESERVED LOCALLY     belongs to this computer or must never be deleted by a restore:
                        the folder paths, the network adapter and address, the certificate
                        paths (machine-bound keys of a replaced file); the whole of
                        accounts, device sign-in and remote access when the backup carries no
                        section for them (a restore never leaves the host without an
                        administrator, without device sign-in, or silently turns its own
                        listener settings off); the activity log; the certificate and key;
                        and EVERY section of a backup written before this rule existed (it
                        does not say what it managed, so its silence proves nothing).
  REGENERATED           rebuilt rather than restored: the discovered inventory is merged by
                        hardware identity at the next scan; Direct Access links are carried
                        by digest and cannot be displayed again; every sign-in session and
                        CSRF token ends with the process state it lived in.

A restore is one transaction: every file it can write is snapshotted first, the writes are
atomic, and a failure anywhere puts the snapshot back, so there is never a hybrid.

DIRECT ACCESS SURVIVES A DISASTER
=================================

A Direct Access record holds a `digest` -- SHA-256 of the bearer token, used to
recognise it -- and a `blob`, the token itself protected by the platform secret
store so an administrator can display the link again later. The digest is
machine-independent; the blob is not. So the backup carries the digest and drops the
blob: every deployed panel keeps working after the restore, because the token it
holds still matches. What is lost is only the ability to *show* the link again.
The raw token is deliberately NOT carried.

ENCRYPTION
==========

scrypt for the key, AES-256-GCM for the data, both from maintained implementations
-- `hashlib.scrypt` in the standard library and `cryptography`. Nothing here is
invented. The passphrase is checked for length only (8 characters); there are no
composition rules.

The header is plaintext on purpose: the format version, application version and
creation time have to be readable *before* anyone is asked for a passphrase. It is
also the GCM additional authenticated data, so altering a single byte of it makes
the tag fail and the file is refused rather than half-read.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import shutil
import threading
import time
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import luma_mac
import luma_store

#: The first line of every backup. A person looking at the file, and the
#: restore path, can both tell what it is without parsing anything.
MAGIC = "LUMASUITE-BACKUP"

#: Bumped when the payload's shape changes in a way an older reader could
#: misinterpret. A reader refuses a format from the future and migrates a
#: format from the past.
FORMAT_VERSION = 1

#: The oldest payload this build can still read and migrate forward.
MIN_SUPPORTED_FORMAT = 1

#: scrypt. The same work factor this project already uses for account
#: passwords, so there is one answer to "how hard is a guess here".
SCRYPT_N = 1 << 15
SCRYPT_R = 8
SCRYPT_P = 1
KEY_BYTES = 32
SALT_BYTES = 16
NONCE_BYTES = 12

#: Short enough not to be a nuisance, long enough that the scrypt work factor
#: is doing its job rather than covering for a four-character passphrase.
#:
#: Length is the only rule. No mandatory capital, digit or symbol: those push
#: people towards one predictable shape -- a word, a capital, a digit, an
#: exclamation mark -- and a passphrase nobody can remember gets written on
#: the box the backup is stored in, which is a worse outcome than a shorter
#: one. Letters, digits, punctuation and spaces are all accepted, and the
#: whole thing goes through scrypt either way.
MIN_PASSPHRASE = 8


class BackupError(Exception):
    """Something was wrong with the file, the passphrase, or the request.

    Carries a `reason` for a caller to branch on and a sentence for a person.
    One exception type for the whole module, so a stray ValueError escaping
    where callers expect this cannot surface as a 500 on a bad passphrase.
    """

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason
        self.message = message


# ======================================================================
# The container
# ======================================================================

def _derive(passphrase: str, salt: bytes) -> bytes:
    return hashlib.scrypt(passphrase.encode("utf-8"), salt=salt,
                          n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
                          dklen=KEY_BYTES, maxmem=64 * 1024 * 1024)


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(text: str, what: str) -> bytes:
    try:
        return base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError, TypeError):
        raise BackupError("corrupt",
                          "This file is damaged and cannot be read (%s)." % what)


def seal(payload: dict, passphrase: str, *, app_version: str = "",
         now: float = None) -> bytes:
    """Encrypt a payload into a backup file's bytes.

    The header is written first and is the additional authenticated data, so it
    cannot be edited without the tag failing.
    """
    if not isinstance(passphrase, str) or len(passphrase) < MIN_PASSPHRASE:
        raise BackupError(
            "weak_passphrase",
            "Choose a backup passphrase of at least %d characters. You will "
            "need it to restore, and nobody can recover it for you."
            % MIN_PASSPHRASE)
    # Whatever they chose is used as typed. Nothing is trimmed, folded or
    # rejected for its shape: a trailing space somebody meant is part of the
    # passphrase, and silently removing it would make the backup refuse the
    # very thing they typed to create it.

    salt = os.urandom(SALT_BYTES)
    nonce = os.urandom(NONCE_BYTES)
    header = {
        "format": FORMAT_VERSION,
        "lumasuite_version": app_version or "",
        "created_at": _timestamp(now),
        "cipher": "AES-256-GCM",
        "kdf": {"name": "scrypt", "n": SCRYPT_N, "r": SCRYPT_R,
                "p": SCRYPT_P, "salt": _b64(salt), "dklen": KEY_BYTES},
        "nonce": _b64(nonce),
    }
    # Sorted and compact so the bytes signed are the bytes written, whatever
    # dict ordering happens to be.
    header_line = json.dumps(header, sort_keys=True, separators=(",", ":"))
    aad = header_line.encode("utf-8")

    plaintext = json.dumps(payload, sort_keys=True).encode("utf-8")
    ciphertext = AESGCM(_derive(passphrase, salt)).encrypt(nonce, plaintext, aad)

    return b"\n".join([MAGIC.encode("ascii"), aad,
                       _b64(ciphertext).encode("ascii")]) + b"\n"


#: The header this application writes is about 300 bytes. Everything in it is parsed
#: before the passphrase is checked, so how much of it is read is the sender's choice
#: unless it is bounded.
MAX_HEADER_BYTES = 4096


def read_header(blob: bytes) -> dict:
    """The non-secret header, without needing the passphrase.

    Lets the UI refuse an incompatible or damaged file before prompting for
    something the operator would have typed for nothing.
    """
    if not isinstance(blob, (bytes, bytearray)) or not blob.strip():
        raise BackupError("corrupt", "That file is empty.")
    parts = bytes(blob).split(b"\n", 2)
    if len(parts) < 3 or parts[0].strip() != MAGIC.encode("ascii"):
        raise BackupError(
            "not_a_backup",
            "That is not an LumaSuite backup file.")
    if len(parts[1]) > MAX_HEADER_BYTES:
        raise BackupError("corrupt", "This backup file's header is damaged.")
    try:
        header = json.loads(parts[1].decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise BackupError("corrupt",
                          "This backup file's header is damaged.")
    if not isinstance(header, dict):
        raise BackupError("corrupt",
                          "This backup file's header is damaged.")

    fmt = header.get("format")
    if not isinstance(fmt, int):
        raise BackupError("corrupt",
                          "This backup file's header is damaged.")
    if fmt > FORMAT_VERSION:
        raise BackupError(
            "too_new",
            "This backup was written by a newer version of LumaSuite "
            "(backup format %d; this build reads up to %d). Restore it with "
            "that version, or upgrade this one." % (fmt, FORMAT_VERSION))
    if fmt < MIN_SUPPORTED_FORMAT:
        raise BackupError(
            "too_old",
            "This backup is in a format this build can no longer read "
            "(format %d)." % fmt)
    return header


def _bounded_kdf(kdf: dict, nonce: bytes) -> tuple:
    """(n, r, p, dklen) from the header, or BackupError if they are not ours to run.

    The key-derivation parameters sit in the plaintext header and are used BEFORE the
    passphrase is checked, so whoever supplies the file chooses how much work this
    process does. scrypt's memory cap does not bound the time: with r=1 the memory stays
    small while the cost grows linearly in p, and p=2**14 held a thread for minutes on a
    header that was otherwise well formed.

    A file this application wrote carries exactly the parameters it writes, so the bound
    is "no more expensive than what we write": a power-of-two N of at least 2**10, and
    n*r*p no larger than our own. The key and nonce sizes are fixed too -- a wrong size
    would otherwise escape as a bare ValueError from the cipher.
    """
    damaged = BackupError("corrupt", "This backup file's header is damaged.")
    try:
        n = int(kdf.get("n") or SCRYPT_N)
        r = int(kdf.get("r") or SCRYPT_R)
        p = int(kdf.get("p") or SCRYPT_P)
        dklen = int(kdf.get("dklen") or KEY_BYTES)
    except (TypeError, ValueError, OverflowError):
        raise damaged
    if (dklen != KEY_BYTES or len(nonce) != NONCE_BYTES
            or n < (1 << 10) or n & (n - 1) or r < 1 or p < 1
            or n * r * p > SCRYPT_N * SCRYPT_R * SCRYPT_P):
        raise damaged
    return n, r, p, dklen


def unseal(blob: bytes, passphrase: str) -> tuple:
    """(header, payload). Raises BackupError for every way it can go wrong."""
    header = read_header(blob)
    parts = bytes(blob).split(b"\n", 2)
    aad = parts[1]
    ciphertext = _unb64(parts[2].decode("ascii", "replace").strip(),
                        "the encrypted section")

    kdf = header.get("kdf")
    if not isinstance(kdf, dict) or kdf.get("name") != "scrypt":
        raise BackupError("corrupt",
                          "This backup uses an encryption method this build "
                          "does not know.")
    salt = _unb64(str(kdf.get("salt") or ""), "the key material")
    nonce = _unb64(str(header.get("nonce") or ""), "the key material")
    if len(salt) != SALT_BYTES or not nonce:
        raise BackupError("corrupt", "This backup file's header is damaged.")
    n, r, p, dklen = _bounded_kdf(kdf, nonce)

    try:
        key = hashlib.scrypt(
            (passphrase or "").encode("utf-8"), salt=salt,
            n=n, r=r, p=p, dklen=dklen, maxmem=64 * 1024 * 1024)
    except (ValueError, OverflowError, MemoryError, TypeError):
        # A header claiming absurd scrypt parameters is a damaged or hostile
        # file, not a reason to try to allocate it. Which exception an
        # out-of-range value raises depends on the interpreter and platform:
        # Python 3.12 on Windows raises TypeError for a value that does not fit
        # a C unsigned int, where other builds raise ValueError or OverflowError.
        raise BackupError("corrupt", "This backup file's header is damaged.")

    try:
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, aad)
    except InvalidTag:
        # One answer for a wrong passphrase and for a tampered file. They are
        # not distinguishable without weakening the authentication, and the
        # remedy an operator has is the same: check the passphrase, then check
        # the file came from where they think.
        raise BackupError(
            "cannot_decrypt",
            "That passphrase does not open this backup, or the file has been "
            "altered since it was written. Nothing has been changed.")

    try:
        payload = json.loads(plaintext.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise BackupError("corrupt",
                          "This backup decrypted but its contents are not "
                          "readable.")
    if not isinstance(payload, dict):
        raise BackupError("corrupt",
                          "This backup decrypted but its contents are not "
                          "readable.")
    return header, migrate(payload, header)


def migrate(payload: dict, header: dict) -> dict:
    """Bring an older supported payload up to the current shape.

    One place, so a reader never has to ask what an old file's section means.
    Format 1 is current and needs nothing; the function exists and is tested so
    that the next format has somewhere to go.
    """
    fmt = int(header.get("format") or FORMAT_VERSION)
    payload = dict(payload)
    if fmt < 1:                                   # pragma: no cover - refused
        raise BackupError("too_old", "This backup is too old to read.")
    payload.setdefault("sections", {})
    payload.setdefault("machine", {})
    payload["migrated_from"] = fmt
    payload["migrated_accounts"] = _migrate_routing_interfaces(payload)
    return payload


def _migrate_routing_interfaces(payload: dict) -> int:
    """Give an account that predates the routing-interface choice what it always had.

    A restricted account saved before Standard and Matrix routing were separate settings could
    route in the ordinary interface and had no Matrix, so it comes back with Standard on and
    Matrix off -- nobody loses the interface they use, and nobody is handed one they were never
    given. An account that already carries either setting is left exactly as written (a value
    that is present is the administrator's decision, whichever way it points). Administrators
    have no per-interface setting. Returns how many accounts were filled in.
    """
    users = (((payload.get("sections") or {}).get("accounts") or {}).get("users"))
    filled = 0
    if not isinstance(users, list):
        return filled
    for entry in users:
        if not isinstance(entry, dict) or entry.get("role") == "admin":
            continue
        permissions = entry.get("permissions")
        if not isinstance(permissions, dict):
            continue
        if "standard_routing" in permissions or "matrix_routing" in permissions:
            continue
        permissions["standard_routing"] = True
        permissions["matrix_routing"] = False
        filled += 1
    return filled


def _timestamp(now: float = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now or time.time()))


def suggested_filename(app_version: str = "", now: float = None) -> str:
    """A name that says what it is and when, and is legal on all three OSes."""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now or time.time()))
    version = "".join(c for c in (app_version or "") if c.isalnum() or c in "._-")
    middle = ("-" + version) if version else ""
    return "LumaSuite%s-backup-%s.lumabak" % (middle, stamp)



# ======================================================================
# What a deployment is made of
# ======================================================================
#
# Each entry says how a file is treated. `machine` fields are carried so the
# restore can REPORT them, and are stripped before anything is written, so a
# replacement PC never inherits an adapter GUID or a path that does not exist
# on it.

class Section:
    """How one file is carried.

    `on_absent` says what a restore does when the backup's writer managed this section but
    the backup carries none: "remove" (the backup is authoritative, so it was empty) or
    "keep" (never removed: removing it could lock the operator out or silently change this
    computer's own settings).
    """
    __slots__ = ("name", "filename", "machine_keys", "describe", "optional", "on_absent")

    def __init__(self, name, filename, machine_keys=(), describe=None,
                 optional=True, on_absent="remove"):
        self.name = name
        self.filename = filename
        self.machine_keys = tuple(machine_keys)
        self.describe = describe or (lambda data: "")
        self.optional = optional
        self.on_absent = on_absent


def _count(data, *keys):
    for key in keys:
        value = (data or {}).get(key)
        if isinstance(value, (list, dict)):
            return len(value)
    if isinstance(data, list):
        return len(data)
    return 0


SECTIONS = (
    # Accounts are part of the authoritative set: a backup that held none removes the ones on
    # this computer, and the operator is told so (and which) before anything is touched. The
    # local operator can always create an administrator again, because a request from this
    # computer is trusted by the socket it arrived on, not by a sign-in.
    Section("accounts", "users.json", on_absent="remove",
            describe=lambda d: "%d account(s)" % _count(d, "users")),
    Section("device_names", "device_aliases.json",
            describe=lambda d: "%d name(s)" % _count(d, "aliases")),
    Section("display_groups", "display_groups.json",
            describe=lambda d: "%d display group(s)" % _count(d, "groups")),
    # The three folders are paths on the old machine's disk.
    Section("device_credentials", "producer_config.json", on_absent="keep",
            machine_keys=("firmware_path", "imagestream_path", "logos_path"),
            describe=lambda d: "device sign-in for %s"
                               % ((d or {}).get("username") or "admin")),
    # The adapter and the address are hardware on the old machine.
    Section("remote_access", "remote_access.json", on_absent="keep",
            machine_keys=("adapter_id", "adapter_name", "address",
                          "cert_path", "key_path"),
            describe=lambda d: "remote access %s"
                               % ("on" if (d or {}).get("enabled") else "off")),
    Section("video_walls", "video_walls.json",
            describe=lambda d: "%d video wall(s)" % _count(d, "walls")),
    Section("production_studio", "producer_state.json"),
)

#: What a restore does to each thing, in the words an operator and a test can both read.
#: The module docstring says why; this is the same contract as data.
RESTORE_RULES = {
    "replaced": ("accounts and permissions", "device names", "display groups",
                 "saved video walls", "Production Studio settings", "device sign-in",
                 "remote access settings that are not machine-bound"),
    "removed_when_the_backup_has_none": tuple(
        s.name for s in SECTIONS if s.on_absent == "remove"),
    "preserved_locally": ("folder paths", "network adapter and address", "certificate paths",
                          "the activity log", "the certificate and private key",
                          "device sign-in and remote access when the backup has "
                          "no section for them", "everything a backup written before this "
                          "rule existed does not mention"),
    "regenerated": ("the discovered inventory (merged at the next scan)",
                    "Direct Access links (carried by digest, cannot be shown again)",
                    "sign-in sessions of accounts the restore removed or changed (the others stay)"),
}

#: Deliberately never exported. Each one has a reason, and the reasons are
#: different -- which is why this is a table and not a comment.
EXCLUDED = {
    "security_audit.json": "the old machine's activity record",
    "remote_cert.pem": "valid only for the old machine's address",
    "remote_key.pem": "a private key does not travel",
    "local_port.json": "this computer's local listener port; a replacement PC chooses its own",
    "units_cache.json": "mostly volatile; only the learned part is carried",
}

#: The learned part of a device record: expensive to rediscover, and keyed by
#: hardware identity so it can be merged onto whatever a rescan finds.
LEARNED_DEVICE_FIELDS = ("hostname", "model", "serialnumber", "type", "role")


def collect_devices(units) -> list:
    """Identity-keyed device facts worth carrying, and nothing else.

    `units_cache.json` is nearly half a megabyte of mostly volatile device
    state -- framerate options, HDCP lists, what was routed at the moment it
    was written. None of that is deployment configuration and all of it comes
    back on the next scan. What does not come back by itself is the credential
    a unit was learned to answer to.

    A record with no MAC is dropped rather than carried under its address: an
    address is not an identity, and a device later appearing at an old address
    must never inherit another unit's authorisation.
    """
    out = []
    for unit in units or []:
        if not isinstance(unit, dict):
            continue
        mac = _normal_mac(unit.get("mac"))
        if not mac:
            continue
        record = {"mac": mac}
        for field in LEARNED_DEVICE_FIELDS:
            value = unit.get(field)
            if value not in (None, ""):
                record[field] = value
        out.append(record)
    out.sort(key=lambda r: r["mac"])
    return out


def _normal_mac(raw) -> str:
    return luma_mac.normalise_mac(raw if isinstance(raw, str) else str(raw or ""))


def strip_direct_access_blobs(accounts: dict) -> int:
    """Drop the platform-protected copy of every Direct Access token.

    The digest stays, so a panel already holding its link keeps working after
    the restore -- which is the whole operational point. The blob cannot be
    decrypted on another machine anyway, and carrying the raw token instead
    would put a working passwordless credential in a file that leaves the
    building.

    Returns how many links were carried, so the restore can tell the
    administrator which ones they will no longer be able to re-display.
    """
    carried = 0
    for account in (accounts or {}).get("users") or []:
        record = account.get("direct_access")
        if not isinstance(record, dict):
            continue
        if record.get("enabled") and record.get("digest"):
            carried += 1
        record["blob"] = ""
        record["protection"] = "not-portable"
    return carried


def _read_text_locked(path: Path) -> str:
    """A configuration file's text, read under the file's own lock.

    Every write to these files is an atomic replace made under the same lock. On Windows a
    replace is refused while any other handle has the target open, so a read made without the
    lock (an export, or the confirmation screen) that overlaps a restore's write makes that
    write fail and the restore roll back for nothing. Reading under the lock waits for the
    replace to finish instead.
    """
    with luma_store.lock_for(path):
        return path.read_text(encoding="utf-8")


def build_payload(data_dir, units, app_version="") -> dict:
    """Read the deployment off disk and classify it as it goes."""
    data_dir = Path(data_dir)
    sections, machine, notes = {}, {}, []

    for section in SECTIONS:
        path = data_dir / section.filename
        if not path.exists():
            continue
        try:
            content = json.loads(_read_text_locked(path))
        except (ValueError, OSError, UnicodeDecodeError) as e:
            # A file that cannot be read is named rather than silently
            # skipped: a backup missing a section without saying so is worse
            # than one that refuses.
            raise BackupError(
                "unreadable_source",
                "%s could not be read, so this backup would be incomplete "
                "(%s)." % (section.filename, type(e).__name__))
        if not isinstance(content, dict):
            continue

        held = {}
        for key in section.machine_keys:
            if key in content:
                held[key] = content.pop(key)
        if held:
            machine[section.name] = held
        sections[section.name] = content

    links = strip_direct_access_blobs(sections.get("accounts") or {})
    if links:
        notes.append("%d Direct Access link(s) carried; the links keep "
                     "working but cannot be displayed again after a restore "
                     "onto another computer." % links)

    devices = collect_devices(units)
    return {
        "sections": sections,
        # What this writer manages. A section named here and absent from `sections` was empty
        # when the backup was made, which is what lets a restore remove the matching current
        # file. A backup without this key says nothing about absence.
        "managed": [section.name for section in SECTIONS],
        "machine": machine,
        "devices": devices,
        "notes": notes,
        "excluded": dict(EXCLUDED),
        "app_version": app_version,
    }


def sections_to_remove(payload: dict, data_dir=None) -> list:
    """Sections this restore will delete because the backup managed them and held none.

    Empty for a backup written before the backup said what it managed, and never contains
    a section whose rule is "keep". With `data_dir` only the ones that exist now are named,
    which is what an operator needs to be told; without it, every one that would apply.
    """
    managed = payload.get("managed")
    if not isinstance(managed, list):
        return []
    sections = payload.get("sections") or {}
    out = []
    for section in SECTIONS:
        if section.on_absent != "remove" or section.name not in managed:
            continue
        if section.name in sections:
            continue
        if data_dir is not None and not (Path(data_dir) / section.filename).exists():
            continue
        out.append(section.name)
    return out


def _account_key(entry) -> str:
    return str((entry or {}).get("username") or "").strip().lower()


def _account_sig(entry) -> tuple:
    """What an account holds that a restore would change for the person using it."""
    entry = entry or {}
    direct = entry.get("direct_access") if isinstance(entry.get("direct_access"), dict) else {}
    return (entry.get("password_hash"), entry.get("role"), bool(entry.get("enabled", True)),
            json.dumps(entry.get("permissions") or {}, sort_keys=True),
            (direct.get("digest") or "") if direct.get("enabled") else "")


def account_changes(payload: dict, data_dir=None) -> dict:
    """Which accounts a restore removes, adds and replaces, by name.

    Names only: an administrator can already list accounts, and nothing here is a hash, a
    token or a link. `replaced` means present in both with something that changes for the
    person (password, role, enabled, permissions or Direct Access link).
    """
    out = {"removed": [], "added": [], "replaced": [], "unchanged": 0}
    if data_dir is None:
        return out
    path = Path(data_dir) / "users.json"
    current = {}
    try:
        for entry in (json.loads(_read_text_locked(path)) or {}).get("users") or []:
            if isinstance(entry, dict) and _account_key(entry):
                current[_account_key(entry)] = entry
    except (OSError, ValueError, AttributeError):
        current = {}
    carried = ((payload.get("sections") or {}).get("accounts"))
    incoming = {}
    for entry in ((carried or {}).get("users") or []) if isinstance(carried, dict) else []:
        if isinstance(entry, dict) and _account_key(entry):
            incoming[_account_key(entry)] = entry
    names = {}
    for entry in list(current.values()) + list(incoming.values()):
        names[_account_key(entry)] = str(entry.get("username") or "")
    managed = payload.get("managed")
    # A backup that does not say what it manages says nothing about absence.
    absent_means_removal = isinstance(managed, list) and "accounts" in managed
    if carried is None and not absent_means_removal:
        return out
    for key in sorted(current):
        if key not in incoming:
            out["removed"].append(names[key])
    for key in sorted(incoming):
        if key not in current:
            out["added"].append(names[key])
        elif _account_sig(incoming[key]) != _account_sig(current[key]):
            out["replaced"].append(names[key])
        else:
            out["unchanged"] += 1
    return out


def summarise(payload: dict, header: dict = None, data_dir=None) -> dict:
    """What the administrator is shown before anything is touched."""
    sections = payload.get("sections") or {}
    rows = []
    for section in SECTIONS:
        if section.name not in sections:
            continue
        rows.append({"name": section.name,
                     "detail": section.describe(sections[section.name]) or ""})
    return {
        "created_at": (header or {}).get("created_at") or "",
        "lumasuite_version": (header or {}).get("lumasuite_version") or "",
        "format": (header or {}).get("format"),
        "sections": rows,
        "devices": len(payload.get("devices") or []),
        "notes": list(payload.get("notes") or []) + _restore_notes(payload, data_dir),
        "machine_specific": sorted((payload.get("machine") or {}).keys()),
        "excluded": payload.get("excluded") or dict(EXCLUDED),
        "will_remove": sections_to_remove(payload, data_dir),
        "accounts": account_changes(payload, data_dir),
    }


_REMOVE_LABELS = {"accounts": "accounts and permissions",
                  "device_names": "device names", "display_groups": "display groups",
                  "video_walls": "saved video walls",
                  "production_studio": "Production Studio settings"}


def _plural(n, one, many):
    return "%d %s" % (n, one if n == 1 else many)


def _restore_notes(payload: dict, data_dir) -> list:
    notes = []
    changes = account_changes(payload, data_dir)
    if changes["removed"]:
        notes.append("%s on this computer (%s) %s not in the backup and will be removed. Their "
                     "sign-in sessions and Direct Access links stop working."
                     % (_plural(len(changes["removed"]), "account", "accounts"),
                        ", ".join(changes["removed"]),
                        "is" if len(changes["removed"]) == 1 else "are"))
    if changes["replaced"]:
        notes.append("%s will be replaced by the backup's version (%s). A password, role, "
                     "permission or Direct Access link that differs changes back, and anyone "
                     "signed in as that account is signed out. An account that comes back "
                     "exactly as it was stays signed in."
                     % (_plural(len(changes["replaced"]), "account", "accounts"),
                        ", ".join(changes["replaced"])))
    gone = [name for name in sections_to_remove(payload, data_dir) if name != "accounts"]
    if gone:
        notes.append("This computer's %s are not in the backup and will be removed."
                     % ", ".join(_REMOVE_LABELS.get(name, name.replace("_", " ")) for name in gone))
    filled = payload.get("migrated_accounts")
    if isinstance(filled, int) and filled:
        notes.append("%d account%s from an earlier version will keep Standard routing."
                     % (filled, "" if filled == 1 else "s"))
    return notes


# ======================================================================
# Restore: snapshot, apply, and put it back if anything goes wrong
# ======================================================================

#: Written last, so a snapshot without one was interrupted and is not trusted.
MANIFEST = "snapshot-manifest.json"


def _atomic_copy(source, target) -> None:
    """Copy a file so the target is the old content or the new, never half of it.

    Unique temp file beside the target, fsync, os.replace: the project's one
    rule for writing a shared file, and the reason a rollback interrupted in the
    middle cannot truncate the very file it was recovering.
    """
    source, target = Path(source), Path(target)
    tmp = target.with_suffix(
        "%s.%d-%d.tmp" % (target.suffix, os.getpid(), threading.get_ident()))
    try:
        with open(source, "rb") as reader, open(tmp, "wb") as writer:
            shutil.copyfileobj(reader, writer)
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(tmp, target)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def snapshot(data_dir, into) -> list:
    """Copy every file the restore could touch, before it touches any.

    Not a nicety. A restore that fails halfway leaves a deployment that is
    half one site and half another, which is worse than either -- so the way
    back exists before there is anything to come back from.

    The folder is reused from one restore to the next, so whatever an earlier
    restore left in it is removed first: a rollback must never resurrect a file
    that was not there when *this* snapshot was taken. The manifest records
    which files were present, which is what lets the rollback also remove a
    file the restore created.
    """
    data_dir, into = Path(data_dir), Path(into)
    into.mkdir(parents=True, exist_ok=True)
    for stale in [section.filename for section in SECTIONS] + [MANIFEST]:
        try:
            (into / stale).unlink()
        except FileNotFoundError:
            pass
    saved = []
    for section in SECTIONS:
        source = data_dir / section.filename
        if source.exists():
            shutil.copy2(source, into / section.filename)
            saved.append(section.filename)
    _atomic_write_text(into / MANIFEST, json.dumps({"present": saved}))
    return saved


def _atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_suffix(
        "%s.%d-%d.tmp" % (path.suffix, os.getpid(), threading.get_ident()))
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _manifest(snapshot_dir: Path):
    """The set of files present when the snapshot was taken, or None."""
    try:
        data = json.loads((snapshot_dir / MANIFEST).read_text(encoding="utf-8"))
        present = data.get("present")
        if isinstance(present, list):
            return {str(name) for name in present}
    except (OSError, ValueError, AttributeError):
        pass
    return None


def rollback(snapshot_dir, data_dir, touched) -> tuple:
    """Put back exactly what was there. Returns (restored, failures).

    With a manifest, every file a restore can write is judged: one that was
    present is put back, one that was not is removed -- including a file the
    restore itself created, which `touched` cannot name because it only lists
    what existed beforehand. Without one (a snapshot taken by older code) the
    caller's list is all there is.
    """
    snapshot_dir, data_dir = Path(snapshot_dir), Path(data_dir)
    present = _manifest(snapshot_dir)
    names = ([section.filename for section in SECTIONS] if present is not None
             else list(touched))
    restored, failures = [], []
    for name in names:
        saved = snapshot_dir / name
        target = data_dir / name
        try:
            if present is not None and name in present and not saved.exists():
                failures.append("%s (snapshot copy missing)" % name)
            elif saved.exists() and (present is None or name in present):
                _atomic_copy(saved, target)
                restored.append(name)
            elif target.exists():
                # It did not exist before the restore, so putting it back
                # means removing it.
                target.unlink()
                restored.append(name)
        except OSError as e:
            failures.append("%s (%s)" % (name, type(e).__name__))
    return restored, failures


def apply_payload(payload: dict, data_dir, *, write_json) -> dict:
    """Write the restored configuration, or leave the installation alone.

    `write_json` is the application's own atomic writer, passed in rather than
    reimplemented: every write to a shared JSON file in this project is a
    unique temp file, fsync and os.replace under one lock, and a restore is
    not an excuse to open that file a second way.
    """
    data_dir = Path(data_dir)
    sections = payload.get("sections") or {}
    machine = payload.get("machine") or {}
    written, skipped, attention = [], [], []

    removed = []
    gone = set(sections_to_remove(payload))
    for section in SECTIONS:
        content = sections.get(section.name)
        if content is None:
            if section.name in gone:
                # The backup managed this and held none: it was empty then, so it is empty now.
                # Inside the transaction -- the snapshot has the file, so a later failure puts
                # it back.
                target = data_dir / section.filename
                if target.exists():
                    target.unlink()
                    removed.append(section.filename)
            skipped.append(section.name)
            continue
        content = json.loads(json.dumps(content))     # never share the payload

        # Machine-bound values are NOT written. The replacement PC keeps
        # whatever it already had -- its own adapter, its own paths -- and the
        # administrator is told which decisions did not travel.
        held = machine.get(section.name) or {}
        current = {}
        target = data_dir / section.filename
        if target.exists():
            try:
                existing = json.loads(target.read_text(encoding="utf-8"))
                if isinstance(existing, dict):
                    current = existing
            except (ValueError, OSError, UnicodeDecodeError):
                current = {}
        for key in section.machine_keys:
            if key in current:
                content[key] = current[key]
            if key in held:
                attention.append({
                    "section": section.name, "setting": key,
                    "was": _readable(held[key]),
                    "now": _readable(current.get(key)),
                })

        write_json(target, content)
        written.append(section.filename)

    return {"written": written, "skipped": skipped, "removed": removed, "attention": attention}


def _readable(value) -> str:
    """Machine-specific values are shown to an administrator, so no secrets.

    Nothing in `machine_keys` is a credential, and this is the only place a
    carried value is rendered -- so it stays a small, obvious function rather
    than a formatting call scattered about.
    """
    if value in (None, ""):
        return "not set"
    return str(value)[:120]


def merge_devices(payload: dict, units) -> tuple:
    """Put learned device facts back onto whatever a scan has found.

    Matched by hardware identity and never by address. A unit that has moved
    to a new address is still that unit; a different unit that has taken the
    old address is not, and must not inherit anything.

    Returns (units, matched, unmatched) -- unmatched being equipment the
    backup knew about that this installation has not seen yet, which is
    ordinary immediately after a restore and before the first scan.
    """
    remembered = {r["mac"]: r for r in (payload.get("devices") or [])
                  if isinstance(r, dict) and r.get("mac")}
    units = [dict(u) for u in (units or []) if isinstance(u, dict)]
    matched = 0
    for unit in units:
        mac = _normal_mac(unit.get("mac"))
        record = remembered.get(mac)
        if not record:
            continue
        matched += 1
        for field, value in record.items():
            if field == "mac":
                continue
            # Live data wins where the scan actually learned something; the
            # backup fills what it did not.
            if unit.get(field) in (None, ""):
                unit[field] = value
    return units, matched, len(remembered) - matched



def validate(payload: dict) -> list:
    """Everything wrong with this payload, before anything is written.

    Returns a list of sentences -- all of them, not the first -- because an
    operator fixing a backup wants to know what is wrong with it rather than
    discovering the next problem on the next attempt.

    This is deliberately about *structure and relationships*, not taste. A
    backup with no Multiview layouts is perfectly valid; one whose accounts
    are not a list is not, and would fail halfway through writing.
    """
    problems = []
    if not isinstance(payload.get("sections"), dict):
        return ["the backup has no configuration in it"]

    sections = payload["sections"]

    accounts = sections.get("accounts")
    if accounts is not None:
        users = (accounts or {}).get("users")
        if not isinstance(users, list):
            problems.append("the account list is damaged")
        else:
            seen_ids, seen_names = set(), set()
            for entry in users:
                if not isinstance(entry, dict):
                    problems.append("an account record is damaged")
                    break
                for field in ("id", "username", "password_hash", "role"):
                    if not entry.get(field):
                        problems.append(
                            "an account is missing its %s"
                            % ("sign-in name" if field == "username"
                               else field.replace("_", " ")))
                        break
                else:
                    # Duplicates would silently collapse on load, taking one
                    # person's permissions with them.
                    if entry["id"] in seen_ids:
                        problems.append("two accounts share an identifier")
                    if entry["username"].lower() in seen_names:
                        problems.append("two accounts share the sign-in name "
                                        "%r" % entry["username"])
                    seen_ids.add(entry["id"])
                    seen_names.add(entry["username"].lower())

    groups = sections.get("display_groups")
    if groups is not None:
        entries = (groups or {}).get("groups")
        if not isinstance(entries, list):
            problems.append("the display group list is damaged")
        else:
            seen_groups = set()
            for entry in entries:
                if not isinstance(entry, dict) or not entry.get("id") or not entry.get("name"):
                    problems.append("a display group record is damaged")
                    break
                if entry["id"] in seen_groups:
                    problems.append("two display groups share an identifier")
                seen_groups.add(entry["id"])
                members = entry.get("members")
                if not isinstance(members, list) or any(not _normal_mac(m) for m in members):
                    problems.append("a display group lists a display with no hardware identity")
                    break

    devices = payload.get("devices")
    if devices is not None and not isinstance(devices, list):
        problems.append("the device list is damaged")
    elif isinstance(devices, list):
        for record in devices:
            if not isinstance(record, dict) or not _normal_mac(record.get("mac")):
                # A device record without a usable hardware identity cannot be
                # matched to anything, and matching it by address is exactly
                # what must never happen.
                problems.append("a device record has no hardware identity")
                break

    for key in ("machine", "sections"):
        if key in payload and not isinstance(payload[key], dict):
            problems.append("the backup's %s section is damaged" % key)

    # Every section must be an object; a list or a string here would be
    # written to disk and then fail to load on the next start.
    for name, content in sections.items():
        if not isinstance(content, dict):
            problems.append("the %s section is damaged"
                            % name.replace("_", " "))

    # Deduplicate while keeping the order they were found in.
    out, seen = [], set()
    for problem in problems:
        if problem not in seen:
            seen.add(problem)
            out.append(problem)
    return out
