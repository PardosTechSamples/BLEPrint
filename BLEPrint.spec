# -*- mode: python ; coding: utf-8 -*-
# PyInstaller build recipe for BLEPrint.  Build with:  ./build.sh   (or: pyinstaller BLEPrint.spec)

a = Analysis(
    ['gui.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name='BLEPrint',
    console=False,          # windowed GUI app
    icon='assets/logo.icns',
)
coll = COLLECT(exe, a.binaries, a.datas, name='BLEPrint')

# macOS .app bundle. The Info.plist keys below are what let the built app use
# Bluetooth without crashing (NSBluetoothAlwaysUsageDescription is required).
app = BUNDLE(
    coll,
    name='BLEPrint.app',
    icon='assets/logo.icns',
    bundle_identifier='com.bleprint.app',
    info_plist={
        'CFBundleName': 'BLEPrint',
        'CFBundleDisplayName': 'BLEPrint',
        'CFBundleShortVersionString': '0.2.1',
        'NSHighResolutionCapable': True,
        'NSBluetoothAlwaysUsageDescription':
            'BLEPrint uses Bluetooth to connect to your label printer.',
    },
)
