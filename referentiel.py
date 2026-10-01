"""Référentiel géographique du Bénin et normalisation des champs de la fiche manuscrite.

La fiche est remplie à la main : « Natitingui », « AB-Calavi », « ZALI »… On ramène la ville à
l'une des 77 communes (ou à la commune d'un arrondissement connu) et on en déduit le
département, ce qui évite aussi les incohérences du type « ZOU » + « COTONOU ».
"""
import re
import unicodedata
from difflib import SequenceMatcher

COMMUNES = {
    "ALIBORI": ["Banikoara", "Gogounou", "Kandi", "Karimama", "Malanville", "Ségbana"],
    "ATACORA": ["Boukoumbé", "Cobly", "Kérou", "Kouandé", "Matéri", "Natitingou", "Péhunco",
                "Tanguiéta", "Toucountouna"],
    "ATLANTIQUE": ["Abomey-Calavi", "Allada", "Kpomassè", "Ouidah", "Sô-Ava", "Toffo",
                   "Tori-Bossito", "Zè"],
    "BORGOU": ["Bembèrèkè", "Kalalé", "N'Dali", "Nikki", "Parakou", "Pèrèrè", "Sinendé",
               "Tchaourou"],
    "COLLINES": ["Bantè", "Dassa-Zoumè", "Glazoué", "Ouèssè", "Savalou", "Savè"],
    "COUFFO": ["Aplahoué", "Djakotomey", "Dogbo", "Klouékanmè", "Lalo", "Toviklin"],
    "DONGA": ["Bassila", "Copargo", "Djougou", "Ouaké"],
    "LITTORAL": ["Cotonou"],
    "MONO": ["Athiémé", "Bopa", "Comè", "Grand-Popo", "Houéyogbé", "Lokossa"],
    "OUEME": ["Adjarra", "Adjohoun", "Aguégués", "Akpro-Missérété", "Avrankou", "Bonou",
              "Dangbo", "Porto-Novo", "Sèmè-Kpodji"],
    "PLATEAU": ["Adja-Ouèrè", "Ifangni", "Kétou", "Pobè", "Sakété"],
    "ZOU": ["Abomey", "Agbangnizoun", "Bohicon", "Covè", "Djidja", "Ouinhi", "Zagnanado",
            "Za-Kpota", "Zogbodomey"],
}
DEPARTEMENTS = list(COMMUNES)
# arrondissements et localités souvent écrits dans la case « Ville » -> commune
LOCALITES = {
    "CALAVI": "Abomey-Calavi", "ABCALAVI": "Abomey-Calavi", "AKASSATO": "Abomey-Calavi",
    "GODOMEY": "Abomey-Calavi", "OUEDO": "Abomey-Calavi", "HEVIE": "Abomey-Calavi",
    "TOGBA": "Abomey-Calavi", "ZINVIE": "Abomey-Calavi", "GLODJIGBE": "Abomey-Calavi",
    "KPANROUN": "Abomey-Calavi", "COCOTOMEY": "Abomey-Calavi", "COCODJI": "Abomey-Calavi",
    "COCOCODJI": "Abomey-Calavi", "ZOGBADJE": "Abomey-Calavi", "PAHOU": "Ouidah",
    "SAVI": "Ouidah", "HOUEGBO": "Toffo", "SEKOU": "Allada", "ATTOGON": "Allada",
    "TORI": "Tori-Bossito", "SEME": "Sèmè-Kpodji", "DJEFFA": "Sèmè-Kpodji",
    "EKPE": "Sèmè-Kpodji", "PORTONOVO": "Porto-Novo", "DASSA": "Dassa-Zoumè",
    "DOGBO": "Dogbo", "NATITINGOU": "Natitingou",
    "AKOSSAVIE": "Abomey-Calavi", "COCOCODJI": "Abomey-Calavi", "GOLODJIGBE": "Abomey-Calavi",
    "DJADJO": "Abomey-Calavi", "BIDOSSESSI": "Abomey-Calavi", "KPOSSIDJA": "Abomey-Calavi",
    "KILIBO": "Ouèssè", "AGBANGNIZOUN": "Agbangnizoun", "BOHIQUE": "Bohicon",
    "GOUNLI": "Covè", "AZOVE": "Aplahoué",
}

