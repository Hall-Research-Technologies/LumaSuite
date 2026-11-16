"""Friendly device names, owned by LumaSuite and keyed by stable identity.

A teacher's phone should say "Document Camera", not "at-example-111-00001". That
is a labelling problem, and it is deliberately *only* a labelling problem:

  * Nothing here writes to a device. The OmniStream hostname is untouched, and
    an alias never becomes a device configuration change.
  * Nothing here participates in authorization or routing. Permissions are
    keyed by the normalised hardware identity and stay that way; an alias is a
    string hung off that identity, and renaming one moves no boundary.

The second point is the one worth being careful about. If an alias could ever
be resolved back to a device, two accounts could be given the same alias and
one would reach the other's equipment. So the mapping is one-directional by
construction -- identity to label -- and there is no lookup by name anywhere in
this module. The tests assert that property rather than trusting the comment.

Stored centrally on the host so every browser sees the same names, keyed by
identity so an address change does not lose them, and written with the
atomic-replace-under-one-lock rule the rest of the project uses.
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

import luma_store
from luma_users import _atomic_write_json, _normalise_identity

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

#: A label is a label. Long enough to be useful, short enough to fit a phone.
MAX_ALIAS_LENGTH = 48


def clean_alias(text) -> str:
    """Normalise a submitted alias, or return "" for anything unusable.

    Control characters are stripped rather than escaped: this string is
    rendered by several surfaces, and the only safe assumption is that one of
    them will eventually interpolate it somewhere it should not. Trimming the
    input is the cheap half of that; every renderer using textContent is the
    other half.
    """
    if text is None:
        return ""
    collapsed = " ".join(str(text).split())
    printable = "".join(ch for ch in collapsed if ch.isprintable())
    return printable[:MAX_ALIAS_LENGTH].strip()


class AliasStore:
    """identity -> friendly name. Nothing else, and nothing in reverse."""

    def __init__(self, path: Path):
        self._path = Path(path)
        self._lock = threading.RLock()
        self._aliases: dict[str, str] = {}
        self._load()

    # ---- persistence -------------------------------------------------
    def _load(self) -> None:
        with self._lock:
            self._aliases = {}
            if not self._path.exists():
                return
            try:
                raw = json.loads(luma_store.read_text_retrying(self._path))
            except luma_store.ReadFailed as exc:
                log.error("[ALIAS] %s could not be read (%s); leaving it in place",
                          self._path.name, type(exc.__cause__ or exc).__name__)
                return
            except Exception as exc:
                # A broken labels file must never stop the application: the
                # worst case is that devices show their hostnames again.
                log.error("[ALIAS] %s is unreadable (%s); quarantining",
                          self._path.name, type(exc).__name__)
                try:
                    self._path.replace(self._path.with_suffix(".corrupt"))
                except OSError:
                    pass
                return
            for identity, alias in ((raw or {}).get("aliases") or {}).items():
                key = _normalise_identity(identity)
                label = clean_alias(alias)
                if key and label:
                    self._aliases[key] = label

    def _persist(self) -> bool:
        with self._lock:
            return _atomic_write_json(self._path, {
                "version": SCHEMA_VERSION,
                "aliases": dict(self._aliases),
            })

    # ---- reading -----------------------------------------------------
    def alias_for(self, identity) -> str:
        """The configured label for a device, or "" if it has none."""
        key = _normalise_identity(identity)
        if not key:
            return ""
        with self._lock:
            return self._aliases.get(key, "")

    def display_name(self, identity, fallback) -> str:
        """What to show. The alias when there is one, otherwise the fallback.

        The fallback is the name the rest of LumaSuite already uses, so a
        device with no alias reads exactly as it did before this existed.
        """
        return self.alias_for(identity) or (str(fallback or "").strip()
                                            or "Unnamed device")

    def all(self) -> dict:
        with self._lock:
            return dict(self._aliases)

    def duplicates(self) -> dict:
        """Labels used by more than one device, as {label: [identities]}.

        Not prohibited -- two rooms may genuinely each have a "Front Display" --
        but an administrator assigning permissions from a list of identical
        names is choosing blind, so the UI is told and says so.
        """
        seen: dict[str, list] = {}
        with self._lock:
            for identity, alias in self._aliases.items():
                seen.setdefault(alias.casefold(), []).append(identity)
        return {alias: ids for alias, ids in seen.items() if len(ids) > 1}

    # ---- writing -----------------------------------------------------
    def set_alias(self, identity, alias) -> dict:
        """Name a device, or clear its name with an empty string."""
        key = _normalise_identity(identity)
        if not key:
            return {"ok": False, "error": "a device identity is required"}
        label = clean_alias(alias)
        with self._lock:
            if label:
                self._aliases[key] = label
            else:
                self._aliases.pop(key, None)
            self._persist()
        return {"ok": True, "id": key, "alias": label}

    def set_many(self, mapping) -> dict:
        """Apply several at once, so one save covers an editing session."""
        if not isinstance(mapping, dict):
            return {"ok": False, "error": "expected a mapping"}
        applied = 0
        with self._lock:
            for identity, alias in mapping.items():
                key = _normalise_identity(identity)
                if not key:
                    continue
                label = clean_alias(alias)
                if label:
                    self._aliases[key] = label
                else:
                    self._aliases.pop(key, None)
                applied += 1
            self._persist()
        return {"ok": True, "applied": applied}

    def forget_many(self, identities) -> dict:
        """Drop the names of devices that are no longer known, all-or-nothing."""
        with self._lock:
            before = dict(self._aliases)
            gone = {_normalise_identity(i) for i in identities if i}
            removed = [k for k in gone if k in self._aliases]
            for key in removed:
                del self._aliases[key]
            if removed and not self._persist():
                self._aliases = before
                return {"ok": False, "removed": 0, "before": before}
            return {"ok": True, "removed": len(removed), "before": before}

    def restore(self, before) -> None:
        with self._lock:
            self._aliases = dict(before)
            self._persist()

    def forget(self, identity) -> dict:
        return self.set_alias(identity, "")
