#!/usr/bin/env python3
"""
Standalone application launcher for LumaSuite
Opens a window and auto-launches the web UI in the default browser
"""

import sys
import os
import threading
import time
import webbrowser
import socket
import queue
import traceback
from pathlib import Path


def _get_log_path() -> Path:
    if sys.platform == 'darwin':
        base = Path.home() / 'Library' / 'Logs' / 'LumaSuite'
    elif sys.platform == 'win32':
        base = Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'LumaSuite' / 'logs'
    else:
        base = Path.home() / '.lumasuite' / 'logs'
    base.mkdir(parents=True, exist_ok=True)
    return base / 'launcher.log'


def _get_data_dir() -> Path:
    if sys.platform == 'darwin':
        base = Path.home() / 'Library' / 'Application Support' / 'LumaSuite'
    elif sys.platform == 'win32':
        base = Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'LumaSuite'
    else:
        base = Path.home() / '.lumasuite'
    base.mkdir(parents=True, exist_ok=True)
    return base


LOG_PATH = _get_log_path()
DATA_DIR = _get_data_dir()


def log_message(message: str):
    timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
    line = f"[{timestamp}] {message}"
    print(line)
    try:
        with LOG_PATH.open('a', encoding='utf-8') as log_file:
            log_file.write(line + '\n')
    except Exception:
        pass


def _log_unhandled_exception(exc_type, exc_value, exc_traceback):
    details = ''.join(traceback.format_exception(exc_type, exc_value, exc_traceback))
    log_message(f"Unhandled exception:\n{details}")


sys.excepthook = _log_unhandled_exception

# Try to import GUI libraries
try:
    import tkinter as tk
    from tkinter import messagebox, ttk
    from PIL import Image, ImageTk
    import pystray
except ImportError as e:
    log_message(f"Error: Required GUI library not available: {e}")
    log_message("This requires tkinter, Pillow (PIL), and pystray")
    sys.exit(1)

# Application Version
__version__ = "1.1.0"

# Detect if we're running as a frozen executable
IS_FROZEN = getattr(sys, 'frozen', False)
if IS_FROZEN:
    # PyInstaller sets sys.frozen and sets sys._MEIPASS to the temp directory
    BASE_DIR = Path(sys._MEIPASS)
else:
    BASE_DIR = Path(__file__).parent


def resolve_asset(*relative_parts):
    """Resolve asset paths across frozen onefile/onedir and source runs."""
    rel = Path(*relative_parts)
    candidates = [
        BASE_DIR / rel,
        Path(__file__).parent / rel,
        Path.cwd() / rel,
    ]

    if IS_FROZEN:
        exe_dir = Path(sys.executable).resolve().parent
        candidates.extend([
            exe_dir / rel,
            exe_dir / 'ui' / rel.name,
            BASE_DIR / 'ui' / rel.name,
        ])

    for path in candidates:
        if path.exists():
            return path
    return None

# Add the app directory to path so we can import the server
sys.path.insert(0, str(BASE_DIR))

try:
    os.environ.setdefault("LUMA_DATA_DIR", str(DATA_DIR))
    from lumaserver import APP as app, start_remote_access, start_firmware_checks
    from lumaserver import DATA_DIR as SERVER_DATA_DIR
    import luma_remote
    import luma_portcontrol
except ImportError as e:
    log_message(f"Error importing lumaserver: {e}")
    sys.exit(1)


