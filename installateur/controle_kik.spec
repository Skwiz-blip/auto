# Recette PyInstaller : « Controle KIK.exe » et tout ce qu'il lui faut (lancée par construire.bat).
# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules

ICI = Path(SPECPATH)
PROJET = ICI.parent
TESSERACT = ICI / "tmp" / "tesseract"   # copie réduite préparée par construire.bat

datas = [
    (str(PROJET / "application" / "ui"), "ui"),
    (str(PROJET / "tessdata"), "tessdata"),
    (str(PROJET / "MODELE SHAREPOINT VC.xlsx"), "."),
    (str(TESSERACT), "tesseract"),
]
binaries = collect_dynamic_libs("pyzbar")          # libzbar-64.dll, libiconv.dll
msvcr = Path(r"C:\Windows\System32\msvcr120.dll")  # requis par libzbar
if msvcr.exists():
    binaries.append((str(msvcr), "."))

a = Analysis(
    [str(PROJET / "application" / "app.py")],
    pathex=[str(PROJET)],
    binaries=binaries,
    datas=datas,
    hiddenimports=["controle", "sharepoint", "ocr_local", "qr_officiel", "bench_dossiers",
                   "chemins", "openpyxl"] + collect_submodules("webview"),
    excludes=["tkinter", "matplotlib", "IPython", "pytest"],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Controle KIK",
          console=False, upx=False)
coll = COLLECT(exe, a.binaries, a.datas, upx=False, name="Controle KIK")
