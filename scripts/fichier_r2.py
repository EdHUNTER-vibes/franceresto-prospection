"""Fichier de travail de la prospection : base SQLite complète, gardée dans le bucket R2 privé
« franceresto-prospects » (jamais dans le dépôt : il contient des adresses e-mail).

- `recuperer` : télécharge `prospects.sqlite.gz` vers BASE_FICHIER. S'il n'existe pas encore et qu'aucune
  archive n'existe non plus, le fichier est créé à partir de la base D1 (premier passage) ; s'il manque
  alors que des archives existent, arrêt : il faut restaurer une archive (voir PROSPECTION.md).
- `envoyer` : contrôle d'intégrité, compactage, puis envoi de `prospects.sqlite.gz` et d'une archive du
  jour de la semaine (`archives/prospects-<jour>.sqlite.gz`, 7 copies tournantes).

Variables : BASE_FICHIER, R2_ENDPOINT, R2_BUCKET, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY
(jeton R2 limité au bucket) ; pour la création à partir de D1, celles de D1.
"""

import gzip
import os
import shutil
import sqlite3
import sys
import time

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

sys.path.insert(0, os.path.dirname(__file__))
from commun import D1  # noqa: E402

CLE = "prospects.sqlite.gz"
JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
SCHEMAS = ["0001_schema.sql", "0002_listes_externes.sql", "0003_recherche_sites.sql"]
DOSSIER_SCHEMAS = os.path.join(os.path.dirname(__file__), "..", "d1")
TABLES = ["etablissements", "emails", "email_sources", "exclusions", "campagnes", "envois", "journal",
          "listes_externes"]


def client_r2():
    return boto3.client(
        "s3", endpoint_url=os.environ["R2_ENDPOINT"], region_name="auto",
        # R2 n'accepte pas toutes les sommes de contrôle ajoutées par défaut par les versions récentes de boto3
        config=Config(request_checksum_calculation="when_required", response_checksum_validation="when_required"))


def appliquer_schema(chemin: str):
    connexion = sqlite3.connect(chemin)
    for nom in SCHEMAS:
        with open(os.path.join(DOSSIER_SCHEMAS, nom)) as f:
            connexion.executescript(f.read())
    connexion.close()


def creer_depuis_d1(chemin: str):
    """Copie de toutes les tables de D1 (lecture seule de D1), page par page selon le rowid."""
    appliquer_schema(chemin)
    d1 = D1()
    connexion = sqlite3.connect(chemin)
    connexion.execute("PRAGMA foreign_keys = OFF")  # tables copiées dans un ordre quelconque
    for table in TABLES:
        colonnes = [c[1] for c in connexion.execute(f"PRAGMA table_info({table})")]
        dernier, total = 0, 0
        while True:
            lignes = d1.requete(f"SELECT rowid AS _r, {', '.join(colonnes)} FROM {table} "
                                "WHERE rowid > ? ORDER BY rowid LIMIT 2000", [dernier])
            if not lignes:
                break
            connexion.executemany(
                f"INSERT INTO {table} ({', '.join(colonnes)}) VALUES ({', '.join('?' * len(colonnes))})",
                [[l[c] for c in colonnes] for l in lignes])
            dernier = lignes[-1]["_r"]
            total += len(lignes)
        print(f"{table} : {total} lignes copiées depuis D1")
    connexion.commit()
    probleme = connexion.execute("PRAGMA foreign_key_check").fetchall()
    connexion.close()
    if probleme:
        raise RuntimeError(f"Clés étrangères incohérentes dans la copie : {probleme[:5]}")


def recuperer(chemin: str):
    r2, bucket = client_r2(), os.environ["R2_BUCKET"]
    try:
        r2.download_file(bucket, CLE, chemin + ".gz")
    except ClientError as e:
        if e.response["Error"]["Code"] not in ("404", "NoSuchKey"):
            raise
        archives = r2.list_objects_v2(Bucket=bucket, Prefix="archives/").get("KeyCount", 0)
        if archives:
            raise SystemExit(f"{CLE} absent alors que {archives} archive(s) existent : restaurer une archive "
                             "(voir le README) au lieu de repartir de D1.")
        print(f"{CLE} absent : premier passage, création à partir de D1")
        creer_depuis_d1(chemin)
        return
    with gzip.open(chemin + ".gz", "rb") as src, open(chemin, "wb") as dst:
        shutil.copyfileobj(src, dst)
    os.remove(chemin + ".gz")
    appliquer_schema(chemin)  # tables ou index ajoutés depuis (rejouable)
    print(f"{CLE} récupéré ({os.path.getsize(chemin) // 1024} Ko)")


def envoyer(chemin: str):
    connexion = sqlite3.connect(chemin)
    if connexion.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise SystemExit("Fichier de travail corrompu : pas d'envoi")
    if connexion.execute("PRAGMA foreign_key_check").fetchall():
        raise SystemExit("Clés étrangères incohérentes : pas d'envoi")
    comptes = {t: connexion.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in TABLES}
    connexion.execute("VACUUM")
    connexion.close()
    with open(chemin, "rb") as src, gzip.open(chemin + ".gz", "wb", compresslevel=9) as dst:
        shutil.copyfileobj(src, dst)
    r2, bucket = client_r2(), os.environ["R2_BUCKET"]
    jour = JOURS[time.gmtime().tm_wday]
    for cle in (CLE, f"archives/prospects-{jour}.sqlite.gz"):
        r2.upload_file(chemin + ".gz", bucket, cle, ExtraArgs={"ContentType": "application/gzip"})
    print(f"Envoyé ({os.path.getsize(chemin + '.gz') // 1024} Ko compressé, archive « {jour} ») : {comptes}")


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else ""
    if action not in ("recuperer", "envoyer"):
        raise SystemExit("Usage : fichier_r2.py recuperer|envoyer")
    {"recuperer": recuperer, "envoyer": envoyer}[action](os.environ["BASE_FICHIER"])
