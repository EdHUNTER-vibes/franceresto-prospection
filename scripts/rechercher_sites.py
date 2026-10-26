"""Recherche du site des établissements qui n'en ont pas (aucune source ne l'a donné).

Pour chaque établissement actif sans site, pas encore cherché depuis 180 jours :
1. domaines candidats tirés du nom (enseigne) et de la commune, ex. « Le Petit Crocus » à Liffré :
   lepetitcrocus.fr, petitcrocus.fr, le-petit-crocus.com, restaurant-lepetitcrocus.fr,
   petitcrocus-restaurant.fr, petitcrocus-liffre.fr… ; pour « Hôtel Printania » aussi printaniahotel.com
   (.fr et .com, .bzh en Bretagne) ;
2. seuls les domaines qui existent (DNS) sont ouverts : page d'accueil et au plus 2 pages
   « mentions légales / contact », robots.txt respecté, agent identifié ;
3. le site n'est retenu que si ses pages **prouvent** qu'il s'agit de cet établissement :
   son numéro SIREN, ou son code postal, sa commune et un mot distinctif de sa rue. Un domaine qui
   prouverait plusieurs établissements différents (chaîne, annuaire) est écarté.
Le site retenu est enregistré avec `site_source = 'recherche'` ; la lecture des sites (lire_sites.py)
y cherche ensuite les adresses e-mail. Aucune recherche sur Google ni sur un annuaire.

Variables : DEPARTEMENTS (liste, ou « metropole »), LIMITE (établissements par passage, 3 000 par défaut),
DUREE_MAX (secondes, 2 700 par défaut), BASE_FICHIER, RESUME.
"""

import asyncio
import json
import os
import re
import sys
import time
from urllib.parse import urljoin, urlparse

import dns.asyncresolver
import dns.exception
import dns.resolver
import httpx

sys.path.insert(0, os.path.dirname(__file__))
from commun import liste_departements, maintenant, ouvrir_base, sans_accents  # noqa: E402
from lire_sites import AGENT, RE_LIEN, lire, robots  # noqa: E402

ARTICLES = {"le", "la", "les", "l", "au", "aux", "chez", "a"}
FORMES_JURIDIQUES = {"sarl", "sas", "sasu", "eurl", "snc", "sci", "ste", "societe", "ets", "sa"}
# Noms trop vagues pour deviner un domaine (le domaine existe, mais appartient à n'importe qui).
NOMS_VAGUES = {"restaurant", "hotel", "bar", "cafe", "brasserie", "pizzeria", "creperie", "snack",
               "traiteur", "boulangerie", "kebab", "tacos", "burger", "sushi", "pizza", "bistrot", "bistro",
               "auberge", "relais", "grill", "wok", "buffet", "cantine", "resto", "food", "lounge"}
PREFIXES = {"restaurant": ["restaurant", "resto"], "restauration_rapide": ["restaurant"],
            "cafeteria": ["restaurant"], "bar": ["bar"], "hotel": ["hotel"], "traiteur": ["traiteur"]}
BRETAGNE = {"22", "29", "35", "56", "44"}
MOTS_RUE = {"rue", "avenue", "av", "boulevard", "bd", "place", "pl", "chemin", "route", "rte", "allee", "impasse",
            "quai", "cours", "square", "passage", "faubourg", "fbg", "lieu", "dit", "zone", "za", "zi", "zac",
            "parc", "centre", "commercial", "rond", "point", "de", "du", "des", "la", "le", "les", "l", "d",
            "saint", "sainte", "st", "ste", "bis", "ter", "b", "grande", "grand", "petite", "petit"}
PAGES_PREUVE = re.compile(r"mention|legal|l[eé]gales|contact|cgv|conditions|a-propos|apropos|about|infos?", re.I)
CHAINES = 2  # au-delà, le domaine sert plusieurs établissements : écarté


def mots_nom(nom: str | None) -> list[str]:
    mots = [m for m in re.split(r"[^a-z0-9]+", sans_accents(nom or "").lower()) if m]
    return [m for m in mots if m not in FORMES_JURIDIQUES]


def domaines_candidats(etab: dict) -> list[str]:
    mots = mots_nom(etab["nom"])
    sans_article = mots[1:] if len(mots) > 1 and mots[0] in ARTICLES else mots
    if not sans_article or "".join(sans_article) in NOMS_VAGUES or len("".join(sans_article)) < 4:
        return []
    if all(m in NOMS_VAGUES | ARTICLES for m in sans_article):
        return []
    # Cœur du nom sans le mot de catégorie en tête (« Hôtel Printania » → printania).
    coeurs = [sans_article]
    if len(sans_article) > 1 and sans_article[0] in NOMS_VAGUES:
        reste = sans_article[1:]
        reste = reste[1:] if len(reste) > 1 and reste[0] in ARTICLES else reste
        if len("".join(reste)) >= 4:
            coeurs.append(reste)
    commune = "".join(re.split(r"[^a-z0-9]+", sans_accents(etab["commune"] or "").lower()))
    commune = commune if commune and len(commune) <= 20 else ""
    mots_categorie = PREFIXES.get(etab["categorie"], [])
    noms = {"".join(mots), "-".join(mots)}
    if commune:
        noms.add("".join(mots) + commune)
    for coeur in coeurs:
        for f in ("".join(coeur), "-".join(coeur)):
            noms.add(f)
            if commune:
                noms |= {f"{f}-{commune}", f + commune}
            for mot in mots_categorie:
                if coeur[0] != mot:
                    noms |= {f"{mot}-{f}", f"{mot}{f}", f"{f}-{mot}", f"{f}{mot}"}
                    if commune:
                        noms.add(f"{mot}-{f}-{commune}")
    extensions = ["fr", "com"] + (["bzh"] if etab["departement"] in BRETAGNE else [])
    return sorted({f"{n}.{x}" for n in noms if 3 <= len(n) <= 63 and not n.startswith("-") for x in extensions})


