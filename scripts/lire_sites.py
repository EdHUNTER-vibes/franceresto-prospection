"""Étape 2 : lecture des sites des établissements et vérification des adresses.

Pour chaque établissement actif qui a un site (pas lu depuis 90 jours) :
- `robots.txt` respecté, agent identifié, une page à la fois par site, 1 s entre deux pages ;
- page d'accueil puis au plus 4 pages du même site dont le lien évoque contact, mentions
  légales, réservation, privatisation, recrutement ou « à propos » ;
- adresses lues dans les liens mailto:, le texte, les données structurées (schema.org) et la
  protection d'e-mail de Cloudflare ; formes « contact [at] domaine.fr » reconnues ;
- seules sont gardées les adresses du domaine du site ou d'une messagerie grand public
  (gmail, orange…) : les adresses de l'agence web ou de l'hébergeur (mentions légales) sont écartées.
Puis chaque nouveau domaine est vérifié (serveur de messagerie MX, sinon adresse A) : « valide »
ou « invalide ». Aucun e-mail n'est envoyé pour vérifier une adresse.

Variables : DEPARTEMENTS, LIMITE (nombre de sites par passage, 400 par défaut), DUREE_MAX (secondes,
2 700 par défaut : au-delà, plus aucun nouveau lot n'est commencé), et celles de la base.
"""

import asyncio
import html
import json
import os
import re
import sys
import time
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import dns.asyncresolver
import dns.exception
import httpx

sys.path.insert(0, os.path.dirname(__file__))
from commun import MESSAGERIES, liste_departements, maintenant, normaliser_email, ouvrir_base, type_email  # noqa: E402

AGENT = "FranceRestoBot/1.0 (+https://franceresto.net/contact ; lecture des pages contact)"
PAGES_UTILES = re.compile(
    r"contact|mention|legal|l[eé]gales|reserv|r[eé]sa|privati|recrut|emploi|job|a-propos|apropos|"
    r"about|qui-sommes|infos?-pratiques|acces|nous-trouver|equipe|traiteur|evenement", re.I)
_SEP = r"(?:\.|\s*(?:\[dot\]|\(dot\)|\[point\]|\(point\))\s*)"
RE_TEXTE = re.compile(
    r"[a-z0-9._%+'-]+(?:@|\s*(?:\[at\]|\(at\)|\{at\})\s*|\s+arobase\s+)[a-z0-9-]+(?:" + _SEP + r"[a-z0-9-]+)+", re.I)
RE_LIEN = re.compile(r"""<a\s[^>]*href\s*=\s*["']([^"'#]+)["']""", re.I)
RE_MAILTO = re.compile(r"""mailto:([^"'?>\s]+)""", re.I)
RE_CFEMAIL = re.compile(r"""data-cfemail\s*=\s*["']([0-9a-f]+)["']""", re.I)
RE_JSONLD = re.compile(r"""<script[^>]+application/ld\+json[^>]*>(.*?)</script>""", re.I | re.S)
TAILLE_MAX = 1_500_000
DELAI_PAGE = 1.0


def domaine_de(url: str) -> str:
    hote = urlparse(url).hostname or ""
    return hote.lower().removeprefix("www.")


def meme_site(domaine_email: str, domaine_site: str) -> bool:
    racine = ".".join(domaine_site.split(".")[-2:])
    return domaine_email == domaine_site or domaine_email.endswith("." + racine) or domaine_email == racine


def decoder_cfemail(code: str) -> str:
    cle = int(code[:2], 16)
    return "".join(chr(int(code[i:i + 2], 16) ^ cle) for i in range(2, len(code), 2))


def emails_jsonld(objet) -> list[str]:
    trouves = []
    if isinstance(objet, dict):
        for cle, valeur in objet.items():
            if cle.lower() == "email" and isinstance(valeur, str):
                trouves.append(valeur)
            else:
                trouves += emails_jsonld(valeur)
    elif isinstance(objet, list):
        for x in objet:
            trouves += emails_jsonld(x)
    return trouves


def extraire(page: str) -> set[str]:
    brutes = set(RE_MAILTO.findall(page))
    brutes |= {decoder_cfemail(c) for c in RE_CFEMAIL.findall(page) if len(c) % 2 == 0}
    for bloc in RE_JSONLD.findall(page):
        try:
            brutes |= set(emails_jsonld(json.loads(bloc)))
        except ValueError:
            pass
    texte = html.unescape(re.sub(r"<(script|style)[^>]*>.*?</\1>|<[^>]+>", " ", page, flags=re.I | re.S))
    for m in RE_TEXTE.findall(texte):
        m = re.sub(r"\s*(\[at\]|\(at\)|\{at\}|arobase)\s*", "@", m, flags=re.I)
        m = re.sub(r"\s*(\[dot\]|\(dot\)|\[point\]|\(point\))\s*", ".", m, flags=re.I)
        brutes.add(re.sub(r"\s+", "", m))
    return {e for e in (normaliser_email(b) for b in brutes) if e}


