"""The preview proxy against hostile or broken devices.

A device is a stranger on the network: the server fetches from it on a user's behalf, so what
it answers must never steer the server, exhaust it, or be passed on as something it is not.
The "devices" here are real servers on loopback (the fence allows loopback and nothing else), so
the HTTP behaviour under test is real; the production address check is stubbed only where a
loopback test device has to stand in for a device address.
"""
import http.server
import io
import threading
import time
import unittest
from unittest import mock

import _support  # noqa: F401  (fence and scratch data folder first)

import lumaserver
import luma_statecache


def jpeg_bytes(size=(16, 16)):
    from PIL import Image
    buffer = io.BytesIO()
    Image.new("RGB", size, (200, 30, 30)).save(buffer, "JPEG")
    return buffer.getvalue()


class FakeDevice:
    """A loopback server whose behaviour a test scripts. Counts every request it gets."""

    def __init__(self, handler):
        self.requests = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def do_GET(self):
                outer.requests.append(self.path)
                try:
                    handler(self)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return "http://127.0.0.1:%d/thumbnail/thumbnail1.jpg" % self.server.server_port

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


def serve(handler):
    return FakeDevice(handler)


def respond(handler, status=200, ctype="image/jpeg", body=b"", headers=()):
    handler.send_response(status)
    if ctype:
        handler.send_header("Content-Type", ctype)
    for key, value in headers:
        handler.send_header(key, value)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class Base(unittest.TestCase):
    def device(self, handler):
        d = serve(handler)
        self.addCleanup(d.close)
        return d


class RedirectTests(Base):
    def test_a_redirect_to_another_host_is_never_followed(self):
        secret = self.device(lambda h: respond(h, body=jpeg_bytes()))
        redirecting = self.device(lambda h: respond(h, status=302, ctype=None, headers=[("Location", secret.url)]))
        self.assertIsNone(lumaserver._preview_still(redirecting.url))
        self.assertEqual(secret.requests, [], "the server must not have been steered to the redirect target")

    def test_every_kind_of_redirect_is_refused(self):
        target = self.device(lambda h: respond(h, body=jpeg_bytes()))
        for status in (301, 302, 303, 307, 308):
            redirecting = self.device(lambda h, s=status: respond(
                h, status=s, ctype=None, headers=[("Location", target.url)]))
            self.assertIsNone(lumaserver._preview_still(redirecting.url), status)
        self.assertEqual(target.requests, [])

    def test_a_redirect_to_localhost_services_or_a_metadata_address_is_not_followed(self):
        local = self.device(lambda h: respond(h, body=jpeg_bytes()))
        for location in (local.url, "http://localhost:%d/x" % local.server.server_port,
                         "http://[::1]:%d/x" % local.server.server_port, "http://169.254.169.254/latest/meta-data/",
                         "file:///etc/passwd", "//127.0.0.1/x"):
            redirecting = self.device(lambda h, loc=location: respond(
                h, status=302, ctype=None, headers=[("Location", loc)]))
            self.assertIsNone(lumaserver._preview_still(redirecting.url), location)
        self.assertEqual(local.requests, [])

    def test_the_helper_that_touches_the_network_forbids_redirects_whatever_the_caller_asks(self):
        with mock.patch.object(lumaserver.requests, "get") as get:
            lumaserver._preview_http_get("http://192.0.2.1/x", allow_redirects=True, timeout=1)
        self.assertIs(get.call_args.kwargs["allow_redirects"], False)


