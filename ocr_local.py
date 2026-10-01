"""Lecture locale des dossiers (PP-OCR via RapidOCR) : identification des pages + extraction.

Étape 1 du traitement économique :
  - identifie chaque page (fiche, RCCM, IFU, carte professionnelle, CNSS, ONG, pièce
    d'identité, contrat), y compris les pages scannées couchées ;
  - lit les pièces imprimées et en extrait les champs par motifs (numéros, dates, noms) ;
  - signale ce qu'il n'a pas pu lire : ces pages-là seront envoyées à Claude.

PP-OCR remplace Tesseract (septembre 2026) : sur les photos grises, les CIP délavées et les
pages couchées, Tesseract ne rendait presque rien. Tout reste local, rien n'est écrit sur disque.

Usage :
    python ocr_local.py --limit 5          # essai
    python ocr_local.py --json sortie.json # lecture complète du lot
"""

import argparse
import io
import json
import logging
import os
import re
import time
import unicodedata
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import fitz
import numpy as np
from PIL import Image

ROOT = Path(__file__).parent
# fils de calcul par processus : le moteur tourne déjà dans plusieurs processus en parallèle
THREADS_OCR = int(os.environ.get("KIK_THREADS_OCR", "2"))

# Marqueurs cherchés dans le texte des pages (normalisé sans accents ; comparés aussi sans
# espaces, l'OCR collant parfois les mots : « CARTEPROFESSIONNELLE »).
# L'ordre compte : la fiche est testée en premier (son titre « FICHE DE CREATION MARCHAND -
# CELTIIS CASH » et ses cases « CNI / CIP / Passeport » ressemblent au contrat et à une pièce)
MARQUEURS = {
    # titre, puis libellés du corps du formulaire (photo pâle : le titre est souvent illisible)
    "fiche": ["FICHE DE CREATION", "KIK EXPERIENCE", "CREATION MARCHAND",
              "TAUX DE REVERSEMENT", "NOMBRE DE HEAD", "NOMBRE DE SOUS COMPTES",
              "REPRESENTANT LEGAL", "NOM DE LA STRUCTURE", "MODE DE REMONTEE"],
    # (pas « ANNEXE » seul : le RCCM a une rubrique « ANNEXES » ; pas « CELTIIS CASH » : c'est
    # aussi le titre de la fiche)
    "contrat": ["CONTRAT DE PAIEMENT", "ANNEXE 1", "ANNEXE 2", "ANNEXE 3",
                "ANNEXE I", "OBLIGATIONS DU MARCHAND", "OBLIGATIONS DE LA SBIN"],
    "rccm": ["EXTRAIT DU REGISTRE", "REGISTRE DU COMMERCE", "GREFFE DU TRIBUNAL",
             "TRIBUNAL DE COMMERCE"],
    "ifu": ["ATTESTATION D'IMMATRICULATION", "ATTESTATION D IMMATRICULATION",
            "IDENTIFIANT FISCAL UNIQUE", "DIRECTION GENERALE DES IMPOTS"],
    "apiex": ["CARTE PROFESSIONNELLE", "APIEX"],
    # attestation CNSS : son matricule employeur commence par le n° IFU (accepté par KIK)
    "cnss": ["SECURITE SOCIALE", "IMMATRICULATION EMPLOYEUR"],
    # association / ONG : récépissé de déclaration au lieu du RCCM (contrôle humain)
    "ong": ["ORGANISATION NON GOUVERNEMENTALE", "RECEPISSE DE DECLARATION"],
    # (pas « PASSEPORT » seul : la fiche a une case « CNI / CIP / Passeport »)
    "piece_identite": ["CERTIFICAT D'IDENTIFICATION PERSONNELLE",
                       "CERTIFICAT D IDENTIFICATION PERSONNELLE", "IDENTIFICATION PERSONNELLE",
                       "NUMERO PERSONNEL D'IDENTIFICATION", "CARTE NATIONALE D'IDENTITE",
                       "CARTE D'IDENTITE CEDEAO", "IDENTITE CEDEAO", "ECOWAS IDENTITY",
                       "PASSPORT"],
}
# pièces qui remplacent l'IFU ou le RCCM (règles KIK)
SUBSTITUTS_IFU = ("apiex", "cnss")
SUBSTITUTS_RCCM = ("ong",)

RE_RCCM = re.compile(r"RB\s*/?\s*[A-Z]{2,4}\s*/?\s*\d{2}\s*[A-Z]\s*\d{3,6}")
RE_IFU = re.compile(r"\b\d{13}\b")
RE_NPI = re.compile(r"\b\d{14}\b")
RE_DATE = re.compile(r"\b(\d{2})[-/.](\d{2})[-/.](\d{4})\b")
RE_EXPIRE = re.compile(
    r"EXP\w*\s*(?:LE)?\s*:?\s*(\d{1,2})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{4})")