class AppWindow:
    NORMAL_INFO = "The server will run until you click Exit"
    RECOVERY_INFO = "The server is not running. Choose an action above."

    def __init__(self, root, host='127.0.0.1', port=None):
        self.root = root
        self.host = host
        # `port` is an explicit session override (`--port=N`); None means the configured
        # behaviour: automatic selection from the default port, or the fixed port.
        self.override_port = port
        self.port = port or luma_portcontrol.DEFAULT_PORT
        self.ui_queue = queue.Queue()
        self.change_dialog = None
        self.conflict_port = None
        self.closing = False
        self.ctl = luma_portcontrol.PortController(
            app, host, SERVER_DATA_DIR, retire_delay=1.0, exit_callback=self._exit_for_restart)
        luma_portcontrol.register(self.ctl)
        # The window shows the controller's state, whoever moved it (a button here, or Settings).
        self._synced = False               # not before the first start has been reported
        self.ctl.add_observer(self._on_controller_change)
        self.server_thread = None
        self.running = False
        self.logo_image = None
        self.logo_photo = None
        self.footer_logo_image = None
        self.footer_logo_photo = None
        self.window_icon_photo = None
        self.tray_icon = None
        self.window_width = 540
        self.window_height = 420
        
        # Set up window
        self.root.title(f"LumaSuite v{__version__}")
        self.root.geometry(f"{self.window_width}x{self.window_height}")
        self.root.resizable(False, False)
        
        # Set window icon to favicon
        favicon_path = resolve_asset("ui", "favicon.ico")
        if favicon_path and favicon_path.exists():
            try:
                self.root.iconbitmap(favicon_path)
            except Exception as e:
                log_message(f"Could not set window icon: {e}")

        # Linux/macOS often prefer PNG iconphoto over ICO iconbitmap.
        png_icon = resolve_asset("ui", "companylogo.png")
        if png_icon and png_icon.exists():
            try:
                icon_image = Image.open(png_icon)
                self.window_icon_photo = ImageTk.PhotoImage(icon_image)
                self.root.iconphoto(True, self.window_icon_photo)
            except Exception as e:
                log_message(f"Could not set PNG window icon: {e}")
        else:
            log_message("Could not find ui/companylogo.png for window icon")
        
        # Center window on screen
        self.root.update_idletasks()
        x = (self.root.winfo_screenwidth() // 2) - (self.window_width // 2)
        y = (self.root.winfo_screenheight() // 2) - (self.window_height // 2)
        self.root.geometry(f"+{x}+{y}")
        
        # Create UI
        self.create_widgets()
        
        # pystray is fragile on macOS app bundles, especially on Apple Silicon.
        # Skip it there and rely on the main window instead.
        if sys.platform != 'darwin':
            self.setup_tray_icon()
        else:
            log_message("Skipping tray icon setup on macOS")
        
        # Handle window close
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        
        # Results of background work reach the window through one queue, on its own thread
        self._pump()

        # Start server on app load
        self.start_server()
    
    def find_available_port(self, start_port):
        """Find an available port starting from start_port"""
        port = start_port
        while port < start_port + luma_portcontrol.DISCOVERY_SPAN:  # Try up to 100 ports
            if luma_portcontrol.probe_bind(self.host, port)[0]:
                return port
            port += 1
        # If all ports are taken, return the original
        return start_port
    
    def create_widgets(self):
        # Create canvas with black background
        self.canvas = tk.Canvas(self.root, width=self.window_width, height=self.window_height, highlightthickness=0, bg="#000000")
        self.canvas.pack(fill="both", expand=True)
        
        # Create main frame on black background
        main_frame = tk.Frame(self.canvas, bg="#000000", bd=0)
        main_frame.place(relx=0.5, rely=0.5, anchor="center", width=480, height=390)
        self.main_frame = main_frame
        
        # Load and display logo on top of black background
        logo_path = resolve_asset("hallway.png")
        log_message(f"Launcher header logo path: {logo_path}")
        if logo_path and logo_path.exists():
            try:
                self.logo_image = Image.open(logo_path)
                # Resize logo to fit nicely at top
                logo_width = 300
                logo_height = int(self.logo_image.height * (logo_width / self.logo_image.width))
                self.logo_image = self.logo_image.resize((logo_width, logo_height), Image.Resampling.LANCZOS)
                self.logo_photo = ImageTk.PhotoImage(self.logo_image)
                
                logo_label = tk.Label(main_frame, image=self.logo_photo, bg="#000000", bd=0)
                logo_label.pack(pady=(6, 6))
            except Exception as e:
                log_message(f"Could not load logo image: {e}")
        else:
            log_message("Could not find hallway.png for launcher header")
        
        # Title with better styling
        title = tk.Label(
            main_frame,
            text="LumaSuite",
            font=("Helvetica", 22, "bold"),
            fg="#ffffff",
            bg="#000000"
        )
        title.pack(pady=(2, 6))
        
        # Version label
        version_label = tk.Label(
            main_frame,
            text=f"v{__version__}",
            font=("Helvetica", 9),
            fg="#888888",
            bg="#000000"
        )
        version_label.pack(pady=(0, 4))
        
        # Status
        self.status_label = tk.Label(
            main_frame,
            text="Starting server...",
            font=("Helvetica", 12),
            fg="#4CAF50",
            bg="#000000"
        )
        self.status_label.pack(pady=6)
        
        # URL as clickable link
        self.url_text = tk.StringVar(value="")
        self.url_label = tk.Label(
            main_frame,
            textvariable=self.url_text,
            font=("Helvetica", 11, "underline"),
            fg="#64B5F6",
            bg="#000000",
            cursor="hand2"
        )
        self.url_label.pack(pady=3)
        self.url_label.bind("<Button-1>", lambda e: self.open_browser())
        self._url_look = (self.url_label.cget("font"), self.url_label.cget("fg"), self.url_label.cget("cursor"))

        # Only shown while running on a temporary port (fixed-port recovery)
        self.note_label = tk.Label(
            main_frame,
            text="",
            font=("Helvetica", 9),
            fg="#FFB74D",
            bg="#000000"
        )
        
        # Buttons frame
        btn_frame = tk.Frame(main_frame, bg="#000000")
        btn_frame.pack(pady=14)
        self.btn_frame = btn_frame
        
        if sys.platform == 'win32':
            self.open_btn = tk.Button(
                btn_frame,
                text="Open Browser",
                command=self.open_browser,
                width=14,
                bg="#2196F3",
                fg="white",
                font=("Helvetica", 10, "bold"),
                relief="flat",
                padx=10,
                pady=6,
                cursor="hand2",
                state="disabled"
            )
        else:
            self.open_btn = ttk.Button(
                btn_frame,
                text="Open Browser",
                command=self.open_browser,
                width=14,
                state="disabled"
            )
        self.open_btn.grid(row=0, column=0, padx=6)
        
        if sys.platform == 'win32':
            exit_btn = tk.Button(
                btn_frame,
                text="Exit",
                command=self.on_close,
                width=14,
                bg="#f44336",
                fg="white",
                font=("Helvetica", 10, "bold"),
                relief="flat",
                padx=10,
                pady=6,
                cursor="hand2"
            )
        else:
            exit_btn = ttk.Button(
                btn_frame,
                text="Exit",
                command=self.on_close,
                width=14
            )
        exit_btn.grid(row=0, column=1, padx=6)
        self.exit_btn = exit_btn

        self._build_conflict_frame(main_frame)
        
        # Info label at bottom
        info = self.info_label = tk.Label(
            main_frame,
            text=self.NORMAL_INFO,
            font=("Helvetica", 9),
            fg="#999999",
            bg="#000000"
        )
        info.pack(pady=(10, 4))

        footer_logo_candidates = [
            resolve_asset("atlona.png"),
            resolve_asset("ui", "companylogo.png"),
        ]
        footer_logo_path = next((p for p in footer_logo_candidates if p and p.exists()), None)
        log_message(f"Launcher footer logo path: {footer_logo_path}")
        if footer_logo_path:
            try:
                self.footer_logo_image = Image.open(footer_logo_path)
                footer_width = 190
                footer_height = int(self.footer_logo_image.height * (footer_width / self.footer_logo_image.width))
                self.footer_logo_image = self.footer_logo_image.resize((footer_width, footer_height), Image.Resampling.LANCZOS)
                self.footer_logo_photo = ImageTk.PhotoImage(self.footer_logo_image)

                footer_logo_label = tk.Label(main_frame, image=self.footer_logo_photo, bg="#000000", bd=0, cursor="hand2")
                footer_logo_label.pack(pady=(0, 6))
                footer_logo_label.bind("<Button-1>", lambda e: webbrowser.open("https://www.hallresearch.com"))
            except Exception as e:
                log_message(f"Could not load footer logo image: {e}")
    
    def setup_tray_icon(self):
        """Setup system tray icon with favicon"""
        favicon_path = BASE_DIR / "ui" / "favicon.ico"
        if not favicon_path.exists():
            # Try alternate location
            favicon_path = BASE_DIR / "favicon.ico"
        
        if favicon_path.exists():
            try:
                icon_image = Image.open(favicon_path)
                
                menu = pystray.Menu(
                    pystray.MenuItem("Open Browser", lambda: self.root.after(0, self.open_browser)),
                    pystray.MenuItem("Show Window", lambda: self.root.after(0, self.show_window)),
                    pystray.MenuItem("Exit", lambda: self.root.after(0, self.on_close))
                )
                
                self.tray_icon = pystray.Icon("LumaSuite", icon_image, "LumaSuite", menu)
                
                # On non-macOS platforms, running the tray loop in a daemon thread is sufficient.
                threading.Thread(target=self.tray_icon.run, daemon=True).start()
            except Exception as e:
                log_message(f"Could not create system tray icon: {e}")
    
    def show_window(self):
        """Show the main window"""
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
    
    def start_server(self):
        """Start the Flask server in a background thread"""
        self.server_thread = threading.Thread(target=self.run_server, daemon=True)
        self.server_thread.start()

    def run_server(self):
        """Start remote access and the checks once, then the local listener."""
        try:
            self.running = True
            # Suppress Flask logging for cleaner output
            import logging
            log = logging.getLogger('werkzeug')
            log.setLevel(logging.ERROR)

            # Remote access is off unless an administrator left it on; restoring it
            # never prevents the local application from starting.
            start_remote_access()
            # The launch check of Hall Research's published firmware, then once
            # every seven days. One background thread; nothing waits for it.
            start_firmware_checks()

            # The local listener is created through the tagging factory (inside the port
            # controller), so every request carries which listener accepted it: this one is the
            # local listener (loopback), and anything not bound to loopback is gated.
            log_message(f"Starting local server (override port: {self.override_port})")
            outcome = self.ctl.startup(self.override_port)
            self._post(self.after_start, outcome)
        except Exception as e:
            log_message(f"Server error: {e}")
            log_message(traceback.format_exc())
            self.running = False
            self._post(self.on_server_failed)

    # ---- work done off the window's thread reaches it through this queue ----------
    def _post(self, fn, *args):
        self.ui_queue.put((fn, args))

    def _pump(self):
        if self.closing:
            return
        try:
            while True:
                fn, args = self.ui_queue.get_nowait()
                try:
                    fn(*args)
                except Exception as e:
                    log_message(f"Launcher callback error: {e}")
                    log_message(traceback.format_exc())
        except queue.Empty:
            pass
        try:
            self.root.after(50, self._pump)
        except tk.TclError:
            pass

    def is_server_ready(self):
        """Check if server is responding"""
        try:
            sock = socket.create_connection((self.host, self.port), timeout=1)
            sock.close()
            return True
        except (socket.timeout, ConnectionRefusedError, OSError):
            return False

    def _on_controller_change(self, view=None):
        """Called by the controller on whichever thread changed state. Only queues: the window is
        touched on its own thread, and the state is read again there, so the last word wins."""
        if not self.closing:
            self._post(self._sync_from_controller)

    def _sync_from_controller(self):
        """Show what the controller says now (URL, status, temporary note, Open Browser target)."""
        if self.closing or not self._synced:
            return
        view = self.ctl.view()
        if view["running"] and view["port"]:
            self._render_running(view)

    def _render_running(self, view):
        """The one place the running state is drawn. Open Browser reads self.port, set here."""
        self.port = view["port"]
        self.running = True
        self._show_normal()
        self.status_label.config(text="✓ Server Running", fg="#4CAF50")
        self.url_text.set(f"http://{self.host}:{self.port}")
        self.open_btn.config(state="normal")
        if view["temporary"]:
            self.note_label.config(text=f"Temporary port (configured: {view['configured_port']})")
            self.note_label.pack(pady=(0, 2), before=self.btn_frame)
        else:
            self.note_label.pack_forget()

    def after_start(self, outcome):
        """The result of the first start, or of a recovery action."""
        self._synced = True
        if outcome.get("ok"):
            self.on_server_ready(f"http://{self.host}:{self.ctl.current_port}")
            return
        fixed_mode = self.ctl.mode == luma_portcontrol.MODE_FIXED and self.override_port is None
        if fixed_mode and outcome.get("reason") in ("in_use", "bind_failed", "not_serving"):
            self.show_conflict(outcome)
        else:
            self.running = False
            self.on_server_failed()

    def on_server_ready(self, url):
        """Called when server is ready"""
        log_message(f"Server ready at {url}")
        self._render_running(self.ctl.view())

        # Auto-open browser
        self.open_browser()

    def on_server_failed(self):
        """Called if server fails to start"""
        log_message("Server failed to start")
        self.status_label.config(text="✗ Server Failed to Start", fg="#f44336")
        self.open_btn.config(state="disabled")
        messagebox.showerror("Server Error", "Failed to start the server. Please check the logs.")

    # ---- fixed-port recovery -----------------------------------------------------
    def _build_conflict_frame(self, main_frame):
        frame = tk.Frame(main_frame, bg="#000000")
        self.conflict_frame = frame
        specs = [
            ("retry_btn", "Retry", self.retry_fixed, "#2196F3", 0, 0),
            ("alternate_btn", "Start on Alternate Port", self.start_alternate, "#546E7A", 0, 1),
            ("change_btn", "Change Fixed Port", self.open_change_dialog, "#546E7A", 1, 0),
            ("conflict_exit_btn", "Exit", self.exit_now, "#f44336", 1, 1),
        ]
        for attr, text, command, color, row, col in specs:
            if sys.platform == 'win32':
                btn = tk.Button(frame, text=text, command=command, width=21, bg=color, fg="white",
                                font=("Helvetica", 10, "bold"), relief="flat", padx=4, pady=3,
                                cursor="hand2")
            else:
                btn = ttk.Button(frame, text=text, command=command, width=21)
            btn.grid(row=row, column=col, padx=4, pady=3)
            setattr(self, attr, btn)

    def _conflict_buttons(self):
        return (self.retry_btn, self.alternate_btn, self.change_btn)

    def show_conflict(self, outcome):
        self.running = False
        port = outcome.get("port") or self.ctl.configured_port
        self.conflict_port = port
        self.info_label.config(text=self.RECOVERY_INFO)
        log_message(f"Local server could not start on the configured TCP port {port}")
        self.status_label.config(text="Server could not start", fg="#f44336")
        if outcome.get("reason") == "in_use" or not outcome.get("message"):
            detail = f"Configured TCP port {port} is already in use."
        else:
            detail = outcome["message"]
        self.url_text.set(detail)
        self.url_label.config(font=("Helvetica", 11), fg="#ffffff", cursor="")
        self.url_label.unbind("<Button-1>")
        self.open_btn.config(state="disabled")
        self.note_label.pack_forget()
        self.retry_btn.config(text=f"Retry {port}")
        for btn in self._conflict_buttons():
            btn.config(state="normal")
        self.btn_frame.pack_forget()
        self.conflict_frame.pack(pady=10, before=self.info_label)

    def _show_normal(self):
        """The ordinary state: the link look, and the Open Browser / Exit buttons."""
        if self.conflict_frame.winfo_manager():
            self.conflict_frame.pack_forget()
            self.btn_frame.pack(pady=14, before=self.info_label)
        self.info_label.config(text=self.NORMAL_INFO)
        font, fg, cursor = self._url_look
        self.url_label.config(font=font, fg=fg, cursor=cursor)
        self.url_label.bind("<Button-1>", lambda e: self.open_browser())

    def _run_recovery(self, fn, *args):
        for btn in self._conflict_buttons():
            btn.config(state="disabled")

        def work():
            try:
                outcome = fn(*args)
            except Exception as e:
                log_message(f"Recovery error: {e}")
                outcome = {"ok": False, "reason": "error", "message": "The action could not be completed."}
            self._post(self._after_recovery, outcome)
        threading.Thread(target=work, daemon=True).start()

    def _after_recovery(self, outcome):
        if outcome.get("ok"):
            self.after_start(outcome)
            return
        for btn in self._conflict_buttons():
            btn.config(state="normal")
        if outcome.get("reason") == "no_free_port":
            self.url_text.set(outcome.get("message") or "No free TCP port was found.")

    def retry_fixed(self):
        self._run_recovery(self.ctl.retry_fixed)

    def start_alternate(self):
        self._run_recovery(self.ctl.start_alternate)

    def open_change_dialog(self):
        if self.change_dialog is not None:
            return
        self.change_dialog = ChangePortDialog(self)

    def exit_now(self):
        """Leave from the recovery state: nothing is running and nothing is changed."""
        log_message("Exit chosen from the port recovery state")
        self.closing = True
        self.ctl.remove_observer(self._on_controller_change)
        self.ctl.stop()
        if self.tray_icon:
            self.tray_icon.stop()
        self.root.destroy()
        sys.exit(0)

    def _exit_for_restart(self):
        """The new instance has been started; this one leaves without a prompt."""
        def leave():
            self.closing = True
            self.ctl.remove_observer(self._on_controller_change)
            if self.tray_icon:
                self.tray_icon.stop()
            self.root.quit()
        self._post(leave)

    def open_browser(self):
        """Open the web UI in the default browser"""
        url = f"http://{self.host}:{self.port}"
        try:
            log_message(f"Opening browser at {url}")
            webbrowser.open(url)
        except Exception as e:
            log_message(f"Browser error: {e}")
            messagebox.showerror("Browser Error", f"Failed to open browser: {e}")

    def on_close(self):
        """Handle window close"""
        if messagebox.askokcancel("Exit LumaSuite", "Stop the server and exit the application?"):
            self.running = False
            self.closing = True
            self.ctl.remove_observer(self._on_controller_change)
            self.ctl.stop()
            if self.tray_icon:
                self.tray_icon.stop()
            self.root.destroy()
            sys.exit(0)


class ChangePortDialog:
    """Change fixed TCP port: recovery without the browser.

    Validate, try to start on the new port, and only after the server is up persist it.
    A port that is unavailable leaves the saved value exactly as it was.
    """

    def __init__(self, owner):
        self.owner = owner
        self.top = tk.Toplevel(owner.root)
        self.top.title("Change fixed TCP port")
        self.top.configure(bg="#000000")
        self.top.resizable(False, False)
        self.top.transient(owner.root)
        port = owner.conflict_port
        tk.Label(self.top, text="Change fixed TCP port", font=("Helvetica", 13, "bold"),
                 fg="#ffffff", bg="#000000").pack(padx=24, pady=(16, 4))
        self.message_label = tk.Label(self.top, text=f"Configured port {port} is unavailable.",
                                      font=("Helvetica", 10), fg="#cccccc", bg="#000000")
        self.message_label.pack(padx=24, pady=(0, 10))
        row = tk.Frame(self.top, bg="#000000")
        row.pack(padx=24)
        tk.Label(row, text="New TCP port:", font=("Helvetica", 10), fg="#ffffff",
                 bg="#000000").pack(side="left", padx=(0, 8))
        self.entry = tk.Entry(row, width=8, font=("Helvetica", 11))
        self.entry.pack(side="left")
        suggestion = owner.find_available_port((port or luma_portcontrol.DEFAULT_PORT) + 1)
        self.entry.insert(0, str(suggestion))          # advisory only: the bind decides
        self.error_label = tk.Label(self.top, text="", font=("Helvetica", 9), fg="#f44336",
                                    bg="#000000", wraplength=300)
        self.error_label.pack(padx=24, pady=(8, 0))
        buttons = tk.Frame(self.top, bg="#000000")
        buttons.pack(pady=(10, 16))
        self.cancel_btn = tk.Button(buttons, text="Cancel", width=10, command=self.cancel,
                                    bg="#546E7A", fg="white", relief="flat")
        self.cancel_btn.grid(row=0, column=0, padx=6)
        self.start_btn = tk.Button(buttons, text="Start", width=10, command=self.start,
                                   bg="#2196F3", fg="white", relief="flat")
        self.start_btn.grid(row=0, column=1, padx=6)
        self._centre_over(owner.root)
        self.top.protocol("WM_DELETE_WINDOW", self.cancel)
        self.top.bind("<Return>", lambda e: self.start())
        self.top.bind("<Escape>", lambda e: self.cancel())
        try:
            self.top.grab_set()
            self.entry.focus_set()
        except tk.TclError:
            pass

    def _centre_over(self, root):
        """Place the dialog in the middle of the launcher window, not at the screen's corner."""
        try:
            import re
            self.top.update_idletasks()
            m = re.match(r"(\d+)x(\d+)([+-]\d+)([+-]\d+)", root.geometry())
            if not m:
                return
            rw, rh, rx, ry = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
            w, h = self.top.winfo_reqwidth(), self.top.winfo_reqheight()
            x = max(0, rx + (rw - w) // 2)
            y = max(0, ry + (rh - h) // 2)
            self.top.geometry(f"+{x}+{y}")
        except Exception as e:
            log_message(f"Could not centre the dialog: {e}")

    def close(self):
        self.owner.change_dialog = None
        try:
            self.top.grab_release()
        except tk.TclError:
            pass
        self.top.destroy()

    def cancel(self):
        self.close()

    def start(self):
        text = self.entry.get()
        try:
            luma_portcontrol.validate_port(text)
        except luma_portcontrol.PortError as e:
            self.error_label.config(text=str(e))
            return
        self.error_label.config(text="")
        self.start_btn.config(state="disabled")
        self.cancel_btn.config(state="disabled")

        def work():
            try:
                outcome = self.owner.ctl.change_fixed(text)
            except Exception as e:
                log_message(f"Change fixed port error: {e}")
                outcome = {"ok": False, "reason": "error", "message": "The port could not be changed."}
            self.owner._post(self.finish, outcome)
        threading.Thread(target=work, daemon=True).start()

    def finish(self, outcome):
        if outcome.get("ok"):
            self.close()
            self.owner.after_start(outcome)
            return
        self.error_label.config(text=outcome.get("message") or "The port could not be changed.")
        self.start_btn.config(state="normal")
        self.cancel_btn.config(state="normal")


def main():
    # Determine host and port
    host = '127.0.0.1'
    port = None           # an explicit session override; None means the configured behaviour
    
    # Check for command-line arguments
    for arg in sys.argv[1:]:
        if arg.startswith('--host='):
            host = arg.split('=')[1]
        elif arg.startswith('--port='):
            try:
                port = luma_portcontrol.validate_session_port(arg.split('=', 1)[1])
            except luma_portcontrol.PortError as e:
                log_message(f"Ignoring {arg}: {e}")
    
    log_message(f"Launcher starting. Frozen={IS_FROZEN} BaseDir={BASE_DIR}")
    log_message(f"Log file: {LOG_PATH}")
    try:
        root = tk.Tk()
        app_window = AppWindow(root, host, port)
        root.mainloop()
    except Exception as e:
        log_message(f"Fatal launcher error: {e}")
        log_message(traceback.format_exc())
        raise


if __name__ == '__main__':
    main()
