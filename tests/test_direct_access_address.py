"""A Direct Access link when the address remote access serves changes.

The URL a person is handed contains the address remote access was serving. When that address
changes -- another adapter is chosen, or DHCP moves the computer -- a copy of the old URL points at
nothing, and the NEW link has to be offered and the old one not left looking current.

What is decided, and fenced here:

  * The token is NOT replaced because the host changed. It names an account and works at whatever
    address the server is listening on, so the new link is the same token on the new origin. Exactly
    one token stays live and nobody is signed out. (Regenerating is still one explicit button, and
    the advice says when to use it: if the old address may now belong to another device.)
  * The change is derived from the current listener on every read, so it is seen without a rescan.
  * The old URL is never rebuilt or returned: it carries the token, and there is no reason to
    produce a second copy of a credential.
  * An address that was never recorded is unknown, and unknown never warns.
  * Acknowledging records the address only; no session ends and no token moves.

The listener test at the end uses two real loopback addresses on one port: the same token opens the
panel at the new origin, and the old origin refuses the connection because nothing listens there.

No address, hostname or token in this file is real.
"""
import http.client
import os
import socket
import unittest
from unittest import mock

from _support import System

import luma_adapters
import luma_remote
import luma_users

A = ("198.51.100.10", 18443)
B = ("198.51.100.77", 18443)
ORIGIN_A = "http://198.51.100.10:18443"
ORIGIN_B = "http://198.51.100.77:18443"


class Base(unittest.TestCase):
    def setUp(self):
        self.s = System()
        self.addCleanup(self.s.close)
        self.s.admin("root")
        self.s.user("alex", sources=[], destinations=[])
        self.local = self.s.client(remote=False)
        self.listener = self.s.remote.listener
        self.move(A)
        self.id = next(u["id"] for u in self.s.store.list_accounts() if u["username"] == "alex")
        self.url = "/api/admin/users/%s/direct_access" % self.id

    def move(self, bound):
        """Make the listener look as though it serves `bound`, or nothing."""
        if bound is None:
            self.listener._server = None
            self.listener._bound = None
        else:
            self.listener._server = object()
            self.listener._bound = bound
            self.listener._scheme = "http"

    def act(self, action):
        r = self.local.post(self.url, json={"action": action})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return r.get_json()

    def read(self):
        return self.local.get(self.url).get_json()

    def state(self):
        return self.local.get("/api/remote_access").get_json()

    def token_of(self, link):
        return link.rsplit("/access/", 1)[1]


