import argparse
import asyncio
import base64
import contextvars
import csv
import hashlib
import io
import json
import random
import threading
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import date, datetime
from pathlib import Path

import anthropic
import fitz
import numpy as np
from anthropic.types import Message
from PIL import Image

from bench_dossiers import (DATE_CONTRADICTOIRE, PHOTO_CONTESTEE, meme_rccm, ROOT, SCHEMA, SYSTEM, _date, _norm, _rccm, _template, apply_rules,
                            conform, load_api_key, marquer_doublons, parse_json)
from ocr_local import lire_dossier
import referentiel
import registre
import sharepoint
from qr_officiel import pieces_officielles


def recadrer(img: Image.Image, marge: float = 0.02) -> Image.Image:
    """Retire le blanc autour du document : une CIP posée sur une page A4 n'en occupe souvent
    que le quart, et Claude facture chaque pixel envoyé."""
    k = 4  # repérage sur une image réduite : rapide, et insensible aux poussières
    gris = np.asarray(img.convert("L").reduce(k))
    sombre = gris < 170
    lignes = np.flatnonzero(sombre.mean(axis=1) > 0.01)
    colonnes = np.flatnonzero(sombre.mean(axis=0) > 0.01)
    if not len(lignes) or not len(colonnes):
        return img
    h, w = gris.shape
    y0, y1, x0, x1 = lignes[0], lignes[-1] + 1, colonnes[0], colonnes[-1] + 1
    if (y1 - y0) * (x1 - x0) > 0.85 * h * w:
        return img  # le document remplit la page : rien à gagner
    my, mx = int(marge * h) + 1, int(marge * w) + 1
    return img.crop((max(0, x0 - mx) * k, max(0, y0 - my) * k,
                     min(w, x1 + mx) * k, min(h, y1 + my) * k))


def images_pages(pdf: Path, pages: list[int], rotations: dict, max_side: int, quality: int,
                 cotes: dict | None = None):
    """Rend uniquement les pages demandées, redressées et débarrassées de leurs marges blanches.
    cotes : côté maximal propre à certaines pages (pages de simple contrôle, moins détaillées)."""
    sorties = []
    with fitz.open(pdf) as doc:
        for numero in pages:
            page = doc[numero - 1]
            cote = (cotes or {}).get(numero, max_side)
            # rendu plus fin que nécessaire : après recadrage, une petite carte garde ses détails
            zoom = 2400 / max(page.rect.width, page.rect.height)
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace=fitz.csRGB)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            angle = rotations.get(str(numero), rotations.get(numero, 0))
            if angle:
                img = img.rotate(-angle, expand=True)
            img = recadrer(img)
            if max(img.size) > cote:
                r = cote / max(img.size)
                img = img.resize((round(img.width * r), round(img.height * r)), Image.LANCZOS)
            tampon = io.BytesIO()
            img.save(tampon, format="JPEG", quality=quality)
            sorties.append((numero, tampon.getvalue()))
    return sorties


# modèles disponibles : prix en $ par token (entrée, sortie), tarif normal
# (Sonnet 5.5 : tarif repris de Sonnet 5, à confirmer sur console.anthropic.com)
MODELES = {
    "sonnet": ("claude-sonnet-5-5", 2.00 / 1e6, 10.00 / 1e6),
    "haiku": ("claude-haiku-4-5", 1.00 / 1e6, 5.00 / 1e6),
}
MODEL, PRICE_IN, PRICE_OUT = MODELES["sonnet"]
EFFORT = None  # niveau de réflexion (non accepté par Haiku 4.5)
REMISE_LOT = 0.5  # Message Batches API : tous les tokens à moitié prix
MAX_DOSSIERS = 50  # par lancement : au-delà, seuls les 50 premiers PDF (ordre alphabétique)
REMISE = REMISE_LOT
CREDIT_EPUISE = threading.Event()  # levé au premier refus pour crédit insuffisant
# lectures de la date d'expiration en désaccord : si toutes sont postérieures à cette marge
# (en jours), la pièce est valide quelle que soit la bonne lecture (règle KIK : valide tant
# que la date n'est pas passée)
MARGE_JOURS = 0

# champs jamais utilisés (ni règles, ni fichier SharePoint) : pas demandés à Claude, qui écrit
# ainsi moins (les tokens de sortie sont les plus chers)
NON_DEMANDES = {
    "fiche": ("carre_maison_ilot", "gps_longitude", "gps_latitude", "taux_reversement",
              "date_demande"),
    "rccm": ("nationalite", "date_naissance", "lieu_naissance", "adresse", "activite",
             "date_debut_exploitation", "registre_date_immatriculation",
             "registre_date_delivrance"),
    "ifu": ("categorie", "adresse", "regime_fiscal", "centre_impots", "date_emission"),
    "piece_identite": ("date_naissance", "lieu_naissance", "nationalite"),
    "analyse": ("observations",),
}
COTE_CONTROLE = 1200  # px : signature, cachet et date se voient très bien à cette taille
COTE_IMPRIME = 1300   # px : texte dactylographié (RCCM, IFU…) encore net
COTE_PIECE = 1400     # px : pièce d'identité (sa date a en plus son propre gros plan) ; la
                      # fiche manuscrite garde la pleine résolution
IMPRIMES = ("rccm", "ifu", "apiex", "cnss", "ong")


def _consignes() -> list[dict]:
    """Instructions système, identiques pour tous les dossiers : Claude les relit depuis son
    cache, à 10 % du prix (les sections que le QR officiel a déjà fournies sont écartées dans
    la consigne propre à chaque dossier, voir CONSIGNE)."""
    schema = {**SCHEMA, "properties": dict(SCHEMA["properties"])}
    for section, inutiles in NON_DEMANDES.items():
        if section in schema["properties"]:
            partie = dict(schema["properties"][section])
            partie["properties"] = {k: v for k, v in partie["properties"].items()
                                    if k not in inutiles and not k.startswith(("case_", "autorisee"))}
            schema["properties"][section] = partie
    entete = SYSTEM.split("Réponds uniquement")[0]
    # JSON compact (sans indentation ni retours à la ligne) : moins de tokens écrits
    texte = (entete + "Réponds uniquement avec un objet JSON compact, sur une seule ligne, sans "
             "texte autour, ayant exactement cette structure :\n"
             + json.dumps(_template(schema), ensure_ascii=False, separators=(",", ":")))
    # partie fixe d'une requête à l'autre : mise en cache (lectures facturées à 10 %)
    return [{"type": "text", "text": texte, "cache_control": {"type": "ephemeral"}}]


CONSIGNES = _consignes()


