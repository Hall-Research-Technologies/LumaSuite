"""The dependency declarations stay honest.

`flask-cors` was declared and hidden-imported but never imported; it has been removed, and
nothing may bring it back. The lock must pin the whole graph (every line an exact pin), and
the direct list must be a subset of it at the same versions.
"""
import ast
import os
import re
import unittest

import _support

ROOT = _support.ROOT
SKIP_DIRS = {".git", ".venv", "build", "dist", "node_modules", "__pycache__", "tests", "ToDelete"}


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as handle:
        return handle.read()


def pins(name):
    """name -> {version: marker or ""}. A package may carry one pin per platform marker."""
    found = {}
    for line in read(name).splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            match = re.match(r"^([A-Za-z0-9_.-]+)==([^;\s]+)\s*(;.*)?$", line)
            assert match, "%s: %r is not an exact pin" % (name, line)
            marker = (match.group(3) or "").lstrip("; ").strip()
            found.setdefault(re.sub(r"[-_.]+", "-", match.group(1)).lower(), {})[match.group(2)] = marker
    return found


def numeric(version):
    return tuple(int(part) for part in version.split("."))


class DependencyHygieneTests(unittest.TestCase):
    def test_flask_cors_is_not_declared_or_hidden_imported(self):
        for name in ("requirements-build.txt", "requirements-build-lock.txt", "build_executable.py", "LumaSuite.spec"):
            self.assertNotRegex(read(name).lower(), r"flask[-_]cors", name)

    def test_no_shipped_module_imports_flask_cors(self):
        offenders = []
        for folder, dirs, files in os.walk(ROOT):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for name in files:
                if name.endswith(".py"):
                    path = os.path.join(folder, name)
                    tree = ast.parse(open(path, encoding="utf-8").read(), path)
                    for node in ast.walk(tree):
                        modules = []
                        if isinstance(node, ast.Import):
                            modules = [alias.name for alias in node.names]
                        elif isinstance(node, ast.ImportFrom):
                            modules = [node.module or ""]
                        if any(m.split(".")[0] == "flask_cors" for m in modules):
                            offenders.append(os.path.relpath(path, ROOT))
        self.assertEqual(offenders, [])

    def test_the_lock_pins_every_direct_dependency_at_the_same_version(self):
        direct, lock = pins("requirements-build.txt"), pins("requirements-build-lock.txt")
        for name, versions in direct.items():
            self.assertEqual(lock.get(name), versions, name)

    def test_the_web_stack_is_not_below_the_fixed_releases(self):
        """Werkzeug 3.1.9 and Flask 3.1.3 close CVE-2024-49767 (multipart parsing reachable
        before sign-in on the Remote Access listener) and the Werkzeug advisories before it;
        Werkzeug 2.3 has no backport. requests 2.34 and cryptography 50 are supported lines."""
        lock = pins("requirements-build-lock.txt")
        for name, floor in (("flask", "3.1.3"), ("werkzeug", "3.1.9"), ("requests", "2.34.2")):
            self.assertEqual(len(lock[name]), 1, name)
            self.assertGreaterEqual(numeric(next(iter(lock[name]))), numeric(floor), name)

    def test_cryptography_is_50_on_every_supported_platform(self):
        for name in ("requirements-build.txt", "requirements-build-lock.txt"):
            self.assertEqual(pins(name)["cryptography"],
                             {"50.0.2": ""}, name)

    def test_the_lock_carries_the_transitive_graph(self):
        lock = pins("requirements-build-lock.txt")
        for name in ("blinker", "certifi", "cffi", "charset-normalizer", "click", "idna", "itsdangerous",
                     "jinja2", "markupsafe", "pycparser", "six", "urllib3", "altgraph", "packaging",
                     "pefile", "pywin32-ctypes", "pyinstaller-hooks-contrib"):
            self.assertIn(name, lock, name)

    def test_windows_only_packages_carry_a_platform_marker(self):
        text = read("requirements-build-lock.txt")
        for name in ("pefile", "pywin32-ctypes"):
            self.assertRegex(text, r"(?m)^%s==[^;\n]+; sys_platform == \"win32\"$" % name)


if __name__ == "__main__":
    unittest.main()
