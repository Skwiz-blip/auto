import argparse
import asyncio
import base64
import csv
import json
import os
import random
import re
import statistics
import time
import unicodedata
from datetime import date, datetime
from difflib import SequenceMatcher
from pathlib import Path

import anthropic
import fitz  # PyMuPDF

ROOT = Path(__file__).parent
MODEL = "claude-sonnet-4-5"
PRICE_IN, PRICE_OUT = 3.00 / 1e6, 15.00 / 1e6  # $/token, Claude Sonnet 4.5

PIECES = ["fiche", "rccm", "ifu", "piece_identite"]
PIECE_LABELS = {
    "fiche": "Fiche de création marchand",
    "rccm": "RCCM",
    "ifu": "IFU",
    "piece_identite": "CNI / CIP / Passeport",
}
FICHE_OBLIGATOIRES = {
    "nom_structure": "nom de la structure",
    "representant": "représentant légal",
    "ville": "ville",
}
# Champs dont un doute de lecture justifie un contrôle humain (les autres, comme le GPS
# ou le téléphone, ne sont plus contrôlés : un doute dessus ne change rien à la décision)
CHAMPS_CONTROLES = ("nom", "prenom", "expiration", "numero", "rccm", "ifu",
                    "photo", "structure", "representant")
# mêmes champs, désignés par leur nom exact (« section.champ » dans les doutes du modèle)
CHAMPS_DOUTE = {"nom", "prenoms", "date_expiration", "numero", "rccm", "photo_lisible",
                "nom_structure", "representant"}

 
def _obj(props: dict) -> dict:
    return {"type": "object", "properties": props,
            "required": list(props), "additionalProperties": False}

S, B = {"type": "string"}, {"type": "boolean"}
STR_LIST = {"type": "array", "items": S}

SCHEMA = _obj({
    "pieces": _obj({p: _obj({
        "presente": B, "lisible": B,
        "pages": {"type": "array", "items": {"type": "integer"}},
        "remarque": S,
    }) for p in PIECES}),
    "fiche": _obj({k: S for k in [
        "nom_structure", "secteur", "representant", "telephone",
        "carre_maison_ilot", "quartier", "ville", "departement",
        "gps_longitude", "gps_latitude", "taux_reversement",
        "commercial", "date_demande"]} | {k: B for k in [
        "case_rccm", "case_ifu", "case_piece_identite", "case_contrat",
        "signature_commercial", "autorisee_par_signee"]}),
    "rccm": _obj({k: S for k in [
        "numero", "nom", "prenoms", "nationalite", "date_naissance",
        "lieu_naissance", "enseigne", "nom_commercial", "adresse",
        "activite", "date_debut_exploitation", "date_delivrance",
        "telephone"]} | {"cachet_greffe": B}),
    "ifu": _obj({k: S for k in [
        "numero", "nom", "prenoms", "nom_etablissement", "categorie",
        "adresse", "rccm", "regime_fiscal", "centre_impots",
        "date_emission", "telephone", "document_substitut"]} | {"cachet_dgi": B}),
    "piece_identite": _obj({k: S for k in [
        "type", "numero", "nom", "prenoms", "date_naissance",
        "lieu_naissance", "nationalite", "date_expiration"]} | {
        "photo_lisible": B}),
    "analyse": _obj({
        "plusieurs_points_de_vente": B,
        "champs_incertains": STR_LIST,
        "observations": STR_LIST,
    }),
})

