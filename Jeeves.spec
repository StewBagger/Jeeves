# -*- mode: python ; coding: utf-8 -*-
# JeevesBot PyInstaller spec file
# Run with: pyinstaller Jeeves.spec

import os

block_cipher = None

# Collect all .py files in the bot directory as data files for cog loading
cog_files = [
    ('auto_restart.py', '.'),
    ('chat_relay.py', '.'),
    ('jeeves_drops.py', '.'),
    ('jeeves_events.py', '.'),
    ('lua_bridge.py', '.'),
    ('mod_check_timer.py', '.'),
    ('player_tracker.py', '.'),
    ('rank_sync.py', '.'),
    ('server_update.py', '.'),
    ('config.env.example', '.'),
]

a = Analysis(
    ['Jeeves.py'],
    pathex=[],
    binaries=[],
    datas=cog_files,
    hiddenimports=[
        'discord',
        'discord.ext.commands',
        'discord.ext.tasks',
        'discord.app_commands',
        'rcon',
        'rcon.source',
        'httpx',
        'dotenv',
        'sqlite3',
        'auto_restart',
        'chat_relay',
        'jeeves_drops',
        'jeeves_events',
        'lua_bridge',
        'mod_check_timer',
        'player_tracker',
        'rank_sync',
        'server_update',
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
    icon=None,
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