class AddressChangeTests(Base):
    def test_a_link_handed_out_at_the_current_address_raises_nothing(self):
        made = self.act("enable")
        self.assertTrue(made["link"].startswith(ORIGIN_A + "/access/"))
        self.assertEqual(made["issued_origin"], ORIGIN_A)
        self.assertIs(made["address_changed"], False)
        self.assertEqual(self.state()["direct_links_stale"], [])

    def test_a_change_of_address_is_said_and_the_new_link_is_offered(self):
        first = self.act("enable")
        self.move(B)
        seen = self.read()
        self.assertIs(seen["address_changed"], True)
        self.assertEqual(seen["issued_origin"], ORIGIN_A)
        self.assertEqual(seen["link_origin"], ORIGIN_B)
        self.assertTrue(seen["link"].startswith(ORIGIN_B + "/access/"))
        self.assertEqual(self.token_of(seen["link"]), self.token_of(first["link"]),
                         "the host changed, not the secret: the same token, a new address")

    def test_the_old_url_is_never_returned(self):
        first = self.act("enable")
        self.move(B)
        text = self.local.get(self.url).get_data(as_text=True)
        self.assertNotIn("198.51.100.10:18443/access", text)
        self.assertEqual(text.count(self.token_of(first["link"])), 1)

    def test_the_notice_is_not_cleared_by_looking_at_it(self):
        self.act("enable")
        self.move(B)
        self.assertIs(self.read()["address_changed"], True)
        self.assertIs(self.read()["address_changed"], True)
        self.assertEqual(self.state()["direct_links_stale"], ["alex"])

    def test_settings_names_the_accounts_whose_link_is_out_of_date_and_no_link(self):
        self.act("enable")
        self.move(B)
        state = self.state()
        self.assertEqual(state["direct_links_stale"], ["alex"])
        self.assertNotIn("/access/", str(state))

    def test_the_change_is_derived_every_time_so_no_rescan_is_needed(self):
        self.act("enable")
        self.assertEqual(self.state()["direct_links_stale"], [])
        self.move(B)
        self.assertEqual(self.state()["direct_links_stale"], ["alex"])
        self.move(A)
        self.assertEqual(self.state()["direct_links_stale"], [])

    def test_a_change_of_port_counts_as_a_change_of_address(self):
        self.act("enable")
        self.move((A[0], 18444))
        self.assertIs(self.read()["address_changed"], True)

    def test_acknowledging_records_the_address_and_moves_nothing_else(self):
        first = self.act("enable")
        self.move(B)
        session = self.s.store.open_session(self.id, luma_users.BY_DIRECT_LINK)
        before = self.s.store.direct_access_token(self.id)
        done = self.act("acknowledge_address")
        self.assertIs(done["address_changed"], False)
        self.assertEqual(done["issued_origin"], ORIGIN_B)
        self.assertEqual(self.s.store.direct_access_token(self.id), before)
        self.assertEqual(self.token_of(done["link"]), self.token_of(first["link"]))
        account, _live = self.s.store.resolve_session(session.token)
        self.assertIsNotNone(account, "acknowledging must not sign anybody out")
        self.assertEqual(self.state()["direct_links_stale"], [])

    def test_acknowledging_is_audited_without_a_token(self):
        self.act("enable")
        self.move(B)
        self.act("acknowledge_address")
        entries = [e for e in self.s.store.audit_entries(limit=50)
                   if e["event"] == "direct_access.address_acknowledged"]
        self.assertEqual(len(entries), 1)

    def test_regenerating_after_a_change_replaces_the_token_and_ends_the_old_one(self):
        first = self.act("enable")
        old = self.token_of(first["link"])
        self.move(B)
        fresh = self.act("regenerate")
        self.assertNotEqual(self.token_of(fresh["link"]), old)
        self.assertIs(fresh["address_changed"], False)
        self.assertEqual(fresh["issued_origin"], ORIGIN_B)
        self.assertIsNone(self.s.store.resolve_direct_token(old))

    def test_a_change_of_address_alone_never_revokes_or_regenerates(self):
        first = self.act("enable")
        self.move(B)
        self.read()
        self.state()
        self.assertEqual(self.token_of(self.read()["link"]), self.token_of(first["link"]))
        self.assertIsNotNone(self.s.store.resolve_direct_token(self.token_of(first["link"])))

    def test_with_remote_access_off_there_is_nothing_to_compare(self):
        self.act("enable")
        self.move(None)
        seen = self.read()
        self.assertEqual(seen["link"], "")
        self.assertIs(seen["address_changed"], False)
        self.assertEqual(self.state()["direct_links_stale"], [])
        self.move(B)
        self.assertIs(self.read()["address_changed"], True)

    def test_an_address_that_was_never_recorded_is_unknown_and_does_not_warn(self):
        self.act("enable")
        self.s.store.get(self.id).direct_access.issued_origin = ""
        self.move(B)
        self.assertIs(self.read()["address_changed"], False)
        self.assertEqual(self.state()["direct_links_stale"], [])
        self.assertEqual(self.s.store.get(self.id).direct_access.issued_origin, ORIGIN_B)
        self.move(A)
        self.assertIs(self.read()["address_changed"], True)

    def test_learning_an_origin_never_overwrites_one_that_was_recorded(self):
        """The API only learns when nothing is recorded, so this second line of defence is reached
        directly: only an acknowledgement may replace a recorded address."""
        self.act("enable")
        store = self.s.store
        self.assertFalse(store.note_direct_origin(self.id, ORIGIN_B))
        self.assertEqual(store.get(self.id).direct_access.issued_origin, ORIGIN_A)
        self.assertTrue(store.note_direct_origin(self.id, ORIGIN_B, acknowledge=True))
        self.assertEqual(store.get(self.id).direct_access.issued_origin, ORIGIN_B)
        self.assertFalse(store.note_direct_origin(self.id, ""), "an empty origin records nothing")

    def test_what_the_public_projection_says_is_unchanged(self):
        self.act("enable")
        self.assertEqual(set(self.s.store.get(self.id).public()["direct_access"]),
                         {"enabled", "protection", "created_at"})

    def test_the_accounts_own_link_endpoint_has_no_address_history(self):
        source = open(os.path.join(os.path.dirname(luma_remote.__file__), "luma_remote_api.py"),
                      encoding="utf-8").read()  # noqa
        body = source[source.index('@app.route("/access/<token>"'):]
        self.assertNotIn("issued_origin", body[:body.index("def direct_access_entry")])
        self.assertIn("_admin_direct_payload", source)

    def test_an_administrator_still_cannot_be_given_a_link(self):
        admin = next(u["id"] for u in self.s.store.list_accounts() if u["username"] == "root")
        r = self.local.post("/api/admin/users/%s/direct_access" % admin, json={"action": "enable"})
        self.assertEqual(r.status_code, 400)


