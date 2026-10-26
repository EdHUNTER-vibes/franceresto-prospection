"""Rattache les adresses des listes externes (table listes_externes) aux établissements SIRENE.

Pour chaque adresse pas encore rattachée (ou rattachée à une fiche provisoire « ext: ») :
1. domaine de l'adresse = domaine du site d'un établissement (hors messageries grand public) ;
2. sinon nom donné par la liste ressemblant à celui d'un établissement du même département
   (et de la même commune quand elle est connue) ;
3. sinon, pour les listes sans nom, partie locale de l'adresse reprenant le nom d'un seul
   établissement du département (ex. bistrotlesullychartres@ → « Le Sully », Chartres) ;
4. sinon fiche provisoire « ext:… » (nom, commune et département de la liste), reprise à chaque
   passage : dès qu'un établissement SIRENE correspond, l'adresse lui est rattachée.
L'adresse entre dans `emails` (domaine vérifié par MX) avec sa source (« prompt », nom de la liste) ;
un envoi déjà fait par la liste (statut « envoye ») est inscrit dans `envois` pour ne pas recontacter.

Variables : celles de D1 ; DEPARTEMENTS (facultatif) limite aux départements déjà importés.
"""

import asyncio
import hashlib
import json
import os
import re
import sys
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(__file__))
from commun import MESSAGERIES, liste_departements, maintenant, ouvrir_base, ressemblance, sans_accents, type_email  # noqa: E402
from lire_sites import verifier_domaines  # noqa: E402

CAMPAGNE_ANCIENNE = "Envois antérieurs des listes externes"
DATE_ANCIENNE = "2026-10-14T00:00:00Z"


def racine(domaine: str | None) -> str | None:
    if not domaine:
        return None
    domaine = domaine.lower().removeprefix("www.")
    return ".".join(domaine.split(".")[-2:])


def norm_commune(commune: str | None) -> str:
    texte = sans_accents(commune or "").upper()
    texte = re.sub(r"\b(ST|STE)\b", lambda m: "SAINT" if m.group(1) == "ST" else "SAINTE", texte)
    return re.sub(r"[^A-Z0-9]", "", texte)


