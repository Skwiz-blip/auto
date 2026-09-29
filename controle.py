import argparse
import asyncio
import base64
import csv
import io
import json
import random
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import date, datetime
from pathlib import Path

import anthropic
import fitz
from PIL import Image

from bench_dossiers import (ROOT, SCHEMA, SYSTEM, _norm, _rccm, _template, apply_rules,
                            conform, load_api_key, marquer_doublons, parse_json)
from ocr_local import lire_dossier
from qr_officiel import pieces_officielles


def images_pages(pdf: Path, pages: list[int], rotations: dict, max_side: int, quality: int):
    """Rend uniquement les pages demandées, en redressant celles qui sont pivotées."""
    sorties = []
    with fitz.open(pdf) as doc:
        for numero in pages:
            page = doc[numero - 1]
            zoom = max_side / max(page.rect.width, page.rect.height)
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace=fitz.csRGB)
            data = pix.tobytes("jpeg", jpg_quality=quality)
            angle = rotations.get(str(numero), rotations.get(numero, 0))
            if angle:
                img = Image.open(io.BytesIO(data)).rotate(-angle, expand=True)
                tampon = io.BytesIO()
                img.save(tampon, format="JPEG", quality=quality)
                data = tampon.getvalue()
            sorties.append((numero, data))
    return sorties


# modèles disponibles : prix en $ par token (entrée, sortie)
MODELES = {
    "haiku": ("claude-haiku-4-5", 1.00 / 1e6, 5.00 / 1e6),
    "sonnet": ("claude-sonnet-5", 2.00 / 1e6, 10.00 / 1e6),
}
MODEL, PRICE_IN, PRICE_OUT = MODELES["haiku"]
EFFORT = None  # niveau de réflexion (non accepté par Haiku 4.5)
# lectures de la date d'expiration en désaccord : si toutes sont postérieures à cette marge
# (en jours), la pièce est valide quelle que soit la bonne lecture (règle KIK : valide tant
# que la date n'est pas passée)
MARGE_JOURS = 0

def _consignes(sans: tuple[str, ...]) -> list[dict]:
    """Instructions système avec un modèle JSON réduit : les sections déjà fournies par le
    QR officiel ne sont pas redemandées (moins de texte généré, donc moins cher)."""
    schema = {**SCHEMA, "properties": {k: v for k, v in SCHEMA["properties"].items()
                                       if k not in sans}}
    fiche = dict(schema["properties"]["fiche"])
    fiche["properties"] = {k: v for k, v in fiche["properties"].items()
                           if not k.startswith(("case_", "autorisee"))}
    schema["properties"]["fiche"] = fiche
    entete = SYSTEM.split("Réponds uniquement")[0]
    texte = (entete + "Réponds uniquement avec un objet JSON (sans texte autour) ayant exactement "
             "cette structure :\n" + json.dumps(_template(schema), ensure_ascii=False, indent=1))
    # partie fixe d'une requête à l'autre : mise en cache (lectures facturées à 10 %)
    return [{"type": "text", "text": texte, "cache_control": {"type": "ephemeral"}}]


CONSIGNES = {
    "complet": _consignes(()),
    "sans_rccm": _consignes(("rccm",)),
    "sans_ifu": _consignes(("ifu",)),
    "sans_rccm_ifu": _consignes(("rccm", "ifu")),
}


CONSIGNE = """Tu reçois seulement les pages qu'une lecture automatique n'a pas pu traiter :
la fiche de création (manuscrite), la pièce d'identité, et éventuellement des pages non
identifiées. Chaque image est précédée de son numéro de page dans le dossier.

Déjà obtenu de façon fiable (QR officiel de l'État et lecture locale) — reprends ces
valeurs telles quelles, ne les corrige pas :
{deja_lu}

Pages fournies :
{attendu}

Consignes particulières :
- Sur la pièce d'identité, lis la date d'expiration chiffre par chiffre. Si tu n'es pas
  certain, mets "" et signale "piece_identite.date_expiration" dans champs_incertains.
- Sur la fiche manuscrite, tout mot que tu ne déchiffres pas avec certitude doit rester
  vide et figurer dans champs_incertains. N'invente jamais un nom.
- champs_incertains est important : il déclenche un contrôle humain, ce qui est toujours
  préférable à une valeur inventée."""


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
    return ocr