ADAPTER = {"id": "adapter-one", "name": "Test Network", "mac": "02:00:5e:00:00:50"}


def adapter(address):
    return dict(ADAPTER, address=address)


class TheComputersAddressMovesWhileOnTests(Base):
    """The adapter's CURRENT address versus the one the listener is bound to.

    Found by hand: the computer's address changed during a session and Settings went on saying ON,
    securely, at the old address. It is derived from the live interface list on every state read;
    nothing is polled, nothing is rebound, nothing is reissued, and no interface is touched."""

    def setUp(self):
        super().setUp()
        self.live = [adapter("198.51.100.10")]
        patcher = mock.patch.object(luma_adapters, "enumerate_adapters",
                                    lambda *a, **k: [dict(x) for x in self.live])
        patcher.start()
        self.addCleanup(patcher.stop)
        self.s.remote.save_preference(True, "198.51.100.10", 18443, "http", adapter_id=ADAPTER["id"])

    def moved(self):
        return self.state()["address_moved"]

    def test_the_same_address_says_nothing(self):
        self.assertIsNone(self.moved())

    def test_a_new_address_on_the_chosen_adapter_is_said_in_plain_words(self):
        self.live = [adapter("198.51.100.77")]
        moved = self.moved()
        self.assertEqual((moved["from"], moved["to"]), ("198.51.100.10", "198.51.100.77"))
        self.assertEqual(moved["message"],
                         "This computer's address changed from 198.51.100.10 to 198.51.100.77. "
                         "Remote Access is still listening on 198.51.100.10. Turn Remote Access "
                         "off and on to move it.")

    def test_it_is_derived_on_every_read_so_moving_back_clears_it(self):
        self.live = [adapter("198.51.100.77")]
        self.assertIsNotNone(self.moved())
        self.live = [adapter("198.51.100.10")]
        self.assertIsNone(self.moved())

    def test_an_adapter_that_still_carries_the_bound_address_has_not_moved(self):
        self.live = [adapter("198.51.100.10"), adapter("198.51.100.77")]
        self.assertIsNone(self.moved())

    def test_an_adapter_that_is_gone_is_not_a_move(self):
        self.live = []
        self.assertIsNone(self.moved())

    def test_with_remote_access_off_there_is_nothing_to_say(self):
        self.live = [adapter("198.51.100.77")]
        self.move(None)
        self.assertIsNone(self.moved())

    def test_saying_so_changes_nothing(self):
        self.act("enable")
        self.live = [adapter("198.51.100.77")]
        before = open(str(self.s.remote.preference_file), encoding="utf-8").read()
        token = self.s.store.direct_access_token(self.id)
        with mock.patch.object(luma_remote.RemoteListener, "start") as start, \
                mock.patch.object(luma_remote.RemoteListener, "stop") as stop, \
                mock.patch("luma_tls.generate_self_signed") as generate:
            self.assertIsNotNone(self.moved())
            self.read()
        start.assert_not_called()
        stop.assert_not_called()
        generate.assert_not_called()
        self.assertEqual(open(str(self.s.remote.preference_file), encoding="utf-8").read(), before)
        self.assertEqual(self.s.store.direct_access_token(self.id), token)

    def test_the_direct_link_is_not_presented_as_current_at_the_new_address(self):
        first = self.act("enable")
        self.live = [adapter("198.51.100.77")]
        seen = self.read()
        self.assertEqual(seen["address_moved"]["to"], "198.51.100.77")
        self.assertTrue(seen["link"].startswith(ORIGIN_A + "/access/"))
        self.assertNotIn("198.51.100.77", seen["link"])
        self.assertIs(seen["address_changed"], False)
        self.assertEqual(self.token_of(seen["link"]), self.token_of(first["link"]))


