"""Drives the REAL launcher (app_launcher.AppWindow) under a real, withdrawn Tk root.

Run by tests/test_launcher_port.py, one scenario per process, so each starts from a clean
interpreter exactly as the packaged application does:

    python tests/launcher_port_driver.py <scenario> <first-port> <data-dir>

Exit status: 0 the scenario held, 1 an assertion failed, 77 Tk could not initialise (no display).

Everything is real except what must not happen on a test machine: the browser (a recording
stub), the system tray icon, the firmware check (it would reach the Internet) and the
native "exit?" box. Buttons are pressed with btn.invoke(), the way a person presses them.
Only loopback is used, on the ports it is given (>= 27000).
"""
import json
import os
import socket
import sys
import time
import traceback
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path[:0] = [HERE, ROOT]

SCENARIO, FIRST_PORT, DATA = sys.argv[1], int(sys.argv[2]), sys.argv[3]
os.makedirs(DATA, exist_ok=True)
# The launcher resolves its data and log folders from these; none of them is the operator's.
for var in ("LUMA_DATA_DIR", "LOCALAPPDATA", "HOME", "USERPROFILE"):
    os.environ[var] = DATA
os.environ["LUMA_DATA_DIR"] = DATA

import _fence  # noqa: E402

_fence.install()
_fence.isolate_data_dir()

import tkinter as tk  # noqa: E402

try:
    ROOT_WINDOW = tk.Tk()
except tk.TclError as exc:
    print("TK_UNAVAILABLE: %s" % exc)
    sys.exit(77)
ROOT_WINDOW.withdraw()

import app_launcher  # noqa: E402
import luma_portcontrol as pc  # noqa: E402

app_launcher.start_firmware_checks = lambda: None
app_launcher.AppWindow.setup_tray_icon = lambda self: None
OPENED = []
app_launcher.webbrowser.open = lambda url, *a, **k: OPENED.append(url) or True
ERRORS = []
app_launcher.messagebox.showerror = lambda *a, **k: ERRORS.append(a)
app_launcher.messagebox.askokcancel = lambda *a, **k: True

CONFIG = os.path.join(DATA, pc.FILE_NAME)
HOLDERS = []
_next = [FIRST_PORT]
WINDOWS = []


def new_port():
    while True:
        port = _next[0]
        _next[0] += 1
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue


def occupy(port=None):
    s = socket.socket()
    s.bind(("127.0.0.1", port or new_port()))
    s.listen(5)
    HOLDERS.append(s)
    return s


def release(sock):
    sock.close()
    HOLDERS.remove(sock)


def write_config(mode, port):
    with open(CONFIG, "w", encoding="utf-8") as h:
        json.dump({"local_port_mode": mode, "configured_local_fixed_port": port}, h)


def config_bytes():
    with open(CONFIG, "rb") as h:
        return h.read()


