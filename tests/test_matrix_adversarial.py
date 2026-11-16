"""The restricted Matrix route when the world changes under it.

test_user_matrix.py proves each refusal in a still world. These are the interleavings: a person
presses a cell, and between the page being drawn, the request being admitted and the device being
written to, an administrator takes something away. The backend must decide from the account and the
inventory as they are at the moment of acting, and a refusal must be a 4xx that writes nothing.

Nothing reaches a device: the A/V engine, the breakaway engine and the wall operation are recorders.
"""
import threading
import time
import unittest

import _support
from _support import DISPLAY_1, DISPLAY_2, SOURCE_A, SOURCE_B, identity

import luma_users
from test_user_matrix import Fixture, A, B, D1, D2, WALL

# no 500 may ever come out of a stale reference
REFUSALS = (400, 401, 403, 404, 409)


class WaitingBehindTheLock(Fixture):
    """A request queued behind another operation on the same display, while permission is withdrawn.

    A refusal before the wait is a 4xx. A refusal after it comes back inside the multi-display answer,
    which is how a route reports each display: HTTP 200, `ok` false, every display `refused`. What is
    asserted is the decision (not ok, nothing written), not the carrier it travels in.
    """

    def assertRefusedWithoutWriting(self, response):
        body = response.get_json() or {}
        self.assertLess(response.status_code, 500, response.get_data(as_text=True)[:200])
        if response.status_code == 200:
            self.assertIs(body.get("ok"), False, body)
            self.assertEqual(body.get("routed"), 0, body)
            self.assertTrue(body.get("results") and all(r.get("result") == "refused" for r in body["results"]), body)
        else:
            self.assertIn(response.status_code, (401, 403), body)
        self.assertEqual((self.s.router.calls, self.s.mode_router.calls, self.s.wall_router.calls), ([], [], []))

    def _queue_a_matrix_route(self, client, **route):
        """Hold the display's lock, start a Matrix route, and return (waiter results, release)."""
        lock = self.s.remote.locks
        results = []
        acquired, release = threading.Event(), threading.Event()

        def holder():
            with lock.hold(lock.decoder(D1)):
                acquired.set()
                release.wait(10)

        threading.Thread(target=holder, daemon=True).start()
        self.assertTrue(acquired.wait(5))
        waiter = threading.Thread(target=lambda: results.append(self.route(client, A, "av", displays=[D1], **route)),
                                  daemon=True)
        waiter.start()
        time.sleep(0.4)
        self.assertEqual(self.s.router.calls, [], "precondition: the request must be queued, not running")
        self.addCleanup(release.set)
        return results, release, waiter

    def test_matrix_removed_while_the_route_waits_is_refused_and_writes_nothing(self):
        client = self.signed_in(sources=[A], destinations=[D1], standard=True, matrix=True)
        results, release, waiter = self._queue_a_matrix_route(client)
        self.set_permissions({"sources": [A], "destinations": [D1], "standard_routing": True,
                              "matrix_routing": False})
        release.set()
        waiter.join(10)
        self.assertRefusedWithoutWriting(results[0])

    def test_the_source_withdrawn_while_the_route_waits_is_refused(self):
        client = self.signed_in(sources=[A], destinations=[D1], matrix=True)
        results, release, waiter = self._queue_a_matrix_route(client)
        self.set_permissions({"sources": [B], "destinations": [D1], "matrix_routing": True})
        release.set()
        waiter.join(10)
        self.assertRefusedWithoutWriting(results[0])

    def test_the_display_withdrawn_while_the_route_waits_is_refused(self):
        client = self.signed_in(sources=[A], destinations=[D1], matrix=True)
        results, release, waiter = self._queue_a_matrix_route(client)
        self.set_permissions({"sources": [A], "destinations": [D2], "matrix_routing": True})
        release.set()
        waiter.join(10)
        self.assertRefusedWithoutWriting(results[0])

    def test_the_account_deleted_while_the_route_waits_is_refused(self):
        client = self.signed_in(sources=[A], destinations=[D1], matrix=True)
        results, release, waiter = self._queue_a_matrix_route(client)
        self.s.store.delete(self.user_id())
        release.set()
        waiter.join(10)
        self.assertRefusedWithoutWriting(results[0])

    def test_the_account_disabled_while_the_route_waits_is_refused(self):
        client = self.signed_in(sources=[A], destinations=[D1], matrix=True)
        results, release, waiter = self._queue_a_matrix_route(client)
        self.s.store.set_enabled(self.user_id(), False)
        release.set()
        waiter.join(10)
        self.assertRefusedWithoutWriting(results[0])


class WallWaitingBehindTheLock(Fixture):
    """The same, for a saved wall: its members are locked together and the grant is read again after."""

    def test_a_wall_grant_withdrawn_while_the_wall_route_waits_is_refused(self):
        client = self.signed_in(sources=[A], walls=[WALL], matrix=True)
        lock = self.s.remote.locks
        results = []
        acquired, release = threading.Event(), threading.Event()

        def holder():
            with lock.hold(lock.decoder(D1), lock.decoder(D2)):
                acquired.set()
                release.wait(10)

        threading.Thread(target=holder, daemon=True).start()
        self.assertTrue(acquired.wait(5))
        self.addCleanup(release.set)
        waiter = threading.Thread(target=lambda: results.append(self.route(client, A, "av", walls=[WALL])),
                                  daemon=True)
        waiter.start()
        time.sleep(0.4)
        self.assertEqual(self.s.wall_router.calls, [], "precondition: the wall route is queued, not running")
        self.set_permissions({"sources": [A], "destinations": [], "walls": [], "matrix_routing": True})
        release.set()
        waiter.join(10)
        body = results[0].get_json() or {}
        self.assertLess(results[0].status_code, 500)
        self.assertIs(body.get("ok"), False, body)
        self.assertEqual(self.s.wall_router.calls, [], "the wall was written to after its grant was withdrawn")


