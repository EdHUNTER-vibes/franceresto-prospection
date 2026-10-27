"""Synchronisation entre le fichier de travail (R2) et la base D1 du back-office.

Le fichier contient tout (établissements de tous les départements importés, sites lus, journal) ;
D1 ne garde que ce qui sert aux envois, pour rester loin de la limite gratuite de 100 000 lignes
écrites par jour :
- les établissements qui ont au moins une adresse e-mail (ou un envoi) ;
- toutes les adresses, leurs origines et les listes externes ;
- les campagnes, les envois et les exclusions.

- `tirer` (D1 → fichier, avant chaque traitement) : campagnes, envois et exclusions, et les statuts
  d'adresse posés par les envois (bounce, desinscrit, plainte). D1 en est la référence.
- `pousser` (fichier → D1, après) : seulement les lignes nouvelles ou modifiées, et la suppression de
  ce qui n'a plus sa place dans D1 (origines retirées, établissements sans adresse). Arrêt quand le
  budget d'écritures du passage (BUDGET, 60 000 par défaut) est atteint : le passage suivant reprend.
  Le budget est aussi limité par un plafond commun à tous les passages du jour (PLAFOND_JOUR, 75 000
  par défaut, compté dans le journal de D1) : le reste de la limite de 100 000 est gardé pour les
  envois et le back-office (enregistrement des campagnes, des envois et des événements Brevo).

Variables : BASE_FICHIER, BUDGET, PLAFOND_JOUR, RESUME ; celles de D1 (D1_TEST : autre fichier SQLite à la place
de D1, pour les essais).
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
from commun import D1, Fichier, QuotaD1, maintenant  # noqa: E402

STATUTS_ENVOI = ("bounce", "desinscrit", "plainte")
# (table, clé, lignes du fichier à mettre dans D1, mode) ; « miroir » : ajout, modification, suppression ;
# « ajout » : seulement les lignes absentes de D1 (D1 garde ses propres mises à jour).
TABLES = [
    ("etablissements", ["siret"],
     "SELECT * FROM etablissements WHERE siret IN (SELECT siret FROM email_sources) "
     "OR siret IN (SELECT siret FROM envois WHERE siret IS NOT NULL)", "miroir"),
    ("emails", ["email"], "SELECT * FROM emails", "miroir"),
    ("email_sources", ["email", "siret", "source"], "SELECT * FROM email_sources", "miroir"),
    ("listes_externes", ["email"], "SELECT * FROM listes_externes", "miroir"),
    ("campagnes", ["id"], "SELECT * FROM campagnes", "ajout"),
    ("envois", ["campagne_id", "email", "etape"], "SELECT * FROM envois", "ajout"),
]
SUPPRESSIONS = ["email_sources", "etablissements"]  # dans cet ordre (clés étrangères) ; jamais d'adresse
LOT = 400


class BudgetAtteint(Exception):
    pass


def ouvrir_d1() -> D1:
    return Fichier(os.environ["D1_TEST"]) if os.environ.get("D1_TEST") else D1()


def lire_d1(d1: D1, table: str, colonnes: list[str]) -> list[dict]:
    lignes, dernier = [], 0
    while True:
        page = d1.requete(f"SELECT rowid AS _r, {', '.join(colonnes)} FROM {table} "
                          "WHERE rowid > ? ORDER BY rowid LIMIT 2000", [dernier])
        if not page:
            return lignes
        dernier = page[-1]["_r"]
        lignes += page


def colonnes_de(base: Fichier, table: str) -> list[str]:
    return [c["name"] for c in base.requete(f"PRAGMA table_info({table})")]


def ecrire(base: D1, table: str, colonnes: list[str], cles: list[str], lignes: list[tuple], mode: str,
           budget: int | None = None):
    if mode == "ajout":
        conflit = f"ON CONFLICT ({', '.join(cles)}) DO NOTHING"
    else:
        maj = [f"{c} = excluded.{c}" for c in colonnes if c not in cles]
        if table == "emails":  # un statut posé par les envois n'est jamais effacé par le fichier
            maj = [m for m in maj if not m.startswith("statut ")] + [
                f"statut = CASE WHEN emails.statut IN {STATUTS_ENVOI} THEN emails.statut ELSE excluded.statut END"]
        conflit = f"ON CONFLICT ({', '.join(cles)}) DO UPDATE SET {', '.join(maj)}"
    for i in range(0, len(lignes), LOT):
        if budget is not None and base.lignes_ecrites >= budget:
            raise BudgetAtteint
        base.inserer(table, colonnes, lignes[i:i + LOT], conflit)


def tirer(fichier: Fichier, d1: D1) -> dict:
    resume = {}
    for table, cles in (("campagnes", ["id"]), ("envois", ["campagne_id", "email", "etape"]),
                        ("exclusions", ["email_hash", "raison"])):
        colonnes = colonnes_de(fichier, table)
        lignes = [tuple(l[c] for c in colonnes) for l in lire_d1(d1, table, colonnes)]
        ecrire(fichier, table, colonnes, cles, lignes, "miroir")
        resume[table] = len(lignes)
    statuts = d1.requete(f"SELECT email, statut FROM emails WHERE statut IN {STATUTS_ENVOI}")
    for s in statuts:
        fichier.requete("UPDATE emails SET statut = ? WHERE email = ?", [s["statut"], s["email"]])
    resume["statuts_envoi"] = len(statuts)
    return resume


def ecrites_aujourdhui(d1: D1) -> int:
    """Lignes écrites dans D1 depuis minuit UTC par les synchronisations (journal de D1)."""
    minuit = time.strftime("%Y-%m-%dT00:00:00Z", time.gmtime())
    return int(d1.requete("SELECT coalesce(sum(json_extract(detail, '$.lignes_ecrites_d1')), 0) AS n FROM journal "
                          "WHERE tache = 'synchro_d1' AND date >= ?", [minuit])[0]["n"])


def pousser(fichier: Fichier, d1: D1, budget: int, plafond_jour: int | None = None) -> dict:
    resume, voulu = {"complet": False}, {}
    if plafond_jour is not None:
        deja = ecrites_aujourdhui(d1)
        resume["deja_ecrites_aujourdhui"] = deja
        budget = min(budget, plafond_jour - deja)
        if budget <= 0:
            resume["plafond_jour_atteint"] = True
            resume["lignes_ecrites_d1"] = 0
            print(f"Plafond du jour ({plafond_jour} lignes) atteint : la suite après minuit (UTC)")
            return resume
    try:
        for table, cles, requete, mode in TABLES:
            colonnes = colonnes_de(fichier, table)
            souhaite = {tuple(l[c] for c in cles): tuple(l[c] for c in colonnes) for l in fichier.requete(requete)}
            actuel = {tuple(l[c] for c in cles): tuple(l[c] for c in colonnes) for l in lire_d1(d1, table, colonnes)}
            if mode == "ajout":
                a_ecrire = [v for k, v in souhaite.items() if k not in actuel]
            else:
                a_ecrire = [v for k, v in souhaite.items() if actuel.get(k) != v]
            voulu[table] = (cles, souhaite, actuel)
            resume[table] = {"fichier": len(souhaite), "d1": len(actuel), "ecrites": len(a_ecrire)}
            ecrire(d1, table, colonnes, cles, a_ecrire, mode, budget)
        for table in SUPPRESSIONS:
            cles, souhaite, actuel = voulu[table]
            en_trop = [k for k in actuel if k not in souhaite]
            resume[table]["supprimees"] = len(en_trop)
            par = 90 // len(cles)  # D1 : 100 paramètres au plus par requête
            for i in range(0, len(en_trop), par):
                if d1.lignes_ecrites >= budget:
                    raise BudgetAtteint
                lot = en_trop[i:i + par]
                d1.requete(f"DELETE FROM {table} WHERE ({', '.join(cles)}) IN "
                           f"(VALUES {', '.join('(' + ', '.join('?' * len(cles)) + ')' for _ in lot)})",
                           [x for k in lot for x in k])
        resume["complet"] = True
    except BudgetAtteint:
        print(f"Budget de {budget} lignes écrites atteint : la suite au prochain passage")
    except QuotaD1:
        # Le fichier de travail est déjà enregistré sur R2 : la synchronisation de nuit reprendra.
        resume["quota_d1_atteint"] = True
        print("Limite quotidienne d'écritures de D1 atteinte : la synchronisation de nuit reprendra")
    resume["lignes_ecrites_d1"] = d1.lignes_ecrites
    return resume


def principal():
    action = sys.argv[1] if len(sys.argv) > 1 else ""
    if action not in ("tirer", "pousser"):
        raise SystemExit("Usage : synchro_d1.py tirer|pousser")
    fichier, d1 = Fichier(os.environ["BASE_FICHIER"]), ouvrir_d1()
    if action == "tirer":
        resume = tirer(fichier, d1)
    else:
        resume = pousser(fichier, d1, int(os.environ.get("BUDGET", "60000")),
                         int(os.environ.get("PLAFOND_JOUR", "75000")))
        try:
            d1.journal("synchro_d1", resume)
        except QuotaD1:
            pass
    fichier.journal(f"synchro_d1_{action}", resume)
    print(json.dumps(resume, ensure_ascii=False, indent=1))
    if os.environ.get("RESUME"):
        with open(os.environ["RESUME"], "a") as f:
            f.write(json.dumps({action: resume, "date": maintenant()}, ensure_ascii=False, indent=1) + "\n")


if __name__ == "__main__":
    principal()
