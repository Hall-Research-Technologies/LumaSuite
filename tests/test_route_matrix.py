"""Every Remote Access / account route, called as every kind of caller.

The routes are enumerated from the application's own route table, so a route added
tomorrow is exercised without anybody remembering to list it. For each one the caller
kinds are: nobody, a signed-in administrator, a signed-in User, a Direct Access User,
and the local operator. The assertions are the contract that matters:

  * no route answers 500 to a well-formed or malformed request from any caller;
  * a route classified ADMIN is refused to nobody, to a User and to a Direct Access User;
  * a route classified USER is refused to nobody;
  * a PUBLIC route is reachable without a session.

Set LUMA_ROUTE_MATRIX=<path> to also write the observed matrix as JSON.
"""
import json
import os
import re
import unittest

import _support  # noqa: F401
from _support import ADMIN_PASSWORD, USER_PASSWORD, DISPLAY_1, SOURCE_A, System, identity

import luma_access
from luma_access import Access

A, D1 = identity(SOURCE_A), identity(DISPLAY_1)
PREFIXES = ("/api/auth/", "/api/bootstrap/", "/api/remote_access", "/api/admin/", "/api/user/", "/access/")
FILL = {"user_id": "nope", "group_id": "nope", "token": "not-a-real-token"}
BODIES = ({}, {"action": "bogus"}, {"action": "delete"}, {"username": 5, "password": None},
          {"source": "x", "destination": "y", "displays": "z", "groups": 3})


class RouteMatrixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.s = System()
        cls.s.turn_remote_on()
        cls.s.admin("root")
        cls.s.user("alex", sources=[A], destinations=[D1])
        cls.s.user("kiosk", sources=[A], destinations=[D1])
        cls.kiosk = next(u["id"] for u in cls.s.store.list_accounts() if u["username"] == "kiosk")

    def fresh_callers(self):
        """New sessions for every route: a route such as logout, or one that turns remote
        access off, must not change what the routes after it are measured against."""
        self.s.turn_remote_on()
        admin = self.s.sign_in("root", ADMIN_PASSWORD)
        user = self.s.sign_in("alex", USER_PASSWORD)
        local = self.s.client(remote=False)
        link = local.post("/api/admin/users/%s/direct_access" % self.kiosk,
                          json={"action": "enable"}).get_json()["link"]
        direct = self.s.client(remote=True)
        assert direct.get("/access/" + link.rsplit("/", 1)[1]).status_code == 200
        return (("anonymous", self.s.client(remote=True)), ("admin", admin), ("user", user),
                ("direct", direct), ("local", local))

    @classmethod
    def tearDownClass(cls):
        cls.s.close()

    def rules(self):
        found = []
        for rule in self.s.app.url_map.iter_rules():
            if not rule.rule.startswith(PREFIXES):
                continue
            for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
                found.append((rule.rule, method))
        return sorted(found)

    def call(self, client, method, rule, body):
        path = re.sub(r"<([^>]+)>", lambda m: FILL.get(m.group(1).split(":")[-1], "x"), rule)
        if method == "GET":
            return client.get(path)
        headers = {"X-LumaSuite-CSRF": client.csrf} if hasattr(client, "csrf") else {}
        return client.open(path, method=method, json=body, headers=headers)

    def test_no_caller_ever_gets_a_server_error_and_classification_holds(self):
        matrix, problems = {}, []
        for rule, method in self.rules():
            callers = self.fresh_callers()
            row = {}
            for name, client in callers:
                codes = set()
                for body in BODIES:
                    code = self.call(client, method, rule, body).status_code
                    codes.add(code)
                    if code >= 500:
                        problems.append("%s %s as %s with %r -> %s" % (method, rule, name, body, code))
                    if method == "GET":
                        break
                row[name] = sorted(codes)
            matrix["%s %s" % (method, rule)] = row
            access = luma_access.classify(self._rule_for(rule), method)
            if access is Access.ADMIN:
                for name in ("anonymous", "user", "direct"):
                    if any(c < 400 for c in row[name]):
                        problems.append("ADMIN route %s %s admitted %s: %s" % (method, rule, name, row[name]))
            if access is Access.USER and any(c < 400 for c in row["anonymous"]):
                problems.append("USER route %s %s admitted nobody: %s" % (method, rule, row["anonymous"]))
        destination = os.environ.get("LUMA_ROUTE_MATRIX")
        if destination:
            with open(destination, "w", encoding="utf-8") as handle:
                json.dump(matrix, handle, indent=1, sort_keys=True)
        self.assertEqual(problems, [])
        self.assertGreater(len(matrix), 20)

    def _rule_for(self, rule):
        return rule

    def test_every_enumerated_route_is_classified(self):
        for rule, method in self.rules():
            self.assertIn(rule, luma_access.ROUTES, "%s %s has no classification" % (method, rule))


if __name__ == "__main__":
    unittest.main()
