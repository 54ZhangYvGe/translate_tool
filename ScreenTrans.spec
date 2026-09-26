# Windows portable one-directory build. No local configuration or credentials.
from PyInstaller.utils.hooks import collect_all, collect_data_files
import os
diagnostic = os.environ.get("SCREENTRANS_BUILD_CONSOLE") == "1"

ocr_data, ocr_binaries, ocr_imports = collect_all("rapidocr")
a = Analysis(
    ["resident_app.py"],
    pathex=[],
    binaries=ocr_binaries,
    datas=ocr_data + collect_data_files("onnxruntime") + [("assets/transeasy-icon.ico", "assets")],
    hiddenimports=ocr_imports + ["screen_ocr", "onnxruntime", "release_smoke"],
    hookspath=[],
    runtime_hooks=[],
    excludes=["PyQt5", "PyQt6", "PySide2", "torch", "tensorflow", "paddle"],
    noarchive=False,
)
# Qt uses Windows' system ICU API. A developer PATH (e.g. Poppler) can
# accidentally contribute an incompatible same-named ICU build.
a.binaries = [entry for entry in a.binaries
              if entry[0].lower() not in ("icuuc.dll", "icudt78.dll")]
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True,
    name="ScreenTransResident", debug=False, bootloader_ignore_signals=False,
    icon="assets/transeasy-icon.ico",
    strip=False, upx=False, console=diagnostic,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="ScreenTrans-debug" if diagnostic else "ScreenTrans")
