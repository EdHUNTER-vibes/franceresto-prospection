"""Outils communs aux scripts de prospection : accès à D1 (API Cloudflare), e-mails."""

import difflib
import json
import os
import re
import sqlite3
import time
import unicodedata

import requests

D1_URL = "https://api.cloudflare.com/client/v4/accounts/{compte}/d1/database/{base}/query"
TAILLE_MAX_SQL = 90_000  # D1 refuse les requêtes de plus de 100 Ko

# Partie locale des adresses de contact d'un établissement (et non d'une personne).
LOCAUX_GENERIQUES = {
    "contact", "contacts", "info", "infos", "information", "informations", "accueil", "bonjour",
    "hello", "hi", "reservation", "reservations", "resa", "booking", "book", "reception",
    "restaurant", "resto", "hotel", "bar", "brasserie", "cafe", "creperie", "pizzeria", "traiteur",
    "commande", "commandes", "order", "orders", "evenement", "evenements", "events", "event",
    "privatisation", "groupe", "groupes", "seminaire", "direction", "gerant", "gerance", "manager",
    "recrutement", "jobs", "job", "emploi", "rh", "team", "equipe", "office", "bureau", "mail",
    "email", "sales", "commercial", "boutique", "shop", "cuisine", "salle", "admin", "administration",
    "compta", "comptabilite", "facturation",
}
# Adresses à ne jamais retenir : techniques, agences web, plateformes.
LOCAUX_EXCLUS = {
    "noreply", "no-reply", "donotreply", "ne-pas-repondre", "nepasrepondre", "webmaster", "postmaster",
    "hostmaster", "abuse", "root", "mailer-daemon", "privacy", "dpo", "rgpd", "gdpr", "example",
    "exemple", "test", "wordpress", "support", "newsletter",
}
DOMAINES_EXCLUS = {
    "example.com", "example.fr", "domain.com", "domaine.com", "sentry.io", "wixpress.com",
    "sentry.wixpress.com", "godaddy.com", "ovh.net", "ovh.com", "o2switch.fr", "ionos.fr",
    "tripadvisor.com", "tripadvisor.fr", "thefork.com", "lafourchette.com", "ubereats.com",
    "deliveroo.fr", "facebook.com", "instagram.com", "google.com", "zenchef.com", "guestonline.fr",
    "pagesjaunes.fr", "solocal.com", "booking.com", "expedia.com", "sumup.com", "eatbu.com",
    "dish.co", "metro.fr", "wix.com", "jimdo.com", "squarespace.com", "shopify.com",
}
# Réseaux et chaînes : leurs adresses sont celles du siège ou d'une centrale de réservation, pas de
# l'établissement (ex. assistance.reservation@accor.com pour un hôtel ibis franchisé). Un domaine
# rattaché à au moins SEUIL_CHAINE établissements est traité de même (marquer_chaines).
DOMAINES_CHAINES = {
    "accor.com", "accorhotels.com", "ibis.com", "novotel.com", "mercure.com", "sofitel.com", "pullman.com",
    "bestwestern.fr", "bestwestern.com", "marriott.com", "hilton.com", "ihg.com", "hyatt.com",
    "radissonhotels.com", "choicehotels.com", "wyndham.com", "louvrehotels.com", "campanile.com",
    "kyriad.com", "premiereclasse.com", "hotel-bb.com", "hotelf1.com", "logishotels.com", "logis-hotels.com",
    "brithotel.fr", "contact-hotel.com", "the-originals-hotels.com", "appartcity.com", "adagio-city.com",
    "mcdonalds.fr", "mcdonalds.com", "burgerking.fr", "kfc.fr", "quick.fr", "subway.com", "dominos.fr",
    "pizzahut.fr", "buffalo-grill.fr", "courtepaille.com", "hippopotamus.fr", "flunch.fr", "del-arte.fr",
    "leon-de-bruxelles.fr", "bistro-regent.fr", "paul.fr", "briochedoree.fr", "brioche-doree.fr",
    "starbucks.fr", "otacos.com", "o-tacos.com", "pomdepain.fr", "columbuscafe.com", "cojean.fr",
    "sodexo.com", "elior.com", "elior.fr", "compass-group.fr", "api-restauration.fr", "groupe-bertrand.com",
}
SEUIL_CHAINE = 5


