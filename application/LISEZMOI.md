# Contrôle des dossiers marchands : application

Interface de bureau du moteur de contrôle (`..\controle.py`, dossier parent).
L'application lance le moteur et lit ses résultats ; elle ne le modifie pas.

## Démarrer

Double-cliquer sur `Lancer.bat`.

Au premier lancement, aller dans **Paramètres** et coller la clé API Claude. La clé est
testée avant d'être enregistrée, puis chiffrée avec le compte Windows
(`%APPDATA%\KIK-Controle\config.json`).

## Utilisation

1. **Nouveau contrôle** : choisir le dossier des PDF, choisir le modèle, puis lancer.
   La progression, le coût et le temps restant s'affichent en direct.
2. Un contrôle arrêté ou interrompu se **reprend** là où il s'était arrêté : les dossiers
   déjà traités ne sont ni relus ni refacturés.
3. **Tableau de bord** : décisions, motifs de rejet, raisons des vérifications, puis la
   liste des dossiers. Cliquer sur une ligne pour voir le pourquoi et ouvrir le PDF.
4. **Exporter Excel** crée `resultats.xlsx` dans le dossier du lancement.

## Fichiers

| Fichier | Rôle |
|---|---|
| `app.py` | fenêtre, lancement du moteur, lecture des résultats |
| `ui/` | interface (HTML, CSS, JavaScript) |
| `Lancer.bat` | ouvre l'application sans console |

Les résultats restent dans `..\sorties\controle_*`.
