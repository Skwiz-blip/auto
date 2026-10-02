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
from itertools import permutations
from pathlib import Path

import anthropic
import fitz  # PyMuPDF

import chemins
import referentiel

ROOT = chemins.DONNEES
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
    "telephone": "numéro personnel",
    "ville": "ville",
    "commercial": "« Demandé par » (commercial)",
}
# cases manuscrites qu'on ne « devine » pas : illisibles, elles font passer le dossier en
# À VÉRIFIER (elles servent aux contrôles ou au fichier SharePoint)
# (head et sous-comptes : jamais en doute, le chiffre écrit ou 1 / 0 par défaut ; commercial :
# rattaché à la liste officielle, contrôlé à part)
FICHE_A_LIRE = {
    "nom_structure": "nom de la structure", "representant": "représentant légal",
    "telephone": "numéro personnel", "secteur": "secteur d'activité", "ville": "ville",
    "quartier": "quartier", "departement": "département",
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
        "nombre_head", "nombre_sous_comptes",
        "commercial", "date_demande"]} | {k: B for k in [
        "case_rccm", "case_ifu", "case_piece_identite", "case_contrat",
        "signature_commercial", "autorisee_par_signee"]}),
    "rccm": _obj({k: S for k in [
        "numero", "nom", "prenoms", "nationalite", "date_naissance",
        "lieu_naissance", "enseigne", "nom_commercial", "adresse",
        "activite", "date_debut_exploitation", "date_delivrance",
        "telephone", "document_substitut", "reference_verification",
        # dates du registre officiel (QR), remplies par le programme, pas par Claude
        "registre_date_immatriculation", "registre_date_delivrance"]} | {"cachet_greffe": B}),
    "ifu": _obj({k: S for k in [
        "numero", "nom", "prenoms", "nom_etablissement", "categorie",
        "adresse", "rccm", "regime_fiscal", "centre_impots",
        "date_emission", "telephone", "document_substitut"]} | {"cachet_dgi": B}),
    "piece_identite": _obj({k: S for k in [
        "type", "numero", "nom", "prenoms", "date_naissance",
        "lieu_naissance", "nationalite", "date_expiration", "telephone"]} | {
        "photo_lisible": B}),
    "contrat": _obj({"present": B, "page_signature_trouvee": B, "signe_marchand": B,
                     "nom_signataire": S, "remarque": S}),
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
- piece_identite : toute pièce d'identité officielle : CIP ("CERTIFICAT D'IDENTIFICATION
  PERSONNELLE"), CNI, carte d'identité CEDEAO, passeport, permis de conduire, carte consulaire…
- contrat : contrat de paiement Celtiis Cash. On ne t'en montre en général que la page de
  signature (« FAIT EN TROIS (03) EXEMPLAIRES ORIGINAUX… POUR LA SBIN… POUR <marchand> ») :
  contrôle seulement la signature du marchand.

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
  sur l'attestation IFU (champs « Nom » / « Prénom(s) », ou « Contribuable » / « Nom prénom /
  Dénomination » qui contient nom ET prénoms : répartis-les, sans répéter un mot). Le nom
  commercial va dans nom_etablissement ; si l'IFU est celui d'une société (pas de nom de
  personne), laisse "".
- piece_identite.nom / prenoms : uniquement le titulaire (lignes « Nom » et « Prénom(s) »),
  JAMAIS les lignes « Père » ou « Mère » de la rubrique Filiation. La carte est souvent
  scannée de travers : repère bien les libellés.