def marquer_chaines(base: "D1") -> int:
    """Adresses (hors messageries) d'une chaîne ou d'un réseau → « invalide » : jamais ciblées."""
    liste = ", ".join(sql_texte(d) for d in sorted(DOMAINES_CHAINES))
    messageries = ", ".join(sql_texte(d) for d in sorted(MESSAGERIES))
    avant = base.lignes_ecrites
    base.requete(f"""
        UPDATE emails SET statut = 'invalide'
        WHERE statut IN ('a_verifier', 'valide') AND (
          domaine IN ({liste})
          OR domaine IN (SELECT m.domaine FROM emails m JOIN email_sources s ON s.email = m.email
                         WHERE m.domaine NOT IN ({messageries})
                         GROUP BY m.domaine HAVING count(DISTINCT s.siret) >= {SEUIL_CHAINE}))""")
    return base.lignes_ecrites - avant


# Sites qui ne sont pas « le site de l'établissement » (on ne les lit pas).
SITES_PLATEFORMES = (
    "facebook.com", "fb.com", "instagram.com", "tripadvisor.", "thefork.", "lafourchette.",
    "ubereats.", "deliveroo.", "just-eat.", "justeat.", "pagesjaunes.", "google.", "goo.gl",
    "booking.com", "expedia.", "airbnb.", "linktr.ee", "twitter.com", "x.com", "tiktok.com",
    "youtube.com", "petitfute.", "michelin.", "gaultmillau.", "restaurantguru.", "yelp.",
    "zenchef.", "guestonline.", "linkedin.com", "wa.me", "whatsapp.com",
)

# Messageries grand public : une adresse chez elles peut être celle de l'établissement.
MESSAGERIES = {
    "gmail.com", "googlemail.com", "orange.fr", "wanadoo.fr", "free.fr", "sfr.fr", "neuf.fr",
    "laposte.net", "hotmail.com", "hotmail.fr", "outlook.com", "outlook.fr", "live.fr", "live.com",
    "yahoo.fr", "yahoo.com", "icloud.com", "me.com", "aol.com", "bbox.fr", "numericable.fr",
    "club-internet.fr", "aliceadsl.fr", "proton.me", "protonmail.com", "gmx.fr", "msn.com",
}

MOTS_VIDES = {
    "le", "la", "les", "l", "de", "du", "des", "d", "et", "au", "aux", "a", "en", "chez", "the",
    "restaurant", "resto", "bar", "cafe", "hotel", "brasserie", "sarl", "sas", "sasu", "eurl", "snc",
    "ste", "societe", "ets", "creperie", "pizzeria", "traiteur",
}


def mots(nom: str | None) -> list[str]:
    if not nom:
        return []
    return [m for m in re.split(r"[^a-z0-9]+", sans_accents(nom).lower()) if m and m not in MOTS_VIDES]


def ressemblance(a: str | None, b: str | None) -> float:
    ma, mb = mots(a), mots(b)
    if not ma or not mb:
        return 0.0
    commun = len(set(ma) & set(mb)) / min(len(set(ma)), len(set(mb)))
    return max(commun, difflib.SequenceMatcher(None, " ".join(ma), " ".join(mb)).ratio())

RE_EMAIL = re.compile(r"^[a-z0-9._%+'-]+@[a-z0-9-]+(\.[a-z0-9-]+)*\.[a-z]{2,24}$")


DEPARTEMENTS_METROPOLE = [f"{n:02d}" for n in range(1, 96) if n != 20] + ["2A", "2B"]


def liste_departements(texte: str | None) -> list[str]:
    """« 35,44 » → ['35', '44'] ; « metropole » → les 96 départements de France métropolitaine."""
    departements = []
    for d in (texte or "").split(","):
        d = d.strip()
        if d.lower() in ("metropole", "métropole"):
            departements += DEPARTEMENTS_METROPOLE
        elif d:
            departements.append(d)
    return list(dict.fromkeys(departements))