CONSIGNE = """Tu reçois seulement les pages utiles : la fiche de création (manuscrite), la pièce
d'identité, la dernière page du RCCM (cachet du greffe, date de délivrance), la page de
signature du contrat, et éventuellement des pages non identifiées. Chaque image est précédée
de son numéro de page dans le dossier.

Documents officiels récupérés auprès de l'État (QR code) : ces valeurs font foi, reprends-les
telles quelles :
{officiel}

Lecture automatique (OCR) des autres pages, souvent fautive (étiquettes collées au nom,
lettres mal lues) : ne la recopie pas, lis toi-même ces champs sur l'image et corrige-les :
{indicatif}

Pages fournies :
{attendu}

{omettre}

Consignes particulières :
- Sur la pièce d'identité, lis la date d'expiration chiffre par chiffre. Si tu n'es pas
  certain, mets "" et signale "piece_identite.date_expiration" dans champs_incertains.
- Sur la fiche manuscrite, recopie lettre par lettre ce qui est écrit, fautes comprises, avec
  « ? » pour chaque caractère que tu ne lis pas avec certitude (et la case dans
  champs_incertains) ; laisse "" seulement si la case est vide. Ne devine jamais un mot.
- Sur la pièce d'identité et le RCCM, signale dans champs_incertains tout nom ou chiffre dont
  tu n'es pas sûr : mieux vaut un contrôle humain qu'une valeur inventée.
- RCCM : cachet_greffe, date_delivrance et reference_verification (extrait électronique) se
  lisent sur la page RCCM fournie, même quand le reste du RCCM vient du registre officiel.
- Contrat : sur la page de signature, regarde seulement si le marchand a signé sous
  « POUR <marchand> » (page_signature_trouvee, signe_marchand, nom_signataire). Si aucune page
  fournie ne contient ce bloc, page_signature_trouvee = false."""


def _preparer(pdf_str: str) -> dict:
    """Lecture locale complète d'un dossier : OCR puis QR officiels (un processus par dossier)."""
    pdf = Path(pdf_str)
    ocr = lire_dossier(pdf)
    t0 = time.perf_counter()
    # pièces identifiées d'abord, puis toutes les autres pages de tête : l'identification
    # locale se trompe parfois de type sur les pages pivotées, le QR, lui, ne ment pas
    types = {int(p): t for p, t in ocr["pages"].items()}
    prioritaires = [p for p, t in sorted(types.items()) if t in ("rccm", "ifu", "apiex")]
    reste = [p for p, t in sorted(types.items())
             if p <= 8 and p not in prioritaires and t not in ("fiche",)]
    ocr["officiel"] = pieces_officielles(pdf, prioritaires + reste)
    ocr["t_qr_s"] = round(time.perf_counter() - t0, 1)
    # les pièces confirmées par le portail n'ont plus besoin d'être envoyées à Claude
    resolues = set(ocr["officiel"])
    if "apiex" in resolues:
        resolues.add("ifu")
    # on n'écarte que les pages dont le QR a répondu (et le folio 2 d'un RCCM confirmé) :
    # une page seulement « supposée » IFU peut être autre chose, par ex. la CIP à l'envers
    pages_officielles = {c.get("page") for c in ocr["officiel"].values()}
    ocr["pages_a_envoyer"] = sorted(
        p for p in ocr["pages_a_envoyer"]
        if p not in pages_officielles
        and not (ocr["pages"].get(str(p), ocr["pages"].get(p)) == "rccm" and "rccm" in resolues))
    # pièce d'identité localisée : les pages inconnues de la fin sont celles du contrat
    if any(t == "piece_identite" for t in ocr["pages"].values()):
        ocr["pages_a_envoyer"] = [p for p in ocr["pages_a_envoyer"]
                                  if p <= 8 or ocr["pages"].get(str(p)) != "inconnu"]
    # pièce d'identité introuvable en local : on envoie toutes les pages de tête non confirmées
    else:
        tete = [int(p) for p, t in ocr["pages"].items()
                if int(p) <= 8 and t != "contrat" and int(p) not in pages_officielles]
        ocr["pages_a_envoyer"] = sorted(set(ocr["pages_a_envoyer"]) | set(tete))
    # contrôles du papier, même quand le QR a tout confirmé : dernière page du RCCM (cachet du
    # greffe, date de délivrance) et page de signature du contrat (ou les plus probables)
    rccm = [int(p) for p, t in ocr["pages"].items() if t == "rccm"]
    # page de signature reconnue par comparaison avec l'exemplaire (ou page douteuse, que
    # Claude tranche) ; aucune page ressemblante : elle manque, rien à envoyer
    signature = ocr.get("signature") or {}
    papier = ([max(rccm)] if rccm else []) + [
        p for p in (signature.get("page"), signature.get("douteuse")) if p]
    ocr["pages_a_envoyer"] = sorted(set(ocr["pages_a_envoyer"]) | set(papier))
    return ocr


def sections_officielles(ocr: dict) -> set[str]:
    """Sections du JSON remplies par un document officiel récupéré via QR (valeurs sûres)."""
    officiel = ocr.get("officiel", {})
    return ({"rccm"} if "rccm" in officiel else set()) | (
        {"ifu"} if "ifu" in officiel or "apiex" in officiel else set())


