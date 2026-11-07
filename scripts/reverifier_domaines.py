"""Revérification des adresses marquées « invalide » à cause de leur domaine.

Jusqu'au 07/11, la vérification des domaines (lire_sites.verifier_domaines) lançait toutes ses requêtes DNS
en même temps : le résolveur saturait et chaque expiration comptait comme un domaine inexistant. Ce script
relit, avec la vérification corrigée, le domaine de chaque adresse « invalide » (hors chaînes et réseaux) :
- le domaine répond (MX ou A) → « valide » ;
- le domaine n'existe pas, ou n'a ni MX ni A → reste « invalide » ;
- pas de réponse → « a_verifier » (revérifiée par la lecture des sites suivante).
Les règles des chaînes et réseaux (commun.marquer_chaines) sont réappliquées à la synchronisation vers D1.
Aucun e-mail n'est envoyé.

Variables : celles de la base (BASE_FICHIER pour le fichier de travail), RESUME (facultatif).
"""

import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
from commun import DOMAINES_CHAINES, maintenant, ouvrir_base  # noqa: E402
from lire_sites import statut_domaine, verifier_domaines  # noqa: E402


async def principal():
    debut = time.time()
    base = ouvrir_base()
    domaines = {r["domaine"] for r in base.requete("SELECT DISTINCT domaine FROM emails WHERE statut = 'invalide'")
                if r["domaine"] and r["domaine"] not in DOMAINES_CHAINES}
    print(f"{len(domaines)} domaines à revérifier")
    resultats = await verifier_domaines(domaines)
    date = maintenant()
    resume = {"domaines": len(domaines)}
    for statut in ("valide", "a_verifier"):
        lot = sorted(d for d, v in resultats.items() if statut_domaine(v) == statut)
        resume[f"domaines_{statut}"] = len(lot)
        avant = base.lignes_ecrites
        for i in range(0, len(lot), 90):
            part = lot[i:i + 90]
            base.requete(f"""UPDATE emails SET statut = ?, verifie_le = ?
                             WHERE statut = 'invalide' AND domaine IN ({','.join('?' * len(part))})""",
                         [statut, date] + part)
        resume[f"adresses_{statut}"] = base.lignes_ecrites - avant
    resume["domaines_invalides"] = sum(1 for v in resultats.values() if v is False)
    resume["duree_s"] = round(time.time() - debut)
    print(json.dumps(resume, ensure_ascii=False, indent=1))
    if os.environ.get("RESUME"):
        with open(os.environ["RESUME"], "w") as f:
            json.dump(resume, f, ensure_ascii=False, indent=1)
    base.journal("reverifier_domaines", resume)


if __name__ == "__main__":
    asyncio.run(principal())