async def obtenir(client: httpx.AsyncClient, url: str, taille_max: int,
                  html_seulement: bool = True) -> tuple[str | None, str]:
    """Page lue en flux et abandonnée au-delà de taille_max : télécharger la réponse entière avant de la
    mesurer laissait un flux sans fin (radio, vidéo) remplir la mémoire et faire couper le runner."""
    try:
        async with client.stream("GET", url) as r:
            type_contenu = r.headers.get("content-type", "")
            if r.status_code != 200 or (html_seulement and "html" not in type_contenu):
                return None, str(r.url)
            morceaux, taille = [], 0
            async for morceau in r.aiter_bytes():
                taille += len(morceau)
                if taille > taille_max:
                    return None, str(r.url)
                morceaux.append(morceau)
            return b"".join(morceaux).decode(r.encoding or "utf-8", errors="replace"), str(r.url)
    except (httpx.HTTPError, ValueError, LookupError):
        return None, url


async def lire(client: httpx.AsyncClient, url: str) -> tuple[str | None, str]:
    return await obtenir(client, url, TAILLE_MAX)


async def robots(client: httpx.AsyncClient, url: str) -> RobotFileParser:
    rp = RobotFileParser()
    try:
        texte, _ = await obtenir(client, urljoin(url, "/robots.txt"), 200_000, html_seulement=False)
    except ValueError:
        texte = None
    rp.parse(texte.splitlines() if texte else [])
    return rp


async def lire_site(client: httpx.AsyncClient, etab: dict) -> dict:
    site = etab["site_web"]
    resultat = {"siret": etab["siret"], "emails": {}, "statut": "sans_email",
                "noms": (etab.get("nom"), etab.get("raison_sociale"))}
    rp = await robots(client, site)
    if not rp.can_fetch(AGENT, site):
        resultat["statut"] = "interdit_robots"
        return resultat
    page, url_finale = await lire(client, site)
    if page is None:
        resultat["statut"] = "injoignable"
        return resultat
    domaine = domaine_de(url_finale)
    a_lire, vus = [], {url_finale}
    for lien in RE_LIEN.findall(page):
        try:
            cible = urljoin(url_finale, html.unescape(lien)).split("#")[0]
        except ValueError:  # lien mal formé (ex. « //trustindex%20… »)
            continue
        if cible in vus or domaine_de(cible) != domaine or not cible.startswith("http"):
            continue
        if PAGES_UTILES.search(urlparse(cible).path + " " + lien):
            vus.add(cible)
            a_lire.append(cible)
    pages = [(url_finale, page)]
    for cible in a_lire[:4]:
        if not rp.can_fetch(AGENT, cible):
            continue
        await asyncio.sleep(DELAI_PAGE)
        contenu, url = await lire(client, cible)
        if contenu:
            pages.append((url, contenu))
    for url, contenu in pages:
        for email in extraire(contenu):
            dom = email.split("@")[1]
            if meme_site(dom, domaine) or dom in MESSAGERIES:
                resultat["emails"].setdefault(email, url)
    if resultat["emails"]:
        resultat["statut"] = "ok"
    return resultat


async def verifier_domaines(domaines: set[str]) -> dict[str, bool]:
    resolveur = dns.asyncresolver.Resolver()
    resolveur.lifetime = 8

    async def un(d):
        for type_rr in ("MX", "A"):
            try:
                await resolveur.resolve(d, type_rr)
                return d, True
            except (dns.resolver.NXDOMAIN, dns.resolver.NoNameservers):
                return d, False
            except (dns.resolver.NoAnswer, dns.exception.Timeout):
                continue
        return d, False

    resultats = await asyncio.gather(*(un(d) for d in domaines))
    return dict(resultats)


