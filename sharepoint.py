"""Remplit directement le fichier SharePoint (MODELE SHAREPOINT VC.xlsx) avec les dossiers VALIDÉS.

Une ligne par dossier validé, ajoutée à la suite des lignes déjà présentes.
Le texte manuscrit mal lu est reporté tel quel (« ? » à la place des caractères illisibles) :
le dossier est validé, seule la saisie reste à compléter à la main.
"""
import json
import re
import shutil
import sys
from datetime import date
from pathlib import Path

from openpyxl import load_workbook

from bench_dossiers import SCHEMA, conform

ROOT = Path(__file__).resolve().parent
MODELE = ROOT / "MODELE SHAREPOINT VC.xlsx"


def _compte(valeur: str, defaut: str) -> str:
    """« 1 », « 01 », « 1 head » -> « 01 » ; case vide ou illisible -> valeur par défaut."""
    chiffres = re.sub(r"\D", "", valeur or "")
    return chiffres.zfill(2) if chiffres else defaut


def ligne(journal: dict, date_transmission: str) -> list[str]:
    # conform : complète les champs absents des résultats plus anciens (ex. nombre_head)
    e = conform(journal["extraction"], SCHEMA)
    fiche, rccm, pid = e["fiche"], e["rccm"], e["piece_identite"]
    structure = fiche["nom_structure"].strip() or rccm["enseigne"] or rccm["nom_commercial"]
    # promoteur : nom et prénoms du RCCM (à défaut, ceux de la pièce d'identité)
    nom = (rccm["nom"] or pid["nom"]).strip()
    prenoms = (rccm["prenoms"] or pid["prenoms"]).strip()
    contact = re.sub(r"[^\d?+]", "", fiche["telephone"])  # « 01 69 83 76 30 » -> « 0169837630 »
    return [
        date_transmission,                                   # A DATE DE TRANSMISSION
        structure,                                           # B NOM STRUCTURE
        _compte(fiche["nombre_head"], "01"),                 # C PRINCIPAL
        _compte(fiche["nombre_sous_comptes"], "00"),         # D SOUS COMPTE
        nom, prenoms, contact,                               # E-G PROMOTEUR
        nom, prenoms, contact,                               # H-J GESTIONNAIRE (le même)
        fiche["secteur"].strip(),                            # K SECTEUR D'ACTIVITE
        fiche["departement"].strip(),                        # L DEPARTEMENT
        fiche["ville"].strip(),                              # M VILLE/COMMUNE
        fiche["quartier"].strip(),                           # N SITUATION GEOGRAPHIQUE
        fiche["commercial"].strip(),                         # O NOM DU COMMERCIAL
        "",                                                  # P NUMERO ATTRIBUE (laissé vide)
    ]


def _derniere_ligne(feuille) -> int:
    """Dernière ligne réellement remplie (openpyxl compte parfois des lignes vides mises en forme)."""
    for n in range(feuille.max_row, 1, -1):
        if any(feuille.cell(row=n, column=c).value not in (None, "") for c in range(1, 17)):
            return n
    return 1


def remplir(sortie: Path, journaux: list[dict], date_transmission: str = "") -> Path | None:
    """Ajoute les dossiers VALIDÉS à la suite des lignes de MODELE SHAREPOINT VC.xlsx.

    Un même lancement n'est jamais ajouté deux fois : les dossiers déjà reportés sont notés
    dans <sortie>/sharepoint_ajoutes.txt. Une copie du modèle avant écriture est gardée dans
    <sortie>/MODELE SHAREPOINT VC (avant).xlsx. None si rien de nouveau à ajouter."""
    registre = sortie / "sharepoint_ajoutes.txt"
    deja = set(registre.read_text(encoding="utf-8").splitlines()) if registre.exists() else set()
    valides = sorted((j for j in journaux
                      if (j.get("decision") or {}).get("statut") == "VALIDÉ"
                      and j["dossier"] not in deja),
                     key=lambda j: j["dossier"])
    if not valides or not MODELE.exists():
        return None
    date_transmission = date_transmission or date.today().strftime("%d/%m/%Y")
    classeur = load_workbook(MODELE)
    feuille = classeur.worksheets[0]
    depart = _derniere_ligne(feuille) + 1
    for n, journal in enumerate(valides, start=depart):
        for col, valeur in enumerate(ligne(journal, date_transmission), start=1):
            cellule = feuille.cell(row=n, column=col, value=valeur)
            cellule.number_format = "@"  # texte : garde le 0 des numéros et de « 01 »
    shutil.copy2(MODELE, sortie / f"{MODELE.stem} (avant).xlsx")
    classeur.save(MODELE)  # PermissionError si le fichier est ouvert dans Excel
    with open(registre, "a", encoding="utf-8") as f:
        f.write("".join(j["dossier"] + "\n" for j in valides))
    return MODELE


def remplir_depuis(sortie: Path) -> Path | None:
    journaux = [json.loads(l) for l in (sortie / "resultats.jsonl").read_text(
        encoding="utf-8").splitlines() if l.strip()]
    return remplir(sortie, journaux)


if __name__ == "__main__":
    # python sharepoint.py <dossier de sortie d'un lancement>
    print(remplir_depuis(Path(sys.argv[1])))
