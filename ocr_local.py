"""Lecture locale des dossiers (Tesseract) : identification des pages + extraction.

Étape 1 du traitement économique :
  - identifie chaque page en lisant seulement son en-tête (rapide) ;
  - lit en entier les pièces imprimées (RCCM, IFU, carte APIEx, pièce d'identité) ;
  - extrait les champs par motifs (numéros, dates, noms) ;
  - signale ce qu'il n'a pas pu lire : ces pages-là seront envoyées à Claude Haiku.

Rien n'est envoyé à l'extérieur, rien n'est écrit sur le disque.

Usage :
    python ocr_local.py --limit 5          # essai
    python ocr_local.py --json sortie.json # lecture complète du lot
"""

import argparse
import io
import json
import os
import re
import time
import unicodedata
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import fitz
import pytesseract
from PIL import Image

ROOT = Path(__file__).parent
os.environ.setdefault("TESSDATA_PREFIX", str(ROOT / "tessdata"))
pytesseract.pytesseract.tesseract_cmd = os.environ.get(
    "TESSERACT_EXE", r"C:\Program Files\Tesseract-OCR\tesseract.exe")
LANGUES = "fra+eng"

# Marqueurs cherchés dans l'en-tête des pages (texte normalisé sans accents)
# l'ordre compte : le contrat est testé en premier, car ses pages citent aussi « CELTIIS »
MARQUEURS = {
    # (pas « ANNEXE » seul : le RCCM a une rubrique « ANNEXES »)
    "contrat": ["CONTRAT DE PAIEMENT", "CELTIIS CASH |", "ANNEXE 1", "ANNEXE 2", "ANNEXE 3",
                "ANNEXE I", "OBLIGATIONS DU MARCHAND", "OBLIGATIONS DE LA SBIN"],
    # (pas « CELTIIS » seul : toutes les pages du contrat le citent)
    "fiche": ["FICHE DE CREATION", "KIK EXPERIENCE", "CREATION MARCHAND"],
    "rccm": ["EXTRAIT DU REGISTRE", "REGISTRE DU COMMERCE", "GREFFE DU TRIBUNAL",
             "TRIBUNAL DE COMMERCE"],
    "ifu": ["ATTESTATION D'IMMATRICULATION", "ATTESTATION D IMMATRICULATION",
            "IDENTIFIANT FISCAL UNIQUE", "DIRECTION GENERALE DES IMPOTS"],
    "apiex": ["CARTE PROFESSIONNELLE", "APIEX"],
    "piece_identite": ["CERTIFICAT D'IDENTIFICATION PERSONNELLE",
                       "CERTIFICAT D IDENTIFICATION PERSONNELLE",
                       "CARTE NATIONALE D'IDENTITE", "PASSEPORT"],
}

RE_RCCM = re.compile(r"RB\s*/?\s*[A-Z]{2,4}\s*/?\s*\d{2}\s*[A-Z]\s*\d{3,6}")
RE_IFU = re.compile(r"\b\d{13}\b")
RE_NPI = re.compile(r"\b\d{14}\b")
RE_DATE = re.compile(r"\b(\d{2})[-/.](\d{2})[-/.](\d{4})\b")
RE_EXPIRE = re.compile(r"EXPIRE?\s*(?:LE)?\s*:?\s*(\d{2})[-/.](\d{2})[-/.](\d{4})")
MOTS_ENTETE = ("MINISTERE", "ECONOMIE", "FINANCE", "REPUBLIQUE", "DIRECTION", "GENERALE",
               "IMPOT", "ATTESTATION", "IDENTIFIANT", "REGISTRE", "COMMERCE", "GREFFE",
               "TRIBUNAL", "CERTIFICAT", "BENIN", "ETAT", "AGENCE")
