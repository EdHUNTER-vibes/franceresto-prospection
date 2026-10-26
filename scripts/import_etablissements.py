"""Import des établissements CHR d'un ou plusieurs départements dans le fichier de travail (ou D1).

1. SIRENE (Licence Ouverte) : établissements actifs des codes NAF CHR, sans les entreprises
   en diffusion partielle (statut « P » : la personne s'est opposée à l'usage commercial de
   ses données), coordonnées Lambert 93 converties en GPS.
2. OpenStreetMap (ODbL, usage interne) : site web et e-mail publiés sur les lieux CHR,
   rattachés à l'établissement SIRENE le plus proche dont le nom correspond.
   DATAtourisme (Licence Ouverte, fichiers régionaux de data.gouv.fr) : site web des restaurants,
   bars et hôtels, utilisé quand OSM n'en donne pas.
3. Établissements absents du nouvel import : marqués inactifs (fermés).

Variables : DEPARTEMENTS (ex. « 35 » ou « 35,56 »), SIRENE_ETAB, SIRENE_UL (fichiers parquet,
chemins locaux ou adresses https),
OSM_PBF (fichiers .osm.pbf, séparés par des virgules), DATATOURISME_CSV (fichiers régionaux), CLOUDFLARE_ACCOUNT_ID, D1_DATABASE_ID,
CLOUDFLARE_API_TOKEN. APERCU=oui : rien n'est écrit, résumé seulement.
"""

import csv
import json
import os
import re
import sys

import duckdb
import osmium
from pyproj import Transformer

sys.path.insert(0, os.path.dirname(__file__))
from commun import liste_departements, maintenant, normaliser_email, ouvrir_base, ressemblance, site_utilisable, type_email  # noqa: E402

CATEGORIES = {
    "55.10Z": "hotel",
    "56.10A": "restaurant",
    "56.10B": "cafeteria",
    "56.10C": "restauration_rapide",
    "56.21Z": "traiteur",
    "56.30Z": "bar",
}
OSM_LIEUX = {
    "amenity": {"restaurant", "fast_food", "cafe", "bar", "pub", "biergarten", "food_court", "ice_cream"},
    "tourism": {"hotel", "motel", "guest_house"},
    "craft": {"caterer"},
    "shop": {"catering"},
}
def lire_sirene(departements: list[str]) -> list[dict]:
    con = duckdb.connect()
    if os.environ["SIRENE_ETAB"].startswith("http"):
        # Lecture à distance des seules colonnes utiles (le téléchargement complet, 3 Go, est
        # parfois coupé par le serveur) ; DuckDB reprend les requêtes en échec.
        con.execute("INSTALL httpfs; LOAD httpfs; SET http_retries = 10; SET http_retry_wait_ms = 2000; "
                    "SET http_timeout = 120000")
    filtre_dep = " OR ".join(f"e.codeCommuneEtablissement LIKE '{d}%'" for d in departements)
    nafs = ", ".join(f"'{n}'" for n in CATEGORIES)
    lignes = con.execute(f"""
        SELECT e.siret, e.siren, e.activitePrincipaleEtablissement AS naf,
               coalesce(nullif(trim(e.enseigne1Etablissement), ''),
                        nullif(trim(e.denominationUsuelleEtablissement), '')) AS enseigne,
               coalesce(u.denominationUniteLegale,
                        nullif(trim(concat_ws(' ', u.prenomUsuelUniteLegale, u.nomUniteLegale)), '')) AS raison_sociale,
               cast(u.categorieJuridiqueUniteLegale AS VARCHAR) AS cat_juridique,
               nullif(trim(concat_ws(' ', e.numeroVoieEtablissement, e.indiceRepetitionEtablissement,
                                     e.typeVoieEtablissement, e.libelleVoieEtablissement)), '') AS adresse,
               e.codePostalEtablissement AS code_postal, e.libelleCommuneEtablissement AS commune,
               e.codeCommuneEtablissement AS code_commune,
               try_cast(e.coordonneeLambertAbscisseEtablissement AS DOUBLE) AS x,
               try_cast(e.coordonneeLambertOrdonneeEtablissement AS DOUBLE) AS y,
               e.trancheEffectifsEtablissement AS tranche, e.dateCreationEtablissement AS creation
        FROM read_parquet('{os.environ["SIRENE_ETAB"]}') e
        JOIN read_parquet('{os.environ["SIRENE_UL"]}') u USING (siren)
        WHERE e.etatAdministratifEtablissement = 'A'
          AND e.activitePrincipaleEtablissement IN ({nafs})
          AND e.statutDiffusionEtablissement = 'O'
          AND u.statutDiffusionUniteLegale = 'O'
          AND ({filtre_dep})
    """).fetchall()
    noms = [d[0] for d in con.description]
    l93 = Transformer.from_crs("EPSG:2154", "EPSG:4326", always_xy=True)
    etablissements = []
    for ligne in lignes:
        e = dict(zip(noms, ligne))
        lat = lon = None
        if e["x"] and e["y"]:
            lon, lat = l93.transform(e["x"], e["y"])
            lat, lon = round(lat, 6), round(lon, 6)
        code = e["code_commune"] or ""
        # Raison sociale d'un entrepreneur individuel = nom d'une personne : jamais affichée comme nom.
        personne = (e["cat_juridique"] or "").startswith("1")
        etablissements.append({
            "siret": e["siret"], "siren": e["siren"], "naf": e["naf"], "categorie": CATEGORIES[e["naf"]],
            "nom": e["enseigne"] or (None if personne else e["raison_sociale"]),
            "raison_sociale": None if personne else e["raison_sociale"],
            "adresse": e["adresse"], "code_postal": e["code_postal"], "commune": e["commune"],
            "code_commune": code, "departement": code[:3] if code.startswith("97") else code[:2],
            "lat": lat, "lon": lon, "tranche_effectif": e["tranche"],
            "date_creation": str(e["creation"]) if e["creation"] else None,
        })
    return etablissements