async def principal():
    departements = liste_departements(os.environ["DEPARTEMENTS"])
    limite = int(os.environ.get("LIMITE", "400"))
    d1 = ouvrir_base()
    marques = ",".join("?" * len(departements))
    etabs = d1.requete(
        f"""SELECT siret, site_web, nom, raison_sociale FROM etablissements
            WHERE actif = 1 AND site_web IS NOT NULL AND departement IN ({marques})
              AND (site_lu_le IS NULL OR site_lu_le < datetime('now', '-90 days'))
            ORDER BY site_lu_le IS NOT NULL, site_lu_le LIMIT ?""", departements + [limite])
    print(f"{len(etabs)} sites à lire")
    debut = time.time()
    # Pas de plafond de connexions : avec un plafond, les connexions gardées ouvertes vers les sites déjà lus
    # occupaient la réserve et les sites suivants échouaient en « PoolTimeout » (ici, la moitié d'entre eux).
    limites = httpx.Limits(max_connections=None, max_keepalive_connections=8)
    async with httpx.AsyncClient(headers={"User-Agent": AGENT, "Accept-Language": "fr"}, timeout=httpx.Timeout(15, pool=None),
                                 follow_redirects=True, limits=limites, max_redirects=5) as client:
        semaphore = asyncio.Semaphore(12)

        async def borne(e):
            async with semaphore:
                try:
                    return await asyncio.wait_for(lire_site(client, e), 90)
                except Exception as erreur:  # un site illisible ne doit jamais arrêter tout le passage
                    if not isinstance(erreur, asyncio.TimeoutError):
                        print(f"{e['site_web']} : {type(erreur).__name__} {str(erreur)[:120]}")
                    return {"siret": e["siret"], "emails": {}, "statut": "injoignable"}

        # Par lots, avec une durée maximale : le passage s'arrête proprement et enregistre ce qu'il a lu
        # (le fichier de travail n'est renvoyé sur R2 qu'à la fin du workflow).
        resultats, duree_max = [], int(os.environ.get("DUREE_MAX", "2700"))
        for i in range(0, len(etabs), 240):
            if time.time() - debut > duree_max:
                print(f"Durée maximale atteinte : {len(resultats)} sites lus sur {len(etabs)}, la suite au prochain passage")
                break
            resultats += await asyncio.gather(*(borne(e) for e in etabs[i:i + 240]))

    date = maintenant()
    nouveaux = {e: r["noms"] for r in resultats for e in r["emails"]}
    valides = await verifier_domaines({e.split("@")[1] for e in nouveaux})
    emails = [(e, e.split("@")[1], type_email(e, *noms), "valide" if valides.get(e.split("@")[1]) else "invalide",
               date, date) for e, noms in nouveaux.items()]
    sources = [(e, r["siret"], "site", u, date) for r in resultats for e, u in r["emails"].items()]
    if emails:
        d1.inserer("emails", ["email", "domaine", "type", "statut", "verifie_le", "cree_le"], emails,
                   """ON CONFLICT (email) DO UPDATE SET
                        statut = CASE WHEN emails.statut IN ('a_verifier', 'valide', 'invalide')
                                      THEN excluded.statut ELSE emails.statut END,
                        type = excluded.type, verifie_le = excluded.verifie_le""")
        d1.inserer("email_sources", ["email", "siret", "source", "url", "consulte_le"], sources,
                   "ON CONFLICT (email, siret, source) DO UPDATE SET url = excluded.url, consulte_le = excluded.consulte_le")
    for statut in ("ok", "sans_email", "injoignable", "interdit_robots"):
        sirets = [r["siret"] for r in resultats if r["statut"] == statut]
        for i in range(0, len(sirets), 90):
            lot = sirets[i:i + 90]
            d1.requete(f"UPDATE etablissements SET site_lu_le = ?, site_statut = ? WHERE siret IN ({','.join('?' * len(lot))})",
                       [date, statut] + lot)
    # Adresses OSM encore à vérifier : même contrôle du domaine.
    restants = d1.requete("SELECT email FROM emails WHERE statut = 'a_verifier' LIMIT 2000")
    if restants:
        ok = await verifier_domaines({r["email"].split("@")[1] for r in restants})
        for statut in ("valide", "invalide"):
            lot = [r["email"] for r in restants if ok.get(r["email"].split("@")[1]) == (statut == "valide")]
            for i in range(0, len(lot), 90):
                part = lot[i:i + 90]
                d1.requete(f"UPDATE emails SET statut = ?, verifie_le = ? WHERE email IN ({','.join('?' * len(part))})",
                           [statut, date] + part)
    resume = {
        "departements": departements if len(departements) < 10 else f"{len(departements)} départements",
        "sites_lus": len(resultats), "restants": len(etabs) - len(resultats),
        "par_statut": {s: sum(1 for r in resultats if r["statut"] == s)
                       for s in ("ok", "sans_email", "injoignable", "interdit_robots")},
        "emails_trouves": len(nouveaux),
        "emails_generiques": sum(1 for e in emails if e[2] == "generique"),
        "domaines_invalides": sum(1 for v in valides.values() if not v),
        "osm_verifies": len(restants), "duree_s": round(time.time() - debut),
    }
    print(json.dumps(resume, ensure_ascii=False, indent=1))
    if os.environ.get("RESUME"):
        with open(os.environ["RESUME"], "w") as f:
            json.dump(resume, f, ensure_ascii=False, indent=1)
    d1.journal("lire_sites", resume)


if __name__ == "__main__":
    asyncio.run(principal())