- fiche (FICHE DE CREATION MARCHAND - CELTIIS CASH, manuscrite) : nom_structure = « Nom de la
  structure » ; secteur = « Secteur d'activité » ; representant = « Nom du représentant
  légal » ; telephone = « Numéro personnel » ; ville, quartier, departement = « Ville », « Quartier », « Département » ;
  nombre_head = « Nombre de Head » ; nombre_sous_comptes = « Nombre de sous comptes » (pour
  ces deux cases : le chiffre auquel l'écriture ressemble le plus, jamais « ? ») ;
  commercial = nom écrit après « Demandé par » ; date_demande = « Date et signature ».
  Recopie lettre par lettre ce qui est écrit, fautes comprises : le nom de la structure et
  celui du représentant seront comparés au RCCM, une faute du commercial doit rester visible.
  Ne devine JAMAIS : chaque lettre ou chiffre que tu ne lis pas avec certitude est remplacé
  par « ? » (ex. « AGO?A »), et la case est ajoutée à champs_incertains. Ne complète pas un
  mot d'après le sens ou d'après une autre pièce. Laisse "" uniquement si la case est
  vraiment vide sur le formulaire.
- rccm.telephone / piece_identite.telephone : numéro imprimé sur le RCCM (« Tel : ») et sur la
  CIP (« Numéro de téléphone »), tel qu'écrit.
- Dates au format AAAA-MM-JJ quand elles sont lisibles, sinon "". Exception :
  piece_identite.date_expiration se recopie TELLE QU'IMPRIMÉE, au format JJ/MM/AAAA (ex.
  « Expire le : 08/11/2026 » -> "08/11/2026") : le premier nombre est le jour.
- Booléens de signature / cachet : true seulement si l'élément est visible sur le scan.
- piece_identite.photo_lisible : true seulement si la photo est présente et que le visage est
  reconnaissable (pas noirci, pas effacé, pas coupé).
- ifu : seulement une attestation d'immatriculation IFU de la DGI. Un autre document qui
  mentionne un numéro IFU (carte professionnelle APIEx, etc.) n'est pas une attestation IFU :
  mets alors pieces.ifu.presente = false, indique ce document dans ifu.document_substitut
  (ex : "Carte professionnelle APIEx") et remplis les champs ifu (numero, nom, prenoms, rccm…)
  à partir de ce document. S'il n'y a ni attestation ni substitut, document_substitut = "".
  Même principe pour une attestation CNSS (« Numéro d'immatriculation employeur ») : son
  matricule commence par le n° IFU (13 premiers chiffres).
- rccm : seulement un extrait du registre du commerce. Une association / ONG fournit un
  récépissé de déclaration : mets pieces.rccm.presente = false et rccm.document_substitut =
  "Récépissé de déclaration d'ONG".
- rccm.cachet_greffe : true seulement si un cachet (tampon) du greffe est visible sur une page
  du RCCM. Les extraits électroniques du Ministère de la Justice n'ont pas de cachet : ils
  portent « Vérifiez la conformité de ce document » et un « Numéro de référence » (ex. « IHAP
  QXZL CEXR ZQKT ») à recopier dans rccm.reference_verification ("" s'il n'y en a pas).
  rccm.date_delivrance : date de « certifié conforme et délivré le … », souvent sur la 2e page
  du RCCM ; si cette ligne est restée vide, la date du tampon de certification du greffe
  (« Vu certifié par le Greffier en Chef… Ce 07 AOÛT 2026 ») ; "" si aucune date n'y figure.
- contrat : page_signature_trouvee = true si la page du bloc de signatures (« FAIT EN TROIS
  (03) EXEMPLAIRES ORIGINAUX… POUR LA SBIN… POUR <marchand> ») est fournie ; signe_marchand =
  true seulement si une signature manuscrite figure sous « POUR <marchand> » (la signature de
  la SBIN n'est pas exigée) ; nom_signataire = nom écrit sous cette signature. Le champ
  "present" est rempli par le programme : laisse-le à false.
- "lisible" = false si la pièce est floue, coupée ou trop sombre au point de gêner la lecture
  d'un champ important ; précise le problème dans "remarque". Toute "remarque" reste "" s'il
  n'y a rien d'anormal ; sinon quelques mots.
- Dans les numéros RCCM, le code du greffe désigne la ville (ex : ABC = Abomey-Calavi,
  COT = Cotonou).
- plusieurs_points_de_vente : true si le dossier indique plus d'un point marchand.
- champs_incertains : liste des champs que tu n'as pas pu lire avec certitude (écriture peu
  lisible, chiffre ambigu, zone masquée ou coupée). Mieux vaut signaler un doute que deviner :
  ces champs seront revus par un humain. Exemple : "piece_identite.date_expiration".
- observations : au plus 3 anomalies notables, en quelques mots chacune.

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
    """Date au format AAAA-MM-JJ ou JJ/MM/AAAA (le modèle rend parfois l'un, parfois l'autre)."""
    value = (value or "").strip()
    try:
        return date.fromisoformat(value)
    except ValueError:
        pass
    m = re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})", value)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None
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
    ref, other = list(dict.fromkeys(ref)), list(dict.fromkeys(other))  # mots répétés
    if sorted(ref) == sorted(other):
        return "identique"
    short, long_ = sorted([ref, other], key=len)
    remaining = list(long_)

    def score(word, w):  # une initiale ("B.") correspond à un prénom qui commence par B
        if len(word) == 1 or len(w) == 1:
            return 1.0 if word[0] == w[0] else 0.0
        if len(word) == len(w) and sum(a != b for a, b in zip(word, w)) == 1:
            return 0.9  # une seule lettre d'écart (« ROCK » / « ROCH ») : proche
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
# ajouts courants au nom du fichier, sans rapport avec la structure :
# horodatage du scan (« 26-09-2026 14.48 », « _092215 »), lieu après une virgule,
# numéro de point de vente (« AMLIHO 2 »)
AJOUTS_FICHIER = [
    re.compile(r"\s*\d{1,2}-\d{1,2}-\d{2,4}\s*\d{1,2}[.:h]\d{2}\s*$"),
    re.compile(r"[\s._-]*_\d{4,6}$"),
    re.compile(r"\s*,.*$"),
    re.compile(r"\s+\d{1,2}$"),
    re.compile(r"(?<=[A-Za-zÀ-ÿ])\d$"),          # numéro collé : « SAGJ GSM PRO1 »
    re.compile(r"\s+PRIME(\s+\d+)?$", re.IGNORECASE),  # « TRAORE ET FILS PRIME »
]