def socle(ocr: dict) -> tuple[dict, list[str]]:
    """JSON de départ : QR officiel d'abord, OCR local ensuite. Renvoie aussi les doutes."""
    data = conform({}, SCHEMA)
    doutes = []
    officiel = ocr.get("officiel", {})

    rccm = officiel.get("rccm", {})
    for source, cible in (("rccm", "numero"), ("nom", "nom"), ("prenoms", "prenoms"),
                          ("enseigne", "enseigne"), ("nom_commercial", "nom_commercial"),
                          ("nationalite", "nationalite"), ("activite", "activite"),
                          ("date_naissance", "date_naissance"),
                          ("lieu_naissance", "lieu_naissance")):
        if rccm.get(source):
            data["rccm"][cible] = rccm[source]
    if rccm:
        data["pieces"]["rccm"].update(presente=True, lisible=True, pages=[rccm.get("page", 0)],
                                      remarque="vérifié par QR officiel")
        data["rccm"]["cachet_greffe"] = True

    ifu = officiel.get("ifu") or officiel.get("apiex", {})
    for source, cible in (("ifu", "numero"), ("rccm", "rccm"), ("nom", "nom"),
                          ("prenoms", "prenoms"), ("nom_etablissement", "nom_etablissement"),
                          ("categorie", "categorie"), ("regime_fiscal", "regime_fiscal"),
                          ("centre_impots", "centre_impots")):
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
                     "piece_identite": {"npi": ("piece_identite", "numero")},
                     }.get(piece, {}).get(cle)
            if cible and isinstance(valeur, str) and not data[cible[0]][cible[1]]:
                data[cible[0]][cible[1]] = valeur
    # le papier et le portail doivent désigner le même registre
    local_rccm = ocr["champs"].get("rccm", {}).get("rccm", "")
    if rccm.get("rccm") and local_rccm and _rccm(local_rccm) != _rccm(rccm["rccm"]):
        doutes.append(f"numéro RCCM du papier ({local_rccm}) différent du registre officiel "
                      f"({rccm['rccm']})")
    for piece in ("fiche", "rccm", "ifu", "piece_identite"):
        pages = [int(p) for p, t in ocr["pages"].items() if t == piece]
        if pages and not data["pieces"][piece]["presente"]:
            data["pieces"][piece].update(presente=True, lisible=True, pages=pages)
    return data, doutes


def fusionner(base: dict, claude: dict, ocr: dict, date_gros_plan: str = "") -> tuple[dict, list[str]]:
    """Le socle (QR/OCR) l'emporte ; Claude comble les vides. Date de CIP : double lecture."""
    doutes = []
    data = json.loads(json.dumps(base))
    for section in ("fiche", "rccm", "ifu", "piece_identite", "pieces", "analyse"):
        for champ, valeur in claude[section].items():
            actuel = data[section][champ]
            if isinstance(valeur, str):
                if valeur and not actuel:
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
    votes = {}
    for source, valeur in lectures.items():
        if valeur:
            votes.setdefault(_norm(valeur), []).append((source, valeur))
    retenue = max(votes.values(), key=len, default=[])
    dates = []
    for valeur in lectures.values():
        try:
            dates.append(date.fromisoformat(valeur))
        except (TypeError, ValueError):
            pass
    aujourd_hui = date.today()
    if len(retenue) >= 2:
        data["piece_identite"]["date_expiration"] = retenue[0][1]
    elif dates and all((d - aujourd_hui).days >= MARGE_JOURS for d in dates):
        # toutes les lectures donnent une carte valide longtemps encore : un chiffre de
        # désaccord ne change pas la décision ; on retient la plus proche par prudence
        data["piece_identite"]["date_expiration"] = min(dates).isoformat()
    elif len(dates) >= 2 and all(d < aujourd_hui for d in dates):
        # toutes les lectures donnent une carte déjà expirée : on retient la plus favorable
        data["piece_identite"]["date_expiration"] = max(dates).isoformat()
    else:
        data["piece_identite"]["date_expiration"] = ""
        detail = ", ".join(f"{s} « {v} »" for s, v in lectures.items() if v) or "aucune lecture"
        doutes.append(f"date d'expiration de la pièce d'identité non confirmée ({detail})")
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
Lis UNIQUEMENT la date d'expiration ("Expire le ..."), chiffre par chiffre.
Réponds par ce JSON, sans rien d'autre :
{"date_expiration": "AAAA-MM-JJ", "lisible": true/false, "remarque": ""}
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
    img = Image.open(io.BytesIO(data))
    if angle:
        img = img.rotate(-angle, expand=True)
    largeur, hauteur = img.size
    bas = img.crop((0, int(hauteur * 0.55), largeur, hauteur))
    if bas.width > 1400:
        bas = bas.resize((1400, int(bas.height * 1400 / bas.width)), Image.LANCZOS)
    tampon = io.BytesIO()
    bas.convert("RGB").save(tampon, format="JPEG", quality=quality)
    return tampon.getvalue()