SYSTEM = """Tu contrôles des dossiers marchands Celtiis Cash (SBIN / KIK EXPERIENCE) au Bénin.
Chaque dossier est un PDF scanné. Les pièces à contrôler sont :
- fiche : "FICHE DE CREATION MARCHAND" (en-têtes SBiN + Celtiis)
- rccm : "EXTRAIT DU REGISTRE DU COMMERCE ET DU CREDIT MOBILIER"
- ifu : "ATTESTATION D'IMMATRICULATION IFU" (Ministère des Finances / DGI)
- piece_identite : CNI, CIP ("CERTIFICAT D'IDENTIFICATION PERSONNELLE") ou passeport
Le dossier contient aussi le contrat Celtiis Cash : il n'est pas à contrôler, sers-t'en
seulement pour savoir s'il y a plusieurs points marchands (Annexe 1).

Identifie sur quelles pages (numérotées à partir de 1) se trouve chaque pièce, puis extrais
les champs demandés en recopiant exactement ce qui est écrit, sans corriger l'orthographe :
les noms et prénoms seront comparés entre les pièces, une faute de frappe sur une pièce
doit rester visible.
Règles de saisie :
- Champ absent, vide ou illisible : chaîne vide "". N'invente jamais une valeur et ne
  recopie jamais un nom d'une pièce vers une autre.
- rccm.nom / ifu.nom / piece_identite.nom : nom de famille (patronymique) ; "prenoms" : tous
  les prénoms, dans l'ordre écrit.
- ifu.nom / ifu.prenoms : nom et prénoms de la personne physique titulaire tels qu'imprimés
  sur l'attestation IFU (souvent le nom commercial est dans nom_etablissement ; si le nom de
  la personne n'apparaît pas sur l'IFU, laisse "").
- Dates au format AAAA-MM-JJ quand elles sont lisibles, sinon "".
- Coordonnées GPS : recopie les nombres tels qu'écrits (point décimal).
- Booléens de signature / cachet : true seulement si l'élément est visible sur le scan.
- piece_identite.photo_lisible : true seulement si la photo est présente et que le visage est
  reconnaissable (pas noirci, pas effacé, pas coupé).
- ifu : seulement une attestation d'immatriculation IFU de la DGI. Un autre document qui
  mentionne un numéro IFU (carte professionnelle APIEx, etc.) n'est pas une attestation IFU :
  mets alors pieces.ifu.presente = false, indique ce document dans ifu.document_substitut
  (ex : "Carte professionnelle APIEx") et remplis les champs ifu (numero, nom, prenoms, rccm…)
  à partir de ce document. S'il n'y a ni attestation ni substitut, document_substitut = "".
- "lisible" = false si la pièce est floue, coupée ou trop sombre au point de gêner la lecture
  d'un champ important ; précise le problème dans "remarque".
- Dans les numéros RCCM, le code du greffe désigne la ville (ex : ABC = Abomey-Calavi,
  COT = Cotonou).
- plusieurs_points_de_vente : true si le dossier indique plus d'un point marchand.
- champs_incertains : liste des champs que tu n'as pas pu lire avec certitude (écriture peu
  lisible, chiffre ambigu, zone masquée ou coupée). Mieux vaut signaler un doute que deviner :
  ces champs seront revus par un humain. Exemple : "piece_identite.date_expiration".
- observations : anomalies notables sur les 4 pièces, en phrases courtes.

Réponds uniquement avec un objet JSON (sans texte autour) ayant exactement cette structure :
"""


def _template(schema: dict):
    """Modèle vide dérivé du schéma : "" pour les textes, false, [] ou [0] pour les listes."""
    t = schema["type"]
    if t == "object":
        return {k: _template(v) for k, v in schema["properties"].items()}
    if t == "array":
        return [0] if schema["items"]["type"] == "integer" else []
    return {"string": "", "boolean": False}[t]


def conform(value, schema: dict):
    """Ramène la réponse du modèle au schéma : champs manquants complétés, types corrigés."""
    t = schema["type"]
    if t == "object":
        value = value if isinstance(value, dict) else {}
        return {k: conform(value.get(k), sub) for k, sub in schema["properties"].items()}
    if t == "array":
        if not isinstance(value, list):
            return []
        return [conform(v, schema["items"]) for v in value]
    if t == "boolean":
        return value is True or (isinstance(value, str) and value.strip().lower() in ("true", "oui"))
    if t == "integer":
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0
    return "" if value is None else str(value)


def parse_json(text: str) -> dict:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("aucun JSON dans la réponse")
    return conform(json.loads(text[start:end + 1]), SCHEMA)


SYSTEM += json.dumps(_template(SCHEMA), ensure_ascii=False, indent=1)

 
def render_pages(pdf_path: Path, max_side: int, quality: int) -> list[bytes]:
    """Rend chaque page en JPEG, en mémoire uniquement (rien n'est écrit sur disque)."""
    images = []
    with fitz.open(pdf_path) as doc:
        for page in doc:
            zoom = max_side / max(page.rect.width, page.rect.height)
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace=fitz.csRGB)
            images.append(pix.tobytes("jpeg", jpg_quality=quality))
    return images


