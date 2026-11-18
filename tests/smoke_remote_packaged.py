# Run by hand against a packaged build:  python tests/smoke_remote_packaged.py <path to the executable>
# Not part of unittest discovery (it launches the artifact), and not run in CI.
"""The remote flow, end to end, against the packaged artifact: bootstrap an administrator, make a
certificate, turn on HTTPS on a real adapter address, sign in over TLS, and check what each kind
of caller can and cannot reach. A throwaway data folder; the listener lives for a few seconds and
is turned off at the end. Nothing is written to any device (there are none in the folder)."""
import http.cookiejar
import json
import os
import re
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

EXE = sys.argv[1]


def kill_tree(proc):
    """End the artifact and anything its bootloader started."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
    else:
        proc.kill()

LOCAL_PORT = 18192
results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(("PASS  " if ok else "FAIL  ") + name + ((" -- " + str(detail)) if detail and not ok else ""))


folder = tempfile.mkdtemp(prefix="luma-smoke-remote-")
for name, body in {"units_cache.json": [], "video_walls.json": {"kind": "lumasuite.video_walls", "version": 1, "walls": []},
                   "producer_config.json": {}, "producer_state.json": {"ticker": {"dir": "rtl", "rows": []}, "streams": []},
                   "producer2_state.json": {"streams": [], "ticker": {}}}.items():
    json.dump(body, open(os.path.join(folder, name), "w", encoding="utf-8"))

proc = subprocess.Popen([EXE, "--host=127.0.0.1", "--port=%d" % LOCAL_PORT],
                        env=dict(os.environ, LUMA_DATA_DIR=folder, BROWSER=("cmd /c rem %s" if os.name == "nt" else "true %s")),
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def local(method, path, body=None):
    request = urllib.request.Request("http://127.0.0.1:%d%s" % (LOCAL_PORT, path), method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=20) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as err:
        try:
            return err.code, json.loads(err.read() or b"{}")
        except Exception:
            return err.code, {}


class Remote:
    """A browser: its own cookie jar, talking HTTPS to the address LumaSuite advertised."""

    def __init__(self, base):
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        context = ssl._create_unverified_context()          # a self-signed certificate, by design
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar),
                                                  urllib.request.HTTPSHandler(context=context))
        self.csrf = None

    def call(self, method, path, body=None, csrf=True):
        headers = {"Content-Type": "application/json", "Sec-Fetch-Mode": "cors"}
        if self.csrf and csrf and method != "GET":
            headers["X-LumaSuite-CSRF"] = self.csrf
        request = urllib.request.Request(self.base + path, method=method, headers=headers,
                                         data=json.dumps(body).encode() if body is not None else None)
        try:
            with self.opener.open(request, timeout=20) as r:
                raw = r.read()
                try:
                    return r.status, json.loads(raw or b"{}"), r.headers
                except ValueError:
                    return r.status, raw, r.headers
        except urllib.error.HTTPError as err:
            raw = err.read()
            try:
                return err.code, json.loads(raw or b"{}"), err.headers
            except ValueError:
                return err.code, raw, err.headers


try:
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            local("GET", "/api/app_version")
            break
        except Exception:
            time.sleep(0.5)

    code, body = local("POST", "/api/bootstrap/admin", {"username": "smokeadmin", "password": "smoke admin pass 1", "confirm": "smoke admin pass 1"})
    check("the first administrator is created on the local computer", code == 200 and body.get("ok"), body)
    code, body = local("GET", "/api/remote_access")
    adapters = body.get("adapters") or []
    check("the packaged application lists network adapters", code == 200 and len(adapters) >= 1, body.get("adapters"))
    adapter = adapters[0]
    address = adapter["address"]

    code, body = local("POST", "/api/remote_access", {"enabled": True, "adapter_id": adapter["id"]})
    check("HTTPS with no certificate refuses to start", code == 400 and "certificate" in json.dumps(body).lower(), body)
    code, body = local("POST", "/api/admin/certificate", {"action": "generate", "address": address})
    check("a certificate is generated inside the package", code == 200 and body.get("ok"), body)
    check("the response carries no key material", "PRIVATE" not in json.dumps(body) and "BEGIN" not in json.dumps(body))
    key_file = os.path.join(folder, "remote_key.pem")
    if os.name != "nt":
        check("the private key is narrowed to its owner", (os.stat(key_file).st_mode & 0o077) == 0)
    code, body = local("POST", "/api/remote_access", {"enabled": True, "adapter_id": adapter["id"], "port": 18193})
    check("HTTPS turns on", code == 200 and body.get("ok") and body.get("secure") is True, body)

    base = "https://%s:%d" % (address, 18193)
    admin = Remote(base)
    code, body, _ = admin.call("GET", "/api/config")
    check("over TLS, an unauthenticated API call is 401", code == 401, code)
    code, body, headers = admin.call("POST", "/api/auth/login", {"username": "smokeadmin", "password": "wrong password!!"})
    check("a wrong password is refused", code == 401)
    code, body, headers = admin.call("POST", "/api/auth/login", {"username": "smokeadmin", "password": "smoke admin pass 1"})
    admin.csrf = (body or {}).get("csrf_token")
    cookie = headers.get("Set-Cookie", "")
    check("sign-in over TLS succeeds with an HttpOnly, SameSite, Secure cookie",
          code == 200 and "HttpOnly" in cookie and "SameSite=Lax" in cookie and "Secure" in cookie, cookie)
    code, body, _ = admin.call("GET", "/api/config")
    check("an administrator reaches the normal pages' API", code == 200 and "password" not in body, body)
    code, body, _ = admin.call("GET", "/api/config?include_password=1")
    check("but is refused the device passwords", code == 403, code)
    code, body, _ = admin.call("GET", "/api/list_dir")
    check("and cannot browse the host filesystem", code == 404, code)
    code, body, _ = admin.call("POST", "/api/clear_units", {}, csrf=False)
    check("a state-changing call without the CSRF token is refused", code == 403, code)
    code, raw, _ = admin.call("GET", "/ui/matrix.html")
    check("an administrator can open an operator page over TLS", code == 200 and len(raw) > 100, code)

    code, body, _ = admin.call("POST", "/api/admin/users", {"username": "smokeuser", "password": "smoke user pass 22", "role": "user", "permissions": {}})
    check("an administrator creates a user", code == 200 and body.get("ok"), body)

    user = Remote(base)
    code, body, _ = user.call("POST", "/api/auth/login", {"username": "smokeuser", "password": "smoke user pass 22"})
    user.csrf = (body or {}).get("csrf_token")
    check("the user signs in", code == 200)
    code, raw, _ = user.call("GET", "/user")
    check("the user reaches the routing page", code == 200 and b"LumaSuite" in raw, code)
    code, body, _ = user.call("GET", "/api/user/state")
    # The exact projection (luma_projection.user_state plus the three keys the route adds): rev is a
    # permission digest, routing is two interface booleans, observed is the shared refresh's own age
    # (fresh / age_s / refreshing), walls is the permitted saved walls. None carries a device identity.
    check("the user sees an empty, allowlisted state",
          code == 200 and set(body) == {"ok", "role", "username", "sources", "destinations", "groups", "walls",
                                        "rev", "routing", "observed"}
          and body["sources"] == [] and body["destinations"] == [] and body["groups"] == [] and body["walls"] == [],
          body)
    check("the user state's shape is exactly the intended one",
          body.get("role") == "user" and body.get("username") == "smokeuser"
          and isinstance(body.get("rev"), str) and re.fullmatch(r"[0-9a-f]{8,64}|all", body["rev"]) is not None
          and set(body.get("routing") or {}) == {"standard", "matrix"}
          and all(isinstance(v, bool) for v in body["routing"].values())
          and set(body.get("observed") or {}) <= {"fresh", "age_s", "refreshing"}, body)
    flat = json.dumps(body)
    check("and no address, hardware address or credential-like value appears in it",
          not re.search(r"\d{1,3}(\.\d{1,3}){3}", flat)
          and not re.search(r"(?i)[0-9a-f]{2}([:-][0-9a-f]{2}){5}", flat)
          and not re.search(r"(?i)password|passwd|secret|token|session|cookie|private", flat), flat)
    for path in ("/ui/index.html", "/ui/settings.js", "/ui/admin_users.js", "/api/config", "/api/admin/users", "/api/cache"):
        code, _, _ = user.call("GET", path)
        check("the user cannot reach " + path, code == 403, code)
    code, _, _ = user.call("GET", "/ui/user.js")
    check("but can load the routing page's own script", code == 200, code)

    code, body = local("POST", "/api/remote_access", {"enabled": False})
    check("remote access turns off", code == 200 and body.get("enabled") is False, body)
    try:
        urllib.request.build_opener(urllib.request.HTTPSHandler(context=ssl._create_unverified_context())).open(base + "/login", timeout=4)
        check("and the network listener is gone", False, "still answering")
    except Exception:
        check("and the network listener is gone", True)
finally:
    kill_tree(proc)

print("\n%d of %d checks passed" % (sum(results), len(results)))
sys.exit(0 if all(results) else 1)
