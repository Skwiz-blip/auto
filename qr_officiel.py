"""Récupération des pièces officielles par leur QR code (portail de l'État béninois).

Le RCCM, l'IFU et la carte professionnelle APIEx portent un QR renvoyant vers
services.monentreprise.bj. Le portail rend le document d'origine en PDF avec son texte :
on obtient les valeurs exactes, sans OCR ni modèle.

Les PDF récupérés sont mis en cache (sorties/cache_qr) pour ne pas réinterroger le
portail à chaque exécution.
"""

import contextlib
import io
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

import cv2
import fitz
import numpy as np
from PIL import Image
from pyzbar import pyzbar

import chemins

ROOT = Path(__file__).parent
CACHE = chemins.SORTIES / "cache_qr"
AGENT = "Mozilla/5.0 (controle dossiers marchands Celtiis Cash)"
DELAI = 45
PAUSE = 0.5  # secondes entre deux consultations du portail (par processus)


@contextlib.contextmanager
def _sans_bruit():
    """zbar écrit un avertissement par image sur la sortie d'erreur du système : on la coupe
    le temps du décodage (les erreurs Python, elles, continuent de passer)."""
    try:
        copie = os.dup(2)
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 2)
    except OSError:
        yield
        return
    try:
        yield
    finally:
        os.dup2(copie, 2)
        os.close(devnull)
        os.close(copie)


def _decoder(image) -> list[str]:
    with _sans_bruit():
        codes = pyzbar.decode(image)
    return [c.data.decode("utf-8", "replace") for c in codes]


def lire_qr(page, largeurs=(1800, 2600, 3600)) -> list[str]:
    """URLs des QR codes d'une page, en insistant (résolutions, seuillages, netteté).

    Un QR se lit dans tous les sens : inutile de faire pivoter l'image.
    """
    for largeur in largeurs:
        zoom = largeur / max(page.rect.width, page.rect.height)
        gris = np.array(Image.open(io.BytesIO(
            page.get_pixmap(matrix=fitz.Matrix(zoom, zoom)).tobytes("png"))).convert("L"))
        net = cv2.addWeighted(gris, 1.8, cv2.GaussianBlur(gris, (0, 0), 3), -0.8, 0)
        variantes = [gris,
                     cv2.threshold(gris, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1],
                     cv2.adaptiveThreshold(gris, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                           cv2.THRESH_BINARY, 31, 10),
                     net]
        for image in variantes:
            urls = [u for u in _decoder(image) if u.startswith("http")]
            if urls:
                return urls
        # dernier recours à cette résolution : le détecteur d'OpenCV, plus tolérant au flou
        ok, decodes, *_ = cv2.QRCodeDetector().detectAndDecodeMulti(gris)
        if ok:
            urls = [d for d in decodes if d.startswith("http")]
            if urls:
                return urls
    return []


def telecharger(url: str) -> str:
    """Texte du PDF officiel ; "" si le portail ne répond pas ou renvoie autre chose."""
    CACHE.mkdir(parents=True, exist_ok=True)
    cle = re.sub(r"[^A-Za-z0-9]", "_", url)[-80:]
    fichier = CACHE / f"{cle}.pdf"
    try:
        if fichier.exists():
            data = fichier.read_bytes()
        else:
            requete = urllib.request.Request(url, headers={"User-Agent": AGENT})
            try:
                with urllib.request.urlopen(requete, timeout=DELAI) as reponse:
                    data = reponse.read()
            finally:
                time.sleep(PAUSE)  # ménager le portail de l'État sur les gros lots
            if data[:4] == b"%PDF":
                fichier.write_bytes(data)
        if data[:4] != b"%PDF":
            return ""
        with fitz.open(stream=data, filetype="pdf") as doc:
            return "\n".join(p.get_text() for p in doc)
    except (urllib.error.URLError, OSError, ValueError, RuntimeError):
        return ""


def _apres(texte: str, etiquette: str, longueur: int = 90) -> str:
    """Valeur qui suit une étiquette, sur la même ligne ou la suivante."""
    m = re.search(etiquette + r"\s*:?\s*\n?\s*(.{2," + str(longueur) + r"})", texte, re.IGNORECASE)
    return " ".join(m.group(1).split()) if m else ""


