# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for PolymarketBot macOS app.

Build with:
    pyinstaller polybot.spec
"""

import sys
from pathlib import Path

block_cipher = None

ROOT = Path(SPECPATH)

# Read version from version.py
_version_globals = {}
exec(open(str(ROOT / 'version.py')).read(), _version_globals)
APP_VERSION = _version_globals['VERSION']

a = Analysis(
    ['app.py'],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        # Dashboard HTML/CSS/JS files
        (str(ROOT / 'dashboard'), 'dashboard'),
    ],
    hiddenimports=[
        'webview',
        'webview.platforms.cocoa',
        'flask',
        'flask.json',
        'jinja2',
        'markupsafe',
        'werkzeug',
        'werkzeug.serving',
        'werkzeug.debug',
        'requests',
        'websocket',
        'dotenv',
        'py_clob_client',
        'bot_state',
        'config',
        'models',
        'market_discovery',
        'market_data',
        'arbitrage',
        'trading',
        'trade_log',
        'notifications',
        'utils',
        'main',
        'dashboard_server',
        'app_config',
        'updater',
        'version',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter',
        'unittest',
        'test',
    ],
    noarchive=False,
    optimize=0,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='PolymarketBot',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,   # No terminal window
    target_arch=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='PolymarketBot',
)

app = BUNDLE(
    coll,
    name='PolymarketBot.app',
    icon=None,  # TODO: add icon.icns
    bundle_identifier='com.polybot.polymarketbot',
    info_plist={
        'CFBundleName': 'PolymarketBot',
        'CFBundleDisplayName': 'PolymarketBot',
        'CFBundleShortVersionString': APP_VERSION,
        'CFBundleVersion': APP_VERSION,
        'LSMinimumSystemVersion': '12.0',
        'NSHighResolutionCapable': True,
        'LSBackgroundOnly': False,
    },
)
