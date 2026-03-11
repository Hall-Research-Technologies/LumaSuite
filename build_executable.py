#!/usr/bin/env python3
"""
Build script to create standalone executables for Windows, macOS, and Linux
Run: python build_executable.py
"""

import os
import sys
import subprocess
import platform
import shutil
import stat
from pathlib import Path

PROJECT_DIR = Path(__file__).parent
DIST_DIR = PROJECT_DIR / 'dist'
BUILD_DIR = PROJECT_DIR / 'build'
LAUNCHER = PROJECT_DIR / 'app_launcher.py'


def _remove_path(path: Path):
    """Remove a file or directory, handling Windows read-only bits."""
    if not path.exists():
        return

    def _onerror(func, p, exc_info):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except Exception:
            pass

    if path.is_dir():
        shutil.rmtree(path, onerror=_onerror)
    else:
        os.chmod(path, stat.S_IWRITE)
        path.unlink()

def run_command(cmd):
    """Run a shell command"""
    print(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, shell=True if os.name == 'nt' else False)
    if result.returncode != 0:
        print(f"Error: Command failed with code {result.returncode}")
        sys.exit(1)

def install_pyinstaller():
    """Install PyInstaller if not available"""
    try:
        import PyInstaller
    except ImportError:
        print("Installing PyInstaller...")
        run_command([sys.executable, '-m', 'pip', 'install', 'pyinstaller'])


def install_build_dependencies():
    """Install pinned build/runtime dependencies required for packaging."""
    lock_file = PROJECT_DIR / 'requirements-build-lock.txt'
    req_file = PROJECT_DIR / 'requirements-build.txt'

    if lock_file.exists():
        selected = lock_file
    elif req_file.exists():
        selected = req_file
    else:
        print("Error: No requirements-build-lock.txt or requirements-build.txt found")
        sys.exit(1)

    print(f"Installing build dependencies from {selected.name}...")
    run_command([sys.executable, '-m', 'pip', 'install', '--upgrade', 'pip'])
    run_command([sys.executable, '-m', 'pip', 'install', '-r', str(selected)])

def build_windows():
    """Build Windows .exe"""
    print("\n" + "="*60)
    print("Building Windows .exe")
    print("="*60)
    
    add_data_sep = ';' if os.name == 'nt' else ':'
    ui_src = PROJECT_DIR / 'ui'
    ui_temp = PROJECT_DIR / 'ui_temp_build'
    dist_target = PROJECT_DIR / 'dist' / 'Windows'
    build_target = PROJECT_DIR / 'build' / 'Windows'
    hallway_logo = PROJECT_DIR / 'hallway.png'
    footer_logo = PROJECT_DIR / 'atlona.png'
    icon_arg = f"--icon={PROJECT_DIR / 'ui' / 'favicon.ico'}" if (PROJECT_DIR / 'ui' / 'favicon.ico').exists() else None

    if not ui_src.exists():
        print(f"Error: UI folder not found at {ui_src}")
        sys.exit(1)
    
    if not hallway_logo.exists():
        print(f"Warning: hallway.png not found at {hallway_logo}")
    if not footer_logo.exists():
        print(f"Warning: footer logo not found at {footer_logo}")
    
    # Create a clean temp ui folder without archives or unneeded logos.
    print("Creating temporary UI folder (excluding .zip files and logos/)...")
    _remove_path(ui_temp)
    _remove_path(dist_target / 'LumaSuite.exe')
    _remove_path(build_target)
    shutil.copytree(ui_src, ui_temp, ignore=shutil.ignore_patterns('*.zip', 'logos', 'logos/*'))

    pyinstaller_cmd = [
        sys.executable, '-m', 'PyInstaller',
        '--clean',
        '--onefile',
        '--windowed',
        '--name=LumaSuite',
        '--add-data', f"{ui_temp}{add_data_sep}ui",
        '--add-data', f"{hallway_logo}{add_data_sep}.",
        '--add-data', f"{footer_logo}{add_data_sep}.",
        '--distpath=dist/Windows',
        '--workpath=build/Windows',
        '--specpath=build/Windows',
        '--hidden-import=flask',
        '--hidden-import=werkzeug',
        '--hidden-import=flask_cors',
        '--hidden-import=requests',
        '--hidden-import=PIL',
        '--hidden-import=PIL.Image',
        '--hidden-import=PIL.ImageTk',
        '--collect-submodules=PIL',
        '--collect-data=PIL',
        '--hidden-import=pystray',
        '--hidden-import=pystray._win32',
        str(LAUNCHER)
    ]
    if icon_arg:
        pyinstaller_cmd.insert(6, icon_arg)
    
    # Remove empty strings
    pyinstaller_cmd = [c for c in pyinstaller_cmd if c]
    
    run_command(pyinstaller_cmd)
    
    # Clean up temp folder
    print("Cleaning up temporary UI folder...")
    _remove_path(ui_temp)
    
    print("[OK] Windows .exe built successfully")
    print(f"  Location: {PROJECT_DIR / 'dist' / 'Windows' / 'LumaSuite.exe'}")