class ContentTests(Base):
    def test_a_real_jpeg_is_relayed(self):
        frame = jpeg_bytes()
        d = self.device(lambda h: respond(h, body=frame))
        self.assertEqual(lumaserver._preview_still(d.url), frame)

    def test_a_status_other_than_ok_is_no_preview(self):
        for status in (204, 400, 401, 403, 404, 500, 503):
            d = self.device(lambda h, s=status: respond(h, status=s, body=jpeg_bytes()))
            self.assertIsNone(lumaserver._preview_still(d.url), status)

    def test_html_script_and_svg_are_never_relayed_as_a_picture(self):
        for ctype, body in (("text/html", b"<script>alert(1)</script>"),
                            ("image/svg+xml", b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>"),
                            ("application/json", b'{"image": true}'),
                            ("image/jpeg", b"<html><script>alert(1)</script></html>")):
            d = self.device(lambda h, c=ctype, b=body: respond(h, ctype=c, body=b))
            self.assertIsNone(lumaserver._preview_still(d.url), ctype)

    def test_another_image_format_is_not_relayed_as_a_jpeg(self):
        from PIL import Image
        for fmt, ctype in (("PNG", "image/png"), ("GIF", "image/gif"), ("BMP", "image/bmp")):
            buffer = io.BytesIO()
            Image.new("RGB", (8, 8)).save(buffer, fmt)
            d = self.device(lambda h, c=ctype, b=buffer.getvalue(): respond(h, ctype=c, body=b))
            self.assertIsNone(lumaserver._preview_still(d.url), fmt)

    def test_a_png_labelled_jpeg_is_not_relayed(self):
        from PIL import Image
        buffer = io.BytesIO()
        Image.new("RGB", (8, 8)).save(buffer, "PNG")
        d = self.device(lambda h: respond(h, ctype="image/jpeg", body=buffer.getvalue()))
        self.assertIsNone(lumaserver._preview_still(d.url))

    def test_a_truncated_or_corrupt_jpeg_is_not_relayed(self):
        frame = jpeg_bytes()
        for body in (frame[:40], frame[:-30], b"\xff\xd8" + b"\x00" * 50, b"", b"\xff\xd8\xff\xd9"):
            d = self.device(lambda h, b=body: respond(h, body=b))
            self.assertIsNone(lumaserver._preview_still(d.url), len(body))

    def test_the_first_valid_frame_of_a_multipart_stream_is_taken(self):
        one, two = jpeg_bytes((16, 16)), jpeg_bytes((32, 32))

        def stream(h):
            h.send_response(200)
            h.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            h.end_headers()
            for frame in (one, two):
                h.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n")

        d = self.device(stream)
        self.assertEqual(lumaserver._preview_still(d.url), one)

    def test_a_stream_of_garbage_with_no_frame_gives_up(self):
        def noise(h):
            h.send_response(200)
            h.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            h.end_headers()
            for _ in range(400):
                h.wfile.write(b"\x00" * 1024)

        d = self.device(noise)
        self.assertIsNone(lumaserver._preview_still(d.url))


class BoundsTests(Base):
    def test_an_oversized_response_is_cut_off_and_reading_stops(self):
        sent = []

        def endless(h):
            h.send_response(200)
            h.send_header("Content-Type", "image/jpeg")
            h.end_headers()
            h.wfile.write(b"\xff\xd8")
            try:
                for _ in range(2000):                   # up to 2000 KiB, far past the cap
                    h.wfile.write(b"\x00" * 1024)
                    sent.append(1024)
            except OSError:
                pass

        d = self.device(endless)
        started = time.monotonic()
        self.assertIsNone(lumaserver._preview_still(d.url))
        self.assertLess(time.monotonic() - started, lumaserver.PREVIEW_TIMEOUT_SECONDS * 2 + 2)
        self.assertLess(sum(sent), lumaserver.PREVIEW_MAX_BYTES * 3, "the server kept reading well past the cap")

    def test_a_device_that_stalls_after_the_headers_does_not_hold_the_request_open(self):
        def stall(h):
            h.send_response(200)
            h.send_header("Content-Type", "image/jpeg")
            h.end_headers()
            h.wfile.flush()
            time.sleep(6)

        d = self.device(stall)
        with mock.patch.object(lumaserver, "PREVIEW_TIMEOUT_SECONDS", 0.6):
            started = time.monotonic()
            with self.assertRaises(Exception):
                lumaserver._preview_still(d.url)
            self.assertLess(time.monotonic() - started, 4)

    def test_a_dribbling_device_is_stopped_by_the_overall_deadline(self):
        def dribble(h):
            h.send_response(200)
            h.send_header("Content-Type", "image/jpeg")
            h.end_headers()
            for _ in range(60):
                h.wfile.write(b"\x00")
                h.wfile.flush()
                time.sleep(0.2)

        d = self.device(dribble)
        with mock.patch.object(lumaserver, "PREVIEW_TIMEOUT_SECONDS", 0.5):
            started = time.monotonic()
            try:
                self.assertIsNone(lumaserver._preview_still(d.url))
            except Exception:
                pass                                  # a read timeout is also a bounded failure
            self.assertLess(time.monotonic() - started, 5)


class AddressTests(unittest.TestCase):
    def test_only_an_ordinary_device_address_may_be_fetched_from(self):
        for bad in ("127.0.0.1", "127.9.9.9", "::1", "0.0.0.0", "::", "224.0.0.1", "240.0.0.1",
                    "localhost", "device.example.test", "", None, "192.0.2.1/../x", "192.0.2.1:80",
                    "192.0.2.1@evil", "http://192.0.2.1", "192.0.2.1\r\nHost: x", "1.2.3", "999.1.1.1"):
            self.assertFalse(lumaserver._preview_address_ok(bad), repr(bad))
        for good in ("192.0.2.10", "198.51.100.7", "203.0.113.9", "10.1.2.3", "192.168.1.5", " 192.0.2.10 "):
            self.assertTrue(lumaserver._preview_address_ok(good), repr(good))

    def test_a_cache_entry_holding_something_unsafe_is_not_fetched(self):
        with mock.patch.object(lumaserver, "_preview_still") as still:
            for bad in ("127.0.0.1", "localhost", "192.0.2.1/x"):
                result = lumaserver._fetch_preview_uncached("display", bad)
                self.assertEqual(result["reason"], "preview_unavailable")
        still.assert_not_called()


class EndToEndTests(Base):
    """`_fetch_preview` against a loopback device, with only the address check stood in for."""

    def setUp(self):
        patcher = mock.patch.object(lumaserver, "_preview_address_ok", lambda a: True)
        patcher.start()
        self.addCleanup(patcher.stop)
        cache = mock.patch.object(lumaserver, "_preview_cache", luma_statecache.StateCache(ttl=30))
        cache.start()
        self.addCleanup(cache.stop)

    def point_at(self, device):
        port = device.server.server_port
        patcher = mock.patch.object(lumaserver, "_preview_urls",
                                    lambda kind, address, unit_type: ["http://127.0.0.1:%d/thumbnail/thumbnail1.jpg" % port])
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_working_device_is_fetched_once_for_many_concurrent_viewers(self):
        frame = jpeg_bytes()

        def slow(h):
            time.sleep(0.4)
            respond(h, body=frame)

        d = self.device(slow)
        self.point_at(d)
        results = []
        threads = [threading.Thread(target=lambda: results.append(lumaserver._fetch_preview("display", "192.0.2.50")))
                   for _ in range(12)]
        for t in threads: t.start()
        for t in threads: t.join(10)
        self.assertEqual(len(results), 12)
        self.assertTrue(all(r["image"] == frame for r in results))
        self.assertEqual(len(d.requests), 1, "twelve viewers must cost one fetch from the device")

    def test_an_offline_device_is_no_preview_not_an_error_and_is_remembered_briefly(self):
        d = self.device(lambda h: respond(h, body=jpeg_bytes()))
        self.point_at(d)
        d.close()                                    # the device goes away: connection refused
        first = lumaserver._fetch_preview("display", "192.0.2.51")
        second = lumaserver._fetch_preview("display", "192.0.2.51")
        self.assertEqual(first["reason"], "preview_unavailable")
        self.assertEqual(second["reason"], "preview_unavailable")

    def test_fresh_bypasses_the_cache_and_a_changed_address_is_a_different_entry(self):
        frame = jpeg_bytes()
        d = self.device(lambda h: respond(h, body=frame))
        self.point_at(d)
        lumaserver._fetch_preview("display", "192.0.2.52")
        lumaserver._fetch_preview("display", "192.0.2.52")
        self.assertEqual(len(d.requests), 1)
        with mock.patch.object(lumaserver, "PREVIEW_FRESH_MIN_INTERVAL", 0):
            lumaserver._fetch_preview("display", "192.0.2.52", fresh=True)
        self.assertEqual(len(d.requests), 2)
        lumaserver._fetch_preview("display", "192.0.2.53")           # the device moved
        self.assertEqual(len(d.requests), 3)

    def test_repeating_fresh_does_not_become_one_device_fetch_per_request(self):
        frame = jpeg_bytes()
        d = self.device(lambda h: respond(h, body=frame))
        self.point_at(d)
        lumaserver._fetch_preview("display", "192.0.2.55")
        for _ in range(40):
            lumaserver._fetch_preview("display", "192.0.2.55", fresh=True)
        self.assertLessEqual(len(d.requests), 2,
                             "a caller repeating fresh=1 reached the device every time")

    def test_a_hostile_device_gets_no_preview_through_the_whole_path(self):
        target = self.device(lambda h: respond(h, body=jpeg_bytes()))
        hostile = self.device(lambda h: respond(h, status=302, ctype=None, headers=[("Location", target.url)]))
        self.point_at(hostile)
        self.assertEqual(lumaserver._fetch_preview("display", "192.0.2.54")["reason"], "preview_unavailable")
        self.assertEqual(target.requests, [])


class MechanismTests(unittest.TestCase):
    def test_a_decoder_uses_its_thumbnail_and_an_encoder_the_live_view_by_default(self):
        self.assertEqual(lumaserver._preview_urls("display", "192.0.2.9", "decoder"),
                         ["http://192.0.2.9/thumbnail/thumbnail1.jpg"])
        with mock.patch.object(lumaserver, "ENCODER_PREVIEW_MODE", "stream"):
            self.assertTrue(lumaserver._preview_urls("source", "192.0.2.9", "encoder")[0].startswith(
                "http://192.0.2.9/stream?"))

    def test_the_unverified_encoder_thumbnail_is_opt_in_only(self):
        with mock.patch.object(lumaserver, "ENCODER_PREVIEW_MODE", "thumbnail"):
            self.assertEqual(lumaserver._preview_urls("source", "192.0.2.9", "encoder"),
                             ["http://192.0.2.9/thumbnail/thumbnail1.jpg"])
        with mock.patch.object(lumaserver, "ENCODER_PREVIEW_MODE", "auto"):
            urls = lumaserver._preview_urls("source", "192.0.2.9", "encoder")
            self.assertEqual(len(urls), 2)
            self.assertIn("thumbnail1.jpg", urls[0])
        self.assertEqual(lumaserver.ENCODER_PREVIEW_MODE, "stream")      # the default

    def test_a_url_is_built_only_from_a_validated_address_and_fixed_paths(self):
        for url in lumaserver._preview_urls("source", "192.0.2.9", "encoder") + \
                lumaserver._preview_urls("display", "192.0.2.9", "switcher"):
            self.assertRegex(url, r"^http://192\.0\.2\.9/(thumbnail/thumbnail1\.jpg|stream\?[a-z0-9=&]+)$")


if __name__ == "__main__":
    unittest.main()