def chiffres_colles(texte: str) -> str:
    return re.sub(r"(?<=\d)[\s.  -]+(?=\d)", "", texte)


def texte_de(page: str) -> str:
    texte = re.sub(r"<(script|style)[^>]*>.*?</\1>|<[^>]+>", " ", page, flags=re.I | re.S)
    return sans_accents(texte).lower()


def mots_rue(adresse: str | None) -> set[str]:
    mots = re.split(r"[^a-z0-9]+", sans_accents(adresse or "").lower())
    return {m for m in mots if len(m) >= 4 and not m.isdigit() and m not in MOTS_RUE}


def preuve(etab: dict, textes: list[str]) -> str | None:
    tout = " ".join(textes)
    if etab["siren"] and not etab["siret"].startswith("ext:") and etab["siren"] in chiffres_colles(tout):
        return "siren"
    commune = sans_accents(etab["commune"] or "").lower().replace("-", " ")
    rue = mots_rue(etab["adresse"])
    if (etab["code_postal"] and etab["code_postal"] in chiffres_colles(tout) and commune
            and commune in tout.replace("-", " ") and rue and any(re.search(rf"\b{m}\b", tout) for m in rue)):
        return "adresse"
    return None


async def existants(domaines: set[str]) -> set[str]:
    resolveur = dns.asyncresolver.Resolver()
    resolveur.lifetime = 10
    semaphore = asyncio.Semaphore(int(os.environ.get("CONCURRENCE_DNS", "50")))

    async def un(d):
        async with semaphore:
            for essai in range(3):  # une expiration n'est pas une absence : on réessaie
                try:
                    await resolveur.resolve(d, "A")
                    return d
                except dns.resolver.NXDOMAIN:
                    return None
                except dns.resolver.NoAnswer:
                    try:
                        await resolveur.resolve(d, "AAAA")
                        return d
                    except dns.exception.DNSException:
                        return None
                except (dns.resolver.NoNameservers, dns.exception.Timeout):
                    await asyncio.sleep(1 + essai)
            return None

    return {d for d in await asyncio.gather(*(un(d) for d in domaines)) if d}


async def pages_du_domaine(client: httpx.AsyncClient, domaine: str) -> tuple[str | None, list[str]]:
    """URL finale de la page d'accueil et textes de l'accueil et d'au plus 2 pages utiles."""
    url = f"https://{domaine}/"
    rp = await robots(client, url)
    if not rp.can_fetch(AGENT, url):
        return None, []
    page, url_finale = None, url
    for essai in (url, f"http://{domaine}/", url):  # https, http, puis https une seconde fois
        page, url_finale = await lire(client, essai)
        if page is not None:
            break
    if page is None:
        return None, []
    textes = [texte_de(page)]
    hote = urlparse(url_finale).hostname or ""
    vus = {url_finale}
    for lien in RE_LIEN.findall(page):
        try:
            cible = urljoin(url_finale, lien).split("#")[0]
        except ValueError:
            continue
        if cible in vus or urlparse(cible).hostname != hote or not PAGES_PREUVE.search(urlparse(cible).path):
            continue
        vus.add(cible)
        if len(vus) > 3:
            break
        if rp.can_fetch(AGENT, cible):
            await asyncio.sleep(1)
            contenu, _ = await lire(client, cible)
            if contenu:
                textes.append(texte_de(contenu))
    return url_finale, textes