def socle(ocr: dict) -> tuple[dict, list[str]]:
    """JSON de départ : QR officiel d'abord, OCR local ensuite. Renvoie aussi les doutes."""
    data = conform({}, SCHEMA)
    doutes = []
    officiel = ocr.get("officiel", {})

    rccm = officiel.get("rccm", {})
    for source, cible in (("rccm", "numero"), ("nom", "nom"), ("prenoms", "prenoms"),
                          ("enseigne", "enseigne"), ("nom_commercial", "nom_commercial"),
                          ("nationalite", "nationalite"), ("activite", "activite"),
                          ("date_naissance", "date_naissance"), ("telephone", "telephone"),
                          ("lieu_naissance", "lieu_naissance"),
                          ("date_immatriculation", "registre_date_immatriculation"),
                          ("date_delivrance", "registre_date_delivrance")):
        if rccm.get(source):
            data["rccm"][cible] = rccm[source]
    if rccm:
        # (cachet du greffe et date de délivrance : lus sur le papier par Claude)
        data["pieces"]["rccm"].update(presente=True, lisible=True, pages=[rccm.get("page", 0)],
                                      remarque="vérifié par QR officiel")

    ifu = officiel.get("ifu") or officiel.get("apiex", {})
    for source, cible in (("ifu", "numero"), ("rccm", "rccm"), ("nom", "nom"),
                          ("prenoms", "prenoms"), ("nom_etablissement", "nom_etablissement"),
                          ("categorie", "categorie"), ("regime_fiscal", "regime_fiscal"),
                          ("centre_impots", "centre_impots"), ("telephone", "telephone")):
        if ifu.get(source):
            data["ifu"][cible] = ifu[source]
    if "apiex" in officiel:
        data["ifu"]["document_substitut"] = "Carte professionnelle APIEx"
    if ifu:
        data["pieces"]["ifu"].update(presente="ifu" in officiel, lisible=True,
                                     pages=[ifu.get("page", 0)],
                                     remarque="vérifié par QR officiel")
        data["ifu"]["cachet_dgi"] = True

    # secours : lecture locale, seulement là où le portail n'a rien donné
    for piece, champs in ocr["champs"].items():
        for cle, valeur in champs.items():
            cible = {"rccm": {"rccm": ("rccm", "numero"), "nom": ("rccm", "nom"),
                              "prenoms": ("rccm", "prenoms"), "enseigne": ("rccm", "enseigne"),
                              "nom_commercial": ("rccm", "nom_commercial")},
                     "ifu": {"ifu": ("ifu", "numero"), "rccm": ("ifu", "rccm")},
                     "apiex": {"ifu": ("ifu", "numero"), "rccm": ("ifu", "rccm")},
                     "cnss": {"ifu": ("ifu", "numero")},
                     "piece_identite": {"npi": ("piece_identite", "numero"),
                                        "telephone": ("piece_identite", "telephone")},
                     }.get(piece, {}).get(cle)
            if cible and isinstance(valeur, str) and not data[cible[0]][cible[1]]:
                data[cible[0]][cible[1]] = valeur
    # le papier et le portail doivent désigner le même registre
    local_rccm = ocr["champs"].get("rccm", {}).get("rccm", "")
    if rccm.get("rccm") and local_rccm and not meme_rccm(local_rccm, rccm["rccm"]):
        doutes.append(f"numéro RCCM du papier ({local_rccm}) différent du registre officiel "
                      f"({rccm['rccm']})")
    for piece in ("fiche", "rccm", "ifu", "piece_identite"):
        pages = [int(p) for p, t in ocr["pages"].items() if t == piece]
        if pages and not data["pieces"][piece]["presente"]:
            data["pieces"][piece].update(presente=True, lisible=True, pages=pages)
    # pièces de remplacement reconnues en local (règles KIK) : carte professionnelle ou
    # attestation CNSS à la place de l'IFU, récépissé d'ONG à la place du RCCM
    types = set(ocr["pages"].values())
    if not data["pieces"]["ifu"]["presente"] and not data["ifu"]["document_substitut"]:
        if "apiex" in types:
            data["ifu"]["document_substitut"] = "Carte professionnelle"
        elif "cnss" in types:
            data["ifu"]["document_substitut"] = "Attestation CNSS (matricule = n° IFU)"
    if not data["pieces"]["rccm"]["presente"] and "ong" in types:
        data["rccm"]["document_substitut"] = "Récépissé de déclaration d'ONG"
    # contrat : présent si des pages en ont été reconnues ; sa page de signature est reconnue
    # en local (comparaison avec l'exemplaire), la signature elle-même est vue par Claude
    data["contrat"]["present"] = "contrat" in types
    signature = ocr.get("signature") or {}
    if signature.get("page"):
        data["contrat"]["page_signature_trouvee"] = True
    elif data["contrat"]["present"] and not signature.get("douteuse"):
        data["contrat"]["remarque"] = (
            f"page de signature absente : aucune des {signature.get('lues', 0)} pages lues ne "
            "ressemble au modèle")
    inconnues = sorted(int(p) for p, t in ocr["pages"].items() if t == "inconnu")
    if not data["contrat"]["present"] and inconnues:
        data["contrat"]["remarque"] = f"pages non identifiées : {inconnues}"
    return data, doutes


def fusionner(base: dict, claude: dict, ocr: dict, date_gros_plan: str = "") -> tuple[dict, list[str]]:
    """Le QR officiel l'emporte toujours ; ailleurs la lecture de Claude remplace celle de l'OCR
    pour les noms et l'enseigne (l'OCR y colle des étiquettes). Date de CIP : double lecture."""
    doutes = []
    data = json.loads(json.dumps(base))
    sures = sections_officielles(ocr)
    textes_claude = {"nom", "prenoms", "enseigne", "nom_commercial", "nom_etablissement"}
    for section in ("fiche", "rccm", "ifu", "piece_identite", "contrat", "pieces", "analyse"):
        for champ, valeur in claude.get(section, {}).items():
            if champ not in data[section]:
                continue
            actuel = data[section][champ]
            if isinstance(valeur, str):
                remplacer = (section in ("rccm", "ifu") and section not in sures
                             and champ in textes_claude)
                if valeur and (not actuel or remplacer):
                    data[section][champ] = valeur
            elif isinstance(valeur, list):
                data[section][champ] = list(dict.fromkeys(actuel + valeur)) if actuel else valeur
            elif isinstance(valeur, dict):
                for k, v in valeur.items():
                    if v not in ("", None) and not data[section][champ].get(k):
                        data[section][champ][k] = v
            elif valeur and not actuel:
                data[section][champ] = valeur

    # Date d'expiration : trois lectures indépendantes, il en faut deux concordantes
    lectures = {"OCR local": ocr["champs"].get("piece_identite", {}).get("date_expiration", ""),
                "Claude (page entière)": claude["piece_identite"]["date_expiration"],
                "Claude (gros plan)": date_gros_plan}
    # toutes ramenées au format AAAA-MM-JJ avant de comparer (« 23/05/2031 » = « 2031-05-23 »)
    lectures = {s: (_date(v).isoformat() if _date(v) else "") for s, v in lectures.items()}
    votes = {}
    for source, valeur in lectures.items():
        if valeur:
            votes.setdefault(valeur, []).append((source, valeur))
    retenue = max(votes.values(), key=len, default=[])
    dates = [date.fromisoformat(v) for v in lectures.values() if v]
    aujourd_hui = date.today()
    if len(retenue) >= 2:
        data["piece_identite"]["date_expiration"] = retenue[0][1]
    elif dates and all((d - aujourd_hui).days >= MARGE_JOURS for d in dates):
        # toutes les lectures donnent une carte valide longtemps encore : un chiffre de
        # désaccord ne change pas la décision ; on retient la plus proche par prudence
        data["piece_identite"]["date_expiration"] = min(dates).isoformat()
    elif dates and all(d < aujourd_hui for d in dates):
        # toutes les lectures donnent une carte déjà expirée : on retient la plus favorable
        data["piece_identite"]["date_expiration"] = max(dates).isoformat()
    else:
        # aucune lecture (rejet « Pièce floue ») ou lectures contradictoires, les unes valides,
        # les autres expirées (contrôle humain) : apply_rules tranche selon DATE_CONTRADICTOIRE
        data["piece_identite"]["date_expiration"] = ""
        if dates:
            detail = ", ".join(f"{s} « {v} »" for s, v in lectures.items() if v)
            doutes.append(f"{DATE_CONTRADICTOIRE} ({detail})")
    if data["piece_identite"]["date_expiration"]:
        # date établie par nos lectures croisées : le doute propre au modèle n'a plus d'objet
        data["analyse"]["champs_incertains"] = [
            c for c in data["analyse"]["champs_incertains"]
            if not c.lower().startswith("piece_identite.date_expiration")]
    return data, doutes


