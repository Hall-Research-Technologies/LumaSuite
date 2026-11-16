"""Generated Remote Access certificates: identity, names and calendar validity.

What is asserted is read back out of the PEM with `cryptography`, not out of what the
generator says it did: the subject, the subject alternative names and the real
NotBefore / NotAfter.

The date rule (documented in luma_tls.add_calendar_years): three calendar years later,
same month/day/time; 29 February becomes 28 February of the target year.
"""
import datetime
import ipaddress
import os
import tempfile
import unittest
from unittest import mock

from cryptography import x509
from cryptography.x509.oid import NameOID

from _support import System

import luma_adapters
import luma_remote
import luma_tls
from test_remote_access_api import ADAPTER, started_recorder

UTC = datetime.timezone.utc


def at(y, m, d, hh=12, mm=30, ss=15):
    return datetime.datetime(y, m, d, hh, mm, ss, tzinfo=UTC)


class Generated(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cert = os.path.join(self.dir, "c.pem")
        self.key = os.path.join(self.dir, "k.pem")

    def make(self, address="203.0.113.7", **kw):
        luma_tls.generate_self_signed(address, self.cert, self.key, **kw)
        with open(self.cert, "rb") as handle:
            return x509.load_pem_x509_certificate(handle.read())

    def attrs(self, cert, oid):
        return [a.value for a in cert.subject.get_attributes_for_oid(oid)]


class IdentityTests(Generated):
    def test_subject_is_machine_name_hall_research_lumasuite(self):
        cert = self.make(name="STUDIO-PC-7")
        self.assertEqual(self.attrs(cert, NameOID.COMMON_NAME), ["STUDIO-PC-7"])
        self.assertEqual(self.attrs(cert, NameOID.ORGANIZATION_NAME), ["Hall Research"])
        self.assertEqual(self.attrs(cert, NameOID.ORGANIZATIONAL_UNIT_NAME), ["LumaSuite"])
        self.assertEqual(cert.issuer, cert.subject)

    def test_default_name_is_this_machines_name(self):
        with mock.patch.object(luma_tls.socket, "gethostname", lambda: "Edit-Bay_3"):
            cert = self.make()
        self.assertEqual(self.attrs(cert, NameOID.COMMON_NAME), ["Edit-Bay_3"])

    def test_san_has_machine_dns_and_selected_adapter_ip(self):
        cert = self.make("198.51.100.20", name="rack-host")
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        self.assertEqual(san.get_values_for_type(x509.DNSName), ["rack-host"])
        self.assertEqual(san.get_values_for_type(x509.IPAddress),
                         [ipaddress.ip_address("198.51.100.20")])

    def test_a_hostname_argument_is_still_covered_as_a_dns_name(self):
        cert = self.make("luma.example.test", name="rack-host")
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        self.assertEqual(san.get_values_for_type(x509.DNSName), ["rack-host", "luma.example.test"])
        self.assertEqual(san.get_values_for_type(x509.IPAddress), [])

    def test_describe_reports_identity_and_names_and_covers_the_address(self):
        self.make("198.51.100.20", name="rack-host")
        described = luma_tls.describe(self.cert)
        self.assertEqual(described["subject"], "rack-host")
        self.assertEqual(described["organization"], "Hall Research")
        self.assertEqual(described["organizational_unit"], "LumaSuite")
        self.assertEqual(described["san_ips"], ["198.51.100.20"])
        self.assertTrue(described["self_signed"])
        self.assertTrue(luma_tls.covers(described, "198.51.100.20"))
        self.assertTrue(luma_tls.covers(described, "rack-host"))
        self.assertFalse(luma_tls.covers(described, "198.51.100.21"))

    def test_the_private_key_never_appears_in_the_description(self):
        self.make()
        self.assertNotIn("PRIVATE", repr(luma_tls.describe(self.cert)))


class MachineNameTests(unittest.TestCase):
    def test_unsafe_characters_become_hyphens(self):
        self.assertEqual(luma_tls.sanitise_machine_name("My PC (lab)é"), "My-PC--lab")
        self.assertEqual(luma_tls.sanitise_machine_name("  ..-pc-..  "), "pc")

    def test_empty_or_unusable_falls_back(self):
        for raw in ("", None, "   ", "...", "---"):
            self.assertEqual(luma_tls.sanitise_machine_name(raw), "lumasuite", repr(raw))

    def test_common_name_is_bounded_to_64_and_labels_to_63(self):
        self.assertLessEqual(len(luma_tls.sanitise_machine_name("a" * 200)), 63)
        name = luma_tls.sanitise_machine_name(".".join(["b" * 60] * 5))
        self.assertLessEqual(len(name), 64)
        self.assertTrue(all(len(label) <= 63 for label in name.split(".")))

    def test_a_long_name_still_produces_a_loadable_certificate(self):
        directory = tempfile.mkdtemp()
        cert, key = os.path.join(directory, "c.pem"), os.path.join(directory, "k.pem")
        luma_tls.generate_self_signed("203.0.113.7", cert, key, name="x" * 300)
        luma_tls.load_context(cert, key)

    def test_falls_back_to_platform_node_when_hostname_fails(self):
        def boom():
            raise OSError("no")
        with mock.patch.object(luma_tls.socket, "gethostname", boom), \
                mock.patch.object(luma_tls.platform, "node", lambda: "node-name"):
            self.assertEqual(luma_tls.machine_name(), "node-name")


class CalendarRuleTests(unittest.TestCase):
    def test_ordinary_dates_keep_month_day_and_time(self):
        self.assertEqual(luma_tls.add_calendar_years(at(2026, 11, 6)), at(2029, 11, 6))

    def test_month_ends_map_to_themselves(self):
        for date in [(2026, 1, 31), (2026, 4, 30), (2026, 12, 31), (2026, 2, 28)]:
            moved = luma_tls.add_calendar_years(at(*date))
            self.assertEqual((moved.year, moved.month, moved.day), (date[0] + 3, date[1], date[2]), date)

    def test_leap_day_start_becomes_28_february_when_target_is_a_common_year(self):
        # 2028-02-29 + 3 = 2031 (common): the rule is 28 February, never 1 March.
        self.assertEqual(luma_tls.add_calendar_years(at(2028, 2, 29)), at(2031, 2, 28))

    def test_leap_day_start_stays_29_february_when_target_is_leap(self):
        # 2024-02-29 + 4 = 2028 (leap): the date exists, so it is kept.
        self.assertEqual(luma_tls.add_calendar_years(at(2024, 2, 29), 4), at(2028, 2, 29))

    def test_28_february_before_a_leap_target_is_not_moved_to_29(self):
        self.assertEqual(luma_tls.add_calendar_years(at(2021, 2, 28)), at(2024, 2, 28))

    def test_the_default_is_three_calendar_years(self):
        self.assertEqual(luma_tls.GENERATED_VALIDITY_YEARS, 3)


class X509ValidityTests(Generated):
    def check(self, now, expected_after):
        cert = self.make(now=now)
        self.assertEqual(cert.not_valid_after_utc, expected_after)
        # NotBefore stays five minutes behind generation, for clock skew.
        self.assertEqual(cert.not_valid_before_utc, now - datetime.timedelta(minutes=5))
        return cert

    def test_normal_date(self):
        self.check(at(2026, 11, 6), at(2029, 11, 6))

    def test_month_boundaries(self):
        self.check(at(2026, 1, 31, 0, 0, 0), at(2029, 1, 31, 0, 0, 0))
        self.check(at(2026, 12, 31, 23, 59, 59), at(2029, 12, 31, 23, 59, 59))

    def test_leap_year_dates(self):
        self.check(at(2028, 2, 29), at(2031, 2, 28))
        self.check(at(2027, 2, 28), at(2030, 2, 28))
        self.check(at(2023, 3, 1), at(2026, 3, 1))

    def test_default_uses_the_host_clock(self):
        before = datetime.datetime.now(UTC).replace(microsecond=0)
        cert = self.make()
        after = datetime.datetime.now(UTC)
        self.assertGreaterEqual(cert.not_valid_after_utc, luma_tls.add_calendar_years(before))
        self.assertLessEqual(cert.not_valid_after_utc, luma_tls.add_calendar_years(after))

    def test_not_valid_before_is_not_later_than_generation(self):
        now = at(2026, 6, 15)
        self.assertLess(self.make(now=now).not_valid_before_utc, now)


class ExpiryAndRemainingTests(Generated):
    def test_remaining_lifetime_is_computed_from_the_certificates_own_expiry(self):
        now = at(2026, 11, 6)
        self.make(now=now)
        # 2026-11-06 -> 2029-11-06 is 1096 days (2028 is a leap year).
        self.assertEqual(luma_tls.describe(self.cert, now=now)["days_remaining"], 1096)
        later = luma_tls.describe(self.cert, now=at(2027, 11, 6))
        self.assertEqual(later["days_remaining"], 731)
        self.assertFalse(later["expired"])

    def test_the_displayed_expiry_date_is_the_three_year_date(self):
        self.make(now=at(2028, 2, 29, 8, 0, 0))
        self.assertEqual(luma_tls.describe(self.cert)["not_after"], "Feb 28 08:00:00 2031 GMT")

    def test_expiry_flips_after_not_after(self):
        now = at(2026, 11, 6)
        self.make(now=now)
        self.assertFalse(luma_tls.describe(self.cert, now=at(2029, 11, 6, 12, 0, 0))["expired"])
        gone = luma_tls.describe(self.cert, now=at(2029, 11, 8))
        self.assertTrue(gone["expired"])
        self.assertLess(gone["days_remaining"], 0)


class ExplicitRegenerateTests(unittest.TestCase):
    """Existing certificates are never replaced automatically; only an explicit request does."""

    def setUp(self):
        self.s = System()
        self.addCleanup(self.s.close)
        self.calls, patcher = started_recorder(self.s)
        self.addCleanup(patcher.stop)
        adapters = mock.patch.object(luma_adapters, "enumerate_adapters", lambda *a, **k: [dict(ADAPTER)])
        adapters.start()
        self.addCleanup(adapters.stop)
        self.s.admin("root")
        self.local = self.s.client(remote=False)

    def generate(self):
        return self.local.post("/api/admin/certificate",
                               json={"action": "generate", "address": ADAPTER["address"]})

    def old_format(self):
        """Put an older-format certificate (CN = the address, old organisation) in place."""
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Hall Research Technologies LLC"),
                          x509.NameAttribute(NameOID.COMMON_NAME, ADAPTER["address"])])
        now = datetime.datetime.now(UTC)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - datetime.timedelta(minutes=5))
                .not_valid_after(now + datetime.timedelta(days=825))
                .add_extension(x509.SubjectAlternativeName(
                    [x509.IPAddress(ipaddress.ip_address(ADAPTER["address"]))]), critical=False)
                .sign(key, hashes.SHA256()))
        self.s.remote.cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        self.s.remote.key_file.write_bytes(key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption()))

    def test_an_older_format_certificate_keeps_working_and_is_not_touched(self):
        self.old_format()
        before = luma_tls.describe(str(self.s.remote.cert_file))["fingerprint"]
        response = self.local.post("/api/remote_access", json={
            "enabled": True, "adapter_id": ADAPTER["id"], "transport": "https"})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(luma_tls.describe(str(self.s.remote.cert_file))["fingerprint"], before)
        state = self.local.get("/api/remote_access").get_json()["certificate"]
        self.assertTrue(state["matches_address"])
        self.assertEqual(state["subject"], ADAPTER["address"])

    def test_explicit_regenerate_applies_the_new_identity_and_reports_the_old_fingerprint(self):
        self.old_format()
        before = luma_tls.describe(str(self.s.remote.cert_file))["fingerprint"]
        body = self.generate().get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["previous_fingerprint"], before)
        now = body["certificate"]
        self.assertNotEqual(now["fingerprint"], before)
        self.assertEqual(now["organization"], "Hall Research")
        self.assertEqual(now["organizational_unit"], "LumaSuite")
        self.assertIn(ADAPTER["address"], now["san_ips"])

    def test_a_supplied_certificate_is_never_replaced_by_generate(self):
        directory = tempfile.mkdtemp()
        cert, key = os.path.join(directory, "site.pem"), os.path.join(directory, "site.key")
        luma_tls.generate_self_signed("203.0.113.99", cert, key)
        self.assertEqual(self.local.post("/api/admin/certificate", json={
            "action": "use", "cert_path": cert, "key_path": key}).status_code, 200)
        before = luma_tls.describe(cert)["fingerprint"]
        response = self.generate()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(luma_tls.describe(cert)["fingerprint"], before)
        self.assertFalse(os.path.exists(self.s.remote.cert_file))
        state = self.local.get("/api/remote_access").get_json()["certificate"]
        self.assertFalse(state["generated"])

    def test_the_wrong_network_refusal_names_the_certificates_address(self):
        moved = dict(ADAPTER, address="192.0.2.60")
        self.local.post("/api/admin/certificate", json={"action": "generate", "address": moved["address"]})
        response = self.local.post("/api/remote_access", json={
            "enabled": True, "adapter_id": ADAPTER["id"], "transport": "https"})
        self.assertEqual(response.status_code, 400)
        error = response.get_json()["error"]
        self.assertIn(moved["address"], error)
        self.assertIn(ADAPTER["address"], error)


if __name__ == "__main__":
    unittest.main()
