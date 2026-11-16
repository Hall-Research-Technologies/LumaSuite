#!/usr/bin/env python3
"""Run every automated test the way CI does, one process per suite, and total the results.

    python run_tests.py            all suites
    python run_tests.py -v         also print each suite's whole output

Each suite is its own process because `lumaserver` is imported once per process and holds
module-level state; suites that share one would share each other's. No suite can reach a
device or the network: `tests/_fence.py` blocks every non-loopback socket, and the data
folder is a pre-seeded scratch directory.
"""
import glob
import os
import re
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
VERBOSE = "-v" in sys.argv


def run(label, command):
    started = time.time()
    done = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    output = (done.stdout or "") + (done.stderr or "")
    tests = re.findall(r"^Ran (\d+) tests?", output, flags=re.M)
    scenarios = re.findall(r"(\d+) scenarios? passed", output)
    count = int(tests[-1]) if tests else (int(scenarios[-1]) if scenarios else 0)
    ok = done.returncode == 0
    print("%-4s %-40s %5d  %5.1fs" % ("ok" if ok else "FAIL", label, count, time.time() - started))
    if VERBOSE or not ok:
        print(output[-4000:])
    return ok, count


def main():
    python_suites = sorted(glob.glob(os.path.join(ROOT, "tests", "test_*.py")))
    node_suites = sorted(glob.glob(os.path.join(ROOT, "tests", "*.test.js")) +
                         glob.glob(os.path.join(ROOT, "tests", "*_test.js")))
    results = []
    for path in python_suites:
        results.append(run("python " + os.path.relpath(path, ROOT), [sys.executable, path]))
    for path in node_suites:
        results.append(run("node " + os.path.relpath(path, ROOT), ["node", path]))
    total = sum(count for _ok, count in results)
    failed = sum(1 for ok, _count in results if not ok)
    print("\n%d suites, %d tests/scenarios, %d suite(s) failed" % (len(results), total, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
