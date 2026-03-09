# -*- mode: python ; coding: utf-8 -*-

block_cipher = None

a = Analysis(
    ['app_launcher.py'],
    pathex=['.'],
    binaries=[],
    datas=[
        ('ui', 'ui'),
        ('static', 'static'),
        ('hallway.png', '.'),
        ('atlona.png', '.'),
    ],
    hiddenimports=[
        'flask',
        'werkzeug',
        'flask_cors',
        'requests',
        'PIL',
        'PIL.Image',
        'PIL.ImageTk',
        'pystray'
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    cipher=block_cipher,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='LumaSuite',
    debug=False,
    strip=False,
    upx=True,
    console=False,
    icon='ui/favicon.ico'
)
