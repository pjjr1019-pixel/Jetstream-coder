# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for laptop_launcher.py — Windows only.
# Build on Windows with:  build_launcher.bat
# (PyInstaller cannot cross-compile a Windows EXE from Linux,
#  so this spec is built on the laptop itself, once.)
#
# Output: dist\LaptopLauncher.exe  (single file, WINDOWED — no
# console of its own; the agent it starts runs as a captured
# subprocess and its output goes to the launcher's Log tab.)
#
# The launcher is NOT a bundle of the agent: laptop_agent_v2.py
# (or LaptopAgentV2.exe) stays a separate file beside it, and the
# launcher starts it with an absolute --config path. Keep all
# files in ONE folder.

block_cipher = None

a = Analysis(
    ['laptop_launcher.py'],
    pathex=[],
    binaries=[],
    datas=[('agent_config.example.json', '.')],
    hiddenimports=[],
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
    name='LaptopLauncher',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # windowed: no console — errors go to the Log tab
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
