"""Traitement des dossiers marchands via la Batch API (-50 %, résultats en différé).

Même préparation, même prompt et mêmes règles que bench_dossiers.py.

Usage :
    python batch_dossiers.py soumettre [--exclude "NOM 1" "NOM 2" ...]
    python batch_dossiers.py suivre sorties/batch_AAAAMMJJ_HHMMSS   # reprise du suivi
"""

import argparse
import csv
import json
import random
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import date, datetime
from pathlib import Path

import anthropic
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request

from bench_dossiers import (MODEL, PRICE_IN, PRICE_OUT, ROOT, SYSTEM, apply_rules,
                            build_content, load_api_key, marquer_doublons, parse_json,
                            render_pages)

BATCH_DISCOUNT = 0.5
MAX_LOT_OCTETS = 100_000_000  # marge sous la limite de 256 Mo par batch
POLL_S = 60


def _preparer(args_tuple):
    """Exécuté dans un processus séparé : rendu des pages + contenu du message."""
    pdf, max_side, quality = args_tuple
    t0 = time.perf_counter()
    images = render_pages(Path(pdf), max_side, quality)
    content = build_content(images)
    taille = sum(len(b["source"]["data"]) for b in content if b["type"] == "image")
    return pdf, len(images), content, taille, time.perf_counter() - t0