def pump(condition, what, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        ROOT_WINDOW.update()
        if condition():
            return
        time.sleep(0.02)
    raise AssertionError("timed out waiting for: %s" % what)


def settle(seconds=0.4):
    end = time.time() + seconds
    while time.time() < end:
        ROOT_WINDOW.update()
        time.sleep(0.02)


def launcher(override=None):
    win = app_launcher.AppWindow(ROOT_WINDOW, "127.0.0.1", override)
    WINDOWS.append(win)
    return win


def running(win):
    return lambda: "Server Running" in win.status_label.cget("text")


def conflict(win):
    return lambda: win.status_label.cget("text") == "Server could not start"


def shown(widget):
    return bool(widget.winfo_manager())


def version(port):
    with urllib.request.urlopen("http://127.0.0.1:%d/api/app_version" % port, timeout=5) as r:
        return json.loads(r.read().decode())


def fits(win, label):
    """Every control stays inside the launcher's fixed content frame in this state."""
    ROOT_WINDOW.deiconify()
    ROOT_WINDOW.update()
    height = win.main_frame.winfo_height()
    for child in win.main_frame.winfo_children():
        if child.winfo_manager() and child.winfo_ismapped():
            bottom = child.winfo_y() + child.winfo_height()
            assert bottom <= height, "%s: %s ends at %d, past the %d px frame" % (label, child, bottom, height)
    ROOT_WINDOW.withdraw()


# A ttk widget (the launcher uses ttk buttons off Windows) may hand back its state as a Tcl
# object that does not compare equal to the plain string, so state is compared as text.
def assert_conflict_ui(win, port):
    assert win.status_label.cget("text") == "Server could not start", win.status_label.cget("text")
    assert win.url_text.get() == "Configured TCP port %d is already in use." % port, win.url_text.get()
    assert shown(win.conflict_frame) and not shown(win.btn_frame)
    assert [b.cget("text") for b in (win.retry_btn, win.alternate_btn, win.change_btn, win.conflict_exit_btn)] == \
        ["Retry %d" % port, "Start on Alternate Port", "Change Fixed Port", "Exit"]
    assert str(win.open_btn.cget("state")) == "disabled"
    assert OPENED == [], "no browser is opened for a server that did not start: %r" % OPENED


# ---------------------------------------------------------------- scenarios
def normal():
    base = pc.DEFAULT_PORT = new_port()
    win = launcher()
    pump(running(win), "Server Running")
    url = "http://127.0.0.1:%d" % base
    assert ROOT_WINDOW.title() == "LumaSuite v%s" % app_launcher.__version__
    assert win.status_label.cget("text").endswith("Server Running")
    assert win.url_text.get() == url, win.url_text.get()
    assert str(win.open_btn.cget("state")) == "normal"
    assert shown(win.btn_frame) and not shown(win.conflict_frame) and not shown(win.note_label)
    assert OPENED == [url], "the browser opens by itself exactly once: %r" % OPENED
    assert version(base)["ok"]
    assert not os.path.exists(CONFIG), "a normal start writes no configuration"
    fits(win, "normal")
    win.open_btn.invoke()
    assert OPENED == [url, url]


def drift():
    base = pc.DEFAULT_PORT = new_port()
    occupy(base)
    win = launcher()
    pump(running(win), "Server Running on a neighbouring port")
    port = int(win.url_text.get().rsplit(":", 1)[1])
    assert port != base and port > base, port
    assert OPENED == ["http://127.0.0.1:%d" % port]
    assert version(port)["ok"]
    assert not shown(win.conflict_frame) and not shown(win.note_label), "automatic mode shows no recovery or temporary UI"
    assert not os.path.exists(CONFIG)


def fixed_free():
    port = new_port()
    write_config("fixed", port)
    win = launcher()
    pump(running(win), "Server Running on the fixed port")
    assert win.url_text.get() == "http://127.0.0.1:%d" % port
    assert win.ctl.url == "http://127.0.0.1:%d/" % port
    assert not shown(win.note_label)


def conflict_ui():
    holder = occupy()
    port = holder.getsockname()[1]
    write_config("fixed", port)
    before = config_bytes()
    win = launcher()
    pump(conflict(win), "the conflict state")
    assert_conflict_ui(win, port)
    assert ROOT_WINDOW.title() == "LumaSuite v%s" % app_launcher.__version__
    settle(0.5)
    assert win.status_label.cget("text") == "Server could not start", "the launcher stays open and unchanged"
    # no silent drift
    for neighbour in range(port + 1, port + 4):
        try:
            socket.create_connection(("127.0.0.1", neighbour), timeout=0.3).close()
            raise AssertionError("something serves on %d: the start drifted" % neighbour)
        except OSError:
            pass
    assert config_bytes() == before
    fits(win, "conflict")


def retry():
    holder = occupy()
    port = holder.getsockname()[1]
    write_config("fixed", port)
    before = config_bytes()
    win = launcher()
    pump(conflict(win), "the conflict state")
    for cycle in range(3):                                   # repeated conflict / retry cycles
        win.retry_btn.invoke()
        pump(lambda: str(win.retry_btn.cget("state")) == "normal", "retry to finish (cycle %d)" % cycle)
        assert_conflict_ui(win, port)
    release(holder)
    win.retry_btn.invoke()
    pump(running(win), "Server Running after the port was released")
    assert win.url_text.get() == "http://127.0.0.1:%d" % port
    assert OPENED == ["http://127.0.0.1:%d" % port]
    assert shown(win.btn_frame) and not shown(win.conflict_frame) and not shown(win.note_label)
    assert version(port)["ok"]
    assert config_bytes() == before


def alternate():
    holder = occupy()
    port = holder.getsockname()[1]
    write_config("fixed", port)
    before = config_bytes()
    win = launcher()
    pump(conflict(win), "the conflict state")
    win.alternate_btn.invoke()
    pump(running(win), "Server Running on the alternate port")
    shown_port = int(win.url_text.get().rsplit(":", 1)[1])
    assert shown_port != port
    assert win.url_text.get() == "http://127.0.0.1:%d" % win.ctl.current_port
    assert version(shown_port)["ok"], "the complete application answers on the alternate port"
    assert win.note_label.cget("text") == "Temporary port (configured: %d)" % port, win.note_label.cget("text")
    assert shown(win.note_label)
    del OPENED[:]
    win.open_btn.invoke()
    assert OPENED == ["http://127.0.0.1:%d" % shown_port], OPENED
    assert config_bytes() == before, "the configured fixed port is untouched"
    fits(win, "temporary")
    # the Settings page's view of the same state
    state = win.ctl.snapshot()
    assert state["configured_port"] == port and state["current_port"] == shown_port and state["temporary"]


def restart_retries_fixed():
    """A second launch after an alternate session: the configured port is tried again."""
    port = int(os.environ["MA010_PORT"])
    holder = occupy(port) if os.environ.get("MA010_OCCUPY") == "1" else None
    win = launcher()
    if holder is not None:
        pump(conflict(win), "the configured port is retried and is still busy")
        assert_conflict_ui(win, port)
    else:
        pump(running(win), "Server Running on the configured port")
        assert win.url_text.get() == "http://127.0.0.1:%d" % port


def change_ok():
    holder = occupy()
    port = holder.getsockname()[1]
    write_config("fixed", port)
    win = launcher()
    pump(conflict(win), "the conflict state")
    win.change_btn.invoke()
    dlg = win.change_dialog
    assert dlg is not None
    assert dlg.top.title() == "Change fixed TCP port"
    assert dlg.message_label.cget("text") == "Configured port %d is unavailable." % port
    assert dlg.cancel_btn.cget("text") == "Cancel" and dlg.start_btn.cget("text") == "Start"
    new = new_port()
    dlg.entry.delete(0, "end")
    dlg.entry.insert(0, str(new))
    dlg.start_btn.invoke()
    pump(running(win), "Server Running on the new port")
    assert win.change_dialog is None, "the dialog closes"
    assert win.url_text.get() == "http://127.0.0.1:%d" % new
    assert OPENED == ["http://127.0.0.1:%d" % new]
    saved = json.loads(config_bytes())
    assert saved == {"local_port_mode": "fixed", "configured_local_fixed_port": new}, saved
    assert version(new)["ok"]
    assert not shown(win.note_label), "the new port is the configured one, not a temporary one"


def change_failed():
    holder = occupy()
    port = holder.getsockname()[1]
    write_config("fixed", port)
    before = config_bytes()
    win = launcher()
    pump(conflict(win), "the conflict state")
    win.change_btn.invoke()
    dlg = win.change_dialog
    busy = occupy()
    busy_port = busy.getsockname()[1]
    dlg.entry.delete(0, "end")
    dlg.entry.insert(0, str(busy_port))
    dlg.start_btn.invoke()
    pump(lambda: dlg.error_label.cget("text"), "the unavailable message")
    assert dlg.error_label.cget("text") == "TCP port %d is unavailable." % busy_port
    assert win.change_dialog is dlg, "the dialog stays open for another attempt"
    assert config_bytes() == before, "the saved value is byte-identical"
    assert_conflict_ui(win, port)
    # invalid input is refused before anything is attempted
    for bad in ("", "abc", "80", "70000"):
        dlg.entry.delete(0, "end")
        dlg.entry.insert(0, bad)
        dlg.start_btn.invoke()
        assert dlg.error_label.cget("text"), bad
        assert config_bytes() == before
        assert win.change_dialog is dlg
    # and another attempt is allowed
    good = new_port()
    dlg.entry.delete(0, "end")
    dlg.entry.insert(0, str(good))
    dlg.start_btn.invoke()
    pump(running(win), "Server Running after the corrected attempt")
    assert json.loads(config_bytes())["configured_local_fixed_port"] == good
    # Cancel leaves everything alone


def change_cancel():
    holder = occupy()
    port = holder.getsockname()[1]
    write_config("fixed", port)
    before = config_bytes()
    win = launcher()
    pump(conflict(win), "the conflict state")
    win.change_btn.invoke()
    win.change_dialog.cancel_btn.invoke()
    assert win.change_dialog is None
    assert config_bytes() == before
    assert_conflict_ui(win, port)


def exit_unchanged():
    holder = occupy()
    port = holder.getsockname()[1]
    write_config("fixed", port)
    before = config_bytes()
    win = launcher()
    pump(conflict(win), "the conflict state")
    exits = []
    real_exit = sys.exit
    # sys.exit inside a Tk callback would surface as a TclError through invoke(); record it instead
    sys.exit = lambda code=0: exits.append(code)
    try:
        win.conflict_exit_btn.invoke()
    finally:
        sys.exit = real_exit
    assert exits == [0], exits
    assert win.closing and not win.ctl.running
    assert config_bytes() == before


def race():
    """The preflight says free; the port is taken; the real bind decides and recovery is offered."""
    holder = occupy()
    port = holder.getsockname()[1]
    write_config("fixed", port)
    real = app_launcher.luma_portcontrol.PortController.__init__

    def patched(self, *a, **k):
        real(self, *a, **k)
        self.probe = lambda host, p: (True, 0)
    app_launcher.luma_portcontrol.PortController.__init__ = patched
    try:
        win = launcher()
        pump(conflict(win), "the conflict state despite a passing preflight")
    finally:
        app_launcher.luma_portcontrol.PortController.__init__ = real
    assert_conflict_ui(win, port)
    release(holder)
    win.retry_btn.invoke()
    pump(running(win), "Server Running after release")


def corrupt_config():
    with open(CONFIG, "wb") as h:
        h.write(b"\x00{not json")
    base = pc.DEFAULT_PORT = new_port()
    win = launcher()
    pump(running(win), "Server Running despite an unusable configuration file")
    assert win.url_text.get() == "http://127.0.0.1:%d" % base
    assert os.path.exists(CONFIG + ".corrupt") and not os.path.exists(CONFIG)


def override():
    port = new_port()
    write_config("fixed", new_port())
    before = config_bytes()
    win = launcher(port)
    pump(running(win), "Server Running on the session override")
    assert win.url_text.get() == "http://127.0.0.1:%d" % port
    assert config_bytes() == before


def settings_move():
    """The browser Settings actions run on the controller the launcher registered."""
    holder = occupy()
    port = holder.getsockname()[1]
    write_config("fixed", port)
    win = launcher()
    pump(conflict(win), "the conflict state")
    win.alternate_btn.invoke()
    pump(running(win), "Server Running on the alternate port")
    temp = win.ctl.current_port
    import lumaserver
    client = lumaserver.APP.test_client()
    state = client.get("/api/local_port").get_json()
    assert state["configured_port"] == port and state["current_port"] == temp and state["temporary"], state
    release(holder)
    r = client.post("/api/local_port/retry", json={})
    assert r.status_code == 200 and r.get_json()["url"] == "http://127.0.0.1:%d/" % port, r.get_json()
    win.ctl.join_background()
    assert version(port)["ok"]
    try:
        socket.create_connection(("127.0.0.1", temp), timeout=0.5).close()
        raise AssertionError("the temporary port was not released")
    except OSError:
        pass


def _d1_recovery_to_alternate():
    holder = occupy()
    port = holder.getsockname()[1]
    write_config("fixed", port)
    win = launcher()
    pump(conflict(win), "the conflict state")
    assert win.info_label.cget("text") == app_launcher.AppWindow.RECOVERY_INFO, win.info_label.cget("text")
    assert "run until you click Exit" not in win.info_label.cget("text")
    win.alternate_btn.invoke()
    pump(running(win), "Server Running on the alternate port")
    assert win.info_label.cget("text") == "The server will run until you click Exit"
    return win, holder, port


def d1_settings_retry_updates_the_window():
    """The exact smoke sequence: alternate -> release -> Settings Retry; the WINDOW must follow."""
    win, holder, port = _d1_recovery_to_alternate()
    temp = win.ctl.current_port
    assert win.url_text.get() == "http://127.0.0.1:%d" % temp
    release(holder)
    import lumaserver
    client = lumaserver.APP.test_client()
    r = client.post("/api/local_port/retry", json={})
    assert r.status_code == 200, r.get_json()
    want = "http://127.0.0.1:%d" % port
    pump(lambda: win.url_text.get() == want, "the launcher URL label to follow Settings")
    assert win.status_label.cget("text") == "\u2713 Server Running", win.status_label.cget("text")
    assert not shown(win.note_label), "no temporary note once on the configured port"
    del OPENED[:]
    win.open_btn.invoke()
    assert OPENED == [want], OPENED
    win.ctl.join_background()
    assert win.ctl.view()["port"] == port


def d1_settings_make_fixed_updates_the_note():
    win, holder, port = _d1_recovery_to_alternate()
    temp = win.ctl.current_port
    assert win.note_label.cget("text") == "Temporary port (configured: %d)" % port
    import lumaserver
    r = lumaserver.APP.test_client().post("/api/local_port/make_fixed", json={})
    assert r.status_code == 200, r.get_json()
    pump(lambda: not shown(win.note_label), "the temporary note to go away once the port is the fixed one")
    assert win.url_text.get() == "http://127.0.0.1:%d" % temp
    del OPENED[:]
    win.open_btn.invoke()
    assert OPENED == ["http://127.0.0.1:%d" % temp], OPENED


def d1_settings_change_updates_the_window():
    win, holder, port = _d1_recovery_to_alternate()
    new = new_port()
    import lumaserver
    r = lumaserver.APP.test_client().post("/api/local_port/change", json={"port": new})
    assert r.status_code == 200, r.get_json()
    pump(lambda: win.url_text.get() == "http://127.0.0.1:%d" % new, "the launcher to follow Change Fixed Port")
    assert not shown(win.note_label)
    win.ctl.join_background()


def d1_observer_is_removed_on_exit_and_normal_start_is_unchanged():
    win = launcher()
    pump(running(win), "normal start")
    assert not shown(win.note_label) and win.info_label.cget("text") == "The server will run until you click Exit"
    assert win._on_controller_change in win.ctl._observers
    try:
        win.exit_now()
    except SystemExit:
        pass
    assert win._on_controller_change not in win.ctl._observers


def change_dialog_is_centred_over_the_launcher():
    holder = occupy()
    write_config("fixed", holder.getsockname()[1])
    win = launcher()
    ROOT_WINDOW.geometry("540x420+400+300")
    ROOT_WINDOW.update()
    pump(conflict(win), "the conflict state")
    win.change_btn.invoke()
    settle(0.3)
    top = win.change_dialog.top
    import re
    x, y = [int(v) for v in re.search(r"\+(-?\d+)\+(-?\d+)$", top.geometry()).groups()]
    w, h = top.winfo_reqwidth(), top.winfo_reqheight()
    assert (x, y) == (400 + (540 - w) // 2, 300 + (420 - h) // 2), (x, y, w, h, top.geometry())
    win.change_dialog.cancel()


def main():
    fn = globals()[SCENARIO]
    code = 0
    try:
        fn()
        print("SCENARIO %s OK" % SCENARIO)
    except SystemExit:
        raise
    except BaseException:
        traceback.print_exc()
        print("SCENARIO %s FAILED" % SCENARIO)
        code = 1
    finally:
        for win in WINDOWS:
            try:
                win.closing = True
                win.ctl.stop()
                win.ctl.join_background(5)
            except Exception:
                pass
        for s in list(HOLDERS):
            s.close()
        sys.stdout.flush()
    os._exit(code)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        # exit_unchanged's Exit button raised SystemExit through invoke() and was asserted;
        # reaching here means it escaped from another scenario
        raise