RE_TEL = re.compile(r"T[EÉ]L[EÉ]?PHONE\s*:?\s*(\+?[\d ]{8,17}\d)")
MOTS_ENTETE = ("MINISTERE", "ECONOMIE", "FINANCE", "REPUBLIQUE", "DIRECTION", "GENERALE",
               "IMPOT", "ATTESTATION", "IDENTIFIANT", "REGISTRE", "COMMERCE", "GREFFE",
               "TRIBUNAL", "CERTIFICAT", "BENIN", "ETAT", "AGENCE")
CHAMPS_NOM = {
    # \bNOM\b : ne pas capter le « NOM » de « DENOMINATION » ; « PATRONYMIQUE » est souvent
    # mal lu par l'OCR (« FATRONYMIQUE ») d'où la terminaison seule
    "nom": r"\bNOM\b\s*(?:[A-Z]{0,4}RON\s?YMIQUE)?\s*[:.]?\s*([A-ZÉÈÀÙÇ' -]{2,40})",
    "prenoms": r"\bPRENOMS?\b\s*(?:\(\s*S\s*\))?\s*[:.]?\s*([A-ZÉÈÀÙÇ' -]{2,60})",
    "enseigne": r"ENSEIGNE\s*(?:COMMERCIALE)?\s*[:.]?\s*([A-Z0-9ÉÈÀÙÇ'& -]{3,60})",
    "nom_commercial": r"NOM\s+COMMERC\w*\s*[:.]?\s*([A-Z0-9ÉÈÀÙÇ'& -]{3,60})",
}

_MOTEUR = None


def _moteur():
    """Moteur PP-OCR, chargé une fois par processus."""
    global _MOTEUR
    if _MOTEUR is None:
        from rapidocr import RapidOCR
        _MOTEUR = RapidOCR(params={
            "EngineConfig.onnxruntime.intra_op_num_threads": THREADS_OCR,
            "Global.max_side_len": 2400})
        for nom in list(logging.root.manager.loggerDict):
            if "rapidocr" in nom.lower():
                logging.getLogger(nom).setLevel(logging.ERROR)
    return _MOTEUR


def _sans_accents(texte: str) -> str:
    n = unicodedata.normalize("NFKD", texte)
    return "".join(c for c in n if not unicodedata.combining(c)).upper()


