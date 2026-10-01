"""Emplacements des fichiers, en développement comme dans l'application installée.

- RESSOURCES : fichiers fournis avec le programme, en lecture seule (tessdata, Tesseract,
  modèle SharePoint vierge). Dans l'application installée, c'est le dossier interne de l'exe.
- DONNEES : ce que le programme écrit (résultats, cache des QR, fichier SharePoint rempli,
  .env). Le dossier d'installation n'est pas modifiable : l'application installée écrit dans
  Documents\\KIK-Controle. En développement, tout reste dans le dossier du projet.
"""
import os
import shutil
import sys
from pathlib import Path

INSTALLE = getattr(sys, "frozen", False)
PROJET = Path(__file__).resolve().parent
RESSOURCES = Path(getattr(sys, "_MEIPASS", PROJET))

if os.environ.get("KIK_DONNEES"):
    DONNEES = Path(os.environ["KIK_DONNEES"])
elif INSTALLE:
    DONNEES = Path.home() / "Documents" / "KIK-Controle"
else:
    DONNEES = PROJET

SORTIES = DONNEES / "sorties"
MODELE = DONNEES / "MODELE SHAREPOINT VC.xlsx"
TESSDATA = RESSOURCES / "tessdata"

# Tesseract embarqué avec l'application, sinon celui installé sur la machine
_embarque = RESSOURCES / "tesseract" / "tesseract.exe"
TESSERACT_EXE = str(_embarque if _embarque.exists()
                    else Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe"))


def preparer() -> None:
    """Crée le dossier de données et y dépose le modèle SharePoint vierge au premier lancement."""
    SORTIES.mkdir(parents=True, exist_ok=True)
    vierge = RESSOURCES / "MODELE SHAREPOINT VC.xlsx"
    if not MODELE.exists() and vierge.exists() and vierge != MODELE:
        shutil.copy2(vierge, MODELE)