class LieuxOSM(osmium.SimpleHandler):
    def __init__(self):
        super().__init__()
        self.lieux = []

    def _garder(self, objet, type_osm, lat, lon, ident=None):
        t = objet.tags
        if not any(t.get(cle) in valeurs for cle, valeurs in OSM_LIEUX.items()):
            return
        site = t.get("website") or t.get("contact:website") or t.get("url")
        emails = [t.get(c) for c in ("email", "contact:email") if t.get(c)]
        if not site and not emails:
            return
        self.lieux.append({"id": f"{type_osm}/{ident or objet.id}", "nom": t.get("name"), "lat": lat, "lon": lon,
                           "site": site, "emails": emails})

    def node(self, n):
        if n.location.valid():
            self._garder(n, "node", n.location.lat, n.location.lon)

    def area(self, a):
        try:
            pts = [(n.lat, n.lon) for anneau in a.outer_rings() for n in anneau]
        except Exception:
            return
        if pts:
            lat = sum(p[0] for p in pts) / len(pts)
            lon = sum(p[1] for p in pts) / len(pts)
            self._garder(a, "way" if a.from_way() else "relation", lat, lon, a.orig_id())


def distance_m(a_lat, a_lon, b_lat, b_lon) -> float:
    from math import cos, radians, sqrt
    dx = (b_lon - a_lon) * 111_320 * cos(radians((a_lat + b_lat) / 2))
    dy = (b_lat - a_lat) * 110_540
    return sqrt(dx * dx + dy * dy)


def lire_datatourisme(chemin: str) -> list[dict]:
    """Restaurants, bars et hôtels du fichier régional DATAtourisme (data.gouv.fr, Licence Ouverte).
    Le fichier ne publie pas les e-mails, seulement le site web (colonne Contacts_du_POI :
    « nom#url|nom#url »)."""
    csv.field_size_limit(10 ** 8)
    lieux = []
    with open(chemin, encoding="utf-8", errors="replace", newline="") as f:
        for ligne in csv.DictReader(f):
            types = {c.rsplit("#", 1)[-1].rsplit("/", 1)[-1] for c in (ligne.get("Categories_de_POI") or "").split("|")}
            if not types & {"FoodEstablishment", "Restaurant", "BarOrPub", "HotelTrade", "Hotel"}:
                continue
            sites = [c.split("#", 1)[1] for c in (ligne.get("Contacts_du_POI") or "").split("|") if "#" in c]
            site = next((u for u in sites if site_utilisable(u)), None)
            try:
                lat, lon = float(ligne["Latitude"]), float(ligne["Longitude"])
            except (TypeError, ValueError):
                continue
            if site:
                lieux.append({"id": ligne.get("URI_ID_du_POI"), "nom": ligne.get("Nom_du_POI"), "lat": lat, "lon": lon,
                              "site": site, "emails": []})
    return lieux


