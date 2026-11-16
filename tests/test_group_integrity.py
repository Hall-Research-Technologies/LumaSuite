"""Display Group integrity: a group route reaches exactly its members, whatever the cache says.

The defect this suite pins: a route resolves a member's hardware identity (MAC) to an address
from the host's cache, and the cache is keyed by address. An address can be re-leased to another
display between scans, and a scan that could not read a MAC used to merge the new device into
the old device's row, so the old identity stayed attached to the new hardware. A group route then
wrote to a display that was not in the group.

Two layers are held here, both against synthetic fixtures and fake devices only:

  * INGEST: `lumaserver._ingest_scanned_unit` never attaches one device's identity to another's
    row, never lets a stale MAC index delete a different unit's row, and never leaves one identity
    under two addresses.
  * ROUTE TIME: every write to a known identity first asks the device at the resolved address who
    it is, and refuses on anything but an exact match. What was *physically* written to is what
    is asserted, never what the cache believed.

`GroupLifecycleTests` walks the whole life of two groups and asserts, at every step, that every
intended member was attempted, no non-member was written to, failures are reported truthfully,
and a deleted or recreated group inherits nothing.
"""
import copy
import unittest

import _support
from _support import ADMIN_PASSWORD, DISPLAY_1, DISPLAY_2, DISPLAY_3, DISPLAY_4, RealApp, identity

from test_routing_torture import Base, A, B, D1, D2, D3, D4

import luma_groups
import luma_projection

# Synthetic units that share nothing with the base fixtures.
DISPLAY_5 = {"mac": "02:00:5e:20:00:05", "ip": "198.51.100.25", "type": "decoder",
             "hostname": "Hall Five", "model": "TEST-D1", "status": "idle",
             "serialnumber": "SN-FIVE", "dec": {"video": {"streamname": ""}}}
DISPLAY_6 = {"mac": "02:00:5e:20:00:06", "ip": "198.51.100.26", "type": "decoder",
             "hostname": "Hall Six", "model": "TEST-D1", "status": "idle",
             "serialnumber": "SN-SIX", "dec": {"video": {"streamname": ""}}}
D5, D6 = identity(DISPLAY_5), identity(DISPLAY_6)
canon = luma_projection.canonical_identity