CHAMPS_NOM = {
    # \bNOM\b : ne pas capter le « NOM » de « DENOMINATION » ; « PATRONYMIQUE » est souvent
    # mal lu par l'OCR (« FATRONYMIQUE ») d'où la terminaison seule
    "nom": r"\bNOM\b\s*(?:[A-Z]{0,4}RON\s?YMIQUE)?\s*[:.]?\s*([A-ZÉÈÀÙÇ' -]{2,40})",
    "prenoms": r"\bPRENOMS?\b\s*(?:\(S\))?\s*[:.]?\s*([A-ZÉÈÀÙÇ' -]{2,60})",
    "enseigne": r"ENSEIGNE\s*(?:COMMERCIALE)?\s*[:.]?\s*([A-Z0-9ÉÈÀÙÇ'& -]{3,60})",
    "nom_commercial": r"NOM\s+COMMERC\w*\s*[:.]?\s*([A-Z0-9ÉÈÀÙÇ'& -]{3,60})",
}


def _sans_accents(texte: str) -> str:
    n = unicodedata.normalize("NFKD", texte)
    return "".join(c for c in n if not unicodedata.combining(c)).upper()


def _image(page, largeur: int, haut: float = 1.0) -> Image.Image:
    """Rend une page (ou son en-tête si haut < 1) en niveaux de gris."""
    zoom = largeur / page.rect.width
    clip = None
    if haut < 1.0:
        clip = fitz.Rect(page.rect.x0, page.rect.y0, page.rect.x1,
                         page.rect.y0 + page.rect.height * haut)
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=clip, colorspace=fitz.csGRAY)
    return Image.open(io.BytesIO(pix.tobytes("png")))


def _type_depuis_texte(texte: str) -> str:
    for piece, marqueurs in MARQUEURS.items():
        if any(m in texte for m in marqueurs):
            return piece
    return "inconnu"


def _angle(image: Image.Image) -> int:
    """Orientation détectée par Tesseract (0, 90, 180 ou 270). 0 si indéterminée."""
    try:
        osd = pytesseract.image_to_osd(image, output_type=pytesseract.Output.DICT)
        return int(osd.get("rotate", 0)) % 360
    except pytesseract.TesseractError:
        return 0


def identifier_pages(doc) -> tuple[dict, dict]:
    """({page: type}, {page: angle de rotation}) en lisant surtout les en-têtes."""
    types, angles, contrat_commence = {}, {}, None
    for i, page in enumerate(doc, 1):
        texte = _sans_accents(pytesseract.image_to_string(
            _image(page, 1200, 0.28), lang=LANGUES, config="--psm 6"))
        trouve = _type_depuis_texte(texte)
        angles[i] = 0
        if trouve == "inconnu":
            # la pièce n'occupe parfois qu'une partie de la page (CIP au milieu) : page entière
            pleine = _image(page, 1400)
            trouve = _type_depuis_texte(_sans_accents(pytesseract.image_to_string(
                pleine, lang=LANGUES, config="--psm 6")))
        if trouve == "inconnu":
            # page peut-être pivotée (scan de travers) : on redresse et on réessaie. L'angle
            # n'est retenu que si la page redressée est reconnue : la détection d'orientation
            # se trompe souvent et retournait à l'envers des pages droites
            rot = _angle(pleine)
            if rot:
                texte2 = _sans_accents(pytesseract.image_to_string(
                    pleine.rotate(-rot, expand=True), lang=LANGUES, config="--psm 6"))
                trouve_pivote = _type_depuis_texte(texte2)
                if trouve_pivote not in ("inconnu", "contrat"):
                    trouve, angles[i] = trouve_pivote, rot
        if trouve == "contrat" and contrat_commence is None:
            contrat_commence = i
        types[i] = trouve

    # une page inconnue qui suit la première page du RCCM en est le folio 2/2 ; seul le RCCM
    # s'étend sur deux pages (l'IFU et l'APIEx tiennent sur une : la page suivante peut être
    # la pièce d'identité, scannée de travers)
    for i in range(2, len(types) + 1):
        if types[i] == "inconnu" and types[i - 1] == "rccm" and types.get(i - 2) != "rccm":
            types[i] = "rccm"
    # pages suivant le début du contrat
    if contrat_commence:
        for i in range(contrat_commence + 1, len(types) + 1):
            if types[i] == "inconnu":
                types[i] = "contrat"
    # les 4 pièces trouvées : les pages inconnues restantes en fin de dossier sont le contrat
    if {"fiche", "rccm", "piece_identite"} <= set(types.values()) and (
            "ifu" in types.values() or "apiex" in types.values()):
        derniere_piece = max(i for i, t in types.items()
                             if t in ("fiche", "rccm", "ifu", "apiex", "piece_identite"))
        for i in range(derniere_piece + 1, len(types) + 1):
            if types[i] == "inconnu":
                types[i] = "contrat"
    return types, angles


