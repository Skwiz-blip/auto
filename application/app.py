import ctypes
import ctypes.wintypes
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import webview

ICI = Path(__file__).resolve().parent
if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(ICI.parent))  # en développement, le moteur est le dossier parent
import chemins  # noqa: E402
DONNEES = Path(os.environ.get("APPDATA", str(ICI))) / "KIK-Controle"
CONFIG = DONNEES / "config.json"
MOTEUR_DEFAUT = ICI.parent  # l'application vit dans AUTO\application

# lecture locale mesurée sur 247 dossiers réels (octobre 2026) ; s'y ajoute l'attente des lots
# Claude (Sonnet, -50 %) : en général moins d'une heure, 24 h au plus
MINUTES_PAR_DOSSIER = 0.25
STATUTS = ("VALIDÉ", "À VÉRIFIER", "REJETÉ", "ERREUR")
MAX_DOSSIERS = 50  # par lancement (le moteur ne traite que les 50 premiers PDF)
SANS_FENETRE = 0x08000000  # CREATE_NO_WINDOW : pas de console noire derrière l'application


# --------------------------------------------------------------------------
# Clé API : chiffrée avec le compte Windows (DPAPI), jamais écrite en clair
# --------------------------------------------------------------------------

class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(donnees: bytes, chiffrer: bool) -> bytes:
    tampon = ctypes.create_string_buffer(donnees, len(donnees))
    entree = _Blob(len(donnees), ctypes.cast(tampon, ctypes.POINTER(ctypes.c_char)))
    sortie = _Blob()
    fonction = (ctypes.windll.crypt32.CryptProtectData if chiffrer
                else ctypes.windll.crypt32.CryptUnprotectData)
    if not fonction(ctypes.byref(entree), None, None, None, None, 0, ctypes.byref(sortie)):
        raise OSError("chiffrement Windows indisponible")
    try:
        return ctypes.string_at(sortie.pbData, sortie.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(sortie.pbData)


def lire_config() -> dict:
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def ecrire_config(config: dict) -> None:
    DONNEES.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(config, ensure_ascii=False, indent=1), encoding="utf-8")


def cle_api() -> str:
    chiffree = lire_config().get("cle")
    if not chiffree:
        return ""
    try:
        return _dpapi(bytes.fromhex(chiffree), chiffrer=False).decode()
    except (OSError, ValueError):
        return ""


def moteur() -> Path:
    choisi = Path(lire_config().get("moteur") or MOTEUR_DEFAUT)
    # chemin enregistré sur une autre installation : on revient au dossier du projet
    return choisi if (choisi / "controle.py").exists() else MOTEUR_DEFAUT


# --------------------------------------------------------------------------
# Lecture des résultats produits par le moteur
# --------------------------------------------------------------------------

def _journaux(sortie: Path) -> list[dict]:
    fichier = sortie / "resultats.jsonl"
    if not fichier.exists():
        return []
    lignes = []
    for ligne in fichier.read_text(encoding="utf-8").splitlines():
        try:
            lignes.append(json.loads(ligne))
        except ValueError:
            pass  # ligne en cours d'écriture
    return lignes


SANS_COMMERCIAL = "Commercial non identifié"


def _commercial(j: dict) -> str:
    """Commercial (« Demandé par » de la fiche), nom harmonisé avec la liste connue."""
    import referentiel
    lu = (j.get("extraction") or {}).get("fiche", {}).get("commercial", "")
    return referentiel.commercial(lu).replace("?", "").strip() or SANS_COMMERCIAL


def _statut(j: dict) -> str:
    return j.get("decision", {}).get("statut") or "ERREUR"