# Commerciaux KIK connus (relevés sur les fiches de septembre 2026) : un nom mal écrit ou mal
# lu est rattaché au plus proche. À compléter avec la liste officielle quand KIK la fournira.
COMMERCIAUX = [
    "ADAMOU A. YACOUBOU", "AGBOTO GHISLAIN", "AGNAN RAOUL", "ASSANATA ORPHERIQUE",
    "AWO BERNADIN", "BOSSOU GABIN", "CHICOTO EVRARD LANDRY", "DAGBEGNON JULIEN",
    "DOVONOU BERNARD", "HELE PRINCE", "ILLO ABOUDOU WABI", "KAKPOVI SEBASTIEN",
    "MOUZOUN HONORE", "PADONOU ERIC", "QUENUM SALOMON", "SALIGA THIERRY",
    "SOUNOUVOU FLORENT", "TAGNON ANGE", "TAPE OLIVIER",
]
_COMMUNE_DEPT = {c: d for d, liste in COMMUNES.items() for c in liste}
_NOMS_DEPT = {"ALIBORI": "Alibori", "ATACORA": "Atacora", "ATLANTIQUE": "Atlantique",
              "BORGOU": "Borgou", "COLLINES": "Collines", "COUFFO": "Couffo",
              "DONGA": "Donga", "LITTORAL": "Littoral", "MONO": "Mono", "OUEME": "Ouémé",
              "PLATEAU": "Plateau", "ZOU": "Zou"}


def _cle(texte: str) -> str:
    t = unicodedata.normalize("NFKD", texte or "")
    return re.sub(r"[^A-Z]", "", "".join(c for c in t if not unicodedata.combining(c)).upper())


def _plus_proche(cle: str, choix: dict[str, str], seuil: float) -> str:
    if not cle:
        return ""
    meilleur = max(choix, key=lambda c: SequenceMatcher(None, cle, c).ratio())
    return choix[meilleur] if SequenceMatcher(None, cle, meilleur).ratio() >= seuil else ""


def commune(ville_lue: str) -> str:
    """Commune officielle correspondant à la ville écrite, ou "" si rien ne correspond."""
    cle = _cle(ville_lue)
    if not cle:
        return ""
    choix = {_cle(c): c for c in _COMMUNE_DEPT} | LOCALITES
    if cle in choix:
        return choix[cle]
    # nom entier proche d'abord (« ABOMEY CALAC? » -> Abomey-Calavi, pas Abomey)
    if proche := _plus_proche(cle, choix, 0.8):
        return proche
    # « ABOMEY CALAVI AKASSATO » : une localité connue contenue dans le texte
    for loc in sorted(choix, key=len, reverse=True):
        if len(loc) >= 5 and loc in cle:
            return choix[loc]
    return _plus_proche(cle, choix, 0.7)


def departement(dept_lu: str, commune_trouvee: str = "") -> str:
    """Département officiel : déduit de la commune si elle est connue, sinon le plus proche."""
    if commune_trouvee in _COMMUNE_DEPT:
        return _NOMS_DEPT[_COMMUNE_DEPT[commune_trouvee]]
    trouve = _plus_proche(_cle(dept_lu), {d: d for d in DEPARTEMENTS}, 0.6)
    return _NOMS_DEPT.get(trouve, "")


def commercial(lu: str) -> str:
    """Nom du commercial écrit après « Demandé par », rattaché à la liste connue s'il en est
    proche (« TADE Olivier » -> TAPE OLIVIER, « DOVENOU Benard » -> DOVONOU BERNARD)."""
    texte = re.sub(r"(?i)^\s*demand[ée]e? par\s*:?\s*", "", lu or "").strip()
    cle = _cle(texte)
    if not cle:
        return texte.upper()
    # les deux ordres (nom prénom / prénom nom) sont comparés
    choix = {}
    for nom in COMMERCIAUX:
        mots = nom.replace(".", "").split()
        choix[_cle(nom)] = nom
        choix[_cle(" ".join(mots[1:] + mots[:1]))] = nom
    if trouve := _plus_proche(cle, choix, 0.72):
        return trouve
    # sinon, un mot bien écrit suffit s'il est propre à un seul commercial (« ILLO … », « Orphérique »)
    mots_lus = [_cle(m) for m in texte.split() if len(_cle(m)) >= 4]
    candidats = {nom for nom in COMMERCIAUX for m in mots_lus
                 for mot in nom.replace(".", "").split()
                 if len(mot) >= 4 and SequenceMatcher(None, m, _cle(mot)).ratio() >= 0.85}
    return candidats.pop() if len(candidats) == 1 else texte.upper()


def nombre_comptes(valeur: str, defaut: str, maximum: int) -> str:
    """« 1 », « 01 » -> « 01 » ; vide, illisible (« ? ») ou invraisemblable -> défaut."""
    texte = (valeur or "").strip()
    if not texte or "?" in texte:
        return defaut
    chiffres = re.sub(r"\D", "", texte)
    if not chiffres or int(chiffres) > maximum:
        return defaut
    return chiffres.zfill(2)[-2:]
