# Building Standalone Executables for LumaSuite

This guide explains how to build portable executables for LumaSuite on Windows, macOS, and Linux.

## Requirements

- Python 3.8+
- pip (Python package manager)
- Git (optional, for cloning the repository)

For reproducible builds (recommended for release artifacts), use:

- `requirements-build-lock.txt` (fully pinned build dependencies)

## Installation & Build

### Windows (.exe)

1. **Install dependencies:**
   ```bash
   pip install -r requirements-build-lock.txt
   ```

2. **Build the Windows executable:**
   ```bash
   python build_executable.py windows
   ```

   Or to build for the current platform:
   ```bash
   python build_executable.py
   ```

3. **Output:**
   ```
   dist/Windows/LumaSuite.exe
   ```

4. **Run the executable:**
   - Double-click `LumaSuite.exe`
   - A window will open
   - The default browser will auto-launch with `http://127.0.0.1:8090` (or next available port)
   - Click "Open Browser" to manually open the UI
   - Click "Exit" to close the server and app

### macOS (.app)

**Important:** Builds include universal binary support (both Intel x86_64 and Apple Silicon ARM64).

1. **Install dependencies:**
   ```bash
   pip install -r requirements-build-lock.txt
   ```

2. **Build the macOS app (universal):**
   ```bash
   python build_executable.py macos
   ```

3. **Output:**
   ```
   dist/macOS/LumaSuite.app
   ```

4. **Run the app:**
   - Double-click `LumaSuite.app` or drag to Applications folder
   - A window will open
   - The default browser will auto-launch with `http://127.0.0.1:8090` (or next available port)
   - Click "Open Browser" to manually open the UI
   - Click "Exit" to close the server and app

5. **Architecture Support:**
   - The built `.app` runs on both Intel (x86_64) and Apple Silicon (ARM64) Macs
   - First run may trigger Gatekeeper check; use Cmd+Space → type app name → Cmd+Enter to open

6. **Custom Icon (Optional):**
   - By default, the app uses the macOS generic application icon
   - To use a custom icon, create `ui/LumaSuite.icns` (ICNS format only, PNG won't work)
   - Convert from PNG: `convert ui/companylogo.png -define icon:auto-resize=256,128,96,64,48,32,16 ui/LumaSuite.icns`
   - Or use an online converter: [cloudconvert.com](https://cloudconvert.com) (PNG to ICNS)
   - Rebuild after adding the .icns file

### Linux (binary)

1. **Install dependencies:**
   ```bash
   pip install -r requirements-build-lock.txt
   ```

2. **Build the Linux binary:**
   ```bash
   python build_executable.py linux
   ```

3. **Output:**
   ```
   dist/Linux/LumaSuite
   ```

4. **Make executable (if needed):**
   ```bash
   chmod +x dist/Linux/LumaSuite
   ```

5. **Run the binary:**
   ```bash
   ./dist/Linux/LumaSuite
   ```
   
   Or with GUI window (default):
   ```bash
   ./dist/Linux/LumaSuite
   ```

6. **Requirements:**
   - X11/Wayland display server
   - Python Tkinter (usually included in Linux distributions)
   - For headless systems, redirect output and use SSH X11 forwarding if needed

## Features

- **Standalone:** No Python installation required on end-user machines
- **Portable:** Single executable file (Windows) or app bundle (macOS)
- **Auto-Launch Browser:** Automatically opens the web UI in your default browser
- **GUI Window:** Minimalist control window with status and manual browser open button
- **Clean Exit:** Gracefully shuts down server when window is closed

## Command-Line Arguments

You can pass arguments to control host/port:

```bash
# Windows
LumaSuite.exe --host=0.0.0.0 --port=8080

# macOS
LumaSuite.app/Contents/MacOS/LumaSuite --host=0.0.0.0 --port=8080

# Linux
./LumaSuite --host=0.0.0.0 --port=8080
```

## Troubleshooting

### Windows
- **"Windows protected your PC"** - This is a SmartScreen warning. Click "More info" → "Run anyway"
- **Port already in use** - Use `--port=8080` to specify a different port

### macOS
- **"LumaServer cannot be opened"** - Right-click → Open (or lower security settings temporarily)
- **"Developer cannot be verified"** - System Preferences → Security & Privacy → Open Anyway
- **"bad CPU type in executable"** - Ensure you're running the correct architecture build (universal binary works on both Intel and Apple Silicon)
- **Terminal window appears** - This is the control window (intentional); the browser should auto-launch. If not, use "Open Browser" button
- **Generic application icon** - Add a custom icon by creating `ui/LumaSuite.icns`:
  - From command line: `convert ui/companylogo.png -define icon:auto-resize=256,128,96,64,48,32,16 ui/LumaSuite.icns`
  - Or use online tool: [cloudconvert.com](https://cloudconvert.com) (PNG to ICNS)
  - Rebuild after creating the .icns file

### Linux
- **No window appears / "bad display"** - Ensure X11 or Wayland is running. Use `DISPLAY=:0 ./LumaSuite` if running from SSH
- **"ImportError" for pystray or Tkinter** - Install: `sudo apt-get install python3-tk python3-pystray libx11-dev libxkbcommon-x11-0`
- **Terminal window appears instead of GUI** - Ensure you're running the correct build (use `python build_executable.py linux`)

## Distribution

To distribute:

1. **Windows:** Zip and distribute `dist/Windows/LumaSuite.exe`
2. **macOS:** Create a DMG installer or distribute `dist/macOS/LumaSuite.app`
3. **Linux:** Tar/zip and distribute `dist/Linux/LumaSuite`

## Building from Cross-Platform

To build for a different platform, you need to run the build script on that platform. For example:
- To build Windows .exe: Run the script on a Windows machine
- To build macOS .app: Run the script on a Mac

Alternatively, you can use CI/CD (GitHub Actions, etc.) to automate cross-platform builds.

## GitHub Actions Cross-Platform Build

This repository includes `.github/workflows/build-cross-platform.yml`.

- Trigger manually from the Actions tab (`workflow_dispatch`), or
- Push a tag like `v1.0.0` to trigger a release-style build.

Artifacts produced:

- `LumaSuite-Windows` -> `dist/Windows/LumaSuite.exe`
- `LumaSuite-macOS` -> `dist/macOS/*`
- `LumaSuite-Linux` -> `dist/Linux/*`