def colle(texte: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", sans_accents(texte or "").lower())


def variantes(nom: str | None) -> set[str]:
    """Nom collé, avec et sans article en tête (« Le Bistrot des Voisines » → bistrotdesvoisines)."""
    mots_nom = re.split(r"[^a-z0-9]+", sans_accents(nom or "").lower())
    mots_nom = [m for m in mots_nom if m]
    sans_article = mots_nom[1:] if mots_nom and mots_nom[0] in {"le", "la", "les", "l", "au", "aux", "chez"} else mots_nom
    return {"".join(mots_nom), "".join(sans_article)} - {""}


def identifiant_provisoire(email: str) -> str:
    return "ext:" + hashlib.sha1(email.encode()).hexdigest()[:16]


def chercher(ligne: dict, etabs: list[dict], par_domaine: dict) -> tuple[str | None, str]:
    email = ligne["email"]
    dom = email.split("@")[1]
    if dom not in MESSAGERIES:
        candidats = par_domaine.get(racine(dom), [])
        if ligne["departement"]:
            candidats = [e for e in candidats if e["departement"] == ligne["departement"]] or candidats
        if len(candidats) == 1:
            return candidats[0]["siret"], "domaine"
        if candidats and ligne["commune"]:
            meme = [e for e in candidats if norm_commune(e["commune"]) == norm_commune(ligne["commune"])]
            if len(meme) == 1:
                return meme[0]["siret"], "domaine"
    if not ligne["departement"]:
        return None, "aucun"
    du_dep = [e for e in etabs if e["departement"] == ligne["departement"]]
    if ligne["nom"]:
        commune = norm_commune(ligne["commune"])
        meilleur, score_max = None, 0.0
        for e in du_dep:
            if commune and norm_commune(e["commune"]) != commune:
                continue
            s = max(ressemblance(ligne["nom"], e["nom"]), ressemblance(ligne["nom"], e["raison_sociale"]))
            if s > score_max:
                meilleur, score_max = e, s
        if meilleur and score_max >= (0.75 if commune else 0.9):
            return meilleur["siret"], "nom"
    local = colle(email.split("@")[0])
    if len(local) >= 6:
        trouves = {e["siret"] for e in du_dep
                   if any(len(n) >= 6 and n not in {"restaurant", "brasserie", "pizzeria"} and n in local
                          for n in variantes(e["nom"]))}
        if len(trouves) == 1:
            return trouves.pop(), "adresse"
    return None, "aucun"


async def principal():
    d1 = ouvrir_base()
    date = maintenant()
    departements = liste_departements(os.environ.get("DEPARTEMENTS"))
    filtre = ""
    params: list = []
    if departements:
        filtre = f" AND (departement IS NULL OR departement IN ({','.join('?' * len(departements))}))"
        params = departements
    lignes = d1.requete(
        "SELECT email, liste, departement, commune, nom, statut_envoi, importe_le, siret FROM listes_externes "
        f"WHERE (rattache_le IS NULL OR rattachement = 'aucun'){filtre}", params)
    print(f"{len(lignes)} adresses à rattacher")
    if not lignes:
        return

    deps = sorted({l["departement"] for l in lignes if l["departement"]})
    etabs = []
    for dep in deps:
        etabs += d1.requete("SELECT siret, nom, raison_sociale, commune, departement, site_web FROM etablissements "
                            "WHERE departement = ? AND actif = 1 AND siret NOT LIKE 'ext:%'", [dep])
    avec_site = d1.requete("SELECT siret, nom, commune, departement, site_web FROM etablissements "
                           "WHERE site_web IS NOT NULL AND actif = 1")
    par_domaine: dict[str, list[dict]] = {}
    for e in avec_site:
        r = racine(urlparse(e["site_web"]).hostname)
        if r:
            par_domaine.setdefault(r, []).append(e)
    print(f"{len(etabs)} établissements dans {len(deps)} départements, {len(par_domaine)} domaines de sites")

    resultats = []
    for l in lignes:
        siret, comment = chercher(l, etabs, par_domaine)
        resultats.append((l, siret, comment))

    # Fiches provisoires pour les adresses sans établissement.
    provisoires = []
    for l, siret, comment in resultats:
        if siret is None:
            provisoires.append((identifiant_provisoire(l["email"]), "", l["nom"], None, "?", "inconnu", None, None,
                                l["commune"], None, l["departement"] or "??", None, None, None, None, None, None,
                                None, 1, "liste"))
    if provisoires:
        d1.inserer("etablissements",
                   ["siret", "siren", "nom", "raison_sociale", "naf", "categorie", "adresse", "code_postal", "commune",
                    "code_commune", "departement", "lat", "lon", "tranche_effectif", "date_creation", "site_web",
                    "site_source", "osm_id", "actif", "maj_sirene"],
                   provisoires, "ON CONFLICT (siret) DO NOTHING")

    noms = {e["siret"]: e["nom"] for e in etabs + avec_site}
    valides = await verifier_domaines({l["email"].split("@")[1] for l, _, _ in resultats})
    emails, sources, envois, maj = [], [], [], []
    for l, siret, comment in resultats:
        cible = siret or identifiant_provisoire(l["email"])
        dom = l["email"].split("@")[1]
        emails.append((l["email"], dom, type_email(l["email"], l["nom"], noms.get(siret)), "valide" if valides.get(dom) else "invalide",
                       date, date))
        sources.append((l["email"], cible, "prompt", f"liste:{l['liste']}", l["importe_le"]))
        if l["statut_envoi"] == "envoye":
            envois.append((l["email"], cible))
        maj.append((l["email"], cible, comment))

    d1.inserer("emails", ["email", "domaine", "type", "statut", "verifie_le", "cree_le"], emails,
               """ON CONFLICT (email) DO UPDATE SET
                    statut = CASE WHEN emails.statut IN ('a_verifier', 'valide', 'invalide')
                                  THEN excluded.statut ELSE emails.statut END,
                    verifie_le = excluded.verifie_le""")
    # Une adresse qui passe d'une fiche provisoire à un établissement : l'ancienne source disparaît.
    for email, cible, comment in maj:
        if comment != "aucun":
            d1.requete("DELETE FROM email_sources WHERE email = ? AND source = 'prompt' AND siret LIKE 'ext:%' AND siret <> ?",
                       [email, cible])
    d1.inserer("email_sources", ["email", "siret", "source", "url", "consulte_le"], sources,
               "ON CONFLICT (email, siret, source) DO NOTHING")
    if envois:
        existante = d1.requete("SELECT id FROM campagnes WHERE nom = ?", [CAMPAGNE_ANCIENNE])
        if existante:
            campagne = existante[0]["id"]
        else:
            d1.requete("INSERT INTO campagnes (nom, modele, statut, quota_jour, cree_le, cree_par) "
                       "VALUES (?, 'listes-externes', 'terminee', 0, ?, 'import')", [CAMPAGNE_ANCIENNE, date])
            campagne = d1.requete("SELECT id FROM campagnes WHERE nom = ?", [CAMPAGNE_ANCIENNE])[0]["id"]
        d1.inserer("envois", ["campagne_id", "email", "siret", "etape", "envoye_le", "statut"],
                   [(campagne, email, cible, 1, DATE_ANCIENNE, "envoye") for email, cible in envois],
                   "ON CONFLICT (campagne_id, email, etape) DO UPDATE SET siret = excluded.siret")
    for comment in ("domaine", "nom", "adresse", "aucun"):
        lot = [(e, c) for e, c, k in maj if k == comment]
        for i in range(0, len(lot), 30):  # D1 : 100 paramètres au plus par requête
            part = lot[i:i + 30]
            cas = " ".join("WHEN ? THEN ?" for _ in part)
            valeurs = [x for e, c in part for x in (e, c)]
            d1.requete(f"UPDATE listes_externes SET siret = CASE email {cas} END, rattachement = ?, rattache_le = ? "
                       f"WHERE email IN ({','.join('?' * len(part))})",
                       valeurs + [comment, date] + [e for e, _ in part])
    # Fiches provisoires devenues orphelines.
    d1.requete("DELETE FROM etablissements WHERE siret LIKE 'ext:%' "
               "AND siret NOT IN (SELECT siret FROM email_sources) AND siret NOT IN (SELECT siret FROM envois WHERE siret IS NOT NULL)")

    resume = {
        "adresses": len(resultats),
        "rattachement": {k: sum(1 for *_, c in resultats if c == k) for k in ("domaine", "nom", "adresse", "aucun")},
        "deja_envoyees": len(envois),
        "domaines_invalides": sum(1 for v in valides.values() if not v),
        "lignes_ecrites": d1.lignes_ecrites,
    }
    print(json.dumps(resume, ensure_ascii=False, indent=1))
    if os.environ.get("RESUME_LISTE"):
        with open(os.environ["RESUME_LISTE"], "w") as f:
            json.dump(resume, f, ensure_ascii=False, indent=1)
    d1.journal("rattacher_liste", resume)


if __name__ == "__main__":
    asyncio.run(principal())
