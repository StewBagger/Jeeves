# -*- mode: python ; coding: utf-8 -*-
"""
Jeeves.spec — PyInstaller build specification for JeevesBot.

Build with:   pyinstaller Jeeves.spec
Or use:       build.bat

Output:       dist/Jeeves/Jeeves.exe
"""

import os

block_cipher = None

# Cog modules loaded dynamically via bot.load_extension() — PyInstaller
# can't auto-detect these, so they must be listed as hidden imports.
hidden_imports = [
    'auto_restart',
    'mod_check_timer',
    'player_tracker',
    'rank_sync',
    'chat_relay',
    'jeeves_events',
    'jeeves_drops',
    'jeeves_modsorter',
    'jeeves_modmanager',
    'server_update',
    'server_status',
    'lua_bridge',
    'mod_sorter',
]

spec_dir = os.path.dirname(os.path.abspath(SPEC))

a = Analysis(
    [os.path.join(spec_dir, 'Jeeves.py')],
    pathex=[spec_dir],
    binaries=[],
    datas=[
        (os.path.join(spec_dir, 'config.env.example'), '.'),
    ],
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Jeeves',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='Jeeves',
)
