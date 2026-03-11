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


LOG_PATH = _get_log_path()


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
    from tkinter import messagebox
    from PIL import Image, ImageTk
    import pystray
except ImportError as e:
    log_message(f"Error: Required GUI library not available: {e}")
    log_message("This requires tkinter, Pillow (PIL), and pystray")
    sys.exit(1)

# Application Version
__version__ = "1.0.0"

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
    from lumaserver import APP as app
except ImportError as e:
    log_message(f"Error importing lumaserver: {e}")
    sys.exit(1)


class AppWindow:
    def __init__(self, root, host='127.0.0.1', port=8090):
        self.root = root
        self.host = host
        self.port = self.find_available_port(port)
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
        
        # Start server on app load
        self.start_server()
    
    def find_available_port(self, start_port):
        """Find an available port starting from start_port"""
        port = start_port
        while port < start_port + 100:  # Try up to 100 ports
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.bind(('', port))
                sock.close()
                return port
            except OSError:
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
        
        # Buttons frame
        btn_frame = tk.Frame(main_frame, bg="#000000")
        btn_frame.pack(pady=14)
        
        # Open Browser button with better styling
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
        self.open_btn.grid(row=0, column=0, padx=6)
        
        # Exit button with better styling
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
        exit_btn.grid(row=0, column=1, padx=6)
        
        # Info label at bottom
        info = tk.Label(
            main_frame,
            text="The server will run until you click Exit",
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
        
        # Wait for server to be ready, then open browser
        self.wait_for_server()
    
    def run_server(self):
        """Run the Flask app"""
        try:
            self.running = True
            log_message(f"Starting server on {self.host}:{self.port}")
            # Suppress Flask logging for cleaner output
            import logging
            log = logging.getLogger('werkzeug')
            log.setLevel(logging.ERROR)
            
            app.run(
                host=self.host,
                port=self.port,
                debug=False,
                use_reloader=False,
                threaded=True
            )
        except Exception as e:
            log_message(f"Server error: {e}")
            log_message(traceback.format_exc())
            self.running = False
    
    def is_server_ready(self):
        """Check if server is responding"""
        try:
            sock = socket.create_connection((self.host, self.port), timeout=1)
            sock.close()
            return True
        except (socket.timeout, ConnectionRefusedError, OSError):
            return False
    
    def wait_for_server(self):
        """Wait for server to be ready, then update UI and open browser"""
        def check():
            retries = 0
            max_retries = 30  # 30 seconds timeout
            
            while retries < max_retries:
                if self.is_server_ready():
                    # Server is ready
                    url = f"http://{self.host}:{self.port}"
                    self.root.after(0, lambda: self.on_server_ready(url))
                    return
                
                retries += 1
                time.sleep(1)
            
            # Timeout
            self.root.after(0, self.on_server_failed)
        
        thread = threading.Thread(target=check, daemon=True)
        thread.start()
    
    def on_server_ready(self, url):
        """Called when server is ready"""
        log_message(f"Server ready at {url}")
        self.status_label.config(text="✓ Server Running", fg="#4CAF50")
        self.url_text.set(url)
        self.open_btn.config(state="normal")
        
        # Auto-open browser
        self.open_browser()
    
    def on_server_failed(self):
        """Called if server fails to start"""
        log_message("Server failed to start before timeout")
        self.status_label.config(text="✗ Server Failed to Start", fg="#f44336")
        self.open_btn.config(state="disabled")
        messagebox.showerror("Server Error", "Failed to start the server. Please check the logs.")
    
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
            if self.tray_icon:
                self.tray_icon.stop()
            self.root.destroy()
            sys.exit(0)


def main():
    # Determine host and port
    host = '127.0.0.1'
    port = 8090
    
    # Check for command-line arguments
    for arg in sys.argv[1:]:
        if arg.startswith('--host='):
            host = arg.split('=')[1]
        elif arg.startswith('--port='):
            port = int(arg.split('=')[1])
    
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