def nom_valide(v: str) -> bool:
    """Enseigne réellement renseignée (pas « NEANT », « - - - », « EE AE »)."""
    lettres = re.sub(r"[^A-Za-zÀ-ÿ]", "", v or "")
    return len(lettres) >= 4 and _key(v) not in ("NEANT", "NEAN", "NONE")


def meme_rccm(a: str, b: str) -> bool:
    """Même registre ; un numéro tronqué par l'OCR (« …A 1067 » / « …A 106714 ») compte comme
    le même."""
    x, y = _rccm(a), _rccm(b)
    return x == y or (min(len(x), len(y)) >= 9 and (x.startswith(y) or y.startswith(x)))


def nettoyer_nom_fichier(nom: str) -> str:
    """Nom du PDF ramené au seul nom de la structure."""
    avant = None
    while avant != nom:
        avant = nom
        nom = COPIE.sub("", nom)
        for motif in AJOUTS_FICHIER:
            nom = motif.sub("", nom)
        nom = nom.strip(" ._-")
    return nom


CIVILITES = {"MONSIEUR", "MADAME", "MADEMOISELLE", "MME", "MLLE", "MR", "M"}
DATE_CONTRADICTOIRE = "lectures contradictoires de la date d'expiration"
PHOTO_CONTESTEE = "photo jugée lisible par la seconde lecture"


def comparer_noms(ref: list[str], other: list[str]) -> str:
    """Règle KIK sur les noms : 'identique' (écart d'espaces seulement, « SEGLAPIERRE » =
    « SEGLA PIERRE »), 'proche' (une lettre d'écart, prénom en moins : contrôle humain) ou
    'different'."""
    if "".join(sorted(ref)) == "".join(sorted(other)) or "".join(ref) == "".join(other):
        return "identique"
    return compare_names(ref, other)