# ======================================================================== ingest
class IngestTests(unittest.TestCase):
    def setUp(self):
        self.app = RealApp(self)
        self.m = self.app.m

    def seed(self, *units):
        self.app.seed([copy.deepcopy(u) for u in units])
        with self.m.cache_lock:
            return self.m._mac_index_of_cache()

    def ingest(self, index, ip, **fields):
        info = {"ip": ip, "type": "decoder", "status": "idle"}
        info.update(fields)
        with self.m.cache_lock:
            return self.m._ingest_scanned_unit(ip, info, index)

    def row(self, ip):
        with self.m.cache_lock:
            return copy.deepcopy(self.m.cache_units.get(ip))

    def test_a_different_device_with_an_unreadable_mac_does_not_inherit_the_old_identity(self):
        old = dict(DISPLAY_1, serialnumber="SN-ONE")
        index = self.seed(old)
        self.ingest(index, old["ip"], mac="", serialnumber="SN-OTHER", hostname="Someone Else")
        held = self.row(old["ip"])
        self.assertNotEqual(luma_projection.unit_identity(held), D1,
                            "the new device was given the old device's hardware identity")
        self.assertNotIn(D1, index, "the index still names the departed unit")

    def test_an_unprovable_scan_leaves_the_identified_row_exactly_as_it_was(self):
        index = self.seed(DISPLAY_1)                       # no serial on record
        before = self.row(DISPLAY_1["ip"])
        outcome = self.ingest(index, DISPLAY_1["ip"], mac="", hostname="Mystery")
        self.assertEqual(outcome["action"], "skipped")
        self.assertEqual(self.row(DISPLAY_1["ip"]), before)

    def test_a_scan_with_no_mac_but_the_same_serial_is_the_same_unit_and_keeps_its_identity(self):
        index = self.seed(dict(DISPLAY_1, serialnumber="SN-ONE"))
        outcome = self.ingest(index, DISPLAY_1["ip"], mac="", serialnumber="SN-ONE", status="idle")
        self.assertEqual(outcome["action"], "updated")
        self.assertEqual(luma_projection.unit_identity(self.row(DISPLAY_1["ip"])), D1)

    def test_a_different_mac_at_a_held_address_starts_a_fresh_row(self):
        index = self.seed(dict(DISPLAY_1, routed_streamname="stream-left-behind"))
        self.ingest(index, DISPLAY_1["ip"], mac=DISPLAY_5["mac"], hostname="Hall Five")
        held = self.row(DISPLAY_1["ip"])
        self.assertEqual(luma_projection.unit_identity(held), D5)
        self.assertNotIn("routed_streamname", held, "the departed unit's state was carried over")

    def test_an_index_that_went_stale_by_another_writer_is_not_believed(self):
        # The second line of defence. Something other than this function (a route's cache
        # write, a restore) replaced the row after the index was built, so the index names
        # D1 at an address that now holds D5. It must be checked against the row itself.
        index = self.seed(DISPLAY_1)
        with self.m.cache_lock:
            self.m.cache_units[DISPLAY_1["ip"]] = copy.deepcopy(DISPLAY_5, memo={})
            self.m.cache_units[DISPLAY_1["ip"]]["ip"] = DISPLAY_1["ip"]
        self.assertEqual(index[D1], DISPLAY_1["ip"], "the precondition: the index is stale")
        self.ingest(index, "198.51.100.121", mac=DISPLAY_1["mac"])
        kept = self.row(DISPLAY_1["ip"])
        self.assertIsNotNone(kept, "the unit that holds the address was deleted on a stale index")
        self.assertEqual(luma_projection.unit_identity(kept), D5)
        self.assertEqual(luma_projection.unit_identity(self.row("198.51.100.121")), D1)

    def test_a_stale_index_does_not_delete_the_row_of_the_unit_that_took_the_address(self):
        # D1 leaves .21, D5 takes it, and only then is D1 found at its new address. The index
        # was built before the scan and still says D1 lives at .21.
        index = self.seed(DISPLAY_1)
        old_ip = DISPLAY_1["ip"]
        self.ingest(index, old_ip, mac=DISPLAY_5["mac"], hostname="Hall Five",
                    routed_streamname="stream-five")
        self.ingest(index, "198.51.100.121", mac=DISPLAY_1["mac"], hostname="Lobby One")
        five, one = self.row(old_ip), self.row("198.51.100.121")
        self.assertIsNotNone(five, "the unit that took the address was deleted")
        self.assertEqual(luma_projection.unit_identity(five), D5)
        self.assertEqual(luma_projection.unit_identity(one), D1)
        self.assertNotIn("routed_streamname", one, "one unit's row was merged into the other's")

    def test_a_unit_that_moved_is_one_row_at_its_new_address(self):
        index = self.seed(DISPLAY_1, DISPLAY_2)
        out = self.ingest(index, "198.51.100.121", mac=DISPLAY_1["mac"])
        self.assertEqual(out["moved_from"], DISPLAY_1["ip"])
        self.assertIsNone(self.row(DISPLAY_1["ip"]))
        self.assertIsNotNone(self.row(DISPLAY_2["ip"]))

    def test_a_mac_spelled_another_way_is_still_the_same_unit(self):
        index = self.seed(dict(DISPLAY_1, mac="02-00-5E-20-00-01"))      # cached in another spelling
        self.ingest(index, "198.51.100.121", mac="02:00:5e:20:00:01")
        with self.m.cache_lock:
            owners = [ip for ip, u in self.m.cache_units.items()
                      if luma_projection.unit_identity(u) == D1]
        self.assertEqual(owners, ["198.51.100.121"], "one identity must not hold two addresses")

    def test_a_row_on_disk_for_a_unit_already_held_under_another_address_is_not_added(self):
        import json
        import os
        import tempfile
        moved = dict(DISPLAY_1, ip="198.51.100.121")
        self.seed(moved)
        stale = [dict(DISPLAY_1), dict(DISPLAY_2)]       # D1 at its old address, plus a stranger
        path = os.path.join(tempfile.mkdtemp(prefix="lumasuite-merge-"), "cache.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(stale, handle)
        self.app.patch("CACHE_FILE", path)
        self.m._merge_disk_into_memory()
        with self.m.cache_lock:
            owners = sorted(ip for ip, u in self.m.cache_units.items()
                            if luma_projection.unit_identity(u) == D1)
        self.assertEqual(owners, ["198.51.100.121"])
        self.assertIsNotNone(self.row(DISPLAY_2["ip"]), "an unrelated disk row is still added")


# ===================================================================== route time
class _Admin:
    """A signed-in administrator over the remote listener, CSRF token attached."""

    def __init__(self, remote):
        self.remote = remote
        self.client = remote.sign_in("root", ADMIN_PASSWORD)

    def post(self, path, json=None):
        return self.remote.post(self.client, path, json)

    def get(self, path):
        return self.client.get(path)


class RouteTimeBase(Base):
    """The real application and a stateful device model, with two extra displays."""

    def setUp(self):
        super().setUp()
        self.admin = _Admin(self.remote)
        for unit in (DISPLAY_5, DISPLAY_6):
            self.add_display(unit)

    def add_display(self, unit):
        self.sim.add_decoder(unit["ip"])
        self.sim.macs[unit["ip"]] = unit["mac"]
        with self.m.cache_lock:
            self.m.cache_units[unit["ip"]] = copy.deepcopy(unit)

    def written(self):
        """The hardware addresses (as the devices report them) that were written to."""
        return sorted({self.sim.macs.get(ip, "?" + ip) for ip, _m in self.sim.writes()})

    def route_group(self, group_id, source=A):
        # Every display starts on nothing, so a routed member is a written member: a route
        # that is already true writes nothing, and would hide a miss.
        for decoder in self.sim.decoders.values():
            decoder.video, decoder.audio = "", ""
        self.sim.calls.clear()
        with self.assertLogs("luma_remote_api", level="INFO") as logged:
            response = self.admin.post("/api/user/route_group",
                                       json={"source": source, "group": group_id})
        intent = [line for line in logged.output if "[ROUTE-INTENT]" in line]
        return response, intent


class RouteTimeIdentityTests(RouteTimeBase):
    def setUp(self):
        super().setUp()
        self.gid = self.group([D1, D2])

    def test_an_address_re_leased_to_a_non_member_is_refused_not_written_to(self):
        # The cache still says D2 is at .22. The device answering there is now D6.
        self.sim.macs[DISPLAY_2["ip"]] = DISPLAY_6["mac"]
        response, _ = self.route_group(self.gid)
        body = response.get_json()
        self.assertEqual(self.written(), [DISPLAY_1["mac"]], "only the unit that is still D1 changed")
        self.assertEqual([r["ok"] for r in body["results"]], [True, False])
        self.assertEqual(body["results"][1]["reason"], "identity_mismatch")
        self.assertEqual((body["routed"], body["total"], body["ok"]), (1, 2, False))

    def test_a_device_that_will_not_say_who_it_is_is_not_written_to(self):
        self.sim.macs[DISPLAY_1["ip"]] = ""
        body = self.route_group(self.gid)[0].get_json()
        self.assertNotIn(DISPLAY_1["mac"], self.written())
        self.assertEqual(body["results"][0]["reason"], "identity_unconfirmed")

    def test_a_silent_device_is_a_failure_not_a_substitution(self):
        self.sim.down.add(DISPLAY_1["ip"])
        body = self.route_group(self.gid)[0].get_json()
        self.assertEqual(self.written(), [DISPLAY_2["mac"]])
        self.assertEqual([r["ok"] for r in body["results"]], [False, True])
        self.assertEqual(body["results"][0]["result"], "unreachable", "a silent unit did not answer")

    def test_an_identity_cached_under_two_addresses_is_unresolvable_not_guessed(self):
        stale = dict(DISPLAY_1, ip="198.51.100.99")
        with self.m.cache_lock:
            self.m.cache_units[stale["ip"]] = stale
        self.sim.add_decoder(stale["ip"])
        self.sim.macs[stale["ip"]] = DISPLAY_1["mac"]
        body = self.route_group(self.gid)[0].get_json()
        self.assertEqual(body["results"][0]["result"], "not_found")
        self.assertEqual(body["results"][0]["reason"], "ambiguous_identity")
        self.assertEqual(self.written(), [DISPLAY_2["mac"]])

    def test_a_source_address_re_leased_to_another_encoder_routes_nothing(self):
        self.sim.macs[_support.SOURCE_A["ip"]] = _support.SOURCE_B["mac"]
        body = self.route_group(self.gid)[0].get_json()
        self.assertEqual(self.written(), [])
        self.assertEqual({r["reason"] for r in body["results"]}, {"identity_mismatch"})

    def test_the_single_display_route_confirms_identity_too(self):
        self.sim.macs[DISPLAY_1["ip"]] = DISPLAY_6["mac"]
        self.sim.calls.clear()
        response = self.admin.post("/api/user/route", json={"source": A, "destination": D1})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["reason"], "identity_mismatch")
        self.assertEqual(self.written(), [])

    def test_the_intent_line_names_identities_before_routing_and_no_address(self):
        response, intent = self.route_group(self.gid)
        self.assertEqual(len(intent), 1)
        line = intent[0]
        self.assertIn("displays=%s,%s" % (D1, D2), line)
        self.assertIn("source=%s" % A, line)
        self.assertIn("groups=%s" % self.gid, line)
        self.assertNotIn("198.51.100", line)
        self.assertNotIn("192.0.2", line)

    def test_a_refused_route_still_logs_what_it_intended(self):
        self.sim.macs[DISPLAY_1["ip"]] = DISPLAY_6["mac"]
        _response, intent = self.route_group(self.gid)
        self.assertIn(D1, intent[0])


