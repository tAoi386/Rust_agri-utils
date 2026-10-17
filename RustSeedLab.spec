# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

a = Analysis(
    ['main.py'],
    pathex=['.deps'],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
# Qt needs Windows ICU's unversioned exports. Poppler's ICU uses incompatible
# versioned exports and can be picked from desktop tooling's PATH by accident.
a.binaries = [entry for entry in a.binaries if not (
    Path(entry[0]).name.lower().startswith('icu')
    and '/native/poppler/' in entry[1].replace('\\', '/').lower()
)]
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='RustSeedLab',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['resources/app.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='RustSeedLab',
)
