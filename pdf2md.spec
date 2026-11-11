# -*- mode: python ; coding: utf-8 -*-
# Сборка:  pyinstaller --noconfirm pdf2md.spec
#   Windows → dist/PDF2MD.exe (один файл)
#   macOS   → dist/PDF2MD.app
import sys
from PyInstaller.utils.hooks import collect_all

datas = [("ui/index.html", "ui")]
binaries, hiddenimports = [], ["main"]
for pkg in ("pymupdf", "pymupdf4llm", "onnxruntime", "webview"):
    d, b, h = collect_all(pkg)
    datas += d; binaries += b; hiddenimports += h

a = Analysis(
    ["gui.py"], pathex=["."], datas=datas, binaries=binaries,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "matplotlib", "PyQt5", "PyQt6", "PySide6", "IPython", "pytest"],
)
pyz = PYZ(a.pure)

if sys.platform == "win32":
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name="PDF2MD",
              console=False, icon="assets/icon.ico", upx=False)
else:
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="PDF2MD",
              console=False, icon="assets/icon.icns", upx=False)
    coll = COLLECT(exe, a.binaries, a.datas, name="PDF2MD", upx=False)
    app = BUNDLE(coll, name="PDF2MD.app", icon="assets/icon.icns",
                 bundle_identifier="local.pdf2md",
                 info_plist={"CFBundleDisplayName": "PDF → MD",
                             "NSHighResolutionCapable": True,
                             "LSMinimumSystemVersion": "11.0"})