class GroupLifecycleTests(RouteTimeBase):
    """Create, route, edit, rename, re-address, restart, delete, recreate: one story."""

    def members_of(self, gid):
        return [m["id"] for m in next(g for g in self.admin.get("/api/admin/groups").get_json()["groups"]
                                      if g["id"] == gid)["members"]]

    def assert_routed_exactly(self, gid, expected_macs, source=A):
        response, intent = self.route_group(gid, source)
        body = response.get_json()
        self.assertEqual(response.status_code, 200, body)
        self.assertEqual(self.written(), sorted(canon_to_device(m) for m in expected_macs),
                         "the physical displays written to are not exactly the group's members")
        self.assertEqual(body["total"], len(expected_macs))
        self.assertEqual((body["routed"], body["ok"]), (len(expected_macs), True))
        self.assertEqual(len(intent), 1)
        for mac in expected_macs:
            self.assertIn(mac, intent[0])
        return body

    def test_the_whole_life_of_two_groups(self):
        # --- create A and B through the administrator's API; membership is exact
        made_a = self.admin.post("/api/admin/groups", json={"name": "Group A", "members": [D1, D2]}).get_json()
        made_b = self.admin.post("/api/admin/groups", json={"name": "Group B", "members": [D3, D4]}).get_json()
        a, b = made_a["group"]["id"], made_b["group"]["id"]
        self.assertNotEqual(a, b)
        self.assertEqual(self.members_of(a), [D1, D2])
        self.assertEqual(self.members_of(b), [D3, D4])

        # --- route A: only A's members; then B: only B's
        self.assert_routed_exactly(a, [D1, D2])
        self.assert_routed_exactly(b, [D3, D4])

        # --- edit A (D2 out, D3 in). B is untouched, and a display may be in both.
        self.admin.post("/api/admin/groups/" + a, json={"action": "set_members", "members": [D1, D3]})
        self.assertEqual(self.members_of(a), [D1, D3])
        self.assert_routed_exactly(a, [D1, D3])
        self.assert_routed_exactly(b, [D3, D4])

        # --- friendly names change: a name is not identity
        self.system.aliases.set_alias(D1, "Renamed Lobby")
        self.admin.post("/api/admin/groups/" + a, json={"action": "rename", "name": "Atrium"})
        self.assertEqual(self.members_of(a), [D1, D3])
        self.assert_routed_exactly(a, [D1, D3])

        # --- rescan with a changed IP. D5 takes D1's old address *before* D1 is found at its
        # new one, the order that used to delete D5's row and merge the two units.
        old_ip, new_ip = DISPLAY_1["ip"], "198.51.100.121"
        with self.m.cache_lock:
            index = self.m._mac_index_of_cache()
        self.sim.decoders[new_ip] = self.sim.decoders.pop(old_ip)         # D1 physically moves
        self.sim.macs.pop(old_ip, None)
        self.sim.macs[new_ip] = DISPLAY_1["mac"]
        self.sim.decoders[old_ip] = self.sim.decoders.pop(DISPLAY_5["ip"])   # D5 takes its address
        self.sim.macs.pop(DISPLAY_5["ip"], None)
        self.sim.macs[old_ip] = DISPLAY_5["mac"]
        with self.m.cache_lock:
            self.m._ingest_scanned_unit(old_ip, dict(DISPLAY_5, ip=old_ip), index)
            self.m._ingest_scanned_unit(new_ip, dict(DISPLAY_1, ip=new_ip), index)
            self.m.cache_units.pop(DISPLAY_5["ip"], None)
            held = {luma_projection.unit_identity(u): ip for ip, u in self.m.cache_units.items()}
        self.assertEqual(held[D1], new_ip)
        self.assertEqual(held[D5], old_ip)
        self.assert_routed_exactly(a, [D1, D3])      # D5 now sits at D1's old address: not touched
        self.assertNotIn(DISPLAY_5["mac"], self.written())

        # --- restart: the store is reloaded from disk; nothing differs
        self.system.groups.reload()
        self.assertEqual(self.members_of(a), [D1, D3])
        self.assertEqual(luma_groups.GroupStore(self.system.groups_file, canon).get(a)["members"], [D1, D3])
        self.assert_routed_exactly(a, [D1, D3])
        self.assert_routed_exactly(b, [D3, D4])

        # --- delete A: it cannot be resolved, nothing is written, B is unaffected
        gone = self.admin.post("/api/admin/groups/" + a, json={"action": "delete"})
        self.assertEqual(gone.status_code, 200)
        self.sim.calls.clear()
        missing = self.admin.post("/api/user/route_group", json={"source": A, "group": a})
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(self.sim.writes(), [])
        self.assertIsNone(self.system.groups.get(a))
        self.assert_routed_exactly(b, [D3, D4])

        # --- recreate A with the same name and a different member: a new id, no inheritance
        again = self.admin.post("/api/admin/groups", json={"name": "Atrium", "members": [D2]}).get_json()
        a2 = again["group"]["id"]
        self.assertNotIn(a2, (a, b))
        self.assertEqual(self.members_of(a2), [D2])
        self.sim.calls.clear()
        self.assertEqual(self.admin.post("/api/user/route_group", json={"source": A, "group": a}).status_code, 404)
        self.assertEqual(self.sim.writes(), [], "the deleted group id still resolves")
        self.assert_routed_exactly(a2, [D2])
        self.assert_routed_exactly(b, [D3, D4])

    def test_failures_are_truthful_and_a_stale_member_is_not_retained_or_replaced(self):
        gid = self.group([D1, "02:00:5e:99:99:99", D3])      # the middle unit is not in the inventory
        self.sim.down.add(DISPLAY_3["ip"])                    # and the last one has stopped answering
        response, _ = self.route_group(gid)
        body = response.get_json()
        self.assertEqual(self.written(), [DISPLAY_1["mac"]])
        self.assertEqual([r["ok"] for r in body["results"]], [True, False, False])
        self.assertEqual(body["results"][1]["result"], "not_found")
        self.assertEqual((body["routed"], body["total"], body["ok"]), (1, 3, False))

    def test_forgetting_a_cleared_display_removes_it_from_every_group(self):
        one = self.group([D1, D2], "One")
        two = self.group([D2, D3], "Two")
        self.system.groups.forget_members([D2])
        self.assertEqual(self.members_of(one), [D1])
        self.assertEqual(self.members_of(two), [D3])
        self.assert_routed_exactly(one, [D1])

    def test_routing_a_group_and_a_display_outside_it_in_one_request_stays_exact(self):
        a = self.group([D1, D2], "A")
        self.sim.calls.clear()
        response = self.admin.post("/api/user/route_multi", json={
            "source": A, "displays": [D5], "groups": [a], "interface": "standard"})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(self.written(), sorted([DISPLAY_1["mac"], DISPLAY_2["mac"], DISPLAY_5["mac"]]))
        self.assertNotIn(DISPLAY_6["mac"], self.written())

    def test_two_groups_in_one_request_route_the_union_once(self):
        a, b = self.group([D1, D2], "A"), self.group([D2, D3], "B")
        self.sim.calls.clear()
        body = self.admin.post("/api/user/route_multi", json={
            "source": A, "groups": [a, b], "interface": "standard"}).get_json()
        self.assertEqual(body["total"], 3)
        self.assertEqual(self.written(), sorted([DISPLAY_1["mac"], DISPLAY_2["mac"], DISPLAY_3["mac"]]))


