# -*- mode: python ; coding: utf-8 -*-
"""
Jeeves.spec — PyInstaller build specification for JeevesBot.

Build with:   pyinstaller Jeeves.spec
Or use:       build.bat

Output:       dist/Jeeves/Jeeves.exe
"""

import os

block_cipher = None

# Cog modules loaded dynamically via bot.load_extension() — PyInstaller cannot
# see an import that is a string at runtime, so every name in setup_hook()'s
# extension tuple must be repeated here. THIS LIST HAS TO TRACK THAT TUPLE: when
# `jeeves_events` was renamed to `horde_events` the spec was not updated, so
# every exe built after the rename silently shipped without horde night or the
# leaderboard — the bot started fine and two features simply did not exist.
# If you add, rename or remove a cog, change it in both places.
hidden_imports = [
    # --- cogs, in setup_hook() order ---
    'auto_restart',
    'mod_check_timer',
    'player_tracker',
    'rank_sync',
    'chat_relay',
    'horde_events',
    'jeeves_drops',
    'jeeves_modsorter',
    'jeeves_modmanager',
    'server_update',
    'server_status',
    'horde_leaderboard',
    # --- plain modules imported by the cogs ---
    'lua_bridge',
    'mod_sorter',
    'workshop_acf',
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
