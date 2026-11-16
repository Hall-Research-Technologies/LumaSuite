"""A backup header chooses how much work the restore does, so it is bounded first.

The key-derivation parameters are in the plaintext header and are used before the
passphrase is checked. scrypt's memory ceiling does not bound its time: with r=1 the
memory stays small and the cost grows linearly in p, so a header that was otherwise
well formed (n=2**14, r=1, p=2**14) held a thread for minutes -- measured at about
0.9 s for p=64 and 3.7 s for p=256 before this was bounded.

A file this application wrote carries exactly the parameters it writes, so the limit
is "no more expensive than ours". These tests assert that the work is refused without
being started (scrypt is replaced by something that fails the test if called) and that
every refusal is the module's own error, never a stray ValueError from the cipher.
"""
from __future__ import annotations

import base64
import json
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "tests"))

import _support                                           # noqa: E402,F401  (the fence)
import luma_backup as backup                              # noqa: E402

PASSPHRASE = "a-long-enough-backup-passphrase"


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


class Bounds(unittest.TestCase):

    PRODUCT = "lumasuite"

    @classmethod
    def setUpClass(cls):
        cls.sealed = backup.seal({"hello": "world"}, PASSPHRASE)

    def with_header(self, edit) -> bytes:
        """The sealed file with its header changed. The tag will not match; the point
        is that the refusal comes before anything is derived."""
        magic, header_line, body = self.sealed.split(b"\n", 2)
        header = json.loads(header_line)
        edit(header)
        line = json.dumps(header, sort_keys=True, separators=(",", ":")).encode()
        return b"\n".join([magic, line, body])

    def refused_without_work(self, blob):
        started = time.monotonic()
        with mock.patch.object(backup.hashlib, "scrypt",
                               side_effect=AssertionError("scrypt was started")):
            with self.assertRaises(backup.BackupError) as raised:
                backup.unseal(blob, PASSPHRASE)
        self.assertEqual(raised.exception.reason, "corrupt")
        self.assertLess(time.monotonic() - started, 2.0)

    def test_an_untouched_file_still_opens(self):
        header, payload = backup.unseal(self.sealed, PASSPHRASE)
        self.assertEqual(payload.get("hello"), "world")

    def test_a_cost_above_our_own_is_refused_without_being_started(self):
        for name, edit in {
            "p": lambda h: h["kdf"].update(n=2 ** 14, r=1, p=2 ** 14),
            "p one more": lambda h: h["kdf"].update(p=2),
            "r": lambda h: h["kdf"].update(r=16),
            "n": lambda h: h["kdf"].update(n=2 ** 16),
            "n huge": lambda h: h["kdf"].update(n=2 ** 40),
        }.items():
            with self.subTest(name=name):
                self.refused_without_work(self.with_header(edit))

    def test_parameters_that_are_not_scrypt_parameters_are_refused(self):
        for name, edit in {
            "n not a power of two": lambda h: h["kdf"].update(n=3000),
            "n too small": lambda h: h["kdf"].update(n=2),
            "r zero is the default, negative is not": lambda h: h["kdf"].update(r=-1),
            "p negative": lambda h: h["kdf"].update(p=-4),
            "n text": lambda h: h["kdf"].update(n="many"),
            "n list": lambda h: h["kdf"].update(n=[1]),
            "p float": lambda h: h["kdf"].update(p=1.5e300),
        }.items():
            with self.subTest(name=name):
                self.refused_without_work(self.with_header(edit))

    def test_a_key_or_nonce_of_the_wrong_size_is_the_modules_own_error(self):
        for name, edit in {
            "dklen 16": lambda h: h["kdf"].update(dklen=16),
            "dklen 64": lambda h: h["kdf"].update(dklen=64),
            "dklen huge": lambda h: h["kdf"].update(dklen=2 ** 31),
            "nonce 8 bytes": lambda h: h.update(nonce=b64(b"\0" * 8)),
            "nonce 64 bytes": lambda h: h.update(nonce=b64(b"\0" * 64)),
        }.items():
            with self.subTest(name=name):
                self.refused_without_work(self.with_header(edit))

    def test_a_kdf_that_is_not_an_object_is_the_modules_own_error(self):
        for value in (["scrypt"], "scrypt", 5, [], True):
            with self.subTest(value=value):
                self.refused_without_work(
                    self.with_header(lambda h, v=value: h.update(kdf=v)))

    def test_a_salt_of_any_other_size_is_refused_without_work(self):
        for size in (0, 1, 8, 15, 17, 64, 4096, 1024 * 1024):
            with self.subTest(size=size):
                self.refused_without_work(self.with_header(
                    lambda h, n=size: h["kdf"].update(salt=b64(b"x" * n))))

    def test_a_salt_that_is_not_base64_is_the_modules_own_error(self):
        for value in ("!!!not base64!!!", "", None, 7, ["a"], {"a": 1}):
            with self.subTest(value=value):
                self.refused_without_work(self.with_header(
                    lambda h, v=value: h["kdf"].update(salt=v)))

    def test_an_oversized_header_is_refused_before_it_is_parsed(self):
        """The header is read before the passphrase is checked, so its size is bounded."""
        for name, edit in {
            "a megabyte of metadata": lambda h: h.update(padding="x" * 1024 * 1024),
            "a long version string": lambda h: h.update(**{"%s_version" % self.PRODUCT: "9" * 100000}),
            "many keys": lambda h: h.update({"k%d" % i: i for i in range(5000)}),
        }.items():
            with self.subTest(name=name):
                blob = self.with_header(edit)
                with mock.patch.object(backup.json, "loads",
                                       side_effect=AssertionError("the header was parsed")):
                    with self.assertRaises(backup.BackupError) as raised:
                        backup.read_header(blob)
                self.assertEqual(raised.exception.reason, "corrupt")

    def test_a_header_this_application_writes_is_far_below_the_bound(self):
        header_line = self.sealed.split(b"\n", 2)[1]
        self.assertLess(len(header_line), backup.MAX_HEADER_BYTES // 8)

    def test_extreme_numbers_are_the_modules_own_error_and_start_nothing(self):
        for name, value in {
            "four thousand digits": "9" * 4000,
            "five thousand digits": "9" * 5000,
            "float inf": float("inf"),
            "float nan": float("nan"),
            "negative huge": -(2 ** 200),
            "nested": {"n": 1},
        }.items():
            for field in ("n", "r", "p", "dklen"):
                with self.subTest(name=name, field=field):
                    self.refused_without_work(self.with_header(
                        lambda h, f=field, v=value: h["kdf"].update({f: v})))

    def test_a_zero_or_true_is_never_more_work_than_ours(self):
        """Zero means "the default" and True is 1: lenient, but never costlier."""
        for field in ("n", "r", "p"):
            for value in (0, True):
                with self.subTest(field=field, value=value):
                    seen = []
                    real = backup.hashlib.scrypt

                    def watching(*args, **kwargs):
                        seen.append(kwargs)
                        return real(*args, **kwargs)

                    blob = self.with_header(
                        lambda h, f=field, v=value: h["kdf"].update({f: v}))
                    with mock.patch.object(backup.hashlib, "scrypt", watching):
                        with self.assertRaises(backup.BackupError):
                            backup.unseal(blob, PASSPHRASE)
                    for call in seen:
                        self.assertLessEqual(
                            call["n"] * call["r"] * call["p"],
                            backup.SCRYPT_N * backup.SCRYPT_R * backup.SCRYPT_P)

    def test_the_nonce_is_bounded_in_every_direction(self):
        for size in (0, 1, 11, 13, 16, 4096, 1024 * 1024):
            with self.subTest(size=size):
                self.refused_without_work(self.with_header(
                    lambda h, n=size: h.update(nonce=b64(b"y" * n))))

    def test_a_wrong_passphrase_is_still_a_wrong_passphrase(self):
        with self.assertRaises(backup.BackupError) as raised:
            backup.unseal(self.sealed, PASSPHRASE + "x")
        self.assertEqual(raised.exception.reason, "cannot_decrypt")


if __name__ == "__main__":
    unittest.main()
