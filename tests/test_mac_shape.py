"""A hardware address has a shape, and an address is not one.

`192-168-101-101` and `192:168:101:101` are twelve hex digits once their separators are stripped, and a
normaliser that strips first turned them into the MAC 19:21:68:10:11:01 and accepted that as a display
that was merely not found. The shape is now checked before anything is stripped, in one module that
every normaliser uses. Spellings below are adversarial and share nothing with any real unit.
"""
import unittest

import _support

import luma_adapters
import luma_backup
import luma_groups
import luma_mac
import luma_projection
import luma_users

CANON = "02:00:5e:10:00:4a"
GOOD = ["02:00:5e:10:00:4a", "02:00:5E:10:00:4A", "02-00-5e-10-00-4a", "02-00-5E-10-00-4A",
        "0200.5e10.004a", "0200.5E10.004A", "02005e10004a", "02005E10004A", "  02:00:5e:10:00:4a  "]
IPV4_SPELLINGS = ["192.168.101.101", "192-168-101-101", "192:168:101:101", "192 168 101 101", "192_168_101_101",
                  "192/168/101/101", "192,168,101,101", "192.168-101:101", "1.2.3.4", "10.0.0.1", "010.020.030.040",
                  "255.255.255.255", "198.51.100.7", " 203-0-113-9 "]
MALFORMED = ["02:00-5e:10-00:4a",          # mixed separators
             "02:00:5e:10:00", "02:00:5e:10:00:4a:ff", "0200.5e10.004", "0200.5e10.004a.ff", "0200:5e10:004a",
             "02:00:5e:10:00:4g", "g2:00:5e:10:00:4a", "02:0:5e:10:00:4a", "0:2:00:5e:10:00:4a", "02005e10004",
             "02005e10004ab", "0x02005e10004a", "", " ", "::::::", "mac:02:00:5e:10:00:4a",
             "02:00:5e:10:00:4a;", "\u0660\u0662:00:5e:10:00:4a", "02:00:5e:10:00:4a\u00a0x"]


class ShapeTests(unittest.TestCase):
    def test_every_accepted_spelling_gives_the_one_canonical_form(self):
        for text in GOOD:
            self.assertEqual(luma_mac.normalise_mac(text), CANON, text)

    def test_nothing_that_reads_as_an_ipv4_address_is_ever_a_mac(self):
        for text in IPV4_SPELLINGS:
            self.assertTrue(luma_mac.looks_like_ipv4(text), text)
            self.assertEqual(luma_mac.normalise_mac(text), "", text)

    def test_malformed_input_is_not_a_mac(self):
        for text in MALFORMED:
            self.assertEqual(luma_mac.normalise_mac(text), "", repr(text))
        for value in (None, 5, 1.5, True, [], {}, b"02:00:5e:10:00:4a"):
            self.assertEqual(luma_mac.normalise_mac(value), "", repr(value))

    def test_a_real_mac_is_not_mistaken_for_an_address(self):
        for text in ("12:34:56:78:90:12", "12-34-56-78-90-12", "1234.5678.9012", "123456789012"):
            self.assertFalse(luma_mac.looks_like_ipv4(text), text)
            self.assertNotEqual(luma_mac.normalise_mac(text), "", text)


class EveryNormaliserAgreesTests(unittest.TestCase):
    """The same answer from every module that normalises an identity."""

    def test_good_spellings_agree_everywhere(self):
        for text in GOOD:
            self.assertEqual(luma_projection.canonical_identity(text), CANON, text)
            self.assertEqual(luma_users._normalise_identity(text), CANON, text)
            self.assertEqual(luma_backup._normal_mac(text), CANON, text)
            self.assertEqual(luma_adapters._normalise_mac(text), CANON.replace(":", ""), text)

    def test_ipv4_spellings_are_refused_everywhere(self):
        for text in IPV4_SPELLINGS:
            self.assertEqual(luma_projection.canonical_identity(text), "", text)
            self.assertEqual(luma_users._normalise_identity(text), "", text)
            self.assertEqual(luma_backup._normal_mac(text), "", text)
            self.assertEqual(luma_adapters._normalise_mac(text), "", text)

    def test_the_server_side_mac_normaliser_does_not_find_a_mac_inside_other_text(self):
        import lumaserver
        for text in ("g02:00:5e:10:00:4a", "mac 02:00:5e:10:00:4a", "02:00:5e:10:00:4a:ff", "192-168-101-101"):
            self.assertNotRegex(lumaserver._normalize_mac(text), r"^(?:[0-9a-f]{2}:){5}[0-9a-f]{2}$", text)
        self.assertEqual(lumaserver._normalize_mac("02-00-5E-10-00-4A"), CANON)


class GroupsAndPermissionsTests(unittest.TestCase):
    def store(self, tmp):
        import pathlib
        return luma_groups.GroupStore(pathlib.Path(tmp) / "groups.json", luma_projection.canonical_identity)

    def test_a_group_member_spelled_as_an_address_is_rejected_not_converted(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            groups = self.store(tmp)
            for text in IPV4_SPELLINGS:
                result = groups.create("Lobby", [text])
                self.assertFalse(result["ok"], text)
                self.assertEqual(groups.all(), [], text)
            ok = groups.create("Lobby", ["02-00-5E-10-00-4A"])
            self.assertTrue(ok["ok"])
            self.assertEqual(groups.all()[0]["members"], [CANON])

    def test_an_unreadable_identity_never_becomes_a_permission_nor_matches_one(self):
        perms = luma_users.Permissions(IPV4_SPELLINGS + [CANON], IPV4_SPELLINGS, (), ())
        self.assertEqual(perms.sources, frozenset([CANON]))
        self.assertEqual(perms.destinations, frozenset())
        for text in IPV4_SPELLINGS + ["", "   ", "!!!"]:
            self.assertFalse(perms.may_use_source(text), repr(text))
            self.assertFalse(perms.may_control_destination(text), repr(text))
        self.assertTrue(perms.may_use_source("02-00-5E-10-00-4A"))


if __name__ == "__main__":
    unittest.main()