def canon_to_device(mac):
    """The sim reports a MAC in the fixture's own spelling, which is already canonical."""
    return mac

# ============================================================== saved wall members
class WallMemberIdentityTests(RouteTimeBase):
    """A saved Video Wall's members are confirmed before they are written, on every route."""

    def setUp(self):
        super().setUp()
        self.wall([DISPLAY_1, DISPLAY_2], source=_support.SOURCE_A)

    def route_wall(self, mode="av", source=A):
        for decoder in self.sim.decoders.values():
            # Video-only keeps each display's audio, so there has to be some to keep.
            decoder.video, decoder.audio = "", ("audio-bravo" if mode == "video" else "")
        self.sim.calls.clear()
        with self.assertLogs("luma_remote_api", level="INFO") as logged:
            response = self.admin.post("/api/user/route_multi",
                                       json={"source": source, "walls": ["w1"], "mode": mode,
                                             "interface": "matrix"})
        return response, [line for line in logged.output if "[ROUTE-INTENT]" in line]

    def test_the_control_routes_both_members(self):
        response, _ = self.route_wall()
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(self.written(), sorted([DISPLAY_1["mac"], DISPLAY_2["mac"]]))

    def test_a_member_whose_address_was_re_leased_is_not_written_to(self):
        self.sim.macs[DISPLAY_2["ip"]] = DISPLAY_6["mac"]
        response, _ = self.route_wall()
        self.assertEqual(self.written(), [DISPLAY_1["mac"]], "only the member that is still itself")
        self.assertEqual([w for w in self.sim.writes() if w[0] == DISPLAY_2["ip"]], [])
        self.assertNotEqual(response.get_json()["results"][0]["result"], "verified")

    def refuses_mismatch_on(self, mode):
        # One plane per test: a second route in the same test would find the first one's writes
        # already held and write nothing, which would hide a miss.
        self.sim.macs[DISPLAY_2["ip"]] = DISPLAY_6["mac"]
        self.route_wall(mode=mode)
        self.assertEqual([w for w in self.sim.writes() if w[0] == DISPLAY_2["ip"]], [], mode)
        self.assertTrue([w for w in self.sim.writes() if w[0] == DISPLAY_1["ip"]],
                        "precondition: the confirmed member was written (%s)" % mode)

    def test_the_audio_and_video_plane_refuses_a_mismatched_member_before_writing(self):
        self.refuses_mismatch_on("av")

    def test_the_video_only_plane_refuses_a_mismatched_member_before_writing(self):
        self.refuses_mismatch_on("video")

    def test_the_audio_only_plane_refuses_a_mismatched_member_before_writing(self):
        self.refuses_mismatch_on("audio")

    def test_a_member_that_will_not_say_who_it_is_is_not_written_to(self):
        self.sim.macs[DISPLAY_1["ip"]] = ""
        self.route_wall()
        self.assertEqual(self.written(), [DISPLAY_2["mac"]])

    def test_a_member_identity_held_under_two_addresses_is_never_guessed(self):
        # The duplicate is listed both before and after the real row: neither first nor last wins.
        for position in ("after", "before"):
            with self.subTest(position=position):
                stale = dict(DISPLAY_1, ip="198.51.100.98")
                self.sim.add_decoder(stale["ip"])
                self.sim.macs[stale["ip"]] = DISPLAY_1["mac"]
                with self.m.cache_lock:
                    held = dict(self.m.cache_units)
                    self.m.cache_units.clear()
                    if position == "before":
                        self.m.cache_units[stale["ip"]] = stale
                    self.m.cache_units.update(held)
                    if position == "after":
                        self.m.cache_units[stale["ip"]] = stale
                self.route_wall()
                self.assertEqual(self.written(), [DISPLAY_2["mac"]], position)
                with self.m.cache_lock:
                    self.m.cache_units.pop(stale["ip"], None)

    def test_a_re_leased_source_address_routes_nothing(self):
        self.sim.macs[_support.SOURCE_A["ip"]] = _support.SOURCE_B["mac"]
        response, _ = self.route_wall()
        self.assertEqual(self.written(), [])
        self.assertNotEqual(response.get_json()["results"][0]["result"], "verified")

    def test_the_wall_route_names_member_identities_and_no_address(self):
        _response, intent = self.route_wall()
        wall_lines = [line for line in intent if "wall=w1" in line]
        self.assertEqual(len(wall_lines), 1)
        self.assertIn("members=%s,%s" % (D1, D2), wall_lines[0])
        self.assertIn("source=%s" % A, wall_lines[0])
        self.assertNotIn("198.51.100", wall_lines[0])

    def test_a_refused_wall_route_still_logs_what_it_intended(self):
        self.sim.macs[DISPLAY_1["ip"]] = DISPLAY_6["mac"]
        _response, intent = self.route_wall()
        self.assertTrue(any(D1 in line and "wall=w1" in line for line in intent))

    # ---- the Video Wall page's own route ------------------------------------------------
    def page_route(self):
        wall = self.m._load_video_wall_state()["walls"][0]
        for decoder in self.sim.decoders.values():
            decoder.video, decoder.audio = "", ""
        self.sim.calls.clear()
        with self.assertLogs(level="INFO") as logged:
            response = self.local.post("/api/video_wall/source", json={"wall": wall, "scope": "wall"})
        return response.get_json(), [l for l in logged.output if "[ROUTE-INTENT]" in l]

    def test_the_video_wall_page_confirms_each_member_too(self):
        self.sim.macs[DISPLAY_2["ip"]] = DISPLAY_6["mac"]
        body, _ = self.page_route()
        self.assertEqual(self.written(), [DISPLAY_1["mac"]])
        refused = body["results"][_support.wall_device_id(DISPLAY_2)]
        self.assertEqual((refused["ok"], refused["reason"]), (False, "identity_mismatch"))
        self.assertFalse(body["ok"])

    def test_the_video_wall_page_refuses_an_ambiguous_member(self):
        stale = dict(DISPLAY_1, ip="198.51.100.97")
        self.sim.add_decoder(stale["ip"])
        self.sim.macs[stale["ip"]] = DISPLAY_1["mac"]
        with self.m.cache_lock:
            self.m.cache_units[stale["ip"]] = stale
        body, _ = self.page_route()
        self.assertEqual(body["results"][_support.wall_device_id(DISPLAY_1)]["reason"], "ambiguous_identity")
        self.assertEqual(self.written(), [DISPLAY_2["mac"]])

    def test_the_video_wall_page_logs_member_identities(self):
        _body, intent = self.page_route()
        self.assertEqual(len(intent), 1)
        self.assertIn(D1, intent[0])
        self.assertIn(D2, intent[0])
        self.assertNotIn("198.51.100", intent[0])

    def test_the_video_wall_page_routes_confirmed_members(self):
        body, _ = self.page_route()
        self.assertTrue(body["ok"], body)
        self.assertEqual(self.written(), sorted([DISPLAY_1["mac"], DISPLAY_2["mac"]]))


