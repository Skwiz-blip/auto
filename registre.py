"""Dossiers déjà contrôlés : un nouveau lancement les saute automatiquement.

Un dossier est reconnu par l'empreinte de son PDF (taille + premier Mo du fichier), pas par son
seul nom : un dossier corrigé puis renvoyé sous le même nom est un autre fichier, il est donc
contrôlé à nouveau. Seuls les contrôles qui ont abouti comptent (pas les erreurs techniques).
"""
import hashlib
import json
from pathlib import Path

import chemins


def empreinte_pdf(pdf: Path) -> str:
    """Empreinte rapide d'un PDF : sa taille et le hachage de son premier Mo."""
    with open(pdf, "rb") as f:
        debut = f.read(1 << 20)
    return f"{pdf.stat().st_size}-{hashlib.sha256(debut).hexdigest()[:24]}"


def deja_controles(sorties: Path | None = None) -> dict[str, dict]:
    """{empreinte: {"dossier", "statut", "sortie"}} des dossiers contrôlés lors des lancements
    précédents (le plus récent l'emporte)."""
    connus = {}
    racine = sorties or chemins.SORTIES
    for sortie in sorted(racine.glob("controle_*")):
        fichier = sortie / "resultats.jsonl"
        if not fichier.exists():
            continue
        for ligne in fichier.read_text(encoding="utf-8").splitlines():
            try:
                j = json.loads(ligne)
            except ValueError:
                continue  # ligne en cours d'écriture
            if j.get("empreinte_pdf") and j.get("decision") and not j.get("erreur"):
                connus[j["empreinte_pdf"]] = {"dossier": j["dossier"],
                                              "statut": j["decision"].get("statut", ""),
                                              "sortie": str(sortie)}
    return connus


def a_controler(pdfs: list[Path], recontroler: bool = False,
                sorties: Path | None = None) -> tuple[list[Path], list[Path]]:
    """(PDF à contrôler, PDF déjà contrôlés et sautés)."""
    if recontroler:
        return list(pdfs), []
    connus = deja_controles(sorties)
    nouveaux, sautes = [], []
    for pdf in pdfs:
        (sautes if empreinte_pdf(pdf) in connus else nouveaux).append(pdf)
    return nouveaux, sautes
