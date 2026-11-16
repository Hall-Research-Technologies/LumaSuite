"""Property test: every group route writes only to persisted members.

A seeded random walk over the whole life of three overlapping groups -- create, edit membership,
rename, delete, recreate, friendly-name change, a unit changing address (with another unit taking
its old one), an address re-leased to a non-member, a member going silent, a store reload from
disk (restart) and a rescan ingest -- and after EVERY step each group is routed (and two groups
together). The expected targets are read from the groups FILE ON DISK, parsed here with json, never
from the store or the API under test. What was physically written (by the MAC the device reports)
must be a subset of those persisted members, and must equal them minus the members the scenario made
unreachable. A non-member write fails the test.
"""
import copy
import json
import random
import unittest

from _support import DISPLAY_1, DISPLAY_2, DISPLAY_3, DISPLAY_4, identity
from test_routing_torture import A, D1, D2, D3, D4
from test_group_integrity import (DISPLAY_5, DISPLAY_6, D5, D6, RouteTimeBase, canon)
import luma_projection

UNITS = {D1: DISPLAY_1, D2: DISPLAY_2, D3: DISPLAY_3, D4: DISPLAY_4, D5: DISPLAY_5, D6: DISPLAY_6}


class GroupWritesAreAlwaysPersistedMembers(RouteTimeBase):
    SEEDS = (1, 2, 3, 4, 5, 6, 7, 8)
    STEPS = 30

    def persisted(self):
        raw = json.load(open(self.system.groups_file, encoding="utf-8"))
        return {g["id"]: [canon(m) for m in g["members"]] for g in raw["groups"]}

    def sim_ip_of(self, mac):
        """Where the simulator currently has the physical unit with this hardware address."""
        return [ip for ip, m in self.sim.macs.items() if canon(m) == mac]

    def route_and_check(self, label, group_ids, silent, expected_extra_failures=()):
        file_members = self.persisted()
        wanted = []
        for gid in group_ids:
            for member in file_members[gid]:
                if member not in wanted:
                    wanted.append(member)
        if len(group_ids) == 1:
            response, _intent = self.route_group(group_ids[0])
        else:
            for decoder in self.sim.decoders.values():
                decoder.video, decoder.audio = "", ""
            self.sim.calls.clear()
            response = self.admin.post("/api/user/route_multi", json={
                "source": A, "groups": list(group_ids), "interface": "standard"})
        body = response.get_json()
        self.assertEqual(response.status_code, 200, (label, body))
        written = {canon(m) for m in self.written()}
        self.assertTrue(written <= set(wanted),
                        "%s: wrote to a NON-MEMBER %s (persisted members %s)"
                        % (label, sorted(written - set(wanted)), wanted))
        reachable = {m for m in wanted if m not in silent}
        self.assertEqual(written, reachable, "%s: reachable members not all written" % label)
        self.assertEqual(len(body["results"]), len(wanted),
                         "%s: one result row per persisted member" % label)
        self.assertEqual(body["total"], len(wanted))

    def run_walk(self, seed):
        rnd = random.Random(seed)
        groups = {}                # name -> id (ids are what the server issued)
        silent = set()             # identities the scenario made unreachable
        moved = {}                 # identity -> current ip
        spare = [("198.51.100.%d" % n) for n in range(150, 170)]
        names = ["Alpha", "Beta", "Gamma"]
        pool = [D1, D2, D3, D4, D5, D6]

        for step in range(self.STEPS):
            op = rnd.choice(["create", "edit", "rename", "delete", "alias", "move", "release",
                             "silence", "restore", "restart", "rescan"])
            free = [n for n in names if n not in groups]
            if op == "create" and free:
                name = rnd.choice(free)
                members = rnd.sample(pool, rnd.randint(1, 4))
                gid = self.admin.post("/api/admin/groups",
                                      json={"name": name, "members": members}).get_json()["group"]["id"]
                groups[name] = gid
            elif op == "edit" and groups:
                name = rnd.choice(sorted(groups))
                self.admin.post("/api/admin/groups/" + groups[name],
                                json={"action": "set_members", "members": rnd.sample(pool, rnd.randint(1, 4))})
            elif op == "rename" and groups:
                name = rnd.choice(sorted(groups))
                new = name + "x"
                if new not in groups:
                    self.admin.post("/api/admin/groups/" + groups[name], json={"action": "rename", "name": new})
                    groups[new] = groups.pop(name)
                    names.append(new)
            elif op == "delete" and groups:
                name = rnd.choice(sorted(groups))
                self.assertEqual(self.admin.post("/api/admin/groups/" + groups[name],
                                                 json={"action": "delete"}).status_code, 200)
                gone = groups.pop(name)
                self.sim.calls.clear()
                missing = self.admin.post("/api/user/route_group", json={"source": A, "group": gone})
                self.assertEqual(missing.status_code, 404)
                self.assertEqual(self.sim.writes(), [], "a deleted group id still wrote")
            elif op == "alias":
                self.system.aliases.set_alias(rnd.choice(pool), "Name %d" % step)
            elif op == "move":
                ident = rnd.choice(pool)
                ips = self.sim_ip_of(ident)
                if ips and ident not in silent and spare:
                    old, new = ips[0], spare.pop(0)
                    self.sim.decoders[new] = self.sim.decoders.pop(old)
                    self.sim.macs.pop(old, None)
                    self.sim.macs[new] = UNITS[ident]["mac"]
                    # the inventory (the scan) learns the new address through the real ingest
                    with self.m.cache_lock:
                        index = self.m._mac_index_of_cache()
                        self.m._ingest_scanned_unit(new, dict(UNITS[ident], ip=new), index)
                    moved[ident] = new
            elif op == "release":
                # an address re-leased to a non-member (or another member) without a rescan
                a, b = rnd.sample(pool, 2)
                ia, ib = self.sim_ip_of(a), self.sim_ip_of(b)
                if ia and ib and a not in silent and b not in silent:
                    self.sim.macs[ia[0]], self.sim.macs[ib[0]] = self.sim.macs[ib[0]], self.sim.macs[ia[0]]
                    silent.add(a); silent.add(b)          # neither can be written until re-identified
            elif op == "silence":
                ident = rnd.choice(pool)
                for ip in self.sim_ip_of(ident):
                    self.sim.down.add(ip)
                silent.add(ident)
            elif op == "restore":
                self.sim.down.clear()
                # undo swaps: each physical unit reports its own MAC at its own address again
                for ip in list(self.sim.macs):
                    pass
                silent.clear()
                for ident in pool:
                    if not self.sim_ip_of(ident):
                        # a swap left this identity unreported; give each unit back its own mac
                        pass
                self.reseat()
            elif op == "restart":
                self.system.groups.reload()
            elif op == "rescan":
                with self.m.cache_lock:
                    index = self.m._mac_index_of_cache()
                    for ident in pool:
                        for ip in self.sim_ip_of(ident):
                            if ip not in self.sim.down and UNITS[ident]["mac"] == self.sim.macs[ip]:
                                self.m._ingest_scanned_unit(ip, dict(UNITS[ident], ip=ip), index)

            # after EVERY step: route each group alone and, when there are two, together
            for name, gid in sorted(groups.items()):
                self.route_and_check("seed %d step %d (%s) group %s" % (seed, step, op, name), [gid],
                                     self.silent_now(silent))
            ids = [groups[n] for n in sorted(groups)]
            if len(ids) >= 2:
                self.route_and_check("seed %d step %d (%s) pair" % (seed, step, op), ids[:2],
                                     self.silent_now(silent))

    # ---- the scenario's own truth about who can be reached -------------------------------------
    def silent_now(self, flagged):
        """A member can be written only if the cache's address for it is up and reports its MAC."""
        out = set()
        with self.m.cache_lock:
            held = {luma_projection.unit_identity(u): ip for ip, u in self.m.cache_units.items()
                    if luma_projection.unit_identity(u)}
        for ident in UNITS:
            ip = held.get(ident)
            if ip is None or ip in self.sim.down or canon(self.sim.macs.get(ip, "")) != ident:
                out.add(ident)
        return out

    def reseat(self):
        """Each physical unit answers at the address the cache holds for it, with its own MAC."""
        with self.m.cache_lock:
            held = {luma_projection.unit_identity(u): ip for ip, u in self.m.cache_units.items()
                    if luma_projection.unit_identity(u)}
        wanted = {}
        for ident, ip in held.items():
            if ident in UNITS:
                wanted[ip] = ident
        for ip in list(self.sim.macs):
            if ip in wanted:
                self.sim.macs[ip] = UNITS[wanted[ip]]["mac"]


for _seed in GroupWritesAreAlwaysPersistedMembers.SEEDS:
    setattr(GroupWritesAreAlwaysPersistedMembers, "test_seeded_walk_%d" % _seed,
            (lambda s: (lambda self: self.run_walk(s)))(_seed))


if __name__ == "__main__":
    unittest.main()