def _image(page, cote: int = 1600, angle: int = 0, haut: float = 1.0) -> np.ndarray:
    """Page rendue en RGB (plus grand côté = cote), limitée à son haut si haut < 1, redressée
    de « angle » degrés (sens horaire)."""
    zoom = cote / max(page.rect.width, page.rect.height)
    clip = None
    if haut < 1.0:
        r = page.rect
        clip = fitz.Rect(r.x0, r.y0, r.x1, r.y0 + r.height * haut)
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=clip, colorspace=fitz.csRGB)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)
    return np.ascontiguousarray(np.rot90(img, k=-(angle // 90))) if angle else img


def _ocr(img: np.ndarray) -> tuple[str, float, float]:
    """(texte, confiance moyenne 0-100, part des lignes de texte verticales)."""
    res = _moteur()(img)
    txts, scores = list(res.txts or ()), list(res.scores or ())
    verticales = 0.0
    if res.boxes is not None and len(res.boxes):
        b = np.asarray(res.boxes)
        larg = np.linalg.norm(b[:, 1] - b[:, 0], axis=1)
        haut = np.linalg.norm(b[:, 3] - b[:, 0], axis=1)
        verticales = float(np.mean(haut > 1.5 * larg))
    conf = 100 * sum(scores) / len(scores) if scores else 0.0
    return " ".join(txts), conf, verticales


def _type_depuis_texte(texte: str) -> str:
    compact = texte.replace(" ", "")
    for piece, marqueurs in MARQUEURS.items():
        if any(m in texte or m.replace(" ", "") in compact for m in marqueurs):
            return piece
    return "inconnu"


# pages jamais envoyées à Claude telles quelles : leur orientation importe peu
SANS_REDRESSEMENT = ("contrat", "rccm", "ifu", "ong")


def _lire_redresse(page) -> tuple[str, str, float, int, bool]:
    """(type, texte, confiance, angle, page_entiere) ; lit d'abord l'en-tête seul (rapide), puis
    la page entière, redressée si elle est couchée."""
    texte, conf, verticales = _ocr(_image(page, 1100, haut=0.32))
    trouve = _type_depuis_texte(_sans_accents(texte))
    if trouve != "inconnu" and verticales <= 0.5:
        return trouve, texte, conf, 0, False
    img = _image(page, 1400)
    texte, conf, verticales = _ocr(img)
    trouve = _type_depuis_texte(_sans_accents(texte))
    angle = 0
    if trouve not in SANS_REDRESSEMENT and (verticales > 0.5 or trouve == "inconnu"):
        # page couchée : on garde le quart de tour qui donne le plus de texte fiable
        meilleur = (conf * len(texte), texte, conf, 0)
        for a in (90, 270):
            t, c, _ = _ocr(np.ascontiguousarray(np.rot90(img, k=-(a // 90))))
            if c * len(t) > meilleur[0]:
                meilleur = (c * len(t), t, c, a)
        _, texte, conf, angle = meilleur
        trouve = _type_depuis_texte(_sans_accents(texte))
    return trouve, texte, conf, angle, True


def _identifier(doc) -> tuple[dict, dict, dict]:
    """({page: type}, {page: angle}, {page: (texte, confiance)})."""
    types, angles, textes = {}, {}, {}
    trouvees = set()
    for i, page in enumerate(doc, 1):
        # les quatre pièces sont trouvées : les pages suivantes sont le contrat (ou un
        # folio 2 sans intérêt) ; on ne les lit pas, c'est le texte le plus long du dossier
        if {"fiche", "piece_identite"} <= trouvees and (
                trouvees & {"rccm", *SUBSTITUTS_RCCM}) and (trouvees & {"ifu", *SUBSTITUTS_IFU}):
            types[i], angles[i] = "contrat", 0
            continue
        types[i], texte, conf, angles[i], entiere = _lire_redresse(page)
        if entiere:
            textes[i] = (texte, conf)
        trouvees.add(types[i])

    # une page inconnue qui suit la première page du RCCM en est le folio 2/2 ; seul le RCCM
    # s'étend sur deux pages (l'IFU tient sur une : la page suivante peut être la pièce
    # d'identité, scannée de travers)
    for i in range(2, len(types) + 1):
        if types[i] == "inconnu" and types[i - 1] == "rccm" and types.get(i - 2) != "rccm":
            types[i] = "rccm"
    # pages inconnues après le début du contrat
    debut = min((i for i, t in types.items() if t == "contrat"), default=None)
    if debut:
        for i in range(debut + 1, len(types) + 1):
            if types[i] == "inconnu":
                types[i] = "contrat"
    return types, angles, textes


def identifier_pages(doc) -> tuple[dict, dict]:
    """({page: type}, {page: angle de rotation})."""
    types, angles, _ = _identifier(doc)
    return types, angles


def lire_page(page, largeur: int = 2200, angle: int = 0) -> tuple[str, float]:
    """Texte complet d'une page + confiance moyenne de l'OCR (0-100)."""
    texte, conf, _ = _ocr(_image(page, largeur, angle))
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
        champs["date_expiration"] = f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    if m := RE_TEL.search(up):
        champs["telephone"] = re.sub(r"\s+", "", m.group(1))
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
        types, angles, lus = _identifier(doc)
        res["pages"] = types
        res["rotations"] = {i: a for i, a in angles.items() if a}
        for piece in ("rccm", "ifu", "apiex", "cnss", "piece_identite"):
            pages = [i for i, t in types.items() if t == piece]
            if not pages:
                continue
            textes, confs = [], []
            for i in pages:
                if piece in ("piece_identite", "apiex", "cnss"):
                    # petite carte sur une grande page : relue en plus haute définition
                    texte, conf = lire_page(doc[i - 1], angle=angles[i])
                else:
                    # RCCM / IFU : le QR officiel ou Claude les lit ; texte de l'identification
                    texte, conf = lus.get(i, ("", 0.0))
                textes.append(texte)
                confs.append(conf)
            champs = extraire(" ".join(textes))
            if piece == "cnss" and "ifu" not in champs and "npi" in champs:
                champs["ifu"] = champs["npi"][:13]  # matricule employeur = IFU + 1 chiffre
            res["pieces"][piece] = {"pages": pages, "confiance": round(sum(confs) / len(confs), 1)}
            res["champs"][piece] = champs
            # champs attendus par pièce : ce qui manque part chez Claude
            attendus = {"rccm": ["rccm", "nom"], "ifu": ["ifu"], "apiex": ["ifu", "rccm"],
                        "cnss": ["ifu"], "piece_identite": ["date_expiration", "nom"]}[piece]
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
                if piece == "ifu" and [i for i, t in types.items() if t in SUBSTITUTS_IFU]:
                    manque = "ifu (carte professionnelle ou CNSS trouvée)"
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
