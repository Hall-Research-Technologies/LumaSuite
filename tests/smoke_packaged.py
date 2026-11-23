# Run against a packaged build:  python tests/smoke_packaged.py <path to the executable>
# Not part of unittest discovery or run_tests.py (it launches the artifact). The release workflow runs it on
# the executable extracted from each distributed zip, on both distributed platforms.
"""Start the packaged artifact and ask it questions. "PyInstaller completed successfully" is
not evidence that anything runs: a missing data file or an uncollected import builds cleanly and
fails only when a person opens it.

The artifact runs against a throwaway data folder, pre-seeded with empty state so that nothing
from a developer's real runtime files is migrated into it, and its browser launch is suppressed.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

EXE = sys.argv[1]


def _source_version():
    """The version the packaged artifact must report: read from lumaserver.py, never typed here."""
    import re
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lumaserver.py")
    with open(path, encoding="utf-8") as handle:
        return re.search(r'^__version__ = "([^"]+)"', handle.read(), re.M).group(1)


SOURCE_VERSION = _source_version()


def kill_tree(proc):
    """End the artifact and anything its bootloader started."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
    else:
        proc.kill()

PORT_LOCAL, PORT_NETWORK = 18190, 18191


def seed():
    folder = tempfile.mkdtemp(prefix="luma-smoke-")
    seeds = {"units_cache.json": [], "video_walls.json": {"kind": "lumasuite.video_walls", "version": 1, "walls": []},
             "producer_config.json": {}, "producer_state.json": {"ticker": {"dir": "rtl", "rows": []}, "streams": []},
             "producer2_state.json": {"streams": [], "ticker": {}}}
    for name, body in seeds.items():
        with open(os.path.join(folder, name), "w", encoding="utf-8") as handle:
            json.dump(body, handle)
    return folder


def get(port, path, timeout=5):
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d%s" % (port, path), timeout=timeout) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as err:
        return err.code, err.read(), dict(err.headers)
    except Exception:
        return None, b"", {}


