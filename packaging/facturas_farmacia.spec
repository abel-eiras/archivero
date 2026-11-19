# -*- mode: python ; coding: utf-8 -*-
"""Especificación PyInstaller para generar el ejecutable de Archivero y el actualizador."""

block_cipher = None

a_main = Analysis(
    ['../app/main.py'],
    pathex=['..'],
    binaries=[],
    datas=[
        ('../app/templates', 'app/templates'),
        ('../app/static', 'app/static'),
        ('../docs', 'docs'),
    ],
    hiddenimports=[
        'uvicorn.logging',
        'uvicorn.loops',
        'uvicorn.loops.auto',
        'uvicorn.protocols',
        'uvicorn.protocols.http',
        'uvicorn.protocols.http.auto',
        'uvicorn.lifespan',
        'uvicorn.lifespan.on',
        'keyring.backends',
        'keyring.backends.Windows',
        'keyring.backends.macOS',
        'keyring.backends.SecretService',
        'scripts',
        'scripts.generate_sample_data',
        'multipart',
        'python_multipart',
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

pyz_main = PYZ(a_main.pure, a_main.zipped_data, cipher=block_cipher)

exe_main = EXE(
    pyz_main,
    a_main.scripts,
    a_main.binaries,
    a_main.zipfiles,
    a_main.datas,
    [],
    name='Archivero',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

a_updater = Analysis(
    ['../updater.py'],
    pathex=['..'],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    cipher=block_cipher,
)

pyz_updater = PYZ(a_updater.pure, a_updater.zipped_data, cipher=block_cipher)

exe_updater = EXE(
    pyz_updater,
    a_updater.scripts,
    a_updater.binaries,
    a_updater.zipfiles,
    a_updater.datas,
    [],
    name='ArchiveroUpdater',
    debug=False,
    strip=False,
    upx=True,
    console=True,
)