DATE_SCHEMA = {"type": "object", "additionalProperties": False,
               "required": ["date_expiration", "lisible", "remarque"],
               "properties": {"date_expiration": {"type": "string"},
                              "lisible": {"type": "boolean"},
                              "remarque": {"type": "string"}}}

DATE_CONSIGNE = """Cette image est le bas d'une pièce d'identité béninoise (CIP/CNI), agrandi.
Lis UNIQUEMENT la date d'expiration ("Expire le ..."), chiffre par chiffre, et recopie-la
TELLE QU'IMPRIMÉE au format JJ/MM/AAAA (le premier nombre est le jour).
Réponds par ce JSON, sans rien d'autre :
{"date_expiration": "JJ/MM/AAAA", "lisible": true/false, "remarque": ""}
Si tu n'es pas absolument certain de chaque chiffre : date_expiration = "" et lisible = false."""


def gros_plan_date(pdf: Path, page_num: int, angle: int, quality: int) -> bytes:
    """Bas de la pièce d'identité, agrandi : c'est là que figure « Expire le »."""
    import io

    import fitz
    from PIL import Image
    with fitz.open(pdf) as doc:
        page = doc[page_num - 1]
        zoom = 2600 / max(page.rect.width, page.rect.height)
        data = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom)).tobytes("png")
    img = Image.open(io.BytesIO(data)).convert("RGB")
    if angle:
        img = img.rotate(-angle, expand=True)
    img = recadrer(img)  # la carte seule, pas le bas (souvent blanc) de la page A4
    largeur, hauteur = img.size
    bas = img.crop((0, int(hauteur * 0.55), largeur, hauteur))
    if bas.width > 1200:  # chiffres de la date encore très nets à cette taille
        bas = bas.resize((1200, int(bas.height * 1200 / bas.width)), Image.LANCZOS)
    tampon = io.BytesIO()
    bas.convert("RGB").save(tampon, format="JPEG", quality=quality)
    return tampon.getvalue()


# --------------------------------------------------------------------------
# Appels à Claude : en lot (Message Batches API, -50 %) ou directs
# --------------------------------------------------------------------------

DOSSIER = contextvars.ContextVar("dossier")  # état du dossier en cours (attentes de Claude)


class Direct:
    """Appel immédiat, au tarif normal (essais et mises au point)."""

    def __init__(self, client):
        self.client = client
        self.actifs = 0

    async def creer(self, **params) -> Message:
        return await self.client.messages.create(**params)


