-- Base de prospects France Resto (Cloudflare D1 « franceresto-prospects », WEUR).
-- Hors de la base Supabase : aucune donnée d'utilisateur inscrit n'y est copiée,
-- seulement l'empreinte SHA-256 de leur e-mail (table exclusions, raison « client »).
-- Rejouable : CREATE … IF NOT EXISTS.

-- Établissements CHR actifs (SIRENE), complétés par OSM / DATAtourisme / les sites.
CREATE TABLE IF NOT EXISTS etablissements (
  siret            TEXT PRIMARY KEY,
  siren            TEXT NOT NULL,
  nom              TEXT,               -- enseigne ou dénomination usuelle, sinon raison sociale
  raison_sociale   TEXT,
  naf              TEXT NOT NULL,      -- 55.10Z, 56.10A, 56.10B, 56.10C, 56.21Z, 56.30Z
  categorie        TEXT NOT NULL,      -- hotel, restaurant, cafeteria, restauration_rapide, traiteur, bar
  adresse          TEXT,
  code_postal      TEXT,
  commune          TEXT,
  code_commune     TEXT,
  departement      TEXT NOT NULL,
  lat              REAL,
  lon              REAL,
  tranche_effectif TEXT,
  date_creation    TEXT,
  site_web         TEXT,
  site_source      TEXT,               -- osm, datatourisme, prompt, manuel
  osm_id           TEXT,
  actif            INTEGER NOT NULL DEFAULT 1,  -- 0 : fermé ou disparu de SIRENE
  maj_sirene       TEXT NOT NULL,      -- date de l'import qui l'a vu en dernier
  site_lu_le       TEXT,               -- dernière lecture du site (étape 2)
  site_statut      TEXT                -- ok, sans_email, injoignable, interdit_robots
);
CREATE INDEX IF NOT EXISTS etab_dep_cat ON etablissements (departement, categorie, actif);
CREATE INDEX IF NOT EXISTS etab_site    ON etablissements (site_lu_le) WHERE site_web IS NOT NULL;

-- Adresses e-mail trouvées (une ligne par adresse).
CREATE TABLE IF NOT EXISTS emails (
  email       TEXT PRIMARY KEY,        -- en minuscules
  domaine     TEXT NOT NULL,
  type        TEXT NOT NULL CHECK (type IN ('generique', 'nominatif')),
  statut      TEXT NOT NULL DEFAULT 'a_verifier'
              CHECK (statut IN ('a_verifier', 'valide', 'invalide', 'bounce', 'desinscrit', 'plainte')),
  verifie_le  TEXT,
  cree_le     TEXT NOT NULL
);

-- Origine de chaque adresse : obligatoire (information de la personne, art. 14 RGPD).
CREATE TABLE IF NOT EXISTS email_sources (
  email       TEXT NOT NULL REFERENCES emails (email),
  siret       TEXT NOT NULL REFERENCES etablissements (siret),
  source      TEXT NOT NULL CHECK (source IN ('osm', 'datatourisme', 'site', 'prompt', 'manuel')),
  url         TEXT,                    -- page exacte où l'adresse est publiée
  consulte_le TEXT NOT NULL,
  PRIMARY KEY (email, siret, source)
);
CREATE INDEX IF NOT EXISTS src_siret ON email_sources (siret);

-- Ne jamais écrire à ces adresses (empreinte SHA-256 de l'e-mail en minuscules).
-- Conservée sans limite de durée pour les refus ; « client » est recalculée chaque nuit.
CREATE TABLE IF NOT EXISTS exclusions (
  email_hash TEXT NOT NULL,
  raison     TEXT NOT NULL CHECK (raison IN ('client', 'desinscrit', 'bounce', 'plainte', 'manuel')),
  date       TEXT NOT NULL,
  PRIMARY KEY (email_hash, raison)
);

-- Campagnes lancées depuis le back-office (étape 3).
CREATE TABLE IF NOT EXISTS campagnes (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  nom         TEXT NOT NULL,
  modele      TEXT NOT NULL,           -- identifiant du modèle Brevo
  zone        TEXT,                    -- JSON { adresse, lat, lon, rayon_km } ou département
  categories  TEXT,                    -- JSON
  quota_jour  INTEGER NOT NULL DEFAULT 50,
  statut      TEXT NOT NULL DEFAULT 'brouillon'
              CHECK (statut IN ('brouillon', 'active', 'pause', 'terminee', 'arretee')),
  cree_le     TEXT NOT NULL,
  cree_par    TEXT
);

CREATE TABLE IF NOT EXISTS envois (
  campagne_id INTEGER NOT NULL REFERENCES campagnes (id),
  email       TEXT NOT NULL,
  siret       TEXT,
  etape       INTEGER NOT NULL DEFAULT 1, -- 1 : premier e-mail, 2 : relance
  envoye_le   TEXT NOT NULL,
  message_id  TEXT,
  statut      TEXT NOT NULL DEFAULT 'envoye'
              CHECK (statut IN ('envoye', 'delivre', 'ouvert', 'clique', 'bounce', 'plainte', 'desinscrit', 'inscrit')),
  PRIMARY KEY (campagne_id, email, etape)
);
CREATE INDEX IF NOT EXISTS envois_email ON envois (email);

-- Journal des imports et lectures.
CREATE TABLE IF NOT EXISTS journal (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  date    TEXT NOT NULL,
  tache   TEXT NOT NULL,
  detail  TEXT
);