def maintenant() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def sans_accents(texte: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", texte) if not unicodedata.combining(c))


def normaliser_email(brut: str) -> str | None:
    """Adresse en minuscules si elle est valable pour la prospection, sinon None."""
    if not brut:
        return None
    email = brut.strip().strip(".,;:()<>[]\"'").lower()
    email = email.removeprefix("mailto:").split("?")[0]
    if not RE_EMAIL.match(email) or ".." in email:
        return None
    local, domaine = email.split("@", 1)
    if local in LOCAUX_EXCLUS or domaine in DOMAINES_EXCLUS:
        return None
    if any(domaine.endswith("." + d) for d in DOMAINES_EXCLUS):
        return None
    if re.search(r"\.(png|jpe?g|gif|webp|svg|css|js)$", domaine):  # « logo@2x.png »
        return None
    return email


def type_email(email: str, *noms: str | None) -> str:
    """« generique » pour une adresse de l'établissement (contact@…, ou reprenant son nom,
    ex. soyocaferennes@gmail.com pour « Soyo Café »), sinon « nominatif »."""
    local = sans_accents(email.split("@", 1)[0]).lower()
    mots = set(re.split(r"[._+-]+", local))
    if local in LOCAUX_GENERIQUES or mots & LOCAUX_GENERIQUES:
        return "generique"
    colle = re.sub(r"[^a-z0-9]", "", local)
    for nom in noms:
        for mot in re.split(r"[^a-z0-9]+", sans_accents(nom or "").lower()):
            if len(mot) >= 4 and mot in colle:
                return "generique"
    return "nominatif"


def site_utilisable(url: str | None) -> str | None:
    """URL du site propre de l'établissement (https par défaut), sinon None."""
    if not url:
        return None
    url = url.strip().split()[0].split(";")[0]
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url.lstrip("/")
    hote = re.sub(r"^https?://", "", url, flags=re.I).split("/")[0].lower()
    if "." not in hote or any(p in hote for p in SITES_PLATEFORMES):
        return None
    return url


def sql_texte(valeur) -> str:
    if valeur is None:
        return "NULL"
    if isinstance(valeur, (int, float)):
        return repr(valeur) if valeur == valeur else "NULL"  # NaN → NULL
    return "'" + str(valeur).replace("'", "''") + "'"


class QuotaD1(RuntimeError):
    """Limite quotidienne d'écritures de l'offre gratuite de D1 atteinte (remise à zéro à minuit UTC)."""


class D1:
    """Requêtes SQL sur la base D1 par l'API de Cloudflare (jeton « D1 Edit »)."""

    def __init__(self):
        self.url = D1_URL.format(compte=os.environ["CLOUDFLARE_ACCOUNT_ID"], base=os.environ["D1_DATABASE_ID"])
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {os.environ['CLOUDFLARE_API_TOKEN']}"
        self.lignes_ecrites = 0

    def requete(self, sql: str, params: list | None = None) -> list[dict]:
        for essai in range(4):
            r = self.session.post(self.url, json={"sql": sql, "params": params or []}, timeout=120)
            if r.status_code in (429, 500, 502, 503, 504) and essai < 3:
                time.sleep(2 ** (essai + 1))
                continue
            corps = r.json()
            if not corps.get("success"):
                if any(e.get("code") == 7500 for e in corps.get("errors") or []):
                    raise QuotaD1("D1 : limite quotidienne d'écritures atteinte")
                raise RuntimeError(f"D1 : {json.dumps(corps.get('errors'), ensure_ascii=False)[:500]}")
            resultats = corps["result"]
            self.lignes_ecrites += sum(x.get("meta", {}).get("rows_written", 0) for x in resultats)
            return resultats[-1].get("results", []) if resultats else []
        raise RuntimeError("D1 injoignable")

    def inserer(self, table: str, colonnes: list[str], lignes: list[tuple], conflit: str):
        """INSERT en plusieurs lignes, découpé pour rester sous la taille maximale d'une requête."""
        entete = f"INSERT INTO {table} ({', '.join(colonnes)}) VALUES "
        paquet, taille = [], len(entete) + len(conflit)
        for ligne in lignes:
            valeurs = "(" + ", ".join(sql_texte(v) for v in ligne) + ")"
            if paquet and taille + len(valeurs) + 2 > TAILLE_MAX_SQL:
                self.requete(entete + ",\n".join(paquet) + " " + conflit)
                paquet, taille = [], len(entete) + len(conflit)
            paquet.append(valeurs)
            taille += len(valeurs) + 2
        if paquet:
            self.requete(entete + ",\n".join(paquet) + " " + conflit)

    def journal(self, tache: str, detail: dict):
        self.requete("INSERT INTO journal (date, tache, detail) VALUES (?, ?, ?)",
                     [maintenant(), tache, json.dumps(detail, ensure_ascii=False)])


class Fichier(D1):
    """Même interface que D1, sur le fichier SQLite de travail (copie maîtresse gardée dans R2,
    voir fichier_r2.py) : D1 est elle-même une base SQLite, les requêtes sont identiques."""

    def __init__(self, chemin: str):
        self.connexion = sqlite3.connect(chemin, isolation_level=None)
        self.connexion.row_factory = sqlite3.Row
        self.connexion.execute("PRAGMA foreign_keys = ON")  # comme D1
        self.connexion.execute("PRAGMA synchronous = OFF")
        self.lignes_ecrites = 0

    def requete(self, sql: str, params: list | None = None) -> list[dict]:
        avant = self.connexion.total_changes
        lignes = [dict(r) for r in self.connexion.execute(sql, params or [])]
        self.lignes_ecrites += self.connexion.total_changes - avant
        return lignes


def ouvrir_base() -> D1:
    """Fichier de travail si BASE_FICHIER est donné (cas normal des workflows), sinon D1 directement."""
    chemin = os.environ.get("BASE_FICHIER")
    return Fichier(chemin) if chemin else D1()