async def relire_date(client, pdf: Path, ocr: dict, args) -> tuple[str, dict]:
    """Seconde lecture, indépendante, de la date d'expiration (quelques centaines de tokens)."""
    pages = [int(p) for p, t in ocr["pages"].items() if t == "piece_identite"]
    if not pages:
        return "", {}
    angle = ocr.get("rotations", {}).get(str(pages[0]), 0)
    image = await asyncio.to_thread(gros_plan_date, pdf, pages[0], angle, args.quality)
    options = {}
    if MODEL != MODELES["haiku"][0]:
        # lecture de quelques chiffres : pas besoin de réflexion, et elle déborderait max_tokens
        options["thinking"] = {"type": "disabled"}
    reponse = await client.messages.create(
        model=MODEL, max_tokens=300,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                         "data": base64.standard_b64encode(image).decode()}},
            {"type": "text", "text": DATE_CONSIGNE}]}],
        output_config={"format": {"type": "json_schema", "schema": DATE_SCHEMA}}, **options)
    texte = next(b.text for b in reponse.content if b.type == "text")
    lu = json.loads(texte)
    usage = {"in": reponse.usage.input_tokens, "out": reponse.usage.output_tokens}
    return (lu.get("date_expiration", "") if lu.get("lisible") else ""), usage


