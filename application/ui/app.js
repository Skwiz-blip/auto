"use strict";

const $ = (id) => document.getElementById(id);
const api = () => window.pywebview.api;
const nombre = new Intl.NumberFormat("fr-FR");
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
  commercial: "", grouper: false,
  dossier: "", nbPdf: 0, nbTraites: 0, suivi: null, detail: null,
};

/* ---------------------------------------------------------------- navigation */

function allerA(vue) {
  document.querySelectorAll(".vue").forEach((v) => (v.hidden = v.id !== `vue-${vue}`));
  document.querySelectorAll(".lien").forEach((l) => l.classList.toggle("actif", l.dataset.vue === vue));
  if (vue === "tableau") chargerLancements();
  if (vue === "lancer") afficherInterrompus();
  if (vue === "parametres") afficherParametres();
  if (vue === "aide") afficherAide();
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
  majEstimation();
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
    return `<option value="${echapper(l.sortie)}">${l.date} · ${nombre.format(l.nb)} dossiers${suffixe}</option>`;
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
  $("k-nb").textContent = nombre.format(d.nb);
  $("k-qr").textContent = `${nombre.format(d.avec_qr)} vérifiés par QR officiel`;
  $("k-valide").textContent = nombre.format(c["VALIDÉ"]);
  $("k-valide-p").textContent = pourcent(c["VALIDÉ"], d.nb);
  $("k-verif").textContent = nombre.format(c["À VÉRIFIER"]);
  $("k-verif-p").textContent = pourcent(c["À VÉRIFIER"], d.nb);
  $("k-rejet").textContent = nombre.format(c["REJETÉ"]);
  $("k-rejet-p").textContent = pourcent(c["REJETÉ"], d.nb) + (c["ERREUR"] ? ` · ${c["ERREUR"]} en erreur` : "");
  $("k-duree").textContent = d.minutes != null ? `${nombre.format(Math.round(d.minutes))} min` : "–";
  $("k-modele").textContent = d.modele ? `Modèle ${d.modele}` : "";

  dessinerRepartition(d);
  dessinerBarres($("motifs"), d.motifs, "Aucun dossier rejeté.");
  dessinerBarres($("raisons"), d.raisons, "Aucun dossier à vérifier.");
  dessinerFiltres();
  dessinerChoixCommercial();
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

function dessinerChoixCommercial() {
  const liste = etat.donnees.commerciaux || [];
  if (etat.commercial && !liste.some(([nom]) => nom === etat.commercial)) etat.commercial = "";
  $("choix-commercial").innerHTML = `<option value="">Tous les commerciaux</option>` +
    liste.map(([nom, c]) => `<option value="${echapper(nom)}"${nom === etat.commercial ? " selected" : ""}>
      ${echapper(nom)} (${c.total})</option>`).join("");
}
$("choix-commercial").addEventListener("change", (e) => { etat.commercial = e.target.value; dessinerTable(); });
$("grouper").addEventListener("change", (e) => { etat.grouper = e.target.checked; dessinerTable(); });

function ligneDossier(i, k) {
  return `<tr data-k="${k}">
      <td>${pilule(i.statut)}</td>
      <td class="nom">${echapper(i.dossier)}</td>
      <td class="commercial">${echapper(i.commercial)}</td>
      <td class="pourquoi">${echapper(pourquoi(i))}</td>
      <td>${i.qr.length ? '<span class="badge officiel">QR officiel</span>' : '<span class="badge">Lecture du scan</span>'}</td></tr>`;
}

function dessinerTable() {
  const q = etat.recherche.trim().toLowerCase();
  let items = etat.donnees.items.filter((i) =>
    (etat.filtre === "tous" || i.statut === etat.filtre)
    && (!etat.commercial || i.commercial === etat.commercial)
    && (!q || i.dossier.toLowerCase().includes(q) || i.commercial.toLowerCase().includes(q)));
  if (!items.length) {
    $("lignes").innerHTML = `<tr><td colspan="5" class="table-vide">Aucun dossier.</td></tr>`;
    return;
  }
  if (!etat.grouper) {
    $("lignes").innerHTML = items.map(ligneDossier).join("");
  } else {
    // regroupement : un en-tête par commercial (non identifiés en dernier)
    const inconnu = "Commercial non identifié";
    items = [...items].sort((a, b) => (a.commercial === inconnu) - (b.commercial === inconnu)
      || a.commercial.localeCompare(b.commercial, "fr"));
    let html = "", courant = null;
    items.forEach((i, k) => {
      if (i.commercial !== courant) {
        courant = i.commercial;
        const du = items.filter((x) => x.commercial === courant);
        const n = (s) => du.filter((x) => x.statut === s).length;
        html += `<tr class="groupe"><td colspan="5"><strong>${echapper(courant)}</strong>
          <span>${du.length} dossier${du.length > 1 ? "s" : ""} · ${n("REJETÉ")} rejeté${n("REJETÉ") > 1 ? "s" : ""}
          · ${n("À VÉRIFIER")} à vérifier · ${n("VALIDÉ")} validé${n("VALIDÉ") > 1 ? "s" : ""}</span></td></tr>`;
      }
      html += ligneDossier(i, k);
    });
    $("lignes").innerHTML = html;
  }
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
      <dt>Commercial</dt><dd>${echapper(i.commercial)}</dd>
      <dt>Nom sur la CIP</dt><dd>${echapper(i.cip_nom) || "–"}</dd>
      <dt>Expiration CIP</dt><dd>${echapper(i.cip_expiration) || "non confirmée"}</dd>
      <dt>N° RCCM</dt><dd>${echapper(i.rccm) || "–"}</dd>
      <dt>N° IFU</dt><dd>${echapper(i.ifu) || "–"}</dd>
      <dt>Source RCCM/IFU</dt><dd>${sources}</dd>
      <dt>Pages lues par Claude</dt><dd>${i.pages_envoyees} sur ${i.pages}</dd></dl></div>`;
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

function majEstimation() {
  const c = etat.config;
  const pret = c && c.a_cle && c.moteur_ok;
  $("lancer").disabled = !(pret && etat.nbTraites > 0) || etat.suivi?.en_cours;
  const alerte = $("alerte-lancer");
  if (c && !c.a_cle) { alerte.textContent = "Ajoutez votre clé API Claude dans Paramètres."; alerte.hidden = false; }
  else if (c && !c.moteur_ok) { alerte.textContent = "Moteur de contrôle introuvable : vérifiez Paramètres."; alerte.hidden = false; }
  else alerte.hidden = true;
  if (!etat.nbTraites || !c) return;
  const minutes = Math.max(2, Math.round(etat.nbTraites * c.minutes));
  const duree = minutes >= 90 ? `${nombre.format(Math.round(minutes / 6) / 10)} h` : `${minutes} min`;
  $("estimation").innerHTML = `<strong>${nombre.format(etat.nbTraites)} dossiers</strong> · lecture sur ce PC
    <strong>~${duree}</strong>`;
}

function afficherDossier(r) {
  etat.dossier = r.chemin;
  etat.nbPdf = r.nb_pdf;
  etat.nbTraites = r.nb_traites;
  const morceaux = [`${nombre.format(r.nb_pdf)} PDF trouvé${r.nb_pdf > 1 ? "s" : ""}`];
  if (r.nb_deja) morceaux.push(`${nombre.format(r.nb_deja)} déjà contrôlé${r.nb_deja > 1 ? "s" : ""} (ignoré${r.nb_deja > 1 ? "s" : ""})`);
  let html = `<strong>${echapper(r.chemin)}</strong> ${morceaux.join(" · ")}`;
  if (r.nb_restants) html += ` · <span class="alerte-texte">${r.max} contrôlés maintenant, ${nombre.format(r.nb_restants)} au prochain lancement</span>`;
  $("chemin").innerHTML = html;
  if (!r.nb_pdf) $("estimation").textContent = "Aucun PDF dans ce dossier.";
  else if (!r.nb_traites) $("estimation").textContent = "Tous les PDF de ce dossier ont déjà été contrôlés.";
  majEstimation();
}

$("choisir").addEventListener("click", async () => {
  const r = await api().choisir_dossier();
  if (!r.chemin) return;
  // le dossier choisi est réanalysé selon la case « Recontrôler »
  afficherDossier(await api().analyser_dossier(r.chemin, $("recontroler").checked));
});
$("recontroler").addEventListener("change", async (e) => {
  if (etat.dossier) afficherDossier(await api().analyser_dossier(etat.dossier, e.target.checked));
});

$("lancer").addEventListener("click", () => demarrer(etat.dossier, "", $("recontroler").checked));

async function demarrer(dossier, reprendre, recontroler = false) {
  const r = await api().lancer(dossier, reprendre, recontroler);
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
  $("s-reste").textContent = "";
  // deux étapes : lecture sur ce PC, puis lots envoyés à Claude (réponses par paquets)
  const phases = [];
  if (e.en_cours && e.lus < e.total) phases.push(`Lecture sur ce PC : ${nombre.format(e.lus)} / ${nombre.format(e.total)}`);
  if (e.en_cours && e.lots) phases.push(`${nombre.format(e.lots)} lot${e.lots > 1 ? "s" : ""} chez Claude (${nombre.format(e.requetes)} demande${e.requetes > 1 ? "s" : ""}), réponse en général en moins d'une heure`);
  else if (e.en_cours && e.lus >= e.total && e.total) phases.push("Envoi à Claude…");
  $("s-phase").textContent = phases.join(" · ");
  $("s-phase").hidden = !phases.length;
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
    demarrer(l.dossier, l.sortie);
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
  $("carte-moteur").hidden = !!c.installe;  
  $("moteur").value = c.moteur;
  $("moteur-etat").textContent = c.moteur_ok ? "" : "controle.py introuvable dans ce dossier.";
}

$("enregistrer-cle").addEventListener("click", async () => {
  const cle = $("cle").value.trim();
  if (!cle) { message("Collez d'abord une clé.", false); return; }
  const t = await api().tester_cle(cle);
  if (!t.ok) { message(`${t.message} La clé n'a pas été enregistrée.`, false); return; }
  await api().enregistrer(cle, "");
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
$("choisir-moteur").addEventListener("click", async () => {
  const chemin = await api().choisir_moteur();
  if (chemin) $("moteur").value = chemin;
});
$("enregistrer-moteur").addEventListener("click", async () => {
  await api().enregistrer("", $("moteur").value);
  afficherParametres();
});

/* ---------------------------------------------------------------- guide d'utilisation */

const sansAccents = (t) => t.normalize("NFD").replace(/\p{Diacritic}/gu, "").toLowerCase();

function preparerGuide() {
  const sections = [...document.querySelectorAll(".guide-section")];
  const sommaire = $("guide-sommaire");
  // les pastilles de décision du guide sont celles du tableau de bord
  document.querySelectorAll("#vue-aide [data-statut]").forEach((e) => (e.outerHTML = pilule(e.dataset.statut)));
  // sommaire tiré des titres des rubriques : il reste juste si une rubrique change
  sommaire.innerHTML = sections.map((s) =>
    `<button type="button" data-cible="${s.id}">${echapper(s.querySelector("h2").textContent)}</button>`).join("");
  const boutons = [...sommaire.querySelectorAll("button")];
  boutons.forEach((b) => b.addEventListener("click", () =>
    $(b.dataset.cible).scrollIntoView({ behavior: "smooth", block: "start" })));

  // rubrique en cours de lecture, surlignée dans le sommaire
  const zone = document.querySelector("main");
  let attente = false;
  const surligner = () => {
    attente = false;
    const visibles = sections.filter((s) => !s.hidden);
    const courante = visibles.filter((s) => s.getBoundingClientRect().top < 160).pop() || visibles[0];
    boutons.forEach((b) => b.classList.toggle("actif", !!courante && b.dataset.cible === courante.id));
  };
  zone.addEventListener("scroll", () => {
    if (attente || $("vue-aide").hidden) return;
    attente = true;
    requestAnimationFrame(surligner);
  });

  // recherche : seules les rubriques qui contiennent les mots restent affichées
  $("guide-recherche").addEventListener("input", (e) => {
    const mots = sansAccents(e.target.value).split(/\s+/).filter(Boolean);
    let restantes = 0;
    sections.forEach((s, k) => {
      const texte = sansAccents(s.textContent);
      s.hidden = boutons[k].hidden = !mots.every((m) => texte.includes(m));
      restantes += !s.hidden;
    });
    $("guide-aucun").hidden = restantes > 0;
    surligner();
  });
  $("g-ouvrir-donnees").addEventListener("click", () => api().ouvrir_sortie(etat.config.donnees));
  surligner();
}

async function afficherAide() {
  if (!preparerGuide.fait) { preparerGuide(); preparerGuide.fait = true; }
  if (!etat.config) await rafraichirConfig();
  const c = etat.config;
  $("g-donnees").textContent = c.donnees;
  document.querySelectorAll(".g-max").forEach((e) => (e.textContent = c.max_dossiers));
  // liste réellement chargée par le programme : montre tout de suite un fichier absent ou vide
  const r = await api().commerciaux();
  const n = r.liste.length;
  $("g-commerciaux-etat").textContent = n
    ? `${n} commerci${n > 1 ? "aux reconnus" : "al reconnu"}.`
    : (r.present ? "Le fichier « NOM DES COMMERCIAUX.xlsx » est vide ou illisible : aucun commercial n'est reconnu."
                 : "Le fichier « NOM DES COMMERCIAUX.xlsx » est introuvable dans le dossier de vos fichiers : aucun commercial n'est reconnu.");
  $("g-commerciaux-etat").className = n ? "" : "alerte";
  $("g-commerciaux-table").hidden = !n;
  $("g-commerciaux-liste").innerHTML = r.liste.map((x) =>
    `<tr><td>${echapper(x.nom)}</td><td>${echapper(x.departement) || "–"}</td></tr>`).join("");
}

/* ---------------------------------------------------------------- démarrage */

window.addEventListener("pywebviewready", async () => {
  await rafraichirConfig();
  const e = await api().avancement();
  if (e.en_cours) { etat.suivi = e; allerA("lancer"); $("formulaire").hidden = true; $("suivi").hidden = false; suivre(); }
  else allerA(etat.config.a_cle ? "tableau" : "parametres");
});
