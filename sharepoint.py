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
from openpyxl.styles import PatternFill

import chemins
import referentiel
from bench_dossiers import SCHEMA, conform, nom_valide
from qr_officiel import numero_benin

MODELE = chemins.MODELE
A_COMPLETER = PatternFill("solid", fgColor="FFD8A8")  # orange clair


def _contact(fiche: dict, pid: dict, rccm: dict, ifu: dict) -> str:
    """Numéro de contact (KIK) : le « Numéro personnel » écrit sur la fiche, celui que le
    marchand utilise (le numéro du RCCM n'est parfois plus en service). S'il ne diffère que
    d'un chiffre d'un numéro imprimé (CIP, RCCM, IFU), c'est une erreur de lecture de
    l'écriture : le numéro imprimé est retenu. Fiche vide ou illisible : CIP, RCCM, puis IFU."""
    imprimes = [n for n in (numero_benin(pid["telephone"]), numero_benin(rccm["telephone"]),
                            numero_benin(ifu["telephone"])) if n]
    ecrit = numero_benin(fiche["telephone"]) if "?" not in fiche["telephone"] else ""
    if not ecrit:
        return imprimes[0] if imprimes else ""
    if ecrit in imprimes:
        return ecrit  # confirmé par une pièce imprimée
    proche = next((n for n in imprimes
                   if sum(a != b for a, b in zip(n, ecrit)) == 1), "")
    return proche or ecrit


def ligne(journal: dict, date_transmission: str) -> list:
    """Imprimé d'abord (RCCM, IFU), manuscrit ensuite : l'écriture de la fiche est la source la
    moins sûre. Ville et département sont ramenés au référentiel des communes du Bénin."""
    # conform : complète les champs absents des résultats plus anciens (ex. nombre_head)
    e = conform(journal["extraction"], SCHEMA)
    fiche, rccm, ifu, pid = e["fiche"], e["rccm"], e["ifu"], e["piece_identite"]
    structure = next((v for v in (rccm["enseigne"], rccm["nom_commercial"],
                                   ifu["nom_etablissement"]) if nom_valide(v)),
                     fiche["nom_structure"]).strip()
    # promoteur : nom et prénoms du RCCM (à défaut, ceux de la pièce d'identité)
    nom = (rccm["nom"] or pid["nom"]).strip().upper()
    prenoms = (rccm["prenoms"] or pid["prenoms"]).strip().upper()
    numero = _contact(fiche, pid, rccm, ifu)
    contact = ("229" + numero) if numero else re.sub(r"[^\d?]", "", fiche["telephone"])
    ville = referentiel.commune(fiche["ville"])
    return [
        date_transmission,                                                   # A
        structure,                                                           # B STRUCTURE
        referentiel.nombre_comptes(fiche["nombre_head"], 1),          # C PRINCIPAL : 0 ou 1
        referentiel.nombre_comptes(fiche["nombre_sous_comptes"], 0),  # D SOUS COMPTE : 0 ou 1
        nom, prenoms, contact,                                               # E-G PROMOTEUR
        nom, prenoms, contact,                                               # H-J GESTIONNAIRE
        fiche["secteur"].strip(),                                            # K SECTEUR
        referentiel.departement(fiche["departement"], ville, fiche["commercial"]),       # L
        ville or fiche["ville"].strip(),                                     # M VILLE/COMMUNE
        fiche["quartier"].strip(),                                           # N SITUATION GEO.
        referentiel.commercial(fiche["commercial"]),  # O : nom de la liste officielle, ou vide
        "",                                                                  # P (laissé vide)
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
            # numéros en texte (garde le 0 de « 01… ») ; principal et sous-comptes en nombres
            cellule.number_format = "General" if isinstance(valeur, int) else "@"
            # à compléter à la main : caractère illisible, contact non conforme
            if ("?" in str(valeur) or (col in (7, 10) and not re.fullmatch(r"22901\d{8}", valeur))
                    or (col in (12, 15) and not valeur)):
                cellule.fill = A_COMPLETER
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
