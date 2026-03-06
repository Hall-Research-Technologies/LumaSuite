#!/usr/bin/env python3
"""
Build script to create standalone executables for Windows and macOS
Run: python build_executable.py
"""

import os
import sys
import subprocess
import platform
import shutil
from pathlib import Path

PROJECT_DIR = Path(__file__).parent
DIST_DIR = PROJECT_DIR / 'dist'
BUILD_DIR = PROJECT_DIR / 'build'
LAUNCHER = PROJECT_DIR / 'app_launcher.py'

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

def build_windows():
    """Build Windows .exe"""
    print("\n" + "="*60)
    print("Building Windows .exe")
    print("="*60)
    
    add_data_sep = ';' if os.name == 'nt' else ':'
    ui_src = PROJECT_DIR / 'ui'
    ui_temp = PROJECT_DIR / 'ui_temp_build'
    hallway_logo = PROJECT_DIR / 'hallway.png'
    footer_logo = PROJECT_DIR / 'All-Three-Logos-02-scaled.png'
    icon_arg = f"--icon={PROJECT_DIR / 'ui' / 'favicon.ico'}" if (PROJECT_DIR / 'ui' / 'favicon.ico').exists() else None

    if not ui_src.exists():
        print(f"Error: UI folder not found at {ui_src}")
        sys.exit(1)
    
    if not hallway_logo.exists():
        print(f"Warning: hallway.png not found at {hallway_logo}")
    if not footer_logo.exists():
        print(f"Warning: footer logo not found at {footer_logo}")
    
    # Create a clean temp ui folder without zip files
    print("Creating temporary UI folder (excluding .zip files)...")
    if ui_temp.exists():
        shutil.rmtree(ui_temp, ignore_errors=True)
    shutil.copytree(ui_src, ui_temp, ignore=shutil.ignore_patterns('*.zip'))

    pyinstaller_cmd = [
        sys.executable, '-m', 'PyInstaller',
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
    if ui_temp.exists():
        shutil.rmtree(ui_temp, ignore_errors=True)
    
    print("✓ Windows .exe built successfully")
    print(f"  Location: {PROJECT_DIR / 'dist' / 'Windows' / 'LumaSuite.exe'}")

def build_mac():
    """Build macOS .app"""
    if platform.system() != 'Darwin':
        print("macOS build must be run on macOS. Skipping.")
        return
    print("\n" + "="*60)
    print("Building macOS .app")
    print("="*60)
    
    add_data_sep = ':'
    ui_src = PROJECT_DIR / 'ui'
    hallway_logo = PROJECT_DIR / 'hallway.png'
    icon_arg = f"--icon={PROJECT_DIR / 'ui' / 'favicon.ico'}" if (PROJECT_DIR / 'ui' / 'favicon.ico').exists() else None

    if not ui_src.exists():
        print(f"Error: UI folder not found at {ui_src}")
        sys.exit(1)
    
    if not hallway_logo.exists():
        print(f"Warning: hallway.png not found at {hallway_logo}")

    pyinstaller_cmd = [
        sys.executable, '-m', 'PyInstaller',
        '--onefile',
        '--windowed',
        '--name=LumaSuite',
        '--osx-bundle-identifier=com.lumasuite.app',
        '--add-data', f"{ui_src}{add_data_sep}ui",
        '--add-data', f"{hallway_logo}{add_data_sep}.",
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
        str(LAUNCHER)
    ]
    if icon_arg:
        pyinstaller_cmd.insert(6, icon_arg)
    
    run_command(pyinstaller_cmd)
    print("✓ macOS .app built successfully")
    print(f"  Location: {PROJECT_DIR / 'dist' / 'macOS' / 'LumaSuite.app'}")

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
    install_pyinstaller()
    
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
        else:
            print(f"Unknown platform: {target}")
            print("Supported: windows, mac")
            sys.exit(1)
    else:
        # Build for current platform
        if system == 'Windows':
            build_windows()
        elif system == 'Darwin':
            build_mac()
        else:
            print(f"Unsupported platform: {system}")
            print("Supported: Windows, macOS")
            sys.exit(1)
    
    print("\n" + "="*60)
    print("Build complete!")
    print("="*60)

if __name__ == '__main__':
    main()
