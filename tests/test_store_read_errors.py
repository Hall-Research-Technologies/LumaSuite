"""A file that could not be READ is not a file that is CORRUPT.

`luma_store._read_unlocked` (and the same pattern in the accounts, device-name and port stores)
quarantined -- renamed away -- a file on ANY exception, including a transient OSError such as a
sharing violation while another program (an anti-virus scan, a backup tool) held it open. A good
file was thereby moved out of the way and the application carried on with nothing. Only a file whose
CONTENTS cannot be parsed is quarantined now; a read error is retried, and when it persists the file
is left exactly where it is.

Fences: scratch directories only; nothing here reaches a device or the network.
"""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _support  # noqa: F401  (fence)

import luma_aliases
import luma_portcontrol
import luma_store
import luma_users


def unreadable(path, times=None):
    """Make reading `path` raise PermissionError -- `times` times, or always -- as another program
    holding the file open would. Only reads of that one file are affected, and mock puts the
    original methods back (a test that replaces a name must restore it)."""
    state = {"n": 0}
    real_text, real_bytes = Path.read_text, Path.read_bytes

    def fails(self):
        return Path(self) == Path(path) and (times is None or state["n"] < times)

    def read_text(self, *a, **k):
        if fails(self):
            state["n"] += 1
            raise PermissionError(13, "sharing violation")
        return real_text(self, *a, **k)

    def read_bytes(self, *a, **k):
        if fails(self):
            state["n"] += 1
            raise PermissionError(13, "sharing violation")
        return real_bytes(self, *a, **k)
    stack = mock.patch.multiple(Path, read_text=read_text, read_bytes=read_bytes)
    stack.state = state
    return stack


class Scratch(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="luma-store-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        # no pause between retries in a test (created, so the suite also runs against code without it)
        p = mock.patch.object(luma_store, "READ_PAUSE", 0, create=True)
        p.start()
        self.addCleanup(p.stop)

    def write(self, name, text):
        path = self.dir / name
        path.write_text(text, encoding="utf-8")
        return path

    def corrupt(self, path):
        return Path(str(path) + ".corrupt").exists() or path.with_suffix(".corrupt").exists()


class LumaStoreReads(Scratch):

    def test_a_transient_read_error_is_retried_and_the_file_is_kept(self):
        path = self.write("a.json", '{"x": 1}')
        with unreadable(path, times=2) as _:
            result = luma_store.read_json(path, default="DEFAULT")
        self.assertEqual(result, {"x": 1}, "a read that failed twice and then worked gave up")
        self.assertTrue(path.exists())
        self.assertFalse(self.corrupt(path))

    def test_a_persistent_read_error_leaves_the_file_where_it_is(self):
        path = self.write("a.json", '{"x": 1}')
        with unreadable(path):
            self.assertEqual(luma_store.read_json(path, default="DEFAULT"), "DEFAULT")
        self.assertTrue(path.exists(), "a file that merely could not be read was renamed away")
        self.assertFalse(self.corrupt(path))
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"x": 1})

    def test_unparseable_contents_are_still_quarantined(self):
        path = self.write("a.json", "{ not json")
        self.assertEqual(luma_store.read_json(path, default="D"), "D")
        self.assertFalse(path.exists())
        self.assertTrue(self.corrupt(path))

    def test_contents_that_are_not_utf8_are_still_quarantined(self):
        path = self.dir / "a.json"
        path.write_bytes(b"\xff\xfe\x00bad")
        self.assertEqual(luma_store.read_json(path, default="D"), "D")
        self.assertTrue(self.corrupt(path))

    def test_a_missing_file_is_the_default_and_nothing_is_created(self):
        path = self.dir / "none.json"
        self.assertEqual(luma_store.read_json(path, default=[]), [])
        self.assertFalse(path.exists())

    def test_update_json_does_not_write_over_a_file_it_could_not_read(self):
        path = self.write("a.json", '{"keep": "me"}')
        called = []
        with unreadable(path):
            ok = luma_store.update_json(path, lambda data: called.append(data) or {"replaced": True},
                                        default={})
        self.assertFalse(ok)
        self.assertEqual(called, [], "the mutation ran on a default it was not meant to see")
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"keep": "me"})

    def test_update_json_still_works_normally(self):
        path = self.write("a.json", '{"n": 1}')
        self.assertTrue(luma_store.update_json(path, lambda d: {"n": d["n"] + 1}, default={}))
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"n": 2})


class OtherStoresShareTheRule(Scratch):

    def test_accounts_file_that_cannot_be_read_is_not_renamed_away(self):
        path = self.write("users.json", json.dumps({"users": []}))
        with unreadable(path):
            luma_users.UserStore(path, self.dir / "audit.json")
        self.assertTrue(path.exists())
        self.assertFalse(self.corrupt(path))

    def test_accounts_file_with_bad_contents_is_still_quarantined(self):
        path = self.write("users.json", "{ nope")
        luma_users.UserStore(path, self.dir / "audit.json")
        self.assertFalse(path.exists())
        self.assertTrue(self.corrupt(path))

    def test_aliases_file_that_cannot_be_read_is_not_renamed_away(self):
        path = self.write("aliases.json", json.dumps({"aliases": {}}))
        with unreadable(path):
            luma_aliases.AliasStore(path)
        self.assertTrue(path.exists())
        self.assertFalse(self.corrupt(path))

    def test_aliases_file_with_bad_contents_is_still_quarantined(self):
        path = self.write("aliases.json", "[[[")
        luma_aliases.AliasStore(path)
        self.assertFalse(path.exists())
        self.assertTrue(self.corrupt(path))

    def test_port_settings_that_cannot_be_read_are_not_renamed_away(self):
        path = self.write(luma_portcontrol.FILE_NAME, json.dumps({"mode": "automatic"}))
        settings = luma_portcontrol.PortSettings(self.dir)
        with unreadable(path):
            result = settings.load()
        self.assertEqual(result.mode, luma_portcontrol.MODE_AUTOMATIC)
        self.assertTrue(path.exists())
        self.assertFalse(self.corrupt(path))

    def test_port_settings_with_bad_contents_are_still_quarantined(self):
        path = self.write(luma_portcontrol.FILE_NAME, "garbage")
        luma_portcontrol.PortSettings(self.dir).load()
        self.assertFalse(path.exists())
        self.assertTrue(self.corrupt(path))


if __name__ == "__main__":
    unittest.main()