def soumettre(args, client):
    pdfs = sorted(Path(args.dossier).glob("*.pdf"))
    exclus = {e.strip() for e in args.exclude}
    pdfs = [p for p in pdfs if p.stem.strip() not in exclus]
    if args.limit:
        pdfs = pdfs[:args.limit]

    out = ROOT / "sorties" / f"batch_{datetime.now():%Y%m%d_%H%M%S}"
    out.mkdir(parents=True)
    print(f"{len(pdfs)} dossier(s) à préparer -> {out}")

    t0 = time.perf_counter()
    correspondance, pages, lots, lot, taille_lot = {}, {}, [], [], 0
    with ProcessPoolExecutor() as pool:
        jobs = [(str(p), args.max_side, args.quality) for p in pdfs]
        for i, (pdf, n_pages, content, taille, duree) in enumerate(pool.map(_preparer, jobs), 1):
            nom = Path(pdf).stem.strip()
            custom_id = f"d{i:04d}"  # custom_id : lettres/chiffres uniquement
            correspondance[custom_id] = nom
            pages[custom_id] = n_pages
            print(f"  préparé {nom:<40} {n_pages:>2}p {taille / 1e6:5.1f} Mo  {duree:4.1f}s")
            if lot and taille_lot + taille > MAX_LOT_OCTETS:
                lots.append(lot)
                lot, taille_lot = [], 0
            lot.append(Request(custom_id=custom_id, params=MessageCreateParamsNonStreaming(
                model=MODEL,
                max_tokens=16000,
                system=SYSTEM,
                messages=[{"role": "user", "content": content}],
                output_config={"effort": args.effort},
            )))
            taille_lot += taille
    if lot:
        lots.append(lot)
    t_prep = time.perf_counter() - t0
    print(f"Préparation : {t_prep:.0f}s pour {len(pdfs)} dossiers ({len(lots)} lot(s))")

    batch_ids = []
    for i, requetes in enumerate(lots, 1):
        t1 = time.perf_counter()
        batch = client.messages.batches.create(requests=requetes)
        batch_ids.append(batch.id)
        print(f"  lot {i}/{len(lots)} envoyé : {batch.id} ({len(requetes)} dossiers, "
              f"{time.perf_counter() - t1:.0f}s d'envoi)")

    etat = {"batch_ids": batch_ids, "dossiers": correspondance, "pages": pages,
            "soumis_a": datetime.now().isoformat(timespec="seconds"),
            "t_preparation_s": round(t_prep, 1), "effort": args.effort,
            "max_side": args.max_side, "date_traitement": date.today().isoformat()}
    (out / "etat.json").write_text(json.dumps(etat, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def suivre(out: Path, client):
    etat = json.loads((out / "etat.json").read_text(encoding="utf-8"))
    soumis = datetime.fromisoformat(etat["soumis_a"])
    while True:
        try:
            batches = [client.messages.batches.retrieve(b) for b in etat["batch_ids"]]
        except anthropic.APIConnectionError:
            # coupure réseau : le batch continue chez Anthropic, on réessaie plus tard
            print(f"[{datetime.now():%H:%M:%S}] connexion perdue, nouvel essai dans {POLL_S}s",
                  flush=True)
            time.sleep(POLL_S)
            continue
        fini = sum(b.request_counts.succeeded + b.request_counts.errored
                   + b.request_counts.canceled + b.request_counts.expired for b in batches)
        total = len(etat["dossiers"])
        attente = (datetime.now() - soumis).total_seconds() / 60
        print(f"[{datetime.now():%H:%M:%S}] {fini}/{total} terminés "
              f"({', '.join(b.processing_status for b in batches)}) - {attente:.0f} min", flush=True)
        if all(b.processing_status == "ended" for b in batches):
            break
        time.sleep(POLL_S)
    fin = max(b.ended_at for b in batches)
    duree_min = (fin - batches[0].created_at).total_seconds() / 60

    date_traitement = date.fromisoformat(etat["date_traitement"])
    rows = []
    with open(out / "resultats.jsonl", "w", encoding="utf-8") as log:
        for batch_id in etat["batch_ids"]:
            for res in client.messages.batches.results(batch_id):
                nom = etat["dossiers"][res.custom_id]
                row = {"dossier": nom, "pages": etat["pages"][res.custom_id],
                       "tokens_in": 0, "tokens_out": 0, "cout_usd": 0.0,
                       "statut": "ERREUR", "motifs_rejet": "", "detail": "", "rccm": "",
                       "erreur": ""}
                try:
                    if res.result.type != "succeeded":
                        err = getattr(res.result, "error", None)
                        raise RuntimeError(f"{res.result.type} {err or ''}")
                    msg = res.result.message
                    u = msg.usage
                    row["tokens_in"], row["tokens_out"] = u.input_tokens, u.output_tokens
                    row["cout_usd"] = round((u.input_tokens * PRICE_IN
                                             + u.output_tokens * PRICE_OUT) * BATCH_DISCOUNT, 4)
                    if msg.stop_reason == "refusal":
                        raise RuntimeError(f"refus du modèle ({msg.stop_details})")
                    if msg.stop_reason == "max_tokens":
                        raise RuntimeError("réponse tronquée (max_tokens)")
                    data = parse_json(next(b.text for b in msg.content if b.type == "text"))
                    decision = apply_rules(data, date_traitement, nom)
                    row["statut"] = decision["statut"]
                    row["rccm"] = data["rccm"]["numero"] or data["ifu"]["rccm"]
                    row["motifs_rejet"] = " | ".join(decision["motifs_rejet"])
                    row["detail"] = " | ".join(
                        [f"{b['motif']} : {b['detail']}" for b in decision["bloquants"]]
                        + decision["vigilance"])
                    log.write(json.dumps({"dossier": nom, "batch_id": batch_id,
                                          "custom_id": res.custom_id, "model": msg.model,
                                          "horodatage": datetime.now().isoformat(timespec="seconds"),
                                          "decision": decision, "extraction": data},
                                         ensure_ascii=False) + "\n")
                except Exception as e:  # un dossier en erreur ne bloque pas le lot
                    row["erreur"] = f"{type(e).__name__}: {e}"
                rows.append(row)

    doublons = marquer_doublons(rows)
    rows.sort(key=lambda r: r["dossier"])
    with open(out / "resultats.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]), delimiter=";")
        w.writeheader()
        w.writerows(rows)

    ok = [r for r in rows if not r["erreur"]]
    cout = sum(r["cout_usd"] for r in ok)
    lines = [
        f"Dossiers : {len(rows)}  (réussis {len(ok)}, erreurs {len(rows) - len(ok)})",
        f"Préparation locale : {etat['t_preparation_s']:.0f}s | traitement batch : "
        f"{duree_min:.1f} min (de la création à la fin du dernier lot)",
        f"Coût batch : {cout:.2f} $ au total, {cout / max(len(ok), 1):.3f} $ / dossier",
        f"Tokens moyens : {sum(r['tokens_in'] for r in ok) / max(len(ok), 1):.0f} en entrée, "
        f"{sum(r['tokens_out'] for r in ok) / max(len(ok), 1):.0f} en sortie",
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
    for r in rows:
        if r["erreur"]:
            lines.append(f"ERREUR {r['dossier']} : {r['erreur'][:300]}")
    if ok:
        sample = random.sample(ok, max(1, round(len(ok) * 0.05)))
        lines.append("Échantillon de contrôle (5 %) : " + ", ".join(r["dossier"] for r in sample))
    summary = "\n".join(lines)
    (out / "synthese.txt").write_text(summary, encoding="utf-8")
    print("\n" + summary)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="action", required=True)
    s = sub.add_parser("soumettre")
    s.add_argument("--dossier", default=str(ROOT / "Dossier test"))
    s.add_argument("--exclude", nargs="*", default=[])
    s.add_argument("--limit", type=int, default=0)
    s.add_argument("--effort", default="medium", choices=["low", "medium", "high", "xhigh", "max"])
    s.add_argument("--max-side", type=int, default=1800)
    s.add_argument("--quality", type=int, default=80)
    s.add_argument("--sans-suivi", action="store_true", help="envoyer puis quitter")
    r = sub.add_parser("suivre")
    r.add_argument("sortie")
    args = ap.parse_args()

    client = anthropic.Anthropic(api_key=load_api_key(), max_retries=4, timeout=900)
    if args.action == "soumettre":
        out = soumettre(args, client)
        if args.sans_suivi:
            print(f"Suivi plus tard : python batch_dossiers.py suivre \"{out}\"")
            return
    else:
        out = Path(args.sortie)
    suivre(out, client)


if __name__ == "__main__":
    main()