class TheListenerMovesTests(Base):
    """Two real loopback addresses, one port: the token follows, the old address goes dark."""

    OLD, NEW = "127.98.0.21", "127.98.0.22"

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(luma_remote, "is_usable_remote_address",
                                    lambda host: str(host).startswith("127.98."))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.move(None)
        probe = socket.socket()
        probe.bind((self.OLD, 0))
        self.port = probe.getsockname()[1]
        probe.close()
        self.addCleanup(self.listener.stop)

    def open_panel(self, host, path):
        conn = http.client.HTTPConnection(host, self.port, timeout=5)
        try:
            conn.request("GET", path)
            return conn.getresponse().status
        finally:
            conn.close()

    def test_the_same_token_opens_the_panel_at_the_new_origin_and_the_old_one_is_gone(self):
        self.assertTrue(self.listener.start(self.OLD, self.port)["ok"])
        first = self.act("enable")
        self.assertTrue(first["link"].startswith("http://%s:%d/access/" % (self.OLD, self.port)))
        token = self.token_of(first["link"])
        self.assertEqual(self.open_panel(self.OLD, "/access/" + token), 200)

        self.listener.stop()
        self.assertTrue(self.listener.start(self.NEW, self.port)["ok"])
        seen = self.read()
        self.assertIs(seen["address_changed"], True)
        self.assertTrue(seen["link"].startswith("http://%s:%d/access/" % (self.NEW, self.port)))
        self.assertEqual(self.token_of(seen["link"]), token)
        self.assertEqual(self.open_panel(self.NEW, "/access/" + token), 200,
                         "the same token must open the panel at the new address")
        with self.assertRaises(OSError):
            self.open_panel(self.OLD, "/access/" + token)


class TheLogNeverHoldsALinkTests(unittest.TestCase):
    """The request line carries the token in its path; the log must not.

    Measured on the running application before this: `GET /access/<token>` was written at INFO."""

    TOKEN = "Zq4k_Test-Token-That-Must-Never-Be-Written-Down-0123456789"

    def capture(self, logger_name, message, *args):
        import io
        import logging
        import luma_request
        stream = io.StringIO()
        # The handler sits on the root logger, as the application's does, and the record arrives
        # from a child logger: a filter on a logger is not consulted for those, only one on the
        # handler is, which is why the installation puts it on both.
        handler = logging.StreamHandler(stream)
        root = logging.getLogger("")
        root.addHandler(handler)
        logger = logging.getLogger(logger_name)
        previous = (root.level, logger.level)
        root.setLevel(logging.INFO)
        logger.setLevel(logging.INFO)
        luma_request.install_token_redaction()
        try:
            logger.info(message, *args)
        finally:
            root.removeHandler(handler)
            root.setLevel(previous[0])
            logger.setLevel(previous[1])
            for owner in (root, logging.getLogger("werkzeug")):
                for f in [f for f in owner.filters if isinstance(f, luma_request.RedactDirectAccessToken)]:
                    owner.removeFilter(f)
        return stream.getvalue()

    def test_a_request_line_is_redacted_before_it_is_written(self):
        out = self.capture("werkzeug", '"GET /access/%s HTTP/1.1" 200 -', self.TOKEN)
        self.assertNotIn(self.TOKEN, out)
        self.assertNotIn(self.TOKEN[4:], out)
        self.assertIn("/access/Zq4k...redacted", out, "enough is left to tell two attempts apart")

    def test_an_error_line_that_names_the_path_is_redacted_too(self):
        out = self.capture("some.other.logger", "failed for /access/%s while serving" % self.TOKEN)
        self.assertNotIn(self.TOKEN[4:], out)

    def test_an_ordinary_line_is_left_alone(self):
        out = self.capture("werkzeug", '"GET /api/remote_access HTTP/1.1" 200 -')
        self.assertIn("/api/remote_access", out)

    def test_the_server_installs_it_after_configuring_logging(self):
        source = open(os.path.join(os.path.dirname(luma_remote.__file__), "lumaserver.py"),
                      encoding="utf-8").read()  # noqa
        configured = source.index("luma_logging.configure(DATA_DIR)")
        installed = source.index("luma_request.install_token_redaction()")
        self.assertGreater(installed, configured,
                           "configuring logging replaces the handlers, so the filter must come after it")


if __name__ == "__main__":
    unittest.main()