def numero_benin(brut: str) -> str:
    """Numéro au format béninois actuel, 10 chiffres commençant par 01 : « +229 94 24 34 04 »
    -> « 0194243404 » (depuis 2024, les anciens numéros à 8 chiffres prennent le préfixe 01).
    Chaîne vide si ce n'est pas un numéro béninois reconnaissable."""
    chiffres = re.sub(r"\D", "", brut or "")
    if chiffres.startswith("229") and len(chiffres) in (11, 13):
        chiffres = chiffres[3:]
    if len(chiffres) == 8:
        chiffres = "01" + chiffres
    return chiffres if re.fullmatch(r"01\d{8}", chiffres) else ""


def analyser(texte: str) -> dict:
    """Champs utiles d'un extrait RCCM, d'une attestation IFU ou d'une carte APIEx."""
    champs = {}
    if m := re.search(r"RB\s*/?\s*[A-Z]{2,4}\s*/?\s*\d{2}\s*[A-Z]\s*\d{3,6}", texte.upper()):
        champs["rccm"] = " ".join(m.group().split())
    if m := re.search(r"\b\d{13}\b", texte):
        champs["ifu"] = m.group()
    # « TEL.: +22994243404 », « Tel : 0197857021 », « Téléphone : +229 01 97 … »
    if m := re.search(r"T[ée]l(?:[ée]phone)?\.?\s*:?\s*(\+?\d[\d .]{6,18}\d)", texte, re.IGNORECASE):
        champs["telephone"] = numero_benin(m.group(1))

    if "EXTRAIT DU REGISTRE" in texte.upper():
        champs["type"] = "rccm"
        champs["nom"] = _apres(texte, r"NOM PATRONYMIQUE", 40)
        champs["prenoms"] = _apres(texte, r"PRENOM\(S\)", 60)
        champs["enseigne"] = _apres(texte, r"\nENSEIGNE", 60)
        champs["nom_commercial"] = _apres(texte, r"NOM COMMERCIAL", 60)
        champs["nationalite"] = _apres(texte, r"NATIONALITE", 30)
        champs["activite"] = _apres(texte, r"ACTIVITE EXERCEE", 120)
        if m := re.search(r"NAISSANCE\s*\n?\s*Né\(e\) le\s*([\d-]+)\s*à\s*(.+)", texte):
            champs["date_naissance"] = m.group(1)
            champs["lieu_naissance"] = m.group(2).strip()[:60]
    elif "ATTESTATION D'IMMATRICULATION" in texte.upper():
        champs["type"] = "ifu"
        champs["nom_etablissement"] = " ".join(texte.strip().split("\n")[0].split())[:60]
        champs["categorie"] = _apres(texte, r"Catégorie", 60)
        champs["regime_fiscal"] = _apres(texte, r"Régime fiscal", 60)
        champs["centre_impots"] = _apres(texte, r"Centre des impôts", 60)
        if m := re.search(r"RCCM\s*:?\s*\n?\s*(RB[^\n]{5,40})", texte):
            champs["rccm"] = " ".join(m.group(1).split())
    elif "CARTE PROFESSIONNELLE" in texte.upper():
        champs["type"] = "apiex"
        lignes = [l.strip() for l in texte.strip().split("\n") if l.strip()]
        if len(lignes) > 3:
            champs["prenoms"] = lignes[1][:60]
            champs["nom"] = lignes[2][:40]
            champs["nom_etablissement"] = lignes[3][:60]
        if m := re.search(r"Expire le\s*([\d-]{8,10})", texte):
            j, mo, a = re.split(r"[-/]", m.group(1))
            champs["date_expiration"] = f"{a}-{mo}-{j}"
    return {k: v for k, v in champs.items() if v}


def pieces_officielles(pdf: Path, pages_utiles: list[int]) -> dict:
    """{type de pièce: champs officiels} pour les pages dont le QR a répondu."""
    officiel = {}
    with fitz.open(pdf) as doc:
        for numero in pages_utiles:
            if numero > doc.page_count:
                continue
            if "rccm" in officiel and ("ifu" in officiel or "apiex" in officiel):
                break  # tout ce qu'on cherchait est trouvé
            for url in lire_qr(doc[numero - 1]):
                texte = telecharger(url)
                if not texte:
                    continue
                champs = analyser(texte)
                if champs.get("type"):
                    champs["source"] = url
                    champs["page"] = numero
                    officiel.setdefault(champs.pop("type"), champs)
    return officiel