class LotClaude:
    """Requêtes de tous les dossiers regroupées en lots (Message Batches API : -50 %).

    Chaque dossier attend sa réponse comme pour un appel direct. Un lot part dès que tous les
    dossiers en cours attendent Claude, ou plus tôt s'il devient gros ou ancien (la lecture
    locale continue pendant ce temps). Lots envoyés et réponses reçues sont notés dans le
    dossier de sortie : une reprise attend les lots déjà partis au lieu de les repayer."""

    DELAI = 300        # s : un lot part au plus tard 5 min après sa première requête
    TAILLE = 40e6      # octets par lot (envoi sur une connexion lente)
    SONDAGE = 30       # s entre deux interrogations d'un lot en cours
    REPRISES = ("overloaded_error", "api_error", "rate_limit_error", "timeout_error")

    def __init__(self, client, sortie: Path):
        self.client = client
        self.fichier_reponses = sortie / "claude_reponses.jsonl"
        self.fichier_lots = sortie / "claude_lots.jsonl"
        self.reponses = {}    # empreinte -> réponse reçue (dict)
        self.attentes = {}    # empreinte -> Future partagée
        self.file = []        # [(empreinte, paramètres)] pas encore envoyés
        self.taille = 0
        self.premier = 0.0
        self.dernier = 0.0    # dernière requête mise en file
        self.actifs = 0       # dossiers en calcul (ni terminés, ni en attente de Claude)
        self.en_cours = {}    # lot -> nombre de requêtes
        self._reveil = asyncio.Event()
        self._taches = set()
        if self.fichier_reponses.exists():
            for ligne in self.fichier_reponses.read_text(encoding="utf-8").splitlines():
                try:
                    r = json.loads(ligne)
                    self.reponses[r["empreinte"]] = r["message"]
                except (ValueError, KeyError):
                    pass  # ligne coupée par un arrêt brutal

    @staticmethod
    def empreinte(params: dict) -> str:
        return hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()[:40]

    def demarrer(self) -> None:
        """Boucle d'envoi, et suivi des lots partis lors d'un lancement précédent."""
        self._lancer(self._boucle())
        if self.fichier_lots.exists():
            for ligne in self.fichier_lots.read_text(encoding="utf-8").splitlines():
                try:
                    lot = json.loads(ligne)
                except ValueError:
                    continue
                restantes = [c for c in lot["requetes"] if c not in self.reponses]
                if restantes:
                    for cle in restantes:
                        self.attentes.setdefault(cle, asyncio.get_running_loop().create_future())
                    self.en_cours[lot["lot"]] = len(restantes)
                    self._lancer(self._suivre(lot["lot"], restantes))

    def arreter(self) -> None:
        for tache in self._taches:
            tache.cancel()

    def _lancer(self, coro) -> None:
        tache = asyncio.create_task(coro)
        self._taches.add(tache)
        tache.add_done_callback(self._taches.discard)

    async def creer(self, **params) -> Message:
        cle = self.empreinte(params)
        resultat = {}
        for _ in range(3):
            if cle in self.reponses:
                return Message.model_validate(self.reponses[cle])
            if cle not in self.attentes:
                self.attentes[cle] = asyncio.get_running_loop().create_future()
                if not self.file:
                    self.premier = time.monotonic()
                self.dernier = time.monotonic()
                self.file.append((cle, params))
                self.taille += sum(len(b.get("source", {}).get("data", ""))
                                   for m in params["messages"] for b in m["content"]
                                   if isinstance(b, dict)) + 20_000
            etat = DOSSIER.get(None)
            if etat is not None:
                etat["attentes"] += 1
                if etat["attentes"] == 1:
                    self.actifs -= 1
            self._reveil.set()
            try:
                resultat = await asyncio.shield(self.attentes[cle])
            finally:
                if etat is not None:
                    etat["attentes"] -= 1
                    if etat["attentes"] == 0:
                        self.actifs += 1
            if resultat["type"] == "succeeded":
                return Message.model_validate(resultat["message"])
            if resultat["type"] == "errored" and resultat.get("erreur_type") not in self.REPRISES:
                raise RuntimeError(f"Claude (lot) : {resultat.get('erreur', resultat)}")
            # surcharge passagère, lot expiré ou annulé : la requête repart dans un autre lot
        raise RuntimeError(f"Claude (lot) : requête non traitée après 3 essais ({resultat})")

    async def _boucle(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._reveil.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
            self._reveil.clear()
            maintenant = time.monotonic()
            # tous les dossiers attendent : on laisse 3 s aux requêtes jumelles (gros plan de
            # la date, préparé en parallèle) pour partir dans le même lot
            if self.file and ((self.actifs <= 0 and maintenant - self.dernier >= 3)
                              or self.taille >= self.TAILLE
                              or maintenant - self.premier >= self.DELAI):
                await self._envoyer()

    async def _envoyer(self) -> None:
        file, self.file, self.taille = self.file, [], 0
        try:
            lot = await self.client.messages.batches.create(
                requests=[{"custom_id": cle, "params": params} for cle, params in file],
                timeout=1800)
        except Exception as e:  # crédit épuisé, clé refusée, réseau…
            for cle, _ in file:
                futur = self.attentes.pop(cle, None)
                if futur and not futur.done():
                    futur.set_exception(e)
            return
        with open(self.fichier_lots, "a", encoding="utf-8") as f:
            f.write(json.dumps({"lot": lot.id, "requetes": [c for c, _ in file],
                                "envoi": datetime.now().isoformat(timespec="seconds")}) + "\n")
        self.en_cours[lot.id] = len(file)
        print(f"LOT_ENVOYE {lot.id} {len(file)} {len(self.en_cours)}", flush=True)
        self._lancer(self._suivre(lot.id, [c for c, _ in file]))

    async def _suivre(self, lot_id: str, cles: list[str]) -> None:
        """Attend la fin du lot, puis remet chaque réponse au dossier qui l'attend."""
        while True:
            try:
                lot = await self.client.messages.batches.retrieve(lot_id)
                if lot.processing_status == "ended":
                    recues = {}
                    async for r in await self.client.messages.batches.results(lot_id):
                        recues[r.custom_id] = r.result
                    break
            except (anthropic.APIConnectionError, anthropic.APITimeoutError,
                    anthropic.InternalServerError, anthropic.RateLimitError):
                pass  # réseau coupé ou service saturé : on réessaie au prochain sondage
            await asyncio.sleep(self.SONDAGE)
        with open(self.fichier_reponses, "a", encoding="utf-8") as f:
            for cle in cles:
                res = recues.get(cle)
                if res is not None and res.type == "succeeded":
                    message = res.message.model_dump(mode="json")
                    self.reponses[cle] = message
                    f.write(json.dumps({"empreinte": cle, "message": message},
                                       ensure_ascii=False) + "\n")
                    sortie = {"type": "succeeded", "message": message}
                elif res is not None and res.type == "errored":
                    erreur = res.error.error
                    sortie = {"type": "errored", "erreur_type": erreur.type,
                              "erreur": erreur.message}
                else:
                    sortie = {"type": res.type if res is not None else "absent"}
                futur = self.attentes.pop(cle, None)
                if futur and not futur.done():
                    futur.set_result(sortie)
        self.en_cours.pop(lot_id, None)
        print(f"LOT_TERMINE {lot_id} {len(cles)} {len(self.en_cours)}", flush=True)


def sans_reflexion() -> dict:

    if MODEL == MODELES["haiku"][0]:
        return {}
    return {"thinking": {"type": "disabled" if MODEL == "claude-sonnet-5" else "between_tools"}}


async def relire_date(client, pdf: Path, ocr: dict, args,
                      pages_claude: list | None = None) -> tuple[str, dict]:
    """Seconde lecture, indépendante, de la date d'expiration (quelques centaines de tokens).
    La page de la CIP vient de la lecture locale, à défaut de celle indiquée par Claude."""
    pages = [int(p) for p, t in ocr["pages"].items() if t == "piece_identite"]
    if not pages:
        pages = [int(p) for p in (pages_claude or []) if str(p).isdigit()
                 and 1 <= int(p) <= ocr["nb_pages"]]
    if not pages:
        return "", {}
    angle = ocr.get("rotations", {}).get(str(pages[0]), 0)
    image = await asyncio.to_thread(gros_plan_date, pdf, pages[0], angle, args.quality)
    # lecture de quelques chiffres : pas besoin de réflexion, et elle déborderait max_tokens
    reponse = await client.creer(
        model=MODEL, max_tokens=300,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                         "data": base64.standard_b64encode(image).decode()}},
            {"type": "text", "text": DATE_CONSIGNE}]}],
        output_config={"format": {"type": "json_schema", "schema": DATE_SCHEMA}},
        **sans_reflexion())
    texte = next(b.text for b in reponse.content if b.type == "text")
    lu = json.loads(texte)
    usage = {"in": reponse.usage.input_tokens, "out": reponse.usage.output_tokens}
    return (lu.get("date_expiration", "") if lu.get("lisible") else ""), usage


PHOTO_SCHEMA = {"type": "object", "additionalProperties": False,
                "required": ["visage_reconnaissable", "remarque"],
                "properties": {"visage_reconnaissable": {"type": "boolean"},
                               "remarque": {"type": "string"}}}
PHOTO_CONSIGNE = """Cette image contient une pièce d'identité (CIP, CNI ou passeport), parfois
scannée de travers. La photo du titulaire est-elle présente et son visage reconnaissable
(on distingue les traits, même si l'image est pâle ou grise) ? Réponds par ce JSON :
{"visage_reconnaissable": true/false, "remarque": ""}"""


async def avis_photo(client, pdf: Path, page: int, angle: int, args) -> tuple[bool, dict]:
    """Deuxième avis, indépendant, avant tout rejet pour « Photo illisible »."""
    image = (await asyncio.to_thread(images_pages, pdf, [page], {page: angle}, 1600,
                                     args.quality))[0][1]
    reponse = await client.creer(
        model=MODEL, max_tokens=200,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                         "data": base64.standard_b64encode(image).decode()}},
            {"type": "text", "text": PHOTO_CONSIGNE}]}],
        output_config={"format": {"type": "json_schema", "schema": PHOTO_SCHEMA}},
        **sans_reflexion())
    lu = json.loads(next(b.text for b in reponse.content if b.type == "text"))
    usage = {"in": reponse.usage.input_tokens, "out": reponse.usage.output_tokens}
    return bool(lu.get("visage_reconnaissable")), usage


