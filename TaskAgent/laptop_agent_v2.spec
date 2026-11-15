# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for laptop_agent_v2.py — Windows only.
# Build on Windows with:  build_exe.bat
# (PyInstaller cannot cross-compile a Windows EXE from Linux,
#  so this spec is built on the laptop itself, once.)
#
# Output: dist\LaptopAgentV2.exe  (single file, console window)
# Keep agent_config.json in the SAME folder as the EXE and
# double-click the EXE from that folder. Closing the window
# kills both transports instantly.

block_cipher = None

a = Analysis(
    ['laptop_agent_v2.py'],
    pathex=[],
    binaries=[],
    datas=[('agent_config.example.json', '.')],
    hiddenimports=[
        # websockets is imported lazily inside run_bridge(), so
        # PyInstaller cannot see it by static analysis — list it.
        'websockets',
        'websockets.asyncio.server',
        'websockets.asyncio.client',
        'websockets.exceptions',
        'websockets.legacy',
        'websockets.legacy.protocol',
        # mss / Pillow are imported lazily inside do_screenshot().
        # If they are not bundled, screenshots fail with an honest
        # error instead of crashing — but bundle them when present.
        'mss',
        'PIL',
        'PIL.Image',
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
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='LaptopAgentV2',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,  # keep the console: logs show here, Ctrl+C / close = kill-switch
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