def launch(host, port):
    env = dict(os.environ, LUMA_DATA_DIR=seed(), BROWSER=('cmd /c rem %s' if os.name == 'nt' else 'true %s'))
    proc = subprocess.Popen([EXE, "--host=" + host, "--port=%d" % port], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc


def stop(proc):
    kill_tree(proc)


def wait_for(port, path, seconds=120):
    deadline = time.time() + seconds
    while time.time() < deadline:
        status, body, _ = get(port, path)
        if status is not None:
            return status, body
        time.sleep(0.5)
    return None, b""


def get_json(port, path, seconds=90):
    """Ask until the endpoint gives a real answer: HTTP 200 and a JSON object, or the deadline.

    /api/remote_access lists the network adapters, which the application does by running PowerShell
    on every call (bounded at 25 s by the product itself). Measured on a cold start that is 3 to 7
    seconds a call, so the default 5 s read timeout of get() turns a slow answer into "no answer" --
    which is what a failure of two checks once looked like. The readiness signal is the answer
    itself, so each attempt is allowed longer than the product's own bound and the last status is
    returned, never guessed.
    """
    deadline = time.time() + seconds
    last = (None, {})
    while True:
        status, body, _ = get(port, path, timeout=40)
        try:
            parsed = json.loads(body) if status == 200 else None
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            return status, parsed
        last = (status, {})
        if time.time() > deadline:
            return last
        time.sleep(0.5)


results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(("PASS  " if ok else "FAIL  ") + name + (("  -- " + detail) if detail and not ok else ""))


# ---------------------------------------------------------------- the local listener
proc = launch("127.0.0.1", PORT_LOCAL)
try:
    status, body = wait_for(PORT_LOCAL, "/api/app_version")
    check("the packaged application starts and answers", status == 200, str(status))
    version = json.loads(body or b"{}").get("version") if status == 200 else None
    check("it reports the version it was built from (lumaserver.py, the one authoritative version)",
          version == SOURCE_VERSION, "%s != %s" % (version, SOURCE_VERSION))

    for path in ("/", "/ui/matrix.html", "/ui/wall.html", "/ui/producer.html", "/ui/manual.html",
                 "/ui/user.html", "/ui/admin_users.html", "/ui/login.html", "/login", "/user", "/admin/users",
                 "/ui/user.js", "/ui/login.js", "/ui/remote.css", "/ui/csrf.js", "/ui/remember.js",
                 "/ui/adminnav.js", "/ui/remote_settings.js", "/ui/admin_users.js", "/ui/family.css",
                 "/ui/theme-boot.js", "/ui/dialogs.js", "/ui/settings.js", "/ui/port_settings.js",
                 "/ui/companylogo.png"):
        code, data, _ = get(PORT_LOCAL, path)
        check("page or asset: " + path, code == 200 and len(data) > 20, "HTTP %s, %d bytes" % (code, len(data)))

    # The User Guide's screenshots are inside the package: every picture the guide names is served as
    # an image, the guide is not an empty shell, and it asks for its version from the running server.
    code, guide, _ = get(PORT_LOCAL, "/ui/manual.html")
    guide_text = (guide or b"").decode("utf-8", "replace")
    pictures = re.findall(r'<img\b[^>]*\bsrc="(guide/[^"]+)"', guide_text)
    check("the User Guide names its screenshots", code == 200 and len(pictures) >= 25, "%s pictures" % len(pictures))
    broken = []
    for picture in pictures:
        code, data, headers = get(PORT_LOCAL, "/ui/" + picture)
        if code != 200 or not (data or b"").startswith(b"\x89PNG") or "image/png" not in str(headers.get("Content-Type", "")):
            broken.append("%s: HTTP %s" % (picture, code))
    check("every screenshot in the User Guide is served from the package as a PNG", not broken, "; ".join(broken[:5]))
    check("the User Guide asks the server for its version and does not carry one", "/api/app_version" in guide_text
          and SOURCE_VERSION not in re.sub(r"<script.*?</script>", "", guide_text, flags=re.S))

    # The local application port block: the launcher registered its controller, so the packaged
    # build answers with real data (a source run that was not started by the launcher says "not available").
    code, data, _ = get(PORT_LOCAL, "/api/local_port")
    try:
        port_state = json.loads(data or b"{}")
    except ValueError:
        port_state = {}
    check("the local application port endpoint reports the port in use",
          code == 200 and port_state.get("available") is True and port_state.get("current_port") == PORT_LOCAL,
          "HTTP %s %s" % (code, port_state))

    # /license serves LumaSuite's own licence (with section 11A); the third-party notices and one bundled
    # text must be in the package too.
    code, data, _ = get(PORT_LOCAL, "/license")
    check("the licence route serves the product licence with section 11A",
          code == 200 and b"11A. COMPONENTS LICENSED UNDER" in (data or b""), "HTTP %s" % code)
    code, data, _ = get(PORT_LOCAL, "/licenses/pystray/COPYING.LGPL")
    check("the pystray LGPL text is served from the package", code == 200 and b"GNU LESSER GENERAL PUBLIC LICENSE" in (data or b"").upper(), "HTTP %s" % code)
    code, data, _ = get(PORT_LOCAL, "/licenses/flask/LICENSE.txt")
    check("a bundled third-party licence text is served from the package",
          code == 200 and b"Pallets" in (data or b""), "HTTP %s" % code)
    code, data, _ = get(PORT_LOCAL, "/api/auth/session")
    session = json.loads(data or b"{}")
    check("the local operator is recognised as local", code == 200 and session.get("local") is True, repr(session))
    code, remote = get_json(PORT_LOCAL, "/api/remote_access")
    check("remote access is off by default", code == 200 and remote.get("enabled") is False and remote.get("has_admin") is False,
          "HTTP %s %s" % (code, repr({k: remote.get(k) for k in ("enabled", "has_admin")})))
    check("the certificate generator is available in the package", (remote.get("certificate") or {}).get("can_generate") is True,
          repr(remote.get("certificate")))
    code, data, _ = get(PORT_LOCAL, "/api/bootstrap/state")
    check("first-administrator bootstrap is reachable locally", code == 200 and json.loads(data).get("needs_admin") is True)
    code, data, _ = get(PORT_LOCAL, "/api/user/state")
    check("the user state answers locally with an empty inventory", code == 200 and json.loads(data).get("sources") == [])
    code, data, _ = get(PORT_LOCAL, "/api/config?include_password=1")
    check("the device password is available to the local computer", code == 200 and "password" in json.loads(data))
    code, data, _ = get(PORT_LOCAL, "/api/user/revision")
    check("the permission revision answers locally", code == 200 and json.loads(data).get("ok") is True)
finally:
    stop(proc)

# ------------------------------------------------------ a listener bound to a network address
time.sleep(1)
proc = launch("0.0.0.0", PORT_NETWORK)
try:
    status, body = wait_for(PORT_NETWORK, "/api/app_version")
    check("a copy bound to every interface treats even a loopback connection as remote (refused: remote access is off)",
          status == 404, "HTTP %s" % status)
    code, data, _ = get(PORT_NETWORK, "/api/config?include_password=1")
    check("and a remote caller can never read the device password", code == 404 and b"password" not in data.lower().replace(b"remote access is disabled", b""),
          "HTTP %s" % code)
    code, _, _ = get(PORT_NETWORK, "/api/list_dir")
    check("and cannot browse the host filesystem", code == 404, "HTTP %s" % code)
finally:
    stop(proc)

print("\n%d of %d checks passed" % (sum(results), len(results)))
sys.exit(0 if all(results) else 1)