async def traiter(client, pdf: Path, ocr: dict, args, sem) -> tuple[dict, dict]:
    nom = pdf.stem.strip()
    officiel = list(ocr.get("officiel", {}))
    row = {"dossier": nom, "pages": ocr["nb_pages"], "pages_envoyees": len(ocr["pages_a_envoyer"]),
           "qr_officiel": ",".join(officiel), "t_local_s": ocr["t_ocr_s"] + ocr.get("t_qr_s", 0),
           "t_api_s": 0.0, "tokens_in": 0, "tokens_out": 0, "cout_usd": 0.0,
           "statut": "ERREUR", "motifs_rejet": "", "detail": "", "rccm": "", "commercial": "",
           "erreur": ""}
    base, doutes = socle(ocr)
    journal = {"dossier": nom, "officiel": ocr.get("officiel", {}), "ocr_local": ocr["champs"],
               "pages": ocr["pages"], "horodatage": datetime.now().isoformat(timespec="seconds")}
    async with sem:
        t0 = time.perf_counter()
        try:
            if CREDIT_EPUISE.is_set():
                raise RuntimeError("Crédit Claude épuisé : dossier non traité, à reprendre")
            pages = ocr["pages_a_envoyer"]
            data = base

            async def lire(pages_lues: list[int]) -> dict:
                """Un appel à Claude sur les pages données ; une seconde tentative si le JSON
                renvoyé est mal formé."""
                # pages de simple contrôle (signature du contrat ; cachet et date d'un RCCM
                # déjà confirmé par le QR) : moins détaillées, donc moins chères
                type_de = lambda n: ocr["pages"].get(str(n), ocr["pages"].get(n, "inconnu"))
                rccm_pages = sorted(n for n in pages_lues if type_de(n) == "rccm")
                cotes = {n: COTE_CONTROLE for n in pages_lues
                         if type_de(n) == "contrat"
                         or (type_de(n) == "rccm" and "rccm" in ocr.get("officiel", {}))
                         # 2e page d'un RCCM de deux pages : cachet et date seulement
                         or (len(rccm_pages) > 1 and n == rccm_pages[-1])}
                cotes |= {n: COTE_IMPRIME for n in pages_lues
                          if type_de(n) in IMPRIMES and n not in cotes}
                cotes |= {n: COTE_PIECE for n in pages_lues if type_de(n) == "piece_identite"}
                images = await asyncio.to_thread(images_pages, pdf, pages_lues,
                                                 ocr.get("rotations", {}), args.max_side,
                                                 args.quality, cotes)
                contenu = []
                for numero, image in images:
                    contenu.append({"type": "text", "text": f"Page {numero}"})
                    contenu.append({"type": "image", "source": {
                        "type": "base64", "media_type": "image/jpeg",
                        "data": base64.standard_b64encode(image).decode()}})
                attendu = "\n".join(
                    f"- page {n} : {ocr['pages'].get(str(n), ocr['pages'].get(n, 'inconnu'))}"
                    for n in pages_lues)
                sures = sections_officielles(ocr)
                officiel = {k: base[k] for k in ("rccm", "ifu") if k in sures}
                indicatif = {k: v for k, v in ocr["champs"].items()
                             if k in ("rccm", "ifu", "apiex") and k not in sures}
                # sections déjà fournies par le QR officiel : pas renvoyées (moins de texte
                # écrit), sauf le cachet et la date du RCCM, qui se lisent sur le papier
                off = ocr.get("officiel", {})
                omis = (["la section rccm, sauf cachet_greffe, date_delivrance et "
                         "reference_verification"]
                        if "rccm" in off else []) + (
                    ["la section ifu"] if "ifu" in off or "apiex" in off else [])
                contenu.append({"type": "text", "text": CONSIGNE.format(
                    officiel=json.dumps(officiel, ensure_ascii=False) if officiel else "(aucun)",
                    indicatif=json.dumps(indicatif, ensure_ascii=False) if indicatif else "(aucune)",
                    attendu=attendu,
                    omettre=("Déjà connues par le registre officiel, ne les renvoie pas dans le "
                             "JSON : " + " ; ".join(omis) + ".") if omis else "")})
                options = {"output_config": {"effort": EFFORT}} if EFFORT else {}
                for essai in (1, 2):
                    # 2e essai : requête légèrement différente (une requête identique
                    # renverrait, en lot, la même réponse mise de côté)
                    rappel = [] if essai == 1 else [{"type": "text", "text": (
                        "Rappel : réponds uniquement par l'objet JSON demandé, complet.")}]
                    reponse = await client.creer(
                        model=MODEL, max_tokens=16000, system=CONSIGNES,
                        messages=[{"role": "user", "content": contenu + rappel}], **options)
                    u = reponse.usage
                    # tokens d'entrée ramenés en « équivalent plein tarif » : l'écriture en cache
                    # coûte 1,25 fois le prix, la lecture 0,1 fois
                    row["tokens_in"] += round(u.input_tokens
                                              + 1.25 * (u.cache_creation_input_tokens or 0)
                                              + 0.1 * (u.cache_read_input_tokens or 0))
                    row["tokens_out"] += u.output_tokens
                    if reponse.stop_reason in ("refusal", "max_tokens"):
                        raise RuntimeError(f"réponse inutilisable ({reponse.stop_reason})")
                    texte = next(b.text for b in reponse.content if b.type == "text")
                    try:
                        return parse_json(texte)
                    except (json.JSONDecodeError, ValueError):
                        if essai == 2:
                            raise

            def compter(usage: dict) -> None:
                row["tokens_in"] += usage.get("in", 0)
                row["tokens_out"] += usage.get("out", 0)

            if pages:
                # CIP localisée : son gros plan part en même temps que la lecture principale
                # (en lot, chaque aller-retour supplémentaire coûte une attente)
                cip_locale = any(t == "piece_identite" for t in ocr["pages"].values())
                tache_date = (asyncio.ensure_future(relire_date(client, pdf, ocr, args))
                              if cip_locale else None)
                try:
                    claude = await lire(pages)
                    # pièce d'identité introuvable dans les pages envoyées : avant de conclure
                    # à une pièce manquante, on montre toutes les pages de tête (une CIP
                    # scannée à l'envers peut avoir été prise pour autre chose)
                    if not (claude["pieces"]["piece_identite"]["presente"]
                            and claude["pieces"]["fiche"]["presente"]):
                        qr = {c.get("page") for c in ocr.get("officiel", {}).values()}
                        tete = [p for p in range(1, min(ocr["nb_pages"], 8) + 1) if p not in qr]
                        if set(tete) - set(pages):
                            claude = await lire(sorted(set(tete) | set(pages)))
                            journal["seconde_passe"] = True
                    if tache_date:
                        date_gros_plan, usage_date = await tache_date
                    else:
                        date_gros_plan, usage_date = await relire_date(
                            client, pdf, ocr, args,
                            claude["pieces"]["piece_identite"].get("pages"))
                finally:
                    if tache_date and not tache_date.done():
                        tache_date.cancel()
                compter(usage_date)
                data, doutes_fusion = fusionner(base, claude, ocr, date_gros_plan)
                doutes += doutes_fusion
                # photo jugée illisible : deuxième avis avant tout rejet
                pid_pages = ([int(p) for p, t in ocr["pages"].items() if t == "piece_identite"]
                             or [int(p) for p in data["pieces"]["piece_identite"]["pages"]
                                 if str(p).isdigit() and 1 <= int(p) <= ocr["nb_pages"]])
                if (data["pieces"]["piece_identite"]["presente"] and pid_pages
                        and not data["piece_identite"]["photo_lisible"]):
                    angle = ocr.get("rotations", {}).get(str(pid_pages[0]),
                                                         ocr.get("rotations", {}).get(pid_pages[0], 0))
                    lisible, usage_photo = await avis_photo(client, pdf, pid_pages[0], angle, args)
                    compter(usage_photo)
                    journal["avis_photo"] = lisible
                    if lisible:
                        doutes.append(PHOTO_CONTESTEE)
                row["t_api_s"] = round(time.perf_counter() - t0, 2)
                # coût estimé : en lot, tous les tokens sont à moitié prix
                row["cout_usd"] = round(REMISE * (row["tokens_in"] * PRICE_IN
                                                  + row["tokens_out"] * PRICE_OUT), 5)
            data["analyse"]["champs_incertains"] = list(dict.fromkeys(
                data["analyse"]["champs_incertains"] + doutes))
            sures = {p for p in ("rccm", "ifu") if p in ocr.get("officiel", {})}
            if "apiex" in ocr.get("officiel", {}):
                sures.add("ifu")
            decision = apply_rules(data, date.today(), nom, sources_officielles=sures)
            row["commercial"] = referentiel.commercial(data["fiche"]["commercial"])
            row.update(statut=decision["statut"],
                       rccm=data["rccm"]["numero"] or data["ifu"]["rccm"],
                       motifs_rejet=" | ".join(decision["motifs_rejet"]),
                       detail=" | ".join([f"{b['motif']} : {b['detail']}"
                                          for b in decision["bloquants"]] + decision["vigilance"]))
            journal |= {"decision": decision, "extraction": data}
        except Exception as e:  # un dossier en erreur ne bloque pas le lot
            row["erreur"] = f"{type(e).__name__}: {e}"
            journal["erreur"] = row["erreur"]
            if "credit balance is too low" in str(e) and not CREDIT_EPUISE.is_set():
                # inutile d'insister : les dossiers suivants s'arrêtent aussitôt, sans frais,
                # et seront traités à la reprise (les erreurs sont retentées)
                CREDIT_EPUISE.set()
                print("CREDIT_EPUISE Crédit Claude épuisé : rechargez le compte puis reprenez "
                      "le contrôle.", flush=True)
    print(f"  {row['statut']:<11} {row['pages_envoyees']:>2}/{row['pages']:>2}p -> Claude | "
          f"QR: {row['qr_officiel'] or 'aucun':<10} {row['cout_usd']:.5f}$  {nom}"
          + (f"  !! {row['erreur'][:200]}" if row["erreur"] else ""), flush=True)
    return row, journal


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dossier", default=str(ROOT / "Dossier test"))
    ap.add_argument("--only", nargs="*", default=[])
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--concurrency", type=int, default=5)
    ap.add_argument("--jobs", type=int, default=8)
    # Claude réduit lui-même toute image plus grande : inutile d'envoyer davantage
    ap.add_argument("--max-side", type=int, default=1568)
    ap.add_argument("--quality", type=int, default=80)
    ap.add_argument("--modele", choices=list(MODELES), default="sonnet")
    ap.add_argument("--effort", choices=["low", "medium", "high"], default="low",
                    help="niveau de réflexion (ignoré pour Haiku)")
    ap.add_argument("--direct", action="store_true",
                    help="appels immédiats au tarif normal, au lieu des lots à -50 %% (essais)")
    ap.add_argument("--reprendre", default="",
                    help="dossier de sortie d'un lancement interrompu, à compléter")
    ap.add_argument("--recontroler", action="store_true",
                    help="contrôler aussi les dossiers déjà contrôlés lors d'un lancement précédent")
    args = ap.parse_args()

    global MODEL, PRICE_IN, PRICE_OUT, EFFORT, REMISE
    MODEL, PRICE_IN, PRICE_OUT = MODELES[args.modele]
    EFFORT = None if args.modele == "haiku" else args.effort
    REMISE = 1.0 if args.direct else REMISE_LOT

    pdfs = sorted(Path(args.dossier).glob("*.pdf"))
    if args.only:
        pdfs = [p for p in pdfs if p.stem.strip() in args.only]
    if args.limit:
        pdfs = pdfs[:args.limit]

    # reprise : un lancement interrompu reprend là où il s'était arrêté, avec sa sélection
    if args.reprendre:
        out = Path(args.reprendre)
        selection = out / "selection.json"
        if selection.exists():
            noms = set(json.loads(selection.read_text(encoding="utf-8")))
            pdfs = [p for p in pdfs if p.stem.strip() in noms]
        else:  # lancement antérieur à la sélection enregistrée
            pdfs = pdfs[:MAX_DOSSIERS]
    else:
        # dossiers déjà contrôlés lors d'un lancement précédent (même PDF) : sautés
        pdfs, sautes = registre.a_controler(pdfs, args.recontroler)
        if sautes:
            print(f"{len(sautes)} dossier(s) déjà contrôlé(s) lors d'un lancement précédent : "
                  "ignorés", flush=True)
        if len(pdfs) > MAX_DOSSIERS:
            print(f"{len(pdfs)} dossiers à contrôler : seuls les {MAX_DOSSIERS} premiers sont "
                  "traités (limite par lancement)", flush=True)
            pdfs = pdfs[:MAX_DOSSIERS]
        out = ROOT / "sorties" / f"controle_{datetime.now():%Y%m%d_%H%M%S}"
        out.mkdir(parents=True)
        (out / "selection.json").write_text(json.dumps([p.stem.strip() for p in pdfs],
                                                       ensure_ascii=False), encoding="utf-8")
    journal_fichier = out / "resultats.jsonl"
    faits = {}
    if journal_fichier.exists():
        for ligne in journal_fichier.read_text(encoding="utf-8").splitlines():
            j = json.loads(ligne)
            if "ligne" in j and not j.get("erreur"):
                faits[j["dossier"]] = j
        # on réécrit sans les dossiers en erreur, qui seront retentés
        journal_fichier.write_text("".join(json.dumps(j, ensure_ascii=False) + "\n"
                                           for j in faits.values()), encoding="utf-8")
    a_faire = [p for p in pdfs if p.stem.strip() not in faits]
    total = len(faits) + len(a_faire)
    print(f"DOSSIER_SORTIE {out}", flush=True)
    print(f"{len(a_faire)} dossier(s) à traiter"
          + (f", {len(faits)} déjà faits (reprise)" if faits else ""), flush=True)

    t0 = time.perf_counter()
    client = anthropic.AsyncAnthropic(api_key=load_api_key(), max_retries=4, timeout=600)
    if args.direct:
        appel = Direct(client)
        sem = asyncio.Semaphore(args.concurrency)
    else:
        # en lot, tous les dossiers attendent leur réponse en même temps : pas de limite
        appel = LotClaude(client, out)
        appel.demarrer()
        sem = asyncio.Semaphore(10 ** 6)
    print(f"MODE {'direct' if args.direct else 'lot'} {MODEL}", flush=True)
    boucle = asyncio.get_running_loop()
    termines = len(faits)
    lus = 0

    # chaîne continue : chaque dossier part chez Claude dès que sa lecture locale est finie,
    # au lieu d'attendre la fin de la lecture de tout le lot
    with ProcessPoolExecutor(max_workers=args.jobs) as pool:
        async def un_dossier(pdf: Path):
            nonlocal termines, lus
            DOSSIER.set({"attentes": 0})
            appel.actifs += 1
            try:
                try:
                    ocr = await boucle.run_in_executor(pool, _preparer, str(pdf))
                except Exception as e:  # PDF abîmé, illisible…
                    row = {"dossier": pdf.stem.strip(), "pages": 0, "pages_envoyees": 0,
                           "qr_officiel": "", "t_local_s": 0, "t_api_s": 0, "tokens_in": 0,
                           "tokens_out": 0, "cout_usd": 0.0, "statut": "ERREUR",
                           "motifs_rejet": "", "detail": "", "rccm": "", "commercial": "",
                           "erreur": f"lecture du PDF impossible ({type(e).__name__}: {e})"}
                    journal = {"dossier": row["dossier"], "erreur": row["erreur"]}
                else:
                    lus += 1
                    print(f"LECTURE_LOCALE {len(faits) + lus}/{total}", flush=True)
                    row, journal = await traiter(appel, pdf, ocr, args, sem)
            finally:
                appel.actifs -= 1
            journal["ligne"] = row
            # empreinte du PDF : un prochain lancement saute ce dossier s'il n'a pas changé
            journal["empreinte_pdf"] = registre.empreinte_pdf(pdf)
            with open(journal_fichier, "a", encoding="utf-8") as f:
                f.write(json.dumps(journal, ensure_ascii=False) + "\n")
            termines += 1
            print(f"PROGRESSION {termines}/{total}", flush=True)
            return journal

        try:
            nouveaux = await asyncio.gather(*(un_dossier(p) for p in a_faire))
        finally:
            if isinstance(appel, LotClaude):
                appel.arreter()
    wall = time.perf_counter() - t0

    journaux = list(faits.values()) + nouveaux
    rows = [j["ligne"] for j in journaux]
    if not rows:
        print("Aucun nouveau dossier à contrôler (dossier vide, ou PDF tous déjà contrôlés).")
        return
    avec_qr = sum(1 for r in rows if r["qr_officiel"])
    envoyees = sum(r["pages_envoyees"] for r in rows)
    total_pages = max(sum(r["pages"] for r in rows), 1)
    t_local = sum(r["t_local_s"] for r in rows) / max(args.jobs, 1)
    doublons = marquer_doublons(rows)
    with open(out / "resultats.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]), delimiter=";")
        w.writeheader()
        w.writerows(rows)

    ok = [r for r in rows if not r["erreur"]]
    cout = sum(r["cout_usd"] for r in ok)
    unitaire = cout / max(len(ok), 1)
    lignes = [
        f"Dossiers : {len(rows)}  (réussis {len(ok)}, erreurs {len(rows) - len(ok)})",
        f"Durée totale : {wall / 60:.1f} min (lecture locale ~{t_local / 60:.1f} min, "
        f"menée en parallèle des appels à Claude, attente des lots comprise)",
        f"QR officiel exploité : {avec_qr}/{len(rows)} dossiers",
        f"Pages envoyées à Claude : {envoyees}/{total_pages} "
        f"({100 * envoyees / total_pages:.0f} %)",
        f"Coût estimé : {cout:.3f} $ au total, {unitaire:.5f} $ / dossier ({MODEL}, "
        f"{'appels directs' if REMISE == 1 else 'lots à -50 %'})",
        f"Projection : 150 dossiers/nuit ~{unitaire * 150:.2f} $ | "
        f"3 000 dossiers/mois ~{unitaire * 3000:.0f} $",
        "Statuts : " + ", ".join(f"{s} {sum(r['statut'] == s for r in rows)}"
                                 for s in ["VALIDÉ", "À VÉRIFIER", "REJETÉ", "ERREUR"]),
    ]
    compte = {}
    for r in ok:
        for m in filter(None, r["motifs_rejet"].split(" | ")):
            compte[m] = compte.get(m, 0) + 1
    if compte:
        lignes.append("Motifs de rejet : " + ", ".join(
            f"{m} {n}" for m, n in sorted(compte.items(), key=lambda x: -x[1])))
    for noms in doublons:
        lignes.append("Doublon possible (même RCCM) : " + " / ".join(noms))
    for r in rows:
        if r["erreur"]:
            lignes.append(f"ERREUR {r['dossier']} : {r['erreur'][:300]}")
    if ok:
        lignes.append("Échantillon de contrôle (5 %) : " + ", ".join(
            r["dossier"] for r in random.sample(ok, max(1, round(len(ok) * 0.05)))))
    try:
        fichier_sp = sharepoint.remplir(out, journaux)
        if fichier_sp:
            lignes.append(f"Fichier SharePoint (dossiers validés) : {fichier_sp.name}")
    except (OSError, ValueError) as e:  # modèle ouvert dans Excel, absent…
        lignes.append(f"Fichier SharePoint non créé : {e}")
    resume = "\n".join(lignes)
    (out / "synthese.txt").write_text(resume, encoding="utf-8")
    print("\n" + resume)


if __name__ == "__main__":
    asyncio.run(main())
