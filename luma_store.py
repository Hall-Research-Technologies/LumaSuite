"""Shared persistence for LumaSuite's JSON state files.

Every write to a shared JSON file is atomic and serialised: a unique temporary
file, fsync, then `os.replace`, all under one lock per target file. Nothing is
ever truncated in place, so a crash mid-write leaves the previous file intact.

A file that cannot be parsed is *quarantined* -- renamed beside itself with a
`.corrupt` suffix -- and treated as empty, so a damaged file can never crash
discovery or hand corrupt data to the rest of the application, and the evidence
is kept for someone to look at.

`update_json` holds the file's lock across the whole read-modify-write. Two
individually locked operations do not compose into an atomic one: concurrent
writers that each load their own copy and save would silently discard each
other's changes.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

_registry_guard = threading.Lock()
_locks: dict = {}


def lock_for(path) -> threading.RLock:
    """The one lock that serialises every access to this file in this process."""
    key = os.path.abspath(str(path))
    with _registry_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = _locks[key] = threading.RLock()
        return lock


def _write_unlocked(path: Path, payload, indent=2, private=False) -> bool:
    tmp = path.with_name(f"{path.name}.{os.getpid()}-{threading.get_ident()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=indent)
            handle.flush()
            os.fsync(handle.fileno())
        if private:
            # Credentials: narrow the file before it takes the real name, so it is
            # never readable by other accounts even for an instant.
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
        os.replace(tmp, path)
        return True
    except Exception as exc:
        log.error("[STORE] could not write %s: %s", path.name, type(exc).__name__)
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        return False


def atomic_write_json(path, payload, indent=2, private=False) -> bool:
    """Write `payload` to `path` atomically. Returns False (and logs) on failure.

    `private` narrows the file to its owner (a no-op where the platform has no such
    mode), for files that hold credentials.
    """
    path = Path(path)
    with lock_for(path):
        return _write_unlocked(path, payload, indent, private)


def quarantine(path) -> None:
    """Move an unreadable file aside, keeping it for inspection."""
    path = Path(path)
    target = path.with_name(path.name + ".corrupt")
    try:
        os.replace(path, target)
    except OSError:
        pass


#: A read that fails for a reason other than the file's contents (a sharing violation while
#: another program holds it, a momentary permission error) says nothing about the file.
READ_ATTEMPTS = 3
READ_PAUSE = 0.05


class ReadFailed(OSError):
    """The file could not be read for a reason that is not its contents."""


def read_text_retrying(path: Path) -> str:
    """The file's text. A read changes nothing, so a transient OSError is simply tried again.

    Raises ReadFailed (an OSError) once the attempts are used up, and lets a decoding error
    through untouched: that one IS about the contents.
    """
    last = None
    for attempt in range(READ_ATTEMPTS):
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise
        except OSError as exc:
            last = exc
            if attempt + 1 < READ_ATTEMPTS:
                time.sleep(READ_PAUSE)
    raise ReadFailed(str(last)) from last


def _read_unlocked(path: Path, default, strict=False):
    """The parsed file, `default` when it is absent, or `default` after quarantining it when it
    is unparseable.

    Only a file whose CONTENTS cannot be parsed is quarantined. A file that merely could not be
    read (after retries) is left exactly where it is: renaming a good file because the disk or
    an anti-virus scanner hiccuped would turn a momentary error into permanent data loss. With
    `strict`, that case raises ReadFailed instead of returning `default`, so a caller about to
    write on top of what it read does not write over a file it never saw.
    """
    if not path.exists():
        return default
    try:
        return json.loads(read_text_retrying(path))
    except ReadFailed as exc:
        log.error("[STORE] %s could not be read (%s); leaving it in place", path.name,
                  type(exc.__cause__ or exc).__name__)
        if strict:
            raise
        return default
    except Exception as exc:
        log.error("[STORE] %s is unreadable (%s); quarantining", path.name, type(exc).__name__)
        quarantine(path)
        return default


def read_json(path, default=None):
    """Read a JSON file; a missing file gives `default`, a malformed one is quarantined."""
    path = Path(path)
    with lock_for(path):
        return _read_unlocked(path, default)


def update_json(path, mutate, default=None) -> bool:
    """Read, let `mutate(data)` return the new value, write -- all under one lock.

    `mutate` receives the current contents (or `default`) and returns what to
    store. Returning None leaves the file untouched and reports False.
    """
    path = Path(path)
    with lock_for(path):
        try:
            current = _read_unlocked(path, default, strict=True)
        except ReadFailed:
            return False
        updated = mutate(current)
        if updated is None:
            return False
        return _write_unlocked(path, updated)