def lire_page(page, largeur: int = 2200, angle: int = 0) -> tuple[str, float]:
    """Texte complet d'une page + confiance moyenne de l'OCR (0-100)."""
    img = _image(page, largeur)
    if angle:
        img = img.rotate(-angle, expand=True)
    data = pytesseract.image_to_data(img, lang=LANGUES, output_type=pytesseract.Output.DICT)
    mots = [(m, int(c)) for m, c in zip(data["text"], data["conf"]) if int(c) > 0 and m.strip()]
    texte = " ".join(m for m, _ in mots)
    conf = sum(c for _, c in mots) / len(mots) if mots else 0.0
    return texte, conf


def extraire(texte: str) -> dict:
    """Champs reconnaissables par motif dans un texte imprimé."""
    up = _sans_accents(texte)
    champs = {}
    if m := RE_RCCM.search(up):
        champs["rccm"] = re.sub(r"\s+", " ", m.group()).strip()
    if m := RE_IFU.search(up):
        champs["ifu"] = m.group()
    if m := RE_NPI.search(up):
        champs["npi"] = m.group()
    if m := RE_EXPIRE.search(up):
        champs["date_expiration"] = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    dates = [f"{a}-{mo}-{j}" for j, mo, a in RE_DATE.findall(up)]
    if dates:
        champs["dates"] = sorted(set(dates))
    for cle, motif in CHAMPS_NOM.items():
        if m := re.search(motif, up):
            valeur = " ".join(m.group(1).split())
            # l'OCR rend un texte à plat : on coupe dès l'étiquette suivante
            # (« NOM » seul et « COMMERC… » mal lu : l'étiquette NOM COMMERCIAL qui suit l'enseigne)
            valeur = re.split(r"\b(?:SIGLE|NEANT|NEAN|NATIONALITE|ADRESSE|PRENOMS?|ENSEIGNE"
                              r"|ACTIVITE|DOMICILE|DATE|COMMERC\w*|REGISTRE|NOM)\b",
                              valeur)[0].strip(" -:.")
            # écarte les en-têtes officiels captés par erreur (MINISTERE…, REPUBLIQUE…)
            if len(valeur) > 1 and not any(mot in valeur for mot in MOTS_ENTETE):
                champs[cle] = valeur
    return champs