def rattacher(etablissements: list[dict], lieux: list[dict], cle: str) -> int:
    """Lieu (OSM, DATAtourisme) → établissement SIRENE : moins de 150 m et nom ressemblant.
    Chaque établissement reçoit au plus un lieu par source (e[cle])."""
    grille: dict[tuple, list[dict]] = {}
    for e in etablissements:
        if e["lat"] is not None:
            grille.setdefault((round(e["lat"], 2), round(e["lon"], 2)), []).append(e)
    rattaches = 0
    for lieu in lieux:
        candidats = []
        for dlat in (-0.01, 0, 0.01):
            for dlon in (-0.01, 0, 0.01):
                candidats += grille.get((round(lieu["lat"] + dlat, 2), round(lieu["lon"] + dlon, 2)), [])
        meilleur, score_max = None, 0.0
        proches = []
        for e in candidats:
            d = distance_m(lieu["lat"], lieu["lon"], e["lat"], e["lon"])
            if d > 150:
                continue
            proches.append(e)
            s = max(ressemblance(lieu["nom"], e["nom"]), ressemblance(lieu["nom"], e["raison_sociale"]))
            s -= d / 1500  # à ressemblance égale, le plus proche
            if s > score_max:
                meilleur, score_max = e, s
        if meilleur is None or score_max < 0.55:
            continue
        if cle in meilleur:
            continue
        meilleur[cle] = lieu
        rattaches += 1
    return rattaches