def _distance(a: str, b: str) -> int:
    """Distance d'édition : lettres à changer, ajouter ou retirer pour passer de a à b."""
    precedente = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        courante = [i]
        for j, cb in enumerate(b, 1):
            courante.append(min(precedente[j] + 1, courante[j - 1] + 1,
                                precedente[j - 1] + (ca != cb)))
        precedente = courante
    return precedente[-1]


def comparer_strict(ref: list[str], other: list[str]) -> str:
    """Règle stricte KIK (octobre 2026) : 'identique' (seuls l'ordre des mots, les espaces, les
    accents ou la casse diffèrent), 'lecture' (un seul caractère d'écart : faute ou erreur de
    lecture, un humain tranche) ou 'different' (prénom en moins, initiale, plusieurs lettres)."""
    ref, other = list(dict.fromkeys(ref)), list(dict.fromkeys(other))
    if "".join(sorted(ref)) == "".join(sorted(other)) or "".join(ref) == "".join(other):
        return "identique"
    ecart = min(_distance("".join(ref), "".join(other)),
                _distance("".join(sorted(ref)), "".join(sorted(other))))
    if len(ref) == len(other) <= 6:
        # mots appariés au mieux : « ROCK MARC » / « MARC ROCH »
        ecart = min(ecart, min(sum(_distance(a, b) for a, b in zip(ref, p))
                               for p in permutations(other)))
    return "lecture" if ecart <= 1 else "different"


def sans_forme(v: str) -> str:
    """Nom de structure sans sa forme juridique : « ETS AGS PROD » -> « AGSPROD »."""
    return re.sub(FORMES_JURIDIQUES, "", _key(v))


def comparer_structures(a: str, b: str) -> str:
    """Même règle que comparer_strict pour deux noms de structure (espaces, accents, casse et
    forme juridique mis à part)."""
    x, y = sans_forme(a), sans_forme(b)
    if x == y:
        return "identique"
    return "lecture" if _distance(x, y) <= 1 else "different"