def build_content(images: list[bytes]) -> list[dict]:
    content = []
    for i, img in enumerate(images, 1):
        content.append({"type": "text", "text": f"Page {i}"})
        content.append({"type": "image", "source": {
            "type": "base64", "media_type": "image/jpeg",
            "data": base64.standard_b64encode(img).decode()}})
    content.append({"type": "text", "text": "Analyse ce dossier et renvoie le JSON demandé."})
    return content
 

def _norm(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def _date(value: str):
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _name_tokens(*parts: str) -> list[str]:
    text = unicodedata.normalize("NFKD", " ".join(parts))
    text = "".join(c for c in text if not unicodedata.combining(c)).upper()
    return re.findall(r"[A-Z]+", text)


def compare_names(ref: list[str], other: list[str]) -> str:
    """'identique', 'proche' (orthographe voisine ou prénom en moins) ou 'different'.

    L'ordre nom/prénoms est ignoré. Chaque mot de la liste la plus courte doit
    correspondre à un mot distinct de l'autre liste (similarité >= 0,8).
    """
    if sorted(ref) == sorted(other):
        return "identique"
    short, long_ = sorted([ref, other], key=len)
    remaining = list(long_)

    def score(word, w):  # une initiale ("B.") correspond à un prénom qui commence par B
        if len(word) == 1 or len(w) == 1:
            return 1.0 if word[0] == w[0] else 0.0
        return SequenceMatcher(None, word, w).ratio()

    for word in short:
        best = max(remaining, key=lambda w: score(word, w), default="")
        if not best or score(word, best) < 0.8:
            return "different"
        remaining.remove(best)
    return "proche"


# Motifs phares de rejet (libellés du tableau de suivi KIK)
M_PHOTO = "Photo illisible sur la pièce"
M_NOM_PDF = "Pdf mal nommé"
M_ID_EXPIRE = "Id expiré"
M_FLOUE = "Pièce floue"
M_RCCM_ILLISIBLE = "RCCM illisible"
M_RCCM_NON_CONFORME = "RCCM non conforme"
M_INCOMPLET = "Dossier incomplet"
M_IFU_NON_CONFORME = "IFU non conforme"


def _key(value: str) -> str:
    """Lettres et chiffres uniquement, sans accents : 'Divine Miséricorde !' -> 'DIVINEMISERICORDE'."""
    text = unicodedata.normalize("NFKD", value)
    return re.sub(r"[^A-Z0-9]", "", "".join(c for c in text if not unicodedata.combining(c)).upper())


def _rccm(value: str) -> str:
    """Numéro RCCM canonique : 'COTONOU N° RB/ABC/24 A 108577 du 13-06-2024' -> 'RBABC24A108577'."""
    m = re.search(r"RB\s*/?\s*[A-Z]{2,4}\s*/?\s*\d{2}\s*[A-Z]\s*\d+", value.upper())
    return re.sub(r"[^A-Z0-9]", "", m.group()) if m else _norm(value)


FORMES_JURIDIQUES = r"^(ETS|ETABLISSEMENTS?|STE|SOCIETE|SARL|SARLU|SA|SAS|GIE)"
# suffixes de copie : "-1(2)", "(1)", " - Copie", "- Copie (2)"
COPIE = re.compile(r"\s*-\s*\d+\s*(\(\d+\))?\s*$|\s*\(\d+\)\s*$|\s*-?\s*\bCOPIE\b.*$", re.IGNORECASE)


def apply_rules(d: dict, date_traitement: date, nom_fichier: str,
                sources_officielles: set[str] = frozenset({"rccm", "ifu"}),
                photo_bloquante: bool = True) -> dict:
    """sources_officielles : pièces dont les valeurs viennent d'une source sûre (QR de l'État).
    Un écart de nom lu autrement ne rejette pas : il part en contrôle humain."""
    bloquants, vigilance, particuliers = [], [], []
    pieces, fiche, rccm, ifu, pid, analyse = (
        d["pieces"], d["fiche"], d["rccm"], d["ifu"], d["piece_identite"], d["analyse"])

    def rejet(motif: str, detail: str = ""):
        bloquants.append({"motif": motif, "detail": detail})

    # Dossier incomplet / pièce floue / RCCM illisible
    # carte APIEx (ou autre document portant le n° IFU) à la place de l'attestation : décision humaine
    substitut_ifu = ifu["document_substitut"].strip() or (
        "Carte professionnelle APIEx" if "APIEX" in pieces["ifu"]["remarque"].upper() else "")
    for p in PIECES:
        if p == "ifu" and not pieces[p]["presente"] and substitut_ifu:
            vigilance.append(f"{substitut_ifu} à la place de l'attestation IFU")
        elif not pieces[p]["presente"]:
            rejet(M_INCOMPLET, f"{PIECE_LABELS[p]} absent(e)")
        elif not pieces[p]["lisible"]:
            detail = f"{PIECE_LABELS[p]} : {pieces[p]['remarque']}".rstrip(" :")
            if p == "fiche":
                # la fiche est manuscrite : « peu lisible » est courant et ne bloque pas
                vigilance.append(f"Fiche difficile à lire : {pieces[p]['remarque']}")
            elif p in sources_officielles:
                pass  # le document officiel a été récupéré par QR : la qualité du scan importe peu
            elif p == "piece_identite" and (not photo_bloquante or (
                    pid["nom"] and _date(pid["date_expiration"]))):
                # jugement visuel d'une seule lecture, ou champs essentiels bien lus : un humain
                # confirme avant tout rejet pour « Pièce floue »
                # jugée floue, mais nom et date d'expiration ont bien été lus : pas un rejet
                vigilance.append(f"Pièce d'identité de qualité médiocre ({pieces[p]['remarque'][:80]})"
                                 " — champs essentiels lus, à confirmer")
            else:
                rejet(M_RCCM_ILLISIBLE if p == "rccm" else M_FLOUE, detail)
    # (vérifié plus bas : sans objet si le registre officiel confirme déjà l'identité)
    fiche_vides = []
    if pieces["fiche"]["presente"]:
        fiche_vides = [label for k, label in FICHE_OBLIGATOIRES.items() if not fiche[k].strip()]

    # Pdf mal nommé : le fichier doit porter le nom de la structure
    # (forme juridique ETS/SARL… ignorée ; une lettre d'écart = vigilance ; copie "-1(2)" = rejet)
    def sans_forme(v):
        return re.sub(FORMES_JURIDIQUES, "", _key(v))

    # Les pièces imprimées font foi : le nom manuscrit de la fiche, souvent mal lu,
    # ne peut jamais provoquer un rejet à lui seul.
    imprimes = {sans_forme(v) for v in (rccm["enseigne"], rccm["nom_commercial"],
                                        ifu["nom_etablissement"]) if sans_forme(v)}
    manuscrit = sans_forme(fiche["nom_structure"])
    attendu = rccm["enseigne"] or ifu["nom_etablissement"] or fiche["nom_structure"]
    fichier = sans_forme(COPIE.sub("", nom_fichier))
    if COPIE.search(nom_fichier):
        vigilance.append(f"Doublon possible : fichier « {nom_fichier} » nommé comme une copie")
    if imprimes and fichier not in imprimes:
        proche = max(SequenceMatcher(None, fichier, n).ratio() for n in imprimes)
        if proche >= 0.9:
            # une ou deux lettres d'écart : c'est souvent le registre qui porte la faute
            # (« MISERICODE », « PIERRROS ») ; on le signale sans bloquer
            particuliers.append(f"Nom du PDF « {nom_fichier} » proche de l'enseigne "
                                f"officielle « {attendu} »")
        else:
            rejet(M_NOM_PDF, f"fichier « {nom_fichier} », structure « {attendu} »")
    elif not imprimes and manuscrit and fichier != manuscrit:
        vigilance.append(f"Nom du PDF « {nom_fichier} » différent du nom écrit sur la fiche "
                         f"« {fiche['nom_structure']} » (aucun nom imprimé pour trancher)")

    # Id expiré / photo illisible
    if pieces["piece_identite"]["presente"]:
        exp = _date(pid["date_expiration"])
        if exp is None:
            vigilance.append("Date d'expiration de la pièce d'identité illisible")
        elif exp < date_traitement:
            # règle KIK : tant que la date n'est pas passée, la pièce est valide
            rejet(M_ID_EXPIRE, f"{pid['type'] or 'pièce'} expirée le {exp:%d/%m/%Y}")
        if not pid["photo_lisible"]:
            if photo_bloquante:
                rejet(M_PHOTO, "photo absente ou visage non identifiable")
            else:
                # jugement visuel d'une seule lecture : un humain confirme avant tout rejet
                vigilance.append("Photo de la pièce d'identité jugée illisible (à confirmer "
                                 "avant rejet pour « Photo illisible sur la pièce »)")

    # RCCM non conforme : numéro absent ou nom différent de la pièce d'identité
    if pieces["rccm"]["presente"] and pieces["rccm"]["lisible"]:
        if not _norm(rccm["numero"]) and _rccm(ifu["rccm"]):
            rccm["numero"] = ifu["rccm"]  # repris de l'IFU, qui cite le même registre
        if not _norm(rccm["numero"]):
            if "rccm" in sources_officielles:
                rejet(M_RCCM_NON_CONFORME, "numéro RCCM absent du registre officiel")
            else:
                vigilance.append("Numéro RCCM non lu sur le document (à vérifier)")
        elif not re.search(r"RB/[A-Z]{2,4}/\d{2}[A-Z]\d+", rccm["numero"].upper().replace(" ", "")):
            vigilance.append(f"Format du numéro RCCM inhabituel ({rccm['numero']})")

    ref = _name_tokens(pid["nom"], pid["prenoms"])
    ref_txt = f"{pid['nom']} {pid['prenoms']}".strip()
    # vrai quand un registre officiel (QR) confirme que le titulaire est la personne de la CIP :
    # la fiche manuscrite n'a alors plus rien à prouver sur l'identité
    identite_officielle = False
    if pieces["piece_identite"]["presente"] and not ref:
        vigilance.append("Nom illisible sur la pièce d'identité : comparaison impossible")
    elif ref:
        # l'IFU se rattache au RCCM par le numéro RCCM qu'il porte, ou par le même établissement
        etablissements = {_key(v) for v in (rccm["enseigne"], rccm["nom_commercial"]) if _key(v)}
        rccm_lie = ((bool(_rccm(ifu["rccm"])) and _rccm(rccm["numero"]) == _rccm(ifu["rccm"]))
                    or _key(ifu["nom_etablissement"]) in etablissements)
        for cle, label, motif, present, nom, prenoms in [
            ("rccm", "le RCCM", M_RCCM_NON_CONFORME, pieces["rccm"]["presente"],
             rccm["nom"], rccm["prenoms"]),
            ("ifu", "l'IFU", M_IFU_NON_CONFORME, pieces["ifu"]["presente"] or bool(substitut_ifu),
             ifu["nom"], ifu["prenoms"]),
        ]:
            if not present:
                continue
            other = _name_tokens(nom, prenoms)
            other_txt = f"{nom} {prenoms}".strip()
            # le modèle met parfois le nom de l'entreprise à la place de celui de la personne
            if other and _key(other_txt) in {_key(v) for v in (
                    fiche["nom_structure"], rccm["enseigne"], rccm["nom_commercial"],
                    ifu["nom_etablissement"]) if _key(v)}:
                other = []
            if not other:
                if label == "l'IFU" and rccm_lie:
                    continue  # IFU sans nom de personne : rattaché via le numéro RCCM
                vigilance.append(f"Nom illisible ou absent sur {label}")
                continue
            verdict = compare_names(ref, other)
            if verdict in ("identique", "proche") and cle in sources_officielles:
                identite_officielle = True
            if verdict == "different" and cle not in sources_officielles:
                vigilance.append(f"Nom sur {label} ({other_txt}) différent de la pièce "
                                 f"d'identité ({ref_txt}) — lecture non confirmée par le "
                                 f"registre officiel")
            elif verdict == "different":
                rejet(motif, f"nom sur {label} ({other_txt}) différent de la "
                             f"pièce d'identité ({ref_txt})")
            elif verdict == "proche":
                vigilance.append(f"Nom sur {label} ({other_txt}) légèrement différent de la "
                                 f"pièce d'identité ({ref_txt})")
        rep = _name_tokens(fiche["representant"])
        # « SEGLAPIERRE » et « SEGLA PIERRE » sont le même nom : on compare aussi sans espaces
        meme_nom = _key(fiche["representant"]) == _key(ref_txt)
        if rep and not meme_nom and compare_names(ref, rep) == "different":
            message = (f"Représentant de la fiche ({fiche['representant']}) différent "
                       f"de la pièce d'identité ({ref_txt})")
            (particuliers if identite_officielle else vigilance).append(message)

    if fiche_vides:
        message = "Fiche incomplète : " + ", ".join(fiche_vides)
        (particuliers if identite_officielle else vigilance).append(message)

    # Cohérence des numéros (seulement entre deux vrais numéros RCCM : le modèle range parfois
    # un nom d'entreprise dans le champ « RCCM » de l'IFU)
    motif_rccm = re.compile(r"RB\s*/?\s*[A-Z]{2,4}\s*/?\s*\d{2}\s*[A-Z]\s*\d+")
    if (motif_rccm.search(rccm["numero"].upper()) and motif_rccm.search(ifu["rccm"].upper())
            and _rccm(rccm["numero"]) != _rccm(ifu["rccm"])):
        vigilance.append(f"Numéro RCCM différent entre RCCM ({rccm['numero']}) "
                         f"et IFU ({ifu['rccm']})")
    # GPS, téléphone, signature du commercial et cachet du greffe : non contrôlés (décision KIK)

    # Doutes du modèle, mais seulement sur les champs que les règles utilisent
    def doute_utile(c: str) -> bool:
        """Un doute compte s'il porte sur un champ contrôlé et non déjà fourni par le QR."""
        m = re.match(r"\s*([a-z_]+)\.([a-z_]+)", c.lower())
        if not m:  # doute rédigé en clair (ex. date d'expiration non confirmée)
            return any(mot in c.lower() for mot in CHAMPS_CONTROLES)
        section, champ = m.groups()
        if section in sources_officielles:
            return False  # le registre officiel fait foi sur cette pièce
        if section == "piece_identite" and champ == "numero":
            return False  # le numéro de la CIP n'entre dans aucune règle
        return champ in CHAMPS_DOUTE

    doutes = [c for c in analyse["champs_incertains"] if doute_utile(c)]
    if identite_officielle:
        # l'écriture de la fiche n'est plus décisive : l'identité est établie par le registre
        manuscrits = [c for c in doutes if c.lower().startswith("fiche.")]
        doutes = [c for c in doutes if c not in manuscrits]
        if manuscrits:
            particuliers.append("Fiche manuscrite partiellement illisible (identité confirmée "
                                "par le registre officiel) : " + ", ".join(manuscrits))
    if doutes:
        vigilance.append("Champs non lus avec certitude : " + ", ".join(doutes))

    # Points de vente multiples
    if analyse["plusieurs_points_de_vente"]:
        particuliers.append("Points de vente multiples")

    statut = "REJETÉ" if bloquants else "À VÉRIFIER" if vigilance else "VALIDÉ"
    motifs = list(dict.fromkeys(b["motif"] for b in bloquants))
    return {"statut": statut, "motifs_rejet": motifs, "bloquants": bloquants,
            "vigilance": vigilance, "particuliers": particuliers}


# --------------------------------------------------------------------------
# Traitement d'un dossier
# --------------------------------------------------------------------------

async def process(client, pdf: Path, args, sem, date_traitement, log) -> dict:
    row = {"dossier": pdf.stem.strip(), "pages": 0, "mo_images": 0.0,
           "t_rendu_s": 0.0, "t_api_s": 0.0, "t_total_s": 0.0,
           "tokens_in": 0, "tokens_out": 0, "cout_usd": 0.0,
           "statut": "ERREUR", "motifs_rejet": "", "detail": "", "rccm": "", "erreur": ""}
    async with sem:
        t0 = time.perf_counter()
        try:
            if args.pdf_direct:
                # PDF envoyé tel quel : la conversion des pages se fait chez Anthropic
                data_pdf = pdf.read_bytes()
                with fitz.open(pdf) as doc:
                    row["pages"] = doc.page_count
                row["mo_images"] = round(len(data_pdf) / 1e6, 2)
                content = [
                    {"type": "document", "source": {
                        "type": "base64", "media_type": "application/pdf",
                        "data": base64.standard_b64encode(data_pdf).decode()}},
                    {"type": "text", "text": "Analyse ce dossier et renvoie le JSON demandé."},
                ]
            else:
                images = await asyncio.to_thread(render_pages, pdf, args.max_side, args.quality)
                row["pages"] = len(images)
                row["mo_images"] = round(sum(map(len, images)) / 1e6, 2)
                content = build_content(images)
            t1 = time.perf_counter()
            row["t_rendu_s"] = round(t1 - t0, 2)

            response = await client.beta.messages.create(
                model=MODEL,
                max_tokens=16000,
                system=SYSTEM,
                messages=[{"role": "user", "content": content}],
                output_config={"effort": args.effort},
                betas=["server-side-fallback-2026-07-01"],
                extra_body={"fallbacks": "default"},
            )
            row["t_api_s"] = round(time.perf_counter() - t1, 2)
            u = response.usage
            row["tokens_in"] = u.input_tokens
            row["tokens_out"] = u.output_tokens
            row["cout_usd"] = round(u.input_tokens * PRICE_IN + u.output_tokens * PRICE_OUT, 4)

            if response.stop_reason == "refusal":
                raise RuntimeError(f"refus du modèle ({response.stop_details})")
            if response.stop_reason == "max_tokens":
                raise RuntimeError("réponse tronquée (max_tokens)")
            text = next(b.text for b in response.content if b.type == "text")
            data = parse_json(text)
            decision = apply_rules(data, date_traitement, row["dossier"])
            row["statut"] = decision["statut"]
            row["rccm"] = data["rccm"]["numero"] or data["ifu"]["rccm"]
            row["motifs_rejet"] = " | ".join(decision["motifs_rejet"])
            row["detail"] = " | ".join(
                [f"{b['motif']} : {b['detail']}" for b in decision["bloquants"]]
                + decision["vigilance"])
            log.write(json.dumps({"dossier": row["dossier"], "model": response.model,
                                  "request_id": response._request_id,
                                  "horodatage": datetime.now().isoformat(timespec="seconds"),
                                  "decision": decision, "extraction": data},
                                 ensure_ascii=False) + "\n")
        except (anthropic.APIStatusError, anthropic.APIConnectionError) as e:
            row["erreur"] = f"{type(e).__name__}: {e}"
        except Exception as e:  # un dossier en erreur ne bloque pas le lot
            row["erreur"] = f"{type(e).__name__}: {e}"
        row["t_total_s"] = round(time.perf_counter() - t0, 2)

    print(f"  {row['statut']:<11} {row['t_total_s']:>6.1f}s  {row['pages']:>2}p  "
          f"{row['tokens_in']:>6}in {row['tokens_out']:>5}out  {row['dossier']}"
          + (f"  !! {row['erreur'][:400]}" if row["erreur"] else ""), flush=True)
    return row


def marquer_doublons(rows: list[dict]) -> list[list[str]]:
    """Même n° RCCM dans plusieurs dossiers du lot : doublon ou points de vente multiples,
    à trancher par un humain. Les dossiers VALIDÉ concernés passent en À VÉRIFIER."""
    groupes = {}
    for r in rows:
        if _rccm(r["rccm"]):
            groupes.setdefault(_rccm(r["rccm"]), []).append(r)
    doublons = []
    for groupe in groupes.values():
        if len(groupe) < 2:
            continue
        noms = [r["dossier"] for r in groupe]
        doublons.append(noms)
        for r in groupe:
            autres = ", ".join(n for n in noms if n != r["dossier"])
            if r["statut"] == "VALIDÉ":
                r["statut"] = "À VÉRIFIER"
            r["detail"] = " | ".join(filter(None, [
                r["detail"], f"Doublon possible : même RCCM ({r['rccm']}) que {autres}"]))
    return doublons


def load_api_key():
    if os.environ.get("ANTHROPIC_API_KEY"):
        return None  # le SDK la lit lui-même
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "ANTHROPIC_API_KEY":
                return value.strip().strip('"').strip("'")
    raise SystemExit("Clé API introuvable : définir ANTHROPIC_API_KEY ou créer AUTO/.env")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dossier", default=str(ROOT / "Dossier test"))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only", nargs="*", default=[])
    ap.add_argument("--concurrency", type=int, default=5)
    ap.add_argument("--effort", default="medium", choices=["low", "medium", "high", "xhigh", "max"])
    ap.add_argument("--max-side", type=int, default=1800, help="côté max des pages en pixels")
    ap.add_argument("--quality", type=int, default=80)
    ap.add_argument("--pdf-direct", action="store_true",
                    help="envoyer le PDF tel quel au lieu des pages converties en JPEG")
    args = ap.parse_args()

    pdfs = sorted(Path(args.dossier).glob("*.pdf"))
    if args.only:
        pdfs = [p for p in pdfs if p.stem.strip() in args.only]
    if args.limit:
        pdfs = pdfs[:args.limit]

    out = ROOT / "sorties" / f"bench_{datetime.now():%Y%m%d_%H%M%S}"
    out.mkdir(parents=True)
    client = anthropic.AsyncAnthropic(api_key=load_api_key(), max_retries=4, timeout=600)
    sem = asyncio.Semaphore(args.concurrency)
    today = date.today()

    print(f"{len(pdfs)} dossier(s), concurrence {args.concurrency}, effort {args.effort}, "
          + ("PDF direct" if args.pdf_direct else f"pages {args.max_side}px") + f" -> {out}")
    t0 = time.perf_counter()
    with open(out / "resultats.jsonl", "w", encoding="utf-8") as log:
        rows = await asyncio.gather(*(process(client, p, args, sem, today, log) for p in pdfs))
    wall = time.perf_counter() - t0
    doublons = marquer_doublons(rows)

    with open(out / "temps.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]), delimiter=";")
        w.writeheader()
        w.writerows(rows)

    ok = [r for r in rows if not r["erreur"]]
    lines = [f"Dossiers : {len(rows)}  (réussis {len(ok)}, erreurs {len(rows) - len(ok)})",
             f"Durée réelle du lot : {wall / 60:.1f} min (concurrence {args.concurrency})"]
    if ok:
        tt = [r["t_total_s"] for r in ok]
        per = wall / len(rows)
        cout = sum(r["cout_usd"] for r in ok) / len(ok)
        lines += [
            f"Par dossier : moyenne {statistics.mean(tt):.1f}s, médiane {statistics.median(tt):.1f}s, "
            f"min {min(tt):.1f}s, max {max(tt):.1f}s",
            f"  dont rendu des pages {statistics.mean(r['t_rendu_s'] for r in ok):.1f}s, "
            f"appel API {statistics.mean(r['t_api_s'] for r in ok):.1f}s",
            f"Tokens moyens : {statistics.mean(r['tokens_in'] for r in ok):.0f} en entrée, "
            f"{statistics.mean(r['tokens_out'] for r in ok):.0f} en sortie",
            f"Coût moyen : {cout:.3f} $ / dossier",
            f"Projection 100 dossiers/nuit : ~{per * 100 / 60:.0f} min à cette concurrence, "
            f"{statistics.mean(tt) * 100 / 60:.0f} min en séquentiel, ~{cout * 100:.0f} $/nuit",
            f"Projection 3 000 dossiers/mois : ~{cout * 3000:.0f} $ "
            f"(~{cout * 1500:.0f} $ avec la Batch API à -50 %)",
            "Statuts : " + ", ".join(f"{s} {sum(r['statut'] == s for r in rows)}"
                                     for s in ["VALIDÉ", "À VÉRIFIER", "REJETÉ", "ERREUR"]),
        ]
        compte = {}
        for r in ok:
            for m in filter(None, r["motifs_rejet"].split(" | ")):
                compte[m] = compte.get(m, 0) + 1
        if compte:
            lines.append("Motifs de rejet : " + ", ".join(
                f"{m} {n}" for m, n in sorted(compte.items(), key=lambda x: -x[1])))
        for noms in doublons:
            lines.append("Doublon possible (même RCCM) : " + " / ".join(noms))
        sample = random.sample(ok, max(1, round(len(ok) * 0.05)))
        lines.append("Échantillon de contrôle (5 %) : " + ", ".join(r["dossier"] for r in sample))
    summary = "\n".join(lines)
    (out / "synthese.txt").write_text(summary, encoding="utf-8")
    print("\n" + summary)


if __name__ == "__main__":
    asyncio.run(main())


