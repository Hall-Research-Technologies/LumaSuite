"""Measure how far apart a multi-display route starts its writes, against a RUNNING LumaSuite.

    python tools/measure_route_spread.py --base http://127.0.0.1:5000 \\
        --source 02:00:5e:10:00:0a --displays 02:00:5e:20:00:01,02:00:5e:20:00:02 --confirm

The server answers every multi-destination route with a `timing` object: milliseconds since the
batch began, for the first and last write operation to START and to FINISH, and the bound on how
many ran at once. The number that matters is `start_spread_ms` -- first start to last start. A
sequential route puts it at about (displays - 1) x (time per display); the bounded parallel batch
puts it near zero for up to `workers` displays.

THIS SENDS A REAL ROUTE. It changes what the named displays show, so nothing is sent without
`--confirm`. It is meant to be run by someone watching the displays, on equipment they may change.

Authentication: from the machine running the app (loopback) nothing is needed. For a remote address
give `--user` and `--password`; the script signs in, carries the session cookie and the CSRF token,
and signs out. Passwords are read from the argument or the LUMA_MEASURE_PASSWORD environment
variable and are never printed.

Options choose what to route: --groups, --walls, --mode (av|video|audio; the matrix interface) or
--no-audio-follows-video (Standard routing, video only). --repeat runs it several times, alternating
between --source and --alt-source when given, so the displays visibly change every time.
"""
import argparse
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from http.cookiejar import CookieJar


def build_opener():
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))


def call(opener, base, path, body=None, csrf=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if csrf:
        headers["X-LumaSuite-CSRF"] = csrf
    request = urllib.request.Request(base.rstrip("/") + path, data=data, headers=headers,
                                     method="POST" if body is not None else "GET")
    started = time.monotonic()
    try:
        with opener.open(request, timeout=120) as response:
            payload = json.loads(response.read().decode("utf-8") or "{}")
            return response.status, payload, time.monotonic() - started
    except urllib.error.HTTPError as err:
        try:
            payload = json.loads(err.read().decode("utf-8") or "{}")
        except ValueError:
            payload = {}
        return err.code, payload, time.monotonic() - started


def listed(text):
    return [part.strip() for part in (text or "").split(",") if part.strip()]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base", required=True, help="e.g. http://127.0.0.1:5000")
    ap.add_argument("--source", required=True, help="source MAC (canonical)")
    ap.add_argument("--alt-source", default="", help="a second source, alternated with --source")
    ap.add_argument("--displays", default="", help="comma-separated display MACs")
    ap.add_argument("--groups", default="", help="comma-separated group ids")
    ap.add_argument("--walls", default="", help="comma-separated saved wall ids")
    ap.add_argument("--mode", default="av", choices=("av", "video", "audio"),
                    help="matrix mode (sends interface=matrix)")
    ap.add_argument("--no-audio-follows-video", action="store_true",
                    help="Standard routing with 'Audio follows video' off")
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--user", default="")
    ap.add_argument("--password", default=os.environ.get("LUMA_MEASURE_PASSWORD", ""))
    ap.add_argument("--confirm", action="store_true", help="really send the routes")
    args = ap.parse_args(argv)

    if not args.confirm:
        print("Nothing sent. This measures by sending a real route; add --confirm to run it.")
        return 2
    if not (args.displays or args.groups or args.walls):
        print("Name at least one of --displays, --groups, --walls.")
        return 2

    opener = build_opener()
    csrf = None
    if args.user:
        status, body, _ = call(opener, args.base, "/api/auth/login",
                               {"username": args.user, "password": args.password})
        if status != 200:
            print("Sign-in failed (HTTP %d)." % status)
            return 1
        csrf = body.get("csrf_token")

    runs = []
    try:
        for index in range(max(1, args.repeat)):
            source = args.alt_source if (args.alt_source and index % 2) else args.source
            request = {"source": source, "displays": listed(args.displays),
                       "groups": listed(args.groups), "walls": listed(args.walls)}
            if args.mode != "av":
                request.update(interface="matrix", mode=args.mode)
            elif args.no_audio_follows_video:
                request.update(interface="standard", audio_follows_video=False)
            else:
                request.update(interface="standard")
            status, body, took = call(opener, args.base, "/api/user/route_multi", request, csrf)
            timing = body.get("timing") or {}
            runs.append({"run": index + 1, "http": status, "ok": body.get("ok"),
                         "routed": body.get("routed"), "total": body.get("total"),
                         "request_ms": round(took * 1000), **{k: timing.get(k) for k in (
                             "workers", "first_start_ms", "last_start_ms", "first_end_ms",
                             "last_end_ms", "start_spread_ms")}})
            print(json.dumps(runs[-1]))
            time.sleep(1.0)
    finally:
        if args.user:
            call(opener, args.base, "/api/auth/logout", {}, csrf)

    spreads = [r["start_spread_ms"] for r in runs if isinstance(r.get("start_spread_ms"), (int, float))]
    if spreads:
        print("start spread ms: min %.0f  median %.0f  max %.0f over %d run(s)"
              % (min(spreads), statistics.median(spreads), max(spreads), len(spreads)))
    return 0 if all(r["http"] == 200 for r in runs) else 1


if __name__ == "__main__":
    sys.exit(main())