# ============================================================== startup cache load
class StartupLoadDedupeTests(unittest.TestCase):
    """`_units_by_address`: one persisted row per hardware identity, the newest one."""

    def setUp(self):
        _support.RealApp(self)          # imports lumaserver with the fence and scratch data dir
        import lumaserver
        self.load = lumaserver._units_by_address

    @staticmethod
    def row(ip, mac, **extra):
        return dict({"ip": ip, "mac": mac, "hostname": "unit-" + ip}, **extra)

    def test_a_stale_persisted_duplicate_does_not_survive(self):
        rows = [self.row("203.0.113.10", "02:00:5e:40:00:01"), self.row("203.0.113.20", "02:00:5e:40:00:02"),
                self.row("203.0.113.11", "02:00:5e:40:00:01")]       # the first unit moved, appended last
        loaded = self.load(copy.deepcopy(rows))
        self.assertEqual(list(loaded), ["203.0.113.20", "203.0.113.11"], "newest kept, file order kept")

    def test_the_same_identity_in_another_spelling_is_the_same_unit(self):
        rows = [self.row("203.0.113.10", "02-00-5E-40-00-01"), self.row("203.0.113.11", "02:00:5e:40:00:01")]
        self.assertEqual(list(self.load(copy.deepcopy(rows))), ["203.0.113.11"])

    def test_rows_without_an_identity_are_kept_as_before(self):
        rows = [self.row("203.0.113.10", ""), self.row("203.0.113.11", ""), {"hostname": "no address"}, "junk",
                self.row("203.0.113.10", "", hostname="newer")]
        loaded = self.load(copy.deepcopy(rows))
        self.assertEqual(sorted(loaded), ["203.0.113.10", "203.0.113.11"])
        self.assertEqual(loaded["203.0.113.10"]["hostname"], "newer", "the later row at one address wins")

    def test_distinct_units_are_all_kept(self):
        rows = [self.row("203.0.113.%d" % n, "02:00:5e:40:00:%02x" % n) for n in range(1, 9)]
        self.assertEqual(len(self.load(copy.deepcopy(rows))), 8)

    def test_nothing_is_an_empty_load(self):
        self.assertEqual(self.load([]), {})
        self.assertEqual(self.load(None), {})

if __name__ == "__main__":
    unittest.main()

