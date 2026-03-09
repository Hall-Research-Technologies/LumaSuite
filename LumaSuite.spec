# -*- mode: python ; coding: utf-8 -*-

block_cipher = None

a = Analysis(
    ['app_launcher.py'],
    pathex=['.'],
    binaries=[],
    datas=[
        ('ui', 'ui'),
        ('hallway.png', '.'),
        ('atlona.png', '.'),
    ],
    hiddenimports=[
        'flask',
        'flask_cors',
        'werkzeug',
        'requests',
        'requests.adapters',
        'urllib3',
        'certifi',
        'PIL',
        'PIL.Image',
        'PIL.ImageTk',
        'PIL._tkinter_finder',
        'pystray'
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(
    a.pure,
    a.zipped_data,
    cipher=block_cipher
)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='LumaSuite',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    icon='ui/favicon.ico'
)
