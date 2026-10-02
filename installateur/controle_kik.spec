# Recette PyInstaller : « Controle KIK.exe » et tout ce qu'il lui faut (lancée par construire.bat).
# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

ICI = Path(SPECPATH)
PROJET = ICI.parent

datas = [
    (str(PROJET / "application" / "ui"), "ui"),
    (str(PROJET / "MODELE SHAREPOINT VC.xlsx"), "."),
    (str(PROJET / "NOM DES COMMERCIAUX.xlsx"), "."),   # liste officielle des commerciaux (BDP)
] + collect_data_files("rapidocr")                 # modèles PP-OCR (~31 Mo) et configuration
binaries = collect_dynamic_libs("pyzbar") + collect_dynamic_libs("onnxruntime")
msvcr = Path(r"C:\Windows\System32\msvcr120.dll")  # requis par libzbar
if msvcr.exists():
    binaries.append((str(msvcr), "."))

a = Analysis(
    [str(PROJET / "application" / "app.py")],
    pathex=[str(PROJET)],
    binaries=binaries,
    datas=datas,
    hiddenimports=["controle", "sharepoint", "ocr_local", "qr_officiel", "bench_dossiers", "registre",
                   "chemins", "referentiel", "openpyxl", "onnxruntime"]
                  + collect_submodules("webview") + collect_submodules("rapidocr"),
    excludes=["tkinter", "matplotlib", "IPython", "pytest"],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Controle KIK",
          console=False, upx=False)
coll = COLLECT(exe, a.binaries, a.datas, upx=False, name="Controle KIK")