def apply_rules(d: dict, date_traitement: date, nom_fichier: str,
                sources_officielles: set[str] = frozenset({"rccm", "ifu"}),
                photo_bloquante: bool = True) -> dict:
    """Règles KIK, version stricte (octobre 2026) : tout défaut constaté rejette le dossier avec
    l'un des sept motifs, même un petit écart de nom. « À VÉRIFIER » est réservé à ce que la
    machine ne peut pas trancher seule : écriture illisible (on ne devine pas), lectures qui se
    contredisent, un seul caractère d'écart (faute du commercial ou erreur de lecture ?).
    sources_officielles : pièces dont les valeurs viennent du QR de l'État (font foi).
    (photo_bloquante : conservé pour compatibilité, la photo illisible rejette toujours.)"""
    bloquants, vigilance, particuliers = [], [], []
    pieces, fiche, rccm, ifu, pid, analyse = (
        d["pieces"], d["fiche"], d["rccm"], d["ifu"], d["piece_identite"], d["analyse"])
    contrat = d.get("contrat") or {}
    incertains = [c.lower() for c in analyse["champs_incertains"]]

    def rejet(motif: str, detail: str = ""):
        bloquants.append({"motif": motif, "detail": detail})

    def douteux(section: str, *champs: str) -> bool:
        return any(c.startswith(f"{section}.{ch}") for c in incertains for ch in champs)

    def lisible(valeur: str, section: str, champ: str) -> bool:
        """Écrit, et lu sans doute : pas de « ? » ; pour une pièce imprimée, pas signalé
        incertain non plus (sur la fiche, seul le « ? » compte : le modèle signale des doutes
        sur des cases pourtant bien lues)."""
        return (bool(_key(valeur)) and "?" not in valeur
                and (section == "fiche" or not douteux(section, champ)))

    def ecart(verdict: str, quoi: str, sur: bool = True, motif: str = M_RCCM_NON_CONFORME):
        """Un seul caractère d'écart, ou lecture incertaine : à vérifier ; sinon rejet."""
        if verdict == "lecture":
            vigilance.append(f"{quoi[0].upper() + quoi[1:]} : un seul caractère d'écart (faute "
                             "ou erreur de lecture, à confirmer)")
        elif verdict == "different" and not sur:
            vigilance.append(f"{quoi[0].upper() + quoi[1:]} (lecture incertaine, à confirmer)")
        elif verdict == "different":
            rejet(motif, quoi)

    # ---- Pièces présentes et lisibles --------------------------------------------------
    # carte APIEx à la place de l'attestation IFU : acceptée si le numéro IFU y figure
    substitut_ifu = ifu["document_substitut"].strip() or (
        "Carte professionnelle APIEx" if "APIEX" in pieces["ifu"]["remarque"].upper() else "")
    numero_ifu = re.sub(r"\D", "", ifu["numero"])
    for p in PIECES:
        if p == "ifu" and not pieces[p]["presente"] and substitut_ifu:
            if len(numero_ifu) == 13:
                particuliers.append(f"{substitut_ifu} à la place de l'attestation IFU "
                                    f"(numéro IFU {numero_ifu} présent)")
            else:
                rejet(M_INCOMPLET, f"{substitut_ifu} sans numéro IFU, à la place de "
                                   "l'attestation IFU")
        elif p == "rccm" and not pieces[p]["presente"] and rccm["document_substitut"].strip():
            vigilance.append(f"{rccm['document_substitut']} à la place du RCCM (association : "
                             "décision humaine)")
        elif not pieces[p]["presente"]:
            rejet(M_INCOMPLET, f"{PIECE_LABELS[p]} absent(e)")
        elif not pieces[p]["lisible"] and p not in sources_officielles:
            detail = f"{PIECE_LABELS[p]} : {pieces[p]['remarque']}".rstrip(" :")
            if p == "fiche":
                # écriture manuscrite : on ne force pas la lecture, un humain la fait
                vigilance.append(f"Fiche manuscrite difficile à lire : {pieces[p]['remarque']}"
                                 .rstrip(" :"))
            elif p == "piece_identite" and pid["nom"] and _date(pid["date_expiration"]):
                # jugée floue alors que nom et date ont été lus : les deux constats se
                # contredisent, un humain tranche
                vigilance.append(f"Pièce d'identité jugée floue mais nom et date lus "
                                 f"({pieces[p]['remarque'][:80]})")
            else:
                rejet(M_RCCM_ILLISIBLE if p == "rccm" else M_FLOUE, detail)

    # ---- Fiche : cases obligatoires vides ----------------------------------------------------
    if pieces["fiche"]["presente"]:
        # une case signalée comme mal lue n'est pas vide : elle ne rend pas le dossier incomplet
        vides = [label for k, label in FICHE_OBLIGATOIRES.items()
                 if not fiche[k].strip() and not douteux("fiche", k)]
        if vides:
            rejet(M_INCOMPLET, "fiche non remplie : " + ", ".join(vides))

        # ---- Fiche : écriture illisible (« ? »), jamais devinée --------------------------
        illisibles = []
        commune_lue = referentiel.commune(fiche["ville"].replace("?", ""))
        for k, label in FICHE_A_LIRE.items():
            v = fiche[k].strip()
            if "?" not in v:
                continue
            net = v.replace("?", "")
            # rattachée à une liste fermée (77 communes, départements de KIK) : lecture sûre
            if k == "ville" and commune_lue:
                continue
            if k == "departement" and referentiel.departement(net, commune_lue,
                                                               fiche["commercial"]):
                continue
            illisibles.append(f"{label} (« {v} »)")
        if illisibles:
            vigilance.append("Fiche manuscrite illisible : " + ", ".join(illisibles))
        # commercial : seuls ceux de la liste officielle (NOM DES COMMERCIAUX.xlsx)
        if fiche["commercial"].strip() and not referentiel.commercial(fiche["commercial"]):
            vigilance.append(f"Commercial « {fiche['commercial']} » non reconnu dans la liste "
                             "officielle des commerciaux")

    # ---- Nom commercial : nom du PDF et nom de la structure sur la fiche --------------------
    noms_rccm = [v for v in (rccm["enseigne"], rccm["nom_commercial"]) if nom_valide(v)]
    noms_ifu = [ifu["nom_etablissement"]] if nom_valide(ifu["nom_etablissement"]) else []
    noms_imprimes = list(dict.fromkeys(noms_rccm + noms_ifu))
    # registre officiel (QR) : fait foi ; lecture du scan : sûre sauf doute signalé
    officiel = bool((noms_rccm and "rccm" in sources_officielles)
                    or (noms_ifu and "ifu" in sources_officielles))
    sur = officiel or not (douteux("rccm", "enseigne", "nom_commercial")
                           or douteux("ifu", "nom_etablissement")
                           or any("?" in n for n in noms_imprimes))
    attendu = " / ".join(noms_imprimes)
    fichier = nettoyer_nom_fichier(nom_fichier)
    if COPIE.search(nom_fichier):
        vigilance.append(f"Doublon possible : fichier « {nom_fichier} » nommé comme une copie")
    if noms_imprimes:
        verdicts = {comparer_structures(fichier, n) for n in noms_imprimes}
        if "identique" not in verdicts:
            detail = f"fichier « {nom_fichier} », nom commercial « {attendu} »"
            if not sur:
                vigilance.append(f"Nom du PDF différent du nom commercial lu sur le scan, "
                                 f"lecture à confirmer ({detail})")
            elif "lecture" in verdicts and not officiel:
                vigilance.append(f"Nom du PDF : un seul caractère d'écart avec le nom commercial "
                                 f"({detail}) : faute ou erreur de lecture, à confirmer")
            else:
                rejet(M_NOM_PDF, detail)
        structure = fiche["nom_structure"].strip()
        if pieces["fiche"]["presente"] and lisible(structure, "fiche", "nom_structure"):
            verdicts = {comparer_structures(structure, n) for n in noms_imprimes}
            if "identique" not in verdicts:
                ecart("lecture" if "lecture" in verdicts else "different",
                      f"nom de la structure sur la fiche (« {structure} ») différent du nom "
                      f"commercial du RCCM (« {attendu} »)", sur)
    else:
        # pas de nom commercial imprimé (enseigne « NEANT ») : nom de la fiche ou du promoteur
        candidats = [v for v in (fiche["nom_structure"], f"{rccm['nom']} {rccm['prenoms']}",
                                 f"{pid['nom']} {pid['prenoms']}") if lisible(v, "-", "-")]
        if not any(comparer_structures(fichier, v) == "identique"
                   or comparer_strict(_name_tokens(fichier), _name_tokens(v)) == "identique"
                   for v in candidats):
            vigilance.append(f"Pas de nom commercial sur le RCCM : nom du PDF « {nom_fichier} » "
                             f"à comparer à la fiche (« {fiche['nom_structure']} »)")

    # ---- Pièce d'identité : date d'expiration et photo --------------------------------------
    if pieces["piece_identite"]["presente"]:
        exp = _date(pid["date_expiration"])
        if exp is None:
            if any(DATE_CONTRADICTOIRE in c for c in incertains):
                lecture = next(c for c in analyse["champs_incertains"]
                               if DATE_CONTRADICTOIRE in c.lower())
                vigilance.append(lecture[0].upper() + lecture[1:])
            else:
                rejet(M_FLOUE, "date d'expiration de la pièce d'identité illisible")
        elif exp < date_traitement:
            rejet(M_ID_EXPIRE, f"{pid['type'] or 'pièce'} expirée le {exp:%d/%m/%Y}")
        if not pid["photo_lisible"]:
            if any(PHOTO_CONTESTEE in c for c in incertains):
                vigilance.append("Photo de la pièce d'identité jugée illisible par une seule "
                                 "lecture sur deux (à confirmer)")
            else:
                rejet(M_PHOTO, "photo absente ou visage non identifiable")

    # ---- RCCM : numéro, cachet du greffe, date de délivrance --------------------------------
    if pieces["rccm"]["presente"] and pieces["rccm"]["lisible"]:
        if not _norm(rccm["numero"]) and _rccm(ifu["rccm"]):
            rccm["numero"] = ifu["rccm"]  # repris de l'IFU, qui cite le même registre
        if not _norm(rccm["numero"]):
            if "rccm" in sources_officielles:
                rejet(M_RCCM_NON_CONFORME, "numéro RCCM absent du registre officiel")
            else:
                rejet(M_RCCM_ILLISIBLE, "numéro RCCM illisible")
        elif not re.search(r"RB/[A-Z]{2,4}/\d{2}[A-Z]\d+", rccm["numero"].upper().replace(" ", "")):
            particuliers.append(f"Format du numéro RCCM inhabituel ({rccm['numero']})")
        if not rccm["cachet_greffe"] and _key(rccm.get("reference_verification", "")):
            # extrait électronique : pas de tampon, authentifié par son numéro de référence
            particuliers.append(f"RCCM électronique, numéro de vérification "
                                f"{rccm['reference_verification']}")
        elif not rccm["cachet_greffe"]:
            if douteux("rccm", "cachet"):
                vigilance.append("Cachet du greffe sur le RCCM à confirmer")
            else:
                rejet(M_RCCM_NON_CONFORME, "cachet du greffe absent du RCCM")
        papier, registre = _date(rccm["date_delivrance"]), _date(rccm["registre_date_delivrance"])
        if papier and registre and papier != registre:
            # le QR renvoie au document lui-même : ses dates doivent être celles du papier
            vigilance.append(f"Date de délivrance lue sur le RCCM ({papier:%d/%m/%Y}) différente "
                             f"du registre officiel ({registre:%d/%m/%Y}) : document modifié ou "
                             "erreur de lecture, à confirmer")
        # date établie par le registre officiel (QR) : présente même si le papier est peu lisible
        if not registre and not re.search(r"(19|20)\d\d", rccm["date_delivrance"]):
            if douteux("rccm", "date_delivrance"):
                vigilance.append("Date de délivrance du RCCM à confirmer")
            else:
                rejet(M_RCCM_NON_CONFORME, "date de délivrance absente du RCCM (2e page "
                                           "manquante ?)")

    # ---- Noms : RCCM, IFU et fiche comparés à la pièce d'identité -------------------------
    ref = _name_tokens(pid["nom"], pid["prenoms"])
    ref_txt = f"{pid['nom']} {pid['prenoms']}".strip()
    if pieces["piece_identite"]["presente"] and not ref:
        rejet(M_FLOUE, "nom illisible sur la pièce d'identité")
    elif ref:
        pid_sur = not douteux("piece_identite", "nom", "prenoms")
        if not pid_sur:
            vigilance.append(f"Nom sur la pièce d'identité lu avec doute ({ref_txt})")
        etablissements = {_key(v) for v in (rccm["enseigne"], rccm["nom_commercial"]) if _key(v)}
        rccm_lie = ((bool(_rccm(ifu["rccm"])) and _rccm(rccm["numero"]) == _rccm(ifu["rccm"]))
                    or _key(ifu["nom_etablissement"]) in etablissements)
        promoteur, promoteur_txt = ref, ref_txt  # à défaut de RCCM : la pièce d'identité
        for cle, label, present, nom, prenoms in [
            ("rccm", "le RCCM", pieces["rccm"]["presente"], rccm["nom"], rccm["prenoms"]),
            ("ifu", "l'IFU", pieces["ifu"]["presente"] or bool(substitut_ifu),
             ifu["nom"], ifu["prenoms"]),
        ]:
            if not present:
                continue
            other_txt = f"{nom} {prenoms}".strip()
            other = _name_tokens(nom, prenoms)
            # le modèle range parfois le nom de l'entreprise à la place de celui de la personne
            if other and _key(other_txt) in {_key(v) for v in (
                    fiche["nom_structure"], rccm["enseigne"], rccm["nom_commercial"],
                    ifu["nom_etablissement"]) if _key(v)}:
                other = []
            if not other:
                if cle == "ifu" and rccm_lie:
                    continue  # IFU sans nom de personne, rattaché au RCCM par son numéro
                if cle == "rccm" and cle not in sources_officielles:
                    rejet(M_RCCM_ILLISIBLE, "nom du titulaire illisible sur le RCCM")
                elif cle == "rccm":
                    vigilance.append("Nom du titulaire absent du RCCM")
                else:
                    # IFU d'une société ou carte sans nom de personne : simple information
                    particuliers.append("Pas de nom de personne sur l'IFU")
                continue
            civilite = [t for t in other if t in CIVILITES and t not in ref]
            if civilite:
                # imprimée par le greffe sur le registre officiel : décision humaine (KIK)
                vigilance.append(f"Civilité « {' '.join(civilite)} » ajoutée au nom sur {label} "
                                 f"({other_txt}) ; pièce d'identité ({ref_txt})")
                other = [t for t in other if t not in CIVILITES]
            if cle == "rccm":
                promoteur, promoteur_txt = other, other_txt
            lu_sur = pid_sur and (cle in sources_officielles or not douteux(cle, "nom", "prenoms"))
            ecart(comparer_strict(ref, other),
                  f"nom sur {label} ({other_txt}) différent de la pièce d'identité ({ref_txt})",
                  lu_sur)
            if cle not in sources_officielles and douteux(cle, "nom", "prenoms"):
                vigilance.append(f"Nom sur {label} lu avec doute ({other_txt})")
        # le représentant écrit sur la fiche doit être le promoteur, bien écrit
        representant = fiche["representant"].strip()
        if pieces["fiche"]["presente"] and lisible(representant, "fiche", "representant"):
            ecart(comparer_strict(promoteur, _name_tokens(representant)),
                  f"représentant écrit sur la fiche ({representant}) différent du promoteur "
                  f"({promoteur_txt})")

    # ---- Numéro RCCM cité par l'IFU ------------------------------------------------------
    motif_rccm = re.compile(r"RB\s*/?\s*[A-Z]{2,4}\s*/?\s*\d{2}\s*[A-Z]\s*\d+")
    if (motif_rccm.search(rccm["numero"].upper()) and motif_rccm.search(ifu["rccm"].upper())
            and not meme_rccm(rccm["numero"], ifu["rccm"])):
        message = (f"numéro RCCM différent entre RCCM ({rccm['numero']}) et IFU "
                   f"({ifu['rccm']})")
        if {"rccm", "ifu"} <= set(sources_officielles):
            rejet(M_RCCM_NON_CONFORME, message)
        else:
            vigilance.append(message[0].upper() + message[1:])

    # ---- Contrat : signé par le marchand -----------------------------------------------------
    if not (contrat.get("present") or contrat.get("page_signature_trouvee")):
        if "non identifi" in contrat.get("remarque", ""):
            # pages non identifiées : le contrat y est peut-être
            vigilance.append(f"Contrat non trouvé ({contrat['remarque']})")
        else:
            rejet(M_INCOMPLET, "contrat Celtiis Cash absent")
    elif not contrat.get("page_signature_trouvee"):
        if "page de signature absente" in contrat.get("remarque", ""):
            # toutes les pages du contrat comparées à l'exemplaire, aucune ne lui ressemble
            rejet(M_INCOMPLET, f"contrat incomplet, {contrat['remarque']}")
        else:
            vigilance.append("Page de signature du contrat non reconnue : signature du "
                             "marchand à contrôler")
    elif not contrat.get("signe_marchand"):
        if douteux("contrat", "signe"):
            vigilance.append("Signature du marchand sur le contrat à confirmer")
        else:
            rejet(M_INCOMPLET, "contrat non signé par le marchand")

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