async def traiter(client, pdf: Path, ocr: dict, args, sem) -> tuple[dict, dict]:
    nom = pdf.stem.strip()
    officiel = list(ocr.get("officiel", {}))
    row = {"dossier": nom, "pages": ocr["nb_pages"], "pages_envoyees": len(ocr["pages_a_envoyer"]),
           "qr_officiel": ",".join(officiel), "t_local_s": ocr["t_ocr_s"] + ocr.get("t_qr_s", 0),
           "t_api_s": 0.0, "tokens_in": 0, "tokens_out": 0, "cout_usd": 0.0,
           "statut": "ERREUR", "motifs_rejet": "", "detail": "", "rccm": "", "erreur": ""}
    base, doutes = socle(ocr)
    journal = {"dossier": nom, "officiel": ocr.get("officiel", {}), "ocr_local": ocr["champs"],
               "pages": ocr["pages"], "horodatage": datetime.now().isoformat(timespec="seconds")}
    async with sem:
        t0 = time.perf_counter()
        try:
            pages = ocr["pages_a_envoyer"]
            data = base

            async def lire(pages_lues: list[int]) -> dict:
                """Un appel à Claude sur les pages données ; une seconde tentative si le JSON
                renvoyé est mal formé."""
                images = await asyncio.to_thread(images_pages, pdf, pages_lues,
                                                 ocr.get("rotations", {}), args.max_side,
                                                 args.quality)
                contenu = []
                for numero, image in images:
                    contenu.append({"type": "text", "text": f"Page {numero}"})
                    contenu.append({"type": "image", "source": {
                        "type": "base64", "media_type": "image/jpeg",
                        "data": base64.standard_b64encode(image).decode()}})
                attendu = "\n".join(
                    f"- page {n} : {ocr['pages'].get(str(n), ocr['pages'].get(n, 'inconnu'))}"
                    for n in pages_lues)
                deja = {k: v for k, v in base.items() if k in ("rccm", "ifu")}
                contenu.append({"type": "text", "text": CONSIGNE.format(
                    deja_lu=json.dumps(deja, ensure_ascii=False), attendu=attendu)})
                off = ocr.get("officiel", {})
                a_rccm, a_ifu = "rccm" in off, ("ifu" in off or "apiex" in off)
                variante = ("sans_rccm_ifu" if a_rccm and a_ifu else "sans_rccm" if a_rccm
                            else "sans_ifu" if a_ifu else "complet")
                options = {"output_config": {"effort": EFFORT}} if EFFORT else {}
                for essai in (1, 2):
                    reponse = await client.messages.create(
                        model=MODEL, max_tokens=16000, system=CONSIGNES[variante],
                        messages=[{"role": "user", "content": contenu}], **options)
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

            if pages:
                claude = await lire(pages)
                # pièce d'identité introuvable dans les pages envoyées : avant de conclure à une
                # pièce manquante, on montre toutes les pages de tête (une CIP scannée à
                # l'envers peut avoir été prise pour autre chose)
                if not claude["pieces"]["piece_identite"]["presente"]:
                    qr = {c.get("page") for c in ocr.get("officiel", {}).values()}
                    tete = [p for p in range(1, min(ocr["nb_pages"], 8) + 1) if p not in qr]
                    if set(tete) - set(pages):
                        claude = await lire(sorted(set(tete) | set(pages)))
                        journal["seconde_passe"] = True
                row["t_api_s"] = round(time.perf_counter() - t0, 2)
                row["cout_usd"] = round(row["tokens_in"] * PRICE_IN
                                        + row["tokens_out"] * PRICE_OUT, 5)
                date_gros_plan, usage_date = await relire_date(client, pdf, ocr, args)
                if usage_date:
                    row["tokens_in"] += usage_date["in"]
                    row["tokens_out"] += usage_date["out"]
                    row["cout_usd"] = round(row["tokens_in"] * PRICE_IN
                                            + row["tokens_out"] * PRICE_OUT, 5)
                    row["t_api_s"] = round(time.perf_counter() - t0, 2)
                data, doutes_fusion = fusionner(base, claude, ocr, date_gros_plan)
                doutes += doutes_fusion
            data["analyse"]["champs_incertains"] = list(dict.fromkeys(
                data["analyse"]["champs_incertains"] + doutes))
            sures = {p for p in ("rccm", "ifu") if p in ocr.get("officiel", {})}
            if "apiex" in ocr.get("officiel", {}):
                sures.add("ifu")
            decision = apply_rules(data, date.today(), nom, sources_officielles=sures,
                                   photo_bloquante=False)
            row.update(statut=decision["statut"],
                       rccm=data["rccm"]["numero"] or data["ifu"]["rccm"],
                       motifs_rejet=" | ".join(decision["motifs_rejet"]),
                       detail=" | ".join([f"{b['motif']} : {b['detail']}"
                                          for b in decision["bloquants"]] + decision["vigilance"]))
            journal |= {"decision": decision, "extraction": data}
        except Exception as e:  # un dossier en erreur ne bloque pas le lot
            row["erreur"] = f"{type(e).__name__}: {e}"
            journal["erreur"] = row["erreur"]
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
    ap.add_argument("--max-side", type=int, default=1800)
    ap.add_argument("--quality", type=int, default=80)
    ap.add_argument("--modele", choices=list(MODELES), default="haiku")
    ap.add_argument("--effort", choices=["low", "medium", "high"], default="low",
                    help="niveau de réflexion (ignoré pour Haiku)")
    ap.add_argument("--reprendre", default="",
                    help="dossier de sortie d'un lancement interrompu, à compléter")
    args = ap.parse_args()

    global MODEL, PRICE_IN, PRICE_OUT, EFFORT
    MODEL, PRICE_IN, PRICE_OUT = MODELES[args.modele]
    EFFORT = None if args.modele == "haiku" else args.effort

    pdfs = sorted(Path(args.dossier).glob("*.pdf"))
    if args.only:
        pdfs = [p for p in pdfs if p.stem.strip() in args.only]
    if args.limit:
        pdfs = pdfs[:args.limit]

    # reprise : un lancement interrompu reprend là où il s'était arrêté
    if args.reprendre:
        out = Path(args.reprendre)
    else:
        out = ROOT / "sorties" / f"controle_{datetime.now():%Y%m%d_%H%M%S}"
        out.mkdir(parents=True)
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
    sem = asyncio.Semaphore(args.concurrency)
    boucle = asyncio.get_running_loop()
    termines = len(faits)

    # chaîne continue : chaque dossier part chez Claude dès que sa lecture locale est finie,
    # au lieu d'attendre la fin de la lecture de tout le lot
    with ProcessPoolExecutor(max_workers=args.jobs) as pool:
        async def un_dossier(pdf: Path):
            nonlocal termines
            ocr = await boucle.run_in_executor(pool, _preparer, str(pdf))
            row, journal = await traiter(client, pdf, ocr, args, sem)
            journal["ligne"] = row
            with open(journal_fichier, "a", encoding="utf-8") as f:
                f.write(json.dumps(journal, ensure_ascii=False) + "\n")
            termines += 1
            print(f"PROGRESSION {termines}/{total}", flush=True)
            return journal

        nouveaux = await asyncio.gather(*(un_dossier(p) for p in a_faire))
    wall = time.perf_counter() - t0

    journaux = list(faits.values()) + nouveaux
    rows = [j["ligne"] for j in journaux]
    if not rows:
        print("Aucun dossier PDF trouvé.")
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
        f"menée en parallèle des appels à Claude)",
        f"QR officiel exploité : {avec_qr}/{len(rows)} dossiers",
        f"Pages envoyées à Claude : {envoyees}/{total_pages} "
        f"({100 * envoyees / total_pages:.0f} %)",
        f"Coût : {cout:.3f} $ au total, {unitaire:.5f} $ / dossier ({MODEL})",
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
    resume = "\n".join(lignes)
    (out / "synthese.txt").write_text(resume, encoding="utf-8")
    print("\n" + resume)


if __name__ == "__main__":
    asyncio.run(main())
