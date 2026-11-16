"""LUMA-MA-010 -- the REAL launcher, driven through its own buttons.

Each scenario runs tests/launcher_port_driver.py in a fresh interpreter with a scratch data
folder: it builds the actual app_launcher.AppWindow under a real (withdrawn) Tk root, with a
real LumaSuite server on a real loopback port, and presses btn.invoke() on the actual buttons.

If Tk cannot initialise (no display) every scenario is skipped, LOUDLY: a banner is printed
and the skip reason says so. A silent skip would read as a pass.
"""
import os
import socket
import subprocess
import sys
import tempfile
import unittest

import _support  # noqa: F401

HERE = os.path.dirname(os.path.abspath(__file__))
DRIVER = os.path.join(HERE, "launcher_port_driver.py")
_base = [27800 + (os.getpid() * 17) % 3000]


def port_block():
    """A fresh run of ports for one scenario process, never reused within this run."""
    _base[0] += 40
    if _base[0] > 60000:
        _base[0] = 27800
    return _base[0]


# A bare Tk window that cannot take its icon within this many seconds is a runner whose window
# system is not answering (proved on a hosted macOS Intel runner with no LumaSuite code at all:
# Tk().iconbitmap() never returned). Every scenario would then wait out its own timeout, so the
# condition is detected once, with the same call the launcher makes, and reported loudly.
_TK_PROBE = "; ".join([
    "import os, sys, tkinter",
    "r = tkinter.Tk()", "r.withdraw()", "r.update()",
    "ico = os.path.join(sys.argv[1], 'ui', 'favicon.ico')",
    "r.iconbitmap(ico) if os.path.exists(ico) else None",
    "r.update()", "r.destroy()",
])


def tk_responsive():
    """True when a bare Tk window can be created and updated; None when it cannot be tried."""
    try:
        done = subprocess.run([sys.executable, "-c", _TK_PROBE, os.path.dirname(HERE)],
                              capture_output=True, timeout=45)
    except subprocess.TimeoutExpired:
        return False
    return True if done.returncode == 0 else None


class LauncherScenarios(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if tk_responsive() is False:
            cls.tk_hung = True
        else:
            cls.tk_hung = False

    def setUp(self):
        if self.tk_hung:
            banner = "*** LAUNCHER TESTS SKIPPED: a bare Tk window did not answer within 45 s on this runner ***"
            print(banner, file=sys.stderr)
            self.skipTest(banner)

    def run_scenario(self, name, data=None, env=None, first=None):
        data = data or tempfile.mkdtemp(prefix="luma-launcher-")
        environment = dict(os.environ, **(env or {}))
        try:
            done = subprocess.run([sys.executable, DRIVER, name, str(first or port_block()), data],
                                  capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  timeout=180, env=environment, cwd=os.path.dirname(HERE))
        except subprocess.TimeoutExpired as hung:
            # Report what the hung scenario had printed instead of discarding it.
            partial = [(hung.stdout or b""), (hung.stderr or b"")]
            partial = "".join(p.decode("utf-8", "replace") if isinstance(p, bytes) else p for p in partial)
            self.fail("scenario %s did not finish in 180 s:\n%s" % (name, partial[-3000:]))
        output = (done.stdout or "") + (done.stderr or "")
        if done.returncode == 77:
            banner = "*** LAUNCHER TESTS SKIPPED: Tk could not initialise here (%s) ***" % output.strip()[-200:]
            print("\n" + "*" * len(banner) + "\n" + banner + "\n" + "*" * len(banner), file=sys.stderr)
            self.skipTest(banner)
        self.assertEqual(done.returncode, 0, "scenario %s failed:\n%s" % (name, output[-3000:]))
        self.assertIn("SCENARIO %s OK" % name, output)
        return data, output

    # 1 / 2 / 6 / 21: ordinary starts look and behave as they always did
    def test_normal_startup_is_unchanged_no_recovery_ui_and_the_browser_opens(self):
        self.run_scenario("normal")

    def test_2_automatic_drift_is_unchanged_through_the_launcher(self):
        self.run_scenario("drift")

    def test_6_fixed_port_startup_through_the_launcher(self):
        self.run_scenario("fixed_free")

    def test_a_session_port_override_is_used_and_never_saved(self):
        self.run_scenario("override")

    def test_a_corrupt_configuration_file_cannot_stop_the_launcher(self):
        self.run_scenario("corrupt_config")

    # 7 / 8 / 9
    def test_7_8_9_conflict_state_has_the_exact_texts_and_four_buttons_and_never_drifts(self):
        self.run_scenario("conflict_ui")

    # 10
    def test_10_retry_fails_while_occupied_and_succeeds_after_release_through_the_button(self):
        self.run_scenario("retry")

    # 11 / 12 / 13 / 14
    def test_11_12_13_14_alternate_port_runs_the_app_opens_the_browser_and_keeps_the_config(self):
        self.run_scenario("alternate")

    # 15
    def test_15_restart_after_an_alternate_session_retries_the_configured_port(self):
        data, _ = self.run_scenario("alternate")
        import json
        with open(os.path.join(data, "local_port.json"), encoding="utf-8") as handle:
            saved = json.load(handle)
        port = saved["configured_local_fixed_port"]
        # the original port is occupied by something else again: the conflict state, not a drift
        self.run_scenario("restart_retries_fixed", data=data, env={"MA010_PORT": str(port), "MA010_OCCUPY": "1"})
        # and once the port is free the same configuration starts on it
        self.run_scenario("restart_retries_fixed", data=data, env={"MA010_PORT": str(port), "MA010_OCCUPY": "0"})

    # 16 / 18
    def test_16_18_change_fixed_port_is_completed_entirely_from_the_launcher(self):
        self.run_scenario("change_ok")

    # 17
    def test_17_a_failed_change_keeps_the_saved_value_and_allows_another_attempt(self):
        self.run_scenario("change_failed")

    def test_cancel_in_the_change_dialog_changes_nothing(self):
        self.run_scenario("change_cancel")

    def test_exit_from_the_conflict_state_exits_without_touching_configuration(self):
        self.run_scenario("exit_unchanged")

    # 23
    def test_23_a_port_that_passes_the_preflight_but_is_taken_still_reaches_recovery(self):
        self.run_scenario("race")

    # Settings actions on the launcher's own controller
    def test_the_settings_endpoints_move_the_launchers_server_and_release_the_old_port(self):
        self.run_scenario("settings_move")

    # D1: the window follows the controller, whoever moved it
    def test_d1_settings_retry_moves_the_launcher_url_status_note_and_open_browser(self):
        self.run_scenario("d1_settings_retry_updates_the_window")

    def test_d1_settings_make_fixed_updates_the_launcher_note(self):
        self.run_scenario("d1_settings_make_fixed_updates_the_note")

    def test_d1_settings_change_updates_the_launcher(self):
        self.run_scenario("d1_settings_change_updates_the_window")

    def test_d1_exit_unregisters_the_observer_and_normal_start_is_unchanged(self):
        self.run_scenario("d1_observer_is_removed_on_exit_and_normal_start_is_unchanged")

    def test_n3_the_change_dialog_is_centred_over_the_launcher(self):
        self.run_scenario("change_dialog_is_centred_over_the_launcher")


if __name__ == "__main__":
    unittest.main()