def main():
    departements = liste_departements(os.environ["DEPARTEMENTS"])
    apercu = os.environ.get("APERCU") == "oui"
    date = maintenant()

    etablissements = lire_sirene(departements)
    par_cat: dict[str, int] = {}
    for e in etablissements:
        par_cat[e["categorie"]] = par_cat.get(e["categorie"], 0) + 1
    print(f"SIRENE : {len(etablissements)} établissements actifs ({json.dumps(par_cat)})")

    lieux = []
    for pbf in filter(None, os.environ.get("OSM_PBF", "").split(",")):
        h = LieuxOSM()
        h.apply_file(pbf, locations=True)
        lieux += h.lieux
    rattaches = rattacher(etablissements, lieux, "osm")
    print(f"OSM : {len(lieux)} lieux CHR avec site ou e-mail, {rattaches} rattachés à un établissement")
    lieux_dt = []
    for fichier in filter(None, os.environ.get("DATATOURISME_CSV", "").split(",")):
        lieux_dt += lire_datatourisme(fichier)
    rattaches_dt = rattacher(etablissements, lieux_dt, "dt")
    print(f"DATAtourisme : {len(lieux_dt)} lieux CHR avec site, {rattaches_dt} rattachés à un établissement")

    emails, sources = {}, []
    avec_site = 0
    for e in etablissements:
        osm = e.get("osm")
        e["site_web"] = site_utilisable(osm["site"]) if osm else None
        e["site_source"] = "osm" if e["site_web"] else None
        if not e["site_web"] and e.get("dt"):
            e["site_web"], e["site_source"] = site_utilisable(e["dt"]["site"]), "datatourisme"
        e["osm_id"] = osm["id"] if osm else None
        avec_site += e["site_web"] is not None
        for brut in (osm["emails"] if osm else []):
            for morceau in re.split(r"[;,\s]+", brut):
                email = normaliser_email(morceau)
                if email:
                    emails[email] = (email, email.split("@")[1], type_email(email, e["nom"], e["raison_sociale"], osm["nom"]),
                                     "a_verifier", None, date)
                    sources.append((email, e["siret"], "osm", f"https://www.openstreetmap.org/{osm['id']}", date))
    resume = {
        "departements": departements, "etablissements": len(etablissements), "par_categorie": par_cat,
        "osm_lieux": len(lieux), "osm_rattaches": rattaches,
        "datatourisme_lieux": len(lieux_dt), "datatourisme_rattaches": rattaches_dt,
        "sites_datatourisme": sum(1 for e in etablissements if e["site_source"] == "datatourisme"),
        "avec_site": avec_site,
        "emails_osm": len(emails),
        "emails_generiques": sum(1 for v in emails.values() if v[2] == "generique"),
    }
    print(json.dumps(resume, ensure_ascii=False, indent=1))
    if os.environ.get("RESUME"):
        with open(os.environ["RESUME"], "w") as f:
            json.dump(resume, f, ensure_ascii=False, indent=1)
    if apercu:
        return

    d1 = ouvrir_base()
    colonnes = ["siret", "siren", "nom", "raison_sociale", "naf", "categorie", "adresse", "code_postal",
                "commune", "code_commune", "departement", "lat", "lon", "tranche_effectif", "date_creation",
                "site_web", "site_source", "osm_id", "actif", "maj_sirene"]
    lignes = [(e["siret"], e["siren"], e["nom"], e["raison_sociale"], e["naf"], e["categorie"], e["adresse"],
               e["code_postal"], e["commune"], e["code_commune"], e["departement"], e["lat"], e["lon"],
               e["tranche_effectif"], e["date_creation"], e["site_web"], e["site_source"],
               e["osm_id"], 1, date) for e in etablissements]
    # Un site trouvé autrement (liste, saisie) n'est jamais remplacé par celui d'OSM / DATAtourisme.
    d1.inserer("etablissements", colonnes, lignes, """ON CONFLICT (siret) DO UPDATE SET
        nom = excluded.nom, raison_sociale = excluded.raison_sociale, naf = excluded.naf,
        categorie = excluded.categorie, adresse = excluded.adresse, code_postal = excluded.code_postal,
        commune = excluded.commune, code_commune = excluded.code_commune, departement = excluded.departement,
        lat = excluded.lat, lon = excluded.lon, tranche_effectif = excluded.tranche_effectif,
        site_web = CASE WHEN excluded.site_web IS NOT NULL
                             AND coalesce(etablissements.site_source, 'osm') IN ('osm', 'datatourisme')
                        THEN excluded.site_web ELSE etablissements.site_web END,
        site_source = CASE WHEN excluded.site_web IS NOT NULL
                               AND coalesce(etablissements.site_source, 'osm') IN ('osm', 'datatourisme')
                           THEN excluded.site_source ELSE etablissements.site_source END,
        osm_id = excluded.osm_id, actif = 1, maj_sirene = excluded.maj_sirene""")
    if emails:
        d1.inserer("emails", ["email", "domaine", "type", "statut", "verifie_le", "cree_le"],
                   list(emails.values()), "ON CONFLICT (email) DO UPDATE SET type = excluded.type")
        d1.inserer("email_sources", ["email", "siret", "source", "url", "consulte_le"], sources,
                   "ON CONFLICT (email, siret, source) DO UPDATE SET url = excluded.url, consulte_le = excluded.consulte_le")
    # Fermés ou sortis du périmètre depuis le dernier import de ces départements.
    deps = ", ".join(f"'{d}'" for d in departements)
    # Les fiches sans SIRET (« ext: », liste importée) ne viennent pas de SIRENE : jamais désactivées ici.
    d1.requete(f"UPDATE etablissements SET actif = 0 WHERE departement IN ({deps}) AND maj_sirene < ? AND actif = 1 "
               "AND siret NOT LIKE 'ext:%'",
               [date])
    resume["lignes_ecrites"] = d1.lignes_ecrites
    d1.journal("import_etablissements", resume)
    print(f"D1 : {d1.lignes_ecrites} lignes écrites")


if __name__ == "__main__":
    main()