async def chercher_lot(base, client, etabs: list[dict], stats: dict):
    """Un lot d'établissements : domaines candidats, DNS, pages, preuves ; résultat écrit aussitôt."""
    candidats = {e["siret"]: domaines_candidats(e) for e in etabs}
    tous = {d for ds in candidats.values() for d in ds}
    trouves = await existants(tous)
    textes: dict[str, tuple[str | None, list[str]]] = {}
    semaphore = asyncio.Semaphore(int(os.environ.get("CONCURRENCE", "16")))

    async def ouvrir(d):
        async with semaphore:
            try:
                textes[d] = await asyncio.wait_for(pages_du_domaine(client, d), 90)
            except Exception as erreur:  # domaine illisible : simplement non retenu
                stats["erreurs"][type(erreur).__name__] = stats["erreurs"].get(type(erreur).__name__, 0) + 1
                textes[d] = (None, [])

    await asyncio.gather(*(ouvrir(d) for d in trouves))

    proposes: dict[str, list[tuple[str, str]]] = {}  # site → [(siret, preuve)]
    for e in etabs:
        for d in candidats[e["siret"]]:
            url, pages = textes.get(d, (None, []))
            p = preuve(e, pages) if url else None
            if p:
                site = f"{urlparse(url).scheme}://{urlparse(url).hostname}/"
                proposes.setdefault(site, []).append((e["siret"], p))
                break
    retenus = {}
    for site, liste in proposes.items():
        # Les lots précédents comptent aussi : un domaine qui prouve plusieurs établissements est écarté.
        deja = base.requete("SELECT count(*) AS n FROM recherche_sites WHERE site = ?", [site])[0]["n"]
        if len({s for s, _ in liste}) + deja <= CHAINES:
            for siret, p in liste:
                retenus[siret] = (site, p)
        else:
            stats["ecartes_chaines"] += len(liste)
    date = maintenant()
    base.inserer("recherche_sites", ["siret", "essaye_le", "domaines", "site", "preuve"],
                 [(e["siret"], date, len(candidats[e["siret"]]), *retenus.get(e["siret"], (None, None))) for e in etabs],
                 "ON CONFLICT (siret) DO UPDATE SET essaye_le = excluded.essaye_le, domaines = excluded.domaines, "
                 "site = excluded.site, preuve = excluded.preuve")
    for siret, (site, _) in retenus.items():
        base.requete("UPDATE etablissements SET site_web = ?, site_source = 'recherche', site_lu_le = NULL "
                     "WHERE siret = ? AND site_web IS NULL", [site, siret])
    stats["etablissements"] += len(etabs)
    stats["sans_candidat"] += sum(1 for v in candidats.values() if not v)
    stats["domaines_candidats"] += len(tous)
    stats["domaines_existants"] += len(trouves)
    stats["domaines_ouverts"] += sum(1 for u, _ in textes.values() if u)
    stats["sites_trouves"] += len(retenus)
    for _, p in retenus.values():
        stats["par_preuve"][p] += 1


async def principal():
    departements = liste_departements(os.environ["DEPARTEMENTS"])
    limite = int(os.environ.get("LIMITE", "3000"))
    duree_max = int(os.environ.get("DUREE_MAX", "2700"))
    base = ouvrir_base()
    marques = ",".join("?" * len(departements))
    # Restauration rapide en dernier : noms plus souvent vagues, sites plus rares.
    etabs = base.requete(
        f"""SELECT e.siret, e.siren, e.nom, e.categorie, e.adresse, e.code_postal, e.commune, e.departement
            FROM etablissements e LEFT JOIN recherche_sites r ON r.siret = e.siret
            WHERE e.actif = 1 AND e.site_web IS NULL AND e.departement IN ({marques}) AND e.siret NOT LIKE 'ext:%'
              AND (r.siret IS NULL OR r.essaye_le < datetime('now', '-180 days'))
            ORDER BY e.categorie = 'restauration_rapide', e.departement, e.siret LIMIT ?""", departements + [limite])
    print(f"{len(etabs)} établissements sans site à chercher")
    debut = time.time()
    stats = {"etablissements": 0, "sans_candidat": 0, "domaines_candidats": 0, "domaines_existants": 0,
             "domaines_ouverts": 0, "erreurs": {}, "sites_trouves": 0, "par_preuve": {"siren": 0, "adresse": 0},
             "ecartes_chaines": 0}
    # Pas de plafond de connexions : avec un plafond, les connexions gardées ouvertes vers les sites déjà lus
    # occupaient la réserve et les sites suivants échouaient en « PoolTimeout » (ici, la moitié d'entre eux).
    limites = httpx.Limits(max_connections=None, max_keepalive_connections=8)
    async with httpx.AsyncClient(headers={"User-Agent": AGENT, "Accept-Language": "fr"}, timeout=httpx.Timeout(12, pool=None),
                                 follow_redirects=True, limits=limites, max_redirects=5) as client:
        # Par lots écrits au fur et à mesure, avec une durée maximale (voir lire_sites.py).
        for i in range(0, len(etabs), 1000):
            if time.time() - debut > duree_max:
                print(f"Durée maximale atteinte : {stats['etablissements']} établissements cherchés sur {len(etabs)}")
                break
            await chercher_lot(base, client, etabs[i:i + 1000], stats)
            print(f"{stats['etablissements']} établissements cherchés, {stats['sites_trouves']} sites trouvés "
                  f"({round(time.time() - debut)} s)")
    resume = {"departements": departements if len(departements) < 10 else f"{len(departements)} départements",
              **stats, "restants": len(etabs) - stats["etablissements"], "duree_s": round(time.time() - debut)}
    print(json.dumps(resume, ensure_ascii=False, indent=1))
    if os.environ.get("RESUME"):
        with open(os.environ["RESUME"], "w") as f:
            json.dump(resume, f, ensure_ascii=False, indent=1)
    base.journal("rechercher_sites", resume)


if __name__ == "__main__":
    asyncio.run(principal())