def lire_dossier(pdf: Path, conf_min: float = 75.0) -> dict:
    t0 = time.perf_counter()
    res = {"dossier": pdf.stem.strip(), "pages": {}, "pieces": {}, "champs": {},
           "pages_a_envoyer": [], "manques": []}
    with fitz.open(pdf) as doc:
        res["nb_pages"] = doc.page_count
        types, angles = identifier_pages(doc)
        res["pages"] = types
        res["rotations"] = {i: a for i, a in angles.items() if a}
        for piece in ("rccm", "ifu", "apiex", "piece_identite"):
            pages = [i for i, t in types.items() if t == piece]
            if not pages:
                continue
            textes, confs = [], []
            for i in pages:
                texte, conf = lire_page(doc[i - 1], angle=angles[i])
                textes.append(texte)
                confs.append(conf)
            champs = extraire(" ".join(textes))
            res["pieces"][piece] = {"pages": pages, "confiance": round(sum(confs) / len(confs), 1)}
            res["champs"][piece] = champs
            # champs attendus par pièce : ce qui manque part chez Claude
            attendus = {"rccm": ["rccm", "nom"], "ifu": ["ifu"], "apiex": ["ifu", "rccm"],
                        "piece_identite": ["date_expiration", "nom"]}[piece]
            absents = [c for c in attendus if c not in champs]
            if absents or sum(confs) / len(confs) < conf_min:
                res["manques"].append(f"{piece}: {', '.join(absents) or 'confiance faible'}")
                res["pages_a_envoyer"] += pages
        # Claude reçoit les 4 pièces à contrôler et les pages non identifiées ; le contrat,
        # qui pèse 7 à 8 pages et n'est plus contrôlé, reste en local.
        res["pages_a_envoyer"] += [i for i, t in types.items() if t != "contrat"]
        inconnues = [i for i, t in types.items() if t == "inconnu"]
        if inconnues:
            res["manques"].append(f"pages non identifiées : {inconnues}")
        for piece in ("rccm", "ifu", "piece_identite"):
            if not [i for i, t in types.items() if t == piece]:
                manque = piece
                if piece == "ifu" and [i for i, t in types.items() if t == "apiex"]:
                    manque = "ifu (carte APIEx trouvée)"
                res["manques"].append(f"{manque} non trouvé localement")
    res["pages_a_envoyer"] = sorted(set(res["pages_a_envoyer"]))
    res["t_ocr_s"] = round(time.perf_counter() - t0, 1)
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dossier", default=str(ROOT / "Dossier test"))
    ap.add_argument("--only", nargs="*", default=[])
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--json", default="")
    ap.add_argument("--jobs", type=int, default=0, help="dossiers traités en parallèle")
    args = ap.parse_args()

    pdfs = sorted(Path(args.dossier).glob("*.pdf"))
    if args.only:
        pdfs = [p for p in pdfs if p.stem.strip() in args.only]
    if args.limit:
        pdfs = pdfs[:args.limit]

    resultats, total_pages, total_envoi = [], 0, 0
    t_debut = time.perf_counter()
    with ProcessPoolExecutor(max_workers=args.jobs or None) as pool:
        for r in pool.map(lire_dossier, pdfs):
            resultats.append(r)
            total_pages += r["nb_pages"]
            total_envoi += len(r["pages_a_envoyer"])
            champs = {p: list(c) for p, c in r["champs"].items() if c}
            print(f"{r['dossier'][:34]:<34} {r['t_ocr_s']:>5.1f}s  "
                  f"{r['nb_pages']:>2}p -> envoi {len(r['pages_a_envoyer'])}p "
                  f"{r['pages_a_envoyer']}  {champs}", flush=True)
            for m in r["manques"]:
                print(f"      ? {m}", flush=True)
    if resultats:
        wall = time.perf_counter() - t_debut
        pieces_ok = sum(1 for r in resultats if not r["manques"])
        print(f"\n{len(resultats)} dossiers | durée réelle {wall / 60:.1f} min "
              f"({wall / len(resultats):.1f}s par dossier, {args.jobs or 'auto'} en parallèle) | "
              f"cumul processeur {sum(r['t_ocr_s'] for r in resultats) / 60:.1f} min")
        print(f"Pages à envoyer à Claude : {total_envoi}/{total_pages} "
              f"({100 * total_envoi / total_pages:.0f} %) | "
              f"dossiers lus entièrement en local : {pieces_ok}/{len(resultats)}")
        envois = sorted(len(r["pages_a_envoyer"]) for r in resultats)
        print(f"Pages envoyées par dossier : min {envois[0]}, médiane "
              f"{envois[len(envois) // 2]}, max {envois[-1]}")
    if args.json:
        Path(args.json).write_text(json.dumps(resultats, ensure_ascii=False, indent=1),
                                   encoding="utf-8")


if __name__ == "__main__":
    main()
