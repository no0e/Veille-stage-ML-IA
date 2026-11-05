"""Liste de moyennes entreprises (50 à 999 salariés) pour les candidatures spontanées.

Interroge l'API publique recherche-entreprises.api.gouv.fr (gratuite, sans clé) et écrit
data/entreprises.csv (s'ouvre directement dans Excel).

Usage :
    python entreprises.py                      # réglages par défaut ci-dessous
    python entreprises.py --dep 35 31 --naf 62.01Z 72.19Z
"""
import argparse
import csv
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
SORTIE = BASE / "data" / "entreprises.csv"
API = "https://recherche-entreprises.api.gouv.fr/search"

# Codes NAF : logiciel, conseil informatique, traitement de données, R&D, ingénierie.
NAF_DEFAUT = ["62.01Z", "62.02A", "63.11Z", "58.29C", "72.19Z", "71.12B"]
# Départements : Rennes, Toulouse, Paris, Hauts-de-Seine.
DEP_DEFAUT = ["35", "31", "75", "92"]
# Tranches INSEE : 21 = 50-99, 22 = 100-199, 31 = 200-249, 32 = 250-499, 41 = 500-999 salariés.
TRANCHES = {"21": "50-99", "22": "100-199", "31": "200-249", "32": "250-499", "41": "500-999"}
PAGES_MAX = 20  # 25 résultats par page, donc 500 entreprises max par couple (NAF, département)


def chercher(naf: str, dep: str) -> list[dict]:
    resultats = []
    for page in range(1, PAGES_MAX + 1):
        params = {
            "activite_principale": naf,
            "departement": dep,
            "tranche_effectif_salarie": ",".join(TRANCHES),
            "etat_administratif": "A",
            "per_page": 25,
            "page": page,
        }
        url = f"{API}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"User-Agent": "veille-stages"})
        try:
            with urllib.request.urlopen(req, timeout=20) as rep:
                data = json.load(rep)
        except Exception as err:  # 429 (limite de débit) ou réseau : on passe au suivant
            print(f"  ! {naf} / {dep} page {page} : {err}")
            break
        resultats += data.get("results", [])
        if page >= data.get("total_pages", 0):
            break
        time.sleep(0.2)  # l'API limite à environ 7 requêtes par seconde
    return resultats


def ligne(e: dict, naf: str) -> dict:
    siege = e.get("siege") or {}
    nom = e.get("nom_complet") or e.get("nom_raison_sociale") or ""
    return {
        "nom": nom,
        "effectif": TRANCHES.get(e.get("tranche_effectif_salarie"), e.get("tranche_effectif_salarie") or ""),
        "categorie": e.get("categorie_entreprise") or "",
        "ville": siege.get("libelle_commune") or "",
        "code_postal": siege.get("code_postal") or "",
        "naf": e.get("activite_principale") or naf,
        "creation": e.get("date_creation") or "",
        "siren": e.get("siren") or "",
        "fiche": f"https://annuaire-entreprises.data.gouv.fr/entreprise/{e.get('siren', '')}",
        "linkedin": "https://www.linkedin.com/search/results/companies/?keywords=" + urllib.parse.quote(nom),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--naf", nargs="+", default=NAF_DEFAUT, help="codes NAF")
    parser.add_argument("--dep", nargs="+", default=DEP_DEFAUT, help="départements")
    args = parser.parse_args()

    entreprises = {}
    for naf in args.naf:
        for dep in args.dep:
            trouvees = chercher(naf, dep)
            print(f"{naf} / {dep} : {len(trouvees)} entreprise(s)")
            for e in trouvees:
                entreprises.setdefault(e.get("siren"), ligne(e, naf))

    lignes = sorted(entreprises.values(), key=lambda r: (r["code_postal"][:2], r["nom"]))
    SORTIE.parent.mkdir(parents=True, exist_ok=True)
    with SORTIE.open("w", newline="", encoding="utf-8-sig") as f:  # utf-8-sig : accents corrects dans Excel
        writer = csv.DictWriter(f, fieldnames=list(lignes[0]) if lignes else ["nom"], delimiter=";")
        writer.writeheader()
        writer.writerows(lignes)
    print(f"\n{len(lignes)} entreprises écrites dans {SORTIE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
