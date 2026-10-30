"""Envoi des campagnes de prospection, appelé toutes les 30 minutes par le workflow « envois ».

Pour chaque campagne active (table `campagnes`, réglée depuis le back-office) :
1. hors de ses plages horaires (heure de Paris), rien n'est envoyé ;
2. dans une plage, le reste du quota du jour est réparti sur les passages restants de la journée
   (ex. 40 e-mails par jour sur 9 h 30–11 h et 15 h–17 h 30 : environ 5 par passage) ;
3. les destinataires sont tirés de la base : adresse valide, générique (nominative seulement si la
   campagne le permet), établissement actif de la cible, de la zone et des catégories choisies ;
   jamais une adresse exclue (inscrit France Resto, désinscription, erreur, plainte) ni déjà contactée.
Chaque e-mail porte l'identité de l'expéditeur, l'origine de l'adresse (page où elle est publiée)
et un lien de désinscription (en-têtes List-Unsubscribe et un clic, RFC 8058).
Les services d'envoi (table `fournisseurs`) sont utilisés dans l'ordre de priorité, chacun dans
son quota quotidien ; un service n'est activé qu'après l'accord écrit de l'hébergeur.

Variables : celles de D1 ; BREVO_API_KEY ; LIEN_SECRET (signature des liens de désinscription) ;
DESINSCRIPTION_URL (page du site) ; FONCTION_URL (désinscription en un clic) ;
APERCU=oui : rien n'est envoyé ni écrit, résumé et premier e-mail affichés ; D1_TEST : fichier SQLite
à la place de D1 (essais).
"""

import hashlib
import hmac
import html
import json
import math
import os
import re
import sys
import time
from base64 import urlsafe_b64encode
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

sys.path.insert(0, os.path.dirname(__file__))
from commun import D1, Fichier, QuotaD1, maintenant, sql_texte  # noqa: E402

PARIS = ZoneInfo("Europe/Paris")
PASSAGE_MIN = 30  # minutes entre deux passages du workflow
IDENTITE = "France Resto (SAS France Resto, 1 rue Vauban, 64100 Bayonne, SIREN 109 848 671)"
# Hors des coups de feu du midi et du soir, du lundi au vendredi.
PLAGES_DEFAUT = {
    "chr": [{"jours": [1, 2, 3, 4, 5], "debut": "09:30", "fin": "11:00"},
            {"jours": [1, 2, 3, 4, 5], "debut": "15:00", "fin": "17:30"}],
    "annuaire": [{"jours": [1, 2, 3, 4, 5], "debut": "09:00", "fin": "12:00"},
                 {"jours": [1, 2, 3, 4, 5], "debut": "14:00", "fin": "17:30"}],
}
SPECIALITES = {
    "avocat": "avocat", "expert_comptable": "expert-comptable", "courtier_energie": "courtier en énergie",
    "architecte_interieur": "architecte d'intérieur", "agenceur_cuisine": "agenceur de cuisine",
    "decorateur": "décorateur", "designer_menu": "designer de menu", "hygiene_haccp": "hygiène (HACCP)",
    "securite_accessibilite": "sécurité et accessibilité", "auditeur_food_cost": "auditeur food cost",
    "client_mystere": "client mystère", "chef_consultant": "chef consultant", "mixologue": "mixologue",
    "creation_carte": "création de carte", "sommelier": "sommelier", "agence_communication": "agence de communication",
    "photographe_culinaire": "photographe culinaire", "seo": "référencement (SEO)", "site_web": "création de sites web",
    "formateur": "formateur", "ecole_hoteliere": "école hôtelière", "centre_formation": "centre de formation",
    "consultant_chr": "consultant CHR", "logiciel_caisse": "logiciel de caisse", "immobilier_chr": "immobilier CHR",
    "services_techniques": "services techniques",
}
SOURCES = {"osm": "OpenStreetMap", "datatourisme": "DATAtourisme (offices de tourisme)",
           "site": "votre site", "prompt": "un annuaire public", "manuel": "une source publique"}
RE_DEPARTEMENT = re.compile(r"^(2[AB]|[0-9]{2,3})$")
RE_CATEGORIE = re.compile(r"^[a-z_]{2,40}$")
# Pendant la diffusion de la newsletter (même quota Brevo) : e-mails de prospection gardés par jour
PROSPECTION_PENDANT_NEWSLETTER = int(os.environ.get("PROSPECTION_PENDANT_NEWSLETTER", "30"))


def minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def plages_du_jour(plages: list[dict], instant: datetime) -> list[tuple[int, int]]:
    jour = instant.isoweekday()
    return [(minutes(p["debut"]), minutes(p["fin"])) for p in plages if jour in p.get("jours", [])]


def a_envoyer(plages: list[dict], instant: datetime, reste_du_jour: int) -> int:
    """Part du reste du quota du jour à envoyer maintenant : 0 hors des plages, sinon le reste
    réparti sur les passages restants dans les plages du jour."""
    if reste_du_jour <= 0:
        return 0
    m = instant.hour * 60 + instant.minute
    du_jour = plages_du_jour(plages, instant)
    if not any(debut <= m < fin for debut, fin in du_jour):
        return 0
    restantes = sum(max(0, fin - max(debut, m)) for debut, fin in du_jour)
    passages = max(1, math.ceil(restantes / PASSAGE_MIN))
    return math.ceil(reste_du_jour / passages)


def debut_du_jour_utc(instant: datetime) -> str:
    minuit = instant.replace(hour=0, minute=0, second=0, microsecond=0)
    return minuit.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def empreinte(email: str) -> str:
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()


def signature(email: str, secret: str) -> str:
    return hmac.new(secret.encode(), email.encode(), hashlib.sha256).hexdigest()[:32]


def liens_desinscription(email: str) -> tuple[str, str]:
    """(page du site, adresse du désinscription en un clic) pour cette adresse."""
    e = urlsafe_b64encode(email.encode()).decode().rstrip("=")
    s = signature(email, os.environ["LIEN_SECRET"])
    return (f"{os.environ['DESINSCRIPTION_URL']}?e={e}&s={s}",
            f"{os.environ['FONCTION_URL']}?action=desinscription&e={e}&s={s}")


def joli(texte: str | None) -> str:
    """« LE PETIT CROCUS » → « Le Petit Crocus » (les noms SIRENE sont en capitales)."""
    if not texte:
        return ""
    if texte.isupper():
        petits = {"de", "du", "des", "la", "le", "les", "et", "a", "au", "aux", "en", "sur", "l", "d"}
        mots = re.split(r"(\W+)", texte.lower())
        return "".join(m if (i and m in petits) else m[:1].upper() + m[1:] for i, m in enumerate(mots))
    return texte


SITE = "https://franceresto.net"
RE_LIEN_SITE = re.compile(r"https://(?:www\.)?franceresto\.net(?:[/?#][^\s\"'<>)]*)?")
RE_HREF_SITE = re.compile(r'href="(https://(?:www\.)?franceresto\.net(?:[/?#][^"]*)?)"')


def avec_utm(url: str, campagne: dict) -> str:
    """Lien vers le site avec le suivi de la campagne (paramètres UTM déjà présents conservés)."""
    morceaux = urlsplit(url)
    if not morceaux.path:
        morceaux = morceaux._replace(path="/")
    params = dict(parse_qsl(morceaux.query, keep_blank_values=True))
    params.setdefault("utm_source", "prospection")
    params.setdefault("utm_medium", "email")
    params.setdefault("utm_campaign", campagne.get("code") or f"campagne-{campagne['id']}")
    return urlunsplit(morceaux._replace(query=urlencode(params)))


def suivi_texte(url: str, campagne: dict) -> str:
    """Adresse écrite dans la version texte : la ponctuation qui la suit n'en fait pas partie."""
    fin = re.search(r"[.,;:!?]+$", url)
    return avec_utm(url[:fin.start()], campagne) + fin.group(0) if fin else avec_utm(url, campagne)


def rendre(gabarit: str | None, valeurs: dict, en_html: bool) -> str:
    def remplacer(m):
        v = valeurs.get(m.group(1), "")
        return html.escape(v) if en_html else v
    return re.sub(r"\{\{\s*(\w+)\s*\}\}", remplacer, gabarit or "")