class StaleReferences(Fixture):
    """A page drawn from one world, a click made in another."""

    def test_a_group_deleted_after_the_page_was_drawn_is_refused_not_routed(self):
        group = self.s.remote.groups.create("Pair", [D1, D2])["group"]
        client = self.signed_in(sources=[A], groups=[group["id"]], matrix=True)
        rows = self.matrix(client).get_json()["rows"]
        self.assertTrue(any(r.get("id") == group["id"] for r in rows), "precondition: the group was on the page")
        self.s.remote.groups.delete(group["id"])
        response = self.route(client, A, "av", groups=[group["id"]])
        self.assertIn(response.status_code, REFUSALS, response.get_data(as_text=True)[:200])
        self.assertEqual((self.s.router.calls, self.s.mode_router.calls), ([], []))
        gone = self.matrix(client).get_json()["rows"]
        self.assertFalse(any(r.get("id") == group["id"] for r in gone), "and it leaves the next read")

    def test_a_wall_deleted_after_the_page_was_drawn_is_refused_not_routed(self):
        client = self.signed_in(sources=[A], walls=[WALL], matrix=True)
        rows = self.matrix(client).get_json()["rows"]
        self.assertTrue(any(r.get("id") == WALL for r in rows), "precondition: the wall was on the page")
        self.s.wall_defs.clear()
        response = self.route(client, A, "av", walls=[WALL])
        self.assertIn(response.status_code, REFUSALS, response.get_data(as_text=True)[:200])
        self.assertEqual(self.s.wall_router.calls, [])

    def test_a_group_member_removed_after_the_page_was_drawn_is_not_written_to(self):
        group = self.s.remote.groups.create("Pair", [D1, D2])["group"]
        client = self.signed_in(sources=[A], groups=[group["id"]], matrix=True)
        self.matrix(client)
        self.s.remote.groups.update(group["id"], members=[D1])
        response = self.route(client, A, "av", groups=[group["id"]])
        body = response.get_json()
        self.assertEqual(response.status_code, 200, body)
        self.assertEqual(self.s.router.calls, [(DISPLAY_1["ip"], SOURCE_A["ip"])],
                         "the member that left the group is not written to")

    def test_a_display_that_left_the_inventory_after_the_page_was_drawn_is_not_a_500(self):
        client = self.signed_in(sources=[A], destinations=[D1], matrix=True)
        self.matrix(client)
        self.s.units[:] = [u for u in self.s.units if u["mac"] != DISPLAY_1["mac"]]
        response = self.route(client, A, "av", displays=[D1])
        self.assertLess(response.status_code, 500, response.get_data(as_text=True)[:200])
        self.assertEqual(self.s.router.calls, [])

    def test_a_source_that_left_the_inventory_after_the_page_was_drawn_is_not_a_500(self):
        client = self.signed_in(sources=[A], destinations=[D1], matrix=True)
        self.matrix(client)
        self.s.units[:] = [u for u in self.s.units if u["mac"] != SOURCE_A["mac"]]
        response = self.route(client, A, "av", displays=[D1])
        self.assertLess(response.status_code, 500, response.get_data(as_text=True)[:200])
        self.assertEqual(self.s.router.calls, [])

    def test_a_cached_page_from_a_removed_account_is_refused_on_the_next_read(self):
        client = self.signed_in(sources=[A], destinations=[D1], matrix=True)
        self.assertEqual(self.matrix(client).status_code, 200)
        self.s.store.delete(self.user_id())
        for path in ("/api/user/matrix", "/api/user/state", "/api/user/revision"):
            self.assertIn(client.get(path).status_code, (401, 403), path)


class Hammer(Fixture):
    """Permission edits racing a stream of Matrix routes: every answer is a decision, never a crash."""

    def test_routes_racing_permission_edits_never_500_and_never_write_what_was_withdrawn(self):
        client = self.signed_in(sources=[A], destinations=[D1], matrix=True)
        stop = threading.Event()
        statuses = []

        def routes():
            while not stop.is_set():
                statuses.append(self.route(client, A, "av", displays=[D1]).status_code)

        def edits():
            on = True
            while not stop.is_set():
                self.set_permissions({"sources": [A] if on else [], "destinations": [D1],
                                      "matrix_routing": True})
                on = not on
                time.sleep(0.01)

        workers = [threading.Thread(target=routes, daemon=True), threading.Thread(target=edits, daemon=True)]
        for worker in workers:
            worker.start()
        time.sleep(1.5)
        stop.set()
        for worker in workers:
            worker.join(10)
        self.assertTrue(statuses, "precondition: routes were attempted")
        self.assertEqual([s for s in statuses if s >= 500], [], "a 5xx came out of the race")
        self.assertTrue(set(statuses) <= {200, 401, 403, 409}, sorted(set(statuses)))
        # whatever was written, a withdrawn source was never the one written
        for _decoder, encoder in self.s.router.calls:
            self.assertEqual(encoder, SOURCE_A["ip"])


if __name__ == "__main__":
    unittest.main()