def build_mac():
    """Build macOS .app for a specific architecture (arm64 or x86_64)."""
    if platform.system() != 'Darwin':
        print("macOS build must be run on macOS. Skipping.")
        return
    print("\n" + "="*60)
    target_arch = os.environ.get('PYI_MACOS_ARCH')
    if not target_arch:
        machine = platform.machine().lower()
        target_arch = 'arm64' if 'arm' in machine else 'x86_64'
    print(f"Building macOS .app ({target_arch})")
    print("="*60)
    
    add_data_sep = ':'
    ui_src = PROJECT_DIR / 'ui'
    ui_temp = PROJECT_DIR / 'ui_temp_build_macos'
    dist_target = PROJECT_DIR / 'dist' / 'macOS'
    build_target = PROJECT_DIR / 'build' / 'macOS'
    hallway_logo = PROJECT_DIR / 'hallway.png'
    footer_logo = PROJECT_DIR / 'atlona.png'
    
    # Look for macOS-specific icon (.icns format required for proper bundling)
    # PNG won't bundle correctly in .app; skip icon if .icns not found
    icon_arg = None
    icon_candidates = [
        PROJECT_DIR / 'ui' / 'LumaSuite.icns',
        PROJECT_DIR / 'ui' / 'favicon.icns',
        PROJECT_DIR / 'ui' / 'icon.icns'
    ]
    
    print("Looking for .icns icon files...")
    for icns_path in icon_candidates:
        print(f"  Checking: {icns_path} ... ", end='')
        if icns_path.exists():
            icon_arg = f"--icon={str(icns_path.resolve())}"
            print(f"FOUND")
            print(f"Using icon: {icon_arg}")
            break
        else:
            print("not found")
    
    if not icon_arg:
        print("WARNING: No .icns icon found in ui/ folder")
        print("  Expected one of: LumaSuite.icns, favicon.icns, icon.icns")
        print("  To add a custom icon:")
        print("    convert ui/companylogo.png -define icon:auto-resize=256,128,96,64,48,32,16 ui/favicon.icns")
        print("    Or use online converter: cloudconvert.com (PNG to ICNS)")

    if not ui_src.exists():
        print(f"Error: UI folder not found at {ui_src}")
        sys.exit(1)
    
    if not hallway_logo.exists():
        print(f"Warning: hallway.png not found at {hallway_logo}")
    if not footer_logo.exists():
        print(f"Warning: footer logo not found at {footer_logo}")

    _remove_path(build_target)
    _remove_path(dist_target / 'LumaSuite.app')
    _remove_path(ui_temp)
    shutil.copytree(ui_src, ui_temp, ignore=shutil.ignore_patterns('*.zip', 'logos', 'logos/*'))

    pyinstaller_cmd = [
        sys.executable, '-m', 'PyInstaller',
        '--clean',
        '--onedir',
        '--windowed',
        '--name=LumaSuite',
        '--osx-bundle-identifier=com.lumasuite.app',
        f'--target-architecture={target_arch}',
        '--add-data', f"{ui_temp}{add_data_sep}ui",
        '--add-data', f"{hallway_logo}{add_data_sep}.",
        '--add-data', f"{footer_logo}{add_data_sep}.",
        '--distpath=dist/macOS',
        '--workpath=build/macOS',
        '--specpath=build/macOS',
        '--hidden-import=flask',
        '--hidden-import=werkzeug',
        '--hidden-import=flask_cors',
        '--hidden-import=requests',
        '--hidden-import=PIL',
        '--hidden-import=PIL.Image',
        '--hidden-import=PIL.ImageTk',
        '--collect-submodules=PIL',
        '--collect-data=PIL',
        '--hidden-import=pystray',
        '--hidden-import=pystray._darwin',
        str(LAUNCHER)
    ]
    if icon_arg:
        pyinstaller_cmd.insert(6, icon_arg)
    
    run_command(pyinstaller_cmd)
    _remove_path(ui_temp)
    print(f"[OK] macOS .app built successfully ({target_arch})")
    print(f"  Location: {PROJECT_DIR / 'dist' / 'macOS' / 'LumaSuite.app'}")