def composer(campagne: dict, d: dict) -> dict:
    specialites = json.loads(d.get("specialites") or "[]")
    valeurs = {"etablissement": joli(d["nom"]) or "votre établissement", "commune": joli(d["commune"]),
               "specialite": next((SPECIALITES[s] for s in specialites if s in SPECIALITES), ""),
               # {{lien}} : page d'arrivée suivie ; {{lien_affiche}} : la même, sans suivi (texte du lien)
               "lien": avec_utm(campagne.get("lien") or SITE, campagne), "lien_affiche": campagne.get("lien") or SITE}
    page, un_clic = liens_desinscription(d["email"])
    origine = SOURCES.get(d["source"], "une source publique")
    if d["source"] == "site" and d.get("url"):
        origine = f"votre site ({d['url']})"
    pied_texte = (f"\n\n--\n{IDENTITE}.\nVous recevez ce message car l'adresse {d['email']} est publiée sur {origine}. "
                  f"Pour ne plus recevoir nos messages : {page}")
    pied_html = (f'<hr style="border:none;border-top:1px solid #ddd;margin:24px 0 12px">'
                 f'<p style="font-size:12px;color:#666;line-height:1.5">{html.escape(IDENTITE)}.<br>'
                 f'Vous recevez ce message car l\'adresse {html.escape(d["email"])} est publiée sur '
                 f'{html.escape(origine)}.<br><a href="{html.escape(page)}">Ne plus recevoir nos messages</a></p>')
    corps_html = rendre(campagne["contenu_html"], valeurs, True)
    corps_texte = rendre(campagne["contenu_texte"], valeurs, False) or html.unescape(re.sub(r"<[^>]+>", "", corps_html))
    # Liens vers le site suivis par campagne : cibles des liens HTML (le texte affiché reste lisible) et
    # adresses de la version texte ; le pied de page, ajouté ensuite, n'est pas concerné
    corps_html = RE_HREF_SITE.sub(
        lambda m: f'href="{html.escape(avec_utm(html.unescape(m.group(1)), campagne))}"', corps_html)
    corps_texte = RE_LIEN_SITE.sub(lambda m: suivi_texte(m.group(0), campagne), corps_texte)
    return {
        "objet": rendre(campagne["objet"], valeurs, False),
        "html": f'<div style="font-family:Arial,sans-serif;font-size:15px;line-height:1.6">{corps_html}{pied_html}</div>',
        "texte": corps_texte + pied_texte,
        "entetes": {"List-Unsubscribe": f"<{un_clic}>", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"},
    }


def envoyer_brevo(fournisseur: dict, campagne: dict, d: dict, message: dict) -> str:
    corps = {
        "sender": {"name": campagne["expediteur_nom"] or "France Resto", "email": fournisseur["expediteur"]},
        "to": [{"email": d["email"]}],
        "subject": message["objet"], "htmlContent": message["html"], "textContent": message["texte"],
        "headers": message["entetes"], "tags": [f"prospection-{campagne['code'] or campagne['id']}"],
    }
    if campagne["repondre_a"]:
        corps["replyTo"] = {"email": campagne["repondre_a"]}
    r = requests.post("https://api.brevo.com/v3/smtp/email", json=corps, timeout=30,
                      headers={"api-key": os.environ["BREVO_API_KEY"], "accept": "application/json"})
    if r.status_code != 201:
        raise RuntimeError(f"Brevo {r.status_code} : {r.text[:300]}")
    return r.json()["messageId"]


ENVOI = {"brevo": envoyer_brevo}


def utilise_commune(campagne: dict) -> bool:
    return any(re.search(r"\{\{\s*commune\s*\}\}", campagne.get(k) or "")
               for k in ("objet", "contenu_html", "contenu_texte"))


def destinataires(d1: D1, campagne: dict, n: int) -> list[dict]:
    """n destinataires au plus, tirés au hasard dans la cible de la campagne."""
    zone = json.loads(campagne["zone"] or "{}")
    categories = [c for c in json.loads(campagne["categories"] or "[]") if RE_CATEGORIE.match(c)]
    conditions = ["m.statut = 'valide'", "e.actif = 1", f"e.cible = {sql_texte(campagne['cible'])}",
                  "NOT EXISTS (SELECT 1 FROM envois v WHERE v.email = m.email)"]
    if not campagne["nominatifs"]:
        conditions.append("m.type = 'generique'")
    if utilise_commune(campagne):
        # Message qui cite la commune : jamais de « à . » (fiches provisoires des listes externes, sans commune)
        conditions.append("coalesce(e.commune, '') <> ''")
    departements = [d for d in zone.get("departements", []) if RE_DEPARTEMENT.match(d)]
    if departements:
        conditions.append(f"e.departement IN ({', '.join(sql_texte(d) for d in departements)})")
    centre = zone.get("centre")
    if centre:
        lat, lon, rayon = float(centre["lat"]), float(centre["lon"]), float(centre["rayon_km"])
        dlat, dlon = rayon / 111, rayon / (111 * max(0.2, math.cos(math.radians(lat))))
        conditions.append(f"e.lat BETWEEN {lat - dlat} AND {lat + dlat} AND e.lon BETWEEN {lon - dlon} AND {lon + dlon}")
    if categories:
        if campagne["cible"] == "annuaire":
            conditions.append("(" + " OR ".join(f"e.specialites LIKE {sql_texte('%' + chr(34) + c + chr(34) + '%')}"
                                                for c in categories) + ")")
        else:
            conditions.append(f"e.categorie IN ({', '.join(sql_texte(c) for c in categories)})")
    lignes = d1.requete(
        f"""SELECT m.email, e.siret, e.nom, e.commune, e.departement, e.categorie, e.specialites, e.lat, e.lon,
                   s.source, s.url
            FROM emails m JOIN email_sources s ON s.email = m.email JOIN etablissements e ON e.siret = s.siret
            WHERE {' AND '.join(conditions)} ORDER BY random() LIMIT {int(n) * 4}""")
    if centre:
        def distance(l):
            if l["lat"] is None:
                return 1e9
            x = math.radians(l["lon"] - lon) * math.cos(math.radians((l["lat"] + lat) / 2))
            y = math.radians(l["lat"] - lat)
            return 6371 * math.hypot(x, y)
        lignes = [l for l in lignes if distance(l) <= rayon]
    vus, retenus = set(), []
    for l in lignes:
        if l["email"] not in vus:
            vus.add(l["email"])
            retenus.append(l)
    exclues = set()
    for i in range(0, len(retenus), 90):
        lot = [empreinte(l["email"]) for l in retenus[i:i + 90]]
        exclues |= {r["email_hash"] for r in d1.requete(
            f"SELECT email_hash FROM exclusions WHERE email_hash IN ({','.join('?' * len(lot))})", lot)}
    return [l for l in retenus if empreinte(l["email"]) not in exclues][:n]


def principal():
    apercu = os.environ.get("APERCU") == "oui"
    d1 = Fichier(os.environ["D1_TEST"]) if os.environ.get("D1_TEST") else D1()
    instant = datetime.now(PARIS)
    if os.environ.get("INSTANT"):  # essais
        instant = datetime.fromisoformat(os.environ["INSTANT"]).astimezone(PARIS)
    debut = debut_du_jour_utc(instant)
    horodatage = instant.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Newsletter des inscrits en cours de diffusion (Edge Function admin, même quota Brevo) : la prospection
    # est limitée à PROSPECTION_PENDANT_NEWSLETTER e-mails par jour (30 par défaut), le reste va à la newsletter
    d1.requete("CREATE TABLE IF NOT EXISTS pauses (motif TEXT PRIMARY KEY, jusqu_au TEXT NOT NULL)")
    pause = d1.requete("SELECT motif, jusqu_au FROM pauses WHERE jusqu_au > ?", [horodatage])
    fournisseurs = d1.requete("SELECT * FROM fournisseurs WHERE actif = 1 ORDER BY priorite, nom")
    for f in fournisseurs:
        deja = d1.requete("SELECT count(*) AS n FROM envois WHERE fournisseur = ? AND envoye_le >= ?", [f["nom"], debut])
        f["reste"] = max(0, f["quota_jour"] - deja[0]["n"])
    if pause:
        permis = max(0, PROSPECTION_PENDANT_NEWSLETTER - d1.requete(
            "SELECT count(*) AS n FROM envois WHERE fournisseur IS NOT NULL AND envoye_le >= ?", [debut])[0]["n"])
        for f in fournisseurs:
            f["reste"] = min(f["reste"], permis)
            permis -= f["reste"]
    resume = {"instant": instant.isoformat(timespec="minutes"), "apercu": apercu,
              "fournisseurs": {f["nom"]: f["reste"] for f in fournisseurs}, "campagnes": {}}
    if pause:
        resume["newsletter"] = {"pause": pause, "plafond_prospection": PROSPECTION_PENDANT_NEWSLETTER}
    campagnes = d1.requete("SELECT * FROM campagnes WHERE statut = 'active' AND (debut_le IS NULL OR debut_le <= ?) "
                           "ORDER BY id", [horodatage])
    for c in campagnes:
        plages = json.loads(c["plages"]) if c["plages"] else PLAGES_DEFAUT.get(c["cible"], PLAGES_DEFAUT["chr"])
        jour = d1.requete("SELECT count(*) AS n FROM envois WHERE campagne_id = ? AND envoye_le >= ?", [c["id"], debut])[0]["n"]
        total = d1.requete("SELECT count(*) AS n FROM envois WHERE campagne_id = ?", [c["id"]])[0]["n"]
        reste = c["quota_jour"] - jour
        if c["total_max"] is not None:
            reste = min(reste, c["total_max"] - total)
        n = min(a_envoyer(plages, instant, reste), sum(f["reste"] for f in fournisseurs))
        etat = {"aujourdhui": jour, "total": total, "prevus": n, "envoyes": 0, "erreurs": []}
        resume["campagnes"][c["id"]] = etat
        if c["total_max"] is not None and total >= c["total_max"] and not apercu:
            d1.requete("UPDATE campagnes SET statut = 'terminee', modifiee_le = ? WHERE id = ?", [maintenant(), c["id"]])
            continue
        if n <= 0:
            continue
        liste = destinataires(d1, c, n)
        etat["trouves"] = len(liste)  # 0 : plus personne dans la cible pour l'instant (nouveaux imports à venir)
        for d in liste:
            message = composer(c, d)
            if apercu:
                etat.setdefault("exemple", {"a": d["email"], "objet": message["objet"], "texte": message["texte"][:1500]})
                continue
            f = next((f for f in fournisseurs if f["reste"] > 0), None)
            if f is None:
                break
            # Envoi inscrit AVANT l'appel au service : si D1 refuse l'écriture (limite du jour),
            # rien n'est envoyé, et une adresse envoyée n'est jamais renvoyée au passage suivant.
            try:
                inscrit = d1.requete(
                    "INSERT INTO envois (campagne_id, email, siret, etape, envoye_le, statut, fournisseur) "
                    "VALUES (?, ?, ?, 1, ?, 'envoye', ?) ON CONFLICT (campagne_id, email, etape) DO NOTHING "
                    "RETURNING email", [c["id"], d["email"], d["siret"], horodatage, f["nom"]])
            except QuotaD1:
                resume["quota_d1_atteint"] = True
                break
            if not inscrit:  # déjà contactée par cette campagne
                continue
            try:
                message_id = ENVOI[f["type"]](f, c, d, message)
            except Exception as erreur:  # une adresse refusée ne bloque pas les suivantes
                etat["erreurs"].append(str(erreur)[:200])
                try:  # non envoyé : l'adresse redevient disponible
                    d1.requete("DELETE FROM envois WHERE campagne_id = ? AND email = ? AND etape = 1", [c["id"], d["email"]])
                except QuotaD1:
                    resume["quota_d1_atteint"] = True
                    break
                if len(etat["erreurs"]) >= 5:
                    break
                continue
            try:  # identifiant du message, pour rattacher les événements de Brevo (délivré, ouvert…)
                d1.requete("UPDATE envois SET message_id = ? WHERE campagne_id = ? AND email = ? AND etape = 1",
                           [message_id, c["id"], d["email"]])
            except QuotaD1:
                resume["quota_d1_atteint"] = True
                etat["envoyes"] += 1
                break
            f["reste"] -= 1
            etat["envoyes"] += 1
            time.sleep(0.5)
    print(json.dumps(resume, ensure_ascii=False, indent=1))
    if os.environ.get("RESUME"):
        with open(os.environ["RESUME"], "w") as f:
            json.dump(resume, f, ensure_ascii=False, indent=1)
    if not apercu and any(e["envoyes"] or e["erreurs"] for e in resume["campagnes"].values()):
        try:
            d1.journal("envois", resume)
        except QuotaD1:
            pass


if __name__ == "__main__":
    principal()
