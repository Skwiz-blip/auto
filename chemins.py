"""Emplacements des fichiers, en développement comme dans l'application installée.

- RESSOURCES : fichiers fournis avec le programme, en lecture seule (modèle SharePoint vierge ;
  les modèles PP-OCR sont embarqués avec le paquet rapidocr). Dans l'application installée,
  c'est le dossier interne de l'exe.
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
# liste officielle des commerciaux (BDP) fournie par KIK : modifiable sans toucher au programme
COMMERCIAUX = DONNEES / "NOM DES COMMERCIAUX.xlsx"


def preparer() -> None:
    """Crée le dossier de données et y dépose, au premier lancement, le modèle SharePoint vierge
    et la liste des commerciaux livrés avec le programme."""
    SORTIES.mkdir(parents=True, exist_ok=True)
    for cible in (MODELE, COMMERCIAUX):
        livre = RESSOURCES / cible.name
        if not cible.exists() and livre.exists() and livre != cible:
            shutil.copy2(livre, cible)