def build_linux():
    """Build Linux binary"""
    if platform.system() != 'Linux':
        print("Linux build must be run on Linux. Skipping.")
        return
    print("\n" + "="*60)
    print("Building Linux binary")
    print("="*60)

    add_data_sep = ':'
    ui_src = PROJECT_DIR / 'ui'
    ui_temp = PROJECT_DIR / 'ui_temp_build_linux'
    dist_target = PROJECT_DIR / 'dist' / 'Linux'
    build_target = PROJECT_DIR / 'build' / 'Linux'
    hallway_logo = PROJECT_DIR / 'hallway.png'
    footer_logo = PROJECT_DIR / 'atlona.png'
    
    # Look for PNG icon for Linux
    icon_arg = None
    if (PROJECT_DIR / 'ui' / 'companylogo.png').exists():
        icon_arg = f"--icon={PROJECT_DIR / 'ui' / 'companylogo.png'}"

    if not ui_src.exists():
        print(f"Error: UI folder not found at {ui_src}")
        sys.exit(1)

    if not hallway_logo.exists():
        print(f"Warning: hallway.png not found at {hallway_logo}")
    if not footer_logo.exists():
        print(f"Warning: footer logo not found at {footer_logo}")

    _remove_path(build_target)
    _remove_path(dist_target / 'LumaSuite')
    _remove_path(ui_temp)
    shutil.copytree(ui_src, ui_temp, ignore=shutil.ignore_patterns('*.zip', 'logos', 'logos/*'))

    pyinstaller_cmd = [
        sys.executable, '-m', 'PyInstaller',
        '--clean',
        '--onefile',
        '--windowed',
        '--name=LumaSuite',
        '--add-data', f"{ui_temp}{add_data_sep}ui",
        '--add-data', f"{hallway_logo}{add_data_sep}.",
        '--add-data', f"{footer_logo}{add_data_sep}.",
        '--distpath=dist/Linux',
        '--workpath=build/Linux',
        '--specpath=build/Linux',
        '--hidden-import=flask',
        '--hidden-import=werkzeug',
        '--hidden-import=flask_cors',
        '--hidden-import=requests',
        '--hidden-import=PIL',
        '--hidden-import=PIL.Image',
        '--hidden-import=PIL.ImageTk',
        '--collect-submodules=PIL',
        '--collect-data=PIL',
        '--hidden-import=pystray',
        '--hidden-import=pystray._xlib',
        str(LAUNCHER)
    ]
    if icon_arg:
        pyinstaller_cmd.insert(6, icon_arg)

    run_command(pyinstaller_cmd)
    _remove_path(ui_temp)

    # Add a desktop launcher and icon for Linux desktops.
    linux_dist = PROJECT_DIR / 'dist' / 'Linux'
    icon_src = PROJECT_DIR / 'ui' / 'companylogo.png'
    icon_dst = linux_dist / 'LumaSuite.png'
    desktop_file = linux_dist / 'LumaSuite.desktop'

    if icon_src.exists():
        shutil.copy2(icon_src, icon_dst)

    desktop_content = """[Desktop Entry]
Type=Application
Name=LumaSuite
Comment=LumaSuite control launcher
Exec=./LumaSuite
Icon=LumaSuite
Terminal=false
Categories=Network;Utility;
"""
    desktop_file.write_text(desktop_content, encoding='utf-8')
    os.chmod(desktop_file, 0o755)

    run_script = linux_dist / 'run-lumasuite.sh'
    run_script.write_text("""#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
chmod +x "$SCRIPT_DIR/LumaSuite"
exec "$SCRIPT_DIR/LumaSuite"
""", encoding='utf-8')
    os.chmod(run_script, 0o755)

    install_script = linux_dist / 'install-lumasuite-desktop.sh'
    install_script.write_text("""#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="$SCRIPT_DIR/LumaSuite"
ICON="$SCRIPT_DIR/LumaSuite.png"
DESKTOP_FILE="$HOME/.local/share/applications/lumasuite.desktop"

mkdir -p "$HOME/.local/share/applications"
mkdir -p "$HOME/.local/share/icons/hicolor/256x256/apps"

chmod +x "$APP"
if [[ -f "$ICON" ]]; then
  cp "$ICON" "$HOME/.local/share/icons/hicolor/256x256/apps/lumasuite.png"
fi

cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=LumaSuite
Comment=LumaSuite control launcher
Exec=$APP
Icon=lumasuite
Terminal=false
Categories=Network;Utility;
EOF

chmod +x "$DESKTOP_FILE"
if command -v gio >/dev/null 2>&1; then
  gio set "$DESKTOP_FILE" metadata::trusted true || true
fi
update-desktop-database "$HOME/.local/share/applications" >/dev/null 2>&1 || true
gtk-update-icon-cache "$HOME/.local/share/icons/hicolor" >/dev/null 2>&1 || true

echo "Installed launcher: $DESKTOP_FILE"
echo "You can now open LumaSuite from Show Applications."
""", encoding='utf-8')
    os.chmod(install_script, 0o755)

    print("[OK] Linux binary built successfully")
    print(f"  Location: {PROJECT_DIR / 'dist' / 'Linux' / 'LumaSuite'}")

