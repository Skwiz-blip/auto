"use strict";

const $ = (id) => document.getElementById(id);
const api = () => window.pywebview.api;
const nombre = new Intl.NumberFormat("fr-FR");
const dollars = (v, d = 2) => `${v.toLocaleString("fr-FR", { minimumFractionDigits: d, maximumFractionDigits: d })} $`;
const pourcent = (n, total) => total ? `${Math.round((100 * n) / total)} %` : "–";
const echapper = (t) => String(t ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

const STATUTS = {
  "VALIDÉ": { classe: "ok", libelle: "Validé", couleur: "var(--ok)",
    icone: '<svg viewBox="0 0 24 24"><path d="M5 12.5l4.2 4.2L19 7"/></svg>' },
  "À VÉRIFIER": { classe: "attention", libelle: "À vérifier", couleur: "var(--attention)",
    icone: '<svg viewBox="0 0 24 24"><path d="M12 4l9 16H3z"/><path d="M12 10v4M12 17.3h.01"/></svg>' },
  "REJETÉ": { classe: "rejet", libelle: "Rejeté", couleur: "var(--rejet)",
    icone: '<svg viewBox="0 0 24 24"><path d="M7 7l10 10M17 7L7 17"/></svg>' },
  "ERREUR": { classe: "erreur", libelle: "Erreur", couleur: "var(--erreur)",
    icone: '<svg viewBox="0 0 24 24"><path d="M6 12h12"/></svg>' },
};
const pilule = (s) => `<span class="statut ${STATUTS[s].classe}">${STATUTS[s].icone}${STATUTS[s].libelle}</span>`;

const etat = {
  config: null, lancements: [], sortie: "", donnees: null, filtre: "tous", recherche: "",
  dossier: "", nbPdf: 0, modele: "haiku", suivi: null, detail: null,
};

/* ---------------------------------------------------------------- navigation */

function allerA(vue) {
  document.querySelectorAll(".vue").forEach((v) => (v.hidden = v.id !== `vue-${vue}`));
  document.querySelectorAll(".lien").forEach((l) => l.classList.toggle("actif", l.dataset.vue === vue));
  if (vue === "tableau") chargerLancements();
  if (vue === "lancer") afficherInterrompus();
  if (vue === "parametres") afficherParametres();
}
document.querySelectorAll(".lien").forEach((l) => l.addEventListener("click", () => allerA(l.dataset.vue)));
document.querySelectorAll("[data-aller]").forEach((b) => b.addEventListener("click", () => allerA(b.dataset.aller)));

function toast(message) {
  const t = $("toast");
  t.textContent = message;
  t.hidden = false;
  clearTimeout(toast.minuterie);
  toast.minuterie = setTimeout(() => (t.hidden = true), 3200);
}

/* ---------------------------------------------------------------- bulle d'aide */

function bulle(element, texte) {
  element.addEventListener("mousemove", (e) => {
    const b = $("bulle");
    b.innerHTML = texte;
    b.hidden = false;
    const x = Math.min(e.clientX + 14, window.innerWidth - b.offsetWidth - 8);
    b.style.left = `${x}px`;
    b.style.top = `${e.clientY + 16}px`;
  });
  element.addEventListener("mouseleave", () => ($("bulle").hidden = true));
}

/* ---------------------------------------------------------------- état de connexion */

async function rafraichirConfig() {
  etat.config = await api().config();
  const c = etat.config;
  const pret = c.a_cle && c.moteur_ok;
  $("etat-connexion").innerHTML =
    `<span class="point" style="background:${pret ? "var(--ok)" : "var(--attention)"}"></span>` +
    (pret ? "Prêt" : !c.a_cle ? "Clé API à ajouter" : "Moteur introuvable");
  if (!etat.suivi?.en_cours) etat.modele = c.modele;
  majModele();
}

/* ---------------------------------------------------------------- tableau de bord */

async function chargerLancements() {
  etat.lancements = await api().lancements();
  const choix = $("tb-lancement");
  if (!etat.lancements.length) {
    $("tb-vide").hidden = false;
    $("tb-contenu").hidden = true;
    choix.hidden = $("tb-ouvrir").hidden = $("tb-exporter").hidden = $("tb-ranger").hidden = $("tb-sharepoint").hidden = true;
    $("tb-source").textContent = "";
    return;
  }
  choix.hidden = $("tb-ouvrir").hidden = $("tb-exporter").hidden = $("tb-ranger").hidden = $("tb-sharepoint").hidden = false;
  if (!etat.lancements.some((l) => l.sortie === etat.sortie)) etat.sortie = etat.lancements[0].sortie;
  choix.innerHTML = etat.lancements.map((l) => {
    const suffixe = l.en_cours ? " · en cours" : l.termine ? "" : " · interrompu";
    return `<option value="${echapper(l.sortie)}">${l.date} · ${nombre.format(l.nb)} dossiers${l.modele ? " · " + l.modele : ""}${suffixe}</option>`;
  }).join("");
  choix.value = etat.sortie;
  await chargerResultats();
}

$("tb-lancement").addEventListener("change", (e) => { etat.sortie = e.target.value; chargerResultats(); });
$("tb-ouvrir").addEventListener("click", () => api().ouvrir_sortie(etat.sortie));
$("tb-ranger").addEventListener("click", async () => {
  const r = await api().ranger(etat.sortie, true);
  const c = r.compte;
  toast(`Rangés : ${c["VALIDÉ"]} validés, ${c["À VÉRIFIER"]} à vérifier, ${c["REJETÉ"]} rejetés`
    + (r.manquants.length ? ` · ${r.manquants.length} PDF introuvables` : ""));
});
$("tb-sharepoint").addEventListener("click", async () => {
  const r = await api().sharepoint(etat.sortie);
  toast(r.ok ? `${r.nb} dossier(s) validé(s) ajouté(s) au fichier SharePoint.` : r.message);
});
$("tb-exporter").addEventListener("click", async () => {
  const r = await api().exporter(etat.sortie);
  toast(r.ok ? "Fichier Excel ouvert." : r.message);
});

async function chargerResultats() {
  const d = await api().resultats(etat.sortie);
  etat.donnees = d;
  $("tb-vide").hidden = true;
  $("tb-contenu").hidden = false;
  $("tb-source").textContent = `Source : ${d.dossier_source}${d.termine ? "" : " — contrôle incomplet"}`;

  const c = d.compte;
  const reussis = d.nb - c["ERREUR"];
  $("k-nb").textContent = nombre.format(d.nb);
  $("k-qr").textContent = `${nombre.format(d.avec_qr)} vérifiés par QR officiel`;
  $("k-valide").textContent = nombre.format(c["VALIDÉ"]);
  $("k-valide-p").textContent = pourcent(c["VALIDÉ"], d.nb);
  $("k-verif").textContent = nombre.format(c["À VÉRIFIER"]);
  $("k-verif-p").textContent = pourcent(c["À VÉRIFIER"], d.nb);
  $("k-rejet").textContent = nombre.format(c["REJETÉ"]);
  $("k-rejet-p").textContent = pourcent(c["REJETÉ"], d.nb) + (c["ERREUR"] ? ` · ${c["ERREUR"]} en erreur` : "");
  $("k-cout").textContent = dollars(d.cout);
  $("k-cout-p").textContent = reussis ? `${dollars(d.cout_moyen, 3)} par dossier` : "";
  $("k-cout-p").title = reussis ? `Projection : ~${nombre.format(d.projection_mois)} $ pour 3 000 dossiers par mois` : "";
  $("k-duree").textContent = d.minutes != null ? `${nombre.format(Math.round(d.minutes))} min` : "–";
  $("k-modele").textContent = d.modele ? `Modèle ${d.modele}` : "";

  dessinerRepartition(d);
  dessinerBarres($("motifs"), d.motifs, "Aucun dossier rejeté.");
  dessinerBarres($("raisons"), d.raisons, "Aucun dossier à vérifier.");
  dessinerFiltres();
  dessinerTable();
}

function dessinerRepartition(d) {
  const zone = $("repartition");
  zone.innerHTML = "";
  const presents = Object.keys(STATUTS).filter((s) => d.compte[s] > 0);
  zone.setAttribute("aria-label", presents.map((s) => `${STATUTS[s].libelle} ${d.compte[s]}`).join(", "));
  presents.forEach((s) => {
    const n = d.compte[s];
    const seg = document.createElement("div");
    seg.className = "segment-barre";
    seg.style.flex = `${n} 1 0`;
    seg.style.background = STATUTS[s].couleur;
    bulle(seg, `<strong>${STATUTS[s].libelle}</strong> · ${nombre.format(n)} dossiers (${pourcent(n, d.nb)})`);
    zone.appendChild(seg);
  });
  $("legende").innerHTML = presents.map((s) =>
    `<li><i class="pastille ${STATUTS[s].classe}"></i>${STATUTS[s].libelle} <strong>${nombre.format(d.compte[s])}</strong> <span>${pourcent(d.compte[s], d.nb)}</span></li>`).join("");
}

function dessinerBarres(zone, paires, texteVide) {
  if (!paires.length) {
    zone.innerHTML = `<p class="rien">${texteVide}</p>`;
    return;
  }
  const max = Math.max(...paires.map((p) => p[1]));
  zone.innerHTML = "";
  paires.forEach(([libelle, n]) => {
    const ligne = document.createElement("div");
    ligne.className = "barre-ligne";
    ligne.innerHTML = `<span class="barre-lib" title="${echapper(libelle)}">${echapper(libelle)}</span>
      <span class="barre-piste"><span class="barre-rempli" style="width:${Math.max(2, (82 * n) / max)}%"></span>
      <span class="barre-val">${nombre.format(n)}</span></span>`;
    bulle(ligne, `<strong>${echapper(libelle)}</strong> · ${nombre.format(n)} dossier${n > 1 ? "s" : ""}`);
    zone.appendChild(ligne);
  });
}

function dessinerFiltres() {
  const c = etat.donnees.compte;
  const options = [["tous", "Tous", etat.donnees.nb], ["VALIDÉ", "Validés", c["VALIDÉ"]],
    ["À VÉRIFIER", "À vérifier", c["À VÉRIFIER"]], ["REJETÉ", "Rejetés", c["REJETÉ"]]];
  if (c["ERREUR"]) options.push(["ERREUR", "Erreurs", c["ERREUR"]]);
  $("filtres").innerHTML = options.map(([cle, lib, n]) =>
    `<button class="filtre${etat.filtre === cle ? " actif" : ""}" data-filtre="${cle}" role="tab">
       ${cle !== "tous" ? `<i class="pastille ${STATUTS[cle].classe}"></i>` : ""}${lib} <span class="n">${nombre.format(n)}</span></button>`).join("");
  $("filtres").querySelectorAll(".filtre").forEach((b) => b.addEventListener("click", () => {
    etat.filtre = b.dataset.filtre;
    dessinerFiltres();
    dessinerTable();
  }));
}

function pourquoi(i) {
  if (i.statut === "REJETÉ") return i.bloquants.map((b) => b.motif).join(" · ");
  if (i.statut === "À VÉRIFIER") return i.familles.join(" · ");
  if (i.statut === "ERREUR") return i.erreur.slice(0, 120);
  return "Conforme";
}

function dessinerTable() {
  const q = etat.recherche.trim().toLowerCase();
  const items = etat.donnees.items.filter((i) =>
    (etat.filtre === "tous" || i.statut === etat.filtre) && (!q || i.dossier.toLowerCase().includes(q)));
  if (!items.length) {
    $("lignes").innerHTML = `<tr><td colspan="5" class="table-vide">Aucun dossier.</td></tr>`;
    return;
  }
  $("lignes").innerHTML = items.map((i, k) => `<tr data-k="${k}">
      <td>${pilule(i.statut)}</td>
      <td class="nom">${echapper(i.dossier)}</td>
      <td class="pourquoi">${echapper(pourquoi(i))}</td>
      <td>${i.qr.length ? '<span class="badge officiel">QR officiel</span>' : '<span class="badge">Lecture du scan</span>'}</td>
      <td class="num">${dollars(i.cout, 3)}</td></tr>`).join("");
  $("lignes").querySelectorAll("tr[data-k]").forEach((tr) =>
    tr.addEventListener("click", () => ouvrirDetail(items[Number(tr.dataset.k)])));
}
$("recherche").addEventListener("input", (e) => { etat.recherche = e.target.value; dessinerTable(); });

/* ---------------------------------------------------------------- détail */

function ouvrirDetail(i) {
  etat.detail = i;
  $("d-statut").innerHTML = pilule(i.statut);
  $("d-nom").textContent = i.dossier;
  const bloc = (titre, classe, lignes) => lignes.length
    ? `<div class="bloc ${classe}"><h3>${titre}</h3><ul>${lignes.join("")}</ul></div>` : "";
  const sources = i.qr.length
    ? `Registre officiel (QR) : ${i.qr.map((s) => s.toUpperCase()).join(", ")}` : "Lecture du document scanné";
  let corps = "";
  if (i.statut === "VALIDÉ") {
    corps += `<div class="bloc"><h3>Décision</h3><ul><li>Toutes les règles de contrôle sont respectées.</li></ul></div>`;
  }
  if (i.erreur) corps += bloc("Erreur technique", "rejet", [`<li>${echapper(i.erreur)}</li>`]);
  corps += bloc("Motifs de rejet", "rejet",
    i.bloquants.map((b) => `<li><strong>${echapper(b.motif)}</strong>${echapper(b.detail)}</li>`));
  corps += bloc("Points à vérifier", "attention", i.vigilance.map((v) => `<li>${echapper(v)}</li>`));
  corps += bloc("Informations", "", i.particuliers.map((p) => `<li>${echapper(p)}</li>`));
  corps += `<div class="bloc"><h3>Données lues</h3><dl class="infos">
      <dt>Nom sur la CIP</dt><dd>${echapper(i.cip_nom) || "–"}</dd>
      <dt>Expiration CIP</dt><dd>${echapper(i.cip_expiration) || "non confirmée"}</dd>
      <dt>N° RCCM</dt><dd>${echapper(i.rccm) || "–"}</dd>
      <dt>N° IFU</dt><dd>${echapper(i.ifu) || "–"}</dd>
      <dt>Source RCCM/IFU</dt><dd>${sources}</dd>
      <dt>Pages lues par Claude</dt><dd>${i.pages_envoyees} sur ${i.pages}</dd>
      <dt>Coût</dt><dd>${dollars(i.cout, 4)}</dd></dl></div>`;
  $("d-corps").innerHTML = corps;
  $("voile").hidden = false;
  $("tiroir").classList.add("ouvert");
  $("tiroir").setAttribute("aria-hidden", "false");
}
function fermerDetail() {
  $("tiroir").classList.remove("ouvert");
  $("tiroir").setAttribute("aria-hidden", "true");
  $("voile").hidden = true;
}
$("fermer").addEventListener("click", fermerDetail);
$("voile").addEventListener("click", fermerDetail);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") fermerDetail(); });
$("d-pdf").addEventListener("click", async () => {
  const r = await api().ouvrir_pdf(etat.sortie, etat.detail.dossier);
  if (!r.ok) toast(r.message);
});

/* ---------------------------------------------------------------- nouveau contrôle */

function majModele() {
  document.querySelectorAll("#modele button").forEach((b) => b.classList.toggle("actif", b.dataset.modele === etat.modele));
  majEstimation();
}
document.querySelectorAll("#modele button").forEach((b) => b.addEventListener("click", () => {
  etat.modele = b.dataset.modele;
  majModele();
}));

function majEstimation() {
  const c = etat.config;
  const pret = c && c.a_cle && c.moteur_ok;
  $("lancer").disabled = !(pret && etat.nbPdf > 0) || etat.suivi?.en_cours;
  const alerte = $("alerte-lancer");
  if (c && !c.a_cle) { alerte.textContent = "Ajoutez votre clé API Claude dans Paramètres."; alerte.hidden = false; }
  else if (c && !c.moteur_ok) { alerte.textContent = "Moteur de contrôle introuvable : vérifiez Paramètres."; alerte.hidden = false; }
  else alerte.hidden = true;
  if (!etat.nbPdf || !c) return;
  const cout = etat.nbPdf * c.cout[etat.modele];
  const minutes = Math.max(2, Math.round(etat.nbPdf * c.minutes));
  const duree = minutes >= 90 ? `${nombre.format(Math.round(minutes / 6) / 10)} h` : `${minutes} min`;
  $("estimation").innerHTML = `<strong>${nombre.format(etat.nbPdf)} dossiers</strong> · coût estimé
    <strong>~${dollars(cout)}</strong> · durée estimée <strong>~${duree}</strong>`;
}

$("choisir").addEventListener("click", async () => {
  const r = await api().choisir_dossier();
  if (!r.chemin) return;
  etat.dossier = r.chemin;
  etat.nbPdf = r.nb_pdf;
  $("chemin").innerHTML = `<strong>${echapper(r.chemin)}</strong> — ${nombre.format(r.nb_pdf)} PDF trouvé${r.nb_pdf > 1 ? "s" : ""}`;
  if (!r.nb_pdf) $("estimation").textContent = "Aucun PDF dans ce dossier.";
  majEstimation();
});

$("lancer").addEventListener("click", () => demarrer(etat.dossier, etat.modele, ""));

async function demarrer(dossier, modele, reprendre) {
  const r = await api().lancer(dossier, modele, reprendre);
  if (!r.ok) { toast(r.message); return; }
  allerA("lancer");
  $("formulaire").hidden = true;
  $("interrompus").hidden = true;
  $("suivi").hidden = false;
  $("voir-resultats").hidden = true;
  $("ouvrir-tri").hidden = true;
  $("s-tri").hidden = true;
  $("s-erreur").hidden = true;
  $("arreter").hidden = false;
  $("suivi-titre").textContent = reprendre ? "Reprise en cours" : "Vérification en cours";
  suivre();
}

async function suivre() {
  const e = await api().avancement();
  etat.suivi = e;
  const part = e.total ? e.fait / e.total : 0;
  $("progression-barre").style.width = `${Math.round(part * 100)}%`;
  $("s-fait").textContent = nombre.format(e.fait);
  $("s-total").textContent = nombre.format(e.total);
  $("s-ecoule").textContent = `${Math.floor(e.ecoule_s / 60)} min`;
  $("s-cout").textContent = dollars(e.cout);
  const nouveaux = Object.values(e.compte).reduce((a, b) => a + b, 0);
  if (e.en_cours && nouveaux >= 3 && e.total > e.fait) {
    const reste = Math.round(((e.ecoule_s / nouveaux) * (e.total - e.fait)) / 60);
    $("s-reste").innerHTML = `Reste environ <strong>${reste >= 90 ? nombre.format(Math.round(reste / 6) / 10) + " h" : reste + " min"}</strong>`;
  } else $("s-reste").textContent = e.en_cours ? "Lecture des premiers dossiers…" : "";
  $("s-compte").innerHTML = Object.keys(STATUTS).filter((s) => e.compte[s])
    .map((s) => `${pilule(s)} <strong>${nombre.format(e.compte[s])}</strong>`).join("&nbsp;&nbsp;");
  $("s-derniers").innerHTML = e.derniers.map((d) => `<li>${pilule(d.statut)} ${echapper(d.dossier)}</li>`).join("");

  if (e.en_cours) { setTimeout(suivre, 1500); return; }
  $("arreter").hidden = true;
  if (e.erreur) {
    $("suivi-titre").textContent = "Contrôle interrompu";
    $("s-erreur").textContent = e.erreur;
    $("s-erreur").hidden = false;
  } else {
    $("suivi-titre").textContent = e.arrete ? "Contrôle arrêté" : "Vérification terminée";
  }
  $("voir-resultats").hidden = !e.sortie;
  $("ouvrir-tri").hidden = !e.tri;
  $("s-tri").hidden = !(e.tri || e.tri_erreur);
  $("s-tri").textContent = e.tri ? `PDF rangés par décision dans : ${e.tri}` : e.tri_erreur;
  $("formulaire").hidden = false;
  majEstimation();
  afficherInterrompus();
}

$("arreter").addEventListener("click", async () => {
  if (!confirm("Arrêter le contrôle ? Les dossiers déjà traités sont conservés et vous pourrez reprendre.")) return;
  await api().arreter();
});
$("ouvrir-tri").addEventListener("click", () => api().ouvrir_sortie(etat.suivi.tri));
$("voir-resultats").addEventListener("click", () => {
  etat.sortie = etat.suivi.sortie;
  allerA("tableau");
});

async function afficherInterrompus() {
  const liste = (await api().lancements()).filter((l) => !l.termine && !l.en_cours);
  $("interrompus").hidden = !liste.length || etat.suivi?.en_cours;
  $("liste-interrompus").innerHTML = liste.map((l, k) => `<li>
      <span><strong>${l.date}</strong> — ${nombre.format(l.nb)} sur ${nombre.format(l.total_source || l.nb)} dossiers<br>
      <small class="sous">${echapper(l.dossier)}</small></span>
      <button class="btn secondaire" data-k="${k}">Reprendre</button></li>`).join("");
  $("liste-interrompus").querySelectorAll("button").forEach((b) => b.addEventListener("click", () => {
    const l = liste[Number(b.dataset.k)];
    demarrer(l.dossier, l.modele || etat.modele, l.sortie);
  }));
}

/* ---------------------------------------------------------------- paramètres */

function message(texte, ok) {
  const m = $("cle-message");
  m.textContent = texte;
  m.className = `message ${ok ? "ok" : "ko"}`;
  m.hidden = false;
}

async function afficherParametres() {
  await rafraichirConfig();
  const c = etat.config;
  $("cle-etat").textContent = c.a_cle
    ? `Clé enregistrée (${c.cle_apercu}). Saisissez-en une nouvelle pour la remplacer.`
    : "Aucune clé enregistrée. Collez votre clé API Claude ci-dessous.";
  $("oublier-cle").hidden = !c.a_cle;
  document.querySelectorAll("#modele-defaut button").forEach((b) => b.classList.toggle("actif", b.dataset.modele === c.modele));
  $("carte-moteur").hidden = !!c.installe;  // application installée : moteur intégré
  $("moteur").value = c.moteur;
  $("moteur-etat").textContent = c.moteur_ok ? "Moteur trouvé (controle.py)." : "controle.py introuvable dans ce dossier.";
}

$("enregistrer-cle").addEventListener("click", async () => {
  const cle = $("cle").value.trim();
  if (!cle) { message("Collez d'abord une clé.", false); return; }
  const t = await api().tester_cle(cle);
  if (!t.ok) { message(`${t.message} La clé n'a pas été enregistrée.`, false); return; }
  await api().enregistrer(cle, "", "");
  $("cle").value = "";
  await afficherParametres();
  message("Clé vérifiée et enregistrée.", true);
});
$("tester-cle").addEventListener("click", async () => {
  message("Test en cours…", true);
  const t = await api().tester_cle($("cle").value);
  message(t.message, t.ok);
});
$("oublier-cle").addEventListener("click", async () => {
  if (!confirm("Supprimer la clé enregistrée sur ce PC ?")) return;
  await api().oublier_cle();
  await afficherParametres();
  message("Clé supprimée.", true);
});
document.querySelectorAll("#modele-defaut button").forEach((b) => b.addEventListener("click", async () => {
  await api().enregistrer("", b.dataset.modele, "");
  afficherParametres();
}));
$("choisir-moteur").addEventListener("click", async () => {
  const chemin = await api().choisir_moteur();
  if (chemin) $("moteur").value = chemin;
});
$("enregistrer-moteur").addEventListener("click", async () => {
  await api().enregistrer("", "", $("moteur").value);
  afficherParametres();
});

/* ---------------------------------------------------------------- démarrage */

window.addEventListener("pywebviewready", async () => {
  await rafraichirConfig();
  const e = await api().avancement();
  if (e.en_cours) { etat.suivi = e; allerA("lancer"); $("formulaire").hidden = true; $("suivi").hidden = false; suivre(); }
  else allerA(etat.config.a_cle ? "tableau" : "parametres");
});