def _source(sortie: Path) -> dict:
    try:
        return json.loads((sortie / "source.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {"dossier": str(chemins.DONNEES / "Dossier test"), "modele": ""}


def _raison_courte(texte: str) -> str:
    """Regroupe les points de vigilance en quelques familles lisibles."""
    t = texte.lower()
    if "liste officielle" in t:
        return "Commercial hors liste officielle"
    if "fiche manuscrite" in t:
        return "Écriture de la fiche illisible"
    if "un seul caractère" in t:
        return "Un caractère d'écart à confirmer"
    if "contrat" in t:
        return "Signature du contrat à confirmer"
    if "nom du pdf" in t or "nom commercial" in t:
        return "Nom du PDF à confirmer"
    if "cachet" in t or "délivrance" in t:
        return "Cachet ou date du RCCM à confirmer"
    if "apiex" in t:
        return "Carte APIEx au lieu de l'IFU"
    if "expire bientôt" in t:
        return "Pièce d'identité proche de l'expiration"
    if "expiration" in t:
        return "Date d'expiration non confirmée"
    if "photo" in t or "qualité médiocre" in t:
        return "Photo ou pièce peu lisible"
    if "doublon" in t:
        return "Doublon possible"
    if t.startswith("nom") or "représentant" in t:
        return "Nom à confirmer"
    if "fiche" in t:
        return "Fiche manuscrite incomplète ou illisible"
    if "rccm" in t:
        return "Numéro RCCM à confirmer"
    return "Autre point à vérifier"


def _nombre(texte: str) -> float:
    try:
        return float(texte)
    except (TypeError, ValueError):
        return 0.0


def _minutes(sortie: Path) -> float | None:
    try:
        texte = (sortie / "synthese.txt").read_text(encoding="utf-8")
    except OSError:
        return None
    m = re.search(r"Durée totale : ([\d.]+) min", texte)
    return float(m.group(1)) if m else None


# --------------------------------------------------------------------------
# Fonctions appelées depuis l'interface
# --------------------------------------------------------------------------

class Api:
    def __init__(self):
        self._fenetre = None
        self._processus = None
        self._verrou = threading.Lock()
        self._etat = self._etat_vide()

    @staticmethod
    def _etat_vide() -> dict:
        return {"en_cours": False, "fait": 0, "total": 0, "lus": 0, "lots": 0, "requetes": 0,
                "sortie": "", "debut": 0.0, "fin": 0.0, "derniers": [], "erreur": "",
                "arrete": False, "compte": {s: 0 for s in STATUTS}, "tri": "", "tri_erreur": ""}

    # ---- paramètres

    def config(self) -> dict:
        cfg = lire_config()
        cle = cle_api()
        return {"a_cle": bool(cle), "cle_apercu": f"…{cle[-4:]}" if cle else "",
                "moteur": str(moteur()),
                "moteur_ok": chemins.INSTALLE or (moteur() / "controle.py").exists(),
                "installe": chemins.INSTALLE, "donnees": str(chemins.DONNEES),
                "minutes": MINUTES_PAR_DOSSIER}

    def enregistrer(self, cle: str = "", chemin_moteur: str = "") -> dict:
        cfg = lire_config()
        if cle.strip():
            cfg["cle"] = _dpapi(cle.strip().encode(), chiffrer=True).hex()
        if chemin_moteur.strip():
            cfg["moteur"] = chemin_moteur.strip()
        ecrire_config(cfg)
        return self.config()

    def oublier_cle(self) -> dict:
        cfg = lire_config()
        cfg.pop("cle", None)
        ecrire_config(cfg)
        return self.config()

    def tester_cle(self, cle: str = "") -> dict:
        cle = cle.strip() or cle_api()
        if not cle:
            return {"ok": False, "message": "Aucune clé à tester."}
        try:
            import anthropic
            anthropic.Anthropic(api_key=cle, max_retries=1, timeout=20).models.list(limit=1)
            return {"ok": True, "message": "Clé valide : la connexion à Claude fonctionne."}
        except Exception as e:  # clé refusée, pas de réseau…
            nom = type(e).__name__
            if "Authentication" in nom or "Permission" in nom:
                return {"ok": False, "message": "Clé refusée par Claude."}
            return {"ok": False, "message": f"Connexion impossible ({nom})."}

    def choisir_moteur(self) -> str:
        choix = self._fenetre.create_file_dialog(webview.FOLDER_DIALOG)
        return choix[0] if choix else ""

    # ---- lancement

    def choisir_dossier(self) -> dict:
        choix = self._fenetre.create_file_dialog(webview.FOLDER_DIALOG)
        # l'analyse (dossiers déjà contrôlés) suit, selon la case « Recontrôler »
        return {"chemin": choix[0]} if choix else {}

    def analyser_dossier(self, chemin: str, recontroler: bool = False) -> dict:
        """PDF du dossier, dont ceux déjà contrôlés lors d'un lancement précédent (sautés, sauf
        si l'on demande de les recontrôler) ; 50 au plus sont traités par lancement."""
        import registre
        dossier = Path(chemin)
        pdfs = sorted(dossier.glob("*.pdf")) if dossier.is_dir() else []
        try:
            nouveaux, sautes = registre.a_controler(pdfs, recontroler)
        except OSError:  # PDF ouvert ailleurs, dossier réseau indisponible…
            nouveaux, sautes = pdfs, []
        return {"chemin": str(dossier), "nb_pdf": len(pdfs), "nb_deja": len(sautes),
                "nb_traites": min(len(nouveaux), MAX_DOSSIERS),
                "nb_restants": max(0, len(nouveaux) - MAX_DOSSIERS), "max": MAX_DOSSIERS}

    def lancer(self, dossier: str, reprendre: str = "", recontroler: bool = False) -> dict:
        """Contrôle avec Claude Sonnet, en lots (Message Batches API : -50 %)."""
        with self._verrou:
            if self._etat["en_cours"]:
                return {"ok": False, "message": "Un contrôle est déjà en cours."}
            cle = cle_api()
            if not cle:
                return {"ok": False, "message": "Ajoutez d'abord votre clé API dans Paramètres."}
            if chemins.INSTALLE:
                # application installée : le même exe fait tourner le moteur
                commande, dossier_travail = [sys.executable, "--moteur"], chemins.DONNEES
            else:
                script = moteur() / "controle.py"
                if not script.exists():
                    return {"ok": False, "message": f"Moteur introuvable : {script}"}
                commande, dossier_travail = [sys.executable, "-u", str(script)], moteur()
            commande += ["--dossier", dossier, "--modele", "sonnet",
                         "--jobs", str(max(2, min(6, (os.cpu_count() or 4) - 2)))]
            if reprendre:
                commande += ["--reprendre", reprendre]
            elif recontroler:
                commande += ["--recontroler"]
            env = {**os.environ, "ANTHROPIC_API_KEY": cle, "PYTHONIOENCODING": "utf-8"}
            DONNEES.mkdir(parents=True, exist_ok=True)
            erreurs = open(DONNEES / "dernier_lancement_erreurs.txt", "w", encoding="utf-8")
            self._processus = subprocess.Popen(
                commande, cwd=str(dossier_travail), env=env, stdout=subprocess.PIPE, stderr=erreurs,
                text=True, encoding="utf-8", errors="replace", creationflags=SANS_FENETRE)
            self._etat = self._etat_vide() | {"en_cours": True, "debut": time.time()}
            if reprendre:
                self._etat["sortie"] = reprendre
        threading.Thread(target=self._suivre, args=(dossier, erreurs), daemon=True).start()
        return {"ok": True}

    def _suivre(self, dossier: str, erreurs) -> None:
        motif_ligne = re.compile(r"^\s+(VALIDÉ|À VÉRIFIER|REJETÉ|ERREUR)\s.*?([\d.]+)\$\s+(.*)$")
        for ligne in self._processus.stdout:
            ligne = ligne.rstrip()
            with self._verrou:
                e = self._etat
                if ligne.startswith("CREDIT_EPUISE "):
                    e["erreur"] = ("Crédit Claude épuisé. Rechargez le compte sur "
                                   "console.anthropic.com, puis cliquez sur « Reprendre » : "
                                   "les dossiers déjà traités ne seront pas refacturés.")
                elif ligne.startswith("DOSSIER_SORTIE "):
                    e["sortie"] = ligne.split(" ", 1)[1]
                    (Path(e["sortie"]) / "source.json").write_text(
                        json.dumps({"dossier": dossier, "modele": "sonnet (lots)"},
                                   ensure_ascii=False), encoding="utf-8")
                elif ligne.startswith("PROGRESSION "):
                    fait, total = ligne.split()[1].split("/")
                    e["fait"], e["total"] = int(fait), int(total)
                elif ligne.startswith("LECTURE_LOCALE "):
                    e["lus"] = int(ligne.split()[1].split("/")[0])
                elif ligne.startswith(("LOT_ENVOYE ", "LOT_TERMINE ")):
                    # « LOT_ENVOYE <lot> <requêtes> <lots en cours> »
                    _, _, requetes, en_cours = ligne.split()
                    e["lots"] = int(en_cours)
                    e["requetes"] += int(requetes) * (1 if ligne.startswith("LOT_E") else -1)
                    e["requetes"] = max(e["requetes"], 0)
                elif m := re.match(r"(\d+) dossier\(s\) à traiter(?:, (\d+) déjà faits)?", ligne):
                    e["fait"] = int(m.group(2) or 0)
                    e["total"] = int(m.group(1)) + e["fait"]
                elif m := motif_ligne.match(ligne):
                    statut, _, nom = m.groups()
                    e["compte"][statut] += 1
                    e["derniers"] = ([{"statut": statut, "dossier": nom.split("  !!")[0]}]
                                     + e["derniers"])[:8]
        code = self._processus.wait()
        erreurs.close()
        with self._verrou:
            e = self._etat
            e["en_cours"], e["fin"] = False, time.time()
            if code == 0 and e["sortie"] and not e["erreur"]:
                # contrôle terminé : les PDF sont rangés par décision, sans intervention
                try:
                    e["tri"] = self.ranger(e["sortie"], ouvrir=False)["dossier"]
                except OSError as err:
                    e["tri_erreur"] = f"Rangement impossible : {err}"
            if code != 0 and not e["arrete"]:
                try:
                    fin = (DONNEES / "dernier_lancement_erreurs.txt").read_text(
                        encoding="utf-8").strip().splitlines()[-3:]
                except OSError:
                    fin = []
                e["erreur"] = " ".join(fin)[-400:] or f"Le contrôle s'est arrêté (code {code})."

    def avancement(self) -> dict:
        with self._verrou:
            e = dict(self._etat)
        fin = e["fin"] if not e["en_cours"] and e["fin"] else time.time()
        e["ecoule_s"] = round(fin - e["debut"]) if e["debut"] else 0
        return e

    def arreter(self) -> dict:
        with self._verrou:
            if not (self._processus and self._etat["en_cours"]):
                return {"ok": False}
            self._etat["arrete"] = True
            # /T : arrête aussi les processus de lecture lancés par le moteur
            subprocess.run(["taskkill", "/PID", str(self._processus.pid), "/T", "/F"],
                           capture_output=True, creationflags=SANS_FENETRE)
        return {"ok": True}

    # ---- résultats

    def lancements(self) -> list[dict]:
        racine = chemins.SORTIES
        liste = []
        for sortie in sorted(racine.glob("controle_*"), reverse=True):
            journaux = _journaux(sortie)
            if not journaux:
                continue
            src = _source(sortie)
            try:
                quand = datetime.strptime(sortie.name[9:], "%Y%m%d_%H%M%S")
                date_txt = quand.strftime("%d/%m/%Y %H:%M")
            except ValueError:
                date_txt = sortie.name
            total = min(MAX_DOSSIERS, len(list(Path(src["dossier"]).glob("*.pdf")))
                        if Path(src["dossier"]).is_dir() else 0)
            liste.append({"sortie": str(sortie), "date": date_txt, "nb": len(journaux),
                          # des dossiers en erreur (crédit épuisé, réseau…) se reprennent
                          "termine": (sortie / "synthese.txt").exists()
                                     and not any(j.get("erreur") for j in journaux),
                          "total_source": total, "dossier": src["dossier"],
                          "modele": src.get("modele", ""),
                          "en_cours": self._etat["en_cours"] and self._etat["sortie"] == str(sortie)})
        return liste

    def resultats(self, sortie: str) -> dict:
        dossier = Path(sortie)
        items, compte, motifs, raisons = [], {s: 0 for s in STATUTS}, {}, {}
        avec_qr = 0
        # les lancements antérieurs à la reprise n'ont leurs pages que dans resultats.csv
        anciennes = {}
        if (dossier / "resultats.csv").exists():
            import csv
            with open(dossier / "resultats.csv", encoding="utf-8-sig") as f:
                for r in csv.DictReader(f, delimiter=";"):
                    anciennes[r["dossier"]] = {k: _nombre(v) for k, v in r.items()
                                               if k in ("pages", "pages_envoyees")}
        for j in _journaux(dossier):
            statut = _statut(j)
            compte[statut] += 1
            dec = j.get("decision", {})
            ext = j.get("extraction", {})
            ligne = j.get("ligne") or anciennes.get(j["dossier"], {})
            cip = ext.get("piece_identite", {})
            officiel = list(j.get("officiel", {}))
            avec_qr += bool(officiel)
            for b in dec.get("bloquants", []):
                motifs[b["motif"]] = motifs.get(b["motif"], 0) + 1
            # « Autre » en dernier : les raisons précises d'abord
            familles = sorted({_raison_courte(v) for v in dec.get("vigilance", [])},
                              key=lambda f: (f.startswith("Autre"), f))
            if statut == "À VÉRIFIER":
                for f in familles:
                    raisons[f] = raisons.get(f, 0) + 1
            items.append({
                "dossier": j["dossier"], "statut": statut, "commercial": _commercial(j),
                "bloquants": dec.get("bloquants", []), "vigilance": dec.get("vigilance", []),
                "particuliers": dec.get("particuliers", []), "familles": familles,
                "erreur": j.get("erreur", ""), "qr": officiel,
                "cip_nom": " ".join(filter(None, [cip.get("nom", ""), cip.get("prenoms", "")])),
                "cip_expiration": cip.get("date_expiration", ""),
                "rccm": ext.get("rccm", {}).get("numero", ""),
                "ifu": ext.get("ifu", {}).get("numero", ""),
                "pages": ligne.get("pages", 0), "pages_envoyees": ligne.get("pages_envoyees", 0)})
        ordre = {"REJETÉ": 0, "À VÉRIFIER": 1, "ERREUR": 2, "VALIDÉ": 3}
        items.sort(key=lambda i: (ordre[i["statut"]], i["dossier"]))
        # par commercial : nombre de dossiers par statut (non identifiés en dernier)
        commerciaux = {}
        for i in items:
            c = commerciaux.setdefault(i["commercial"], {s: 0 for s in STATUTS} | {"total": 0})
            c[i["statut"]] += 1
            c["total"] += 1
        commerciaux = sorted(commerciaux.items(),
                             key=lambda x: (x[0] == SANS_COMMERCIAL, x[0]))
        nb = len(items)
        src = _source(dossier)
        return {"items": items, "compte": compte, "nb": nb, "commerciaux": commerciaux,
                "minutes": _minutes(dossier), "avec_qr": avec_qr,
                "motifs": sorted(motifs.items(), key=lambda x: -x[1]),
                "raisons": sorted(raisons.items(), key=lambda x: -x[1]),
                "termine": (dossier / "synthese.txt").exists(),
                "modele": src.get("modele", ""), "dossier_source": src["dossier"]}

    def ouvrir_pdf(self, sortie: str, nom: str) -> dict:
        source = Path(_source(Path(sortie))["dossier"])
        for pdf in source.glob("*.pdf"):
            if pdf.stem.strip() == nom:
                os.startfile(pdf)
                return {"ok": True}
        return {"ok": False, "message": f"PDF introuvable dans {source}"}

    def ouvrir_sortie(self, sortie: str) -> None:
        os.startfile(sortie)

    def ranger(self, sortie: str, ouvrir: bool = True) -> dict:
        """Range les PDF du lancement en trois dossiers : Validés, À vérifier, Rejetés.

        Les originaux ne bougent pas. Sur le même disque, chaque PDF rangé est un lien
        physique vers l'original (aucune place en plus) ; sinon, une copie.
        """
        sortie = Path(sortie)
        source = Path(_source(sortie)["dossier"])
        pdfs = {p.stem.strip(): p for p in source.glob("*.pdf")}
        try:
            quand = datetime.strptime(sortie.name[9:], "%Y%m%d_%H%M%S").strftime("%Y-%m-%d %Hh%M")
        except ValueError:
            quand = sortie.name
        racine = source / f"Tri du {quand}"
        noms = {"VALIDÉ": "1 - Validés", "À VÉRIFIER": "2 - À vérifier",
                "REJETÉ": "3 - Rejetés", "ERREUR": "4 - Erreurs techniques"}
        motifs = {s: {} for s in noms}  # statut -> commercial -> blocs
        compte = {s: 0 for s in noms}
        manquants = []
        for j in _journaux(sortie):
            statut = _statut(j)
            pdf = pdfs.get(j["dossier"])
            if not pdf:
                manquants.append(j["dossier"])
                continue
            cible_dossier = racine / noms[statut]
            cible_dossier.mkdir(parents=True, exist_ok=True)
            cible = cible_dossier / pdf.name
            if not cible.exists():
                try:
                    os.link(pdf, cible)
                except OSError:  # autre disque ou système de fichiers sans liens
                    import shutil
                    shutil.copy2(pdf, cible)
            compte[statut] += 1
            dec = j.get("decision", {})
            raisons = ([f"{b['motif']} : {b['detail']}" for b in dec.get("bloquants", [])]
                       + dec.get("vigilance", []) + ([j["erreur"]] if j.get("erreur") else []))
            if statut != "VALIDÉ":
                commercial = _commercial(j)
                motifs[statut].setdefault(commercial, []).append(
                    f"{j['dossier']} — "
                    + (commercial if commercial == SANS_COMMERCIAL else f"Commercial : {commercial}")
                    + "\n"
                    + "".join(f"  - {r}\n" for r in raisons))
        # à côté des PDF, la liste des raisons regroupée par commercial, lisible sans
        # ouvrir l'application
        for statut, par_commercial in motifs.items():
            if not par_commercial:
                continue
            sections = []
            for commercial in sorted(par_commercial, key=lambda c: (c == SANS_COMMERCIAL, c)):
                blocs = par_commercial[commercial]
                sections.append(f"===== {commercial} ({len(blocs)} dossier"
                                f"{'s' if len(blocs) > 1 else ''}) =====\n\n" + "\n".join(blocs))
            (racine / noms[statut] / "_motifs.txt").write_text("\n\n".join(sections),
                                                             encoding="utf-8")
        if ouvrir and racine.exists():
            os.startfile(racine)
        return {"ok": True, "dossier": str(racine), "compte": compte, "manquants": manquants}

    def sharepoint(self, sortie: str) -> dict:
        """Modèle SharePoint rempli avec les dossiers validés du lancement, puis ouvert."""
        try:
            import sharepoint
            fichier = sharepoint.remplir_depuis(Path(sortie))
        except PermissionError:
            return {"ok": False, "message": "Fermez le fichier SharePoint dans Excel puis réessayez."}
        except (ImportError, OSError, ValueError) as e:
            return {"ok": False, "message": f"Fichier SharePoint impossible : {e}"}
        if not fichier:
            fichier = chemins.MODELE
            if fichier.exists():
                os.startfile(fichier)
            return {"ok": False, "message": "Rien de nouveau : les dossiers validés de ce "
                                            "contrôle sont déjà dans le fichier SharePoint."}
        os.startfile(fichier)
        valides = sum(1 for j in _journaux(Path(sortie)) if _statut(j) == "VALIDÉ")
        return {"ok": True, "nb": valides}

    def exporter(self, sortie: str) -> dict:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill

        donnees = self.resultats(sortie)
        classeur = Workbook()
        feuille = classeur.active
        feuille.title = "Résultats"
        entetes = ["Statut", "Dossier", "Commercial", "Motifs de rejet", "Points à vérifier",
                   "Nom sur la CIP", "Expiration CIP", "N° RCCM", "N° IFU",
                   "Source RCCM/IFU"]
        feuille.append(entetes)
        for i in donnees["items"]:
            feuille.append([
                i["statut"], i["dossier"], i["commercial"],
                "\n".join(f"{b['motif']} : {b['detail']}" for b in i["bloquants"]) or i["erreur"],
                "\n".join(i["vigilance"]), i["cip_nom"], i["cip_expiration"], i["rccm"], i["ifu"],
                "QR officiel" if i["qr"] else "Lecture du scan"])
        bleu = PatternFill("solid", fgColor="1F5FD6")
        for cellule in feuille[1]:
            cellule.font = Font(bold=True, color="FFFFFF")
            cellule.fill = bleu
        for colonne, largeur in zip("ABCDEFGHIJ", (13, 34, 26, 50, 60, 30, 15, 22, 17, 17)):
            feuille.column_dimensions[colonne].width = largeur
        for ligne in feuille.iter_rows(min_row=2):
            for cellule in ligne:
                cellule.alignment = Alignment(wrap_text=True, vertical="top")
        feuille.freeze_panes = "A2"
        feuille.auto_filter.ref = feuille.dimensions

        # Par commercial : un bloc par commercial, ses dossiers rejetés et à vérifier
        recap = classeur.create_sheet("Par commercial")
        recap.append(["Commercial", "Rejetés", "À vérifier", "Validés", "Total"])
        for cellule in recap[1]:
            cellule.font = Font(bold=True, color="FFFFFF")
            cellule.fill = bleu
        for commercial, c in donnees["commerciaux"]:
            recap.append([commercial, c["REJETÉ"], c["À VÉRIFIER"], c["VALIDÉ"], c["total"]])
        recap.append([])
        recap.append(["Commercial", "Dossier", "Statut", "Motif / point à vérifier"])
        for cellule in recap[recap.max_row]:
            cellule.font = Font(bold=True, color="FFFFFF")
            cellule.fill = bleu
        a_traiter = sorted((i for i in donnees["items"] if i["statut"] != "VALIDÉ"),
                           key=lambda i: (i["commercial"] == SANS_COMMERCIAL, i["commercial"],
                                          i["statut"], i["dossier"]))
        for i in a_traiter:
            raisons = ([f"{b['motif']} : {b['detail']}" for b in i["bloquants"]]
                       + i["vigilance"] + ([i["erreur"]] if i["erreur"] else []))
            recap.append([i["commercial"], i["dossier"], i["statut"], "\n".join(raisons)])
        for colonne, largeur in zip("ABCDE", (30, 40, 13, 90, 8)):
            recap.column_dimensions[colonne].width = largeur
        for ligne in recap.iter_rows(min_row=2):
            for cellule in ligne:
                cellule.alignment = Alignment(wrap_text=True, vertical="top")
        fichier = Path(sortie) / "resultats.xlsx"
        try:
            classeur.save(fichier)
        except PermissionError:
            return {"ok": False, "message": "Fermez resultats.xlsx dans Excel puis réessayez."}
        os.startfile(fichier)
        return {"ok": True}


def moteur_integre() -> None:
    """Application installée : « Controle KIK.exe --moteur … » fait tourner le moteur de contrôle
    (lancé en arrière-plan par la fenêtre, qui lit sa sortie)."""
    import asyncio
    import io
    # exe sans console : si aucun canal de sortie n'a été transmis, on écrit dans un journal
    if sys.stdout is None:
        sys.stdout = open(chemins.DONNEES / "moteur_sortie.txt", "w", encoding="utf-8")
    elif hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
    if sys.stderr is None:
        sys.stderr = sys.stdout
    sys.argv = [sys.argv[0]] + [a for a in sys.argv[1:] if a != "--moteur"]
    import controle
    asyncio.run(controle.main())


def main():
    import multiprocessing
    multiprocessing.freeze_support()  # processus de lecture lancés par le moteur (exe)
    chemins.preparer()
    if "--moteur" in sys.argv:
        moteur_integre()
        return
    api = Api()
    fenetre = webview.create_window(
        "Contrôle des dossiers marchands", url=str(ICI / "ui" / "index.html"), js_api=api,
        width=1320, height=860, min_size=(1040, 680), background_color="#FFFFFF")
    api._fenetre = fenetre
    webview.start()


if __name__ == "__main__":
    main()