def main():
    print("LumaServer Standalone Build Script")
    print("="*60)
    
    # Check Python version
    if sys.version_info < (3, 8):
        print("Error: Python 3.8+ required")
        sys.exit(1)
    
    # Install dependencies
    print("\nChecking dependencies...")
    ui_src = PROJECT_DIR / 'ui'
    install_build_dependencies()
    
    if not ui_src.exists():
        print(f"Error: UI folder not found at {ui_src}")
        sys.exit(1)

    # Determine platform and build
    system = platform.system()
    
    if len(sys.argv) > 1:
        # Build specific platform
        target = sys.argv[1].lower()
        if target == 'windows':
            build_windows()
        elif target == 'mac' or target == 'macos':
            build_mac()
        elif target == 'linux':
            build_linux()
        else:
            print(f"Unknown platform: {target}")
            print("Supported: windows, mac, linux")
            sys.exit(1)
    else:
        # Build for current platform
        if system == 'Windows':
            build_windows()
        elif system == 'Darwin':
            build_mac()
        elif system == 'Linux':
            build_linux()
        else:
            print(f"Unsupported platform: {system}")
            print("Supported: Windows, macOS, Linux")
            sys.exit(1)
    
    print("\n" + "="*60)
    print("Build complete!")
    print("="*60)

if __name__ == '__main__':
    main()
