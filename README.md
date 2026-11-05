# Veille stages

Outil local qui collecte les offres de stage de tes sources chaque matin et t'aide à suivre tes candidatures.

## Installation (une fois)

1. Installer Python 3.11 ou plus récent (python.org, cocher « Add Python to PATH »).
2. Double-cliquer sur `installer.bat` (ou `pip install -r requirements.txt`).
3. Ouvrir `config/profile.json` : mettre ton nom, ton année de diplôme, et noter tes compétences de 1 à 3
   (elles servent à classer les offres). La base `data/veille.db` se crée toute seule au premier lancement.

## Lancer

Double-clique sur `lancer.bat` : la page s'ouvre sur http://127.0.0.1:5057.

Collecte automatique chaque matin (facultatif, Windows) : dans un terminal ouvert dans ce dossier,
`schtasks /Create /TN VeilleStagesML /SC DAILY /ST 08:30 /TR "python \"%CD%\collecte.py\""`.
Sinon, l'outil collecte tout seul à l'ouverture si la dernière collecte date de plus de 12 h.
Journal : `data/collecte.log`.

## Les trois onglets

- **Offres** : les offres regroupées par entreprise. Clique sur une entreprise pour voir ses offres.
  Filtres : recherche, ML / DL / Stats, zone, statut, prestige, nouvelles de la semaine. Chaque offre a son lien,
  son texte, et les boutons ★ Intéressé, ✓ Postulé et ✕ Écarter. « Masquer l'entreprise » cache toutes ses offres.
- **Candidatures** : chaque offre marquée « ✓ Postulé » y arrive toute seule ; les autres s'ajoutent à la main.
  Pour chacune : date d'envoi, étape, le CV et la lettre envoyés (bouton « Joindre »), notes.
  Les fichiers sont copiés dans `data/candidatures/<numéro>/` (bouton 📁 pour ouvrir le dossier).
- **Sources** : ajouter une page carrières (URL ou nom d'entreprise), activer / désactiver, changer le prestige,
  supprimer. Les sources sans collecte automatique sont des liens à ouvrir. Les entreprises masquées s'y réaffichent.

## Réglages (`config/profile.json`, rubrique `reglages`)

- `masquer_inaccessibles` : cache les offres réservées aux doctorants, à une autre nationalité, à une autre promo,
  ou exigeant une autorisation de travail américaine.
- `debut_au_plus_tot` : offres commençant avant cette date signalées (« 2027-03 »).
- `exclure_finance_de_marche`, `entreprises_exclues`, `rafraichissement_auto_heures`, `nouveau_jours`.

## Fichiers

- `app.py` : la page web. `collecte.py` : la collecte du matin.
- `veille/` : collecteurs (`fetchers.py`, `providers.py`), filtres (`scoring.py`), base (`db.py`).
- `config/sources_seed.json` : les sources installées au premier lancement.
- `data/veille.db` : la base locale.

## Candidatures spontanées

`python entreprises.py` liste les moyennes entreprises (50 à 999 salariés) actives par code NAF et département,
via l'API publique annuaire-entreprises, dans `data/entreprises.csv` (séparateur `;`, s'ouvre dans Excel).
Réglages en haut du fichier, ou en ligne de commande : `python entreprises.py --dep 35 31 --naf 62.01Z 72.19Z`.

Tests : `python -m pytest -q tests`
